"""Build DraftKings lineups for any sport (NBA, NHL, MLB, tennis, golf, NASCAR, MMA, CFB, WNBA, ...).

Two inputs:
  --archive SPORT DATE                  replay a slate from the SaberSim all-sports archive ($SS_ALLSPORTS)
  --sport SPORT --dk DKSalaries.csv --proj projections.csv
                                        a live slate: the DK salary export (ID, Name, Roster Position, Salary,
                                        TeamAbbrev, Game Info) joined by name to a projections CSV with columns
                                        name, proj [, p25 p50 p75 p85 p95 p99, team, opp, line, order]. Missing
                                        percentiles come from the sport's fitted distribution shape (shapes.json).

    python jobs/build_lineups_sport.py --archive NBA 2026-01-10 --n 20 --stack 3 --rank p90
    python jobs/build_lineups_sport.py --sport MLB --dk DKSalaries.csv --proj my_mlb_proj.csv --n 20 --stack 5,3 --out DK_mlb.csv

Writes a DK-uploadable CSV (one row per lineup, columns = roster slots, cells = DK IDs when the salary file is
given, else names) and prints every lineup with its sim mean / p90 / salary.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from sports import RULES, Field, Opts, build, field_kinds, load_slate, sim_matrix
from sports.rules import NO_POSITIONS

SHAPES = json.load(open(os.path.join(os.path.dirname(__file__), "sports", "shapes.json")))


def norm(s):
    return re.sub(r"[^a-z0-9]", "", (s or "").lower().replace("jr", "").replace("sr", "").replace("iii", "").replace("ii", ""))


def shape_bucket(sport, pos):
    p0 = sorted(pos)[0] if pos else "X"
    if sport == "MLB":
        return "P" if "P" in pos else "H"
    if sport == "NHL":
        return "G" if "G" in pos else "S"
    if sport in ("CFB", "UFL", "NFL"):
        for t in ("QB", "RB", "WR", "TE", "DST"):
            if t in pos:
                return t
    if sport == "LOL":
        return "TEAM" if "TEAM" in pos else "P"
    return "X"


def fill_quantiles(sport, p):
    if max(p["q"].values()) > 0:
        return
    sh = SHAPES.get(sport, {})
    s = sh.get(shape_bucket(sport, p["pos"])) or sh.get("X") or next(iter(sh.values()), None)
    if not s:
        s = [0.6, 0.95, 1.3, 1.5, 1.85, 2.3]
    for k, f in zip((25, 50, 75, 85, 95, 99), s):
        p["q"][k] = p["proj"] * f


def read_live(sport, dk_path, proj_path):
    dk = list(csv.DictReader(open(dk_path, encoding="utf-8-sig")))
    pr = {}
    for r in csv.DictReader(open(proj_path, encoding="utf-8-sig")):
        pr[norm(r.get("name") or r.get("Name") or r.get("player"))] = r
    players, miss = [], []
    for i, r in enumerate(dk):
        name = r.get("Name") or r.get("name")
        q = pr.get(norm(name))
        if not q:
            miss.append(name)
        proj = float((q or {}).get("proj") or (q or {}).get("Projection") or 0) if q else 0.0
        pos_s = r.get("Roster Position") or r.get("Position") or ""
        pos = set(t for t in pos_s.split("/") if t)
        cpt = "CPT" in pos
        if sport in NO_POSITIONS and not cpt:
            pos = {"CNSTR"} if "CNSTR" in pos else {"X"}
        team = r.get("TeamAbbrev") or r.get("Team") or (q or {}).get("team") or ""
        gi = r.get("Game Info") or ""
        m = re.match(r"(\w+)@(\w+)", gi)
        opp = ""
        if m:
            a, h = m.group(1), m.group(2)
            opp = h if team == a else a
        opp = (q or {}).get("opp") or opp
        qs = {k: float(q.get(f"p{k}") or 0) for k in (25, 50, 75, 85, 95, 99)} if q else {k: 0.0 for k in (25, 50, 75, 85, 95, 99)}
        p = {"i": i, "uuid": r.get("ID") or str(i), "base": (r.get("ID") or str(i)), "cpt": cpt, "name": name,
             "salary": int(float(r.get("Salary") or 0)), "pos": pos, "pos_str": pos_s, "team": team, "opp": opp,
             "home": team == (m.group(2) if m else ""), "game": "@".join(sorted([team, opp])) if team and opp else "",
             "proj": max(0.0, proj), "q": qs, "actual": None, "own": {}, "ss_own": None, "status": "",
             "line": (q or {}).get("line", ""), "order": (q or {}).get("order", ""), "tee": ""}
        if cpt:
            p["base"] = p["base"] + "_base"      # DK gives CPT rows their own ID; pair them by name instead
            p["base"] = "n:" + norm(name)
        else:
            p["base"] = "n:" + norm(name) if sport in ("F1", "LOL") else p["base"]
        fill_quantiles(sport, p)
        players.append(p)
    if miss:
        print(f"{len(miss)} DK players without a projection (treated as 0): {', '.join(miss[:12])}{' ...' if len(miss) > 12 else ''}", file=sys.stderr)
    return {"sport": sport, "date": "live", "players": players, "meta": {}, "root": ""}


def assign_slots(sport, ps):
    """Players -> roster slots by maximum bipartite matching (Hall's condition made the lineup feasible)."""
    from scipy.optimize import linear_sum_assignment
    slots = RULES[sport]["slots"]
    cost = np.full((len(ps), len(slots)), 1e6)
    for i, p in enumerate(ps):
        for j, (_, tok) in enumerate(slots):
            if p["pos"] & tok:
                cost[i, j] = 0
    r, c = linear_sum_assignment(cost)
    out = [None] * len(slots)
    for i, j in zip(r, c):
        out[j] = ps[i]
    return out


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--archive", nargs=2, metavar=("SPORT", "DATE"))
    ap.add_argument("--sport")
    ap.add_argument("--dk")
    ap.add_argument("--proj")
    ap.add_argument("--n", type=int, default=20)
    ap.add_argument("--sims", type=int, default=5000)
    ap.add_argument("--stack", default="", help="comma list, e.g. 5,3 (MLB) or 3 (NHL line / NBA game)")
    ap.add_argument("--bringback", action="store_true")
    ap.add_argument("--allow-opp", action="store_true", help="allow hitters vs your pitcher / skaters vs your goalie")
    ap.add_argument("--rank", default="p90", choices=["p90", "p98", "mean", "p50"])
    ap.add_argument("--max-exp", type=float, default=0.5)
    ap.add_argument("--min-uniq", type=int, default=3)
    ap.add_argument("--candidates", type=int, default=4)
    ap.add_argument("--min-salary", type=int, default=0)
    ap.add_argument("--lock", action="append", default=[])
    ap.add_argument("--exclude", action="append", default=[])
    ap.add_argument("--set", action="append", default=[], help="name=projection override")
    ap.add_argument("--seed", type=int)
    ap.add_argument("--out")
    ap.add_argument("--grade", action="store_true", help="archive only: place the lineups in the real fields")
    a = ap.parse_args(argv)
    if a.archive:
        sport, date = a.archive[0].upper(), a.archive[1]
        slate = load_slate(sport, date)
    else:
        if not (a.sport and a.dk and a.proj):
            ap.error("--archive SPORT DATE or --sport/--dk/--proj")
        sport = a.sport.upper()
        slate = read_live(sport, a.dk, a.proj)
    byname = {}
    for p in slate["players"]:
        byname.setdefault(norm(p["name"]), []).append(p)
    for s in a.set:
        nm, v = s.rsplit("=", 1)
        for p in byname.get(norm(nm), []):
            p["proj"] = float(v); p["q"] = {k: 0.0 for k in p["q"]}; fill_quantiles(sport, p)
    ps, D = sim_matrix(slate, a.sims, seed=a.seed)
    idx = {p["uuid"]: k for k, p in enumerate(ps)}
    locks = {idx[p["uuid"]] for nm in a.lock for p in byname.get(norm(nm), []) if p["uuid"] in idx}
    excl = {idx[p["uuid"]] for nm in a.exclude for p in byname.get(norm(nm), []) if p["uuid"] in idx}
    opts = Opts(n=a.n, stacks=[int(x) for x in a.stack.split(",") if x], bringback=a.bringback, no_opp=not a.allow_opp,
                rank=a.rank, max_exp=a.max_exp, min_uniq=a.min_uniq, candidates=a.candidates, min_salary=a.min_salary,
                locks=locks, excludes=excl, seed=a.seed)
    lineups = build(slate, ps, D, opts, log=lambda *x: print(*x, file=sys.stderr))
    fields = {k: Field(slate, k) for k in field_kinds(slate)} if (a.archive and a.grade) else {}
    slots = [s for s, _ in RULES[sport]["slots"]]
    rows = []
    for n, c in enumerate(lineups, 1):
        ordered = assign_slots(sport, [ps[k] for k in c["idx"]])
        line = f"#{n:<3} ${c['salary']:<6} mean {c['mean']:6.1f}  p90 {c['p90']:6.1f}  " + " | ".join(f"{s}: {p['name']}" for s, p in zip(slots, ordered))
        if fields:
            act = sum(ps[k]["actual"] or 0 for k in c["idx"])
            line += f"   actual {act:.1f} " + " ".join(f"{k}:#{F.place(act)[0]}/${F.place(act)[1]:.0f}" for k, F in fields.items())
        print(line)
        rows.append([p["uuid"] if not a.archive else p["name"] for p in ordered])
    if a.out:
        with open(a.out, "w", newline="") as f:
            w = csv.writer(f); w.writerow(slots); w.writerows(rows)
        print(f"wrote {a.out}", file=sys.stderr)


if __name__ == "__main__":
    main()
