"""Sunday smoke tests: run the exact commands the scheduled tasks run, against the frozen week-3 slate.

A failure here means a Sunday task would have crashed or produced invalid lineups. Every test is offline
(tests/fakedb.py); the whole file runs in a couple of minutes on a GitHub runner.
"""
import csv
import json
from collections import Counter

import pytest

from conftest import run_main

SITE_CAP = 50000
SLOTS = ["QB", "RB", "RB", "WR", "WR", "WR", "TE", "FLEX", "DST"]
BASE_GPP = "--contest gpp --n 50 --candidates 8 --stack 2 --bringback --max-exp 0.35 --min-uniq 4 --fade 0 --seed 7".split()


def board(db):
    return {r["site_player_id"]: r for r in db.rows("slate_board")}


def ext_flags(db, monkeypatch, tmp_path, setup="props"):
    import ext_projections
    flags, info = ext_projections.choose(ext_projections.Rest(), "DK-2026-03-main", setup, str(tmp_path))
    assert info["setup"] == setup, info
    return flags.split(), info


def check_lineup(ids, b, *, stack=2, bringback=True, max_te=None):
    ps = [b[i] for i in ids]
    pos = Counter(p["position"] for p in ps)
    assert len(ids) == 9 and len(set(ids)) == 9
    assert sum(float(p["salary"]) for p in ps) <= SITE_CAP
    assert pos["QB"] == 1 and pos["DST"] == 1
    assert pos["RB"] >= 2 and pos["WR"] >= 3 and pos["TE"] >= 1 and pos["RB"] + pos["WR"] + pos["TE"] == 7
    if max_te is not None:
        assert pos["TE"] <= max_te
    qb = next(p for p in ps if p["position"] == "QB")
    mates = [p for p in ps if p["team"] == qb["team"] and p["position"] in ("WR", "TE")]
    assert len(mates) >= stack, f"QB {qb['player_name']} stacked with {len(mates)}"
    if bringback:
        assert any(p["team"] == qb.get("opponent") and p["position"] in ("RB", "WR", "TE") for p in ps)


# ------------------------------------------------------------------ projection setup
def test_ext_projections_sabersim_file_wins(db, tmp_path):
    import ext_projections
    flags, info = ext_projections.choose(ext_projections.Rest(), "DK-2026-03-main", "auto", str(tmp_path))
    assert info["setup"] == "sabersim" and info["rbs_adjusted"] > 60
    assert "--ext-sim 1.0" in flags


def test_ext_projections_props_file(db, tmp_path):
    import ext_projections
    flags, info = ext_projections.choose(ext_projections.Rest(), "DK-2026-03-main", "props", str(tmp_path))
    assert info["setup"] == "props" and info["players"] >= 150 and info["rbs_adjusted"] >= 30
    assert "--ext-sim 0.55" in flags and "--max-te 1" in flags and "--min-uniq 3" in flags
    rows = list(csv.DictReader(open(tmp_path / "props_rbadj.csv")))
    assert set(rows[0]) == {"Name", "Team", "Pos", "Proj"}
    # the RB rule is bounded to +/-15%
    props = {r["site_player_id"]: float(r["mean"]) for r in db.rows("slate_projections") if r["method"] == "props"}
    b = board(db)
    by_name = {(b[i]["player_name"], b[i]["team"]): v for i, v in props.items() if i in b}
    for r in rows:
        base = by_name[(r["Name"], r["Team"])]
        ratio = float(r["Proj"]) / base if base else 1
        assert (0.849 <= ratio <= 1.151) if r["Pos"] == "RB" else abs(ratio - 1) < 1e-3


def test_ext_projections_falls_back_without_books(db, tmp_path):
    import ext_projections
    db.tables["slate_projections"] = [r for r in db.rows("slate_projections") if r["method"] != "props"]
    flags, info = ext_projections.choose(ext_projections.Rest(), "DK-2026-03-main", "props" if False else "auto", str(tmp_path))
    # week 3 still has the SaberSim file; force past it:
    flags, info = ext_projections.choose(ext_projections.Rest(), "DK-2026-03-main", "props", str(tmp_path))
    assert info["players"] == 0 and info["setup"] == "none" and flags == ""


