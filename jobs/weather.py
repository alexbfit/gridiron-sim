"""
Kickoff weather forecasts for upcoming outdoor games -> games.forecast_* (Open-Meteo, free, no key).

  python jobs/weather.py            # every REG game in the next 8 days with roof outdoors/open
  python jobs/weather.py --dry-run

For each game: hourly forecast at the stadium for kickoff .. kickoff+3h (ET kickoff from gameday+gametime),
stored as forecast_wind (mph, mean sustained), forecast_gust (mph, max), forecast_temp (F at kickoff),
forecast_precip (inches over the game), forecast_pop (max precipitation probability %), forecast_at.
simulate.load_context passes these to the sim as ctx["weather"]; the sim's wind effect (WEATHER_STRENGTH)
trims passing efficiency and pass rate above WIND_FLOOR mph. Domes and closed roofs are skipped.

Evidence (665 outdoor games 2019-20/2023-24, nflverse game-time wind): the Vegas total already prices the
weather (total - line is flat across wind), but passing yards per attempt fall ~5-7% at 10-20 mph. On 2024
windy games (>= 12 mph, 248 player-games) turning the wind effect on cut QB MAE 4.59 -> 4.44 and QB bias
+1.0 -> +0.5; RB unchanged; WR/TE MAE slightly better. Small, but it is the right direction where it bites.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from zoneinfo import ZoneInfo

from common import fetch_all, get_client

ET = ZoneInfo("America/New_York")
OM = "https://api.open-meteo.com/v1/forecast"

# home-team stadium coordinates (2026). Domes are listed too; the games.roof column decides whether we look.
STADIUMS = {
    "ARI": (33.5276, -112.2626), "ATL": (33.7554, -84.4010), "BAL": (39.2780, -76.6227), "BUF": (42.7738, -78.7870),
    "CAR": (35.2258, -80.8528), "CHI": (41.8623, -87.6167), "CIN": (39.0954, -84.5160), "CLE": (41.5061, -81.6995),
    "DAL": (32.7473, -97.0945), "DEN": (39.7439, -105.0201), "DET": (42.3400, -83.0456), "GB": (44.5013, -88.0622),
    "HOU": (29.6847, -95.4107), "IND": (39.7601, -86.1639), "JAX": (30.3239, -81.6373), "KC": (39.0489, -94.4839),
    "LV": (36.0909, -115.1833), "LAC": (33.9535, -118.3392), "LA": (33.9535, -118.3392), "MIA": (25.9580, -80.2389),
    "MIN": (44.9736, -93.2575), "NE": (42.0909, -71.2643), "NO": (29.9511, -90.0812), "NYG": (40.8135, -74.0745),
    "NYJ": (40.8135, -74.0745), "PHI": (39.9008, -75.1675), "PIT": (40.4468, -80.0158), "SF": (37.4030, -121.9700),
    "SEA": (47.5952, -122.3316), "TB": (27.9759, -82.5033), "TEN": (36.1665, -86.7713), "WAS": (38.9077, -76.8645),
}
# international / neutral sites, matched on a word in games.stadium
NEUTRAL = {
    "wembley": (51.5560, -0.2796), "tottenham": (51.6043, -0.0664), "allianz": (48.2188, 11.6247),
    "deutsche bank": (50.0686, 8.6455), "frankfurt": (50.0686, 8.6455), "azteca": (19.3029, -99.1505),
    "arena corinthians": (-23.5453, -46.4742), "neo quimica": (-23.5453, -46.4742), "bernab": (40.4531, -3.6883),
    "croke": (53.3607, -6.2511), "melbourne": (-37.8200, 144.9834), "olympiastadion": (52.5147, 13.2395),
}


def coords(game) -> tuple[float, float] | None:
    name = (game.get("stadium") or "").lower()
    for k, v in NEUTRAL.items():
        if k in name:
            return v
    return STADIUMS.get(game["home_team"])


def kickoff_utc(game) -> dt.datetime:
    t = (game.get("gametime") or "13:00")[:5]
    local = dt.datetime.fromisoformat(f"{game['gameday']} {t}").replace(tzinfo=ET)
    return local.astimezone(dt.timezone.utc)


def fetch_hourly(points: list[tuple[float, float]], day_from: dt.date, day_to: dt.date) -> list[dict]:
    """One Open-Meteo call for every stadium (comma-separated coordinates) — one request per run instead of
    one per game, which matters because the free tier rate-limits by IP and shared runners share IPs."""
    q = urllib.parse.urlencode({
        "latitude": ",".join(f"{la:.4f}" for la, _ in points), "longitude": ",".join(f"{lo:.4f}" for _, lo in points),
        "timezone": "UTC", "start_date": day_from.isoformat(), "end_date": day_to.isoformat(),
        "hourly": "temperature_2m,precipitation,precipitation_probability,wind_speed_10m,wind_gusts_10m",
        "wind_speed_unit": "mph", "temperature_unit": "fahrenheit", "precipitation_unit": "inch",
    })
    for attempt in range(4):                       # 429 bursts, transient TLS/proxy hiccups
        try:
            with urllib.request.urlopen(f"{OM}?{q}", timeout=60) as r:
                d = json.load(r)
            return d if isinstance(d, list) else [d]
        except Exception:
            if attempt == 3:
                raise
            time.sleep(3.0 * (attempt + 1))


def window(hourly: dict, ko: dt.datetime) -> dict | None:
    """Game-window summary (kickoff - 30 min .. kickoff + 3 h) from one location's hourly block."""
    h = hourly
    times = [dt.datetime.fromisoformat(t).replace(tzinfo=dt.timezone.utc) for t in h["time"]]
    idx = [i for i, t in enumerate(times) if ko - dt.timedelta(minutes=30) <= t <= ko + dt.timedelta(hours=3)]
    if not idx:
        return None
    pick = lambda k, f: f([h[k][i] for i in idx if h[k][i] is not None] or [0])
    return {
        "forecast_wind": round(pick("wind_speed_10m", lambda v: sum(v) / len(v)), 1),
        "forecast_gust": round(pick("wind_gusts_10m", max), 1),
        "forecast_temp": round(h["temperature_2m"][idx[0]] if h["temperature_2m"][idx[0]] is not None else 60.0, 1),
        "forecast_precip": round(pick("precipitation", sum), 2),
        "forecast_pop": round(pick("precipitation_probability", max)),
    }


