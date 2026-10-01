"""jobs/recap.py against the frozen week-3 slate: the ledger row carries the numbers the database holds, nothing typed in."""
import json

from conftest import run_main


def _run(db, monkeypatch, tmp_path, *extra):
    rc = run_main("recap", ["--out-dir", str(tmp_path), "--season", "2026", *extra], monkeypatch)
    assert rc == 0
    led = json.loads((tmp_path / "ledger.json").read_text())
    return led


def test_week3_row_from_lineup_rows(db, monkeypatch, tmp_path):
    """The fixture slate has no results_meta / flashback (make_fixture strips them), so the row is derived from the
    graded slate_lineups rows alone — the fallback a half-scored week takes."""
    led = _run(db, monkeypatch, tmp_path)
    assert led["season"] == 2026 and len(led["weeks"]) == 1
    w = led["weeks"][0]
    assert w["week"] == 3 and w["slate_key"] == "DK-2026-03-main" and w["recap"] == "recap_2026_wk03.md"
    lu = w["lineups"]
    gpp = [L for L in db.rows("slate_lineups") if L["source"] == "claude" and L["contest"] == "gpp"]
    assert lu["n"] == len(gpp) == 50
    assert abs(lu["avg_pts"] - sum(L["actual"] for L in gpp) / len(gpp)) < 0.01
    assert 120 < lu["avg_pts"] < 160 and lu["best_pts"] >= lu["avg_pts"]
    assert lu["best_rank"] == min(L["real_rank"] for L in gpp)
    assert 0.5 < lu["beat_pct"] < 0.8
    assert lu["entries"] is None and lu["field_median_pts"] is None and lu["avg_vs_field_median"] is None   # no standings meta in the fixture
    fb = w["flashback"]
    assert fb["present"] is False
    assert abs(fb["consensus_roi"] - sum(L["fb_roi_cons"] for L in gpp) / len(gpp)) < 1e-4      # portfolio ROI = mean of per-lineup ROI
    assert fb["field_mean_roi"] is None and w["dupes"] is None and w["real_money"] is None
    assert w["news"] is None                       # fixture notes carry no graded columns
    ww = w["what_won"]
    assert ww and ww["qb"] and ww["lineups_with_qb"] >= 1 and ww["top_lineups"] == 10 and ww["qb"] in ww["line"]
    t = led["totals"]
    assert t["weeks"] == 1 and t["lineups"] == 50 and t["best_finish"]["rank"] == lu["best_rank"] and t["real_money"] is None
    md = (tmp_path / "recap_2026_wk03.md").read_text()
    assert md.startswith("# Week 3 recap") and "## What won" in md and "## Honest read" in md and "Flashback: not run" in md
    assert f"{lu['avg_pts']:.1f} pts" in md


def test_week3_row_with_flashback_and_real_money(db, monkeypatch, tmp_path, capsys):
    """With a saved Flashback summary (the shape jobs/flashback.py writes) the row carries the consensus view, dupes
    and the owner's real-money reconstruction, and the season totals add them up."""
    s = db.rows("slates")[0]
    s["results_meta"] = {"n": 143, "r": 0.592, "mae": 5.29, "bias": -0.21, "scored_at": "2026-09-29T00:13:26+00:00",
                         "by_method": {"sim": {"n": 143, "r": 0.592, "mae": 5.29, "bias": -0.21}},
                         "lineups": {"claude": {"gpp": {"n": 50, "proj": 136.5, "actual": 138.5, "best": 184.9, "sim_pct": 0.631,
                                                         "contest_pct": 0.647, "best_contest_pct": 0.99}}}}
    s["flashback"] = {"claude/gpp": {"n": 50, "entries": 296894, "fee": 3.0, "payout": "gpp", "sims": 2000,
                                     "consensus": {"roi": 0.0975, "bench_mean": -0.2142, "bench_median": -0.4307, "beats_pct": 0.843, "cash": 0.2651, "top1": 0.0177},
                                     "model": {"roi": 0.0152, "beats_pct": 0.814, "top1": 0.0192},
                                     "real_roi": -0.367, "real_best_rank": 3115, "real_cash": 0.32, "real_points": 138.52, "field_median_points": 124.64,
                                     "me": {"entries": 20, "best_rank": 3114, "mean_points": 150.7, "roi": 0.6843},
                                     "dupes": {"lineups_with_copy": 4, "copies_total": 5, "max_copies": 2, "top20_with_copy": 2, "top20_copies": 2,
                                               "winnings_lost_pct": 0.03, "bench_with_copy": 0.11}}}
    led = _run(db, monkeypatch, tmp_path, "--week", "3", "--print")
    w = led["weeks"][0]; lu, fb = w["lineups"], w["flashback"]
    assert fb["present"] and fb["consensus_roi"] == 0.0975 and fb["field_mean_roi"] == -0.2142 and fb["beats_pct"] == 0.843
    assert lu["entries"] == 296894 and lu["best_rank"] == 3115 and abs(lu["avg_vs_field_median"] - (138.5 - 124.64)) < 1e-6
    assert lu["top1_lineups"] == 0                      # 3115 / 296894 is just outside the top 1%
    assert w["dupes"]["lineups_with_copy"] == 4 and w["dupes"]["winnings_lost_pct"] == 0.03
    rm = w["real_money"]
    assert rm["in"] == 60.0 and abs(rm["out"] - 60 * 1.6843) < 1e-6 and rm["best_rank"] == 3114
    assert w["accuracy"]["r"] == 0.592 and w["accuracy"]["by_method"]["sim"]["mae"] == 5.29
    t = led["totals"]
    assert t["weeks_above_field_median"] == 1 and t["weeks_consensus_above_field_median"] == 1
    assert t["real_money"]["in"] == 60.0 and abs(t["real_money"]["roi"] - 0.6843) < 1e-6
    assert t["consensus_roi_vs_field"] == round(0.0975 + 0.2142, 4)
    out = capsys.readouterr().out
    assert "+10%" in out and "−21%" in out and "$101.06" in out and "Duplicates: 4/50" in out


def test_recap_handles_no_scored_slates(db, monkeypatch, tmp_path):
    db.tables["slate_lineups"] = []
    db.rows("slates")[0].pop("results_meta", None)
    rc = run_main("recap", ["--out-dir", str(tmp_path)], monkeypatch)
    assert rc == 0
    led = json.loads((tmp_path / "ledger.json").read_text())
    assert led["weeks"] == [] and led["totals"] is None and not list(tmp_path.glob("recap_*.md"))
