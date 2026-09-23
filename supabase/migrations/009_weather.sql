-- gridiron-sim: kickoff weather forecasts (jobs/weather.py, Open-Meteo). Run after 008.
alter table games add column if not exists forecast_wind   numeric;   -- mph, mean sustained over the game window
alter table games add column if not exists forecast_gust   numeric;   -- mph, max gust
alter table games add column if not exists forecast_temp   numeric;   -- F at kickoff
alter table games add column if not exists forecast_precip numeric;   -- inches over the game
alter table games add column if not exists forecast_pop    numeric;   -- max precipitation probability %
alter table games add column if not exists forecast_at     timestamptz;
