-- gridiron-sim: Phase 1 schema
-- Run in Supabase SQL editor (or `supabase db push`).

create extension if not exists "pgcrypto";

-- ---------------------------------------------------------------
-- Reference
-- ---------------------------------------------------------------
create table if not exists teams (
  team_abbr   text primary key,
  team_name   text,
  team_conf   text,
  team_division text,
  team_color  text,
  team_logo   text
);

-- ---------------------------------------------------------------
-- Games (from nflverse schedules; carries Vegas lines)
-- ---------------------------------------------------------------
create table if not exists games (
  game_id       text primary key,
  season        int not null,
  week          int not null,
  season_type   text not null,          -- REG / POST
  gameday       date,
  gametime      text,
  weekday       text,
  home_team     text not null,
  away_team     text not null,
  home_score    int,
  away_score    int,
  spread_line   numeric,                -- home-team perspective (positive = home favored)
  total_line    numeric,
  home_moneyline int,
  away_moneyline int,
  roof          text,
  surface       text,
  temp          int,
  wind          int,
  stadium       text,
  updated_at    timestamptz default now()
);
create index if not exists games_season_week_idx on games (season, week);

-- ---------------------------------------------------------------
-- Player game stats (nflverse weekly player stats)
-- DK / FD points are generated columns so scoring lives in the DB.
-- ---------------------------------------------------------------
create table if not exists player_game_stats (
  player_id         text not null,
  season            int  not null,
  week              int  not null,
  season_type       text not null default 'REG',
  player_name       text,
  position          text,
  position_group    text,
  team              text,
  opponent_team     text,
  headshot_url      text,

  -- passing
  completions       int default 0,
  attempts          int default 0,
  passing_yards     numeric default 0,
  passing_tds       int default 0,
  interceptions     int default 0,
  sacks             numeric default 0,
  passing_air_yards numeric default 0,
  passing_epa       numeric,
  passing_2pt       int default 0,

  -- rushing
  carries           int default 0,
  rushing_yards     numeric default 0,
  rushing_tds       int default 0,
  rushing_fumbles_lost int default 0,
  rushing_epa       numeric,
  rushing_2pt       int default 0,

  -- receiving
  targets           int default 0,
  receptions        int default 0,
  receiving_yards   numeric default 0,
  receiving_tds     int default 0,
  receiving_fumbles_lost int default 0,
  receiving_air_yards numeric default 0,
  receiving_yac     numeric default 0,
  receiving_epa     numeric,
  receiving_2pt     int default 0,
  target_share      numeric,
  air_yards_share   numeric,
  wopr              numeric,

  -- misc
  special_teams_tds int default 0,
  fantasy_points     numeric,
  fantasy_points_ppr numeric,

  -- DraftKings classic scoring
  dk_points numeric generated always as (
      passing_yards * 0.04 + passing_tds * 4 - interceptions * 1
    + (case when passing_yards >= 300 then 3 else 0 end)
    + rushing_yards * 0.1 + rushing_tds * 6
    + (case when rushing_yards >= 100 then 3 else 0 end)
    + receptions * 1 + receiving_yards * 0.1 + receiving_tds * 6
    + (case when receiving_yards >= 100 then 3 else 0 end)
    - (rushing_fumbles_lost + receiving_fumbles_lost) * 1
    + (passing_2pt + rushing_2pt + receiving_2pt) * 2
    + special_teams_tds * 6
  ) stored,

  -- FanDuel scoring (half PPR, no yardage bonuses)
  fd_points numeric generated always as (
      passing_yards * 0.04 + passing_tds * 4 - interceptions * 1
    + rushing_yards * 0.1 + rushing_tds * 6
    + receptions * 0.5 + receiving_yards * 0.1 + receiving_tds * 6
    - (rushing_fumbles_lost + receiving_fumbles_lost) * 2
    + (passing_2pt + rushing_2pt + receiving_2pt) * 2
    + special_teams_tds * 6
  ) stored,

  updated_at timestamptz default now(),
  primary key (player_id, season, week, season_type)
);
create index if not exists pgs_season_week_idx on player_game_stats (season, week);
create index if not exists pgs_position_idx on player_game_stats (position);
create index if not exists pgs_team_idx on player_game_stats (team);

-- ---------------------------------------------------------------
-- Team game stats
-- ---------------------------------------------------------------
create table if not exists team_game_stats (
  team          text not null,
  season        int not null,
  week          int not null,
  season_type   text not null default 'REG',
  opponent_team text,
  completions   int, attempts int, passing_yards numeric, passing_tds int,
  interceptions int, sacks numeric, passing_epa numeric,
  carries int, rushing_yards numeric, rushing_tds int, rushing_epa numeric,
  targets int, receptions int, receiving_yards numeric, receiving_tds int,
  updated_at timestamptz default now(),
  primary key (team, season, week, season_type)
);

