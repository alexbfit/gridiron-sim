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
