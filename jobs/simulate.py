"""
Monte Carlo game simulation for a slate (Phase 3).

For every game on the slate, each simulation draws:
  1. the score (margin ~ N(spread, 13.5), total ~ N(total, 10.5)) -> team points
  2. plays and pass rate (team pace, shifted by game script: trailing teams pass more)
  3. team touchdowns ~ Poisson(points / 7.3), split into pass / rush TDs
  4. each player's target / carry share (noisy around their recent usage; OUT players
     are removed and their share flows to teammates; D/Q players are active in 50% / 90% of sims)
  5. targets, receptions, carries, yards, TDs, INTs, fumbles per player
  6. DK and FD fantasy points, including yardage bonuses
DST scoring comes from the opponent's simulated points, sacks, INTs and turnovers.
Because every player's line comes from the same drawn game, outcomes are correlated:
a QB's big day is his receivers' big day, a blowout feeds the favourite's RB, etc.

Outputs:
  * slate_projections rows with method = 'sim' (the board prefers these over baseline)
  * a gzip JSON player x sim matrix (STORE_SIMS columns) in the public 'sims' storage
    bucket, which the lineup builder uses to score whole lineups
  * slates.sim_meta

Usage:
  python jobs/simulate.py --slate-key DK-2026-02-main [--sims 10000]
  python jobs/simulate.py --latest
  python jobs/simulate.py --slate-key DK-2026-02-main --dry-run     # no writes; prints checks
"""
from __future__ import annotations

import argparse
import datetime as dt
import gzip
import json
import math
from collections import defaultdict

import numpy as np

from common import chunked, fetch_all, get_client

N_SIMS = 10000
DIAG = {}
COMPONENTS = None      # set to {} to collect per-player mean stat lines (backtest diagnostics)
STORE_SIMS = 2000
DECAY, PRIOR_SEASON_W, MAX_GAMES = 0.90, 0.5, 20
MARGIN_SD, TOTAL_SD, PLAYS_SD = 13.5, 10.5, 5.0
PTS_PER_TD = 8.8                   # calibrated on 2025 wk 5-18 backtest (QB pass-TD bias -> 0)
SCRIPT_PASS_SLOPE = 0.005          # pass-rate change per point of deficit
ACTIVE_PROB = {"OUT": 0.0, "IR": 0.0, "O": 0.0, "D": 0.5, "Q": 0.9}
FUMBLE_RATE = 0.010                # lost fumbles per touch
OTHER_TGT = (0.05, 0.15)           # share of targets left for players not on the slate (min, max)
OTHER_CAR = (0.03, 0.12)
YDS_BASE, YDS_PER_PT, YDS_SD = 140.0, 8.5, 45.0   # team yards ~ N(base + per_pt * points, sd)
OTHER_YPT = 0.65 * 9.0             # yards per target for the "other" receiving bucket
RB_TD_PRIOR, QB_TD_PRIOR = 0.03, 0.045
SHARE_CV_MIN = 0.15                # floor on per-sim usage-share noise (coefficient of variation)
YDS_COUPLING = 1.0                 # 1 = force team yards to the score-implied total, 0.5 = half-way

# position priors for regression
POS = {
    "catch_rate": {"RB": 0.76, "WR": 0.64, "TE": 0.70, "QB": 0.6},
    "ypr":        {"RB": 8.0,  "WR": 13.0, "TE": 11.5, "QB": 8.0},
    "ypc":        {"RB": 4.3,  "WR": 6.0,  "TE": 4.0,  "QB": 5.0},
    "rec_td":     {"RB": 0.035, "WR": 0.055, "TE": 0.08, "QB": 0.03},    # per target
    "rush_td":    {"RB": 0.03, "WR": 0.03,  "TE": 0.03,  "QB": 0.045},   # per carry
}
K = {"catch_rate": 20, "ypr": 15, "ypc": 40, "rec_td": 60, "rush_td": 80}
BOOM = {"QB": 25, "RB": 20, "WR": 20, "TE": 15, "DST": 12}
BUST = {"QB": 12, "RB": 7, "WR": 6, "TE": 4, "DST": 3}
PA_TIERS = [(0, 10), (6, 7), (13, 4), (20, 1), (27, 0), (34, -1), (10 ** 9, -4)]


