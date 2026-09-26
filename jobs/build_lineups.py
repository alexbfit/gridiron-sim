"""
Command-line lineup builder — the same optimizer the website runs, in Python, so lineups can be
built (and reviewed) without a browser. Reads the slate board + sim matrix with the public key.

  python jobs/build_lineups.py --contest cash --n 1
  python jobs/build_lineups.py --contest gpp --n 20 --stack 1 --bringback --max-exp 0.5 --fade 0.4
  python jobs/build_lineups.py --contest gpp --n 20 --lock "Bijan Robinson" --exclude "Zay Flowers" \
      --set "George Kittle=9.5" --set-own "Kalif Raymond=25" --out lineups.csv --json lineups.json
  python jobs/build_lineups.py --contest gpp --n 20 ... --save --note "faded X (DNP Fri)"   # record for Monday scoring
  python jobs/build_lineups.py --contest gpp --n 50 --objective ev --exposure "Trey McBride=10" --exposure "Bijan Robinson=0"
      (a stand: force >= 10% of lineups to hold McBride; 0 = fade. Prints a leverage table = exposure - projected ownership.)

Env: SUPABASE_URL + SUPABASE_ANON_KEY (read-only is enough; --save goes through the save_lineups RPC).
Objective / rules mirror web/lineups.js:
  cash: 0.8*proj + 0.2*floor        gpp: 0.6*proj + 0.4*p85 - fade*0.06*own + jitter
  DK: 1 QB, 2-3 RB, 3-4 WR, 1-2 TE, 1 DST, 9 total, <= $50k, players from >= 2 games, max N per team
  GPP stacks: QB + n same-team WR/TE, optional bring-back (opponent RB/WR/TE)
  uniqueness across lineups, exposure cap, "candidates x" over-generate then keep best by sim p50 (cash) / p90 (gpp)

Contest-aware GPP selection (--objective ev): over-generate candidates with varied fade/jitter, sample a
field of opponents from projected ownership, and keep the lineups with the best expected payout against
that field across the stored sims (see contest_sim.py). Structural knobs the winners use:
  --max-own 120        cap the lineup's summed ownership %
  --stack-rb           let the QB's RB count toward --stack (QB + RB + TE is a stack)
  --objective ev --entries 200000 --payout milly --fee 20 --field 4000
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

from common import fetch_all, get_client, norm_name, norm_team

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
    props = {}
    try:
        for r in fetch_all(client.table("slate_projections").select("site_player_id,mean,components")
                           .eq("slate_id", slate["slate_id"]).eq("method", "props"), order="site_player_id"):
            props[r["site_player_id"]] = (float(r["mean"]), (r.get("components") or {}).get("src"))
    except Exception:
        pass
    for r in rows:
        pv = props.get(r["site_player_id"])
        r["props"] = pv[0] if pv else None
        r["props_src"] = pv[1] if pv else None
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


def read_own_file(path, byname):
    """{site_player_id: own%} from a CSV with a player-name column and an ownership column.
    Handles DK contest-standings exports (Player / %Drafted per roster slot -> summed per player)."""
    with open(path, encoding="utf-8-sig", newline="") as f:
        rows = list(csv.reader(f))
    hdr = [h.strip().lower() for h in rows[0]]
    pi = next((i for i, h in enumerate(hdr) if h in ("player", "player name", "name")), None)
    oi = next((i for i, h in enumerate(hdr) if h in ("%drafted", "% drafted", "ownership", "own", "own%", "proj own", "projected ownership")), None)
    if pi is None or oi is None:
        raise SystemExit(f"--own-file: need player + ownership columns, got {rows[0]}")
    out = {}
    for r in rows[1:]:
        if len(r) <= max(pi, oi) or not r[pi] or not r[oi]:
            continue
        c = byname.get(norm_name(r[pi]))
        if not c:
            continue
        try:
            v = float(r[oi].replace("%", ""))
        except ValueError:
            continue
        out[c[0]["site_player_id"]] = out.get(c[0]["site_player_id"], 0.0) + v
    return out


# ------------------------------------------------------------------ optimizer
MISSING_DEFAULT = "QB=0.15,RB=0.8,WR=0.4,TE=0.5"


def apply_missing(rows, matrix, have, spec, overrides=(), min_team=4):
    """Market-missing discount. When a team's player markets are posted (its QB plus >= min_team-1 others have a
    projection in `have`), a skill player on that team with NO market is usually inactive, a backup or a gadget
    player: scale his mean and sim draws by the per-position factor in `spec` ("QB=0.15,RB=0.8,..."). 2024 wk1-15:
    props + this discount r .648 / RMSE 6.70 vs SaberSim .632 / 6.82 on every board player (props alone .621)."""
    if not spec or not have:
        return 0
    fac = {k.strip().upper(): float(v) for k, v in (x.split("=") for x in spec.split(",") if "=" in x)}
    by_team = {}
    for r in rows:
        if r["position"] in fac:
            by_team.setdefault(r["team"], []).append(r)
    n = 0
    for team, rs in by_team.items():
        posted = [r for r in rs if r["site_player_id"] in have]
        if len(posted) < min_team or not any(r["position"] == "QB" for r in posted):
            continue
        for r in rs:
            i = r["site_player_id"]
            if i in have or i in overrides or r["mean"] is None or r["mean"] < 1.0:
                continue
            f = fac.get(r["position"], 1.0)
            for k in ("mean", "median", "p15", "p85", "p95", "floor", "ceiling"):
                if r.get(k) is not None:
                    r[k] = r[k] * f
            if matrix is not None and i in matrix:
                matrix[i] = matrix[i] * np.float32(f)
            n += 1
    return n


def apply_props(rows, matrix, weight, overrides=()):
    """Pull each player's board mean toward his props projection (weight 0..1) and rescale his quantiles and sim
    draws by the same factor, so correlations survive. Shared by the builder, late_swap and flashback."""
    n_p = 0
    if not weight or weight <= 0:
        return 0
    for r in rows:
        pv = r.get("props")
        if pv is None or r["site_player_id"] in overrides or r["mean"] is None or eff_status(r) in OUT_STATUSES:
            continue
        new = (1 - weight) * r["mean"] + weight * pv
        f = new / r["mean"] if r["mean"] > 0.5 else 1.0
        f = min(max(f, 0.25), 4.0)
        for k in ("mean", "median", "p15", "p85", "p95", "floor", "ceiling"):
            if r.get(k) is not None:
                r[k] = r[k] * f
        if r["mean"] == 0 and pv > 0:
            r["mean"] = new
        if matrix is not None and r["site_player_id"] in matrix:
            matrix[r["site_player_id"]] = matrix[r["site_player_id"]] * np.float32(f)
        n_p += 1
    return n_p


EXT_ALIASES = {
    "name": ["name", "player", "player name", "player_name", "nickname", "full name"],
    "team": ["team", "tm", "teamabbrev", "team abbrev"],
    "pos": ["pos", "position", "roster position"],
    "proj": ["ss proj", "ssproj", "saber proj", "sabersim proj", "ss projection", "proj", "projection", "projected points", "fpts", "points",
             "dk proj", "dkproj", "dk points", "fantasy points", "proj pts", "my proj", "median"],
    "own": ["own", "own%", "ownership", "proj own", "%drafted", "pown", "flagship mme own", "adj own"],
}


def read_ext_file(path, rows):
    """{site_player_id: {"mean", "own"}} from an outside projections CSV (e.g. a SaberSim projections export).
    Any file with a player-name column and a projection column; team/position/ownership optional."""
    with open(path, encoding="utf-8-sig", newline="") as f:
        data = list(csv.DictReader(f))
    if not data:
        return {}
    hdr = {h.lower().strip(): h for h in data[0].keys()}
    col = {k: next((hdr[a] for a in al if a in hdr), None) for k, al in EXT_ALIASES.items()}
    if not col["name"] or not col["proj"]:
        raise SystemExit(f"--ext-file: need a player-name and a projection column, got {list(data[0].keys())}")
    idx = {}
    for r in rows:
        idx.setdefault(norm_name(r["player_name"]), []).append(r)
    out = {}
    for d in data:
        try:
            v = float(str(d[col["proj"]]).replace(",", ""))
        except (TypeError, ValueError):
            continue
        c = idx.get(norm_name(d[col["name"]] or ""), [])
        if len(c) > 1 and col["team"]:
            c = [x for x in c if x.get("team") == norm_team(d[col["team"]])] or c
        if len(c) > 1 and col["pos"]:
            c = [x for x in c if x.get("position") == str(d[col["pos"]]).strip().upper()[:3].replace("DEF", "DST")] or c
        if not c:
            continue
        own = None
        if col["own"] and d.get(col["own"]) not in (None, ""):
            try:
                own = float(str(d[col["own"]]).replace("%", ""))
            except ValueError:
                pass
        out[c[0]["site_player_id"]] = {"mean": v, "own": own}
    return out


def apply_ext(rows, matrix, ext, weight, overrides=()):
    """Pull each player's board mean toward the outside projection (weight 0..1) and rescale his quantiles and sim
    draws by the same factor (correlations kept), like the props blend. A 0 outside projection zeroes the player at any weight;
    players the sim has at ~0 are left alone (no sim draws to rescale)."""
    n = 0
    if not weight or weight <= 0 or not ext:
        return 0
    for r in rows:
        e = ext.get(r["site_player_id"])
        if not e or r["site_player_id"] in overrides or r["mean"] is None:
            continue
        # an outside 0 means "not playing" (SaberSim zeroes inactives) - zero him at any blend weight
        new = (1 - weight) * r["mean"] + weight * e["mean"] if e["mean"] > 0 else 0.0
        if r["mean"] > 0.3:
            f = max(new / r["mean"], 0.0)
            for k in ("mean", "median", "p15", "p85", "p95", "floor", "ceiling"):
                if r.get(k) is not None:
                    r[k] = r[k] * f
            if matrix is not None and r["site_player_id"] in matrix:
                matrix[r["site_player_id"]] = matrix[r["site_player_id"]] * np.float32(f)
            n += 1
    return n


# --- correlation calibration -------------------------------------------------------------------------------
# The sim's player-vs-player correlations were checked against 2024-25 outcomes (Contest Flashback archive,
# ~750 team-weeks, residual = actual - SaberSim projection). It under-links a QB to his WR1/WR2 (.27/.22 vs
# real .39/.38) and the two sides of a game (QB-opp QB -.02 vs .13, WR1-opp WR1 .02 vs .12), and over-links a
# QB to his own RB1 and DST (.21/.10 vs .04/-.08) and RB1 to RB2 (.03 vs -.08). apply_corr() nudges every
# game's sim draws toward those real correlations: Gaussian-copula normal scores -> the smallest linear map that
# takes the sim's correlation C to the target T (C + shrunk gap per role pair) -> back to each player's own
# sorted draws. Every player's distribution (mean, p85, ...) is unchanged; only who-booms-with-whom moves.
CORR_ROLES = (("QB", 1), ("RB", 2), ("WR", 3), ("TE", 1), ("DST", 1))
CORR_DELTA = {  # pooled 2024+2025, shrunk (tau .08), |d| >= .03
    "opp DST-DST": 0.032, "opp QB-QB": 0.108, "opp QB-RB1": 0.095, "opp QB-RB2": 0.04, "opp QB-TE": 0.044, "opp QB-WR1": 0.048,
    "opp RB1-RB1": -0.03, "opp RB2-RB2": 0.036, "opp RB2-TE": -0.036, "opp RB2-WR1": -0.034, "opp TE-DST": 0.041, "opp WR1-DST": -0.108,
    "opp WR1-TE": 0.053, "opp WR1-WR1": 0.07, "opp WR2-DST": -0.061, "opp WR2-WR2": 0.08, "opp WR3-DST": -0.042,
    "same QB-DST": -0.15, "same QB-RB1": -0.146, "same QB-RB2": -0.057, "same QB-WR1": 0.1, "same QB-WR2": 0.129, "same QB-WR3": 0.039,
    "same RB1-DST": -0.081, "same RB1-RB2": -0.095, "same RB1-WR2": -0.043, "same RB2-DST": -0.063, "same RB2-TE": -0.03,
    "same TE-DST": -0.082, "same WR1-TE": 0.056, "same WR1-WR2": 0.11, "same WR1-WR3": 0.03, "same WR2-DST": -0.036, "same WR2-TE": 0.057,
    "same WR2-WR3": 0.037, "same WR3-TE": 0.031,
}
_ROLE_IDX = {"QB": 0, "RB1": 1, "RB2": 2, "WR1": 3, "WR2": 4, "WR3": 5, "TE": 6, "DST": 7}
_ZTAB = {}


def _normal_scores(x):
    """Average-rank normal scores (ties share a score) without scipy."""
    n = len(x)
    if n not in _ZTAB:
        from statistics import NormalDist
        nd = NormalDist()
        _ZTAB[n] = np.array([nd.inv_cdf((j / 2 + 0.5) / n) for j in range(2 * n - 1)])
    order = np.argsort(x, kind="mergesort")
    xs = x[order]
    _, first, counts = np.unique(xs, return_index=True, return_counts=True)
    twice = np.repeat(2 * first + (counts - 1), counts)          # 2 x average rank, an integer
    z = np.empty(n)
    z[order] = _ZTAB[n][twice]
    return z


def _msqrt(a, inv=False):
    w, v = np.linalg.eigh(a)
    w = np.clip(w, 1e-6, None)
    return (v * (w ** (-0.5 if inv else 0.5))) @ v.T


def corr_roles(rows, skip=()):
    """{site_player_id: role} - per team, top-salary QB / RB1-2 / WR1-3 / TE / DST among players still projected."""
    by = {}
    for r in rows:
        if r["site_player_id"] in skip or (r.get("mean") or 0) < 1.0 or eff_status(r) in OUT_STATUSES:
            continue
        by.setdefault((r.get("team"), r["position"]), []).append(r)
    roles = {}
    for (team, pos), ps in by.items():
        n = dict(CORR_ROLES).get(pos)
        if not n:
            continue
        for k, r in enumerate(sorted(ps, key=lambda r: -(r.get("salary") or 0))[:n]):
            roles[r["site_player_id"]] = pos if n == 1 else f"{pos}{k + 1}"
    return roles


def apply_corr(rows, matrix, delta=None, skip=()):
    """Move each game's sim draws toward the real role-pair correlations (see CORR_DELTA). Returns games adjusted."""
    if not matrix:
        return 0
    delta = CORR_DELTA if delta is None else delta
    roles = corr_roles(rows, skip)
    games = {}
    for r in rows:
        i = r["site_player_id"]
        if r.get("game_id") and i in matrix:
            games.setdefault(r["game_id"], []).append(r)
    n_games = 0
    for gid, ps in games.items():
        ps = [r for r in ps if np.std(matrix[r["site_player_id"]]) > 0]
        if len(ps) < 3:
            continue
        ids = [r["site_player_id"] for r in ps]
        D = np.stack([np.asarray(matrix[i], dtype=np.float64) for i in ids], axis=1)      # sims x players
        Z = np.stack([_normal_scores(D[:, j]) for j in range(len(ids))], axis=1)
        Z -= Z.mean(axis=0)
        Z /= Z.std(axis=0)
        C = np.corrcoef(Z, rowvar=False)
        T = C.copy()
        touched = False
        for a in range(len(ps)):
            ra = roles.get(ids[a])
            if not ra:
                continue
            for b in range(a + 1, len(ps)):
                rb = roles.get(ids[b])
                if not rb:
                    continue
                same = ps[a].get("team") == ps[b].get("team")
                x, y = sorted((ra, rb), key=_ROLE_IDX.get)
                d = delta.get(f"{'same' if same else 'opp'} {x}-{y}")
                if d:
                    T[a, b] = T[b, a] = float(np.clip(C[a, b] + d, -0.95, 0.95))
                    touched = True
        if not touched:
            continue
        w, v = np.linalg.eigh(T)                                  # nearest PSD, unit diagonal
        T = (v * np.clip(w, 1e-4, None)) @ v.T
        s = np.sqrt(np.diag(T))
        T = T / np.outer(s, s)
        Ch, Ci = _msqrt(C), _msqrt(C, inv=True)
        M = Ci @ _msqrt(Ch @ T @ Ch) @ Ci                         # optimal-transport map C -> T (smallest change)
        Z2 = Z @ M
        for j, i in enumerate(ids):
            srt = np.sort(D[:, j])
            new = np.empty(len(srt))
            new[np.argsort(Z2[:, j], kind="mergesort")] = srt
            matrix[i] = new.astype(np.asarray(matrix[i]).dtype)
        n_games += 1
    return n_games


