# Historical sportsbook player props (The Odds API, pulled 9/28/2026)

All snapshots are **Sunday 7:00 AM ET** (pre-game, honest for backtests) unless the name says otherwise,
for every DraftKings **main-slate** game of that week (matched to the week's DKSalaries file).
Columns: week, game_id, commence_time, home_team, away_team, bookmaker, market, label (Over/Under/Yes/team), player, price (American), point.

| file | season / weeks | markets |
|---|---|---|
| props_2024_7am_full.csv.gz | 2024 wk 1–18 (199 games) | core + extra (below) |
| props_2025_7am_core.csv.gz | 2025 wk 1–18 (198 games) | core |
| props_2025_7am_extra.csv.gz | 2025 wk 1–18 (198 games) | extra |
| props_2025_1245pm_core_4wks.csv.gz | 2025, 4 weeks, 12:45 PM ET | core (line-movement check) |
| props_2026_7am_full.csv.gz | 2026 wk 1–3 | core + extra |

- **core:** player_pass_yds, player_pass_tds, player_rush_yds, player_reception_yds, player_receptions, player_anytime_td
- **extra:** player_pass_attempts, player_pass_completions, player_pass_interceptions, player_rush_attempts,
  alternates (player_pass_yds / pass_tds / rush_yds / reception_yds / receptions _alternate — each player's line ladder, i.e. the books' ceiling view), spreads, totals

Books: DraftKings, FanDuel, BetMGM, Caesars, Fanatics, BetRivers, Bovada, BetOnline and a few small ones.
Do NOT use the old nickpasternak 2024 props file for backtests — it contains in-game lines (round 15).
Raw JSON (one file per game) is zipped in `OneDrive\Desktop\DFS DATA\_props_archive\props_raw_json.zip`.
Re-pull / extend: `ODDS_API_KEY=... python pull_props_history.py <season> <weeks>` (historical event odds: 10 credits per market per game).