# ------------------------------------------------------------------ data
def load_slate(client, slate_key, latest):
    q = client.table("slates").select("*")
    q = q.eq("slate_key", slate_key) if slate_key else q.order("imported_at", desc=True).limit(1)
    rows = q.execute().data
    if not rows:
        raise SystemExit(f"slate not found: {slate_key or '(latest)'}")
    slate = rows[0]
    sal = fetch_all(client.table("slate_salaries").select("*").eq("slate_id", slate["slate_id"]), order="site_player_id")
    return slate, sal


def load_context(client, season, week, player_ids):
    ctx = {"games": fetch_all(client.table("games").select("*").in_("season", [season, season - 1]), order="game_id")}
    ctx["dvp"] = fetch_all(client.table("defense_vs_position").select("*")
                           .in_("season", [season, season - 1]).eq("season_type", "REG"),
                           order=["defense", "season", "position"])
    ctx["team"] = fetch_all(client.table("team_game_stats").select("*")
                            .in_("season", [season, season - 1]).eq("season_type", "REG"),
                            order=["team", "season", "week"])
    cols = ("player_id,season,week,team,position,targets,receptions,receiving_yards,receiving_tds,"
            "carries,rushing_yards,rushing_tds,attempts,passing_yards,passing_tds,interceptions")
    logs = []
    for batch in chunked(sorted(set(player_ids)), 200):
        logs += fetch_all(client.table("player_game_stats").select(cols)
                          .in_("player_id", batch).in_("season", [season, season - 1]).eq("season_type", "REG"),
                          order=["player_id", "season", "week"])
    ctx["logs"] = logs
    return ctx


# ------------------------------------------------------------------ parameter estimation
def weights(n, seasons, season):
    return np.array([DECAY ** i * (PRIOR_SEASON_W if s < season else 1.0) for i, s in enumerate(seasons[:n])])


def wmean(vals, w):
    vals = np.asarray(vals, float)[:len(w)]
    return float((vals * w).sum() / w.sum()) if w.sum() else 0.0


def wsd(vals, w, m):
    vals = np.asarray(vals, float)[:len(w)]
    return float(math.sqrt(((vals - m) ** 2 * w).sum() / w.sum())) if w.sum() else 0.0


def ratio(num, den, w, prior, k):
    """Weighted ratio regressed toward prior with k pseudo-denominator units."""
    num, den = np.asarray(num, float)[:len(w)], np.asarray(den, float)[:len(w)]
    n, d = (num * w).sum(), (den * w).sum()
    return (n + k * prior) / (d + k) if (d + k) > 0 else prior


def team_params(team_rows, season):
    """Per team: pace, pass rate, sack/INT rates, TD split, targets per attempt."""
    by_team = defaultdict(list)
    for r in team_rows:
        by_team[r["team"]].append(r)
    out, games_by_key = {}, {}
    for t, rows in by_team.items():
        rows.sort(key=lambda r: (r["season"], r["week"]), reverse=True)
        rows = rows[:MAX_GAMES]
        for r in rows:
            games_by_key[(t, r["season"], r["week"])] = r
        w = weights(len(rows), [r["season"] for r in rows], season)
        g = lambda c: np.array([float(r[c] or 0) for r in rows])
        att, car, sck, tgt = g("attempts"), g("carries"), g("sacks"), g("targets")
        plays = att + car + sck
        ptd, rtd, ints = g("passing_tds"), g("rushing_tds"), g("interceptions")
        pm = wmean(plays, w)
        out[t] = {
            "plays": pm, "plays_sd": max(wsd(plays, w, pm), 4.0),
            "pass_rate": ratio(att + sck, plays, w, 0.58, 60),
            "sack_rate": ratio(sck, att + sck, w, 0.065, 200),
            "int_rate": ratio(ints, att, w, 0.022, 300),
            "pass_td_share": ratio(ptd, ptd + rtd, w, 0.58, 12),
            "tgt_per_att": ratio(tgt, att, w, 0.97, 100),
        }
    return out, games_by_key


