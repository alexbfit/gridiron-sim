# Sportsbook props vs SaberSim — CORRECTED (2026-09-26)

**The first version of this file (round 14, props +10 pts per lineup over SaberSim) was wrong: the 2024 props file it used contains post-kickoff lines.** Below is the correction and the honest 2025 test with real Sunday 7 AM lines (project doc contest-backtest-round3.md, round 15).
**Round 14's conclusion (props beat SaberSim by +10 pts per lineup) is wrong.** The 2024 props file (`DK_props_odds_nickpasternak_2024`, no timestamps) contains lines captured after kickoff for part of the players.
- Proof: with the paid Odds API (Alex's 20K plan, 9/26) I pulled real Sunday 7 AM lines for 2024 weeks 3/6/9/12 (44 games). On the same 539 players, the file's line minus the 7 AM line correlates **+.36** with (actual − 7 AM line); of the 49 players whose line differs by ≥ 2 pts, **86% moved toward the actual result**. Real pregame movement (2025, 7 AM → 12:45 PM, 577 players) correlates +.05, and 2 of the 4 big movers went the right way — chance. The file's value-r was .44 vs .33 for the real 7 AM lines of the same players.
- The round-9/25 leak checks (anytime-TD calibration, 1 PM vs late-game gap) did not catch it. Lesson: test a historical odds file against a timestamped pull before trusting it.
- **Everything in round 14 that used the 2024 props file is void:** props vs SaberSim, props .85 vs 1.0, props + SaberSim, the rank-group profits, and the props-based QB+3 / 20-lineup / `--corr` comparisons. Still valid: the correlation measurements themselves (from actual outcomes), the `--corr` tests on sim-only and SaberSim builds (neutral), the 2025 SaberSim QB+3 test (no gain), the real-field QB+N shares, and the distribution calibration.

**Honest 2025 test.** 7 AM Sunday props for every 2025 main-slate game (198 games, 8 books, `hist/player_props_2025_7am.csv.gz`, ~11,900 credits; CALIB fit on 2024 so fully out of sample).
- Accuracy, 2,450 player-weeks. Value-over-salary r: SaberSim **.345**, props .328, props .85 + sim .333, our sim .290. Blends with SaberSim .342–.344 (no gain). By position, props trail SaberSim at QB (.17 vs .27) and WR (.24 vs .25), tie RB, lead TE.
- A late refresh adds almost nothing. On 4 weeks, 12:45 PM lines differ from 7 AM by 0.16 pts per player; value-r .334 vs .331.
- Contest backtest, 2025 weeks 1–18, seeds 7 + 11, 50 lineups QB+2, real Millionaire fields; Play-Action (#1–20) / mini-MAX (#1–40) re-graded:

| setup | avg pts/lineup | Milly top-1%/wk | top-0.1%/wk | best ≤1000 | median best | Milly ROI | PA #1-20 top-1%/wk | PA ROI | mini #1-40 top-1%/wk | mini ROI |
|---|---|---|---|---|---|---|---|---|---|---|
| sim only | 135.2 | 1.24 | 0.06 | 16/34 | 1,559 | −26% | 1.12 | +58% | 0.96 | −17% |
| **SaberSim 1.0** | **138.0** | 1.15 | 0.12 | **19/34** | **895** | −24% | 0.53 | +121% | 0.93 | +2% |
| props .85 | 134.2 | 0.91 | 0.09 | 13/34 | 1,474 | −44% | 0.34 | −7% | 0.39 | −36% |
| props .85 + no-market discount | 134.7 | 0.94 | 0.03 | 15/34 | 1,140 | −42% | 0.47 | −7% | 0.43 | −35% |

- Paired (36 cells): SaberSim vs sim +3.6 ± 1.9 pts per lineup; props setup vs sim +0.5 ± 2.0 pts, top-1% −0.28 ± 0.28/wk (no measurable difference); props setup vs SaberSim −3.1 ± 1.5 pts. The no-market discount is +0.8 ± 0.5 pts on props. Dollar ROIs are lottery-driven.
- **Conclusion:** SaberSim is still the best projection source we've tested (about +3 pts per lineup). Sportsbook props are as good as the sim at contest level and better on accuracy, but they do not replace SaberSim. Once SaberSim ends, expect to lose those ~3 pts per lineup.
- **Tasks reverted 9/26 11:30 AM:** SaberSim `--ext-sim 1.0` whenever the file exists; props .85 + no-market discount always on underneath (covers players SaberSim doesn't list, and is the setup from week 4).
- Still valid from round 14: 98fb10e (props.py `--source` — the reason no props ever landed), 05d5c10 + e03eda1 (no-market discount with the Kalshi guard). Kalshi now covers ~155 slate players with full projections (week 3), untested for accuracy.
- Credits: ~2,600 of 20,000 left after the 2025 pull, the 2024 7 AM check and the 12:45 PM sample. Scripts: `hist/pull.py` (resumable; SNAP_ET, TAG), `acc2025.py`, `snapcmp.py`, `milly25.py`; all in `DFS DATA\_backtest_archive` after the next archive refresh.
