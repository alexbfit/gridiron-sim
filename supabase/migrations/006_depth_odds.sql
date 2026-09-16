-- gridiron-sim: Phase 8 — depth charts, live odds, external projections
-- Run after 005_results_ownership.sql.

create table if not exists depth_charts (
  player_id    text not null,
  season       int  not null,
  week         int  not null,
  team         text,
  position     text,
  pos_rank     int,
  snapshot_at  timestamptz,
  updated_at   timestamptz default now(),
  primary key (player_id, season, week)
);
create index if not exists depth_charts_season_week_idx on depth_charts (season, week);
alter table depth_charts enable row level security;
drop policy if exists "depth_charts public read" on depth_charts;
create policy "depth_charts public read" on depth_charts for select using (true);

-- live odds (jobs/odds.py) overwrite spread_line / total_line; keep the source + time
alter table games add column if not exists lines_source text;
alter table games add column if not exists lines_updated_at timestamptz;

-- external projections import (jobs/import_projections.py) writes slate_projections with
-- method = 'external'; the board keeps preferring sim, the builder blends them on demand.