def player_params(logs, games_by_key, season, current_team):
    team_latest = {}
    for (t, se, wk) in games_by_key:
        if (se, wk) > team_latest.get(t, (0, 0)):
            team_latest[t] = (se, wk)
    """Per player usage/efficiency. Usage shares come from games with the player's CURRENT
    team only (old-team usage is meaningless after a move); efficiency uses all games."""
    by_p = defaultdict(list)
    for r in logs:
        by_p[r["player_id"]].append(r)
    out = {}
    for pid, rows in by_p.items():
        rows.sort(key=lambda r: (r["season"], r["week"]), reverse=True)
        rows = rows[:MAX_GAMES]
        pos = rows[0]["position"] if rows[0]["position"] in POS["ypr"] else "WR"
        team = current_team.get(pid)
        usage_rows = [r for r in rows if r["team"] == team] or rows

        def shares(rs):
            w = weights(len(rs), [r["season"] for r in rs], season)
            g = lambda c: np.array([float(r[c] or 0) for r in rs])
            tt = np.array([float((games_by_key.get((r["team"], r["season"], r["week"]), {}).get("targets") or 30)) for r in rs])
            tc = np.array([float((games_by_key.get((r["team"], r["season"], r["week"]), {}).get("carries") or 26)) for r in rs])
            ta = np.array([float((games_by_key.get((r["team"], r["season"], r["week"]), {}).get("attempts") or 33)) for r in rs])
            ts, cs, as_ = g("targets") / tt, g("carries") / tc, g("attempts") / ta
            return w, ts, cs, as_, g("carries")

        w_u, ts, cs, as_, car_u = shares(usage_rows)
        tsm, csm = wmean(ts, w_u), wmean(cs, w_u)
        if usage_rows is rows and team and rows[0]["team"] != team:
            tsm, csm = 0.7 * tsm, 0.7 * csm        # moved teams, no games there yet: haircut old-team usage
        # bench haircut: shares from scattered fill-in weeks overstate a player's role once the
        # starters are all active. If he didn't appear in the team's most recent game, halve it.
        latest = team_latest.get(team)
        if latest and (usage_rows[0]["season"], usage_rows[0]["week"]) != latest:
            tsm, csm = 0.5 * tsm, 0.5 * csm
        sd_floor = 0.35 if len(usage_rows) < 3 else 0.25

        w = weights(len(rows), [r["season"] for r in rows], season)
        g = lambda c: np.array([float(r[c] or 0) for r in rows])
        tgt, rec, ryd, rtd = g("targets"), g("receptions"), g("receiving_yards"), g("receiving_tds")
        car, cyd, ctd = g("carries"), g("rushing_yards"), g("rushing_tds")
        out[pid] = {
            "pos": pos, "n": len(rows), "n_team": len(usage_rows),
            "tgt_share": tsm, "tgt_share_sd": max(wsd(ts, w_u, tsm), sd_floor * tsm, 0.01),
            "carry_share": csm, "carry_share_sd": max(wsd(cs, w_u, csm), sd_floor * csm, 0.01),
            "att_share": wmean(as_, w_u), "att_share_recent": float(as_[0]) if len(as_) else 0.0,
            "qb_carries": wmean(car_u, w_u) if pos == "QB" else 0.0,
            "catch_rate": ratio(rec, tgt, w, POS["catch_rate"][pos], K["catch_rate"]),
            "ypr": ratio(ryd, rec, w, POS["ypr"][pos], K["ypr"]),
            "ypc": ratio(cyd, car, w, POS["ypc"][pos], K["ypc"]),
            "rec_td": ratio(rtd, tgt, w, POS["rec_td"][pos], K["rec_td"]),
            "rush_td": ratio(ctd, car, w, (QB_TD_PRIOR if pos == "QB" else RB_TD_PRIOR if pos == "RB" else POS["rush_td"][pos]), K["rush_td"]),
        }
    return out


