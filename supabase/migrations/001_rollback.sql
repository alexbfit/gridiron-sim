-- Undo 001_schema.sql. Drops everything the migration created.
-- Only run this on a project where 001_schema.sql was applied by mistake.
-- CAUTION: if the project already had tables with these names, they were
-- skipped by "create table if not exists" and would be dropped here.

drop view if exists defense_vs_position;
drop view if exists player_season_stats;

drop table if exists lineups cascade;
drop table if exists sim_summaries cascade;
drop table if exists slate_salaries cascade;
drop table if exists slates cascade;
drop table if exists model_params cascade;
drop table if exists team_game_stats cascade;
drop table if exists player_game_stats cascade;
drop table if exists games cascade;
drop table if exists teams cascade;

-- pgcrypto is left in place; Supabase ships with it enabled anyway.