# ------------------------------------------------------------------ 9:30 build
def test_build_gpp_props_only_is_valid(db, monkeypatch, tmp_path):
    """The exact week-4 Sunday command (props-only setup)."""
    ss, _ = ext_flags(db, monkeypatch, tmp_path)
    out, js = tmp_path / "gpp.csv", tmp_path / "gpp.json"
    rc = run_main("build_lineups", BASE_GPP + ss + ["--json", str(js), "--out", str(out), "--save", "--note", "smoke"], monkeypatch)
    assert rc == 0
    d = json.load(open(js))
    lineups = d["lineups"] if isinstance(d, dict) else d
    assert len(lineups) == 50
    b = board(db)
    for L in lineups:
        check_lineup(L["ids"], b, stack=2, max_te=1)
    # min-uniq 3 from $SS overrides the 4 on the command line: no two lineups share more than 6 players
    sets = [set(L["ids"]) for L in lineups]
    assert max(len(a & c) for i, a in enumerate(sets) for c in sets[i + 1:]) <= 6
    # max-exp .35 of 50 -> no player in more than 18
    use = Counter(i for L in lineups for i in L["ids"])
    assert max(use.values()) <= 18
    # DK upload file: header + 50 rows of 9 ids
    rows = list(csv.reader(open(out)))
    assert rows[0][:9] == SLOTS and len(rows) == 51 and all(len(r) >= 9 for r in rows[1:])
    # --save went through the RPC with 50 lineups
    saves = [p for n, p in db.rpc_calls if n == "save_lineups"]
    assert saves and saves[-1]["p_contest"] == "gpp" and len(saves[-1]["p_lineups"]) == 50


def test_build_gpp_sabersim_setup_runs(db, monkeypatch, tmp_path):
    ss, _ = ext_flags(db, monkeypatch, tmp_path, "sabersim")
    js = tmp_path / "g.json"
    rc = run_main("build_lineups", ["--contest", "gpp", "--n", "20", "--candidates", "4", "--stack", "2", "--bringback",
                                    "--max-exp", "0.35", "--min-uniq", "4", "--fade", "0", "--seed", "7"] + ss + ["--json", str(js)], monkeypatch)
    assert rc == 0
    d = json.load(open(js))
    assert len(d["lineups"] if isinstance(d, dict) else d) == 20


def test_build_gpp_with_overrides(db, monkeypatch, tmp_path):
    """Exclusions, projection sets and exposure stands — the review step's flags."""
    ss, _ = ext_flags(db, monkeypatch, tmp_path)
    b = board(db)
    top = sorted((r for r in b.values() if r["position"] == "WR" and r.get("mean")), key=lambda r: -float(r["mean"]))
    excl, setp, stand = top[0]["player_name"], top[1]["player_name"], top[5]["player_name"]
    js = tmp_path / "o.json"
    rc = run_main("build_lineups", BASE_GPP[:3] + ["20"] + BASE_GPP[4:] + ss + ["--exclude", excl, "--set", f"{setp}=4.0",
                                                                          "--exposure", f"{stand}=20", "--json", str(js)], monkeypatch)
    assert rc == 0
    d = json.load(open(js))
    lineups = d["lineups"] if isinstance(d, dict) else d
    ids = lambda name: {i for i, r in b.items() if r["player_name"] == name}
    assert not any(ids(excl) & set(L["ids"]) for L in lineups)
    assert sum(bool(ids(stand) & set(L["ids"])) for L in lineups) >= 4          # 20% of 20


def test_build_cash_benchmark(db, monkeypatch, tmp_path):
    ss, _ = ext_flags(db, monkeypatch, tmp_path)
    js = tmp_path / "c.json"
    rc = run_main("build_lineups", ["--contest", "cash", "--n", "1", "--seed", "7"] + ss + ["--json", str(js)], monkeypatch)
    assert rc == 0
    d = json.load(open(js))
    L = (d["lineups"] if isinstance(d, dict) else d)[0]
    check_lineup(L["ids"], board(db), stack=0, bringback=False, max_te=1)