def dvp_factors(dvp, season):
    cur = {(d["defense"], d["position"]): d for d in dvp if d["season"] == season}
    prev = {(d["defense"], d["position"]): d for d in dvp if d["season"] == season - 1}
    blended, by_pos = {}, defaultdict(list)
    for key in set(cur) | set(prev):
        c, p = cur.get(key), prev.get(key)
        gc = float(c["games"]) if c else 0.0
        vc = float(c["dk_allowed_pg"]) if c else 0.0
        vp = float(p["dk_allowed_pg"]) if p else vc
        blended[key] = (gc * vc + 4.0 * vp) / (gc + 4.0)
        by_pos[key[1]].append(blended[key])
    avg = {pos: float(np.mean(v)) for pos, v in by_pos.items()}
    return {k: min(1.15, max(0.85, 1 + 0.5 * (v / avg[k[1]] - 1))) for k, v in blended.items()}


# ------------------------------------------------------------------ sim helpers
def allocate(counts, weights_):
    """counts: (N,) ints; weights_: (N, P) >= 0. Returns (N, P) ints — each count assigned
    to one column with probability proportional to its weight (vectorised multinomial)."""
    N, P = weights_.shape
    out = np.zeros((N, P), dtype=np.int32)
    tot = weights_.sum(axis=1, keepdims=True)
    ok = tot[:, 0] > 0
    if not ok.any():
        return out
    cum = np.cumsum(weights_[ok] / tot[ok], axis=1)
    kmax = int(counts.max()) if counts.size else 0
    idx_ok = np.where(ok)[0]
    for k in range(kmax):
        live = counts[ok] > k
        if not live.any():
            break
        u = np.random.random(live.sum())
        col = (cum[live] < u[:, None]).sum(axis=1)
        col = np.minimum(col, P - 1)
        out[idx_ok[live], col] += 1
    return out


def pa_tier(pts):
    out = np.full(pts.shape, -4.0)
    for upper, v in reversed(PA_TIERS):
        out[pts <= upper] = v
    return out


def lognoise(mu, sd, n):
    """Multiplicative share noise: mu * exp(N(0, cv)) with the mean preserved (no clipping bias)."""
    cv = np.clip(sd / np.maximum(mu, 1e-6), SHARE_CV_MIN, 0.6)
    z = np.random.normal(0, 1, (n, len(mu)))
    return mu * np.exp(cv * z - 0.5 * cv ** 2)


def clipnorm(mean, sd, lo=0.0):
    return np.maximum(lo, np.random.normal(mean, np.maximum(sd, 1e-6)))


