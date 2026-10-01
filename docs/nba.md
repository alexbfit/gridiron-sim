# NBA live pipeline

Daily NBA DraftKings lineups from player props, on top of the sport-generic engine (`jobs/sports/`: DK rules, correlated
Monte Carlo, MIP optimizer). Nothing here touches the NFL engine.

```
DraftKings lobby ──► jobs/sports/fetch_dk.py ──► data/sports/nba/<date>/DKSalaries_NBA_<date>.csv
The Odds API + Kalshi ──► jobs/sports/props_nba.py ──► projections.csv (+ projections_info.json)
projections.csv ──► jobs/build_lineups_sport.py --sport NBA ──► lineups.csv (DK upload), lineups.txt, README.md
                                                                └─► Supabase sport_projections (source 'props_nba', optional)
```

One command runs all of it: `python jobs/sports/nba_daily.py [--date YYYY-MM-DD]`. The GitHub workflow `nba-daily`
(`.github/workflows/nba-daily.yml`) runs it on demand (date input) and commits `data/sports/nba/<date>/`; its 3 PM ET
cron is written in but commented out until tip-off.

## The pieces

### 1. Salaries — `jobs/fetch_dk_salaries.py --sport NBA` (= `jobs/sports/fetch_dk.py`)
DraftKings' public lobby JSON (`lobby/getcontests?sport=NBA`) lists every draft group. The NBA main slate is the
**Classic** group (contest type discovered from the lobby's `GameTypes`; DK's NBA Classic id has been **70** for years and
is the fallback) that starts on the requested date in the evening ET, has **no start-time suffix** (DK labels the others
"(Early Only)", "(Late Night)", "(Turbo)", "(DAL @ LAL)"), and the most games; ties go to the "Featured" tag. The CSV is
the lobby's own "Export to CSV" link. `--list` prints every group (useful when the lobby looks odd), `--group <id>` forces one.

Lobby on 2026-10-01 (preseason): three groups — two WNBA Showdown Captain Mode games (type 81) and the NBA Best Ball
sit-and-go (type 170) — i.e. **no Classic slate yet**; the fetcher reports that and exits 2.

### 2. Projections — `jobs/sports/props_nba.py`
Sources:
- **The Odds API** (`ODDS_API_KEY`), sport `basketball_nba`, markets `player_points, player_rebounds, player_assists,
  player_threes, player_blocks, player_steals, player_turnovers, player_points_rebounds_assists`. One free `/events` call
  plus one `/events/<id>/odds` call per slate game; **cost = 8 credits per game** (markets × 1 region). A 10-game slate is
  ~80 credits, a month of slates ~2,000. Without the key the step is skipped (Kalshi only).
- **Kalshi** (free, no key): player ladders ("Jalen Brunson: 25+ points") in series **KXNBAPTS, KXNBAREB, KXNBAAST,
  KXNBA3PT, KXNBASTL, KXNBABLK, KXNBAPRA**; **KXNBA2D / KXNBA3D** (double/triple-double yes-no) are read for a sanity
  check of the simulated DD probability. Also on Kalshi but unused: KXNBAPA, KXNBAPR, KXNBARA, KXNBASTOCK(S), KXNBAFTM,
  KXNBAPTSLEADER, KXNBAFIRSTBASKET. No turnover series exists. Ladder math is `jobs/kalshi.py`'s (survival-curve median,
  `E[X] = Σ P(X ≥ k)` for complete integer ladders, Poisson fit otherwise). All series were empty on 2026-10-01
  (markets are posted the morning of each game day).

Line → mean, per stat: the sportsbook point is a **median**; the Over/Under prices shift it
(`mean0 = point + sd · z(p_over)`, `p_over` de-vigged and averaged over books), then a per-stat **skew factor** converts
median to mean (pts +1.5 %, reb +3 %, ast +4 %, 3PM +8 %, stl/blk +12 %, TO +8 % — negative-binomial medians sit below
their means by about these fractions at NBA volumes). Low-count stats quoted at k.5 (3PM, stl, blk, TO) use the
price-implied Poisson mean instead, as the NFL engine does for pass TDs. Where both sources exist their means are averaged.

Per-player Monte Carlo (20,000 draws): each stat ~ NegativeBinomial(mean, var = DISP·mean) with DISP pts 2.2, reb 1.8,
ast 1.7, 3PM 1.35, stl 1.15, blk 1.2, TO 1.25; all stats share a latent minutes/usage factor through a Gaussian copula
(RHO pts .55, reb .40, ast .40, 3PM .45, stl .30, blk .30, TO .50). DK scoring per draw: pts + 0.5·3PM + 1.25·reb +
1.5·ast + 2·stl + 2·blk − 0.5·TO + 1.5·[double-double] + 3·[triple-double] (10+ in ≥ 2 / ≥ 3 of pts, reb, ast, stl, blk).
`proj` is the mean of the draws, `p25…p99` their percentiles, `p_dd`/`p_td` the bonus probabilities — so a 28/8/8 player
projects 51.1 (P(DD) .50, P(TD) .12), a 26/12.5/9.5 big 61.7 (P(DD) .83, P(TD) .35).

Coverage: a player needs a **points** line; missing secondary markets are filled from position ratios to points
(`source = props_partial`). Players with no points line are left out (never rostered) unless `--fallback-proj` supplies a
(name, proj) CSV for them — those rows carry `proj` only and `shapes.json` gives their curve in the builder. Names match
the DK file via `common.norm_name`, then first-initial + last-name; output rows use DK's exact Name.

