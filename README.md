# Gridiron Sim

Fantasy football game simulator + DFS lineup optimizer for DraftKings and FanDuel.
Supabase (Postgres) for data, Netlify for the front end, GitHub Actions for the nightly ingest.

## Layout

```
supabase/migrations/001_schema.sql   schema, DK/FD scoring as generated columns, views, RLS
supabase/migrations/002_slates.sql   slates, salaries, projections, slate_board view (Phase 2)
supabase/migrations/003_sim.sql      projections keyed by method, sims storage bucket (Phase 3)
supabase/migrations/004_snaps_injuries.sql  snap counts + official injury reports (Phase 6)
supabase/migrations/005_results_ownership.sql  slate_results, slate_ownership (Phase 7)
jobs/results.py                      score a finished slate: proj vs actual, PIT, ownership → slate_results
jobs/import_ownership.py             DK contest standings CSV → slate_ownership (real %drafted + exact FPTS)
jobs/fit_ownership.py                fit the builder's ownership heuristic to real ownership → model_params
data/ownership/                      drop DK contest standings exports here
web/results.html                     results page: last week's projections vs actual, your exported lineups
jobs/ingest.py                       nightly nflverse → Supabase pull (nflreadpy)
jobs/import_salaries.py              DK/FD salary CSV → slates + slate_salaries (name → player_id)
jobs/project.py                      baseline projections → slate_projections (method=baseline)
jobs/simulate.py                     Monte Carlo game sim → slate_projections (method=sim) + sims bucket
jobs/backtest.py                     replay past weeks, score sim/baseline/naive → web/data/backtest.json
jobs/pipeline.py                     import + project in one go
.github/workflows/nightly-ingest.yml scheduled ingest + re-project latest slate
.github/workflows/slate-pipeline.yml runs on push of data/slates/*.csv
data/slates/                         drop DK / FD salary exports here
web/index.html                       sortable stats site
web/lineups.html                     lineup builder + optimizer (GLPK in the browser), DK/FD CSV export
web/backtest.html                    backtest report (accuracy by position, weekly error, calibration)
```

## Setup

1. **Supabase** — create a project, open the SQL editor, paste and run
   `supabase/migrations/001_schema.sql`.
2. **GitHub** — push this folder to a repo. Add two repository secrets:
   `SUPABASE_URL` and `SUPABASE_SERVICE_ROLE_KEY` (Project Settings → API).
3. **Backfill** — Actions → *nightly-ingest* → *Run workflow*, seasons `2023 2024 2025`.
   After that it runs itself every morning at 5 AM ET.
4. **Web** — copy `web/config.example.js` to `web/config.js`, add the project URL and
   **anon** key. Deploy: connect the repo to Netlify (publish dir is `web`, already set
   in `netlify.toml`). Since `config.js` is git-ignored, either commit it (anon key is
   public-safe) or remove it from `.gitignore`.

Local run of the ingest:

```
pip install -r jobs/requirements.txt
python jobs/ingest.py --dry-run
SUPABASE_URL=... SUPABASE_SERVICE_ROLE_KEY=... python jobs/ingest.py --seasons 2024 2025
```

## Slate day (Phase 2)

1. DraftKings / FanDuel lobby → pick the contest → **Export to CSV**.
2. Save it as `data/slates/DKSalaries_<season>_wk<NN>_main.csv` (any name works; the word
   `showdown`/`primetime` etc. in the filename sets the slate type).
3. Commit + push. The `slate-pipeline` action imports the file, maps every player to their
   nflverse id, and writes baseline projections. ~1 minute.
4. Open `/lineups.html`, pick the slate, lock/exclude, set rules, **Generate**, **Export CSV**,
   upload the CSV in the DK/FD lineup editor.

Re-run projections without a new CSV (lines moved, injuries): Actions → slate-pipeline →
Run workflow → enter the slate key (e.g. `DK-2026-02-main`). The nightly ingest also
re-projects the latest slate.

### Baseline projection model
recent weighted game log (0.85^games ago, prior season ×0.5) → regressed to a per-position
salary prior (3 pseudo-games) → × opponent factor (`defense_vs_position`, shrunk 50%)
× Vegas factor (implied team total vs slate avg, shrunk 50%), product capped ±20% →
injury status (OUT/IR = 0, D ×0.5, Q ×0.95). Stdev from the player's own variance shrunk
to a position default; floor/ceiling/boom/bust from a normal. DST: opponents' sacks + INTs
allowed + expected points-allowed tier from the opponent's implied total.

