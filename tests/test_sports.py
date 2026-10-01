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
from sports.optimize import assign_slots, hall_constraints, solve_one    # noqa: E402

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
    a = assign_slots(sport, [ps[k] for k in L])
    assert a is not None and all(p is not None for _, p in a), "no slot assignment"


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


# ------------------------------------------------------------------ NBA live pipeline (jobs/sports/props_nba.py, nba_daily.py, fetch_dk.py)
from pathlib import Path                                                 # noqa: E402

NBA_FIX = Path(__file__).parent / "fixtures" / "nba"
NBA_DK = NBA_FIX / "DKSalaries_NBA_2026-10-21.csv"
NBA_PROPS = NBA_FIX / "props_2026-10-21.json"


def test_props_nba_star_projects_about_fifty_with_dd():
    """28 pts / 8 reb / 8 ast -> 28 + 10 + 12 = 50 plus the double-double bonus x its probability (about half the nights)."""
    from sports.props_nba import simulate_player, line_to_mean, collect_outcomes
    s = simulate_player({"pts": 28, "reb": 8, "ast": 8}, 20000, np.random.default_rng(1))
    assert 45 <= s["proj"] <= 55, s
    assert 0.3 <= s["p_dd"] <= 0.7 and 0.03 <= s["p_td"] <= 0.25
    assert s["q"][25] < s["q"][50] < s["q"][75] < s["q"][85] < s["q"][95] < s["q"][99]
    assert abs(s["q"][50] - s["proj"]) < 3            # near-symmetric at this volume
    # a 26 / 12.5 / 9.5 big is a near-certain double-double and a frequent triple-double
    big = simulate_player({"pts": 26, "reb": 12.5, "ast": 9.5, "fg3": 1, "stl": 1.5, "blk": 0.9, "tov": 3.5}, 20000, np.random.default_rng(2))
    assert big["p_dd"] > 0.7 and 0.2 < big["p_td"] < 0.55 and 56 <= big["proj"] <= 68
    # line -> mean: a fair half-point line is the median (x the small skew); a juiced Over lifts it; counts use the price-implied Poisson mean
    assert abs(line_to_mean("pts", 24.5, 0.5) - 24.5 * 1.015) < 1e-6
    assert line_to_mean("pts", 24.5, 0.58) > line_to_mean("pts", 24.5, 0.5) > line_to_mean("pts", 24.5, 0.42)
    assert 1.3 < line_to_mean("stl", 1.5, 0.45) < 1.7
    # collect_outcomes de-vigs per book and medians the points across books
    out = collect_outcomes([("A. Star", "player_points", "Over", -110, 27.5, "dk"), ("A. Star", "player_points", "Under", -110, 27.5, "dk"),
                            ("A. Star", "player_points", "Over", -120, 28.5, "fd"), ("A. Star", "player_points", "Under", 100, 28.5, "fd"),
                            ("A. Star", "player_threes", "Over", -130, 2.5, "dk"), ("A. Star", "player_threes", "Under", 105, 2.5, "dk")])
    assert set(out) == {"a star"} and 27.5 < out["a star"]["pts"] < 30 and 2.5 < out["a star"]["fg3"] < 3.3


def test_props_nba_csv_format_and_matching(tmp_path):
    from sports.props_nba import run, HEADER
    import csv
    out = tmp_path / "proj.csv"
    info = run(str(NBA_DK), str(out), date="2026-10-21", props_json=str(NBA_PROPS), sims=3000, log=lambda *a: None)
    rows = list(csv.DictReader(open(out)))
    assert list(rows[0].keys()) == HEADER
    assert info["players"] == len(rows) == 70 and info["unmatched_props"] == 0     # 10 fixture bench players have no props -> excluded
    dk_names = {r["Name"] for r in csv.DictReader(open(NBA_DK, encoding="utf-8-sig"))}
    assert all(r["name"] in dk_names for r in rows)                                   # exact DK names, builder's join is exact
    assert all(r["team"] and r["opp"] and r["team"] != r["opp"] for r in rows)
    for r in rows:
        q = [float(r[k]) for k in ("p25", "p50", "p75", "p85", "p95", "p99")]
        assert q == sorted(q) and q[0] < float(r["proj"]) < q[-1] and 0 <= float(r["p_dd"]) <= 1
    star = next(r for r in rows if r["name"] == "Nikola Jokic")
    assert star["source"] == "props_partial" and 46 <= float(star["proj"]) <= 62 and float(star["p_dd"]) > 0.35
    assert abs(float(star["pts"]) - 28) < 1.5 and abs(float(star["reb"]) - 8) < 1.5 and abs(float(star["ast"]) - 8) < 1.5
    # the slate's best player projects most; nobody is absurd
    assert 20 < max(float(r["proj"]) for r in rows) < 80 and min(float(r["proj"]) for r in rows) > 3
    # fallback rows carry proj only (shapes.json fills the curve in the builder)
    fb = tmp_path / "fb.csv"
    fb.write_text("name,proj\nTrey Booker,12.5\n")
    info = run(str(NBA_DK), str(tmp_path / "proj2.csv"), date="2026-10-21", props_json=str(NBA_PROPS), sims=500, fallback_proj=str(fb), log=lambda *a: None)
    rows = list(csv.DictReader(open(tmp_path / "proj2.csv")))
    assert info["fallback"] == 1 and any(r["name"] == "Trey Booker" and r["source"] == "fallback" and r["p50"] == "" for r in rows)


