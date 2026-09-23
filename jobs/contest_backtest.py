"""
Contest backtest: replay past DraftKings main slates with the production sim + builder and grade the
lineups against the REAL contest field (a DK contest-standings export) with the real payout curve.

Unlike jobs/backtest.py (player projection accuracy), this answers the money question: which builder
settings / selection modes would have cashed, and how often would 50 entries have hit the top 1%.

Data it needs (all offline — nothing is written to the DB):
  --data-dir     games.json, team.json, logs.json, snaps.json, injuries.json, depth.json for the season and
                 the one before (see the "offline pull" block at the bottom: python jobs/contest_backtest.py --pull 2019 2020)
  --salaries     DKSalaries CSV per week, e.g. "dir/DKSalaries_NFL2020_Week_{week:02d}_ms.csv"
  --standings    DK standings CSV per week (Rank, Points, Lineup, Player, %Drafted, FPTS), e.g. "dir/NFL2020_w{week:02d}_*.csv"

    python jobs/contest_backtest.py --season 2020 --weeks 2-16 --sims 3000 --n 50 --seeds 7 11 \
        --salaries "DFS DATA/DK_2020_tidyDK_salaries/DKSalaries_NFL2020_Week_{week:02d}_ms.csv" \
        --standings "DFS DATA/DK_2020_tidyDK_Millionaire/NFL2020_w{week:02d}_*.csv" --out bt/contest_2020.json

Per week it: builds the pre-kickoff context exactly like backtest.py (stats strictly before the week, that
week's injury report + depth chart), rosters the DK salary file (names matched like import_salaries.py),
runs simulate.simulate, builds the board rows + sim matrix, then calls build_lineups.build() once per MODE
and grades every lineup with the real FPTS from the standings file: actual points, percentile of the real
field, real rank, and the prize from contest_sim.payout_fn scaled to the real entry count.
"""
from __future__ import annotations

import argparse
import csv
import glob
import json
import os
import re
import sys
import time
from collections import defaultdict

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import backtest as bt
import build_lineups as bl
import simulate as sim
from contest_sim import payout_fn
from common import norm_name, norm_team
from import_salaries import match_players, parse_dk

INJ_TO_SITE = {"Out": "OUT", "Doubtful": "D", "Questionable": "Q"}
ROSTER = {}          # (player_id, week) -> nflverse weekly roster status ("ACT", "RES" = IR, "PUP", "SUS", ...)

# Selection modes to compare. Each is a build_lineups argv (contest gpp, --n and --seed added by the harness).
COMMON = ["--stack", "1", "--bringback", "--max-exp", "0.5", "--min-uniq", "3", "--candidates", "6"]
MODES = {
    "sunday (ev, fade .4)":   COMMON + ["--fade", "0.4", "--objective", "ev", "--payout", "milly", "--fee", "20"],
    "ev, fade 0":             COMMON + ["--fade", "0", "--objective", "ev", "--payout", "milly", "--fee", "20"],
    "ev, fade .4, real own":  COMMON + ["--fade", "0.4", "--objective", "ev", "--payout", "milly", "--fee", "20", "--own-file", "{standings}"],
    "p90 rank, fade .4":      COMMON + ["--fade", "0.4"],
    "p90 rank, fade 0":       COMMON + ["--fade", "0"],
    "ev, fade .4, exp .35":   ["--stack", "1", "--bringback", "--max-exp", "0.35", "--min-uniq", "4", "--candidates", "6",
                               "--fade", "0.4", "--objective", "ev", "--payout", "milly", "--fee", "20"],
    # round 2: projection blends + structure, on the plain p90 ranking
    "p90 f0, market .5":      COMMON + ["--fade", "0", "--market", "0.5"],
    "p90 f0, dkavg blend .5": COMMON + ["--fade", "0", "--blend", "0.5"],       # ext = DK AvgPointsPerGame from the salary file
    "p90 f0, no bringback":   ["--stack", "1", "--max-exp", "0.5", "--min-uniq", "3", "--candidates", "6", "--fade", "0"],
    "p90 f0, exp .35 uniq 4": ["--stack", "1", "--bringback", "--max-exp", "0.35", "--min-uniq", "4", "--candidates", "6", "--fade", "0"],
    "p90 f0, stack 2":        ["--stack", "2", "--bringback", "--max-exp", "0.5", "--min-uniq", "3", "--candidates", "6", "--fade", "0"],
}