### Monte Carlo game sim (Phase 3)
`jobs/simulate.py` runs 10,000 simulations of every game on the slate. Per sim it draws the
score from the Vegas line (margin ~ N(spread, 13.5), total ~ N(total, 10.5)), plays and pass
rate (team pace, shifted by game script), team TDs ~ Poisson(points/7.3) split pass/rush, then
each player's target and carry share (noisy around his usage with his *current* team; OUT
players are removed and their share flows to teammates; D/Q players are active in 50%/90% of
sims), receptions, yards (tied to the simulated score), TDs, INTs, fumbles, and finally DK / FD
points with bonuses. DST scores come from the opponent's simulated points, sacks and turnovers.
Because every player's line comes from the same drawn game, outcomes are correlated (QB ↔ his
receivers ≈ +0.3, QB ↔ opposing DST ≈ −0.3).

Outputs: `slate_projections` rows with `method = 'sim'` (the board prefers these over baseline),
and a 2,000-column player × sim matrix (`sims/<slate_key>.json.gz` in Supabase storage) that
the lineup builder downloads to score whole lineups (p10 / p50 / p90 / p98 of the lineup total).
With the matrix loaded, "Candidates ×" builds extra lineups and keeps the best by simulated
median (cash) or 90th percentile (GPP), still honouring exposure caps.

### Backtest (Phase 4)
`jobs/backtest.py --season 2025 --weeks 5-18` replays each week with only pre-kickoff data
(closing lines, prior logs, players who dressed), runs the sim and the baseline, and scores them
against actual DK points for DFS-relevant players. Report at `/backtest.html`; regenerate from
Actions → *backtest* (commits `web/data/backtest.json`). Calibration result on 2025 wk 5–18:
sim MAE 6.20 / r 0.45 / bias −0.2 vs naive 6.50 / 0.39; 11% of actuals under p10, 89% under
p90. Constants tuned from it: `PTS_PER_TD 8.8`, TE priors, `DECAY 0.90`.

### Ownership (Phase 5, first cut)
The builder shows a heuristic projected ownership (softmax over value and projection within each
position, scaled to the position's roster slots). Overwrite the Own% column with real projected
ownership if you have a source. GPP objective subtracts `fade × 0.06 × own%`; lineup cards show
summed ownership.

### Snap counts + injury reports (Phase 6)
The nightly ingest also pulls nflverse snap counts (`player_snaps`) and official injury reports
(`injury_reports`). The sim scales a **TE's** usage by the change in his recent snap share
(backtest: TE MAE 5.30 → 5.18, r 0.30 → 0.34; the same adjustment hurt RB/WR, so it is off for
them). When the DK/FD status is blank, the official report (Out / Doubtful / Questionable) is
applied instead, and the builder shows it as `OUT*` / `D*` / `Q*` with the injury on hover.

### Results + ownership (Phase 7)
Once a slate's games are final, the nightly job scores it (`jobs/results.py --all`): projection
vs actual per player, PIT (where the actual landed in the sim distribution), calibration
summary in `slates.results_meta`, and a Season-to-date trend on `/results.html`. Lineups you
export from the builder are remembered in your browser and scored there too. Projections
freeze once a slate's games start (no post-hoc re-sim).

For real ownership: DK → any contest you entered → download the standings CSV → save it as
`data/ownership/<slate-key>_standings.csv` (e.g. `DK-2026-02-main_standings.csv`) → push. The
pipeline imports %Drafted and exact FPTS (incl. DST), re-scores the slate with exact points,
and `fit_ownership.py` re-fits the builder's ownership coefficients once ≥100 player rows exist.

### Optimizer
Mixed-integer program solved in the browser (glpk.js). Cash: 0.8·proj + 0.2·floor.
GPP: 0.6·proj + 0.4·p85 with per-lineup jitter, QB stacks, bring-back, exposure caps,
min-unique players across lineups. Roster/salary/max-per-team/2-game rules per site.

## Scoring

Stored in the DB as generated columns on `player_game_stats`:

| | DK | FD |
|---|---|---|
| Pass yd / TD / INT | 0.04 / 4 / -1 | 0.04 / 4 / -1 |
| 300+ pass yds | +3 | — |
| Rush yd / TD | 0.1 / 6 | 0.1 / 6 |
| 100+ rush yds | +3 | — |
| Reception | 1 | 0.5 |
| Rec yd / TD | 0.1 / 6 | 0.1 / 6 |
| 100+ rec yds | +3 | — |
| Fumble lost | -1 | -2 |
| 2-pt conversion | 2 | 2 |

## Roadmap

1. ✅ Data pipeline + sortable stats site
2. ✅ DK/FD salary CSV import, baseline projections, MIP optimizer with upload-CSV export
3. ✅ Monte Carlo game sim (player × sim matrix, correlations)
4. ✅ Backtesting harness (calibration of percentiles vs actuals)
5. ◐ Ownership: heuristic + fade in the GPP objective (multi-lineup builder with stacks/exposure done in phase 2)
6. ✅ Snap counts (TE usage) + official injury reports
7. ✅ Results tracking + real ownership from contest exports
8. Next: showdown slates, FanDuel validation, route data, QB modelling
