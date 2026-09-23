"""
Sportsbook player props -> projections (slate_projections, method = 'props').

Why: on 2024 weeks 1-15 a projection implied by the books' player props (yards / receptions / TD lines)
predicted actual DK points far better than our sim (r .48 vs .35; MAE 5.9 vs 6.5) and better than a
FantasyPros-style consensus (.42). The builder therefore pulls each player's sim mean toward the props
mean (build_lineups --props, default 0.85) and rescales his sim draws to match, keeping the sim's
variance and correlation structure.

Source: The Odds API (same ODDS_API_KEY as jobs/odds.py). Player props are per-event requests and
cost (markets x regions) credits each: 6 markets x 1 region x ~14 games = ~84 credits per pull on the
500/month free tier, so this runs ONCE per week (Sunday 10 AM ET in gameday-refresh).

  python jobs/props.py                 # latest slate, live API
  python jobs/props.py --dry-run       # fetch + match, no writes
  python jobs/props.py --file props.csv --week 5 --slate-key DK-2024-05-main   # archive CSV (backtests)

Projection = calibrated( pass_yds*0.04 + pass_tds*4 - ints + rush_yds*0.1 + receptions + rec_yds*0.1 + P(anytime TD)*6 )
using the median line across books and a de-vigged anytime-TD probability. Lines are medians, which
understate means (skew), so a per-position linear calibration fitted on 2024 is applied (slope ~1.3).
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import gzip
import json
import os
import sys
import urllib.parse
import urllib.request
from collections import defaultdict

import numpy as np

from common import chunked, fetch_all, get_client, norm_name

API = "https://api.the-odds-api.com/v4/sports/americanfootball_nfl"
MARKETS = ["player_pass_yds", "player_pass_tds", "player_rush_yds", "player_reception_yds", "player_receptions", "player_anytime_td"]
TD_DEVIG = 0.88                       # anytime-TD "Yes" prices carry ~12% vig one-sided
# actual ~= a + b * raw_props, fitted per position on 2024 wk1-15 (n=1,284 players with all markets)
CALIB = {"QB": (-4.90, 1.35), "RB": (-2.15, 1.32), "WR": (-1.55, 1.26), "TE": (-0.59, 1.13)}
NAME_TO_ABBR = {
    "Arizona Cardinals": "ARI", "Atlanta Falcons": "ATL", "Baltimore Ravens": "BAL", "Buffalo Bills": "BUF",
    "Carolina Panthers": "CAR", "Chicago Bears": "CHI", "Cincinnati Bengals": "CIN", "Cleveland Browns": "CLE",
    "Dallas Cowboys": "DAL", "Denver Broncos": "DEN", "Detroit Lions": "DET", "Green Bay Packers": "GB",
    "Houston Texans": "HOU", "Indianapolis Colts": "IND", "Jacksonville Jaguars": "JAX", "Kansas City Chiefs": "KC",
    "Las Vegas Raiders": "LV", "Los Angeles Chargers": "LAC", "Los Angeles Rams": "LA", "Miami Dolphins": "MIA",
    "Minnesota Vikings": "MIN", "New England Patriots": "NE", "New Orleans Saints": "NO", "New York Giants": "NYG",
    "New York Jets": "NYJ", "Philadelphia Eagles": "PHI", "Pittsburgh Steelers": "PIT", "San Francisco 49ers": "SF",
    "Seattle Seahawks": "SEA", "Tampa Bay Buccaneers": "TB", "Tennessee Titans": "TEN", "Washington Commanders": "WAS",
}


def implied(price) -> float:
    p = float(price)
    return 100 / (p + 100) if p > 0 else -p / (-p + 100)


def raw_points(lines: dict, td_prob: float) -> float:
    L = lambda m: lines.get(m, 0.0)
    pts = (L("player_pass_yds") * 0.04 + L("player_pass_tds") * 4 - L("player_pass_interceptions")
           + L("player_rush_yds") * 0.1 + L("player_receptions") + L("player_reception_yds") * 0.1 + td_prob * 6)
    if L("player_pass_yds") >= 300:
        pts += 3
    return pts


def calibrated(pos: str, raw: float) -> float:
    a, b = CALIB.get(pos, (0.0, 1.0))
    return max(0.0, a + b * raw)


# ------------------------------------------------------------------ sources
def api_get(path, params):
    q = urllib.parse.urlencode(params)
    with urllib.request.urlopen(f"{API}/{path}?{q}", timeout=60) as r:
        return json.load(r), r.headers.get("x-requests-remaining"), r.headers.get("x-requests-used")


def collect(outcomes_iter):
    """outcomes_iter yields (player, market, label, price, point). Returns {player: (lines, td_prob)}."""
    lines, tds = defaultdict(lambda: defaultdict(list)), defaultdict(list)
    for player, market, label, price, point in outcomes_iter:
        n = norm_name(player)
        if market == "player_anytime_td" and label == "Yes":
            tds[n].append(implied(price))
        elif label == "Over" and point not in (None, ""):
            lines[n][market].append(float(point))
    out = {}
    for n in set(lines) | set(tds):
        med = {m: float(np.median(v)) for m, v in lines[n].items()}
        td = float(np.mean(tds[n])) * TD_DEVIG if tds.get(n) else 0.0
        out[n] = (med, td)
    return out


def from_api(key: str, slate_games: list[dict]):
    """Per-event player props for the slate's games. Returns ({norm_name: (lines, td_prob)}, {norm_name: team})."""
    events, remaining, _ = api_get("events", {"apiKey": key})
    want = {(g["home_team"], g["away_team"]): g for g in slate_games}
    picked = []
    for ev in events:
        h, a = NAME_TO_ABBR.get(ev["home_team"]), NAME_TO_ABBR.get(ev["away_team"])
        if (h, a) in want:
            picked.append((ev, h, a))
    print(f"  {len(picked)} of {len(events)} events match the slate · requests remaining: {remaining}")
    if remaining is not None and int(remaining) < len(picked) * len(MARKETS) + 20:
        raise SystemExit(f"not enough Odds API credits left ({remaining}) for {len(picked)} events x {len(MARKETS)} markets")
    outcomes, team_of = [], {}
    for ev, h, a in picked:
        data, remaining, used = api_get(f"events/{ev['id']}/odds",
                                        {"apiKey": key, "regions": "us", "markets": ",".join(MARKETS), "oddsFormat": "american"})
        n_out = 0
        for book in data.get("bookmakers", []):
            for m in book.get("markets", []):
                for o in m.get("outcomes", []):
                    outcomes.append((o.get("description", ""), m["key"], o.get("name"), o.get("price"), o.get("point")))
                    n_out += 1
        print(f"  {a}@{h}: {n_out} outcomes from {len(data.get('bookmakers', []))} books · remaining {remaining}")
    return collect(outcomes), team_of


