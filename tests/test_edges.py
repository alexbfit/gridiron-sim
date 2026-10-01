"""Prop edge screener (jobs/edges.py): the probability math, and a full run on the week-3 fixture with a mocked
Kalshi ladder that must produce web/data/edges.json with the documented columns."""
import gzip
import json
import math

import pytest

from conftest import run_main

import edges

REQUIRED_COLS = {"player", "team", "market", "rung", "our_p", "market_p", "edge", "ev", "kelly", "source", "url"}


# ------------------------------------------------------------------ probability math
def test_lognormal_mean_and_median_round_trip():
    mu, s = edges.lognormal_params(100.0, 0.5)
    assert math.exp(mu + s * s / 2) == pytest.approx(100.0)                  # mean preserved
    assert edges.lognormal_sf(100.0, 0.5, math.exp(mu)) == pytest.approx(0.5)  # survival at the median is 1/2
    assert edges.median_to_mean(math.exp(mu), 0.5) == pytest.approx(100.0)


def test_lognormal_sf_monotone_and_bounded():
    ps = [edges.lognormal_sf(60.0, 0.75, t) for t in (0, 10, 40, 60, 100, 200)]
    assert ps[0] == 1.0 and all(a >= b for a, b in zip(ps, ps[1:])) and 0 < ps[-1] < 0.05
    assert edges.lognormal_sf(0.0, 0.5, 10) == 0.0


def test_poisson_ge():
    lam = 1.7
    assert edges.poisson_ge(lam, 0) == 1.0
    assert edges.poisson_ge(lam, 1) == pytest.approx(1 - math.exp(-lam))
    assert edges.poisson_ge(lam, 2) == pytest.approx(1 - math.exp(-lam) * (1 + lam))
    assert edges.poisson_ge(0.0, 1) == 0.0
    # receptions "5+" is P(X > 4.5) -> k = 5
    p, cv = edges.over_prob("player_receptions", "WR", 4.0, 4.5)
    assert p == pytest.approx(edges.poisson_ge(4.0, 5)) and cv is None


def test_devig_and_market_prices():
    assert edges.devig(0.55, 0.55) == pytest.approx(0.5)
    po, pu = 1 / 1.80, 1 / 2.10                     # -125 / +110 style pair
    assert edges.devig(po, pu) + edges.devig(pu, po) == pytest.approx(1.0)
    assert edges.american_payout(-110) == pytest.approx(1 + 100 / 110)
    assert edges.american_payout(+150) == pytest.approx(2.5)


def test_kalshi_fee_and_ev():
    assert edges.kalshi_fee(0.5) == pytest.approx(0.0175)                    # 1.75c at 50c, ~3.5% of the stake
    payout = 1 / (0.5 + edges.kalshi_fee(0.5))
    assert edges.ev_per_dollar(0.5, payout) < 0                               # fair price minus fee loses
    assert edges.ev_per_dollar(0.6, payout) == pytest.approx(0.6 * payout - 1)


def test_kelly_fraction_and_cap():
    assert edges.kelly(0.5, 2.0) == 0.0                                       # no edge, no stake
    assert edges.kelly(0.3, 2.0) == 0.0                                       # negative edge clipped to 0
    f_full = (0.55 * 1.0 - 0.45) / 1.0                                        # even money, p = .55 -> 10% full Kelly
    assert edges.kelly(0.55, 2.0, frac=0.25, cap=1.0) == pytest.approx(0.25 * f_full)
    assert edges.kelly(0.9, 2.0) == edges.KELLY_CAP                           # capped at 2%


def test_fit_lognormal_cv_recovers_shape():
    mean, cv = 70.0, 0.65
    pts = [(t, edges.lognormal_sf(mean, cv, t)) for t in (24.5, 39.5, 49.5, 59.5, 69.5, 79.5, 99.5, 119.5)]
    assert edges.fit_lognormal_cv(pts) == pytest.approx(cv, abs=0.03)


# ------------------------------------------------------------------ full run on the week-3 fixture
def _ladder(name, series, event, strikes, probs):
    """Kalshi-shaped markets for one player: strike t means "t+0.5 +" (floor_strike = t)."""
    out = []
    for t, p in zip(strikes, probs):
        out.append({"title": f"{name}: {int(t + 0.5)}+ x", "event_ticker": f"{series}-{event}", "floor_strike": t,
                    "ticker": f"{series}-{event}-X-{int(t + 0.5)}", "yes_bid_dollars": f"{max(0.01, p - 0.03):.2f}",
                    "yes_ask_dollars": f"{min(0.99, p + 0.03):.2f}"})
    return out


