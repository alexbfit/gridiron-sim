-- gridiron-sim: team news readers + upset picks (run after 009).
-- news_notes: structured, sourced role/injury notes from the per-game news agents (Sat evening + Sun morning),
--             applied by the Sunday lineup task as overrides and graded after the games.
-- upset_picks: the agents' win probabilities per game, logged next to the market's and graded (for fun / to test).

create table if not exists news_notes (
  id             bigserial primary key,
  slate_id       uuid references slates(slate_id) on delete cascade,
  run            text not null,                 -- 'sat' | 'sun' | 'manual'
  game_id        text,
  team           text,
  player_name    text not null,
  site_player_id text,                          -- matched to slate_salaries by name (+team) when possible
  position       text,
  change         text not null,                 -- what the reporting says, one line
  adjustment     text not null,                 -- 'exclude' | 'scale' | 'set' | 'note'
  value          numeric,                       -- scale: factor (0.6); set: projection in DK pts
  confidence     numeric,                       -- 0..1 as judged by the agent
  source_name    text,
  source_url     text,
  proj_at_note   numeric,                       -- sim mean when the note was recorded
  actual         numeric,                       -- filled after the games
  proj_err       numeric,                       -- |proj_at_note - actual|
  adj_err        numeric,                       -- |adjusted projection - actual|
  helped         boolean,                       -- adj_err < proj_err
  graded_at      timestamptz,
  created_at     timestamptz default now()
);
create index if not exists news_notes_slate_idx on news_notes (slate_id, run);

create table if not exists upset_picks (
  id               bigserial primary key,
  slate_id         uuid references slates(slate_id) on delete cascade,
  game_id          text not null,
  run              text not null,
  home_team        text,
  away_team        text,
  spread_line      numeric,                     -- nflverse convention: > 0 = home favoured
  market_home_prob numeric,                     -- from the spread at save time
  agent_home_prob  numeric not null,
  pick             text,                        -- team the agent picks to win
  upset            boolean,                     -- agent's pick is the market underdog
  reasoning        text,
  winner           text,
  correct          boolean,
  brier_agent      numeric,
  brier_market     numeric,
  graded_at        timestamptz,
  created_at       timestamptz default now(),
  unique (game_id, run)
);

alter table news_notes enable row level security;
alter table upset_picks enable row level security;
drop policy if exists "news_notes public read" on news_notes;
create policy "news_notes public read" on news_notes for select using (true);
drop policy if exists "upset_picks public read" on upset_picks;
create policy "upset_picks public read" on upset_picks for select using (true);

-- Anon-callable save, like save_lineups: only for a slate that has not finished, replaces the (slate, run) set.
-- Notes are matched to slate players by name (then name+team); proj_at_note is the current sim mean.
create or replace function save_news(p_slate_key text, p_run text, p_notes jsonb, p_picks jsonb)
returns jsonb
language plpgsql
security definer
set search_path = public
as $$
declare
  s        slates%rowtype;
  n        jsonb;
  p        jsonb;
  g        games%rowtype;
  sp       record;
  n_notes  int := 0;
  n_picks  int := 0;
  n_match  int := 0;
  mprob    numeric;
  aprob    numeric;
  pick_t   text;
begin
  if p_run not in ('sat', 'sun', 'manual') then raise exception 'bad run %', p_run; end if;
  select * into s from slates where slate_key = p_slate_key;
  if not found then raise exception 'slate % not found', p_slate_key; end if;
  if not exists (select 1 from games x where x.game_id in (select jsonb_array_elements_text(coalesce(s.game_ids, '[]'::jsonb))) and x.home_score is null) then
    raise exception 'slate % is complete', p_slate_key;
  end if;
  if jsonb_typeof(coalesce(p_notes, '[]'::jsonb)) <> 'array' or jsonb_array_length(coalesce(p_notes, '[]'::jsonb)) > 400 then raise exception 'notes must be an array of <= 400'; end if;
  if jsonb_typeof(coalesce(p_picks, '[]'::jsonb)) <> 'array' or jsonb_array_length(coalesce(p_picks, '[]'::jsonb)) > 32 then raise exception 'picks must be an array of <= 32'; end if;

  delete from news_notes where slate_id = s.slate_id and run = p_run;
  for n in select * from jsonb_array_elements(coalesce(p_notes, '[]'::jsonb)) loop
    if coalesce(n->>'player', '') = '' or coalesce(n->>'change', '') = '' then continue; end if;
    if coalesce(n->>'adjustment', 'note') not in ('exclude', 'scale', 'set', 'note') then continue; end if;
    sp := null;
    select ss.site_player_id, ss.position, ss.team, b.mean into sp
      from slate_salaries ss left join slate_board b on b.slate_id = ss.slate_id and b.site_player_id = ss.site_player_id
      where ss.slate_id = s.slate_id
        and lower(regexp_replace(ss.player_name, '[^A-Za-z0-9 ]', '', 'g')) = lower(regexp_replace(n->>'player', '[^A-Za-z0-9 ]', '', 'g'))
        and (coalesce(n->>'team', '') = '' or ss.team = upper(n->>'team'))
      order by (ss.team = upper(coalesce(n->>'team', ''))) desc limit 1;
    if sp.site_player_id is not null then n_match := n_match + 1; end if;
    insert into news_notes (slate_id, run, game_id, team, player_name, site_player_id, position, change, adjustment, value, confidence,
                            source_name, source_url, proj_at_note)
    values (s.slate_id, p_run, n->>'game_id', coalesce(upper(n->>'team'), sp.team), left(n->>'player', 80), sp.site_player_id, sp.position,
            left(n->>'change', 300), coalesce(n->>'adjustment', 'note'), (n->>'value')::numeric,
            least(1, greatest(0, coalesce((n->>'confidence')::numeric, 0.5))), left(n->>'source_name', 80), left(n->>'source_url', 400), sp.mean);
    n_notes := n_notes + 1;
  end loop;

  delete from upset_picks where slate_id = s.slate_id and run = p_run;
  for p in select * from jsonb_array_elements(coalesce(p_picks, '[]'::jsonb)) loop
    select * into g from games where game_id = p->>'game_id';
    if not found or g.home_score is not null then continue; end if;            -- unknown or already started
    aprob := least(0.99, greatest(0.01, (p->>'home_prob')::numeric));
    -- market: logistic approximation of Phi(spread / 13.5); spread_line > 0 = home favoured
    mprob := case when g.spread_line is null then null else 1 / (1 + exp(-0.145 * g.spread_line)) end;
    pick_t := case when aprob >= 0.5 then g.home_team else g.away_team end;
    insert into upset_picks (slate_id, game_id, run, home_team, away_team, spread_line, market_home_prob, agent_home_prob, pick, upset, reasoning)
    values (s.slate_id, g.game_id, p_run, g.home_team, g.away_team, g.spread_line, mprob, aprob, pick_t,
            case when mprob is null then null else (aprob >= 0.5) <> (mprob >= 0.5) end, left(p->>'reasoning', 300));
    n_picks := n_picks + 1;
  end loop;
  return jsonb_build_object('notes', n_notes, 'matched', n_match, 'picks', n_picks);
end;
$$;

revoke all on function save_news(text, text, jsonb, jsonb) from public;
grant execute on function save_news(text, text, jsonb, jsonb) to anon, authenticated, service_role;
