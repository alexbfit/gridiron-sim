"""QB-filter contest backtests on top of the QB+2 default (2026-09-25).

Marks every QB who fails the filter OUT before each build, then runs the normal harness (same arguments as
contest_backtest.py). QBF env var: home | top3 (slate's 3 highest game totals) | tophalf (total >= slate median) | home_tophalf.

    QBF=home python jobs/contest_backtest_qbfilter.py --season 2020 --weeks 2-16 --n 50 --seeds 7 11 \
        --data-dir <offline pull> --cache <dir> --salaries ... --standings ... --modes "QB home, s2" --out q_home.json
"""
import sys, os, json, statistics
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import contest_backtest as cb
import build_lineups as bl
_dd = sys.argv[sys.argv.index('--data-dir') + 1] if '--data-dir' in sys.argv else 'data'
GAMES = {g['game_id']: g for g in json.load(open(os.path.join(_dd, 'games.json')))}
QBF = os.environ['QBF']
_build = bl.build
def build(args, slate, rows, *a, **k):
    gids = {r.get('game_id') for r in rows if r.get('game_id') in GAMES}
    totals = sorted(((GAMES[g]['total_line'] or 0), g) for g in gids)
    med = statistics.median(t for t, _ in totals)
    top3 = {g for _, g in totals[-3:]}
    def ok(r):
        g = GAMES.get(r.get('game_id'))
        if not g: return True
        home = r['team'] == g['home_team']
        hi = (g['total_line'] or 0) >= med
        return {'home': home, 'top3': r.get('game_id') in top3, 'tophalf': hi, 'home_tophalf': home and hi}[QBF]
    n = 0
    for r in rows:
        if r['position'] == 'QB' and not ok(r):
            r['status'] = 'OUT'; n += 1
    return _build(args, slate, rows, *a, **k)
bl.build = build
cb.bl.build = build
S2 = ["--stack", "2", "--bringback", "--max-exp", "0.35", "--min-uniq", "4", "--candidates", "8", "--fade", "0"]
cb.MODES.update({f"QB {QBF}, s2": S2})
cb.main()
