"""Multi-sport DFS engine: DK roster rules, SaberSim-archive loader, correlated Monte Carlo, MIP optimizer, backtest.

The NFL engine (jobs/simulate.py, jobs/build_lineups.py) is untouched; this package is the sport-generic layer
built on the all-sports archive (DFS DATA/_sabersim_allsports). See jobs/build_lineups_sport.py for the CLI.
"""
from .rules import RULES, STACK_KEYS, TEAM_SPORTS
from .archive import load_slate, slate_dates, field_kinds, Field
from .sim import sim_matrix, corr_matrix
from .optimize import Opts, build, solve_one
