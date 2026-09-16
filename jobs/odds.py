"""
Refresh spread / total for upcoming games from The Odds API (https://the-odds-api.com, free tier
500 requests/month — one request refreshes every game). Runs after ingest when ODDS_API_KEY is
set; otherwise exits quietly and the nflverse lines stand.

Books are averaged (consensus). nflverse convention is kept: spread_line > 0 means the HOME team
is favoured by that many points.

  python jobs/odds.py [--dry-run]
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import urllib.parse
import urllib.request

from common import fetch_all, get_client, norm_team

ODDS_URL = "https://api.the-odds-api.com/v4/sports/americanfootball_nfl/odds"
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


def fetch_odds(key: str) -> list[dict]:
    q = urllib.parse.urlencode({"apiKey": key, "regions": "us", "markets": "spreads,totals", "oddsFormat": "american"})
    with urllib.request.urlopen(f"{ODDS_URL}?{q}", timeout=60) as r:
        remaining = r.headers.get("x-requests-remaining")
        data = json.load(r)
    print(f"  {len(data)} events fetched · requests remaining this month: {remaining}")
    return data


def consensus(event: dict) -> tuple[float | None, float | None]:
    home, away = event["home_team"], event["away_team"]
    spreads, totals = [], []
    for book in event.get("bookmakers", []):
        for m in book.get("markets", []):
            if m["key"] == "spreads":
                for o in m["outcomes"]:
                    if o["name"] == home and o.get("point") is not None:
                        spreads.append(-float(o["point"]))          # home -3.5 -> home favoured by 3.5
            elif m["key"] == "totals":
                for o in m["outcomes"]:
                    if o["name"] == "Over" and o.get("point") is not None:
                        totals.append(float(o["point"]))
    avg = lambda xs: round(sum(xs) / len(xs) * 2) / 2 if xs else None
    return avg(spreads), avg(totals)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    key = os.environ.get("ODDS_API_KEY")
    if not key:
        print("ODDS_API_KEY not set — keeping nflverse lines")
        return
    client = get_client(need_write=not args.dry_run)
    events = fetch_odds(key)
    today = dt.date.today()
    games = fetch_all(client.table("games").select("game_id,season,week,home_team,away_team,gameday,spread_line,total_line")
                      .gte("gameday", (today - dt.timedelta(days=1)).isoformat())
                      .lte("gameday", (today + dt.timedelta(days=10)).isoformat()), order="game_id")
    by_pair = {(g["home_team"], g["away_team"]): g for g in games}
    updates = []
    for ev in events:
        h, a = NAME_TO_ABBR.get(ev["home_team"]), NAME_TO_ABBR.get(ev["away_team"])
        g = by_pair.get((h, a))
        if not g:
            continue
        spread, total = consensus(ev)
        if spread is None or total is None:
            continue
        updates.append({"game_id": g["game_id"], "spread_line": spread, "total_line": total,
                        "lines_source": "odds-api", "lines_updated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                        "_old": (g["spread_line"], g["total_line"])})
    for u in updates:
        old = u.pop("_old")
        moved = old[0] is None or abs(float(old[0]) - u["spread_line"]) >= 0.5 or abs(float(old[1] or 0) - u["total_line"]) >= 0.5
        print(f"  {u['game_id']}: spread {old[0]} -> {u['spread_line']}, total {old[1]} -> {u['total_line']}{'  *moved*' if moved else ''}")
        if not args.dry_run:
            client.table("games").update(u).eq("game_id", u["game_id"]).execute()
    print(f"{'would update' if args.dry_run else 'updated'} {len(updates)} games")


if __name__ == "__main__":
    main()