Output columns: `name, team, opp, pos, salary, proj, p25, p50, p75, p85, p95, p99, p_dd, p_td, pts, reb, ast, fg3, stl,
blk, tov, n_markets, source` — the first twelve are what `build_lineups_sport.py --proj` reads.

Offline: `--props-json` takes `{"outcomes": [[player, market, label, price, point, book], ...]}` or a saved Odds API
payload (tests use `tests/fixtures/nba/`).

### 3. Lineups — `jobs/build_lineups_sport.py --sport NBA`
Defaults in `nba_daily.py`, from the multisport backtest (`data/sports_backtest/SUMMARY.txt`, 348 NBA slates replayed in
the real DK fields): for 20-max contests the **p98** ranking (+71 % ROI) beat the 3-player game stack (+46 %) and base
p90 (+29 %), and p98 was the only mode above water in the flagship (+2.6 %). So: `--rank p98 --stack 3 --max-exp 0.35
--min-uniq 3 --n 20 --sims 5000`. `--rank p90` reproduces the "base" mode. Locks/excludes pass through (`--lock`, `--exclude`).

### 4. Supabase
`supabase/pending/018_sport_slates.sql` creates `sport_projections (sport, slate_date, player_name, team, opp, proj,
p25…p99, source, updated_at)` with anon read. `nba_daily.py` upserts the day's rows as source `props_nba` only when
`SUPABASE_SERVICE_ROLE_KEY` is set and the table exists; otherwise it says "skipped" and moves on.

## Going live (2026-27 season tips off ~Oct 21, 2026)
1. Apply `supabase/pending/018_sport_slates.sql` in the SQL editor (optional — only for the site to read projections).
2. `ODDS_API_KEY` is already a repo secret (NFL props). Budget: **8 credits per game per run**, so one run a day on a
   10-game slate ≈ 80 credits ≈ 2,000–2,400 per month (plus the NFL Sunday pulls) — fine on the 20K/month plan, far
   above the 500/month free tier. To spend less: `--markets player_points,player_rebounds,player_assists` (3 credits per
   game, the rest filled from position ratios), `--max-games N`, or `--no-odds-api` (Kalshi only, free, thinner —
   Kalshi tends to list the top ~6 players per team).
3. Run `nba-daily` by hand from the Actions tab for the first slates (date input blank = today). Check the job log for
   "N projected players (full / partial / fallback)" and the Kalshi double-double check line.
4. Uncomment the `schedule:` block in `.github/workflows/nba-daily.yml` (3 PM ET; shift the UTC hour when DST ends).
   Props are fullest 2–4 hours before tip; a second manual run after the injury reports (~6 PM ET) is worth it.

## First live week — what to validate
- **Coverage**: every DK starter (salary ≥ $5,000) should have a `props_nba` row, not `props_partial`; the unmatched-names
  count in the log should be ~0 (fix `NAME_TO_ABBR`/aliases if a team or a name slips).
- **Projection accuracy vs SaberSim** (the yardstick: `DFS DATA/_sabersim_allsports/NBA/` on Alex's PC has 348 slates with
  SaberSim projections and actuals). Copy the week's `data/sports/nba/<date>/projections.csv` next to the archive and run,
  on the PC:
  ```
  # 1) our projections swapped into the archive slates, graded in the REAL contest fields
  python jobs/sports/backtest.py --sport NBA --proj-dir data/sports/nba --dates 2026-10-21-2026-10-28 --out bt/NBA_props.jsonl
  # 2) SaberSim's own projections on the same dates
  python jobs/sports/backtest.py --sport NBA --dates 2026-10-21-2026-10-28 --out bt/NBA_ss.jsonl
  python jobs/sports/backtest.py --summary bt/NBA_props.jsonl bt/NBA_ss.jsonl
  ```
  `--proj-dir` (new) looks for `<dir>/<date>/projections.csv` (or `<dir>/NBA_<date>.csv`), replaces SaberSim's proj and
  percentiles by player name, zeroes everyone we did not project, and otherwise grades identically — so the two summaries
  compare lineup ROI / top-1 % hit rate head to head. The archive's `dk_actual` column also gives the direct check:
  Pearson r and MAE of `proj` vs actual for players who played, by salary tier, for both `ssProj` and our `proj` on the same
  rows. The target is to land within ~.03 of SaberSim's r; if we trail by more, refit the SKEW / DISP constants against the
  archive's actuals per stat (a few weeks of live props plus the archive's box scores are enough).
- **Calibration**: fraction of players beating their `p50` should be ~50 %, beating `p85` ~15 %, beating `p99` ~1 %.
  Too few above p85/p99 → raise RHO (shared minutes factor); too many → lower DISP.
- **Double-doubles**: the log's `mean |P(sim) − P(kalshi)|` for KXNBA2D should sit under ~.08.
- **Lineups**: `lineups.txt` means should sit around 280–300 DK points on a full slate; 20 unique lineups, max exposure 7/20.

## Tests
`tests/test_sports.py` (NBA section) runs everything offline on `tests/fixtures/nba/` (synthetic 8-team, 4-game, 80-player
DK file + 2,084 prop outcomes): CSV format and name matching, the 28/8/8 ≈ 50-point check with DD probability, 20 valid DK
lineups through `nba_daily.py` (slots, cap, 2-game rule, 3-stack, exposure, uniqueness), and the slate-picking rule.
`tests/test_cli.py` compiles `jobs/sports/*.py` and parses the exact workflow command.
