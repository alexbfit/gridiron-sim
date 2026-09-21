-- 1) save_lineups: kickoff check that ignores stray TNF/MNF games (008, merged with the complete-slate guard)
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

-- 2) week-2 main slate: only the 13 Sunday-afternoon games that were actually on the DK slate
update slates set game_ids = (
  select jsonb_agg(distinct ss.game_id order by ss.game_id)
  from slate_salaries ss
  where ss.slate_id = slates.slate_id and ss.game_id is not null and ss.game_id not in ('2026_02_DET_BUF','2026_02_NYG_LA','2026_02_IND_KC'))
where slate_key = 'DK-2026-02-main';

-- 3) record Sunday's lineups (built 11:00 AM ET before the 1 PM kickoff; the RPC refused them because of the TNF bug)
delete from slate_lineups where slate_id = (select slate_id from slates where slate_key='DK-2026-02-main') and source='claude';
insert into slate_lineups (slate_id, source, contest, idx, player_ids, salary, proj, own, p10, p50, p90, note, created_at)
select (select slate_id from slates where slate_key='DK-2026-02-main'), 'claude', v.contest, v.idx, v.ids, v.salary, v.proj, v.own, v.p10, v.p50, v.p90,
       'Sun 11:00 AM ET build (pre-kickoff), recorded by hand after the save bug: JSN 15.0 (Lock starts), Jefferson 14.5 (Wentz starts)', '2026-09-20T15:05:00Z'
from (values
('cash',1,array['44132573','44132656','44132658','44132660','44133012','44133102','44133112','44133498','44133744'],48800,147.7,292,113.3,147.7,184.0),
('gpp',1,array['44132573','44132656','44132658','44132660','44133010','44133102','44133112','44133456','44133747'],49900,145.2,279,109.7,145.1,182.7),
('gpp',2,array['44132584','44132656','44132658','44132660','44133082','44133102','44133112','44133442','44133747'],50000,145.7,286,112.7,145.8,181.8),
('gpp',3,array['44132573','44132656','44132658','44132660','44132996','44133102','44133112','44133452','44133763'],49900,144.1,236,110.9,144.5,180.6),
('gpp',4,array['44132579','44132656','44132658','44132660','44133012','44133066','44133112','44133484','44133742'],49200,144.0,234,110.6,143.8,180.2),
('gpp',5,array['44132584','44132656','44132658','44132660','44133012','44133102','44133112','44133450','44133744'],49600,144.2,251,109.7,143.9,179.7),
('gpp',6,array['44132568','44132656','44132660','44132714','44133012','44133102','44133112','44133442','44133748'],50000,143.7,241,109.9,144.0,179.4),
('gpp',7,array['44132582','44132656','44132658','44132660','44132992','44133016','44133112','44133498','44133747'],50000,143.4,301,107.6,142.7,179.3),
('gpp',8,array['44132583','44132656','44132658','44132660','44132970','44133102','44133112','44133484','44133747'],49900,143.7,278,110.8,143.2,179.1),
('gpp',9,array['44132573','44132656','44132660','44132682','44133012','44133102','44133112','44133452','44133744'],48500,141.9,235,108.5,141.2,179.0),
('gpp',10,array['44132571','44132656','44132658','44132660','44133012','44133102','44133112','44133466','44133742'],50000,144.1,238,110.1,145.1,178.9),
('gpp',11,array['44132573','44132658','44132674','44132682','44132986','44133012','44133102','44133452','44133747'],49800,131.3,128,99.8,130.0,165.6),
('gpp',12,array['44132573','44132658','44132674','44132714','44133010','44133024','44133102','44133442','44133747'],49900,131.1,136,100.0,130.1,164.8),
('gpp',13,array['44132572','44132674','44132682','44132714','44132964','44133012','44133082','44133442','44133766'],49900,126.6,67,98.4,128.7,161.1),
('gpp',14,array['44132572','44132674','44132682','44132700','44132996','44133012','44133066','44133442','44133745'],50000,126.3,62,96.7,124.9,158.2),
('gpp',15,array['44132572','44132674','44132682','44132690','44132966','44133012','44133066','44133450','44133747'],49600,125.5,96,94.6,122.8,156.8),
('gpp',16,array['44132575','44132666','44132674','44132714','44132966','44132986','44133012','44133498','44133745'],49900,123.7,88,93.3,121.9,156.7),
('gpp',17,array['44132579','44132680','44132682','44132714','44132962','44132966','44133066','44133484','44133744'],49800,120.3,62,94.2,123.0,156.5),
('gpp',18,array['44132595','44132680','44132682','44132714','44132964','44132994','44133066','44133442','44133747'],50000,120.7,95,94.0,122.6,154.5),
('gpp',19,array['44132595','44132666','44132682','44132714','44132964','44132966','44133066','44133450','44133748'],50000,119.5,59,91.8,120.4,152.4),
('gpp',20,array['44132576','44132682','44132690','44132704','44132986','44132994','44132996','44133450','44133742'],49800,117.3,37,85.7,115.1,151.2)
) as v(contest, idx, ids, salary, proj, own, p10, p50, p90);

select slate_key, jsonb_array_length(game_ids) games, (select count(*) from slate_lineups l where l.slate_id=s.slate_id) lineups from slates s where slate_key='DK-2026-02-main';
