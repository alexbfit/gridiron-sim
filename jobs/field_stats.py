"""Field structure stats from a DraftKings contest-standings export (the CSV DK offers for ~10 days
after a contest, or an archived one).

    python jobs/field_stats.py data/ownership/DK-2026-02-main_standings.csv [more.csv ...] \
        [--salaries data/slates/DKSalaries_2026_wk02_main.csv]

Prints, per file: entries, score quantiles (top, 1%, 10%, 20%, median), summed ownership of the
lineups by finish bucket (top 100 / top 1% / top 20% / rest), the chalk, and — when a DKSalaries
file for that slate is given (needed for team mapping) — QB-stack, bring-back and game-stack rates
by finish bucket. Use it to calibrate jobs/contest_sim.py's field sampler (stack_prob etc.) and to
sanity-check how low a winning lineup's ownership sum runs in each contest type.
"""
import argparse, csv, re, statistics as st, sys
from collections import defaultdict

POS_RE = re.compile(r'(?:^|\s)(?:QB|RB|WR|TE|FLEX|DST)\s')


def players(lineup: str):
    return [p.strip() for p in POS_RE.split(lineup.strip()) if p.strip()]


def load_standings(path):
    own, pts, rows = {}, {}, []
    with open(path, encoding="utf-8-sig", newline="") as fh:
        for r in csv.DictReader(fh):
            if r.get("Player"):   # a player drafted in two slots (RB + FLEX) gets a row per slot: sum them
                name = r["Player"].strip()
                own[name] = own.get(name, 0.0) + float((r.get("%Drafted") or "0").rstrip("%") or 0)
                try: pts[r["Player"].strip()] = float(r.get("FPTS") or 0)
                except ValueError: pass
            if r.get("Lineup"):
                rows.append((int(r["Rank"]), float(r["Points"] or 0), r["Lineup"]))
    return own, pts, rows


def load_salaries(path):
    """name -> (position, team, opponent) from a DKSalaries export."""
    out = {}
    if not path:
        return out
    with open(path, encoding="utf-8-sig", newline="") as fh:
        for r in csv.DictReader(fh):
            game = (r.get("Game Info") or "").split(" ")[0]
            teams = game.split("@") if "@" in game else []
            team = r.get("TeamAbbrev", "")
            opp = [t for t in teams if t != team]
            out[r["Name"].strip()] = (r["Position"], team, opp[0] if opp else "")
    return out


def bucket(rank, n):
    return "top100" if rank <= 100 else "top1%" if rank <= n * 0.01 else "top20%" if rank <= n * 0.2 else "rest"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("files", nargs="+")
    ap.add_argument("--salaries", help="DKSalaries CSV for the same slate (enables stack stats)")
    a = ap.parse_args()
    sal = load_salaries(a.salaries)
    for f in a.files:
        own, pts, rows = load_standings(f)
        n = len(rows)
        if not n:
            print(f"{f}: no lineups found"); continue
        scores = sorted((p for _, p, _ in rows), reverse=True)
        q = lambda x: scores[min(n - 1, int(n * x))]
        print(f"\n== {f}\n  entries {n:,}  top {scores[0]:.1f}  1% {q(.01):.1f}  10% {q(.10):.1f}  20% {q(.20):.1f}  median {q(.5):.1f}")
        agg = defaultdict(list)
        unmapped = 0
        for rk, p, l in rows:
            ps = players(l)
            osum = sum(own.get(x, 0.0) for x in ps)
            rec = {"own": osum, "n": len(ps)}
            if sal:
                info = [sal.get(x) for x in ps]
                if any(i is None for i in info):
                    unmapped += 1
                else:
                    qb = next((i for i in info if i[0] == "QB"), None)
                    if qb:
                        mates = sum(1 for i in info if i[0] in ("WR", "TE", "RB") and i[1] == qb[1])
                        bring = sum(1 for i in info if i[0] in ("WR", "TE", "RB") and i[1] == qb[2])
                        rec.update(stack=mates > 0, stack_n=mates, bring=(mates > 0 and bring > 0))
            agg[bucket(rk, n)].append(rec)
        for b in ("top100", "top1%", "top20%", "rest"):
            v = agg[b]
            if not v: continue
            line = f"  {b:7s} n={len(v):7,d}  own-sum mean {st.mean(x['own'] for x in v):6.1f}  median {st.median(x['own'] for x in v):6.1f}"
            sv = [x for x in v if "stack" in x]
            if sv:
                line += (f"  | QB-stack {100*st.mean(x['stack'] for x in sv):3.0f}%  bring-back {100*st.mean(x['bring'] for x in sv):3.0f}%"
                         f"  stack size {st.mean(x['stack_n'] for x in sv):.2f}")
            print(line)
        if sal and unmapped:
            print(f"  ({unmapped:,} lineups had a player not in the salaries file; skipped for stack stats)")
        chalk = sorted(own.items(), key=lambda kv: -kv[1])[:8]
        print("  chalk: " + ", ".join(f"{k} {v:.0f}%" for k, v in chalk))
        if pts:
            busts = [(k, v, pts.get(k, 0)) for k, v in chalk if k in pts]
            print("  chalk pts: " + ", ".join(f"{k} {p:.1f}" for k, _, p in busts))


if __name__ == "__main__":
    main()
