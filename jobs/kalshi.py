"""
Kalshi NFL player markets -> prop-style lines (free public API, no key, no credits).

Kalshi lists player yardage / reception / TD markets as LADDERS — "Amon-Ra St. Brown: 70+ receiving yards"
at 40, 50, 60 ... 160 — each a binary market with a two-sided quote. The mid of yes bid/ask is the market's
probability that the player clears that threshold, so a ladder is a survival curve S(t) = P(X >= t):

  * yards / receptions / passing TDs: the MEDIAN is where S crosses 0.5 (interpolated), which is what a
    sportsbook "line" is; props.py then treats it like one more book's line.
  * touchdowns (KXNFLTD "1+ touchdowns"): the mid IS the anytime-TD probability (no vig to strip).

Series used: KXNFLPASSYDS, KXNFLPASSTDS, KXNFLRSHYDS, KXNFLRECYDS, KXNFLREC, KXNFLTD. Public endpoint
GET https://api.elections.kalshi.com/trade-api/v2/markets?series_ticker=...&status=open (rate-limited:
~1 request/second, paginated by cursor). Markets are posted Tuesday/Wednesday for Sunday, so this can run
on every refresh, not just Sunday morning.

  python jobs/kalshi.py 2026-09-27          # print the implied lines for that gameday (debug)
"""
from __future__ import annotations

import datetime as dt
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

from common import norm_name

API = "https://api.elections.kalshi.com/trade-api/v2/markets"
SERIES = {                      # kalshi series -> the market key props.py uses
    "KXNFLPASSYDS": "player_pass_yds",
    "KXNFLPASSTDS": "player_pass_tds",
    "KXNFLRSHYDS": "player_rush_yds",
    "KXNFLRECYDS": "player_reception_yds",
    "KXNFLREC": "player_receptions",
    "KXNFLTD": "anytime_td",
}
MAX_SPREAD = 0.25               # ignore quotes wider than this (no real market)
COUNT_MARKETS = {"player_pass_tds", "player_receptions"}   # integer stats: quoted book-style as k - 0.5
MONTHS = {m: i for i, m in enumerate(["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"], 1)}


def _get(params: dict, tries: int = 5) -> dict:
    q = urllib.parse.urlencode(params)
    for attempt in range(tries):
        try:
            with urllib.request.urlopen(f"{API}?{q}", timeout=60) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code != 429 or attempt == tries - 1:
                raise
            time.sleep(2.0 * (attempt + 1))
        except Exception:
            if attempt == tries - 1:
                raise
            time.sleep(2.0 * (attempt + 1))


def fetch_series(series: str) -> list[dict]:
    out, cursor = [], None
    while True:
        params = {"series_ticker": series, "status": "open", "limit": 1000}
        if cursor:
            params["cursor"] = cursor
        d = _get(params)
        out += d.get("markets", [])
        cursor = d.get("cursor")
        time.sleep(0.8)
        if not cursor:
            return out


def event_date(event_ticker: str) -> dt.date | None:
    m = re.search(r"-(\d{2})([A-Z]{3})(\d{2})", event_ticker)
    if not m:
        return None
    return dt.date(2000 + int(m.group(1)), MONTHS[m.group(2)], int(m.group(3)))


def mid(mk: dict) -> float | None:
    try:
        b, a = float(mk["yes_bid_dollars"]), float(mk["yes_ask_dollars"])
    except (KeyError, TypeError, ValueError):
        return None
    if b <= 0 or a >= 1 or a - b > MAX_SPREAD:
        return None
    return (a + b) / 2


def median_from_ladder(points: list[tuple[float, float]]) -> float | None:
    """points = [(threshold, P(X >= threshold))] -> the value where the survival curve crosses 0.5.
    Thresholds are the market's "t+" integers (floor_strike + 0.5)."""
    pts = sorted(points)
    if len(pts) < 2:
        return None
    if pts[0][1] < 0.5:                                    # median below the lowest rung: S(0) = 1
        t1, p1 = pts[0]
        return t1 * (1 - 0.5) / max(1 - p1, 1e-6)
    for (t0, p0), (t1, p1) in zip(pts, pts[1:]):
        if p0 >= 0.5 >= p1:
            if p0 == p1:
                return (t0 + t1) / 2
            return t0 + (t1 - t0) * (p0 - 0.5) / (p0 - p1)
    return pts[-1][0]                                      # still above 0.5 at the top rung


def kalshi_lines(gameday: dt.date, verbose: bool = False) -> dict:
    """{norm_name: {"lines": {market: median}, "td": prob or None, "n_markets": k}} for games on `gameday`."""
    ladders: dict[tuple[str, str], list] = {}
    counts = {}
    for series, market in SERIES.items():
        try:
            mks = fetch_series(series)
        except Exception as e:
            print(f"  kalshi {series}: {e}")
            continue
        n = 0
        for mk in mks:
            if event_date(mk.get("event_ticker", "")) != gameday:
                continue
            title = mk.get("title") or ""
            name = title.split(":")[0].strip() if ":" in title else None
            p = mid(mk)
            if not name or p is None or mk.get("floor_strike") is None:
                continue
            thr = float(mk["floor_strike"]) + 0.5
            ladders.setdefault((norm_name(name), market), []).append((thr, p))
            n += 1
        counts[series] = n
    if verbose:
        print("  kalshi markets on the slate: " + ", ".join(f"{k} {v}" for k, v in counts.items()))
    out: dict[str, dict] = {}
    for (n, market), pts in ladders.items():
        rec = out.setdefault(n, {"lines": {}, "td": None, "n_markets": 0})
        if market == "anytime_td":
            one = [p for t, p in pts if abs(t - 1.0) < 0.01]
            if one:
                rec["td"] = one[0]; rec["n_markets"] += 1
            continue
        if market in COUNT_MARKETS:
            # integer stat: books quote k - 0.5 with the over near 50%; take the rung whose P(>= k) is closest to 0.5
            k, p = min(pts, key=lambda tp: abs(tp[1] - 0.5))
            med = k - 0.5
        else:
            med = median_from_ladder(pts)
        if med is not None:
            rec["lines"][market] = round(med, 2); rec["n_markets"] += 1
    return out


if __name__ == "__main__":
    day = dt.date.fromisoformat(sys.argv[1]) if len(sys.argv) > 1 else dt.date.today()
    res = kalshi_lines(day, verbose=True)
    print(f"{len(res)} players with Kalshi markets on {day}")
    for n, r in sorted(res.items(), key=lambda kv: -kv[1]["n_markets"])[:25]:
        print(f"  {n:<24} td {r['td'] if r['td'] is not None else '-':>5}  " + ", ".join(f"{k.replace('player_', '')}={v:g}" for k, v in r["lines"].items()))
