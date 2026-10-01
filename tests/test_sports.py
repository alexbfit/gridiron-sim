"""Multi-sport engine (jobs/sports): rules, the Hall slot constraints, the correlated sim, the optimizer and the
field grader - all on small synthetic slates so no archive is needed."""
import random
import sys

import numpy as np
import pytest

from conftest import JOBS

sys.path.insert(0, str(JOBS))
from sports import RULES, Opts, build, corr_matrix, sim_matrix          # noqa: E402
from sports.archive import Field                                         # noqa: E402
from sports.optimize import hall_constraints, solve_one                  # noqa: E402

POS = {
    "NBA": ["PG/G/UTIL", "SG/G/UTIL", "SF/F/UTIL", "PF/F/UTIL", "C/UTIL", "PG/SG/G/UTIL", "SF/PF/F/UTIL", "PF/C/F/UTIL"],
    "NHL": ["C/UTIL", "W/UTIL", "D/UTIL", "G"],
    "MLB": ["P", "C", "1B", "2B", "3B", "SS", "OF", "C/1B", "2B/SS", "OF"],
    "TEN": ["X"],
}


def fake_slate(sport, teams=6, per_team=10, seed=1):
    rng = random.Random(seed)
    ps = []
    pos_list = POS[sport]
    for t in range(teams):
        team, opp = f"T{t}", f"T{t ^ 1}"
        for k in range(per_team):
            proj = rng.uniform(5, 40)
            pos = pos_list[k % len(pos_list)]
            if sport == "MLB" and k == 0:
                pos = "P"
            p = {"i": len(ps), "uuid": f"u{len(ps)}", "base": f"u{len(ps)}", "cpt": False, "name": f"P{len(ps)}",
                 "salary": int(2000 + proj * 220 + rng.uniform(-500, 500)) // 100 * 100,
                 "pos": set(pos.split("/")), "pos_str": pos, "team": team, "opp": opp, "home": t % 2 == 0,
                 "game": "@".join(sorted([team, opp])), "proj": proj,
                 "q": {25: proj * .7, 50: proj * .97, 75: proj * 1.25, 85: proj * 1.4, 95: proj * 1.65, 99: proj * 2.0},
                 "actual": proj * rng.uniform(0.3, 1.8), "own": {}, "ss_own": None, "status": "",
                 "line": str(k % 4 + 1), "order": str(k % 9 + 1), "tee": ""}
            if sport == "TEN":
                p["team"], p["opp"], p["game"] = "", f"P{len(ps) ^ 1}", ""
            ps.append(p)
    return {"sport": sport, "date": "test", "players": ps, "meta": {}, "root": ""}


def valid(sport, ps, L):
    slots = RULES[sport]["slots"]
    assert len(L) == len(slots)
    assert sum(ps[k]["salary"] for k in L) <= 50000
    # a perfect slot assignment exists
    from scipy.optimize import linear_sum_assignment
    cost = np.full((len(L), len(slots)), 1e6)
    for i, k in enumerate(L):
        for j, (_, tok) in enumerate(slots):
            if ps[k]["pos"] & tok:
                cost[i, j] = 0
    r, c = linear_sum_assignment(cost)
    assert cost[r, c].sum() == 0, "no slot assignment"


@pytest.mark.parametrize("sport", ["NBA", "NHL", "MLB", "TEN"])
def test_build_valid_lineups(sport):
    s = fake_slate(sport)
    ps, D = sim_matrix(s, 400, seed=3)
    assert D.shape == (len(ps), 400)
    # the draws keep SaberSim's mean
    assert np.allclose(D.mean(axis=1), [p["proj"] for p in ps], rtol=0.02, atol=0.3)
    opts = Opts(n=4, candidates=2, min_uniq=2, max_exp=0.75, seed=1,
                stacks=[5, 3] if sport == "MLB" else ([3] if sport == "NHL" else []))
    L = build(s, ps, D, opts)
    assert len(L) == 4
    for c in L:
        valid(sport, ps, c["idx"])
        if sport == "MLB":
            hitters = [ps[k] for k in c["idx"] if "P" not in ps[k]["pos"]]
            by = {}
            for p in hitters:
                by[p["team"]] = by.get(p["team"], 0) + 1
            assert max(by.values()) == 5 and sorted(by.values())[-2] >= 3      # 5-3 stack, 5-hitter cap
            for k in c["idx"]:
                if "P" in ps[k]["pos"]:
                    assert not any(h["team"] == ps[k]["opp"] for h in hitters)   # no hitter vs own pitcher
        if sport == "NHL":
            lines = {}
            for k in c["idx"]:
                if "G" not in ps[k]["pos"]:
                    key = ps[k]["team"] + "|" + ps[k]["line"]
                    lines[key] = lines.get(key, 0) + 1
            assert max(lines.values()) >= 3
        if sport == "TEN":
            names = {ps[k]["name"] for k in c["idx"]}
            assert not any(ps[k]["opp"] in names for k in c["idx"])            # never both sides of a match
    # min_uniq between lineups
    for i in range(len(L)):
        for j in range(i + 1, len(L)):
            assert len(set(L[i]["idx"]) & set(L[j]["idx"])) <= len(RULES[sport]["slots"]) - 2