def test_edges_run_on_week3_fixture(db, monkeypatch, tmp_path, capsys):
    import kalshi
    monkeypatch.setattr(edges, "get_client", lambda need_write=False: db.client)
    # pick a WR with sportsbook props and a QB from the fixture to put on the mocked ladders
    props = {r["player_name"]: r for r in db.rows("slate_projections") if r["method"] == "props"}
    wr = max((r for r in props.values() if r["position"] == "WR" and "odds" in r["components"]["src"] and r["components"]["lines"].get("reception_yds")),
             key=lambda r: r["components"]["lines"]["reception_yds"])          # a high-volume WR (line ~70+)
    qb = next(r for r in props.values() if r["position"] == "QB")
    event = "26SEP27AAABBB"                                  # 2026-09-27 = the fixture's gameday

    def fake_fetch(series):
        if series == "KXNFLRECYDS":
            # a market that is far too pessimistic about the WR -> big "yes" edges
            return _ladder(wr["player_name"], series, event, [24.5, 39.5, 49.5, 59.5, 79.5], [0.55, 0.30, 0.20, 0.12, 0.05])
        if series == "KXNFLPASSYDS":
            return _ladder(qb["player_name"], series, event, [199.5, 224.5, 249.5, 274.5, 299.5], [0.85, 0.70, 0.50, 0.30, 0.15])
        if series == "KXNFLTD":
            return _ladder(wr["player_name"], series, event, [0.5], [0.40])
        return []

    monkeypatch.setattr(kalshi, "fetch_series", fake_fetch)
    monkeypatch.delenv("ODDS_API_KEY", raising=False)
    out = tmp_path / "edges.json"
    rc = run_main("edges", ["--slate-key", "DK-2026-03-main", "--out", str(out), "--all"], monkeypatch)
    text = capsys.readouterr().out
    assert rc == 0, text
    d = json.loads(out.read_text())
    assert d["slate"] == "DK-2026-03-main" and d["gameday"] == "2026-09-27" and d["generated_at"]
    assert d["stats"]["kalshi_rungs"] == 11 and d["stats"]["kalshi_matched"] == 11 and d["stats"]["players_matched"] == 2
    assert d["rows"], text
    for r in d["rows"]:
        assert REQUIRED_COLS <= set(r), r
        assert 0 <= r["our_p"] <= 1 and 0 <= r["market_p"] <= 1
        assert r["edge"] == pytest.approx((r["our_p"] - r["market_p"]) * 100, abs=0.06)
        assert 0 <= r["kelly"] <= edges.KELLY_CAP
        assert r["url"].startswith("https://kalshi.com/markets/")
    assert [r["ev"] for r in d["rows"]] == sorted((r["ev"] for r in d["rows"]), reverse=True)   # ranked by EV
    # the WR has a sportsbook line -> high confidence; every rung of his ladder shows up on exactly one side
    wr_rows = [r for r in d["rows"] if r["player"] == wr["player_name"] and r["market"] == "player_reception_yds"]
    assert wr_rows and all(r["confidence"] == "props+sim" for r in wr_rows)
    assert len({r["threshold"] for r in wr_rows}) == 5 and all(r["side"] == "yes" for r in wr_rows)
    assert "sim-only" not in wr_rows[0]["flags"]
    # the pessimistic ladder is a large disagreement -> flagged, not hidden
    assert any("large-disagreement" in r["flags"] for r in wr_rows)


def test_edges_min_edge_filters_small_rows(db, monkeypatch, tmp_path, capsys):
    import kalshi
    monkeypatch.setattr(edges, "get_client", lambda need_write=False: db.client)
    qb = next(r for r in db.rows("slate_projections") if r["method"] == "props" and r["position"] == "QB")
    event = "26SEP27AAABBB"
    # a ladder priced exactly at our own model -> no edges survive the 3-point filter
    sim = next(r for r in db.rows("slate_projections") if r["method"] == "sim" and r["site_player_id"] == qb["site_player_id"])
    players = edges.player_inputs(db.rows("slate_salaries"), [sim], [qb], None)
    pl = players[edges.norm_name(qb["player_name"])]
    mean = pl["means"]["player_pass_yds"][0]
    strikes = [199.5, 224.5, 249.5, 274.5]
    probs = [edges.over_prob("player_pass_yds", "QB", mean, t)[0] for t in strikes]
    monkeypatch.setattr(kalshi, "fetch_series", lambda s: _ladder(qb["player_name"], s, event, strikes, probs) if s == "KXNFLPASSYDS" else [])
    out = tmp_path / "edges.json"
    rc = run_main("edges", ["--slate-key", "DK-2026-03-main", "--out", str(out)], monkeypatch)
    assert rc == 0, capsys.readouterr().out
    d = json.loads(out.read_text())
    assert d["stats"]["kalshi_matched"] == 4 and d["rows"] == []
