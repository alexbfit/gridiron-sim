-- 017: a run log for every scheduled job — the Claude tasks (news sweeps, the 9:30 build, both late swaps, the
-- pre-lock checklists) and the GitHub workflows (nightly, gameday refreshes, slate pipeline, tests).
-- Why: the Saturday 9/26 news sweep failed and left nothing behind; GitHub skipped the 9/27 5 AM and 7 AM runs and
-- nobody could see it from the cloud (the GitHub API isn't reachable there). Now every run writes a 'started' row
-- and a finish row (ok / warn / failed / skipped) with a one-line summary, the pre-lock checklist reads them, and
-- gametimewin.com/status.html shows them.
-- Writes go through log_task_run() so the anon key (Claude tasks) and the service key (GitHub) both work, like
-- save_lineups / save_news. Reads are public (summaries hold no secrets).
-- Run once in the Supabase SQL editor.

create table if not exists task_runs (
  id           bigserial primary key,
  task         text not null,                          -- e.g. 'sunday-build', 'news-sat', 'gameday-refresh'
  run_id       text not null,                          -- start and finish of one run share it (GitHub run id, or the ET date)
  source       text not null default 'claude',         -- 'claude' | 'github' | 'manual'
  status       text not null,                          -- 'started' | 'ok' | 'warn' | 'failed' | 'skipped'
  summary      text,
  details      jsonb,
  started_at   timestamptz not null default now(),
  finished_at  timestamptz,
  unique (task, run_id)
);
create index if not exists task_runs_task_idx on task_runs (task, started_at desc);
create index if not exists task_runs_started_idx on task_runs (started_at desc);

alter table task_runs enable row level security;
drop policy if exists "task_runs public read" on task_runs;
create policy "task_runs public read" on task_runs for select using (true);

create or replace function log_task_run(p_task text, p_run_id text, p_status text,
                                        p_summary text default null, p_details jsonb default null, p_source text default 'claude')
returns bigint
language plpgsql
security definer
set search_path = public
as $$
declare
  rid bigint;
begin
  if p_task is null or p_task !~ '^[a-z0-9][a-z0-9-]{2,39}$' then raise exception 'bad task name %', p_task; end if;
  if p_run_id is null or length(p_run_id) not between 1 and 80 then raise exception 'bad run id'; end if;
  if p_status not in ('started', 'ok', 'warn', 'failed', 'skipped') then raise exception 'bad status %', p_status; end if;
  if coalesce(p_source, 'claude') not in ('claude', 'github', 'manual') then raise exception 'bad source %', p_source; end if;
  if p_details is not null and length(p_details::text) > 20000 then p_details := jsonb_build_object('truncated', true); end if;
  -- flood guard for the anon key: a task logs a handful of rows per day
  if (select count(*) from task_runs where task = p_task and started_at > now() - interval '1 hour') > 40 then
    raise exception 'too many runs logged for % in the last hour', p_task;
  end if;

  insert into task_runs (task, run_id, source, status, summary, details, started_at, finished_at)
  values (p_task, p_run_id, coalesce(p_source, 'claude'), p_status, left(p_summary, 500), p_details, now(),
          case when p_status = 'started' then null else now() end)
  on conflict (task, run_id) do update
    set status      = excluded.status,
        summary     = coalesce(excluded.summary, task_runs.summary),
        details     = coalesce(excluded.details, task_runs.details),
        finished_at = case when excluded.status = 'started' then task_runs.finished_at else now() end,
        started_at  = case when excluded.status = 'started' then now() else task_runs.started_at end
  returning id into rid;

  delete from task_runs where started_at < now() - interval '400 days';
  return rid;
end;
$$;

revoke all on function log_task_run(text, text, text, text, jsonb, text) from public;
grant execute on function log_task_run(text, text, text, text, jsonb, text) to anon, authenticated, service_role;