# ------------------------------------------------------------------ offline data
def load_data(data_dir, season):
    rd = lambda n: json.load(open(os.path.join(data_dir, n + ".json")))
    games, team, logs = rd("games"), rd("team"), rd("logs")
    games = [g for g in games if g["season_type"] == "REG"]
    team = [r for r in team if r["season_type"] == "REG"]
    logs = [r for r in logs if r["season_type"] == "REG" and r["position"] in ("QB", "RB", "WR", "TE")]
    logs.sort(key=lambda r: (r["player_id"], r["season"], r["week"]))
    bt.SNAPS = {(r["player_id"], r["season"], r["week"]): (r["offense_snaps"], float(r["offense_pct"] or 0))
                for r in rd("snaps") if r["season_type"] == "REG"}
    bt.INJ = {}
    for r in rd("injuries"):
        if r["season_type"] == "REG":
            bt.INJ.setdefault((r["season"], r["week"]), {})[r["player_id"]] = {"status": r["report_status"], "practice": r["practice_status"]}
    global ROSTER
    ROSTER = {}
    if os.path.exists(os.path.join(data_dir, "rosters.json")):      # weekly roster status: RES/PUP/SUS/... = not available
        for r in rd("rosters"):
            if r.get("season", season) == season:
                ROSTER[(r["gsis_id"], r["week"])] = r["status"]
    bt.DEPTH = {}
    for r in rd("depth"):
        bt.DEPTH[(r["gsis_id"], int(r.get("season") or season), r["week"])] = [r["pos_abb"], r["pos_rank"], r["team"]]
    return games, team, logs


def read_standings(path):
    """own {name: %}, fpts {name: pts}, sorted field scores (desc), entries."""
    own, fpts, scores = {}, {}, []
    with open(path, encoding="utf-8-sig", newline="") as fh:
        for r in csv.DictReader(fh):
            if r.get("Player"):
                n = r["Player"].strip()
                own[n] = own.get(n, 0.0) + float((r.get("%Drafted") or "0").rstrip("%") or 0)
                try:
                    fpts[n] = float(r.get("FPTS") or 0)
                except ValueError:
                    pass
            if r.get("Lineup"):
                scores.append(float(r["Points"] or 0))
    scores.sort(reverse=True)
    return own, fpts, np.array(scores), len(scores)


# ------------------------------------------------------------------ one week
def roster_from_salaries(path, season, week, games, logs, injuries):
    rows = list(csv.DictReader(open(path, encoding="utf-8-sig", newline="")))
    players = parse_dk(rows)
    # index: every player seen in the logs (this season first, then last season) with his latest team
    latest = {}
    for r in logs:
        if (r["season"], r["week"]) < (season, week):
            latest[r["player_id"]] = r
    index = [{"player_id": pid, "player_name": r["player_name"], "team": r["team"], "position": r["position"]} for pid, r in latest.items()]
    how = match_players(players, index)
    # slate games = games in the salary file
    wk = {(g["away_team"], g["home_team"]): g for g in games if g["season"] == season and g["week"] == week}
    game_ids = set()
    roster = []
    for p in players:
        g = wk.get((p["away"], p["home"]))
        if not g:
            continue
        game_ids.add(g["game_id"])
        if p["position"] == "DST":
            roster.append({"site_player_id": p["site_player_id"], "player_id": f"DST_{p['team']}", "player_name": p["player_name"],
                           "position": "DST", "team": p["team"], "opponent": p["opponent"], "game_id": g["game_id"],
                           "salary": p["salary"], "status": None, "dk_name": p["player_name"]})
            continue
        if not p["player_id"]:
            continue
        rep = injuries.get(p["player_id"])
        st = INJ_TO_SITE.get((rep or {}).get("status") or "", "")
        if st == "OUT" or ROSTER.get((p["player_id"], week), "ACT") != "ACT":
            continue          # Out on the report, or on IR/PUP/suspended that week: DK marks these O/IR and the Sunday task excludes them
        roster.append({"site_player_id": p["site_player_id"], "player_id": p["player_id"], "player_name": p["player_name"],
                       "position": p["position"], "team": p["team"], "opponent": p["opponent"], "game_id": g["game_id"],
                       "salary": p["salary"], "status": None, "dk_name": p["player_name"]})
    return roster, sorted(game_ids), how


