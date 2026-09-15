"""
gridiron-sim nightly ingest.

Pulls schedules (with Vegas lines), weekly player stats and team stats from
nflverse via nflreadpy and upserts them into Supabase.

Usage:
  python jobs/ingest.py                      # current season
  python jobs/ingest.py --seasons 2023 2024  # backfill
  python jobs/ingest.py --dry-run            # pull + transform, no writes

Env:
  SUPABASE_URL              https://xxxx.supabase.co
  SUPABASE_SERVICE_ROLE_KEY service role key (never ship to the browser)
"""
from __future__ import annotations

import argparse
import datetime as dt
import os
import sys

import nflreadpy as nfl
import polars as pl

# ------------------------------------------------------------------
# Column mapping: nflverse name(s) -> our column. First match wins.
# ------------------------------------------------------------------
PLAYER_COLS = {
    "player_id": ["player_id", "gsis_id"],
    "season": ["season"],
    "week": ["week"],
    "season_type": ["season_type"],
    "player_name": ["player_display_name", "player_name"],
    "position": ["position"],
    "position_group": ["position_group"],
    "team": ["team", "recent_team"],
    "opponent_team": ["opponent_team"],
    "headshot_url": ["headshot_url"],
    "completions": ["completions"],
    "attempts": ["attempts"],
    "passing_yards": ["passing_yards"],
    "passing_tds": ["passing_tds"],
    "interceptions": ["passing_interceptions", "interceptions"],
    "sacks": ["sacks_suffered", "sacks"],
    "passing_air_yards": ["passing_air_yards"],
    "passing_epa": ["passing_epa"],
    "passing_2pt": ["passing_2pt_conversions"],
    "carries": ["carries"],
    "rushing_yards": ["rushing_yards"],
    "rushing_tds": ["rushing_tds"],
    "rushing_fumbles_lost": ["rushing_fumbles_lost"],
    "rushing_epa": ["rushing_epa"],
    "rushing_2pt": ["rushing_2pt_conversions"],
    "targets": ["targets"],
    "receptions": ["receptions"],
    "receiving_yards": ["receiving_yards"],
    "receiving_tds": ["receiving_tds"],
    "receiving_fumbles_lost": ["receiving_fumbles_lost"],
    "receiving_air_yards": ["receiving_air_yards"],
    "receiving_yac": ["receiving_yards_after_catch"],
    "receiving_epa": ["receiving_epa"],
    "receiving_2pt": ["receiving_2pt_conversions"],
    "target_share": ["target_share"],
    "air_yards_share": ["air_yards_share"],
    "wopr": ["wopr"],
    "special_teams_tds": ["special_teams_tds"],
    "fantasy_points": ["fantasy_points"],
    "fantasy_points_ppr": ["fantasy_points_ppr"],
}

TEAM_COLS = {
    "team": ["team"],
    "season": ["season"],
    "week": ["week"],
    "season_type": ["season_type"],
    "opponent_team": ["opponent_team"],
    "completions": ["completions"], "attempts": ["attempts"],
    "passing_yards": ["passing_yards"], "passing_tds": ["passing_tds"],
    "interceptions": ["passing_interceptions", "interceptions"],
    "sacks": ["sacks_suffered", "sacks"], "passing_epa": ["passing_epa"],
    "carries": ["carries"], "rushing_yards": ["rushing_yards"],
    "rushing_tds": ["rushing_tds"], "rushing_epa": ["rushing_epa"],
    "targets": ["targets"], "receptions": ["receptions"],
    "receiving_yards": ["receiving_yards"], "receiving_tds": ["receiving_tds"],
}

GAME_COLS = {
    "game_id": ["game_id"], "season": ["season"], "week": ["week"],
    "season_type": ["game_type", "season_type"],
    "gameday": ["gameday"], "gametime": ["gametime"], "weekday": ["weekday"],
    "home_team": ["home_team"], "away_team": ["away_team"],
    "home_score": ["home_score"], "away_score": ["away_score"],
    "spread_line": ["spread_line"], "total_line": ["total_line"],
    "home_moneyline": ["home_moneyline"], "away_moneyline": ["away_moneyline"],
    "roof": ["roof"], "surface": ["surface"], "temp": ["temp"], "wind": ["wind"],
    "stadium": ["stadium"],
}