# ------------------------------------------------------------------ the simulation
def simulate(slate, salaries, ctx, n_sims):
    np.random.seed(int(dt.datetime.now().timestamp()) % 2 ** 31)
    season, week, site = slate["season"], slate["week"], slate["site"]
    tp, gkey = team_params(ctx["team"], season)
    pp = player_params(ctx["logs"], gkey, season, {s["player_id"]: s["team"] for s in salaries if s["player_id"]})
    dvp = dvp_factors(ctx["dvp"], season)

    games = [g for g in ctx["games"] if g["season"] == season and g["week"] == week
             and g["game_id"] in set(slate.get("game_ids") or []) and g["total_line"] is not None]
    if not games:
        raise SystemExit("no games with lines for this slate")

    # slate players grouped by team
    by_team = defaultdict(list)
    for s in salaries:
        by_team[s["team"]].append(s)

    N = n_sims
    scores_dk, scores_fd = {}, {}      # site_player_id -> (N,) arrays
    team_pts_sim, team_sacks, team_ints, team_fum = {}, {}, {}, {}   # what the DEFENSE will need

    for g in games:
        home, away = g["home_team"], g["away_team"]
        spread, total = float(g["spread_line"]), float(g["total_line"])
        margin = np.random.normal(spread, MARGIN_SD, N)
        tot = np.maximum(np.random.normal(total, TOTAL_SD, N), 17)
        pts = {home: np.maximum((tot + margin) / 2, 0), away: np.maximum((tot - margin) / 2, 0)}

        for team, opp in ((home, away), (away, home)):
            T = tp.get(team) or {"plays": 63, "plays_sd": 5, "pass_rate": 0.58, "sack_rate": 0.065,
                                 "int_rate": 0.022, "pass_td_share": 0.58, "tgt_per_att": 0.97}
            own, other = pts[team], pts[opp]
            plays = np.maximum(np.random.normal(T["plays"], T["plays_sd"], N) + 0.15 * (tot - total), 40)
            pass_rate = np.clip(T["pass_rate"] + SCRIPT_PASS_SLOPE * (other - own) + np.random.normal(0, 0.03, N), 0.35, 0.78)
            dropbacks = np.round(plays * pass_rate).astype(int)
            sacks = np.random.binomial(dropbacks, T["sack_rate"])
            att = dropbacks - sacks
            rushes = np.maximum(np.round(plays).astype(int) - dropbacks, 5)
            tds = np.random.poisson(np.maximum(own, 0) / PTS_PER_TD)
            pass_tds = np.random.binomial(tds, np.clip(T["pass_td_share"], 0.3, 0.8))
            rush_tds = tds - pass_tds
            ints = np.random.binomial(att, T["int_rate"])
            targets_total = np.round(att * T["tgt_per_att"]).astype(int)

            roster = [s for s in by_team.get(team, []) if s["position"] != "DST"]
            modeled = [s for s in roster if s["player_id"] in pp]
            # starting QB: highest recent attempt share among non-OUT QBs, else highest salary
            qbs = [s for s in roster if s["position"] == "QB" and ACTIVE_PROB.get((s["status"] or "").upper(), 1.0) > 0]
            qb = None
            if qbs:
                qb = max(qbs, key=lambda s: (pp.get(s["player_id"], {}).get("att_share_recent", 0), s["salary"]))
                if pp.get(qb["player_id"], {}).get("att_share_recent", 0) < 0.3:
                    qb = max(qbs, key=lambda s: s["salary"])

            # active masks (injury designations)
            act = {}
            for s in modeled:
                p_act = ACTIVE_PROB.get((s["status"] or "").upper(), 1.0)
                act[s["site_player_id"]] = (np.random.random(N) < p_act) if p_act < 1 else np.ones(N, bool)

            # ---- receiving shares
            recv = [s for s in modeled if s["position"] in ("RB", "WR", "TE")]
            P = len(recv)
            if P:
                mu = np.array([pp[s["player_id"]]["tgt_share"] for s in recv])
                sd = np.array([pp[s["player_id"]]["tgt_share_sd"] for s in recv])
                mu_t = mu
                sh = lognoise(mu, sd, N) * np.stack([act[s["site_player_id"]] for s in recv], 1)
                other_sh = float(np.clip(1 - mu.sum(), *OTHER_TGT))
                sh = sh / (sh.sum(1, keepdims=True) + other_sh)
                tgts = allocate(targets_total, np.concatenate([sh, np.full((N, 1), other_sh)], 1))[:, :P]
                cr = np.array([pp[s["player_id"]]["catch_rate"] for s in recv])
                recs = np.random.binomial(tgts, np.clip(cr, 0.3, 0.95))
                ypr = np.array([pp[s["player_id"]]["ypr"] * dvp.get((opp, s["position"]), 1.0) for s in recv])
                ryds = np.where(recs > 0, clipnorm(recs * ypr, np.sqrt(np.maximum(recs, 1)) * ypr * 1.1), 0)
                tdw = sh * np.array([pp[s["player_id"]]["rec_td"] for s in recv]) / 0.05
                rtds = allocate(pass_tds, np.concatenate([tdw, np.full((N, 1), other_sh)], 1))[:, :P]
            else:
                tgts = recs = ryds = rtds = np.zeros((N, 0), int)

            # ---- rushing shares: RB/WR/TE plus the starting QB, all from the same pool of rushes
            rush = [s for s in modeled if s["position"] in ("RB", "WR", "TE")]
            qb_mod = qb if (qb is not None and qb["player_id"] in pp) else None
            rushers = rush + ([qb_mod] if qb_mod else [])
            R = len(rush)
            if rushers:
                mu = np.array([pp[s["player_id"]]["carry_share"] for s in rushers])
                sd = np.array([pp[s["player_id"]]["carry_share_sd"] for s in rushers])
                csh = lognoise(mu, sd, N) * np.stack([act[s["site_player_id"]] for s in rushers], 1)
                other_c = float(np.clip(1 - mu.sum(), *OTHER_CAR))
                csh = csh / (csh.sum(1, keepdims=True) + other_c)
                cars_all = allocate(rushes, np.concatenate([csh, np.full((N, 1), other_c)], 1))
                cars, qb_car = cars_all[:, :R], (cars_all[:, R] if qb_mod else np.zeros(N, int))
                other_car = cars_all[:, -1]
                ypc_all = np.array([pp[s["player_id"]]["ypc"] * dvp.get((opp, s["position"]), 1.0) for s in rushers])
                cyds_all = np.where(cars_all[:, :-1] > 0, clipnorm(cars_all[:, :-1] * ypc_all, np.sqrt(np.maximum(cars_all[:, :-1], 1)) * ypc_all * 1.5, lo=-5), 0)
                cyds, qb_cyd = cyds_all[:, :R], (cyds_all[:, R] if qb_mod else np.zeros(N))
                rw = csh * np.array([pp[s["player_id"]]["rush_td"] for s in rushers]) / 0.03
                ctds_all = allocate(rush_tds, np.concatenate([rw, np.full((N, 1), other_c)], 1))
                ctds, qb_ctds = ctds_all[:, :R], (ctds_all[:, R] if qb_mod else np.zeros(N, int))
            else:
                cars = cyds = ctds = np.zeros((N, 0), int)
                qb_car, qb_cyd, qb_ctds, other_car = np.zeros(N, int), np.zeros(N), np.zeros(N, int), rushes

            # ---- tie yardage to the simulated score: NFL teams gain ~140 + 8.5 yds per point
            other_tg = np.maximum(targets_total - (tgts.sum(1) if P else 0), 0)
            other_yds = clipnorm(other_tg * OTHER_YPT, np.sqrt(np.maximum(other_tg, 1)) * 9.0)
            raw_total = (ryds.sum(1) if P else 0) + other_yds + (cyds.sum(1) if R else 0) + qb_cyd + other_car * 4.2
            target_yds = clipnorm(YDS_BASE + YDS_PER_PT * own, YDS_SD, lo=120)
            f = (np.clip(target_yds / np.maximum(raw_total, 80), 0.6, 1.5) ** YDS_COUPLING)[:, None]
            if P:
                ryds = ryds * f
            if R:
                cyds = cyds * f
            other_yds, qb_cyd = other_yds * f[:, 0], qb_cyd * f[:, 0]

            # ---- assemble per-player stat lines -> points
            def score(pyd, ptd, pint, cyd, ctd, rec, ryd, rtd, fum):
                dk = (pyd * 0.04 + ptd * 4 - pint + 3 * (pyd >= 300) + cyd * 0.1 + ctd * 6 + 3 * (cyd >= 100)
                      + rec + ryd * 0.1 + rtd * 6 + 3 * (ryd >= 100) - fum)
                fd = (pyd * 0.04 + ptd * 4 - pint + cyd * 0.1 + ctd * 6 + rec * 0.5 + ryd * 0.1 + rtd * 6 - 2 * fum)
                return dk, fd

            for i, s in enumerate(recv):
                j = rush.index(s)
                touches = recs[:, i] + cars[:, j]
                fum = np.random.binomial(touches, FUMBLE_RATE)
                dk, fd = score(0, 0, 0, cyds[:, j], ctds[:, j], recs[:, i], ryds[:, i], rtds[:, i], fum)
                scores_dk[s["site_player_id"]], scores_fd[s["site_player_id"]] = dk, fd
                if COMPONENTS is not None:
                    COMPONENTS[s["site_player_id"]] = {"targets": tgts[:, i].mean(), "receptions": recs[:, i].mean(),
                        "receiving_yards": ryds[:, i].mean(), "receiving_tds": rtds[:, i].mean(),
                        "carries": cars[:, j].mean(), "rushing_yards": cyds[:, j].mean(), "rushing_tds": ctds[:, j].mean()}

            # QB line: passing yards = all receiving yards incl. the "other" bucket
            pass_yds = (ryds.sum(1) if P else 0) + other_yds
            for s in roster:
                if s["position"] != "QB":
                    continue
                if qb_mod is not None and s["site_player_id"] == qb_mod["site_player_id"]:
                    a = act[s["site_player_id"]]
                    fum = np.random.binomial(sacks + qb_car, FUMBLE_RATE * 1.5)
                    dk, fd = score(pass_yds * a, pass_tds * a, ints * a, qb_cyd * a, qb_ctds * a, 0, 0, 0, fum * a)
                    scores_dk[s["site_player_id"]], scores_fd[s["site_player_id"]] = dk, fd
                    if COMPONENTS is not None:
                        COMPONENTS[s["site_player_id"]] = {"attempts": (att * a).mean(), "passing_yards": (pass_yds * a).mean(),
                            "passing_tds": (pass_tds * a).mean(), "interceptions": (ints * a).mean(),
                            "carries": (qb_car * a).mean(), "rushing_yards": (qb_cyd * a).mean(), "rushing_tds": (qb_ctds * a).mean()}
                elif s["player_id"] in pp:
                    scores_dk[s["site_player_id"]] = scores_fd[s["site_player_id"]] = np.zeros(N)

            DIAG[team] = {"tgt_mu_sum": float(mu_t.sum()) if P else 0, "other_sh": other_sh if P else 1, "targets_total": float(targets_total.mean()),
                          "modeled_tgts": float(tgts.sum(1).mean()) if P else 0, "pts": own.mean(), "plays": plays.mean(), "att": att.mean(), "rushes": rushes.mean(),
                          "pass_rate": pass_rate.mean(), "tds": tds.mean(), "pass_tds": pass_tds.mean(),
                          "pass_yds": pass_yds.mean(), "qb": qb["player_name"] if qb else None, "qb_car": qb_car.mean(),
                          "recv": [(s["player_name"], round(float(pp[s["player_id"]]["tgt_share"]), 3), round(float(tgts[:, i].mean()), 1),
                                    round(float(recs[:, i].mean()), 1), round(float(ryds[:, i].mean()), 1), round(float(rtds[:, i].mean()), 2)) for i, s in enumerate(recv)],
                          "rush": [(s["player_name"], round(float(pp[s["player_id"]]["carry_share"]), 3), round(float(cars[:, j].mean()), 1),
                                    round(float(cyds[:, j].mean()), 1), round(float(ctds[:, j].mean()), 2)) for j, s in enumerate(rush)]}
            team_pts_sim[team], team_sacks[team], team_ints[team] = own, sacks, ints
            team_fum[team] = np.random.binomial(np.round(plays).astype(int), 0.006)

        # ---- DST for both teams
        for team, opp in ((home, away), (away, home)):
            dst = next((s for s in by_team.get(team, []) if s["position"] == "DST"), None)
            if dst is None:
                continue
            pa = pts[opp]
            td = np.random.random(N) < 0.07
            safety = np.random.random(N) < 0.025
            blk = np.random.random(N) < 0.03
            base = team_sacks[opp] * 1 + team_ints[opp] * 2 + team_fum[opp] * 2 + td * 6 + safety * 2 + blk * 2
            dk = base + pa_tier(np.round(pa))
            scores_dk[dst["site_player_id"]] = dk
            scores_fd[dst["site_player_id"]] = dk        # FD DST scoring is the same tiers/values

    return (scores_dk if site == "DK" else scores_fd), scores_dk, scores_fd


