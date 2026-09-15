-- gridiron-sim: Phase 2 — slates, salaries, projections
-- Run after 001_schema.sql.

-- slates: stable key so imports upsert instead of duplicating
alter table slates add column if not exists slate_key text;
alter table slates add column if not exists game_ids jsonb;
alter table slates add column if not exists n_players int;
alter table slates add column if not exists imported_at timestamptz default now();
create unique index if not exists slates_key_idx on slates (slate_key);

-- salaries: extra columns from the DK/FD export
alter table slate_salaries add column if not exists status text;
alter table slate_salaries add column if not exists avg_points numeric;
alter table slate_salaries add column if not exists game_info text;
alter table slate_salaries add column if not exists matched_by text;   -- how player_id was resolved (null = unmatched)

-- projections: one row per slate player, written by jobs/project.py (Phase 2)
-- and later by the Monte Carlo sim (Phase 3, method = 'sim').
create table if not exists slate_projections (
  slate_id       uuid references slates(slate_id) on delete cascade,
  site_player_id text not null,
  player_id      text,
  player_name    text,
  position       text,
  team           text,
  opponent       text,
  salary         int,
  method         text not null default 'baseline',
  mean           numeric,
  stdev          numeric,
  median         numeric,
  p15            numeric,
  p85            numeric,
  p95            numeric,
  floor          numeric,
  ceiling        numeric,
  boom_prob      numeric,
  bust_prob      numeric,
  games_used     int,
  components     jsonb,        -- {base, opp_factor, vegas_factor, implied_total, status_factor, ...}
  updated_at     timestamptz default now(),
  primary key (slate_id, site_player_id)
);
alter table slate_projections enable row level security;
drop policy if exists "slate_projections public read" on slate_projections;
create policy "slate_projections public read" on slate_projections for select using (true);

-- one query for the lineup builder
create or replace view slate_board as
select
  s.slate_id, s.slate_key, s.site, s.season, s.week, s.slate_name,
  ss.site_player_id, ss.player_id, ss.player_name, ss.position, ss.roster_positions,
  ss.team, ss.opponent, ss.game_id, ss.salary, ss.status, ss.avg_points, ss.game_info, ss.matched_by,
  p.method, p.mean, p.stdev, p.median, p.p15, p.p85, p.p95, p.floor, p.ceiling,
  p.boom_prob, p.bust_prob, p.games_used, p.components,
  case when ss.salary > 0 then round(p.mean / (ss.salary / 1000.0), 2) end as value
from slates s
join slate_salaries ss on ss.slate_id = s.slate_id
left join slate_projections p on p.slate_id = ss.slate_id and p.site_player_id = ss.site_player_id;