# ------------------------------------------------------------------ late swaps
def recorded(db, contest="gpp"):
    return sorted((L for L in db.rows("slate_lineups") if L["contest"] == contest and L["source"] == "claude"), key=lambda L: L["idx"])


def test_quick_swap_removes_scratched_player(db, monkeypatch, tmp_path):
    ss, _ = ext_flags(db, monkeypatch, tmp_path)
    b = board(db)
    use = Counter(i for L in recorded(db) for i in L["player_ids"] if b[i]["position"] in ("WR", "RB"))
    pid, n = use.most_common(1)[0]
    name = b[pid]["player_name"]
    js, out = tmp_path / "s.json", tmp_path / "s.csv"
    rc = run_main("late_swap", ["--contest", "all", "--stack", "2"] + ss + ["--exclude", name, "--now", "2026-09-27T11:50",
                                                                           "--out", str(out), "--json", str(js), "--save"], monkeypatch)
    assert rc == 0
    rep = json.load(open(js))
    assert not any(pid in L["ids"] for L in rep["lineups"])
    changed = [L for L in rep["lineups"] if set(L["ids"]) != set(L["original_ids"])]
    assert len(changed) == n                                        # exactly the lineups that had him
    bb = board(db)
    for L in rep["lineups"]:
        if L["contest"] == "gpp":
            check_lineup(L["ids"], bb, stack=1, bringback=False, max_te=None)
    assert any(name == "save_lineups" for name, _ in db.rpc_calls)


def test_quick_swap_scratched_qb_keeps_stack_when_backup_is_set(db, monkeypatch, tmp_path):
    ss, _ = ext_flags(db, monkeypatch, tmp_path)
    b = board(db)
    qbs = Counter(i for L in recorded(db) for i in L["player_ids"] if b[i]["position"] == "QB")
    qb = b[qbs.most_common(1)[0][0]]
    backups = [r for r in b.values() if r["position"] == "QB" and r["team"] == qb["team"] and r["site_player_id"] != qb["site_player_id"]]
    if not backups:
        pytest.skip("no backup QB on the slate for the most-used QB")
    backup = max(backups, key=lambda r: float(r["salary"]))
    js = tmp_path / "q.json"
    rc = run_main("late_swap", ["--contest", "gpp", "--stack", "2"] + ss + [
        "--exclude", qb["player_name"], "--set", f"{backup['player_name']}={0.8 * float(qb['mean']):.1f}",
        "--now", "2026-09-27T11:50", "--json", str(js)], monkeypatch)
    assert rc == 0
    rep = json.load(open(js))
    had = [L for L in rep["lineups"] if qb["site_player_id"] in L["original_ids"]]
    assert had and all(qb["site_player_id"] not in L["ids"] for L in had)
    kept = sum(backup["site_player_id"] in L["ids"] for L in had)
    assert kept >= len(had) // 2, f"backup QB used in {kept}/{len(had)} — stack-aware swap should keep most stacks"


def test_full_swap_leaves_started_games_alone(db, monkeypatch, tmp_path):
    ss, _ = ext_flags(db, monkeypatch, tmp_path)
    js = tmp_path / "f.json"
    rc = run_main("late_swap", ["--contest", "all", "--full", "--stack", "2"] + ss + ["--now", "2026-09-27T15:50", "--json", str(js)], monkeypatch)
    assert rc == 0
    rep = json.load(open(js))
    games = {g["game_id"]: g for g in db.rows("games")}
    b = board(db)
    early = {i for i, r in b.items() if (games.get(r.get("game_id")) or {}).get("gametime", "13:00") < "15:50"}
    for L in rep["lineups"]:
        assert {i for i in L["original_ids"] if i in early} <= set(L["ids"]), "a started player was swapped"
    assert not any(n == "save_lineups" for n, _ in db.rpc_calls)   # the 3:50 swap never saves


