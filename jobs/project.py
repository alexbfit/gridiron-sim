"""
Baseline projections for a slate (Phase 2). Writes one row per slate player to
slate_projections (method = 'baseline'). Phase 3's Monte Carlo sim will write the
same table with method = 'sim'.

Method, per player:
  1. Weighted game log: recent games weigh more (0.85^games_ago), prior season x0.5.
  2. Regress toward a position/salary prior (k = 3 pseudo-games).
  3. Multiply by an opponent factor (defense_vs_position, shrunk 50%, clamped)
     and a Vegas factor (implied team total vs slate average, shrunk 50%); the
     product of the two is capped to [0.8, 1.2].
  4. Injury status: OUT/IR -> 0, D -> x0.5, Q -> x0.95.
  5. Distribution: player game-to-game stdev shrunk toward a position default;
     percentiles from a normal clipped at zero.
DST is projected from what its opponents have given up (sacks, INTs) plus the
expected points-allowed tier from the opponent's implied total.

Usage:
  python jobs/project.py --slate-key DK-2026-02-main
  python jobs/project.py --latest                 # most recently imported slate
  python jobs/project.py --dry-run slate_DK-2026-02-main.json   # from import --dry-run
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import statistics
from collections import defaultdict

from common import chunked, fetch_all, get_client

DECAY = 0.85          # per game ago
PRIOR_SEASON_W = 0.5
K_PRIOR = 3.0         # pseudo-games of regression to the salary prior
MAX_GAMES = 20
POS_SD = {"QB": 7.0, "RB": 7.0, "WR": 7.0, "TE": 5.5, "DST": 5.0}
BOOM = {"QB": 25, "RB": 20, "WR": 20, "TE": 15, "DST": 12}
BUST = {"QB": 12, "RB": 7, "WR": 6, "TE": 4, "DST": 3}
STATUS_MULT = {"OUT": 0.0, "IR": 0.0, "O": 0.0, "D": 0.5, "Q": 0.95}
PA_TIERS = [(0, 10), (6, 7), (13, 4), (20, 1), (27, 0), (34, -1), (10 ** 9, -4)]


# ------------------------------------------------------------------ math helpers
def norm_cdf(x: float) -> float:
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def wmean_wvar(vals, weights):
    sw = sum(weights)
    if sw == 0:
        return 0.0, 0.0, 0.0
    m = sum(v * w for v, w in zip(vals, weights)) / sw
    var = sum(w * (v - m) ** 2 for v, w in zip(vals, weights)) / sw
    return m, var, sw


def pa_tier_expectation(implied_pts: float, sd: float = 9.0) -> float:
    """Expected DST points-allowed score if points allowed ~ N(implied, sd)."""
    exp, prev = 0.0, -1e9
    lo = -1e9
    for upper, pts in PA_TIERS:
        p = norm_cdf((upper + 0.5 - implied_pts) / sd) - norm_cdf((lo + 0.5 - implied_pts) / sd)
        exp += p * pts
        lo = upper
    return exp


def distribution(mean: float, sd: float, pos: str) -> dict:
    if mean <= 0:
        return dict(median=0, p15=0, p85=0, p95=0, floor=0, ceiling=0, boom_prob=0, bust_prob=1)
    z15, z85, z95 = -1.036, 1.036, 1.645
    return dict(
        median=round(mean, 2),
        p15=round(max(0.0, mean + z15 * sd), 2),
        p85=round(mean + z85 * sd, 2),
        p95=round(mean + z95 * sd, 2),
        floor=round(max(0.0, mean - 1.5 * sd), 2),
        ceiling=round(mean + 2.0 * sd, 2),
        boom_prob=round(1 - norm_cdf((BOOM[pos] - mean) / sd), 3),
        bust_prob=round(norm_cdf((BUST[pos] - mean) / sd), 3),
    )


# ------------------------------------------------------------------ data loading
def load_slate(client, slate_key: str | None, latest: bool):
    q = client.table("slates").select("*")
    if slate_key:
        q = q.eq("slate_key", slate_key)
    else:
        q = q.order("imported_at", desc=True).limit(1)
    rows = q.execute().data
    if not rows:
        raise SystemExit(f"slate not found: {slate_key or '(latest)'}")
    slate = rows[0]
    sal = fetch_all(client.table("slate_salaries").select("*").eq("slate_id", slate["slate_id"]),
                    order="site_player_id")
    return slate, sal


def load_context(client, season: int, week: int, player_ids: list[str]):
    ctx = {}
    ctx["games"] = fetch_all(client.table("games").select("*").in_("season", [season, season - 1]),
                             order="game_id")
    ctx["dvp"] = fetch_all(client.table("defense_vs_position").select("*")
                           .in_("season", [season, season - 1]).eq("season_type", "REG"),
                           order=["defense", "season", "position"])
    ctx["team"] = fetch_all(client.table("team_game_stats").select("*")
                            .in_("season", [season, season - 1]).eq("season_type", "REG"),
                            order=["team", "season", "week"])
    logs = []
    for batch in chunked(sorted(set(player_ids)), 200):
        logs += fetch_all(client.table("player_game_stats")
                          .select("player_id,season,week,team,opponent_team,dk_points,fd_points")
                          .in_("player_id", batch).in_("season", [season, season - 1]).eq("season_type", "REG"),
                          order=["player_id", "season", "week"])
    ctx["logs"] = logs
    return ctx


# ------------------------------------------------------------------ model pieces
def game_weights(logs: list[dict], season: int):
    """logs newest-first -> weights."""
    w = []
    for i, g in enumerate(logs[:MAX_GAMES]):
        wt = DECAY ** i
        if g["season"] < season:
            wt *= PRIOR_SEASON_W
        w.append(wt)
    return w


def vegas_factors(games: list[dict], season: int, week: int):
    """{team: (implied_total, opp_implied_total)} for this week + slate average."""
    out = {}
    for g in games:
        if g["season"] != season or g["week"] != week or g["total_line"] is None or g["spread_line"] is None:
            continue
        tot, spr = float(g["total_line"]), float(g["spread_line"])  # spread: + = home favored
        home, away = tot / 2 + spr / 2, tot / 2 - spr / 2
        out[g["home_team"]] = (home, away)
        out[g["away_team"]] = (away, home)
    return out


def dvp_factors(dvp: list[dict], season: int):
    """{(defense, position): factor} blending current and prior season, shrunk."""
    cur = {(d["defense"], d["position"]): d for d in dvp if d["season"] == season}
    prev = {(d["defense"], d["position"]): d for d in dvp if d["season"] == season - 1}
    blended = {}
    for key in set(cur) | set(prev):
        c, p = cur.get(key), prev.get(key)
        gc = float(c["games"]) if c else 0.0
        vc = float(c["dk_allowed_pg"]) if c else 0.0
        vp = float(p["dk_allowed_pg"]) if p else vc
        blended[key] = (gc * vc + 4.0 * vp) / (gc + 4.0) if (gc + 4.0) else vp
    by_pos = defaultdict(list)
    for (d, pos), v in blended.items():
        by_pos[pos].append(v)
    avg = {pos: statistics.mean(v) for pos, v in by_pos.items()}
    factors = {}
    for (d, pos), v in blended.items():
        raw = v / avg[pos] if avg.get(pos) else 1.0
        factors[(d, pos)] = min(1.15, max(0.85, 1 + 0.5 * (raw - 1)))
    return factors


def dst_history(team_stats: list[dict], games: list[dict], season: int):
    """Per team: newest-first list of approx DST points (sacks + 2*INT + PA tier)."""
    pts_for = {}
    for g in games:
        if g["home_score"] is None:
            continue
        pts_for[(g["season"], g["week"], g["home_team"])] = g["home_score"]
        pts_for[(g["season"], g["week"], g["away_team"])] = g["away_score"]
    by_team = defaultdict(list)
    for r in team_stats:                        # r is the OFFENSE's line; credit its opponent's DST
        d = r["opponent_team"]
        if not d:
            continue
        pa = pts_for.get((r["season"], r["week"], r["team"]))
        tier = next(p for upper, p in PA_TIERS if (pa or 0) <= upper) if pa is not None else 1
        sacks = float(r["sacks"] or 0)
        ints = float(r["interceptions"] or 0)
        by_team[d].append(((r["season"], r["week"]), sacks + 2 * ints + tier))
    out = {}
    for d, rows in by_team.items():
        rows.sort(key=lambda x: x[0], reverse=True)
        out[d] = [(sw, v) for sw, v in rows]
    return out


# ------------------------------------------------------------------ main projection
def project(slate: dict, salaries: list[dict], ctx: dict) -> list[dict]:
    season, week, site = slate["season"], slate["week"], slate["site"]
    pts_col = "dk_points" if site == "DK" else "fd_points"

    logs_by_player = defaultdict(list)
    for r in ctx["logs"]:
        logs_by_player[r["player_id"]].append(r)
    for pid in logs_by_player:
        logs_by_player[pid].sort(key=lambda r: (r["season"], r["week"]), reverse=True)

    vegas = vegas_factors(ctx["games"], season, week)
    implied_avg = statistics.mean(v[0] for v in vegas.values()) if vegas else 22.0
    dvp = dvp_factors(ctx["dvp"], season)
    dst_hist = dst_history(ctx["team"], ctx["games"], season)

    # pass 1: raw weighted means
    raw = {}
    for sp in salaries:
        pos = sp["position"]
        if pos == "DST":
            hist = dst_hist.get(sp["team"], [])[:MAX_GAMES]
            vals = [v for _, v in hist]
            w = [DECAY ** i * (PRIOR_SEASON_W if sw[0] < season else 1) for i, (sw, _) in enumerate(hist)]
        else:
            logs = logs_by_player.get(sp["player_id"], []) if sp["player_id"] else []
            vals = [float(g[pts_col] or 0) for g in logs[:MAX_GAMES]]
            w = game_weights(logs, season)
        m, var, n_eff = wmean_wvar(vals, w)
        raw[sp["site_player_id"]] = (m, math.sqrt(var), n_eff, len(vals))

    # salary prior per position: least-squares points ~ salary over players with history
    prior = {}
    for pos in ("QB", "RB", "WR", "TE", "DST"):
        pts = [(sp["salary"], raw[sp["site_player_id"]][0]) for sp in salaries
               if sp["position"] == pos and raw[sp["site_player_id"]][2] >= 2.0 and (sp["status"] or "") not in ("OUT", "IR", "O")]
        if len(pts) >= 5:
            xs, ys = zip(*pts)
            mx, my = statistics.mean(xs), statistics.mean(ys)
            sxx = sum((x - mx) ** 2 for x in xs)
            b = sum((x - mx) * (y - my) for x, y in pts) / sxx if sxx else 0.0
            prior[pos] = (my - b * mx, b)
        else:
            prior[pos] = (0.0, 0.0025)   # ~2.5 pts per $1k fallback

    out = []
    for sp in salaries:
        pos, team, opp = sp["position"], sp["team"], sp["opponent"]
        m_raw, sd_raw, n_eff, n_games = raw[sp["site_player_id"]]
        a, b = prior[pos]
        prior_mean = max(0.0, a + b * sp["salary"])
        if n_games == 0:
            base = 0.7 * prior_mean           # no history: usually a backup / rookie
        else:
            base = (n_eff * m_raw + K_PRIOR * prior_mean) / (n_eff + K_PRIOR)

        # opponent + vegas
        implied, opp_implied = vegas.get(team, (implied_avg, implied_avg))
        if pos == "DST":
            # what DST gives up is driven by the opponent's implied total
            base_def = base - pa_tier_expectation(implied_avg)   # strip avg PA tier baked into history
            mean = base_def + pa_tier_expectation(opp_implied)
            opp_factor = 1.0
            vegas_factor = round(pa_tier_expectation(opp_implied) - pa_tier_expectation(implied_avg), 2)
        else:
            opp_factor = dvp.get((opp, pos), 1.0)
            vegas_factor = round(min(1.2, max(0.8, 1 + 0.5 * (implied / implied_avg - 1))), 3)
            # combined adjustment capped so the two factors can't compound past +/-20%
            mean = base * min(1.2, max(0.8, opp_factor * vegas_factor))

        status = (sp["status"] or "").upper()
        status_mult = STATUS_MULT.get(status, 1.0)
        mean = max(0.0, mean * status_mult)

        sd = (n_eff * sd_raw + K_PRIOR * POS_SD[pos]) / (n_eff + K_PRIOR) if n_games else POS_SD[pos]
        sd = max(sd, 0.35 * mean, 1.0)
        dist = distribution(mean, sd, pos)

        out.append({
            "slate_id": slate["slate_id"], "site_player_id": sp["site_player_id"],
            "player_id": sp["player_id"], "player_name": sp["player_name"],
            "position": pos, "team": team, "opponent": opp, "salary": sp["salary"],
            "method": "baseline",
            "mean": round(mean, 2), "stdev": round(sd, 2), **dist,
            "games_used": n_games,
            "components": {
                "raw_wmean": round(m_raw, 2), "prior": round(prior_mean, 2), "base": round(base, 2),
                "opp_factor": round(opp_factor, 3), "vegas_factor": vegas_factor,
                "implied_total": round(implied, 1), "opp_implied_total": round(opp_implied, 1),
                "status": status or None, "status_mult": status_mult, "n_eff": round(n_eff, 2),
            },
            "updated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        })
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--slate-key")
    ap.add_argument("--latest", action="store_true")
    ap.add_argument("--dry-run", metavar="SLATE_JSON", help="project from an import --dry-run file; writes projections_<key>.json")
    args = ap.parse_args()

    client = get_client(need_write=not args.dry_run)
    if args.dry_run:
        d = json.load(open(args.dry_run))
        slate, salaries = {**d["slate"], "slate_id": None}, d["salaries"]
    else:
        slate, salaries = load_slate(client, args.slate_key, args.latest)
    print(f"projecting {slate['slate_key']}: {len(salaries)} players")

    pids = [s["player_id"] for s in salaries if s["player_id"] and not s["player_id"].startswith("DST_")]
    ctx = load_context(client, slate["season"], slate["week"], pids)
    rows = project(slate, salaries, ctx)

    top = sorted(rows, key=lambda r: -r["mean"])[:15]
    for r in top:
        print(f"  {r['position']:3} {r['team']:4} {r['player_name']:<24} ${r['salary']:<5} "
              f"{r['mean']:5.1f} ± {r['stdev']:4.1f}  p85 {r['p85']:5.1f}  boom {r['boom_prob']:.2f}")

    if args.dry_run:
        path = f"projections_{slate['slate_key']}.json"
        json.dump(rows, open(path, "w"), indent=1)
        print(f"dry run — wrote {path}")
        return
    for batch in chunked(rows):
        client.table("slate_projections").upsert(batch, on_conflict="slate_id,site_player_id").execute()
    print(f"done — {len(rows)} projections written")


if __name__ == "__main__":
    main()