def solve(pool, score, site, opts, prior, blocked, locks, own_map=None):
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
        stack_pos = ("RB", "WR", "TE") if getattr(opts, "stack_rb", False) else ("WR", "TE")
        for qb in [p for p in pool if p["position"] == "QB"]:
            if opts.stack > 0:
                prob += pulp.lpSum(by(lambda p, qb=qb: p["team"] == qb["team"] and p["position"] in stack_pos)) >= opts.stack * x[qb["site_player_id"]]
            if opts.bringback:
                prob += pulp.lpSum(by(lambda p, qb=qb: p["team"] == qb["opponent"] and p["position"] in ("RB", "WR", "TE"))) >= getattr(opts, "bringback_n", 1) * x[qb["site_player_id"]]
        if getattr(opts, "rb_one_per_game", False):          # at most one RB from any game (teammates or opponents)
            for g in {p["game_id"] for p in pool if p.get("game_id") and p["position"] == "RB"}:
                prob += pulp.lpSum(by(lambda p, g=g: p["position"] == "RB" and p.get("game_id") == g)) <= 1
        if getattr(opts, "rb_dst", False):                   # the defense comes with one of its own team's RBs
            for d in [p for p in pool if p["position"] == "DST"]:
                prob += pulp.lpSum(by(lambda p, d=d: p["position"] == "RB" and p["team"] == d["team"])) >= x[d["site_player_id"]]
    if getattr(opts, "max_own", 0) and own_map:
        prob += pulp.lpSum(own_map.get(p["site_player_id"], 0.0) * x[p["site_player_id"]] for p in pool) <= opts.max_own
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
def parse_args(argv=None):
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
    ap.add_argument("--max-own", type=float, default=0, help="cap on a lineup's summed ownership %% (gpp)")
    ap.add_argument("--stack-rb", action="store_true", help="QB's RB counts toward --stack")
    ap.add_argument("--rb-one-per-game", action="store_true", help="gpp: at most one RB from any one game")
    ap.add_argument("--bringback-n", type=int, default=1, help="gpp with --bringback: how many opponents of the QB (2 = full game stack)")
    ap.add_argument("--ev-key", default="ev", choices=["ev", "p_top1", "p_top01"], help="--objective ev: rank candidates by expected profit or by P(top 1%%) / P(top 0.1%%) vs the simulated field")
    ap.add_argument("--max-qb-exp", type=float, default=0, help="gpp: separate (lower) exposure cap for QBs, e.g. 0.12 spreads 50 lineups over 9+ QBs")
    ap.add_argument("--ceil-weight", type=float, default=0.4, help="gpp score = (1-w)*proj + w*ceiling (default 0.4)")
    ap.add_argument("--ceil-q", default="p85", choices=["p85", "p95"], help="gpp: which percentile is the ceiling in the score")
    ap.add_argument("--rb-dst", action="store_true", help="gpp: pair the DST with one of its own team's RBs")
    ap.add_argument("--rank", choices=["p90", "p98", "proj"], default="p90",
                    help="gpp candidate ranking key from the sim: p90 (default), p98 (fatter tail — tournament-winner hunting), proj")
    ap.add_argument("--objective", choices=["default", "ev"], default="default",
                    help="ev: rank candidates by expected payout vs a simulated field (gpp only)")
    ap.add_argument("--entries", type=int, default=200000, help="contest size for --objective ev")
    ap.add_argument("--fee", type=float, default=20.0, help="entry fee for --objective ev")
    ap.add_argument("--payout", choices=["milly", "gpp", "se"], default="milly", help="payout curve for --objective ev")
    ap.add_argument("--field", type=int, default=4000, help="sampled opponent lineups for --objective ev")
    ap.add_argument("--exclude-q", action="store_true")
    ap.add_argument("--blend", type=float, default=0.0, help="weight on external projections if imported")
    ap.add_argument("--market", type=float, default=0.0,
                    help="pull RB/WR projections this far (0-1) toward the salary-implied line; sims are rescaled to match. "
                         "Early season (few games) 0.5 beat the raw sim on 2026 wk2.")
    ap.add_argument("--market-pos", default="RB,WR", help="positions the --market blend applies to")
    ap.add_argument("--props", type=float, default=0.85,
                    help="weight (0-1) on the sportsbook-props projection (jobs/props.py) for players who have one; the sim mean is "
                         "pulled toward it and his sim draws rescaled. 2024 backtest: props r .48 vs sim .35. 0 disables.")
    ap.add_argument("--props-missing", default=MISSING_DEFAULT,
                    help='per-position factor for skill players with NO props on a team whose markets are posted (likely '
                         'inactive / backup): "QB=0.15,RB=0.8,WR=0.4,TE=0.5" (default). "" disables. Only runs when props exist.')
    ap.add_argument("--ext-missing", default="",
                    help='same discount for players missing from --ext-file / imported external projections (off by default; '
                         'use it when the outside file is a props-style projection that only lists players with markets).')
    ap.add_argument("--corr", default="off",
                    help='"on" = calibrate the sim\'s player-vs-player correlations to 2024-25 outcomes (QB-WR1/WR2 and '
                         'both sides of a game up; QB-own RB1/DST and RB1-RB2 down; marginals unchanged), "off", or a JSON '
                         'file of role-pair deltas like CORR_DELTA.')
    ap.add_argument("--ext-file", help="CSV of outside projections (e.g. SaberSim's projections export: Name, Team, Pos, SS Proj ...), "
                                       "matched by name (+team/position). Used by --ext-sim.")
    ap.add_argument("--ext-sim", type=float, default=0.0,
                    help="weight (0-1) pulling each player's sim (mean, quantiles AND sim draws) toward the outside projection "
                         "(--ext-file, or projections imported with jobs/import_projections.py). 2024-26 contest backtest vs real "
                         "Millionaire fields: SaberSim projections at 1.0 beat the raw sim. Applied after --props. 0 = off.")
    ap.add_argument("--lock", action="append", default=[], help="player name (repeatable)")
    ap.add_argument("--exclude", action="append", default=[])
    ap.add_argument("--set", action="append", default=[], help='"Name=proj" projection override')
    ap.add_argument("--set-own", action="append", default=[], help='"Name=own%%" ownership override')
    ap.add_argument("--own-file", help="CSV of projected (or, for backtests, actual) ownership: any file with a player-name "
                                       "column and a %%-drafted/ownership column, incl. a DK contest-standings export "
                                       "(slot rows are summed). Replaces the heuristic for players it covers.")
    ap.add_argument("--exposure", action="append", default=[],
                    help='"Name=pct" target exposure across the final set, e.g. "Trey McBride=10" (repeatable). '
                         'A stand: the player is forced into at least that share of lineups. Use 0 to fade entirely.')
    ap.add_argument("--leverage", type=int, default=12, help="rows to show in the leverage table (exposure - projected ownership)")
    ap.add_argument("--seed", type=int, default=None)
    ap.add_argument("--out", help="DK/FD upload CSV path")
    ap.add_argument("--json", help="lineup details JSON path")
    ap.add_argument("--save", action="store_true", help="record the lineups in the DB (slate_lineups) so Monday's scoring grades them")
    ap.add_argument("--source", default="claude", choices=["claude", "web"])
    ap.add_argument("--note", default=None, help="short note stored with saved lineups (what was overridden and why)")
    ap.add_argument("--append", action="store_true", help="with --save: add to existing lineups instead of replacing")
    return ap.parse_args(argv)


