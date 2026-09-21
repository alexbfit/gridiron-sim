"""
Contest simulation for GPP lineup selection (what SaberSim-style builders do).

Instead of ranking candidate lineups by their own p90, rank them by how they finish against a
simulated FIELD of opponents in each stored sim. The field is sampled from projected ownership
(chalk appears often, gets duplicated, and dies together when it busts); the candidate's rank in
each sim maps to a prize through a top-heavy payout curve. The result naturally favours:
  * correlated ceilings (stacks) over raw projection
  * low-ownership players with real ceilings (leverage) over popular ones
  * lineups that are unique when they hit

Usage from build_lineups.py:  --objective ev [--entries 200000 --field 4000 --payout milly --fee 20]

Payout tables are ranks -> prize (inclusive upper rank). They are scaled to --entries, so the
same table works for any contest size: a rank r in an N-entry contest is treated like rank
r * (table_entries / N) in the table's contest.
"""
from __future__ import annotations

import numpy as np

# DK "Fantasy Football Millionaire" style curve (~200k entries, $20, 1st = $1M). Approximate.
PAYOUTS = {
    "milly": {"entries": 206000, "table": [
        (1, 1_000_000), (2, 100_000), (3, 40_000), (4, 25_000), (5, 20_000), (7, 12_500), (10, 7_500),
        (15, 5_000), (20, 3_000), (30, 2_000), (50, 1_000), (100, 500), (200, 300), (500, 200),
        (1000, 100), (2500, 60), (5000, 50), (10000, 40), (25000, 30), (48000, 25)]},
    # flatter large GPP (~20% cash, 1st ~ 5% of pool)
    "gpp": {"entries": 100000, "table": [
        (1, 50_000), (2, 20_000), (3, 10_000), (5, 5_000), (10, 2_500), (25, 1_000), (50, 500), (100, 250),
        (250, 100), (1000, 50), (5000, 25), (20000, 10)]},
    # single-entry / small field, ~25% cash
    "se": {"entries": 1000, "table": [
        (1, 1000), (2, 500), (3, 300), (5, 200), (10, 100), (25, 60), (50, 40), (100, 30), (250, 20)]},
}

SLOT_NEEDS = {"QB": 1, "RB": 2, "WR": 3, "TE": 1, "DST": 1}   # + 1 FLEX from RB/WR/TE
DUP_K = 0.2                                                    # duplication scale (see evaluate)


def payout_fn(kind: str, entries: int):
    """Returns f(rank_array) -> prize_array for a contest of `entries` entries."""
    spec = PAYOUTS[kind]
    scale = spec["entries"] / max(entries, 1)
    uppers = np.array([u for u, _ in spec["table"]], dtype=np.float64)
    prizes = np.array([p for _, p in spec["table"]], dtype=np.float64)
    def f(rank):
        r = np.asarray(rank, dtype=np.float64) * scale
        idx = np.searchsorted(uppers, np.maximum(r, 1.0), side="left")
        out = np.where(idx < len(prizes), prizes[np.minimum(idx, len(prizes) - 1)], 0.0)
        return out
    return f


