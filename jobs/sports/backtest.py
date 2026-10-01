"""Replay every archived slate of a sport: sim -> optimizer -> grade the lineups inside the REAL contest fields.

    python jobs/sports/backtest.py --sport MLB --n 20 --sims 4000 --every 2 --out bt/sports/MLB.jsonl --workers 2
    python jobs/sports/backtest.py --summary bt/sports/*.jsonl
    python jobs/sports/backtest.py --sport NBA --proj-dir data/sports/nba --out bt/sports/NBA_props.jsonl
                                  # replay with OUR projection CSVs (<proj-dir>/<date>/projections.csv or <proj-dir>/NBA_<date>.csv)
                                  # swapped in for SaberSim's; slates without a file are skipped. Same fields, same grading,
                                  # so the two jsonl files compare our props model against SaberSim lineup-for-lineup.

Per slate and mode it writes one JSON line per contest kind (flagship / 20max / minimax): the lineups' real
points, their percentile in the field, real rank and prize (ties split), ROI for entering the lineups at the
contest's fee (capped at the contest's max entries per user), and top-1% hits. `--summary` aggregates per
sport x mode x kind. Candidate lineups are shared between modes that only differ in the ranking stat.

MODES per sport: "base" (no structure, p90 ranking), "stack" (the sport's natural stack), "mean" (cash-style
ranking), "p98" (fatter-tail ranking). A mode's structure comes from STRUCT, its ranking from the name.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sys
import time
from collections import defaultdict
from multiprocessing import Pool

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from sports import Field, Opts, field_kinds, load_slate, slate_dates, sim_matrix
from sports.optimize import candidates, select

STRUCT = {       # the sport's natural stack structure for the "stack" modes
    "MLB": dict(stacks=[5, 3], no_opp=True),
    "NHL": dict(stacks=[3], no_opp=True),
    "NBA": dict(stacks=[3]),
    "WNBA": dict(stacks=[2]),
    "CBB": dict(stacks=[3]),
    "CFB": dict(stacks=[2], bringback=True),
    "UFL": dict(stacks=[2], bringback=True),
    "NFL": dict(stacks=[2], bringback=True),
    "LOL": dict(stacks=[4]),
    "SOCCER": dict(stacks=[3]),
}
RANK = {"base": "p90", "stack": "p90", "mean": "mean", "p98": "p98", "stack_mean": "mean", "stack_p98": "p98"}
STRUCT_OF = {"base": "none", "mean": "none", "p98": "none", "stack": "stack", "stack_mean": "stack", "stack_p98": "stack"}


def grade(slate, ps, lineups, fields):
    out = {}
    for kind, F in fields.items():
        n_ent = min(len(lineups), F.max_entries) if F.max_entries else len(lineups)
        rows = []
        for c in lineups[:n_ent]:
            act = float(sum(ps[k]["actual"] or 0.0 for k in c["idx"]))
            rank, prize, pct = F.place(act)
            rows.append((act, rank, prize, pct, c["mean"], c["p90"]))
        a = np.array(rows)
        fee = F.fee
        out[kind] = {
            "contest": F.name, "fee": fee, "field": F.n, "entries": n_ent, "max_entries": F.max_entries,
            "avg_pts": round(float(a[:, 0].mean()), 2), "avg_proj": round(float(a[:, 4].mean()), 2), "avg_p90": round(float(a[:, 5].mean()), 2),
            "avg_pct": round(float(a[:, 3].mean()), 4), "best_rank": int(a[:, 1].min()), "best_pts": round(float(a[:, 0].max()), 2),
            "prize": round(float(a[:, 2].sum()), 2), "cost": round(fee * n_ent, 2),
            "roi": round(float(a[:, 2].sum() / (fee * n_ent) - 1), 4) if fee else None,
            "top1": int((a[:, 1] <= max(1, F.n * 0.01)).sum()), "top10": int((a[:, 1] <= F.n * 0.10).sum()),
            "cashed": int((a[:, 2] > 0).sum()),
            "winner_pts": round(float(F.points[0]), 2), "top1_cut": round(float(F.points[max(0, int(F.n * 0.01) - 1)]), 2),
            "field_median": round(float(np.median(F.points)), 2),
            "field_roi": round(float(F.payout.sum() / (fee * F.n) - 1), 4) if fee else None,
        }
    return out


def apply_proj_csv(slate, path):
    """Replace SaberSim's proj / percentiles with ours (projections CSV: name, proj, p25..p99), matched by name.
    Players without a row get proj 0 (never rostered). Returns the number of players matched."""
    import csv
    from build_lineups_sport import norm, fill_quantiles
    rows = {norm(r.get("name") or ""): r for r in csv.DictReader(open(path, encoding="utf-8-sig"))}
    hit = 0
    for p in slate["players"]:
        r = rows.get(norm(p["name"]))
        if not r or not float(r.get("proj") or 0):
            p["proj"] = 0.0
            continue
        hit += 1
        p["proj"] = float(r["proj"])
        p["q"] = {k: float(r.get(f"p{k}") or 0) for k in (25, 50, 75, 85, 95, 99)}
        fill_quantiles(slate["sport"], p)
    return hit


def proj_csv_path(proj_dir, sport, date):
    for cand in (os.path.join(proj_dir, date, "projections.csv"), os.path.join(proj_dir, f"{sport}_{date}.csv"),
                 os.path.join(proj_dir, f"{date}.csv")):
        if os.path.exists(cand):
            return cand
    return None


def run_slate(args):
    sport, date, modes, n, sims, seed, root = args[:7]
    proj_dir = args[7] if len(args) > 7 else None
    t0 = time.time()
    try:
        slate = load_slate(sport, date, root)
        if proj_dir:
            pc = proj_csv_path(proj_dir, sport, date)
            if not pc:
                return date, None, "no projection csv"
            hit = apply_proj_csv(slate, pc)
            if hit < 2 * len(__import__("sports").RULES[sport]["slots"]):
                return date, None, f"only {hit} players matched in {os.path.basename(pc)}"
        kinds = field_kinds(slate)
        if not kinds:
            return date, None, "no field"
        ps, D = sim_matrix(slate, sims, seed=seed)
        if len(ps) < 2 * len(__import__("sports").RULES[sport]["slots"]):
            return date, None, f"only {len(ps)} projected players"
        fields = {k: Field(slate, k) for k in kinds}
        fields = {k: F for k, F in fields.items() if F.fee > 0 and F.n > 50}
        if not fields:
            return date, None, "no priced field"
        results = {}
        cache = {}
        for mode in modes:
            st = STRUCT_OF[mode]
            kw = STRUCT.get(sport, {}) if st == "stack" else {}
            if st == "stack" and not kw:
                continue
            opts = Opts(n=n, rank=RANK[mode], max_exp=0.5, min_uniq=3, candidates=3, seed=seed, **kw)
            if st not in cache:
                cache[st] = candidates(slate, ps, D, opts)
            lineups = select(slate, ps, D, cache[st], opts)
            if len(lineups) < max(3, n // 2):
                results[mode] = {"error": f"only {len(lineups)} lineups"}
                continue
            results[mode] = grade(slate, ps, lineups, fields)
            results[mode]["_lineups"] = [[ps[k]["uuid"] for k in c["idx"]] for c in lineups]
        return date, results, f"{time.time() - t0:.0f}s"
    except Exception as e:      # keep the run going; the summary reports errors
        import traceback
        return date, None, "ERR " + traceback.format_exc().splitlines()[-1]


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--sport")
    ap.add_argument("--modes", default="base,stack,mean,p98")
    ap.add_argument("--n", type=int, default=20)
    ap.add_argument("--sims", type=int, default=4000)
    ap.add_argument("--every", type=int, default=1, help="use every k-th slate")
    ap.add_argument("--dates", default="", help="comma list or a-b range of dates to restrict to")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--workers", type=int, default=2)
    ap.add_argument("--root", default=os.environ.get("SS_ALLSPORTS", os.path.expanduser("~/ssa")))
    ap.add_argument("--limit", type=int, default=0, help="stop after this many slates (chunked runs)")
    ap.add_argument("--proj-dir", help="swap in our projection CSVs (<dir>/<date>/projections.csv or <dir>/<SPORT>_<date>.csv)")
    ap.add_argument("--out")
    ap.add_argument("--summary", nargs="*")
    a = ap.parse_args(argv)
    if a.summary:
        return summary(a.summary)
    modes = [m for m in a.modes.split(",") if m]
    dates = slate_dates(a.sport, a.root)[::a.every]
    if a.dates:
        if "-" in a.dates and len(a.dates) == 21:
            lo, hi = a.dates.split("-", 1)[0] + "-" + a.dates[5:10], a.dates[11:]
            dates = [d for d in dates if lo <= d <= hi]
        else:
            dates = [d for d in dates if d in set(a.dates.split(","))]
    done = set()
    if a.out and os.path.exists(a.out):
        for line in open(a.out):
            try:
                done.add(json.loads(line)["date"])
            except Exception:
                pass
    todo = [(a.sport, d, modes, a.n, a.sims, a.seed, a.root, a.proj_dir) for d in dates if d not in done]
    if a.limit:
        todo = todo[:a.limit]
    print(f"{a.sport}: {len(dates)} slates, {len(todo)} to do, modes {modes}", file=sys.stderr)
    os.makedirs(os.path.dirname(a.out) or ".", exist_ok=True)
    fo = open(a.out, "a") if a.out else sys.stdout
    with Pool(a.workers) as pool:
        for date, res, note in pool.imap_unordered(run_slate, todo):
            print(f"{a.sport} {date}: {note}", file=sys.stderr, flush=True)
            if res is None:
                fo.write(json.dumps({"sport": a.sport, "date": date, "skip": note}) + "\n")
            else:
                fo.write(json.dumps({"sport": a.sport, "date": date, "n": a.n, "modes": res}) + "\n")
            fo.flush()


def summary(paths):
    agg = defaultdict(list)
    for p in paths:
        for line in open(p):
            r = json.loads(line)
            if "modes" not in r:
                continue
            for mode, kinds in r["modes"].items():
                for kind, g in kinds.items():
                    if kind.startswith("_") or "error" in g:
                        continue
                    agg[(r["sport"], mode, kind)].append(g)
    print(f"{'sport':7} {'mode':11} {'kind':9} {'slates':>6} {'avg pct':>8} {'ROI':>7} {'fieldROI':>8} {'top1%/slate':>11} {'cash%':>6} {'best rank med':>13} {'$ in':>8} {'$ out':>8}")
    for (sport, mode, kind), gs in sorted(agg.items()):
        pct = np.mean([g["avg_pct"] for g in gs])
        cost = sum(g["cost"] for g in gs); prize = sum(g["prize"] for g in gs)
        roi = prize / cost - 1 if cost else float("nan")
        froi = np.mean([g["field_roi"] for g in gs if g["field_roi"] is not None])
        top1 = np.mean([g["top1"] for g in gs])
        cash = np.mean([g["cashed"] / g["entries"] for g in gs])
        br = np.median([g["best_rank"] / g["field"] for g in gs])
        print(f"{sport:7} {mode:11} {kind:9} {len(gs):6} {pct:8.3f} {roi:7.1%} {froi:8.1%} {top1:11.2f} {cash:6.1%} {br:13.3%} {cost:8.0f} {prize:8.0f}")


if __name__ == "__main__":
    main()