def build(args, slate, rows, ext, own_model, matrix, quiet=False):
    """The builder proper: candidates -> (contest sim) -> selection. Returns (lineups, leverage, proj, own_map).
    rows = slate_board rows (mean/stdev/p85/floor/... per player), matrix = {site_player_id: sim draws} or None.
    Used by main() with the DB board and by jobs/contest_backtest.py with an offline board."""
    if args.seed is not None:
        random.seed(args.seed); np.random.seed(args.seed)
    log = (lambda *a: None) if quiet else (lambda *a: print(*a, file=sys.stderr))
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
    if args.market > 0:
        # salary-implied "market" line per position from the sim's own mean ~ salary fit, then pull
        # each player's mean toward it and rescale his sim draws by the same factor (keeps correlation)
        for pos in [p.strip().upper() for p in args.market_pos.split(",") if p.strip()]:
            ps = [r for r in rows if r["position"] == pos and (r["mean"] or 0) >= 3 and eff_status(r) not in OUT_STATUSES]
            if len(ps) < 8:
                continue
            b = np.polyfit([r["salary"] for r in ps], [r["mean"] for r in ps], 1)
            for r in ps:
                if r["site_player_id"] in overrides:
                    continue
                mkt = max(0.0, float(np.polyval(b, r["salary"])))
                new = (1 - args.market) * r["mean"] + args.market * mkt
                f = new / r["mean"] if r["mean"] else 1.0
                for k in ("mean", "median", "p15", "p85", "p95", "floor", "ceiling"):
                    if r.get(k) is not None:
                        r[k] = r[k] * f
                if matrix is not None and r["site_player_id"] in matrix:
                    matrix[r["site_player_id"]] = matrix[r["site_player_id"]] * np.float32(f)
        log(f"market blend {args.market:g} on {args.market_pos}")
    n_p = apply_props(rows, matrix, args.props, overrides)
    if n_p:
        log(f"props blend {args.props:g}: {n_p} players pulled toward the sportsbook projection")
        # only trust "no market = not playing" when sportsbook props are in (Kalshi alone lists ~5 players a team)
        have = {r["site_player_id"] for r in rows if r.get("props") is not None and (r.get("props_src") is None or "odds" in (r.get("props_src") or []))}
        n_m = apply_missing(rows, matrix, have, getattr(args, "props_missing", ""), overrides) if len(have) >= 100 else 0
        if n_m:
            log(f"props-missing discount ({args.props_missing}): {n_m} players with no market on a posted team")
    if getattr(args, "ext_file", None):
        ext = dict(ext)
        ext.update(read_ext_file(args.ext_file, rows))
        log(f"outside projections from {args.ext_file}: {len(ext)} players matched")
    n_e = apply_ext(rows, matrix, ext, getattr(args, "ext_sim", 0.0), overrides)
    if n_e:
        log(f"outside-projection blend {args.ext_sim:g}: {n_e} players pulled toward it (sim draws rescaled)")
        if getattr(args, "ext_missing", ""):
            n_m = apply_missing(rows, matrix, set(ext), args.ext_missing, overrides)
            log(f"ext-missing discount ({args.ext_missing}): {n_m} players not in the outside file on a posted team")
    corr = getattr(args, "corr", "off") or "off"
    if matrix and corr != "off":
        delta = None if corr == "on" else json.load(open(corr))
        n_c = apply_corr(rows, matrix, delta, {find(n)["site_player_id"] for n in args.exclude})
        log(f"correlation calibration ({corr}): {n_c} games")
    own_over = {}
    if args.own_file:
        own_over.update(read_own_file(args.own_file, byname))
        log(f"ownership from {args.own_file}: {len(own_over)} players")
    own_over.update({find(s.split("=")[0])["site_player_id"]: float(s.split("=")[1]) for s in args.set_own})
    locks = {find(n)["site_player_id"] for n in args.lock}
    excludes = {find(n)["site_player_id"] for n in args.exclude}
    exp_targets = {}
    for spec in args.exposure:
        name, pct = spec.rsplit("=", 1)
        pid = find(name)["site_player_id"]
        pct = float(pct)
        if pct <= 0:
            excludes.add(pid)
        else:
            exp_targets[pid] = min(pct, 100.0) / 100.0

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
    log(f"{slate['slate_key']} · {len(pool)} eligible players · sim matrix {'loaded' if matrix else 'MISSING'} · "
          f"ownership {'fitted' if own_model['b'] != 1.4 else 'heuristic'}{' · external blend ' + str(args.blend) if ext else ''}")

    use_ev = args.objective == "ev" and args.contest == "gpp" and matrix is not None
    if args.objective == "ev" and not use_ev:
        log("--objective ev needs --contest gpp and a sim matrix; falling back to default ranking")
    # EV mode wants a wide, varied candidate set: the field sim does the choosing, not the MIP score
    cand_mult = max(args.candidates, 6.0) if use_ev else args.candidates
    n_cand = int(min(600, max(args.n, round(args.n * (cand_mult if matrix else 1)))))
    lineups, usage, blocked = [], {}, set()
    for k in range(n_cand):
        score = {}
        # in EV mode sweep fade (0 .. 2x) and use heavier jitter so candidates span chalk -> contrarian
        fade_k = args.fade * (2.0 * k / max(n_cand - 1, 1)) if use_ev else args.fade
        rand_k = args.rand * (1.6 if use_ev else 1.0)
        for p in pool:
            m = proj(p)
            cw, cq = getattr(args, "ceil_weight", 0.4), getattr(args, "ceil_q", "p85")
            base = 0.8 * m + 0.2 * (p["floor"] or 0) if args.contest == "cash" else (1 - cw) * m + cw * ((p.get(cq) or p["p85"] or m) * (m / max(p["mean"] or 0.1, 0.1)))
            jit = 1 + (rand_k * random.gauss(0, 1) * ((p["stdev"] or 5) / max(m, 1)) if args.contest == "gpp" else 0)
            score[p["site_player_id"]] = base * jit - (fade_k * 0.06 * own_map[p["site_player_id"]] if args.contest == "gpp" else 0)
        ids = solve(pool, score, site, args, [L["ids"] for L in lineups], blocked, locks, own_map)
        if not ids:
            log(f"stopped at {k} candidates — no feasible lineup left")
            break
        lineups.append(lineup_stats(ids, byid, proj, own_map, matrix))
        for i in ids:
            usage[i] = usage.get(i, 0) + 1
            qcap = getattr(args, "max_qb_exp", 0) if byid[i]["position"] == "QB" else 0
            if i not in locks and (usage[i] >= max(1, round(args.max_exp * n_cand)) or (qcap and usage[i] >= max(1, round(qcap * n_cand)))):
                blocked.add(i)
    key = "p50" if args.contest == "cash" else getattr(args, "rank", "p90")
    if use_ev:
        from contest_sim import evaluate, sample_field
        rng = np.random.default_rng(args.seed)
        field_pool = [p for p in pool if own_map.get(p["site_player_id"], 0) > 0 or proj(p) >= 3]
        field = sample_field(field_pool, own_map, site["cap"], args.field, rng, max_team=site["max_team"])
        proj_by_id = lambda i: proj(byid[i])
        ev = evaluate([tuple(L["ids"]) for L in lineups], field, matrix, proj_by_id, args.entries, args.fee, args.payout, own=own_map)
        for L, e in zip(lineups, ev):
            L.update(e)
        key = getattr(args, "ev_key", "ev")
        log(f"contest sim: {len(field)} field lineups · {args.entries:,} entries · {args.payout} payouts · ${args.fee:g} fee · "
              f"{len(lineups)} candidates, best EV ${max(L['ev'] for L in lineups):.2f}")
        lineups.sort(key=lambda L: (-L[key], -L["ev"]))
    elif matrix:
        lineups.sort(key=lambda L: -L.get(key, L["proj"]))
    cap, used, kept = max(1, int(np.ceil(args.max_exp * args.n))), {}, []
    def take(L):
        kept.append(L)
        for i in L["ids"]:
            used[i] = used.get(i, 0) + 1
    qcap = int(np.ceil(args.max_qb_exp * args.n)) if getattr(args, "max_qb_exp", 0) else cap
    def fits(L, need=None):
        return all(i in locks or i == need or used.get(i, 0) < (qcap if byid[i]["position"] == "QB" else cap) for i in L["ids"])
    # exposure stands first: for each target, pull the best-ranked candidates containing the player until the share is met
    for pid, share in sorted(exp_targets.items(), key=lambda kv: -kv[1]):
        want = int(np.ceil(share * args.n))
        for L in lineups:
            if used.get(pid, 0) >= want or len(kept) >= args.n:
                break
            if L not in kept and pid in L["ids"] and fits(L, need=pid):
                take(L)
        if used.get(pid, 0) < want:
            log(f"exposure target {byid[pid]['player_name']} {share:.0%}: only {used.get(pid, 0)}/{want} lineups available in the candidate pool "
                  f"(raise --candidates or lower --min-uniq)")
    for L in lineups:
        if len(kept) >= args.n:
            break
        if L not in kept and fits(L):
            take(L)
    lineups = kept
    # leverage = your exposure - projected field ownership. Positive = a stand, negative = a fade.
    exp = {}
    for L in lineups:
        for i in L["ids"]:
            exp[i] = exp.get(i, 0) + 1
    lev = []
    if len(lineups) > 1:
        for r in pool:
            i = r["site_player_id"]
            e = 100.0 * exp.get(i, 0) / len(lineups)
            o = own_map.get(i, 0.0)
            if e > 0 or o >= 5:
                lev.append((e - o, e, o, r))
        lev.sort(key=lambda t: -t[0])
    return lineups, lev, proj, own_map