def sample_field(pool, own, salary_cap, n_field, rng, stack_prob=0.55, min_salary_frac=0.94, max_team=8):
    """Sample n_field opponent lineups from projected ownership.

    own: {site_player_id: ownership %}. Players are drawn per position with probability
    proportional to ownership (a 60%-owned RB shows up in ~60% of field lineups). About half
    the field stacks the QB with a same-team WR/TE, like real GPP fields. Lineups that bust the
    cap, leave too much salary, or violate roster rules are rejected and redrawn.
    Returns a list of id-tuples.
    """
    by_pos = {pos: [p for p in pool if p["position"] == pos] for pos in SLOT_NEEDS}
    weights = {pos: np.array([max(own.get(p["site_player_id"], 0.0), 0.05) for p in ps]) for pos, ps in by_pos.items()}
    for pos in weights:
        weights[pos] = weights[pos] / weights[pos].sum()
    flex_pool = by_pos["RB"] + by_pos["WR"] + by_pos["TE"]
    flex_w = np.concatenate([weights["RB"] * 2.4, weights["WR"] * 3.4, weights["TE"] * 1.2]); flex_w /= flex_w.sum()
    sal = {p["site_player_id"]: p["salary"] for p in pool}
    team = {p["site_player_id"]: p["team"] for p in pool}
    out, tries = [], 0
    while len(out) < n_field and tries < n_field * 60:
        tries += 1
        ids = []
        qb = by_pos["QB"][rng.choice(len(by_pos["QB"]), p=weights["QB"])]
        ids.append(qb["site_player_id"])
        for pos in ("RB", "WR", "TE", "DST"):
            k = SLOT_NEEDS[pos]
            picks = rng.choice(len(by_pos[pos]), size=k, replace=False, p=weights[pos])
            ids += [by_pos[pos][i]["site_player_id"] for i in picks]
        if rng.random() < stack_prob:      # swap one WR/TE for a same-team receiver
            mates = [p for p in by_pos["WR"] + by_pos["TE"] if p["team"] == qb["team"] and p["site_player_id"] not in ids]
            if mates:
                mw = np.array([max(own.get(p["site_player_id"], 0.0), 0.05) for p in mates]); mw /= mw.sum()
                mate = mates[rng.choice(len(mates), p=mw)]
                # replace the lowest-owned WR (or TE if mate is TE)
                repl_pos = mate["position"]
                cands = [i for i in ids if i in sal and any(p["site_player_id"] == i and p["position"] == repl_pos for p in by_pos[repl_pos])]
                if cands:
                    ids.remove(min(cands, key=lambda i: own.get(i, 0.0)))
                    ids.append(mate["site_player_id"])
        fl = flex_pool[rng.choice(len(flex_pool), p=flex_w)]["site_player_id"]
        if fl in ids:
            continue
        ids.append(fl)
        s = sum(sal[i] for i in ids)
        if s > salary_cap or s < min_salary_frac * salary_cap:
            continue
        teams = [team[i] for i in ids]
        if max(teams.count(t) for t in set(teams)) > max_team:
            continue
        out.append(tuple(ids))
    return out


def lineup_totals(id_lists, matrix, proj, n_sims):
    """(len(id_lists), n_sims) array of simulated lineup totals."""
    tot = np.zeros((len(id_lists), n_sims), dtype=np.float32)
    for k, ids in enumerate(id_lists):
        for i in ids:
            a = matrix.get(i)
            tot[k] += a[:n_sims] if a is not None else np.float32(proj(i))
    return tot


def evaluate(cand_ids, field_ids, matrix, proj, entries, fee, payout="milly", dup_penalty=True, own=None):
    """Score candidate lineups against the field in every sim.

    Returns per-candidate dict: ev (expected profit per entry), p_cash, p_top1 (top 1% of field),
    p_top01 (top 0.1%), mean field-percentile, est. duplicates.
    """
    n_sims = len(next(iter(matrix.values())))
    F = lineup_totals(field_ids, matrix, proj, n_sims)          # (nf, S)
    C = lineup_totals(cand_ids, matrix, proj, n_sims)           # (nc, S)
    nf = F.shape[0]
    Fs = np.sort(F, axis=0)                                     # ascending per sim
    pay = payout_fn(payout, entries)
    # number of field lineups strictly below each candidate in each sim: (nc, S)
    below = np.empty(C.shape, dtype=np.int32)
    for s in range(n_sims):
        below[:, s] = np.searchsorted(Fs[:, s], C[:, s], side="left")
    res = []
    for k in range(C.shape[0]):
        beaten_frac = below[k] / nf
        # rank in the real contest (1 = first). Within the top bucket we can't resolve finer
        # than entries/nf, so assume uniform inside it (expected prize over that bucket).
        rank = np.maximum(1.0, (1.0 - beaten_frac) * entries)
        top_bucket = beaten_frac >= 1.0 - 0.5 / nf                  # beat every sampled opponent
        prize = pay(rank)
        if top_bucket.any():
            bucket = entries / nf
            ranks = np.arange(1, int(np.ceil(bucket)) + 1)
            prize[top_bucket] = pay(ranks).mean()
        dups = 1.0
        if dup_penalty and own is not None:
            # expected extra copies of this exact lineup in the field. Independence overstates it
            # (ownership is correlated), so scale by DUP_K: nine 55%-owned players in 200k entries
            # -> ~180 copies, nine 20% players -> ~0. Only bites on all-chalk builds; splits the prize.
            p = np.prod([max(own.get(i, 0.0), 0.1) / 100.0 for i in cand_ids[k]])
            dups = 1.0 + entries * p * DUP_K
        ev = float((prize / dups).mean() - fee)
        res.append({"ev": round(ev, 2), "p_cash": round(float((prize > 0).mean()), 3),
                    "p_top1": round(float((beaten_frac >= 0.99).mean()), 4),
                    "p_top01": round(float((beaten_frac >= 0.999).mean()), 4),
                    "field_pct": round(float(beaten_frac.mean()), 3), "dups": round(dups, 1)})
    return res