INT_COLS = {
    "completions", "attempts", "passing_tds", "interceptions", "passing_2pt",
    "carries", "rushing_tds", "rushing_fumbles_lost", "rushing_2pt",
    "targets", "receptions", "receiving_tds", "receiving_fumbles_lost",
    "receiving_2pt", "special_teams_tds", "home_score", "away_score",
    "home_moneyline", "away_moneyline", "temp", "wind", "season", "week",
}


def select_mapped(df: pl.DataFrame, mapping: dict[str, list[str]]) -> pl.DataFrame:
    exprs = []
    for out, candidates in mapping.items():
        src = next((c for c in candidates if c in df.columns), None)
        if src is None:
            exprs.append(pl.lit(None).alias(out))
        else:
            exprs.append(pl.col(src).alias(out))
    out = df.select(exprs)
    # normalise types
    casts = []
    for c in out.columns:
        if c in INT_COLS:
            casts.append(pl.col(c).cast(pl.Float64, strict=False).round(0).cast(pl.Int64, strict=False))
    if casts:
        out = out.with_columns(casts)
    return out


def current_season() -> int:
    today = dt.date.today()
    return today.year if today.month >= 9 else today.year - 1


def pull(seasons: list[int]) -> tuple[pl.DataFrame, pl.DataFrame, pl.DataFrame]:
    print(f"Pulling seasons {seasons} ...")
    sched = nfl.load_schedules(seasons)
    if not isinstance(sched, pl.DataFrame):
        sched = pl.DataFrame(sched)
    games = select_mapped(sched, GAME_COLS)
    games = games.with_columns(
        pl.col("season_type").map_elements(
            lambda s: "POST" if s in ("WC", "DIV", "CON", "SB", "POST") else "REG",
            return_dtype=pl.Utf8,
        ),
        pl.col("gameday").cast(pl.Utf8),
    )

    pstats = nfl.load_player_stats(seasons, summary_level="week")
    if not isinstance(pstats, pl.DataFrame):
        pstats = pl.DataFrame(pstats)
    players = select_mapped(pstats, PLAYER_COLS)
    players = players.filter(pl.col("player_id").is_not_null())
    players = players.with_columns(pl.col("season_type").fill_null("REG"))
    # some rows in nflverse are per-team-week; dedupe defensively
    players = players.unique(subset=["player_id", "season", "week", "season_type"], keep="last")

    tstats = nfl.load_team_stats(seasons, summary_level="week")
    if not isinstance(tstats, pl.DataFrame):
        tstats = pl.DataFrame(tstats)
    teams = select_mapped(tstats, TEAM_COLS)
    teams = teams.with_columns(pl.col("season_type").fill_null("REG"))
    teams = teams.unique(subset=["team", "season", "week", "season_type"], keep="last")

    print(f"  games={games.height} player-games={players.height} team-games={teams.height}")
    return games, players, teams


def to_records(df: pl.DataFrame) -> list[dict]:
    recs = df.to_dicts()
    # NaN -> None for JSON
    for r in recs:
        for k, v in r.items():
            if isinstance(v, float) and v != v:
                r[k] = None
    return recs


def upsert(client, table: str, df: pl.DataFrame, on_conflict: str, batch: int = 1000):
    recs = to_records(df)
    print(f"  upserting {len(recs)} rows into {table}")
    for i in range(0, len(recs), batch):
        client.table(table).upsert(recs[i:i + batch], on_conflict=on_conflict).execute()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seasons", nargs="*", type=int, default=[current_season()])
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    games, players, teams = pull(args.seasons)

    if args.dry_run:
        print(players.head(5))
        print("dry run — no writes")
        return

    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
    if not url or not key:
        print("SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY not set", file=sys.stderr)
        sys.exit(1)

    from supabase import create_client
    client = create_client(url, key)

    upsert(client, "games", games, "game_id")
    upsert(client, "team_game_stats", teams, "team,season,week,season_type")
    upsert(client, "player_game_stats", players, "player_id,season,week,season_type")
    print("done")


if __name__ == "__main__":
    main()
