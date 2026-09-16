-- gridiron-sim: Phase 6 — snap counts + official injury reports
-- Run after 003_sim.sql. Then Actions → nightly-ingest → Run workflow with seasons "2024 2025 2026".

create table if not exists player_snaps (
  player_id     text not null,
  season        int  not null,
  week          int  not null,
  season_type   text not null default 'REG',
  team          text,
  position      text,
  offense_snaps int,
  offense_pct   numeric,
  updated_at    timestamptz default now(),
  primary key (player_id, season, week, season_type)
);
create index if not exists player_snaps_season_week_idx on player_snaps (season, week);

create table if not exists injury_reports (
  player_id        text not null,
  season           int  not null,
  week             int  not null,
  season_type      text not null default 'REG',
  team             text,
  position         text,
  player_name      text,
  report_status    text,      -- Out / Doubtful / Questionable / null
  practice_status  text,
  primary_injury   text,
  updated_at       timestamptz default now(),
  primary key (player_id, season, week, season_type)
);
create index if not exists injury_reports_season_week_idx on injury_reports (season, week);

alter table player_snaps enable row level security;
alter table injury_reports enable row level security;
drop policy if exists "player_snaps public read" on player_snaps;
create policy "player_snaps public read" on player_snaps for select using (true);
drop policy if exists "injury_reports public read" on injury_reports;
create policy "injury_reports public read" on injury_reports for select using (true);

-- board: surface the official report next to the DK/FD status
create or replace view slate_board as
select
  s.slate_id, s.slate_key, s.site, s.season, s.week, s.slate_name,
  ss.site_player_id, ss.player_id, ss.player_name, ss.position, ss.roster_positions,
  ss.team, ss.opponent, ss.game_id, ss.salary, ss.status, ss.avg_points, ss.game_info, ss.matched_by,
  ir.report_status as injury_report, ir.practice_status, ir.primary_injury,
  p.method, p.mean, p.stdev, p.median, p.p15, p.p85, p.p95, p.floor, p.ceiling,
  p.boom_prob, p.bust_prob, p.games_used, p.components,
  case when ss.salary > 0 then round(p.mean / (ss.salary / 1000.0), 2) end as value
from slates s
join slate_salaries ss on ss.slate_id = s.slate_id
left join injury_reports ir on ir.player_id = ss.player_id and ir.season = s.season and ir.week = s.week and ir.season_type = 'REG'
left join lateral (
  select * from slate_projections sp
  where sp.slate_id = ss.slate_id and sp.site_player_id = ss.site_player_id
  order by case sp.method when 'sim' then 0 when 'baseline' then 1 else 2 end
  limit 1
) p on true;
