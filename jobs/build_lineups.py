"""
Command-line lineup builder — the same optimizer the website runs, in Python, so lineups can be
built (and reviewed) without a browser. Reads the slate board + sim matrix with the public key.

  python jobs/build_lineups.py --contest cash --n 1
  python jobs/build_lineups.py --contest gpp --n 20 --stack 1 --bringback --max-exp 0.5 --fade 0.4
  python jobs/build_lineups.py --contest gpp --n 20 --lock "Bijan Robinson" --exclude "Zay Flowers" \
      --set "George Kittle=9.5" --set-own "Kalif Raymond=25" --out lineups.csv --json lineups.json

Env: SUPABASE_URL + SUPABASE_ANON_KEY (read-only is enough).
Objective / rules mirror web/lineups.js:
  cash: 0.8*proj + 0.2*floor        gpp: 0.6*proj + 0.4*p85 - fade*0.06*own + jitter
  DK: 1 QB, 2-3 RB, 3-4 WR, 1-2 TE, 1 DST, 9 total, <= $50k, players from >= 2 games, max N per team
  GPP stacks: QB + n same-team WR/TE, optional bring-back (opponent RB/WR/TE)
  uniqueness across lineups, exposure cap, "candidates x" over-generate then keep best by sim p50 (cash) / p90 (gpp)
"""
from __future__ import annotations

import argparse
import csv
import gzip
import json
import os
import random
import sys
import urllib.request

import numpy as np
import pulp

from common import fetch_all, get_client, norm_name

SITES = {
    "DK": {"cap": 50000, "slots": ["QB", "RB", "RB", "WR", "WR", "WR", "TE", "FLEX", "DST"], "max_team": 8, "def": "DST"},
    "FD": {"cap": 60000, "slots": ["QB", "RB", "RB", "WR", "WR", "WR", "TE", "FLEX", "DEF"], "max_team": 4, "def": "DEF"},
}
SLOTS_PER_POS = {"QB": 1.0, "RB": 2.4, "WR": 3.4, "TE": 1.2, "DST": 1.0}
OUT_STATUSES = {"OUT", "IR", "O"}
INJ_MAP = {"Out": "OUT", "Doubtful": "D", "Questionable": "Q"}


# ------------------------------------------------------------------ data
def load_board(client, slate_key=None):
    q = client.table("slates").select("*")
    q = q.eq("slate_key", slate_key) if slate_key else q.order("imported_at", desc=True).limit(1)
    slates = q.execute().data
    if not slates:
        raise SystemExit("no slate")
    slate = slates[0]
    rows = fetch_all(client.table("slate_board").select("*").eq("slate_id", slate["slate_id"]), order="site_player_id")
    for r in rows:
        for k in ("mean", "stdev", "median", "p15", "p85", "p95", "floor", "ceiling", "boom_prob", "bust_prob", "salary"):
            r[k] = float(r[k]) if r.get(k) is not None else None
    ext = {}
    try:
        for r in fetch_all(client.table("slate_projections").select("site_player_id,mean,components")
                           .eq("slate_id", slate["slate_id"]).eq("method", "external"), order="site_player_id"):
            ext[r["site_player_id"]] = {"mean": float(r["mean"]), "own": (r.get("components") or {}).get("ownership")}
    except Exception:
        pass
    own_model = {"b": 1.4, "c": 0.8, "cap": 60.0}
    try:
        mp = client.table("model_params").select("param_value").eq("param_key", "ownership_model").execute().data
        if mp and mp[0]["param_value"].get("b") is not None:
            own_model = {k: float(mp[0]["param_value"][k]) for k in ("b", "c", "cap")}
    except Exception:
        pass
    matrix = None
    url = (slate.get("sim_meta") or {}).get("url")
    if url:
        raw = urllib.request.urlopen(url, timeout=60).read()
        d = json.loads(gzip.decompress(raw) if raw[:2] == b"\x1f\x8b" else raw)
        matrix = {pid: np.array(s, dtype=np.float32) for pid, s in zip(d["players"], d["scores"])}
    return slate, rows, ext, own_model, matrix