def week_board(season, week, games, team, logs, salaries_path, n_sims, store):
    slate, _, ctx, _ = bt.build_week(season, week, games, team, logs)
    roster, game_ids, how = roster_from_salaries(salaries_path, season, week, games, logs, ctx["injuries"])
    slate = dict(slate, game_ids=game_ids, slate_key=f"BT-{season}-{week:02d}")
    # the sim only knows players with logs; DSTs are simulated from team lines
    sim.COMPONENTS = None
    scores, _, _ = sim.simulate(slate, roster, ctx, n_sims)
    rows = sim.summarize(slate, roster, scores)
    byid = {r["site_player_id"]: r for r in roster}
    for r in rows:
        src = byid[r["site_player_id"]]
        r["game_id"] = src["game_id"]
        rep = ctx["injuries"].get(src["player_id"])
        r["injury_report"] = (rep or {}).get("status")
        r["dk_name"] = src["dk_name"]
        r["avg_points"] = src.get("avg_points")
    matrix = {pid: x[:store].astype(np.float32) for pid, x in scores.items()}
    return slate, rows, matrix, how


def grade(lineups, rows_by_id, fpts, field_scores, entries, pay, fee):
    out = []
    for L in lineups:
        a = 0.0
        for i in L["ids"]:
            a += fpts.get(rows_by_id[i]["dk_name"], 0.0)
        beaten = float((field_scores < a).mean())                    # share of the real field below this total
        rank = int(np.searchsorted(-field_scores, -a, side="left")) + 1  # rank among real entries (ties -> best)
        prize = float(pay([rank])[0])
        out.append({"actual": round(a, 2), "field_pct": round(beaten, 4), "rank": rank, "prize": prize,
                    "proj": L["proj"], "own": L["own"], "ids": L["ids"]})
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--season", type=int, default=2020)
    ap.add_argument("--weeks", default="2-16")
    ap.add_argument("--sims", type=int, default=3000)
    ap.add_argument("--store", type=int, default=2000, help="sim draws kept in the matrix (production STORE_SIMS)")
    ap.add_argument("--n", type=int, default=50)
    ap.add_argument("--seeds", nargs="*", type=int, default=[7])
    ap.add_argument("--modes", nargs="*", default=None, help="subset of MODES names")
    ap.add_argument("--data-dir", default="data")
    ap.add_argument("--salaries", required=True, help="path pattern with {week:02d}")
    ap.add_argument("--standings", required=True, help="glob pattern with {week:02d}")
    ap.add_argument("--fee", type=float, default=20.0)
    ap.add_argument("--own-model", default='{"b": 0.4, "c": 0.6, "cap": 40}', help="fitted ownership model (model_params.ownership_model)")
    ap.add_argument("--out", default="contest_backtest.json")
    ap.add_argument("--cache", default=None, help="dir to cache per-week boards (json + npz)")
    args = ap.parse_args()
    lo, hi = (int(x) for x in args.weeks.split("-"))
    own_model = json.loads(args.own_model)
    modes = {k: v for k, v in MODES.items() if not args.modes or k in args.modes}

    t0 = time.time()
    games, team, logs = load_data(args.data_dir, args.season)
    print(f"loaded {len(games)} games, {len(logs)} player rows in {time.time() - t0:.0f}s", flush=True)
    results = json.load(open(args.out)) if os.path.exists(args.out) else {"meta": vars(args), "weeks": {}}

    for week in range(lo, hi + 1):
        sal = args.salaries.format(week=week)
        st = glob.glob(args.standings.format(week=week))
        if not os.path.exists(sal) or not st:
            print(f"week {week}: missing salaries or standings, skipped"); continue
        st = st[0]
        t1 = time.time()
        own_real, fpts, field, entries = read_standings(st)
        cache = os.path.join(args.cache, f"board_{args.season}_{week:02d}") if args.cache else None
        if cache and os.path.exists(cache + ".json"):
            d = json.load(open(cache + ".json")); slate, rows, how = d["slate"], d["rows"], d["how"]
            z = np.load(cache + ".npz"); matrix = {k: z[k] for k in z.files}
            if rows and "avg_points" not in rows[0]:            # older cache: add DK AvgPointsPerGame from the salary file
                avg = {r["ID"].strip(): float(r["AvgPointsPerGame"] or 0) for r in csv.DictReader(open(sal, encoding="utf-8-sig", newline=""))}
                for r in rows:
                    r["avg_points"] = avg.get(r["site_player_id"])
        else:
            slate, rows, matrix, how = week_board(args.season, week, games, team, logs, sal, args.sims, args.store)
            if cache:
                os.makedirs(args.cache, exist_ok=True)
                json.dump({"slate": slate, "rows": rows, "how": dict(how)}, open(cache + ".json", "w"))
                np.savez_compressed(cache + ".npz", **matrix)
        rows_by_id = {r["site_player_id"]: r for r in rows}
        pay = payout_fn("milly", entries, args.fee)
        wk = results["weeks"].setdefault(str(week), {"entries": entries, "field": {"top": float(field[0]), "p1": float(field[int(entries * .01)]),
                                                     "p20": float(field[int(entries * .2)]), "median": float(np.median(field))},
                                                     "matched": dict(how), "board": len(rows), "modes": {}})
        print(f"\nweek {week}: {len(rows)} on the board ({dict(how)}), field {entries:,} entries, cash line ~{wk['field']['p20']:.1f}, "
              f"1% {wk['field']['p1']:.1f}, top {wk['field']['top']:.1f} · sim {time.time() - t1:.0f}s", flush=True)
        for name, argv in modes.items():
            for seed in args.seeds:
                key = f"{name}|{seed}"
                if key in wk["modes"]:
                    continue
                t2 = time.time()
                av = [a.replace("{standings}", st) for a in argv]
                bargs = bl.parse_args(["--contest", "gpp", "--n", str(args.n), "--seed", str(seed), "--entries", str(entries), *av])
                ext = {r["site_player_id"]: {"mean": float(r["avg_points"]), "own": None} for r in rows if r.get("avg_points")} if bargs.blend > 0 else {}
                lineups, lev, proj, own_map = bl.build(bargs, slate, [dict(r) for r in rows], ext, own_model, dict(matrix), quiet=True)
                g = grade(lineups, rows_by_id, fpts, field, entries, pay, args.fee)
                acts = np.array([x["actual"] for x in g]); pcts = np.array([x["field_pct"] for x in g])
                prizes = np.array([x["prize"] for x in g])
                summ = {"n": len(g), "avg": round(float(acts.mean()), 1), "best": round(float(acts.max()), 1),
                        "avg_pct": round(float(pcts.mean()), 3), "best_pct": round(float(pcts.max()), 4),
                        "best_rank": int(min(x["rank"] for x in g)),
                        "top1": int((pcts >= 0.99).sum()), "top10": int((pcts >= 0.90).sum()), "cashed": int((prizes > 0).sum()),
                        "net": round(float(prizes.sum() - args.fee * len(g)), 0), "proj": round(float(np.mean([x["proj"] for x in g])), 1),
                        "own": round(float(np.mean([x["own"] for x in g])), 1), "lineups": g}
                wk["modes"][key] = summ
                print(f"  {name:<24} seed {seed:2d}: proj {summ['proj']:5.1f} own {summ['own']:5.0f} | actual avg {summ['avg']:5.1f} best {summ['best']:5.1f} "
                      f"| field avg {100 * summ['avg_pct']:3.0f}% best {100 * summ['best_pct']:5.1f}% rank {summ['best_rank']:>6,} | top1% {summ['top1']:2d} top10% {summ['top10']:2d} "
                      f"cashed {summ['cashed']:2d}/{len(g)} | net ${summ['net']:+7,.0f} · {time.time() - t2:.0f}s", flush=True)
                json.dump(results, open(args.out, "w"))
    summarize(results)


