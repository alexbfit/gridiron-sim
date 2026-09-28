-- 015: Sunday refresh times follow the inactives (Sleeper lagged the posted inactives by ~30 min on 9/27).
--   * 12:05 PM ET: new refresh so the board/sim reflect 1 PM inactives before the 1 PM lock.
--   * 3:05 PM -> 3:20 PM ET: late-game inactives post ~2:35 / 2:55; the 3:50 swap reads this sim.
-- Needs 014 (gh_dispatch). Run once in the Supabase SQL editor.
select cron.unschedule(jobname) from cron.job where jobname in ('gtw_sun1505_edt', 'gtw_sun1505_est');
select cron.schedule('gtw_sun1205_edt', '5 16 * * 0',  $$select public.gh_dispatch('gameday-refresh.yml', 12, 5, '{"props":"false"}')$$);
select cron.schedule('gtw_sun1205_est', '5 17 * * 0',  $$select public.gh_dispatch('gameday-refresh.yml', 12, 5, '{"props":"false"}')$$);
select cron.schedule('gtw_sun1520_edt', '20 19 * * 0', $$select public.gh_dispatch('gameday-refresh.yml', 15, 20, '{"props":"false"}')$$);
select cron.schedule('gtw_sun1520_est', '20 20 * * 0', $$select public.gh_dispatch('gameday-refresh.yml', 15, 20, '{"props":"false"}')$$);
select jobname, schedule from cron.job where jobname like 'gtw_%' order by jobname;