def heuristic_own(rows, proj, model):
    out = {}
    for pos, slots in SLOTS_PER_POS.items():
        pool = [r for r in rows if r["position"] == pos and proj(r) > 0 and eff_status(r) not in OUT_STATUSES]
        if not pool:
            continue
        val = np.array([proj(r) / (r["salary"] / 1000) for r in pool]); pr = np.array([proj(r) for r in pool])
        z = lambda a: (a - a.mean()) / (a.std() or 1)
        w = np.exp(model["b"] * z(val) + model["c"] * z(pr)) * np.array([0.7 if eff_status(r) in ("Q", "D") else 1.0 for r in pool])
        for r, wi in zip(pool, w):
            out[r["site_player_id"]] = float(min(model["cap"], 100 * slots * wi / w.sum()))
    return out


def eff_status(r):
    return (r.get("status") or "").upper() or INJ_MAP.get(r.get("injury_report") or "", "")


# ------------------------------------------------------------------ optimizer
def solve(pool, score, site, opts, prior, blocked, locks):
    prob = pulp.LpProblem("lineup", pulp.LpMaximize)
    x = {p["site_player_id"]: pulp.LpVariable("x_" + p["site_player_id"].replace("-", "_"), cat="Binary") for p in pool}
    by = lambda f: [x[p["site_player_id"]] for p in pool if f(p)]
    prob += pulp.lpSum(score[p["site_player_id"]] * x[p["site_player_id"]] for p in pool)
    prob += pulp.lpSum(p["salary"] * x[p["site_player_id"]] for p in pool) <= site["cap"]
    if opts.min_salary:
        prob += pulp.lpSum(p["salary"] * x[p["site_player_id"]] for p in pool) >= opts.min_salary
    prob += pulp.lpSum(x.values()) == 9
    prob += pulp.lpSum(by(lambda p: p["position"] == "QB")) == 1
    prob += pulp.lpSum(by(lambda p: p["position"] == "DST")) == 1
    prob += pulp.lpSum(by(lambda p: p["position"] == "RB")) >= 2
    prob += pulp.lpSum(by(lambda p: p["position"] == "RB")) <= 3
    prob += pulp.lpSum(by(lambda p: p["position"] == "WR")) >= 3
    prob += pulp.lpSum(by(lambda p: p["position"] == "WR")) <= 4
    prob += pulp.lpSum(by(lambda p: p["position"] == "TE")) >= 1
    prob += pulp.lpSum(by(lambda p: p["position"] == "TE")) <= 2
    prob += pulp.lpSum(by(lambda p: p["position"] in ("RB", "WR", "TE"))) == 7
    for t in {p["team"] for p in pool}:
        prob += pulp.lpSum(by(lambda p, t=t: p["team"] == t)) <= min(opts.max_team, site["max_team"])
    for g in {p["game_id"] for p in pool if p.get("game_id")}:
        prob += pulp.lpSum(by(lambda p, g=g: p.get("game_id") == g)) <= 8
    if opts.contest == "gpp":
        for qb in [p for p in pool if p["position"] == "QB"]:
            if opts.stack > 0:
                prob += pulp.lpSum(by(lambda p, qb=qb: p["team"] == qb["team"] and p["position"] in ("WR", "TE"))) >= opts.stack * x[qb["site_player_id"]]
            if opts.bringback:
                prob += pulp.lpSum(by(lambda p, qb=qb: p["team"] == qb["opponent"] and p["position"] in ("RB", "WR", "TE"))) >= x[qb["site_player_id"]]
    for L in prior:
        prob += pulp.lpSum(x[i] for i in L if i in x) <= 9 - opts.min_uniq
    for i in locks:
        if i in x:
            prob += x[i] == 1
    for i in blocked:
        if i in x and i not in locks:
            prob += x[i] == 0
    prob.solve(pulp.PULP_CBC_CMD(msg=False, timeLimit=20))
    if pulp.LpStatus[prob.status] != "Optimal":
        return None
    return [i for i, v in x.items() if v.value() is not None and v.value() > 0.5]


def lineup_stats(ids, byid, proj, own, matrix):
    ps = [byid[i] for i in ids]
    out = {"ids": ids, "salary": sum(p["salary"] for p in ps), "proj": round(sum(proj(p) for p in ps), 1),
           "own": round(sum(own.get(p["site_player_id"], 0) for p in ps)), "players": ps}
    if matrix:
        n = len(next(iter(matrix.values())))
        tot = np.zeros(n, dtype=np.float32)
        for p in ps:
            a = matrix.get(p["site_player_id"])
            tot += a if a is not None else proj(p)
        q = np.percentile(tot, [10, 50, 90, 98])
        out.update({"p10": round(float(q[0]), 1), "p50": round(float(q[1]), 1), "p90": round(float(q[2]), 1), "p98": round(float(q[3]), 1)})
    return out