def forecast(lat, lon, ko: dt.datetime) -> dict | None:
    """Single-location convenience wrapper (tests)."""
    d = fetch_hourly([(lat, lon)], ko.date(), (ko + dt.timedelta(hours=4)).date())
    return window(d[0]["hourly"], ko)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=8)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    client = get_client(need_write=not args.dry_run)
    today = dt.date.today()
    games = fetch_all(client.table("games").select("game_id,home_team,away_team,gameday,gametime,roof,stadium,total_line")
                      .gte("gameday", (today - dt.timedelta(days=1)).isoformat())
                      .lte("gameday", (today + dt.timedelta(days=args.days)).isoformat())
                      .is_("home_score", "null"), order="game_id")
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    todo = []
    for g in games:
        if (g.get("roof") or "outdoors") not in ("outdoors", "open"):
            continue
        c = coords(g)
        if not c:
            print(f"  {g['game_id']}: no coordinates for {g.get('stadium')} / {g['home_team']}")
            continue
        todo.append((g, c, kickoff_utc(g)))
    if not todo:
        print("no upcoming outdoor games"); return
    try:
        blocks = fetch_hourly([c for _, c, _ in todo], min(k for _, _, k in todo).date(), (max(k for _, _, k in todo) + dt.timedelta(hours=4)).date())
    except Exception as e:
        print(f"forecast fetch failed: {e}"); return
    n = 0
    for (g, c, ko), blk in zip(todo, blocks):
        fc = window(blk["hourly"], ko)
        if not fc:
            continue
        flag = " *WIND*" if fc["forecast_wind"] >= 15 else (" *rain*" if fc["forecast_precip"] >= 0.1 and fc["forecast_pop"] >= 60 else "")
        print(f"  {g['game_id']}: wind {fc['forecast_wind']:.0f} mph (gusts {fc['forecast_gust']:.0f}), {fc['forecast_temp']:.0f}F, "
              f"precip {fc['forecast_precip']:.2f} in / {fc['forecast_pop']}%{flag}")
        if not args.dry_run:
            client.table("games").update({**fc, "forecast_at": now}).eq("game_id", g["game_id"]).execute()
        n += 1
    print(f"{'would update' if args.dry_run else 'updated'} {n} outdoor games")


if __name__ == "__main__":
    main()
