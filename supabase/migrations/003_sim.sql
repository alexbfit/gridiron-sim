-- gridiron-sim: Phase 3 — Monte Carlo sim output
-- Run after 002_slates.sql.

-- one projection row per (slate, player, method); the board prefers 'sim' over 'baseline'
alter table slate_projections drop constraint if exists slate_projections_pkey;
alter table slate_projections add primary key (slate_id, site_player_id, method);

-- where the sim matrix lives + run metadata
alter table slates add column if not exists sim_meta jsonb;

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
left join lateral (
  select * from slate_projections sp
  where sp.slate_id = ss.slate_id and sp.site_player_id = ss.site_player_id
  order by case sp.method when 'sim' then 0 when 'baseline' then 1 else 2 end
  limit 1
) p on true;

-- public bucket for the compressed player x sim matrices (jobs/simulate.py uploads here)
insert into storage.buckets (id, name, public)
values ('sims', 'sims', true)
on conflict (id) do update set public = true;
