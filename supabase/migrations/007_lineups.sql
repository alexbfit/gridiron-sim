-- gridiron-sim: Phase 9 — lineup tracking (Claude's Sunday lineups + web exports), scored on Monday
-- Run after 006_depth_odds.sql.

create table if not exists slate_lineups (
  slate_id     uuid references slates(slate_id) on delete cascade,
  source       text not null,            -- 'claude' (Sunday task) | 'web' (exported from the builder)
  contest      text not null,            -- 'cash' | 'gpp'
  idx          int  not null,
  player_ids   text[] not null,          -- 9 site_player_ids
  salary       int,
  proj         numeric,
  own          numeric,                  -- summed projected ownership
  p10          numeric,                  -- lineup's own simulated distribution at save time
  p50          numeric,
  p90          numeric,
  note         text,
  created_at   timestamptz default now(),
  actual       numeric,                  -- filled by jobs/results.py after the games
  sim_pct      numeric,                  -- where the actual landed in the lineup's sim distribution (0..1)
  contest_pct  numeric,                  -- share of real contest entries this total would have beaten (0..1)
  scored_at    timestamptz,
  primary key (slate_id, source, contest, idx)
);

-- contest score distribution from an imported standings file (jobs/import_ownership.py)
-- {name, entries, quantiles: [101 numbers], top, min_cash}
alter table slates add column if not exists contest_meta jsonb;

alter table slate_lineups enable row level security;
drop policy if exists "slate_lineups public read" on slate_lineups;
create policy "slate_lineups public read" on slate_lineups for select using (true);

-- Anon-callable save. Validates the payload (real slate, 9 known players, under the cap, slate not
-- started) so lineups can only be recorded BEFORE kickoff — no back-dating.
create or replace function save_lineups(p_slate_key text, p_source text, p_contest text, p_lineups jsonb, p_replace boolean default true)
returns int
language plpgsql
security definer
set search_path = public
as $$
declare
  s        slates%rowtype;
  cap      int;
  L        jsonb;
  ids      text[];
  n_ok     int;
  next_idx int;
  written  int := 0;
begin
  if p_source not in ('claude', 'web') then raise exception 'bad source %', p_source; end if;
  if p_contest not in ('cash', 'gpp') then raise exception 'bad contest %', p_contest; end if;
  if jsonb_typeof(p_lineups) <> 'array' or jsonb_array_length(p_lineups) > 150 then raise exception 'lineups must be an array of <= 150'; end if;
  select * into s from slates where slate_key = p_slate_key;
  if not found then raise exception 'slate % not found', p_slate_key; end if;
  if exists (select 1 from games g where g.game_id in (select jsonb_array_elements_text(coalesce(s.game_ids, '[]'::jsonb)))
             and (g.home_score is not null or g.gameday < current_date)) then
    raise exception 'slate % has started — lineups can only be saved before kickoff', p_slate_key;
  end if;
  cap := case when s.site = 'FD' then 60000 else 50000 end;

  if p_replace then
    delete from slate_lineups where slate_id = s.slate_id and source = p_source and contest = p_contest;
    next_idx := 1;
  else
    select coalesce(max(idx), 0) + 1 into next_idx from slate_lineups where slate_id = s.slate_id and source = p_source and contest = p_contest;
  end if;

  for L in select * from jsonb_array_elements(p_lineups) loop
    select array_agg(x) into ids from jsonb_array_elements_text(L->'ids') x;
    if ids is null or array_length(ids, 1) <> 9 then raise exception 'each lineup needs 9 ids'; end if;
    select count(*) into n_ok from slate_salaries ss where ss.slate_id = s.slate_id and ss.site_player_id = any(ids);
    if n_ok <> 9 then raise exception 'lineup has players not on slate %', p_slate_key; end if;
    if (select sum(salary) from slate_salaries ss where ss.slate_id = s.slate_id and ss.site_player_id = any(ids)) > cap then
      raise exception 'lineup over the salary cap';
    end if;
    insert into slate_lineups (slate_id, source, contest, idx, player_ids, salary, proj, own, p10, p50, p90, note)
    values (s.slate_id, p_source, p_contest, next_idx, ids,
            (select sum(salary) from slate_salaries ss where ss.slate_id = s.slate_id and ss.site_player_id = any(ids)),
            (L->>'proj')::numeric, (L->>'own')::numeric, (L->>'p10')::numeric, (L->>'p50')::numeric, (L->>'p90')::numeric,
            left(L->>'note', 500));
    next_idx := next_idx + 1;
    written := written + 1;
  end loop;
  return written;
end;
$$;

revoke all on function save_lineups(text, text, text, jsonb, boolean) from public;
grant execute on function save_lineups(text, text, text, jsonb, boolean) to anon, authenticated, service_role;