-- ---------------------------------------------------------------
-- Phase 2+ tables (present now so the schema doesn't churn)
-- ---------------------------------------------------------------
create table if not exists slates (
  slate_id    uuid primary key default gen_random_uuid(),
  site        text not null check (site in ('DK','FD')),
  season      int not null,
  week        int not null,
  slate_name  text,
  slate_type  text default 'main',
  starts_at   timestamptz,
  created_at  timestamptz default now()
);

create table if not exists slate_salaries (
  slate_id     uuid references slates(slate_id) on delete cascade,
  site_player_id text not null,
  player_id    text,                      -- nflverse gsis id (mapped)
  player_name  text,
  position     text,
  roster_positions text,
  team         text,
  opponent     text,
  game_id      text,
  salary       int,
  site_projection numeric,
  projected_ownership numeric,
  primary key (slate_id, site_player_id)
);

create table if not exists model_params (
  param_key   text primary key,
  param_value jsonb not null,
  updated_at  timestamptz default now()
);

create table if not exists sim_summaries (
  slate_id    uuid references slates(slate_id) on delete cascade,
  player_id   text not null,
  n_sims      int,
  mean        numeric, median numeric, stdev numeric,
  p15 numeric, p85 numeric, p95 numeric,
  floor       numeric, ceiling numeric,
  boom_prob   numeric,
  bust_prob   numeric,
  created_at  timestamptz default now(),
  primary key (slate_id, player_id)
);

create table if not exists lineups (
  lineup_id   uuid primary key default gen_random_uuid(),
  user_id     uuid references auth.users(id) on delete cascade,
  slate_id    uuid references slates(slate_id) on delete cascade,
  contest_type text check (contest_type in ('cash','gpp')),
  players     jsonb not null,             -- [{site_player_id, slot, salary}]
  total_salary int,
  projected    numeric,
  created_at  timestamptz default now()
);

-- ---------------------------------------------------------------
-- Views
-- ---------------------------------------------------------------
create or replace view player_season_stats as
select
  player_id, season, season_type,
  max(player_name) as player_name,
  max(position) as position,
  max(position_group) as position_group,
  max(team) as team,
  max(headshot_url) as headshot_url,
  count(*) as games,
  sum(completions) completions, sum(attempts) attempts,
  sum(passing_yards) passing_yards, sum(passing_tds) passing_tds,
  sum(interceptions) interceptions, sum(sacks) sacks,
  sum(carries) carries, sum(rushing_yards) rushing_yards, sum(rushing_tds) rushing_tds,
  sum(targets) targets, sum(receptions) receptions,
  sum(receiving_yards) receiving_yards, sum(receiving_tds) receiving_tds,
  sum(receiving_air_yards) receiving_air_yards,
  sum(rushing_fumbles_lost + receiving_fumbles_lost) fumbles_lost,
  avg(target_share) target_share,
  avg(wopr) wopr,
  sum(dk_points) dk_points, sum(fd_points) fd_points,
  avg(dk_points) dk_ppg, avg(fd_points) fd_ppg
from player_game_stats
group by player_id, season, season_type;

-- Fantasy points allowed by each defense to each position, per season
create or replace view defense_vs_position as
select
  opponent_team as defense,
  season, season_type, position,
  count(distinct week) as games,
  sum(dk_points) / nullif(count(distinct week),0) as dk_allowed_pg,
  sum(fd_points) / nullif(count(distinct week),0) as fd_allowed_pg,
  sum(passing_yards) / nullif(count(distinct week),0) as pass_yds_pg,
  sum(rushing_yards) / nullif(count(distinct week),0) as rush_yds_pg,
  sum(receiving_yards) / nullif(count(distinct week),0) as rec_yds_pg,
  sum(passing_tds + rushing_tds + receiving_tds) / nullif(count(distinct week),0) as tds_pg
from player_game_stats
where position in ('QB','RB','WR','TE')
group by opponent_team, season, season_type, position;

-- ---------------------------------------------------------------
-- Row level security: public read of stats, service-role write,
-- users own their lineups.
-- ---------------------------------------------------------------
alter table teams enable row level security;
alter table games enable row level security;
alter table player_game_stats enable row level security;
alter table team_game_stats enable row level security;
alter table slates enable row level security;
alter table slate_salaries enable row level security;
alter table model_params enable row level security;
alter table sim_summaries enable row level security;
alter table lineups enable row level security;

do $$
declare t text;
begin
  foreach t in array array['teams','games','player_game_stats','team_game_stats',
                           'slates','slate_salaries','model_params','sim_summaries']
  loop
    execute format('drop policy if exists "%s public read" on %I', t, t);
    execute format('create policy "%s public read" on %I for select using (true)', t, t);
  end loop;
end $$;

drop policy if exists "lineups owner all" on lineups;
create policy "lineups owner all" on lineups
  for all using (auth.uid() = user_id) with check (auth.uid() = user_id);
