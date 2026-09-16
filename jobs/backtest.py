"""
Backtest harness (Phase 4). Replays past weeks using only data available before kickoff,
runs the Monte Carlo sim and the baseline model, and scores them against actual DK points.

For each week W of a season:
  * roster  = every QB/RB/WR/TE who appeared for his team in the 4 team games before W and
              dressed for W (no historical injury reports exist, so "dressed" stands in for
              the OUT designations the live slate has; a dummy salary is used)
  * context = games (closing lines), team stats, player logs and defense-vs-position
              recomputed from rows strictly before (season, W)
  * models  = sim (n sims), baseline, naive (season-to-date average, prior season if none)
  * actual  = DK points that week; players who did not play are reported separately
Metrics per position (players who played and were DFS-relevant, i.e. sim mean >= threshold):
  MAE, RMSE, Pearson r, bias for each model; PIT histogram + coverage at p10/p50/p90 for the
  sim (calibration: a well-calibrated sim has ~10% of actuals under its p10, etc.).

Usage:
  python jobs/backtest.py --season 2025 --weeks 5-18 --sims 2000 --out web/data/backtest.json
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import time
from collections import defaultdict

import numpy as np

import project as baseline
import simulate as sim
from common import fetch_all, get_client

RELEVANT = {"QB": 12.0, "RB": 8.0, "WR": 8.0, "TE": 5.0}     # sim-mean threshold to count a player
SNAPS = None   # {(player_id, season, week): (offense_snaps, offense_pct)} — set by run()
PCTS = [5, 10, 25, 50, 75, 90, 95]


# ------------------------------------------------------------------ data
def load_all(client, season):
    seasons = [season - 1, season]
    games = fetch_all(client.table("games").select("*").in_("season", seasons), order="game_id")
    team = fetch_all(client.table("team_game_stats").select("*").in_("season", seasons).eq("season_type", "REG"),
                     order=["team", "season", "week"])
    cols = ("player_id,player_name,season,week,team,opponent_team,position,targets,receptions,receiving_yards,"
            "receiving_tds,carries,rushing_yards,rushing_tds,attempts,passing_yards,passing_tds,interceptions,"
            "dk_points,fd_points")
    logs = fetch_all(client.table("player_game_stats").select(cols).in_("season", seasons).eq("season_type", "REG")
                     .in_("position", ["QB", "RB", "WR", "TE"]), order=["player_id", "season", "week"])
    return games, team, logs


def dvp_from_logs(logs, cutoff):
    """defense_vs_position rows recomputed from logs before the cutoff."""
    agg = defaultdict(lambda: {"dk": 0.0, "weeks": set()})
    for r in logs:
        if (r["season"], r["week"]) >= cutoff or not r["opponent_team"]:
            continue
        k = (r["opponent_team"], r["season"], r["position"])
        agg[k]["dk"] += float(r["dk_points"] or 0)
        agg[k]["weeks"].add(r["week"])
    return [{"defense": d, "season": s, "position": p, "season_type": "REG",
             "games": len(v["weeks"]), "dk_allowed_pg": v["dk"] / max(len(v["weeks"]), 1)}
            for (d, s, p), v in agg.items()]


def build_week(season, week, games, team, logs):
    cutoff = (season, week)
    wk_games = [g for g in games if g["season"] == season and g["week"] == week and g["total_line"] is not None]
    teams = {}
    for g in wk_games:
        teams[g["home_team"]] = (g["away_team"], g["game_id"])
        teams[g["away_team"]] = (g["home_team"], g["game_id"])
    # last 4 team games before the cutoff
    team_games = defaultdict(list)
    for r in team:
        if (r["season"], r["week"]) < cutoff:
            team_games[r["team"]].append((r["season"], r["week"]))
    recent = {t: set(sorted(v)[-4:]) for t, v in team_games.items()}

    # players who dressed that week — stands in for the injury report (DK "OUT" removes them live)
    dressed = {r["player_id"] for r in logs if r["season"] == season and r["week"] == week}
    roster, seen = [], set()
    for r in logs:
        t = r["team"]
        if t not in teams or (r["season"], r["week"]) not in recent.get(t, ()) or r["player_id"] in seen:
            continue
        if r["player_id"] not in dressed:
            continue
        seen.add(r["player_id"])
        roster.append({"site_player_id": r["player_id"], "player_id": r["player_id"], "player_name": r["player_name"],
                       "position": r["position"], "team": t, "opponent": teams[t][0], "game_id": teams[t][1],
                       "salary": 5000, "status": None})
    for t, (opp, gid) in teams.items():
        roster.append({"site_player_id": f"DST_{t}", "player_id": f"DST_{t}", "player_name": f"{t} DST",
                       "position": "DST", "team": t, "opponent": opp, "game_id": gid, "salary": 3000, "status": None})

    slate = {"slate_id": None, "slate_key": f"BT-{season}-{week:02d}", "site": "DK", "season": season, "week": week,
             "game_ids": [g["game_id"] for g in wk_games]}
    pre_games = [dict(g, home_score=None, away_score=None) if (g["season"], g["week"]) >= cutoff else g for g in games]
    ctx = {
        "snaps": {k: v for k, v in (SNAPS or {}).items() if (k[1], k[2]) < cutoff},
        "games": pre_games,
        "team": [r for r in team if (r["season"], r["week"]) < cutoff],
        "logs": [r for r in logs if (r["season"], r["week"]) < cutoff],
        "dvp": dvp_from_logs(logs, cutoff),
    }
    actual = {r["player_id"]: r for r in logs if r["season"] == season and r["week"] == week}
    return slate, roster, ctx, actual


def naive_means(logs, season, cutoff):
    cur, prev = defaultdict(list), defaultdict(list)
    for r in logs:
        if (r["season"], r["week"]) >= cutoff:
            continue
        (cur if r["season"] == season else prev)[r["player_id"]].append(float(r["dk_points"] or 0))
    out = {}
    for pid in set(cur) | set(prev):
        out[pid] = float(np.mean(cur[pid])) if cur.get(pid) else float(np.mean(prev[pid]))
    return out


# ------------------------------------------------------------------ metrics
def metrics(rows, key):
    a = np.array([r["actual"] for r in rows]); p = np.array([r[key] for r in rows])
    if len(a) < 3:
        return None
    return {"n": int(len(a)), "mae": round(float(np.abs(p - a).mean()), 2),
            "rmse": round(float(np.sqrt(((p - a) ** 2).mean())), 2),
            "r": round(float(np.corrcoef(p, a)[0, 1]), 3), "bias": round(float((p - a).mean()), 2)}


def calibration(rows):
    pit = np.array([r["pit"] for r in rows])
    hist = np.histogram(pit, bins=10, range=(0, 1))[0]
    cov = {f"p{q}": round(float(np.mean([r["actual"] <= r["q"][str(q)] for r in rows])), 3) for q in PCTS}
    return {"pit_hist": [int(x) for x in hist], "coverage": cov, "n": int(len(rows))}


def summarize(results):
    out = {"positions": {}, "weeks": {}}
    by_pos = defaultdict(list)
    for r in results:
        if r["played"] and r["relevant"]:
            by_pos[r["position"]].append(r)
    for pos, rows in by_pos.items():
        out["positions"][pos] = {"sim": metrics(rows, "sim_mean"), "baseline": metrics(rows, "base_mean"),
                                 "naive": metrics(rows, "naive"), "calibration": calibration(rows),
                                 "actual_mean": round(float(np.mean([r["actual"] for r in rows])), 2)}
    allrows = [r for rows in by_pos.values() for r in rows]
    out["positions"]["ALL"] = {"sim": metrics(allrows, "sim_mean"), "baseline": metrics(allrows, "base_mean"),
                               "naive": metrics(allrows, "naive"), "calibration": calibration(allrows),
                               "actual_mean": round(float(np.mean([r["actual"] for r in allrows])), 2)}
    byw = defaultdict(list)
    for r in allrows:
        byw[r["week"]].append(r)
    for w, rows in sorted(byw.items()):
        out["weeks"][w] = {"sim": metrics(rows, "sim_mean"), "baseline": metrics(rows, "base_mean"), "naive": metrics(rows, "naive")}
    dnp = [r for r in results if r["relevant"] and not r["played"]]
    out["did_not_play_rate"] = round(len(dnp) / max(1, sum(1 for r in results if r["relevant"])), 3)
    return out


# ------------------------------------------------------------------ main
def run(season, weeks, n_sims, client=None, quiet=False, data=None):
    t0 = time.time()
    global SNAPS
    client = client or get_client()
    games, team, logs = data or load_all(client, season)
    if SNAPS is None:
        SNAPS = {}
        try:
            for r in fetch_all(client.table("player_snaps").select("player_id,season,week,offense_snaps,offense_pct")
                               .in_("season", [season - 1, season]).eq("season_type", "REG"),
                               order=["player_id", "season", "week"]):
                SNAPS[(r["player_id"], r["season"], r["week"])] = (r["offense_snaps"], float(r["offense_pct"] or 0))
        except Exception as e:
            print(f"  (player_snaps unavailable: {e})")
        if not quiet:
            print(f"snap rows: {len(SNAPS)}")
    if not quiet:
        print(f"loaded {len(games)} games, {len(team)} team rows, {len(logs)} player rows in {time.time() - t0:.0f}s")
    results = []
    for week in weeks:
        slate, roster, ctx, actual = build_week(season, week, games, team, logs)
        if not slate["game_ids"]:
            continue
        t1 = time.time()
        sim.COMPONENTS = {}
        scores, _, _ = sim.simulate(slate, roster, ctx, n_sims)
        comps = sim.COMPONENTS
        base = {r["site_player_id"]: r for r in baseline.project(slate, roster, ctx)}
        naive = naive_means(logs, season, (season, week))
        for s in roster:
            if s["position"] == "DST" or s["site_player_id"] not in scores:
                continue
            x = scores[s["site_player_id"]]
            act = actual.get(s["player_id"])
            played = bool(act and (float(act["attempts"] or 0) + float(act["carries"] or 0) + float(act["targets"] or 0)) > 0)
            a = float(act["dk_points"] or 0) if act else 0.0
            q = np.percentile(x, PCTS)
            results.append({
                "season": season, "week": week, "player_id": s["player_id"], "player_name": s["player_name"],
                "position": s["position"], "team": s["team"],
                "sim_mean": round(float(x.mean()), 2), "sim_sd": round(float(x.std()), 2),
                "q": {str(p): round(float(v), 2) for p, v in zip(PCTS, q)},
                "pit": round(float((x <= a).mean()), 3) if played else None,
                "base_mean": base[s["site_player_id"]]["mean"], "naive": round(naive.get(s["player_id"], 0.0), 2),
                "actual": a, "played": played, "relevant": float(x.mean()) >= RELEVANT[s["position"]],
                "sim_comp": {k: round(float(v), 2) for k, v in comps.get(s["site_player_id"], {}).items()},
                "act_comp": {k: float(act[k] or 0) for k in ("attempts", "passing_yards", "passing_tds", "interceptions",
                             "carries", "rushing_yards", "rushing_tds", "targets", "receptions", "receiving_yards", "receiving_tds")} if act else {},
            })
        if not quiet:
            wk = [r for r in results if r["week"] == week and r["played"] and r["relevant"]]
            m = metrics(wk, "sim_mean")
            print(f"  week {week:2d}: {len(roster):4d} rostered, {len(wk):3d} scored · sim MAE {m['mae']} r {m['r']} bias {m['bias']} · {time.time() - t1:.1f}s")
    return results


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--season", type=int, default=2025)
    ap.add_argument("--weeks", default="5-18")
    ap.add_argument("--sims", type=int, default=2000)
    ap.add_argument("--out", default="web/data/backtest.json")
    ap.add_argument("--detail", help="also write per-player rows to this path")
    args = ap.parse_args()
    lo, hi = (int(x) for x in args.weeks.split("-"))
    results = run(args.season, range(lo, hi + 1), args.sims)
    summary = summarize(results)
    summary["meta"] = {"season": args.season, "weeks": [lo, hi], "n_sims": args.sims,
                       "run_at": dt.datetime.now(dt.timezone.utc).isoformat(), "n_player_weeks": len(results),
                       "params": {k: getattr(sim, k) for k in ("MARGIN_SD", "TOTAL_SD", "PTS_PER_TD", "SCRIPT_PASS_SLOPE",
                                                             "DECAY", "PRIOR_SEASON_W", "OTHER_TGT", "OTHER_CAR")}}
    import os
    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    json.dump(summary, open(args.out, "w"), indent=1)
    if args.detail:
        json.dump(results, open(args.detail, "w"))
    print("\nposition   n    | sim MAE  r     bias | base MAE  r     bias | naive MAE r     bias | cov p10/p50/p90")
    for pos in ("QB", "RB", "WR", "TE", "ALL"):
        p = summary["positions"].get(pos)
        if not p:
            continue
        s, b, n, c = p["sim"], p["baseline"], p["naive"], p["calibration"]["coverage"]
        print(f"{pos:8} {s['n']:5d} | {s['mae']:6.2f} {s['r']:.3f} {s['bias']:6.2f} | {b['mae']:6.2f} {b['r']:.3f} {b['bias']:6.2f}"
              f" | {n['mae']:6.2f} {n['r']:.3f} {n['bias']:6.2f} | {c['p10']:.2f}/{c['p50']:.2f}/{c['p90']:.2f}")
    print("PIT deciles (ALL):", summary["positions"]["ALL"]["calibration"]["pit_hist"])
    print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
