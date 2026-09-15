# Gridiron Sim

Fantasy football game simulator + DFS lineup optimizer for DraftKings and FanDuel.
Supabase (Postgres) for data, Netlify for the front end, GitHub Actions for the nightly ingest.

## Layout

```
supabase/migrations/001_schema.sql   schema, DK/FD scoring as generated columns, views, RLS
supabase/migrations/002_slates.sql   slates, salaries, projections, slate_board view (Phase 2)
jobs/ingest.py                       nightly nflverse → Supabase pull (nflreadpy)
jobs/import_salaries.py              DK/FD salary CSV → slates + slate_salaries (name → player_id)
jobs/project.py                      baseline projections → slate_projections
jobs/pipeline.py                     import + project in one go
.github/workflows/nightly-ingest.yml scheduled ingest + re-project latest slate
.github/workflows/slate-pipeline.yml runs on push of data/slates/*.csv
data/slates/                         drop DK / FD salary exports here
web/index.html                       sortable stats site
web/lineups.html                     lineup builder + optimizer (GLPK in the browser), DK/FD CSV export
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
3. Monte Carlo game sim (player × sim matrix, correlations)
4. Backtesting harness (calibration of percentiles vs actuals)
5. Ownership model + multi-lineup GPP builder with stacks/exposure limits
