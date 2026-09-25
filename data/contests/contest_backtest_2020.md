# Contest backtest — 2020 Millionaire Maker, 14 weeks (run 2026-09-23)

`jobs/contest_backtest.py` replayed 2020 weeks 2–16 (no wk 13 file) with the production sim + builder, using only
pre-kickoff information (stats before the week, that week's injury report, weekly roster status for IR/PUP, depth
chart), rostered from the archived DK salary files, 3,000 sims, 50 GPP lineups per mode, seeds 7 and 11, graded
against the real Milly Maker standings (real FPTS, real rank among ~250k entries, milly payout curve scaled to a
15% rake, $20 fee). Numbers below are per week of 50 entries ($1,000 in fees), averaged over 28 week×seed runs.

## Results

```
mode                      proj  own |   act fld% best% | top1% top10% cash% |  net/wk  median wks+ $500+
sunday (ev, fade .4)     141.6  106 | 127.7   45  92.1 |   1.0    7.4  18.1 |    -579    -721    4     0
ev, fade 0               141.9  107 | 128.5   46  93.5 |   1.1    7.6  18.2 |    -561    -732    4     0
ev, fade .4, real own    141.7   99 | 124.3   42  92.0 |   0.9    6.5  14.3 |    -650    -836    3     0
p90 rank, fade .4        144.2  114 | 129.9   48  93.1 |   0.8    9.1  19.6 |    +377    -742    5     1
p90 rank, fade 0         144.2  114 | 130.3   48  93.9 |   1.0    9.6  21.4 |    +414    -661    6     1
ev, fade .4, exp .35     138.6   98 | 127.9   46  95.2 |   0.7    7.5  19.3 |    -488    -685    3     1
p90 f0, market .5        134.1  100 | 127.0   45  94.1 |   0.8    8.5  18.6 |    -541    -799    4     0
p90 f0, dkavg blend .5   134.4   95 | 112.0   28  82.0 |   0.1    2.4   6.5 |    -852    -940    0     0
p90 f0, no bringback     145.0  115 | 130.1   48  94.1 |   0.8    8.1  20.7 |    -511    -601    3     0
p90 f0, exp .35 uniq 4   140.7  104 | 129.6   47  94.1 |   1.6    9.0  20.4 |    -340    -591    4     3
p90 f0, stack 2          142.0  109 | 129.2   47  96.1 |   1.3    8.4  19.4 |    -427    -705    3     1
random entry:             field% 50, top1% 1.0, top10% 10.0, cash% 23, net/wk -197
```
fld% = average percentile of our lineups in the real field; top1%/top10%/cash% = share of our 50 lineups finishing
there; net/wk = prizes − $1,000; "$500+" = lineups that won ≥ $500 across all 28 runs. The p90 modes' positive
net/wk is ONE week-10 hit (a top-10 finish, +$12.8k); their median week is −$660 to −$740 like everything else.

Field percentile by week (p90, fade 0): 47 24 38 28 48 57 33 53 72 74 37 64 47 53 — weeks 2–5 are far below
the field, weeks 9–16 average ~57%. The model is weak early in a season and mildly competitive from mid-season.

## What it settles
1. `--fade 0.4` neither helps nor hurts (fade 0 marginally better on every metric). Dropped from the Sunday task.
2. The contest-sim / EV selection is WORSE than plain sim-p90 ranking on every metric (field% 45 vs 48, top-10%
   7.4 vs 9.6, cash 18 vs 21). Giving it the real ownership made it worse still (42%). Dropped from the Sunday task.
3. Wider diversification (max-exp .35, min-uniq 4) gave the best top-1% rate (1.6% vs 1.0%) and 3 of the 6 $500+
   hits. Adopted. Stack 2 had the best single-lineup ceiling (best% 96.1) but not more money.