def assign_slots(ps, site):
    take = lambda pos, n: sorted([p for p in ps if p["position"] == pos], key=lambda p: -p["salary"])[:n]
    used, slots = set(), {}
    def put(k, p): slots[k] = p; used.add(p["site_player_id"])
    put("QB", take("QB", 1)[0]); put(site["def"], take("DST", 1)[0])
    for i, p in enumerate(take("RB", 2)): put(f"RB{i+1}", p)
    for i, p in enumerate(take("WR", 3)): put(f"WR{i+1}", p)
    put("TE", take("TE", 1)[0])
    put("FLEX", next(p for p in ps if p["site_player_id"] not in used and p["position"] in ("RB", "WR", "TE")))
    return slots


# ------------------------------------------------------------------ main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--slate-key")
    ap.add_argument("--contest", choices=["cash", "gpp"], default="cash")
    ap.add_argument("--n", type=int, default=1)
    ap.add_argument("--candidates", type=float, default=2.0, help="build n x this, keep best by sim")
    ap.add_argument("--stack", type=int, default=1)
    ap.add_argument("--bringback", action="store_true")
    ap.add_argument("--max-team", type=int, default=4)
    ap.add_argument("--max-exp", type=float, default=0.6)
    ap.add_argument("--min-uniq", type=int, default=2)
    ap.add_argument("--rand", type=float, default=0.15)
    ap.add_argument("--fade", type=float, default=0.4)
    ap.add_argument("--min-salary", type=int, default=0)
    ap.add_argument("--exclude-q", action="store_true")
    ap.add_argument("--blend", type=float, default=0.0, help="weight on external projections if imported")
    ap.add_argument("--lock", action="append", default=[], help="player name (repeatable)")
    ap.add_argument("--exclude", action="append", default=[])
    ap.add_argument("--set", action="append", default=[], help='"Name=proj" projection override')
    ap.add_argument("--set-own", action="append", default=[], help='"Name=own%%" ownership override')
    ap.add_argument("--seed", type=int, default=None)
    ap.add_argument("--out", help="DK/FD upload CSV path")
    ap.add_argument("--json", help="lineup details JSON path")
    args = ap.parse_args()
    if args.seed is not None:
        random.seed(args.seed); np.random.seed(args.seed)

    client = get_client()
    slate, rows, ext, own_model, matrix = load_board(client, args.slate_key)
    site = SITES[slate["site"]]
    byid = {r["site_player_id"]: r for r in rows}
    byname = {}
    for r in rows:
        byname.setdefault(norm_name(r["player_name"]), []).append(r)

    def find(name):
        c = byname.get(norm_name(name))
        if not c:
            raise SystemExit(f"player not on slate: {name}")
        return c[0]

    overrides = {find(s.split("=")[0])["site_player_id"]: float(s.split("=")[1]) for s in args.set}
    own_over = {find(s.split("=")[0])["site_player_id"]: float(s.split("=")[1]) for s in args.set_own}
    locks = {find(n)["site_player_id"] for n in args.lock}
    excludes = {find(n)["site_player_id"] for n in args.exclude}

    def proj(r):
        i = r["site_player_id"]
        if i in overrides:
            return overrides[i]
        m = r["mean"] or 0.0
        e = ext.get(i)
        if e and args.blend > 0:
            return (1 - args.blend) * m + args.blend * e["mean"] if r["mean"] is not None else e["mean"]
        return m

    heur = heuristic_own(rows, proj, own_model)
    def own(i):
        if i in own_over:
            return own_over[i]
        e = ext.get(i)
        if e and e.get("own") is not None and args.blend > 0:
            return float(e["own"])
        return heur.get(i, 0.0)
    own_map = {r["site_player_id"]: own(r["site_player_id"]) for r in rows}

    pool = [r for r in rows if r["site_player_id"] not in excludes and r["mean"] is not None and proj(r) > 0
            and eff_status(r) not in OUT_STATUSES and not (args.exclude_q and eff_status(r) in ("Q", "D"))]
    print(f"{slate['slate_key']} · {len(pool)} eligible players · sim matrix {'loaded' if matrix else 'MISSING'} · "
          f"ownership {'fitted' if own_model['b'] != 1.4 else 'heuristic'}{' · external blend ' + str(args.blend) if ext else ''}", file=sys.stderr)

    n_cand = int(min(300, max(args.n, round(args.n * (args.candidates if matrix else 1)))))
    lineups, usage, blocked = [], {}, set()
    for k in range(n_cand):
        score = {}
        for p in pool:
            m = proj(p)
            base = 0.8 * m + 0.2 * (p["floor"] or 0) if args.contest == "cash" else 0.6 * m + 0.4 * ((p["p85"] or m) * (m / max(p["mean"] or 0.1, 0.1)))
            jit = 1 + (args.rand * random.gauss(0, 1) * ((p["stdev"] or 5) / max(m, 1)) if args.contest == "gpp" else 0)
            score[p["site_player_id"]] = base * jit - (args.fade * 0.06 * own_map[p["site_player_id"]] if args.contest == "gpp" else 0)
        ids = solve(pool, score, site, args, [L["ids"] for L in lineups], blocked, locks)
        if not ids:
            print(f"stopped at {k} candidates — no feasible lineup left", file=sys.stderr)
            break
        lineups.append(lineup_stats(ids, byid, proj, own_map, matrix))
        for i in ids:
            usage[i] = usage.get(i, 0) + 1
            if i not in locks and usage[i] >= max(1, round(args.max_exp * n_cand)):
                blocked.add(i)
    key = "p50" if args.contest == "cash" else "p90"
    if matrix:
        lineups.sort(key=lambda L: -L.get(key, L["proj"]))
    cap, used, kept = max(1, int(np.ceil(args.max_exp * args.n))), {}, []
    for L in lineups:
        if len(kept) >= args.n:
            break
        if all(i in locks or used.get(i, 0) < cap for i in L["ids"]):
            kept.append(L)
            for i in L["ids"]:
                used[i] = used.get(i, 0) + 1
    lineups = kept

    order = {"QB": 0, "RB": 1, "WR": 2, "TE": 3, "DST": 4}
    for n, L in enumerate(lineups, 1):
        sim = f" · sim p10 {L['p10']} p50 {L['p50']} p90 {L['p90']}" if "p50" in L else ""
        sal = int(L["salary"])
        print(f"\n#{n}  ${sal:,}  proj {L['proj']}  own {L['own']}%{sim}")
        for p in sorted(L["players"], key=lambda p: (order[p["position"]], -p["salary"])):
            st = eff_status(p)
            psal = int(p["salary"])
            print(f"   {p['position']:3} {p['player_name']:<24} {p['team']:<4} v {p['opponent']:<4} ${psal:<6} {proj(p):5.1f}  own {own_map[p['site_player_id']]:4.0f}%{('  [' + st + ']') if st else ''}")
    exp = {}
    for L in lineups:
        for i in L["ids"]:
            exp[i] = exp.get(i, 0) + 1
    if len(lineups) > 1:
        print("\nexposure: " + " · ".join(f"{byid[i]['player_name']} {round(100 * c / len(lineups))}%" for i, c in sorted(exp.items(), key=lambda kv: -kv[1])[:12]))

    if args.out:
        keys = ["QB", "RB1", "RB2", "WR1", "WR2", "WR3", "TE", "FLEX", site["def"]]
        with open(args.out, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(site["slots"])
            for L in lineups:
                s = assign_slots(L["players"], site)
                w.writerow([s[k]["site_player_id"] for k in keys])
        print(f"\nwrote {args.out} ({len(lineups)} lineups)", file=sys.stderr)
    if args.json:
        json.dump([{**{k: v for k, v in L.items() if k != "players"},
                    "players": [{"name": p["player_name"], "pos": p["position"], "team": p["team"], "opp": p["opponent"],
                                 "salary": p["salary"], "proj": round(proj(p), 1), "own": round(own_map[p["site_player_id"]]),
                                 "status": eff_status(p) or None, "id": p["site_player_id"]} for p in L["players"]]} for L in lineups],
                  open(args.json, "w"), indent=1)


if __name__ == "__main__":
    main()
