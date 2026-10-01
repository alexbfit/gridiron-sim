"""Sport-generic DraftKings lineup optimizer (MIP, pulp/CBC) driven by rules.RULES.

Variables x[p, s] = player p fills roster slot s. Constraints: every slot filled once, a player at most once,
eligibility from the position tokens, salary cap, max per team, at least two games (team sports), MLB's
five-hitters-per-team rule, captain formats, and optional structure:
  stacks     [k1, k2, ...]  k players sharing the sport's stack key (rules.STACK_KEYS), each on a distinct group
  no_opp     MLB: no hitter against a rostered pitcher; NHL: no skater against a rostered goalie; TEN/MMA: never both sides of a match
  bringback  team sports with a stack: at least one player from the stacked group's opponent

build(slate, sims, opts) makes `n` lineups: candidate solves with a cycling upside weight, ranked by the lineup's
simulated p90 (or p98 / mean), selected under an exposure cap and a minimum number of unique players between lineups.
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field

import numpy as np
import pulp

from .rules import RULES, STACK_KEYS, TEAM_SPORTS


@dataclass
class Opts:
    n: int = 20
    stacks: list = field(default_factory=list)   # e.g. [5, 3] for MLB, [3] for NHL lines, [2] for NBA games
    bringback: bool = False
    no_opp: bool = True
    max_exp: float = 0.5
    min_uniq: int = 3
    rank: str = "p90"            # p90 | p98 | mean | p50
    upside: tuple = (0.0, 0.35, 0.7, 1.0)   # cycling objective weights on (p85 - mean)
    rand: float = 0.0            # objective noise (fraction of mean) for exploration
    candidates: int = 6          # candidate solves per selected lineup
    min_salary: int = 0
    max_team: int | None = None
    locks: set = field(default_factory=set)
    excludes: set = field(default_factory=set)
    time_limit: int = 20
    seed: int | None = None


def stack_group(sport: str, p: dict) -> str | None:
    key = STACK_KEYS.get(sport)
    if key == "team_hitters":
        return None if "P" in p["pos"] else p["team"]
    if key == "line":
        if "G" in p["pos"] or not p["line"] or p["line"] in ("—", "-", "0"):
            return None
        return f"{p['team']}|{p['line']}"
    if key == "game":
        return p["game"] or None
    if key == "team_pass":
        return None if ("QB" in p["pos"] or "DST" in p["pos"]) else p["team"]
    if key == "team":
        return p["team"] or None
    return None


def hall_constraints(ps, slots):
    """Exact slot-feasibility for one-variable-per-player models: for every subset S of slots, the number of
    rostered players eligible for at least one slot in S must be >= |S| (Hall's theorem). Returns
    [(player index list, required count)], deduplicated by player set."""
    tok = [frozenset(j for j, (_, t) in enumerate(slots) if p["pos"] & t) for p in ps]
    best = {}
    nslot = len(slots)
    for mask in range(1, 1 << nslot):
        S = {j for j in range(nslot) if mask >> j & 1}
        elig = tuple(k for k in range(len(ps)) if tok[k] & S)
        if len(S) > best.get(elig, 0):
            best[elig] = len(S)
    # a slot set no player can fill makes the slate infeasible; keep those too (they'll make the MIP infeasible)
    return [(list(elig), need) for elig, need in best.items() if need > 0]


def prune(ps, score, slots, keep=28, cheap=10):
    """Candidate pool: per slot token the top `keep` by objective and the top `cheap` by objective per $,
    plus everybody within 5% of the best value. Keeps the MIP small on 1,000-row MLB slates."""
    ok = set()
    for _, tok in slots:
        ks = [k for k, p in enumerate(ps) if p["pos"] & tok and p["salary"] > 0]
        ok.update(sorted(ks, key=lambda k: -score[k])[:keep])
        ok.update(sorted(ks, key=lambda k: -score[k] / ps[k]["salary"])[:cheap])
    return sorted(ok)


def solve_one(sport, ps, score, opts: Opts, prior, blocked, pool=None):
    rules = RULES[sport]
    slots = rules["slots"]
    nslot = len(slots)
    ks = pool if pool is not None else [k for k in range(len(ps)) if any(ps[k]["pos"] & t for _, t in slots)]
    ks = [k for k in ks if k not in blocked or k in opts.locks]
    for k in opts.locks:
        if k not in ks:
            ks.append(k)
    sub = [ps[k] for k in ks]
    prob = pulp.LpProblem("lineup", pulp.LpMaximize)
    x = {k: pulp.LpVariable(f"x_{k}", cat="Binary") for k in ks}
    use = x
    prob += pulp.lpSum(score[k] * x[k] for k in ks)
    prob += pulp.lpSum(x.values()) == nslot
    if not all(s[1] == slots[0][1] for s in slots):
        for elig, need in hall_constraints(sub, slots):
            if len(elig) < len(ks):
                prob += pulp.lpSum(x[ks[i]] for i in elig) >= need
    prob += pulp.lpSum(ps[k]["salary"] * x[k] for k in ks) <= 50000
    if opts.min_salary:
        prob += pulp.lpSum(ps[k]["salary"] * x[k] for k in ks) >= opts.min_salary
    bybase = {}
    for k in ks:
        bybase.setdefault(ps[k]["base"], []).append(k)
    for kk in bybase.values():
        if len(kk) > 1:
            prob += pulp.lpSum(x[k] for k in kk) <= 1
    teams = {ps[k]["team"] for k in ks if ps[k]["team"]}
    max_team = opts.max_team or rules["max_team"]
    if sport in TEAM_SPORTS:
        for t in teams:
            prob += pulp.lpSum(x[k] for k in ks if ps[k]["team"] == t) <= max_team
        games = {ps[k]["game"] for k in ks if ps[k]["game"]}
        if rules.get("min_games", 1) >= 2 and len(games) >= 2:
            for g in games:
                prob += pulp.lpSum(x[k] for k in ks if ps[k]["game"] == g) <= nslot - 1
        if rules.get("max_hitters_team"):
            for t in teams:
                prob += pulp.lpSum(x[k] for k in ks if ps[k]["team"] == t and "P" not in ps[k]["pos"]) <= rules["max_hitters_team"]
    elif sport == "F1":
        for t in teams:
            prob += pulp.lpSum(x[k] for k in ks if ps[k]["team"] == t) <= max_team
    if opts.no_opp:
        if sport == "MLB":
            for kp in [k for k in ks if "P" in ps[k]["pos"]]:
                hs = [k for k in ks if ps[k]["team"] == ps[kp]["opp"] and "P" not in ps[k]["pos"]]
                if hs:
                    prob += pulp.lpSum(x[k] for k in hs) <= 5 * (1 - x[kp])
        elif sport == "NHL":
            for kg in [k for k in ks if "G" in ps[k]["pos"]]:
                sk = [k for k in ks if ps[k]["team"] == ps[kg]["opp"] and "G" not in ps[k]["pos"]]
                if sk:
                    prob += pulp.lpSum(x[k] for k in sk) <= 8 * (1 - x[kg])
        elif sport in ("TEN", "MMA"):
            byname = {ps[k]["name"]: k for k in ks}
            for k in ks:
                o = byname.get(ps[k]["opp"])
                if o is not None and o > k:
                    prob += x[k] + x[o] <= 1
    groups = {}
    for k in ks:
        g = stack_group(sport, ps[k])
        if g:
            groups.setdefault(g, []).append(k)
    if opts.stacks and groups:
        ys = {}
        for j, kreq in enumerate(opts.stacks):
            for g, kk in groups.items():
                if len(kk) < kreq:
                    continue
                y = pulp.LpVariable(f"y_{j}_{len(ys)}", cat="Binary")
                ys[(j, g)] = y
                prob += pulp.lpSum(x[k] for k in kk) >= kreq * y
            prob += pulp.lpSum(y for (jj, g), y in ys.items() if jj == j) == 1
        for g in groups:
            prob += pulp.lpSum(y for (jj, gg), y in ys.items() if gg == g) <= 1
        if STACK_KEYS.get(sport) == "team_pass":
            for (j, g), y in ys.items():
                qbs = [k for k in ks if ps[k]["team"] == g and "QB" in ps[k]["pos"]]
                if qbs:
                    prob += pulp.lpSum(x[k] for k in qbs) >= y
        if opts.bringback and sport in TEAM_SPORTS and STACK_KEYS.get(sport) != "game":
            for (j, g), y in ys.items():
                if j != 0:
                    continue
                t = g.split("|")[0]
                opp = next((ps[k]["opp"] for k in ks if ps[k]["team"] == t and ps[k]["opp"]), None)
                if opp:
                    prob += pulp.lpSum(x[k] for k in ks if ps[k]["team"] == opp and "P" not in ps[k]["pos"] and "G" not in ps[k]["pos"]) >= y
    for L in prior:
        prob += pulp.lpSum(x[k] for k in L if k in x) <= nslot - opts.min_uniq
    for k in opts.locks:
        if k in x:
            prob += x[k] == 1
    for k in opts.excludes:
        if k in x and k not in opts.locks:
            prob += x[k] == 0
    prob.solve(pulp.PULP_CBC_CMD(msg=False, timeLimit=opts.time_limit, gapRel=0.003))
    if pulp.LpStatus[prob.status] != "Optimal":
        return None
    return sorted(k for k in ks if x[k].value() is not None and x[k].value() > 0.5)


def lineup_draws(L, draws):
    return draws[L].sum(axis=0)


def assign_slots(sport, ps):
    """Players -> roster slots (backtracking over the hardest slots first; Hall made a perfect assignment exist).
    Returns [(slot name, player)] or None when no assignment exists."""
    slots = RULES[sport]["slots"]
    elig = [[j for j, (_, tok) in enumerate(slots) if p["pos"] & tok] for p in ps]
    order = sorted(range(len(slots)), key=lambda j: sum(1 for e in elig if j in e))
    out, used = [None] * len(slots), set()

    def go(i):
        if i == len(order):
            return True
        j = order[i]
        for k, p in enumerate(ps):
            if k in used or j not in elig[k]:
                continue
            used.add(k); out[j] = p
            if go(i + 1):
                return True
            used.discard(k); out[j] = None
        return False
    return [(slots[j][0], out[j]) for j in range(len(slots))] if go(0) else None


def _stats(L, ps, draws, w=0.0, fill=False):
    tot = lineup_draws(L, draws)
    q = np.percentile(tot, [50, 90, 98])
    return {"idx": L, "mean": float(tot.mean()), "p50": float(q[0]), "p90": float(q[1]), "p98": float(q[2]),
            "salary": int(sum(ps[k]["salary"] for k in L)), "w": w, "fill": fill}


def candidate_pool(slate, ps, opts: Opts):
    sport = slate["sport"]
    mean = np.array([p["proj"] for p in ps])
    p85 = np.array([max(p["q"][85], p["proj"]) for p in ps])
    pool = prune(ps, mean + 0.5 * (p85 - mean), RULES[sport]["slots"])
    if opts.stacks:          # every stack group keeps its best members so a 5-stack is always possible
        sc = mean + 0.5 * (p85 - mean)
        groups = {}
        for k, p in enumerate(ps):
            g = stack_group(sport, p)
            if g:
                groups.setdefault(g, []).append(k)
        for g, ks in groups.items():
            pool = sorted(set(pool) | set(sorted(ks, key=lambda k: -sc[k])[:max(opts.stacks) + 1]))
    return pool, mean, p85


def candidates(slate: dict, ps: list[dict], draws: np.ndarray, opts: Opts, want: int | None = None, log=None):
    """Distinct candidate lineups from repeated solves (cycling upside weight, min_uniq apart), with sim stats."""
    sport = slate["sport"]
    rng = random.Random(opts.seed)
    pool, mean, p85 = candidate_pool(slate, ps, opts)
    cands, seen, prior = [], set(), []
    want = want or max(opts.n * opts.candidates, opts.n + 4)
    tries = 0
    while len(cands) < want and tries < want * 2:
        w = opts.upside[tries % len(opts.upside)]
        score = mean + w * (p85 - mean)
        if opts.rand > 0:
            score = score * (1 + opts.rand * np.array([rng.gauss(0, 1) for _ in ps]))
        tries += 1
        L = solve_one(sport, ps, score, opts, prior[-200:], set(), pool)
        if L is None:
            break
        prior.append(L)
        key = tuple(L)
        if key in seen:
            continue
        seen.add(key)
        cands.append(_stats(L, ps, draws, w))
    if log:
        log(f"{sport} {slate['date']}: {len(cands)} candidates from {tries} solves (pool {len(pool)})")
    return cands


def select(slate: dict, ps: list[dict], draws: np.ndarray, cands: list[dict], opts: Opts, fill: bool = True):
    """Top-n by the ranking stat under the exposure cap and min_uniq; a fill pass solves what the pool lacks."""
    sport = slate["sport"]
    slots = len(RULES[sport]["slots"])
    keyf = {"p90": lambda c: c["p90"], "p98": lambda c: c["p98"], "mean": lambda c: c["mean"], "p50": lambda c: c["p50"]}[opts.rank]
    cands = sorted(cands, key=keyf, reverse=True)
    cap = max(1, int(round(opts.max_exp * opts.n)))
    chosen, count = [], {}
    for c in cands:
        if len(chosen) >= opts.n:
            break
        if any(count.get(k, 0) >= cap for k in c["idx"]):
            continue
        if any(len(set(c["idx"]) & set(o["idx"])) > slots - opts.min_uniq for o in chosen):
            continue
        chosen.append(c)
        for k in c["idx"]:
            count[k] = count.get(k, 0) + 1
    fills = 0
    if fill:
        pool, mean, p85 = candidate_pool(slate, ps, opts)
        while len(chosen) < opts.n and fills < opts.n * 3:
            fills += 1
            blocked = {k for k, v in count.items() if v >= cap}
            w = opts.upside[fills % len(opts.upside)]
            L = solve_one(sport, ps, mean + w * (p85 - mean), opts, [c["idx"] for c in chosen], blocked, pool)
            if L is None:
                break
            chosen.append(_stats(L, ps, draws, w, True))
            for k in L:
                count[k] = count.get(k, 0) + 1
    return chosen


def build(slate: dict, ps: list[dict], draws: np.ndarray, opts: Opts, log=None):
    """ps = simulated players (indexes into `draws` rows). Returns `opts.n` lineups: {idx (into ps), stats...}."""
    cands = candidates(slate, ps, draws, opts, log=log)
    chosen = select(slate, ps, draws, cands, opts)
    if log:
        log(f"{slate['sport']} {slate['date']}: {len(chosen)} lineups ({sum(1 for c in chosen if c.get('fill'))} from the fill pass)")
    return chosen
