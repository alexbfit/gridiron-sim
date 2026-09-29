"""QB props adjustment — the "SaberSim-gap" model (2026-09-29; project doc claude/qb-model.md).

SaberSim's QB projection differs from the calibrated sportsbook-props projection by +/-0.9 pts, and that difference is fully
informative (actual - props = 0.5 + 1.08 x gap; 2024-25, 773 QB starter-weeks). A ridge on eleven pre-lock features predicts
~40% of it out of sample. Adding the prediction to the props QB projection raised value-over-salary r .180 -> .194 (2024, fit on
2025) and .283 -> .294 (2025, fit on 2024); SaberSim itself .224 / .303. Contest level (live config, 2 seeds): 2025 PA #1-20
+0.4 +/- 0.5 pts, 2024 +0.7 +/- 0.7, never negative. Typical shift +/-0.35 pts, max +/-1.6; clipped at +/-2.

Read: shrink the books' QB number ~15% toward a team-implied-total baseline (props QB lines are over-dispersed), dock wind
(-0.34 per 10 mph), dock favourites (-0.16 for a 7-pt favourite), dock INT-prone QBs, give early-season starters a little back.

Used by props.py (QB rows only). Refit after the 2026 season: jobs in DFS DATA\_backtest_archive\qb_model.zip (qbm/fit4.py).
"""
from __future__ import annotations

from collections import defaultdict

COEF = {"wind": -0.03380, "pr_ryds": -0.00030, "q_rush_yds": -0.00166, "spread": -0.02314, "home": 0.08881, "dome": -0.05020,
        "implied": 0.05564, "q_intr": -6.61479, "q_games_this": -0.02881, "props_raw": -0.14614, "price": 0.00009}
INTERCEPT = 1.2158
IMPUTE = {"wind": 5.181, "pr_ryds": 15.585, "q_rush_yds": 17.54, "spread": -0.069, "home": 0.501, "dome": 0.34, "implied": 22.149,
          "q_intr": 0.022, "q_games_this": 6.146, "props_raw": 16.751, "price": 5750.841}
CLIP = 2.0


def qb_gap(raw: float, price: float, implied=None, spread=None, home=None, dome=None, wind=None, rush_yds_line=None,
           rush_yds_avg=None, int_rate=None, games_this=None) -> float:
    """Points to add to the calibrated props projection of a QB. spread: the QB's team, negative = favourite."""
    x = {"props_raw": raw, "price": price, "implied": implied, "spread": spread, "home": home, "dome": dome, "wind": wind,
         "pr_ryds": rush_yds_line, "q_rush_yds": rush_yds_avg, "q_intr": int_rate, "q_games_this": games_this}
    g = INTERCEPT
    for k, c in COEF.items():
        v = x.get(k)
        if v is None or v != v:
            v = IMPUTE[k]
        g += c * float(v)
    return max(-CLIP, min(CLIP, g))


def game_context(games: list[dict]) -> dict:
    """team -> {implied, spread (team perspective, negative = favourite), home, dome, wind} from games rows
    (spread_line: positive = home favoured; total_line; roof; forecast_wind)."""
    ctx = {}
    for g in games:
        sp, tot = g.get("spread_line"), g.get("total_line")
        roof = (g.get("roof") or "").lower()
        wind = g.get("forecast_wind")
        dome = 1.0 if roof in ("dome", "closed") else 0.0
        if dome:
            wind = 0.0
        for team, is_home in ((g.get("home_team"), 1.0), (g.get("away_team"), 0.0)):
            if not team:
                continue
            tspread = None if sp is None else (-float(sp) if is_home else float(sp))
            implied = None if (tot is None or tspread is None) else (float(tot) - tspread) / 2
            ctx[team] = {"implied": implied, "spread": tspread, "home": is_home, "dome": dome,
                         "wind": None if wind is None else float(wind)}
    return ctx


def qb_history(client, player_ids: list[str], season: int, week: int) -> dict:
    """player_id -> {int_rate, rush_yds_avg, games_this} from player_game_stats, prior weeks of this season
    (last season added when fewer than 3 starts)."""
    from common import fetch_all, chunked
    rows = []
    for batch in chunked(sorted(set(player_ids)), 200):
        rows += fetch_all(client.table("player_game_stats").select("player_id,season,week,attempts,interceptions,rushing_yards")
                          .in_("player_id", batch).in_("season", [season, season - 1]).eq("season_type", "REG"),
                          order=["player_id", "season", "week"])
    by = defaultdict(list)
    for r in rows:
        if r["season"] == season and r["week"] >= week:
            continue
        if (r.get("attempts") or 0) < 5:
            continue
        by[r["player_id"]].append(r)
    out = {}
    for pid, rs in by.items():
        this = [r for r in rs if r["season"] == season]
        use = this if len(this) >= 3 else rs
        att = sum(r["attempts"] or 0 for r in use)
        out[pid] = {"int_rate": (sum(r["interceptions"] or 0 for r in use) / att) if att else None,
                    "rush_yds_avg": sum(float(r["rushing_yards"] or 0) for r in use) / len(use) if use else None,
                    "games_this": len(this)}
    return out
