-- save_lineups: refuse only once the slate has actually kicked off.
-- "Started" = the first not-yet-played game on the slate has kicked off (ET, gameday + gametime).
-- Games earlier than that (a stray Thursday game that ended up in game_ids) are ignored, and a slate
-- with no unplayed games left is complete — no back-dating lineups after the fact.
-- Supersedes the function body in 007_lineups.sql.
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
  -- "started" = the first not-yet-played game on the slate has kicked off (ET). Games earlier than
  -- that (e.g. a stray Thursday game in game_ids) are ignored; a slate with no unplayed games is over.
  if not exists (select 1 from games g where g.game_id in (select jsonb_array_elements_text(coalesce(s.game_ids, '[]'::jsonb))) and g.home_score is null) then
    raise exception 'slate % is complete — lineups can only be saved before kickoff', p_slate_key;
  end if;
  if exists (
    select 1 from games g
    where g.game_id in (select jsonb_array_elements_text(coalesce(s.game_ids, '[]'::jsonb)))
      and g.gameday >= (select min(g2.gameday) from games g2
                        where g2.game_id in (select jsonb_array_elements_text(coalesce(s.game_ids, '[]'::jsonb))) and g2.home_score is null)
      and (g.home_score is not null
           or ((g.gameday::text || ' ' || coalesce(nullif(g.gametime, ''), '13:00'))::timestamp at time zone 'America/New_York') <= now())
  ) then
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