def main():
    args = parse_args()
    client = get_client()
    slate, rows, ext, own_model, matrix = load_board(client, args.slate_key)
    site = SITES[slate["site"]]
    byid = {r["site_player_id"]: r for r in rows}
    lineups, lev, proj, own_map = build(args, slate, rows, ext, own_model, matrix)

    order = {"QB": 0, "RB": 1, "WR": 2, "TE": 3, "DST": 4}
    for n, L in enumerate(lineups, 1):
        sim = f" · sim p10 {L['p10']} p50 {L['p50']} p90 {L['p90']}" if "p50" in L else ""
        evs = f" · EV ${L['ev']:+.2f} top1% {100 * L['p_top1']:.1f}% top0.1% {100 * L['p_top01']:.2f}% dups {L['dups']}" if "ev" in L else ""
        sal = int(L["salary"])
        print(f"\n#{n}  ${sal:,}  proj {L['proj']}  own {L['own']}%{sim}{evs}")
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
        k = max(1, args.leverage // 2)
        print(f"\nleverage (exposure - projected own%):")
        print("  STANDS")
        for d, e, o, r in lev[:k]:
            print(f"    {r['position']:3} {r['player_name']:<24} {r['team']:<4} exp {e:4.0f}%  own {o:4.0f}%  {d:+5.0f}")
        print("  FADES")
        for d, e, o, r in lev[-k:][::-1]:
            print(f"    {r['position']:3} {r['player_name']:<24} {r['team']:<4} exp {e:4.0f}%  own {o:4.0f}%  {d:+5.0f}")
        leverage_rows = [{"name": r["player_name"], "pos": r["position"], "team": r["team"], "exposure": round(e, 1),
                          "own": round(o, 1), "leverage": round(d, 1)} for d, e, o, r in lev]
    else:
        leverage_rows = []

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
        json.dump({"lineups": [{**{k: v for k, v in L.items() if k != "players"},
                    "players": [{"name": p["player_name"], "pos": p["position"], "team": p["team"], "opp": p["opponent"],
                                 "salary": p["salary"], "proj": round(proj(p), 1), "own": round(own_map[p["site_player_id"]]),
                                 "status": eff_status(p) or None, "id": p["site_player_id"]} for p in L["players"]]} for L in lineups],
                   "leverage": leverage_rows,
                   "exposure": {byid[i]["player_name"]: round(100 * c / max(1, len(lineups)), 1) for i, c in sorted(exp.items(), key=lambda kv: -kv[1])}},
                  open(args.json, "w"), indent=1)
    if args.save:
        payload = [{"ids": L["ids"], "proj": L["proj"], "own": L["own"], "p10": L.get("p10"), "p50": L.get("p50"), "p90": L.get("p90"),
                    "note": args.note} for L in lineups]
        try:
            n = client.rpc("save_lineups", {"p_slate_key": slate["slate_key"], "p_source": args.source, "p_contest": args.contest,
                                            "p_lineups": payload, "p_replace": not args.append}).execute().data
            print(f"saved {n} {args.contest} lineups to slate_lineups ({args.source}) — scored automatically after the games", file=sys.stderr)
        except Exception as e:
            print(f"save failed: {e}", file=sys.stderr)
            sys.exit(2)


if __name__ == "__main__":
    main()
