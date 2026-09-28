-- 014: run the GitHub refresh workflows on time from Supabase (pg_cron + pg_net).
-- GitHub's own cron has been hours late or skipped for this repo (9/24-9/27: 5 AM runs at 9:42-10:32 AM,
-- Sun 9/27 5 AM + 7 AM never ran). pg_cron fires on the minute and calls GitHub's workflow_dispatch API.
-- The GitHub schedules stay in the workflow files as a backup; the sportsbook-props step skips itself if
-- props already landed in the last 3 hours, so a duplicate run never pays twice.
--
-- ONE-TIME SETUP (Alex):
--   1. GitHub -> Settings -> Developer settings -> Fine-grained personal access tokens -> Generate new token:
--      repository access = only alexbfit/gridiron-sim, permission Actions = Read and write, expiry 1 year.
--   2. In the Supabase SQL editor run (paste the token between the quotes):
--        select vault.create_secret('github_pat_XXXX', 'github_pat');
--      (to replace it later: select vault.update_secret(id, 'new_token') from vault.secrets where name = 'github_pat';)
--   3. Run this whole file.
--   4. Test (fires a refresh now, props off):
--        select public.gh_dispatch_now('gameday-refresh.yml', '{"props":"false"}');
--        select status_code, content from net._http_response order by created desc limit 1;   -- 204 = success

create extension if not exists pg_cron;
create extension if not exists pg_net;

-- Fire now (used by the schedules below and for manual tests).
create or replace function public.gh_dispatch_now(workflow text, inputs jsonb default '{}'::jsonb)
returns bigint language plpgsql security definer set search_path = public, extensions as $$
declare tok text; req bigint;
begin
  select decrypted_secret into tok from vault.decrypted_secrets where name = 'github_pat' limit 1;
  if tok is null then raise warning 'gh_dispatch: vault secret github_pat is missing'; return null; end if;
  select net.http_post(
    url     := 'https://api.github.com/repos/alexbfit/gridiron-sim/actions/workflows/' || workflow || '/dispatches',
    headers := jsonb_build_object('Authorization', 'Bearer ' || tok, 'Accept', 'application/vnd.github+json',
                                  'X-GitHub-Api-Version', '2022-11-28', 'User-Agent', 'gametimewin-cron',
                                  'Content-Type', 'application/json'),
    body    := jsonb_build_object('ref', 'main', 'inputs', inputs)) into req;
  return req;
end $$;

-- Fire only if it is currently et_hour:et_minute (+-2 min) in New York. Every job is scheduled at both its
-- EDT and EST UTC time, so daylight-saving changes need no edits: only the matching one fires.
create or replace function public.gh_dispatch(workflow text, et_hour int, et_minute int, inputs jsonb default '{}'::jsonb)
returns void language plpgsql security definer set search_path = public, extensions as $$
declare t timestamp := now() at time zone 'America/New_York';
begin
  if extract(hour from t)::int <> et_hour or abs(extract(minute from t)::int - et_minute) > 2 then return; end if;
  perform public.gh_dispatch_now(workflow, inputs);
end $$;

revoke all on function public.gh_dispatch_now(text, jsonb) from public, anon, authenticated;
revoke all on function public.gh_dispatch(text, int, int, jsonb) from public, anon, authenticated;

-- (re)create the schedules
select cron.unschedule(jobname) from cron.job where jobname like 'gtw_%';

-- nightly-ingest: 5:00 AM ET daily
select cron.schedule('gtw_nightly_edt', '0 9 * * *',  $$select public.gh_dispatch('nightly-ingest.yml', 5, 0)$$);
select cron.schedule('gtw_nightly_est', '0 10 * * *', $$select public.gh_dispatch('nightly-ingest.yml', 5, 0)$$);
-- gameday-refresh: Thu + Sat 6:30 PM ET
select cron.schedule('gtw_thu_edt', '30 22 * * 4', $$select public.gh_dispatch('gameday-refresh.yml', 18, 30, '{"props":"false"}')$$);
select cron.schedule('gtw_thu_est', '30 23 * * 4', $$select public.gh_dispatch('gameday-refresh.yml', 18, 30, '{"props":"false"}')$$);
select cron.schedule('gtw_sat_edt', '30 22 * * 6', $$select public.gh_dispatch('gameday-refresh.yml', 18, 30, '{"props":"false"}')$$);
select cron.schedule('gtw_sat_est', '30 23 * * 6', $$select public.gh_dispatch('gameday-refresh.yml', 18, 30, '{"props":"false"}')$$);
-- Sunday 7:00 AM ET: refresh + sportsbook player props (the 9:30 build uses them)
select cron.schedule('gtw_sun0700_edt', '0 11 * * 0', $$select public.gh_dispatch('gameday-refresh.yml', 7, 0, '{"props":"true"}')$$);
select cron.schedule('gtw_sun0700_est', '0 12 * * 0', $$select public.gh_dispatch('gameday-refresh.yml', 7, 0, '{"props":"true"}')$$);
-- Sunday 10:00 AM ET
select cron.schedule('gtw_sun1000_edt', '0 14 * * 0', $$select public.gh_dispatch('gameday-refresh.yml', 10, 0, '{"props":"false"}')$$);
select cron.schedule('gtw_sun1000_est', '0 15 * * 0', $$select public.gh_dispatch('gameday-refresh.yml', 10, 0, '{"props":"false"}')$$);
-- Sunday 11:36 AM ET: right after the 1 PM inactives (11:30); the late swap runs at 11:48
select cron.schedule('gtw_sun1136_edt', '36 15 * * 0', $$select public.gh_dispatch('gameday-refresh.yml', 11, 36, '{"props":"false"}')$$);
select cron.schedule('gtw_sun1136_est', '36 16 * * 0', $$select public.gh_dispatch('gameday-refresh.yml', 11, 36, '{"props":"false"}')$$);
-- Sunday 3:05 PM ET: after the 4 PM inactives (2:35 / 2:55); the full late swap runs at 3:50
select cron.schedule('gtw_sun1505_edt', '5 19 * * 0', $$select public.gh_dispatch('gameday-refresh.yml', 15, 5, '{"props":"false"}')$$);
select cron.schedule('gtw_sun1505_est', '5 20 * * 0', $$select public.gh_dispatch('gameday-refresh.yml', 15, 5, '{"props":"false"}')$$);

select jobname, schedule from cron.job where jobname like 'gtw_%' order by jobname;
