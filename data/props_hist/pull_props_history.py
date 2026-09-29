"""Pull historical Sunday 7 AM ET player props (The Odds API) for DK main-slate games. Resumable; logs credits.
usage: python3 pull.py 2025 1-18   (writes hist/<season>/wkNN/<event>.json and player_props_<season>_7am.csv.gz)"""
import sys, os, json, csv, gzip, time, datetime as dt, urllib.request, urllib.parse
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', 'jobs'))
from props import NAME_TO_ABBR
KEY = os.environ['ODDS_API_KEY']
API = 'https://api.the-odds-api.com/v4/historical/sports/americanfootball_nfl'
BASE = 'player_pass_yds,player_pass_tds,player_rush_yds,player_reception_yds,player_receptions,player_anytime_td'
EXTRA = ('player_pass_attempts,player_pass_completions,player_pass_interceptions,player_rush_attempts,'
         'player_pass_yds_alternate,player_pass_tds_alternate,player_rush_yds_alternate,player_reception_yds_alternate,'
         'player_receptions_alternate,spreads,totals')
MARKETS = os.environ.get('MARKETS', BASE + ',' + EXTRA)
FILETAG = os.environ.get('FILETAG', 'full')
MIN_LEFT = int(os.environ.get('MIN_LEFT', 2500))
season = int(sys.argv[1]); WEEKS = [int(x) for x in sys.argv[2].split(',')] if ',' in sys.argv[2] else list(range(int(sys.argv[2].split('-')[0]), int(sys.argv[2].split('-')[1]) + 1))
ET_H, ET_M = map(int, os.environ.get('SNAP_ET', '07:00').split(':')); TAG = os.environ.get('TAG', '7am')
ABBR = dict(NAME_TO_ABBR); ABBR['Los Angeles Rams'] = 'LAR'
def get(path, params):
    params = dict(params, apiKey=KEY)
    for t in range(5):
        try:
            with urllib.request.urlopen(f'{API}/{path}?{urllib.parse.urlencode(params)}', timeout=60) as r:
                return json.load(r), int(r.headers.get('x-requests-remaining', -1)), int(r.headers.get('x-requests-last', 0))
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503): time.sleep(5 * (t + 1)); continue
            raise
    raise SystemExit('too many retries')
rows_all = []
for wk in WEEKS:
    d = f'./hist/{FILETAG}/{season}/wk{wk:02d}' + ('' if TAG == '7am' else '_' + TAG); os.makedirs(d, exist_ok=True)
    sal = list(csv.DictReader(open(f'./ssall/{season}/DKSalaries_{season}_wk{wk:02d}_main.csv')))
    teams = {r['TeamAbbrev'] for r in sal}
    day = min(dt.datetime.strptime(r['Game Info'].split(' ')[1], '%m/%d/%Y').date() for r in sal if ' ' in r['Game Info'])
    while day.weekday() != 6: day += dt.timedelta(days=1)   # the Sunday
    dst_end = {2024: dt.date(2024, 11, 3), 2025: dt.date(2025, 11, 2), 2026: dt.date(2026, 11, 1)}[season]
    off = 4 if (day < dst_end and day.month >= 3) else 5
    snap = f'{day.isoformat()}T{ET_H + off:02d}:{ET_M:02d}:00Z'
    evf = f'{d}/events.json'
    if os.path.exists(evf): ev = json.load(open(evf))
    else:
        ev, left, _ = get('events', {'date': snap}); json.dump(ev, open(evf, 'w'))
    games = [e for e in ev['data'] if ABBR.get(e['home_team']) in teams and ABBR.get(e['away_team']) in teams and e['commence_time'][:10] == day.isoformat()]
    print(f'wk {wk}: {day} snapshot {snap} ({ev.get("timestamp")}), {len(games)} main-slate games of {len(teams)//2}', flush=True)
    for e in games:
        f = f'{d}/{e["id"]}.json'
        if not os.path.exists(f):
            g, left, last = get(f'events/{e["id"]}/odds', {'date': snap, 'regions': 'us', 'oddsFormat': 'american', 'markets': MARKETS})
            json.dump(g, open(f, 'w'))
            print(f'   {e["away_team"]} @ {e["home_team"]}: {last} credits, {left} left', flush=True)
            if left < MIN_LEFT: raise SystemExit(f'stopping: {left} credits left')
            time.sleep(0.5)
        g = json.load(open(f)); x = g.get('data') or {}
        for b in x.get('bookmakers', []):
            for m in b['markets']:
                for o in m['outcomes']:
                    rows_all.append([wk, x['id'], x['commence_time'], x['home_team'], x['away_team'], b['title'], m['key'], o['name'], o.get('description', ''), o.get('price', ''), o.get('point', '')])
out = f'./hist/player_props_{season}_{TAG}_{FILETAG}_wk{WEEKS[0]}-{WEEKS[-1]}.csv.gz'
with gzip.open(out, 'wt', newline='') as fh:
    w = csv.writer(fh); w.writerow(['week', 'game_id', 'commence_time', 'home_team', 'away_team', 'bookmaker', 'market', 'label', 'player', 'price', 'point']); w.writerows(rows_all)
print('wrote', out, len(rows_all), 'rows')