def from_file(path: str, week: int | None):
    """Archive CSV: week,game_id,commence_time,home_team,away_team,bookmaker,market,label,player,price,point"""
    opener = gzip.open if path.endswith(".gz") else open
    def gen():
        with opener(path, "rt", encoding="utf-8-sig", newline="") as fh:
            for r in csv.DictReader(fh):
                if week is not None and int(r["week"]) != week:
                    continue
                yield r["player"], r["market"], r["label"], r["price"], r.get("point")
    return collect(gen()), {}


# ------------------------------------------------------------------ main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--slate-key")
    ap.add_argument("--file", help="archive props CSV instead of the API")
    ap.add_argument("--week", type=int, help="week filter for --file")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true", help="refetch even if props for this slate are < 6 h old")
    args = ap.parse_args()

    client = get_client(need_write=not args.dry_run)
    q = client.table("slates").select("*")
    q = q.eq("slate_key", args.slate_key) if args.slate_key else q.order("imported_at", desc=True).limit(1)
    slates = q.execute().data
    if not slates:
        raise SystemExit("no slate")
    slate = slates[0]
    sal = fetch_all(client.table("slate_salaries").select("site_player_id,player_id,player_name,position,team,salary")
                    .eq("slate_id", slate["slate_id"]), order="site_player_id")
    if not args.file:
        existing = client.table("slate_projections").select("updated_at").eq("slate_id", slate["slate_id"]).eq("method", "props").limit(1).execute().data
        if existing and not args.force:
            age = dt.datetime.now(dt.timezone.utc) - dt.datetime.fromisoformat(existing[0]["updated_at"].replace("Z", "+00:00"))
            if age < dt.timedelta(hours=6):
                print(f"props for {slate['slate_key']} are {age.seconds // 60} min old — skipping (use --force)")
                return
        key = os.environ.get("ODDS_API_KEY")
        if not key:
            print("ODDS_API_KEY not set — no props")
            return
        games = fetch_all(client.table("games").select("game_id,home_team,away_team,gameday")
                          .in_("game_id", list(slate.get("game_ids") or [])), order="game_id")
        props, _ = from_api(key, games)
    else:
        props, _ = from_file(args.file, args.week)

    idx = defaultdict(list)
    for s in sal:
        idx[norm_name(s["player_name"])].append(s)
    out, matched, unmatched = [], 0, []
    for n, (lines, td) in props.items():
        cands = idx.get(n)
        if not cands:
            unmatched.append(n)
            continue
        c = cands[0]
        if c["position"] not in CALIB:
            continue
        raw = raw_points(lines, td)
        out.append({"slate_id": slate["slate_id"], "site_player_id": c["site_player_id"], "player_id": c["player_id"],
                    "player_name": c["player_name"], "position": c["position"], "team": c["team"], "salary": c["salary"],
                    "method": "props", "mean": round(calibrated(c["position"], raw), 2), "median": round(raw, 2),
                    "components": {"lines": {k.replace("player_", ""): v for k, v in lines.items()}, "td_prob": round(td, 3), "raw": round(raw, 2)},
                    "updated_at": dt.datetime.now(dt.timezone.utc).isoformat()})
        matched += 1
    print(f"{slate['slate_key']}: {matched} players with props matched, {len(unmatched)} names not on the slate"
          + (f" (e.g. {', '.join(unmatched[:6])})" if unmatched else ""))
    top = sorted(out, key=lambda r: -r["mean"])[:12]
    for r in top:
        print(f"   {r['position']:3} {r['player_name']:<24} ${r['salary']:<6} props {r['mean']:5.1f}  (raw {r['median']:5.1f}, TD {r['components']['td_prob']:.2f})")
    if args.dry_run:
        print("dry run — no writes")
        return
    client.table("slate_projections").delete().eq("slate_id", slate["slate_id"]).eq("method", "props").execute()
    for batch in chunked(out):
        client.table("slate_projections").insert(batch).execute()
    print(f"done — {len(out)} props projections for {slate['slate_key']}")


if __name__ == "__main__":
    main()