4. Bring-back: no difference. Kept (the real top 1% bring back 50–60%).
5. Blends: salary-market .5 no help; DK AvgPointsPerGame blend is very bad (28%).
6. The real problem is projection quality, not selection. Among DFS-relevant players (sim mean ≥ 8, n=1,476),
   r(sim, actual) = .46 vs r(salary, actual) = .45 — within position DK's salary predicts points as well as the sim
   (RB .44 vs .40, TE .33 vs .28). After removing salary, the sim's residual correlates +.11 to +.15 with the
   residual actual; the crowd's ownership residual correlates +.13 to +.21. The field knows more than the model
   (news, props, roles), which is also why fading the crowd never worked.

## Sunday task after this (updated 2026-09-23)
`--contest gpp --n 50 --candidates 8 --stack 1 --bringback --max-exp 0.35 --min-uniq 4 --fade 0 --seed 7`
No --objective ev. Expected result at this model quality is roughly a random entry (−15–20% ROI) with a fat right
tail; the edge has to come from better projections.

## Where an edge could come from (not built)
- Early-season shrinkage: weeks 2–5 were 24–38% of field. Heavier prior-season / market weight until ~week 6.
- Signals the crowd has that the sim lacks: Vegas player props (yards/TD lines), weather, beat-writer role news,
  real projected-ownership feeds. The ownership residual (+.13–.21) is the size of the prize.
- Late swap after inactives (the harness rosters with the 90-minute inactives unknown; the real field uses them).

## Reproduce
```
python jobs/contest_backtest.py --season 2020 --weeks 2-16 --sims 3000 --store 2000 --n 50 --seeds 7 11 \
  --data-dir <offline pull> --cache <dir> \
  --salaries "DFS DATA/DK_2020_tidyDK_salaries/DKSalaries_NFL2020_Week_{week:02d}_ms.csv" \
  --standings "DFS DATA/DK_2020_tidyDK_Millionaire/NFL2020_w{week:02d}_*.csv" --out contest_2020.json
```
The offline pull is `ingest.pull([2019, 2020])` + `pull_extras` + weekly rosters + depth charts dumped to JSON (see
the session notes); DK points are computed with the same formula as the DB column. ~6 min per week for 12 mode×seed
runs on 2 cores.

## Addendum 2026-09-24 — "max wins" candidates (seed 7, weeks 2–16, 50 lineups, same harness; payout curve fixed for small fields)
Question: does ranking candidates by sim p98 instead of p90, or spreading wider (max-exp .25, min-uniq 5), produce more
tournament-winning finishes? Metric that matters for "max wins" = weeks with a top-250 / top-1000 finish out of ~230k.

| mode | wk×seed | act avg | fld% | top1/wk | top10/wk | cash% | net $/wk | weeks best ≤250 | ≤1000 | median best rank |
|---|---|---|---|---|---|---|---|---|---|---|
| p90, exp .35, uniq 4 (current Sunday default) | 28 | 129.6 | 47 | 0.79 | 4.5 | 20.9 | −339 | 3 (11%) | 8 (29%) | 5,265 |
| p90, exp .25, uniq 5 | 15 | 128.6 | 47 | 0.40 | 4.5 | 19.4 | −537 | 0 | 2 (13%) | 7,242 |
| p98, exp .35, uniq 4 | 15 | 129.1 | 48 | 0.67 | 4.8 | 20.1 | −462 | 1 (7%) | 3 (20%) | 4,563 |
| p98, exp .25, uniq 5 | 14 | 129.2 | 47 | 0.50 | 5.4 | 21.5 | −436 | 2 (14%) | 3 (21%) | 4,439 |

Reading: nothing beats the current default. p98 + wide spread is a wash (slightly more consistent top-10% / cash, no more
top-250 weeks); p90 with the wider spread is worse. The ranking key and the exposure knobs are not where tournament wins come
from — with projections at consensus level the top finish is a 1-in-8-weeks event for 50 entries whatever the build. What
moves the "max wins" number: more entries in the contest with the biggest top prize (linear in shots), the projection edge
(props + news), and late swap. `build_lineups --rank p98` stays available; the Sunday task keeps p90 / .35 / 4.