def test_hall_catches_infeasible_slots():
    s = fake_slate("NBA", teams=2, per_team=8)
    ps = s["players"]
    for p in ps:
        p["pos"] = {"PG", "G", "UTIL"}        # nobody can play C
    assert any(need >= 1 and len(elig) == 0 for elig, need in hall_constraints(ps, RULES["NBA"]["slots"]))
    score = np.array([p["proj"] for p in ps])
    assert solve_one("NBA", ps, score, Opts(), [], set()) is None


def test_corr_matrix_rules():
    s = fake_slate("NHL", teams=2, per_team=8)
    ps = [p for p in s["players"] if p["proj"] > 0]
    R = corr_matrix("NHL", ps)
    assert np.allclose(R, R.T) and np.all(np.diag(R) == 1)
    f = [k for k, p in enumerate(ps) if "W" in p["pos"] or "C" in p["pos"]]
    same_line = [(a, b) for a in f for b in f if a < b and ps[a]["team"] == ps[b]["team"] and ps[a]["line"] == ps[b]["line"]]
    assert same_line and all(R[a, b] == 0.25 for a, b in same_line)
    g = [k for k, p in enumerate(ps) if "G" in p["pos"]]
    opp_f = [(a, b) for a in g for b in f if ps[a]["opp"] == ps[b]["team"]]
    assert opp_f and all(R[a, b] == -0.20 for a, b in opp_f)
    s = fake_slate("TEN", teams=1, per_team=6)
    R = corr_matrix("TEN", s["players"])
    assert R[0, 1] == -0.85 and R[0, 2] == 0


def test_field_place_and_ties(tmp_path):
    pts = np.array([100.0, 90.0, 90.0, 80.0, 70.0, 60.0], dtype=np.float32)
    np.savez(tmp_path / "field_X_2026-01-01_flagship.npz", pidx=np.zeros((6, 2), np.int16), points=pts,
             payout=np.array([50, 20, 20, 10, 0, 0], np.float32), rank=np.array([1, 2, 2, 4, 5, 6]),
             own=np.zeros(6), stack=np.zeros(6), stack_codes=np.array([]), stack2=np.array([""] * 6),
             sim_roi=np.zeros(6), sim_dupes=np.zeros(6), user=np.zeros(6))
    (tmp_path / "X").mkdir(); (tmp_path / "X" / "s").mkdir()
    import shutil
    shutil.move(tmp_path / "field_X_2026-01-01_flagship.npz", tmp_path / "X" / "s" / "field_X_2026-01-01_flagship.npz")
    slate = {"sport": "X", "date": "2026-01-01", "root": str(tmp_path),
             "meta": {"contests": {"flagship": {"fee": 5, "dk": {"maximumEntriesPerUser": 20,
                      "payout_tiers": [[1, 1, 50], [2, 3, 20], [4, 4, 10]]}}}}}
    F = Field(slate, "flagship")
    assert F.place(101)[0] == 1 and F.place(101)[1] == 50
    assert F.place(95)[0] == 2 and F.place(95)[1] == 20
    rank, prize, pct = F.place(90.0)           # ties two real entries at 90: three-way split of ranks 2-4
    assert rank == 2 and abs(prize - (20 + 20 + 10) / 3) < 1e-6
    assert F.place(0)[1] == 0 and F.place(0)[2] == 0