def summarize(results):
    agg = defaultdict(list)
    for w, wk in results["weeks"].items():
        for key, s in wk["modes"].items():
            agg[key.split("|")[0]].append(s)
    print("\n=== season summary (per week of 50 entries, averaged over weeks x seeds) ===")
    print(f"{'mode':<24} {'wks':>3} {'proj':>6} {'own':>5} | {'act avg':>7} {'best':>6} | {'field%':>6} {'best%':>6} | {'top1%':>5} {'top10%':>6} {'cash':>5} | {'net $/wk':>9} {'ROI':>6}")
    for name, rows in agg.items():
        n = len(rows)
        net = np.mean([r["net"] for r in rows]); cost = 20 * rows[0]["n"]
        print(f"{name:<24} {n:3d} {np.mean([r['proj'] for r in rows]):6.1f} {np.mean([r['own'] for r in rows]):5.0f} | "
              f"{np.mean([r['avg'] for r in rows]):7.1f} {np.mean([r['best'] for r in rows]):6.1f} | "
              f"{100 * np.mean([r['avg_pct'] for r in rows]):5.0f}% {100 * np.mean([r['best_pct'] for r in rows]):5.1f}% | "
              f"{np.mean([r['top1'] for r in rows]):5.1f} {np.mean([r['top10'] for r in rows]):6.1f} {np.mean([r['cashed'] for r in rows]):5.1f} | "
              f"{net:+9,.0f} {100 * net / cost:+5.0f}%")


if __name__ == "__main__":
    main()
