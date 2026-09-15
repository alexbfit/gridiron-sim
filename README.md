# Gridiron Sim

Fantasy football game simulator + DFS lineup optimizer for DraftKings and FanDuel.
Supabase (Postgres) for data, Netlify for the front end, GitHub Actions for the nightly ingest.

## Layout

```
supabase/migrations/001_schema.sql   schema, DK/FD scoring as generated columns, views, RLS
jobs/ingest.py                       nightly nflverse → Supabase pull (nflreadpy)
.github/workflows/nightly-ingest.yml scheduled job + manual backfill
web/                                 sortable stats site (static, Netlify)
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
2. DK/FD salary CSV import, baseline projections, MIP optimizer with upload-CSV export
3. Monte Carlo game sim (player × sim matrix, correlations)
4. Backtesting harness (calibration of percentiles vs actuals)
5. Ownership model + multi-lineup GPP builder with stacks/exposure limits