def test_nba_daily_builds_twenty_valid_lineups_offline(tmp_path, monkeypatch):
    import csv
    from sports import nba_daily
    monkeypatch.delenv("SUPABASE_SERVICE_ROLE_KEY", raising=False)
    rc = nba_daily.main(["--dk", str(NBA_DK), "--props-json", str(NBA_PROPS), "--date", "2026-10-21", "--out-dir", str(tmp_path),
                         "--seed", "3", "--sims", "600", "--props-sims", "2000", "--candidates", "2"])
    assert rc == 0
    for f in ("DKSalaries_NBA_2026-10-21.csv", "projections.csv", "projections_info.json", "lineups.csv", "lineups.txt", "README.md"):
        assert (tmp_path / f).exists(), f
    dk = {r["ID"]: r for r in csv.DictReader(open(NBA_DK, encoding="utf-8-sig"))}
    lines = list(csv.reader(open(tmp_path / "lineups.csv")))
    assert lines[0] == ["PG", "SG", "SF", "PF", "C", "G", "F", "UTIL"]
    lineups = lines[1:]
    assert len(lineups) == 20
    count = {}
    for L in lineups:
        assert len(set(L)) == 8
        ps = [dk[i] for i in L]
        assert sum(int(p["Salary"]) for p in ps) <= 50000
        for slot, p in zip(lines[0], ps):
            assert slot in p["Roster Position"].split("/"), (slot, p["Name"], p["Roster Position"])
        assert len({p["Game Info"] for p in ps}) >= 2 and max(sum(1 for p in ps if p["TeamAbbrev"] == t) for t in {p["TeamAbbrev"] for p in ps}) <= 8
        assert max(sum(1 for p in ps if p["Game Info"] == g) for g in {p["Game Info"] for p in ps}) >= 3    # the 3-player game stack
        for i in L:
            count[i] = count.get(i, 0) + 1
    assert max(count.values()) <= 7                                                      # exposure cap .35 x 20
    for i in range(20):
        for j in range(i + 1, 20):
            assert len(set(lineups[i]) & set(lineups[j])) <= 5                            # min 3 unique
    assert "NBA 2026-10-21" in (tmp_path / "README.md").read_text()


def test_fetch_dk_nba_picks_the_plain_evening_classic_group():
    import datetime as dt
    from sports.fetch_dk import pick_main, classic_type_id
    E = lambda h, m=0, d=21: dt.datetime(2026, 10, d, h, m, tzinfo=dt.timezone(dt.timedelta(hours=-4)))
    groups = [
        {"id": 1, "contest_type": 70, "start": E(19), "games": 9, "tag": "Featured", "suffix": ""},
        {"id": 2, "contest_type": 70, "start": E(19), "games": 4, "tag": "", "suffix": "(Early Only)"},
        {"id": 3, "contest_type": 70, "start": E(22), "games": 3, "tag": "", "suffix": "(Late Night)"},
        {"id": 4, "contest_type": 70, "start": E(19), "games": 11, "tag": "", "suffix": "(Thu-Fri)"},
        {"id": 5, "contest_type": 81, "start": E(19), "games": 1, "tag": "Featured", "suffix": "(BOS @ NY)"},
        {"id": 6, "contest_type": 70, "start": E(19, d=22), "games": 12, "tag": "Featured", "suffix": ""},
    ]
    assert pick_main(groups, dt.date(2026, 10, 21), 70)["id"] == 1          # plain group beats the bigger suffixed one
    assert pick_main([g for g in groups if g["id"] != 1], dt.date(2026, 10, 21), 70)["id"] == 4   # no plain group: biggest evening classic
    assert pick_main(groups, dt.date(2026, 10, 23), 70) is None
    lobby = {"GameTypes": [{"GameTypeId": 1, "Name": "Classic", "SportId": 1}, {"GameTypeId": 70, "Name": "Classic", "SportId": 4}],
             "DraftGroups": [{"DraftGroupId": 9, "SportId": 4}]}
    assert classic_type_id("NBA", lobby) == 70
    assert classic_type_id("NBA", {"GameTypes": [{"GameTypeId": 81, "Name": "Showdown Captain Mode", "SportId": 4}], "DraftGroups": []}) == 70