def test_entries_edit_file_keeps_entry_ids(db, monkeypatch, tmp_path):
    ss, _ = ext_flags(db, monkeypatch, tmp_path)
    ent = tmp_path / "DKEntries.csv"
    with open(ent, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["Entry ID", "Contest Name", "Contest ID", "Entry Fee"] + SLOTS + ["", "Instructions"])
        for k in range(20):
            w.writerow([f"50{k:08d}", "NFL $3 Play-Action [20 Entry Max]", "185000000", "$3"] + [""] * 9 + ["", ""])
    out = tmp_path / "edit.csv"
    rc = run_main("late_swap", ["--contest", "gpp", "--stack", "2"] + ss + ["--entries", str(ent), "--out", str(out),
                                                                           "--now", "2026-09-27T09:40"], monkeypatch)
    assert rc == 0
    rows = list(csv.reader(open(out)))
    assert [r[0] for r in rows[1:21]] == [f"50{k:08d}" for k in range(20)]
    assert all(all(c.strip() for c in r[4:13]) for r in rows[1:21]), "an entry was left without a player"


# ------------------------------------------------------------------ Monday: Flashback
def test_flashback_on_synthetic_standings(db, monkeypatch, tmp_path):
    """Monday's Flashback path: parse a DK standings export, match lineups, score in the sims."""
    import random
    b = board(db)
    rnd = random.Random(3)
    pts = {i: round(rnd.uniform(0, 25), 2) for i in b}
    lus = recorded(db)
    std = tmp_path / "DK-2026-03-main_playaction_standings.csv"
    with open(std, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["Rank", "EntryId", "EntryName", "TimeRemaining", "Points", "Lineup", "", "Player", "Roster Position", "%Drafted", "FPTS"])
        entries = []
        for k in range(400):
            L = lus[k % len(lus)]["player_ids"]
            ps = sorted((b[i] for i in L), key=lambda p: SLOTS.index(p["position"]) if p["position"] in SLOTS else 8)
            text = " ".join(f"{p['position'] if p['position'] != 'DST' else 'DST'} {p['player_name']}" for p in ps)
            entries.append((sum(pts[i] for i in L), f"user{k} (1/1)", text))
        entries.sort(key=lambda e: -e[0])
        players = sorted({i for L in lus for i in L["player_ids"]})
        for k, (p_, name, text) in enumerate(entries):
            pl = b[players[k]] if k < len(players) else None
            w.writerow([k + 1, 900000 + k, name, 0, f"{p_:.2f}", text, "",
                        pl["player_name"] if pl else "", pl["position"] if pl else "", "5.00%" if pl else "", pts[pl["site_player_id"]] if pl else ""])
    js = tmp_path / "fb.json"
    rc = run_main("flashback", ["--slate-key", "DK-2026-03-main", "--standings", str(std), "--contest", "gpp",
                                "--fee", "3", "--payout", "gpp", "--bench", "100", "--json", str(js)], monkeypatch)
    assert rc == 0
    d = json.load(open(js))
    assert d["summary"]["n"] == 50 and "consensus" in d["summary"] and "model" in d["summary"]


def test_full_swap_keeps_recorded_lineups_unless_gain_is_real(db, monkeypatch, tmp_path):
    """--keep-recorded (default): a recorded lineup sitting at the exposure cap / min-uniq is not rebuilt for a tiny gain
    (week 3: #24 and #42 were rebuilt for +1.1 / +0.4 p90 only because the cap forced it)."""
    ss, _ = ext_flags(db, monkeypatch, tmp_path)
    js = tmp_path / "k.json"
    rc = run_main("late_swap", ["--contest", "all", "--full", "--stack", "2", "--gain", "99"] + ss + ["--now", "2026-09-27T15:50", "--json", str(js)], monkeypatch)
    assert rc == 0
    rep = json.load(open(js))
    changed = [L for L in rep["lineups"] if set(L["ids"]) != set(L["original_ids"])]
    assert not changed, f"{len(changed)} lineups rebuilt with an impossible gain bar"