## Addendum 2026-09-24 night — hunting top-250 finishes; 50 vs 150 lineups (Opus session)
Same harness and weeks (2–16, no wk 13), fresh offline pull (game-day inactives treated as ACTIVE: the build doesn't know
them), seeds 7 + 11 unless noted, fade 0, bring-back on, max-exp .35, min-uniq 4. The standings were compacted to
Rank/Points/Player/%Drafted/FPTS (`DFS DATA/_compact_2020_milly/`) so they fit through the device bridge.

| mode | runs | lineups | weeks best ≤250 | ≤1000 | median best rank | lineups ≤250 | top-1%/wk | cash% | net $/wk | ROI |
|---|---|---|---|---|---|---|---|---|---|---|
| QB+1, p90 (current Sunday default) | 28 | 50 | 0 (0%) | 6 (21%) | 4,472 | 0 | 0.50 | 24% | −434 | −44% |
| **QB+2, p90** | 28 | 49 | **4 (14%)** | 6 (21%) | 5,254 | 4 | 0.46 | 24% | −300 | −31% |
| QB+2, p98 | 28 | 50 | 3 (11%) | 7 (25%) | 4,901 | 3 | 0.61 | 25% | −285 | −29% |
| QB+2, lineup own ≤110% | 28 | 48 | 3 (11%) | 6 (21%) | 4,759 | 3 | 0.54 | 23% | −318 | −33% |
| QB+2, rand .30, cand 12 | 28 | 50 | 1 (4%) | 7 (25%) | 8,544 | 1 | 0.71 | 21% | −459 | −46% |
| 150: QB+1, p90 (seed 7) | 14 | 147 | 2 (14%) | 4 (29%) | 2,769 | 3 | 1.57 | 23% | −1,355 | −46% |
| **150: QB+2, p90** | 28 | 146 | **7 (25%)** | 9 (32%) | 2,892 | 9 | 1.39 | 23% | −1,129 | −39% |
| 150: QB+2, p98 (seed 7) | 14 | 149 | 4 (29%) | 5 (36%) | 1,449 | 6 | 1.93 | 23% | −1,118 | −38% |

Reading:
- QB+2 beat QB+1 for top-250 weeks at both sizes (50: 4 vs 0 of 28; 150: 7/28 vs 2/14), with a better ROI (−31% vs −44%).
  Caveat: the hits cluster in weeks 10, 11, 14 and 16 (the QB+2 variants find the same stacks), so this is 3–4 real
  weeks of evidence, not dozens. The direction matches real Milly winners (mostly QB+2 with a bring-back).
- p98 ranking and an ownership cap are neither better nor worse than plain QB+2. More randomness hurt.
- 150 lineups ≈ doubles the share of weeks with a top-250 finish (25–29% vs 11–14%) at the same ROI and 3× the cost.
  Hits cluster by week, so tripling entries doesn't triple the weeks.
- A top-250 finish in a Milly pays only ~$120–$1,200. Across everything there was one top-20 (rank 16, $3,000: the same
  week-11 QB+2 lineup in three modes). ROI is negative in every mode.

## Addendum 2026-09-25 — QB filters on top of QB+2 (jobs/contest_backtest_qbfilter.py)
Same harness, 50 lineups, QB+2 / bring-back / p90 / max-exp .35 / min-uniq 4, seeds 7 + 11 (28 runs each). QBs failing
the filter are marked OUT before each build. Control = "QB+2, p90" from round 3.

| QB pool | wks best ≤250 | ≤1000 | median best rank | lineups ≤1000 | top-1%/wk | cash% | net $/wk | ROI |
|---|---|---|---|---|---|---|---|---|
| all QBs (control) | 4 (14%) | 6 (21%) | 5,254 | 9 | 0.46 | 24% | −300 | −31% |
| home QBs only | 4 (14%) | **9 (32%)** | **2,548** | **15** | **0.89** | 24% | **−228** | **−23%** |
| top-3 game totals | 2 (7%) | 7 (25%) | 3,250 | 8 | 0.71 | 26% | −260 | −26% |
| total ≥ slate median | 3 (11%) | 7 (25%) | 4,586 | 10 | 0.61 | 25% | −384 | −39% |
| home + total ≥ median | 1 (4%) | 5 (18%) | 2,176 | 9 | 0.79 | 25% | −337 | −34% |

