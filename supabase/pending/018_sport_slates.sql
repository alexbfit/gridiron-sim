-- 018: per-slate projections for the non-NFL sports (NBA first), written by jobs/sports/nba_daily.py as source 'props_nba'
-- (and later by other projection methods / sports under their own source). One row per sport x slate date x player x source;
-- re-runs upsert. The site reads it with the anon key (projections hold no secrets); writes need the service role key, which
-- is why nba_daily.py skips the write silently when SUPABASE_SERVICE_ROLE_KEY is not set or this table does not exist yet.
-- Run once in the Supabase SQL editor.

create table if not exists sport_projections (
  id           bigserial primary key,
  sport        text not null,                       -- 'NBA', 'NHL', ...
  slate_date   date not null,                       -- slate day (ET)
  player_name  text not null,                       -- DraftKings' Name column, exactly
  team         text,
  opp          text,
  proj         numeric,                             -- DK points (mean)
  p25          numeric,
  p50          numeric,
  p75          numeric,
  p85          numeric,
  p95          numeric,
  p99          numeric,
  source       text not null default 'props_nba',   -- 'props_nba' | 'props_partial' | 'fallback' | ...
  updated_at   timestamptz not null default now(),
  unique (sport, slate_date, player_name, source)
);
create index if not exists sport_projections_slate_idx on sport_projections (sport, slate_date desc);

alter table sport_projections enable row level security;
drop policy if exists "sport_projections public read" on sport_projections;
create policy "sport_projections public read" on sport_projections for select using (true);
-- no insert/update policy for anon: only the service role (GitHub workflow) writes
