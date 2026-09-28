-- 016: nothing in slate_projections or games is ever lost again.
-- Week 3 (9/27): a post-kickoff refresh deleted every sportsbook props row, and the 3:05 PM odds run overwrote the
-- pregame lines with in-game ones. Both jobs write by delete/upsert, so the old values were simply gone.
-- This adds append-only history tables filled by triggers: every UPDATE or DELETE on slate_projections keeps the OLD
-- row; every change to a game's lines keeps the OLD lines. Reads are public like the source tables; only the
-- service role writes (via the triggers). Restoring = one INSERT ... SELECT from the history table (examples below).
-- Bonus: games_history is a free timestamped odds history (every line move the refreshes saw), which is exactly
-- what the backtests needed and had to buy back from The Odds API.
-- Run once in the Supabase SQL editor.

create table if not exists slate_projections_history (
  hist_id        bigserial primary key,
  op             text not null,                 -- 'UPDATE' | 'DELETE'
  archived_at    timestamptz not null default now(),
  slate_id       uuid,
  site_player_id text,
  player_id      text,
  player_name    text,
  position       text,
  team           text,
  salary         int,
  method         text,
  mean           numeric,
  median         numeric,
  components     jsonb,
  updated_at     timestamptz
);
create index if not exists slate_projections_history_slate_idx on slate_projections_history (slate_id, method, archived_at);

create or replace function public.slate_projections_archive() returns trigger
language plpgsql security definer set search_path = public as $$
begin
  insert into slate_projections_history (op, slate_id, site_player_id, player_id, player_name, position, team, salary,
                                         method, mean, median, components, updated_at)
  values (tg_op, old.slate_id, old.site_player_id, old.player_id, old.player_name, old.position, old.team, old.salary,
          old.method, old.mean, old.median, old.components, old.updated_at);
  return old;
end $$;

drop trigger if exists slate_projections_archive on slate_projections;
create trigger slate_projections_archive
  after update or delete on slate_projections
  for each row execute function public.slate_projections_archive();

create table if not exists games_history (
  hist_id          bigserial primary key,
  archived_at      timestamptz not null default now(),
  game_id          text not null,
  season           int,
  week             int,
  spread_line      numeric,
  total_line       numeric,
  home_moneyline   int,
  away_moneyline   int,
  lines_source     text,
  lines_updated_at timestamptz
);
create index if not exists games_history_game_idx on games_history (game_id, archived_at);

create or replace function public.games_archive_lines() returns trigger
language plpgsql security definer set search_path = public as $$
begin
  -- only when a line actually changes (ingest re-upserts every game nightly)
  if old.spread_line is distinct from new.spread_line or old.total_line is distinct from new.total_line
     or old.home_moneyline is distinct from new.home_moneyline or old.away_moneyline is distinct from new.away_moneyline then
    insert into games_history (game_id, season, week, spread_line, total_line, home_moneyline, away_moneyline, lines_source, lines_updated_at)
    values (old.game_id, old.season, old.week, old.spread_line, old.total_line, old.home_moneyline, old.away_moneyline,
            old.lines_source, old.lines_updated_at);
  end if;
  return new;
end $$;

drop trigger if exists games_archive_lines on games;
create trigger games_archive_lines
  before update on games
  for each row execute function public.games_archive_lines();

alter table slate_projections_history enable row level security;
drop policy if exists "slate_projections_history public read" on slate_projections_history;
create policy "slate_projections_history public read" on slate_projections_history for select using (true);
alter table games_history enable row level security;
drop policy if exists "games_history public read" on games_history;
create policy "games_history public read" on games_history for select using (true);

-- ---------------------------------------------------------------- restore recipes (run by hand when needed)
-- 1) Put back the last deleted props rows of a slate (the newest archived copy per player):
--   insert into slate_projections (slate_id, site_player_id, player_id, player_name, position, team, salary, method, mean, median, components, updated_at)
--   select distinct on (site_player_id) slate_id, site_player_id, player_id, player_name, position, team, salary, method, mean, median, components, updated_at
--   from slate_projections_history
--   where slate_id = '<slate_id>' and method = 'props' and op = 'DELETE'
--   order by site_player_id, archived_at desc
--   on conflict do nothing;
-- 2) Put back a game's pregame lines (the last value stored before kickoff):
--   update games g set spread_line = h.spread_line, total_line = h.total_line, lines_source = h.lines_source, lines_updated_at = h.lines_updated_at
--   from (select distinct on (game_id) * from games_history where game_id = '<game_id>' and lines_updated_at < '<kickoff utc>' order by game_id, archived_at desc) h
--   where g.game_id = h.game_id;