Reading: no filter produced more top-250 weeks. Home-only tied on top-250 weeks and was better on every secondary
measure, with hits spread over 6 weeks instead of 3. But 2020 was the no-fans season (home-field advantage ≈ 0), so
it is the wrong year to trust a home effect, and it is one season. Game-total filters did not help: the sim already
uses the Vegas total, so filtering on it double-counts and just shrinks the QB pool; top-half and home+top-half were
the worst. Not adopted; re-test home-only on a normal season before changing the Sunday build.

## Addendum 2026-09-25 — the big settings sweep (rounds 5–11), plus 2026 week-2 and 3-max checks
~30 settings on top of QB+2 (p90 / .35 / 4 / bring-back), 2020 Milly weeks 2–16. Screened with seed 7, finalists with
seeds 11 and 23. Every lineup was then re-graded against the 2020 nickel 3-max field (~36k entries, same weeks), and
the finalists were rebuilt for 2026 week 2 (Milly 172,692 entries + a 20-max, 11,181) — the only 2026 week with salaries.

Control (QB+2, 3 seeds): top-250 weeks 5/42 (12%), top-1000 10, median best rank 4,508, top-1%/wk 0.60, ROI −22%.

No noticeable change (seed 7, some with more seeds): max-exp .5/uniq 3; 16 candidates; no bring-back; QB+3
(max-team 5); min salary 49,500; rank by proj; QB exposure caps .12 / .08; ceiling weight .7; ceiling p95; one RB per
game; RB+own DST; both; lineup own ≤110%; more randomness; field-sim P(top 0.1%) ranking (worse: 0 top-250,
−54%); chalk boost with our heuristic ownership (3 seeds: tie; one seed's rank-3 $40k was luck).

Signals worth keeping:
- **Home QBs only** (3 seeds): top-250 weeks 7/42 (17%), top-1000 13, median best 2,226, top-1%/wk 0.79, ROI −21%.
  Nickel re-grade: top-1%/wk 0.74 vs 0.60, weeks with a top-0.1% lineup 7 vs 5. 2026 wk2 Milly: best ranks
  778/111/499 vs 5,490/652/3,096, top-1% lineups 6 vs 1; 20-max: 71/6/43 vs 922/49/719. At 150 lineups: top-1000
  weeks 7 vs 5, top-1%/wk 2.29 vs 1.71. Small but the only setting that points the same way in all four fields.
  Caveats: 2020 had no fans, and the 2026 week-2 gain is mostly one home QB (Dak) getting more exposure.
- **Ownership tilt with a SaberSim-quality ownership projection** (noisy real ownership, r ≈ .8, fade −.8, 2 seeds):
  top-1%/wk 0.75, median best 2,959. 2026 wk2 20-max best 49/130/42; Milly mixed. Needs an ownership feed we don't have.
- **Full game stack** (2 bring-backs): good on 2020 seed 7, **failed** on 2026 week 2 in both contests. Not adopted.
- **Week of the season matters more than any setting**: across ~720 week-runs every setting found its top finishes in
  weeks 10/11/14/16; weeks 2–5 averaged 41% of field, 0% top-250 weeks, −58% ROI vs 52% / 14% / −17% in weeks 6–16.
  A salary-market blend did not fix early weeks (35% vs 39% of field in weeks 2–8).

Builder options added for these tests (all off by default): --rb-one-per-game, --rb-dst, --max-qb-exp, --ceil-weight,
--ceil-q, --bringback-n, --ev-key. QB filters: jobs/contest_backtest_qbfilter.py (QBF=home|top3|tophalf|home_tophalf,
EXTRA="<more builder args>", TAG=<mode suffix>).
