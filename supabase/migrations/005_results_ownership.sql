-- gridiron-sim: Phase 7 — results tracking + actual ownership
-- Run after 004_snaps_injuries.sql.

-- per-player outcome for a completed slate (written by jobs/results.py)
create table if not exists slate_results (
  slate_id        uuid references slates(slate_id) on delete cascade,
  site_player_id  text not null,
  player_id       text,
  player_name     text,
  position        text,
  team            text,
  salary          int,
  method          text,                 -- projection method that was live (sim / baseline)
  proj_mean       numeric,
  proj_p10        numeric,
  proj_p50        numeric,
  proj_p90        numeric,
  actual          numeric,              -- site points (DK: from contest FPTS when available, else computed)
  actual_source   text,                 -- 'contest' | 'stats' | 'stats_approx' (DST)
  played          boolean,
  pit             numeric,              -- where the actual landed in the sim distribution (0..1)
  own_actual      numeric,              -- % drafted, from a contest standings file
  own_heuristic   numeric,
  computed_at     timestamptz default now(),
  primary key (slate_id, site_player_id)
);

-- actual ownership from DK/FD contest standings exports (jobs/import_ownership.py)
create table if not exists slate_ownership (
  slate_id        uuid references slates(slate_id) on delete cascade,
  site_player_id  text not null,
  player_id       text,
  ownership_pct   numeric,
  fpts            numeric,
  contest         text,
  imported_at     timestamptz default now(),
  primary key (slate_id, site_player_id)
);

alter table slates add column if not exists results_meta jsonb;   -- {mae, r, bias, coverage, n, scored_at}

alter table slate_results enable row level security;
alter table slate_ownership enable row level security;
drop policy if exists "slate_results public read" on slate_results;
create policy "slate_results public read" on slate_results for select using (true);
drop policy if exists "slate_ownership public read" on slate_ownership;
create policy "slate_ownership public read" on slate_ownership for select using (true);
