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


def pull_extras(seasons: list[int]) -> tuple[pl.DataFrame, pl.DataFrame]:
    """Snap counts (pfr ids -> gsis ids via the players table) and official injury reports."""
    snaps = nfl.load_snap_counts(seasons)
    if not isinstance(snaps, pl.DataFrame):
        snaps = pl.DataFrame(snaps)
    ids = nfl.load_players()
    if not isinstance(ids, pl.DataFrame):
        ids = pl.DataFrame(ids)
    ids = ids.select(["gsis_id", "pfr_id"]).filter(pl.col("pfr_id").is_not_null()).unique(subset=["pfr_id"])
    snaps = (snaps.join(ids, left_on="pfr_player_id", right_on="pfr_id", how="left")
             .filter(pl.col("gsis_id").is_not_null() & pl.col("position").is_in(["QB", "RB", "WR", "TE"]))
             .select([
                 pl.col("gsis_id").alias("player_id"), pl.col("season"), pl.col("week"),
                 pl.col("game_type").map_elements(lambda s: "POST" if s in ("WC", "DIV", "CON", "SB", "POST") else "REG",
                                                  return_dtype=pl.Utf8).alias("season_type"),
                 pl.col("team"), pl.col("position"),
                 pl.col("offense_snaps").cast(pl.Float64, strict=False).round(0).cast(pl.Int64, strict=False),
                 pl.col("offense_pct"),
             ]).unique(subset=["player_id", "season", "week", "season_type"], keep="last"))

    inj = nfl.load_injuries(seasons)
    if not isinstance(inj, pl.DataFrame):
        inj = pl.DataFrame(inj)
    if "season_type" not in inj.columns:      # older seasons carry game_type instead
        inj = inj.with_columns(pl.col("game_type").map_elements(
            lambda s: "POST" if s in ("WC", "DIV", "CON", "SB", "POST") else "REG", return_dtype=pl.Utf8).alias("season_type"))
    inj = (inj.filter(pl.col("gsis_id").is_not_null())
           .select([
               pl.col("gsis_id").alias("player_id"), pl.col("season"), pl.col("week"),
               pl.col("season_type").fill_null("REG"), pl.col("team"), pl.col("position"),
               pl.col("full_name").alias("player_name"), pl.col("report_status"),
               pl.col("practice_status"), pl.col("report_primary_injury").alias("primary_injury"),
           ]).unique(subset=["player_id", "season", "week", "season_type"], keep="last"))
    print(f"  snaps={snaps.height} injuries={inj.height}")
    return snaps, inj


def pull_depth(seasons: list[int], schedules: pl.DataFrame) -> pl.DataFrame:
    """Latest depth-chart snapshot, assigned to the upcoming REG week (the first week whose
    games are not all final). One row per player."""
    d = nfl.load_depth_charts(seasons)
    if not isinstance(d, pl.DataFrame):
        d = pl.DataFrame(d)
    d = d.filter(pl.col("gsis_id").is_not_null() & pl.col("pos_abb").is_in(["QB", "RB", "WR", "TE"]))
    latest = d["dt"].max()
    d = d.filter(pl.col("dt") == latest)
    season = int(max(seasons))
    reg = schedules.filter((pl.col("season") == season) & (pl.col("season_type") == "REG"))
    pending = reg.filter(pl.col("home_score").is_null())
    week = int(pending["week"].min()) if pending.height else int(reg["week"].max())
    out = d.select([
        pl.col("gsis_id").alias("player_id"), pl.lit(season).alias("season"), pl.lit(week).alias("week"),
        pl.col("team"), pl.col("pos_abb").alias("position"),
        pl.col("pos_rank").cast(pl.Int64, strict=False), pl.col("dt").alias("snapshot_at"),
    ]).unique(subset=["player_id"], keep="first")
    print(f"  depth chart snapshot {latest} -> season {season} week {week}: {out.height} players")
    return out


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
    try:
        snaps, injuries = pull_extras(args.seasons)
    except Exception as e:          # never let the extras block the core stats
        print(f"  extras failed: {e}")
        snaps = injuries = None
    try:
        depth = pull_depth(args.seasons, games)
    except Exception as e:
        print(f"  depth charts failed: {e}")
        depth = None

    if args.dry_run:
        print(players.head(5))
        if snaps is not None:
            print(snaps.head(3)); print(injuries.head(3))
        print("dry run — no writes")
        return

    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
    if not url or not key:
        print("SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY not set", file=sys.stderr)
        sys.exit(1)

    from supabase import create_client
    client = create_client(url, key)

    # don't clobber live odds-api lines with nflverse's (older) numbers for upcoming games
    try:
        live = client.table("games").select("game_id,spread_line,total_line").eq("lines_source", "odds-api").execute().data
    except Exception:
        live = []
    keep = {r["game_id"]: (r["spread_line"], r["total_line"]) for r in live}
    if keep:
        games = games.with_columns([
            pl.col("game_id").map_elements(lambda g: float(keep[g][0]) if g in keep else None, return_dtype=pl.Float64).alias("_s"),
            pl.col("game_id").map_elements(lambda g: float(keep[g][1]) if g in keep else None, return_dtype=pl.Float64).alias("_t"),
        ]).with_columns([
            pl.coalesce([pl.col("_s"), pl.col("spread_line")]).alias("spread_line"),
            pl.coalesce([pl.col("_t"), pl.col("total_line")]).alias("total_line"),
        ]).drop(["_s", "_t"])
    upsert(client, "games", games, "game_id")
    upsert(client, "team_game_stats", teams, "team,season,week,season_type")
    upsert(client, "player_game_stats", players, "player_id,season,week,season_type")
    if snaps is not None:
        try:
            upsert(client, "player_snaps", snaps, "player_id,season,week,season_type")
            upsert(client, "injury_reports", injuries, "player_id,season,week,season_type")
        except Exception as e:
            print(f"  snaps/injuries not written (run 004_snaps_injuries.sql?): {e}")
    if depth is not None:
        try:
            upsert(client, "depth_charts", depth, "player_id,season,week")
        except Exception as e:
            print(f"  depth charts not written (run 006_depth_odds.sql?): {e}")
    print("done")


if __name__ == "__main__":
    main()