# ------------------------------------------------------------------ output
def summarize(slate, salaries, scores):
    sal = {s["site_player_id"]: s for s in salaries}
    rows, now = [], dt.datetime.now(dt.timezone.utc).isoformat()
    for pid, x in scores.items():
        s = sal[pid]
        pos = s["position"]
        q = np.percentile(x, [10, 15, 50, 85, 90, 95])
        rows.append({
            "slate_id": slate["slate_id"], "site_player_id": pid, "player_id": s["player_id"],
            "player_name": s["player_name"], "position": pos, "team": s["team"], "opponent": s["opponent"],
            "salary": s["salary"], "method": "sim",
            "mean": round(float(x.mean()), 2), "stdev": round(float(x.std()), 2),
            "median": round(float(q[2]), 2), "p15": round(float(q[1]), 2), "p85": round(float(q[3]), 2),
            "p95": round(float(q[5]), 2), "floor": round(float(q[0]), 2), "ceiling": round(float(q[4]), 2),
            "boom_prob": round(float((x >= BOOM[pos]).mean()), 3), "bust_prob": round(float((x <= BUST[pos]).mean()), 3),
            "games_used": None, "components": {"n_sims": int(x.size), "status": s["status"]},
            "updated_at": now,
        })
    return rows


def matrix_blob(slate, scores, n_store):
    ids = sorted(scores)
    mat = np.stack([scores[i][:n_store] for i in ids]).round(1)
    payload = {"slate_key": slate["slate_key"], "n": int(mat.shape[1]), "players": ids, "scores": mat.tolist(),
               "generated_at": dt.datetime.now(dt.timezone.utc).isoformat()}
    return gzip.compress(json.dumps(payload, separators=(",", ":")).encode())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--slate-key")
    ap.add_argument("--latest", action="store_true")
    ap.add_argument("--sims", type=int, default=N_SIMS)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    client = get_client(need_write=not args.dry_run)
    slate, salaries = load_slate(client, args.slate_key, args.latest)
    print(f"simulating {slate['slate_key']}: {len(salaries)} players, {args.sims} sims")
    pids = [s["player_id"] for s in salaries if s["player_id"] and not s["player_id"].startswith("DST_")]
    ctx = load_context(client, slate["season"], slate["week"], pids)
    scores, dk, fd = simulate(slate, salaries, ctx, args.sims)
    rows = summarize(slate, salaries, scores)
    blob = matrix_blob(slate, scores, STORE_SIMS)

    for r in sorted(rows, key=lambda r: -r["mean"])[:15]:
        print(f"  {r['position']:3} {r['team']:4} {r['player_name']:<24} ${r['salary']:<5} "
              f"{r['mean']:5.1f} ± {r['stdev']:4.1f}  p85 {r['p85']:5.1f}  boom {r['boom_prob']:.2f}")
    print(f"  {len(rows)} players simulated, matrix {len(blob) / 1e6:.2f} MB gz")

    if args.dry_run:
        json.dump(DIAG, open(f"diag_{slate['slate_key']}.json", "w"), indent=1, default=str)
        json.dump(rows, open(f"sim_{slate['slate_key']}.json", "w"), indent=1)
        open(f"sim_{slate['slate_key']}.json.gz", "wb").write(blob)
        print("dry run — wrote local files")
        return

    for batch in chunked(rows):
        client.table("slate_projections").upsert(batch, on_conflict="slate_id,site_player_id,method").execute()
    path = f"{slate['slate_key']}.json.gz"
    client.storage.from_("sims").upload(path, blob, {"content-type": "application/gzip", "upsert": "true",
                                                      "cache-control": "300"})
    url = client.storage.from_("sims").get_public_url(path)
    client.table("slates").update({"sim_meta": {"n_sims": args.sims, "stored": STORE_SIMS, "url": url,
                                                "players": len(rows), "run_at": rows[0]["updated_at"]}}) \
        .eq("slate_id", slate["slate_id"]).execute()
    print(f"done — {len(rows)} sim projections written, matrix at {url}")


if __name__ == "__main__":
    main()
