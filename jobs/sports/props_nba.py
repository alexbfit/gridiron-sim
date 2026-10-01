"""NBA DraftKings projections from player props (The Odds API + Kalshi) -> the projections CSV build_lineups_sport.py reads.

    python jobs/sports/props_nba.py --dk data/slates/DKSalaries_NBA_2026-10-21.csv --out data/sports/nba/2026-10-21/projections.csv
    python jobs/sports/props_nba.py --dk DK.csv --props-json fixture.json --out proj.csv       # offline (tests, replays)
    python jobs/sports/props_nba.py --dk DK.csv --no-odds-api --out proj.csv                   # Kalshi only (free)

SOURCES
  The Odds API  (env ODDS_API_KEY; sport basketball_nba): one call to /events (free) and ONE per slate game to
                /events/<id>/odds with the markets below (cost = markets x regions = 8 credits per game, ~80 per
                10-game slate; the 500/month free tier covers ~6 slates, so --markets can trim the list and
                --max-games caps the spend). Without a key the step is skipped and only Kalshi is used.
  Kalshi        (free, no key): player ladders "Jalen Brunson: 25+ points" in series KXNBAPTS, KXNBAREB, KXNBAAST,
                KXNBA3PT, KXNBASTL, KXNBABLK, KXNBAPRA (also KXNBA2D / KXNBA3D double/triple-double yes-no markets,
                read for a sanity print only). jobs/kalshi.py's ladder math (survival-curve median, Poisson fit for
                integer ladders) is reused as-is.

LINES -> MEANS (per stat)
  A sportsbook line is the MEDIAN; DK scores the MEAN. For every stat the median point across books is shifted by the
  Over/Under prices (a juiced Over means the books' median sits above the point): mean0 = point + sd * z(p_over),
  where p_over is the de-vigged over probability averaged over books and sd the stat's own spread (DISP below).
  Then a skew factor turns the median into a mean: counting stats are right-skewed, so mean = median * (1 + SKEW):
  pts +1.5%, reb +3%, ast +4%, 3PM +8%, stl / blk +12%, TO +8% (negative-binomial medians sit below their means by
  roughly these fractions at typical NBA volumes). Low-count stats (3PM, stl, blk, TO) quoted at k.5 with prices
  instead use the Poisson mean implied by the de-vigged over price (props.poisson_mean), like the NFL engine does
  for pass TDs / receptions. Kalshi integer ladders give E[X] = sum_k P(X >= k) directly.

PER-PLAYER MONTE CARLO (DK points, percentiles, DD/TD probability)
  Each stat ~ NegativeBinomial(mean, var = DISP[stat] * mean) (DISP: pts 2.2, reb 1.8, ast 1.7, 3PM 1.35, stl 1.15,
  blk 1.2, TO 1.25 - the quasi-Poisson dispersions of NBA box scores; a 25-ppg player gets sd ~7.4, a 10-ppg one
  ~4.7). The stats share a latent minutes/usage factor through a Gaussian copula: z_stat = RHO[stat] * z_min +
  sqrt(1 - RHO^2) * eps (RHO: pts .55, reb .40, ast .40, 3PM .45, stl .30, blk .30, TO .50), so a big-minutes night
  lifts everything together and double-doubles are correlated events, not independent coin flips.
  DK NBA scoring per draw: pts + 0.5*3PM + 1.25*reb + 1.5*ast + 2*stl + 2*blk - 0.5*TO + 1.5*[>=2 of pts/reb/ast/stl/blk
  at 10+] + 3*[>=3 at 10+]. proj = mean of the draws; p25/50/75/85/95/99 from the same draws. The percentiles are
  therefore the player's OWN distribution; build_lineups_sport.py only falls back to shapes.json when a row has none.

COVERAGE
  A player needs a POINTS line to get a props projection. Missing secondary markets are filled from position-typical
  ratios to points (FILL below, flagged source=props_partial) so a thin book early in the day does not zero a starter.
  Players with no points line are left out of the CSV (build_lineups_sport treats them as 0 - never rostered) unless
  --fallback-proj gives a (name, proj) CSV for them; those rows carry proj only and shapes.json supplies the curve.
  Names are matched to the DK file with common.norm_name (suffixes, accents, punctuation) then name_key (first
  initial + last name). Output rows use the DK file's exact Name so the builder's own join is exact.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import math
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from collections import defaultdict
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from common import name_key, norm_name  # noqa: E402

ET = ZoneInfo("America/New_York")
ODDS_API = "https://api.the-odds-api.com/v4/sports/basketball_nba"
MARKETS = ["player_points", "player_rebounds", "player_assists", "player_threes", "player_blocks", "player_steals",
           "player_turnovers", "player_points_rebounds_assists"]
MARKET_STAT = {"player_points": "pts", "player_rebounds": "reb", "player_assists": "ast", "player_threes": "fg3",
               "player_blocks": "blk", "player_steals": "stl", "player_turnovers": "tov", "player_points_rebounds_assists": "pra"}
KALSHI_SERIES = {"KXNBAPTS": "pts", "KXNBAREB": "reb", "KXNBAAST": "ast", "KXNBA3PT": "fg3", "KXNBASTL": "stl", "KXNBABLK": "blk", "KXNBAPRA": "pra"}
KALSHI_EXTRA = {"KXNBA2D": "dd", "KXNBA3D": "td"}
STATS = ["pts", "reb", "ast", "fg3", "stl", "blk", "tov"]
COUNT_STATS = {"fg3", "stl", "blk", "tov"}                 # low counts quoted at k.5: price-implied Poisson mean
SKEW = {"pts": 0.015, "reb": 0.03, "ast": 0.04, "fg3": 0.08, "stl": 0.12, "blk": 0.12, "tov": 0.08, "pra": 0.015}
DISP = {"pts": 2.2, "reb": 1.8, "ast": 1.7, "fg3": 1.35, "stl": 1.15, "blk": 1.2, "tov": 1.25, "pra": 2.6}
RHO = {"pts": 0.55, "reb": 0.40, "ast": 0.40, "fg3": 0.45, "stl": 0.30, "blk": 0.30, "tov": 0.50}
DK = {"pts": 1.0, "fg3": 0.5, "reb": 1.25, "ast": 1.5, "stl": 2.0, "blk": 2.0, "tov": -0.5}
# ratios to points for a missing secondary market, by primary DK position (guards pass, bigs rebound/block)
FILL = {
    "G": {"reb": 0.20, "ast": 0.30, "fg3": 0.10, "stl": 0.055, "blk": 0.02, "tov": 0.11},
    "F": {"reb": 0.32, "ast": 0.16, "fg3": 0.08, "stl": 0.045, "blk": 0.035, "tov": 0.09},
    "C": {"reb": 0.60, "ast": 0.14, "fg3": 0.03, "stl": 0.04, "blk": 0.08, "tov": 0.10},
}
NAME_TO_ABBR = {
    "Atlanta Hawks": "ATL", "Boston Celtics": "BOS", "Brooklyn Nets": "BKN", "Charlotte Hornets": "CHA", "Chicago Bulls": "CHI",
    "Cleveland Cavaliers": "CLE", "Dallas Mavericks": "DAL", "Denver Nuggets": "DEN", "Detroit Pistons": "DET",
    "Golden State Warriors": "GS", "Houston Rockets": "HOU", "Indiana Pacers": "IND", "Los Angeles Clippers": "LAC",
    "LA Clippers": "LAC", "Los Angeles Lakers": "LAL", "Memphis Grizzlies": "MEM", "Miami Heat": "MIA", "Milwaukee Bucks": "MIL",
    "Minnesota Timberwolves": "MIN", "New Orleans Pelicans": "NO", "New York Knicks": "NY", "Oklahoma City Thunder": "OKC",
    "Orlando Magic": "ORL", "Philadelphia 76ers": "PHI", "Phoenix Suns": "PHO", "Portland Trail Blazers": "POR",
    "Sacramento Kings": "SAC", "San Antonio Spurs": "SA", "Toronto Raptors": "TOR", "Utah Jazz": "UTA", "Washington Wizards": "WAS",
}
ABBR_ALIASES = {"GSW": "GS", "NOP": "NO", "NYK": "NY", "SAS": "SA", "PHX": "PHO", "UTAH": "UTA", "BRK": "BKN", "CHO": "CHA", "WSH": "WAS"}
QS = (25, 50, 75, 85, 95, 99)
HEADER = ["name", "team", "opp", "pos", "salary", "proj", "p25", "p50", "p75", "p85", "p95", "p99", "p_dd", "p_td",
          "pts", "reb", "ast", "fg3", "stl", "blk", "tov", "n_markets", "source"]


# ------------------------------------------------------------------ odds math
def implied(price) -> float:
    p = float(price)
    return 100 / (p + 100) if p > 0 else -p / (-p + 100)


def z_of(p: float) -> float:
    """Inverse standard normal (Acklam's rational approximation; |err| < 1.2e-9) - no scipy in the workflow image."""
    p = min(max(p, 1e-6), 1 - 1e-6)
    a = [-3.969683028665376e+01, 2.209460984245205e+02, -2.759285104469687e+02, 1.383577518672690e+02, -3.066479806614716e+01, 2.506628277459239e+00]
    b = [-5.447609879822406e+01, 1.615858368580409e+02, -1.556989798598866e+02, 6.680131188771972e+01, -1.328068155288572e+01]
    c = [-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e+00, -2.549732539343734e+00, 4.374664141464968e+00, 2.938163982698783e+00]
    d = [7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e+00, 3.754408661907416e+00]
    if p < 0.02425:
        q = math.sqrt(-2 * math.log(p))
        return (((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / ((((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1)
    if p > 1 - 0.02425:
        q = math.sqrt(-2 * math.log(1 - p))
        return -(((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / ((((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1)
    q = p - 0.5
    r = q * q
    return (((((a[0] * r + a[1]) * r + a[2]) * r + a[3]) * r + a[4]) * r + a[5]) * q / (((((b[0] * r + b[1]) * r + b[2]) * r + b[3]) * r + b[4]) * r + 1)


def _pois_sf(lam: float, k: float) -> float:
    n = int(math.floor(k))
    term, cdf = math.exp(-lam), 0.0
    for i in range(n + 1):
        if i:
            term *= lam / i
        cdf += term
    return 1.0 - cdf


def poisson_mean(p_over: float, point: float) -> float:
    lo, hi = 0.01, 40.0
    for _ in range(50):
        mid = (lo + hi) / 2
        if _pois_sf(mid, point) < p_over:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def stat_sd(stat: str, mean: float) -> float:
    return math.sqrt(max(DISP[stat] * max(mean, 0.05), 1e-6))


def line_to_mean(stat: str, point: float, p_over: float | None) -> float:
    """Sportsbook point (+ de-vigged over probability when both prices exist) -> the stat's mean."""
    if p_over is not None and stat in COUNT_STATS and abs(point - round(point)) > 0.3:
        return poisson_mean(p_over, point)
    med = point
    if p_over is not None:
        med = point + stat_sd(stat, max(point, 0.5)) * z_of(p_over)
    return max(0.0, med * (1 + SKEW[stat]))


def collect_outcomes(outcomes) -> dict:
    """outcomes: iterable of (player, market, label, price, point, bookmaker) -> {norm_name: {stat: mean}}.
    Per book the Over/Under pair at the same point gives a de-vigged over probability; points are medians across books."""
    pts, ou = defaultdict(lambda: defaultdict(list)), defaultdict(dict)
    for player, market, label, price, point, book in outcomes:
        stat = MARKET_STAT.get(market)
        if not stat or point in (None, "") or label not in ("Over", "Under"):
            continue
        n = norm_name(player)
        if label == "Over":
            pts[n][stat].append(float(point))
        if price not in (None, ""):
            ou[(n, stat, book)][label] = (float(point), float(price))
    pover = defaultdict(list)
    for (n, stat, book), d in ou.items():
        if "Over" in d and "Under" in d and d["Over"][0] == d["Under"][0]:
            po, pu = implied(d["Over"][1]), implied(d["Under"][1])
            pover[(n, stat)].append((d["Over"][0], po / (po + pu)))
    out = {}
    for n, by in pts.items():
        rec = {}
        for stat, vals in by.items():
            point = float(np.median(vals))
            pr = pover.get((n, stat))
            p_over = float(np.mean([p for _, p in pr])) if pr else None
            if pr and stat in COUNT_STATS:          # average the price-implied Poisson means per book
                rec[stat] = float(np.mean([line_to_mean(stat, pt, p) for pt, p in pr]))
            else:
                rec[stat] = line_to_mean(stat, point, p_over)
        out[n] = rec
    return out


# ------------------------------------------------------------------ sources
def dk_games(dk_rows: list[dict]) -> dict:
    """{frozenset({away, home}): (away, home, start_et)} from the DK file's Game Info column."""
    games = {}
    for r in dk_rows:
        gi = r.get("Game Info") or ""
        m = re.match(r"(\w+)@(\w+)\s+(\d\d/\d\d/\d{4})\s+(\d\d:\d\d[AP]M)", gi)
        if m:
            a, h = m.group(1), m.group(2)
            try:
                start = dt.datetime.strptime(m.group(3) + " " + m.group(4), "%m/%d/%Y %I:%M%p").replace(tzinfo=ET)
            except ValueError:
                start = None
            games[frozenset((a, h))] = (a, h, start)
    return games


def odds_api_get(path: str, params: dict):
    q = urllib.parse.urlencode(params)
    with urllib.request.urlopen(f"{ODDS_API}/{path}?{q}", timeout=60) as r:
        return json.load(r), r.headers.get("x-requests-remaining"), r.headers.get("x-requests-used")


def from_odds_api(key: str, games: dict, markets: list[str], max_games: int = 0, log=print) -> tuple[list, dict]:
    """One /events call + one /events/<id>/odds per slate game. Returns (outcomes, info)."""
    events, remaining, _ = odds_api_get("events", {"apiKey": key})
    picked = []
    for ev in events:
        h = NAME_TO_ABBR.get(ev.get("home_team", "")); a = NAME_TO_ABBR.get(ev.get("away_team", ""))
        if h and a and frozenset((a, h)) in games:
            picked.append((ev, a, h))
    if max_games:
        picked = picked[:max_games]
    need = len(picked) * len(markets)
    log(f"  odds api: {len(picked)} of {len(events)} events match the slate; {need} credits needed, {remaining} remaining")
    if remaining is not None and int(remaining) < need:
        log(f"  not enough Odds API credits ({remaining} < {need}) - skipping the sportsbook pull")
        return [], {"events": len(picked), "credits": 0, "remaining": remaining}
    outcomes, used_total = [], 0
    for ev, a, h in picked:
        data, remaining, used = odds_api_get(f"events/{ev['id']}/odds", {"apiKey": key, "regions": "us", "markets": ",".join(markets), "oddsFormat": "american"})
        n = 0
        for book in data.get("bookmakers", []):
            for m in book.get("markets", []):
                for o in m.get("outcomes", []):
                    outcomes.append((o.get("description", ""), m["key"], o.get("name"), o.get("price"), o.get("point"), book.get("key")))
                    n += 1
        used_total += len(markets)
        log(f"  {a}@{h}: {n} outcomes from {len(data.get('bookmakers', []))} books (remaining {remaining})")
    return outcomes, {"events": len(picked), "credits": used_total, "remaining": remaining}


def from_kalshi(day: dt.date, log=print) -> tuple[dict, dict]:
    """{norm_name: {stat: mean}} from Kalshi's NBA ladders on `day`, plus {norm_name: {"dd": p, "td": p}}."""
    from kalshi import event_date, fetch_series, median_from_ladder, mid, poisson_fit
    ladders, counts, extra = defaultdict(list), {}, defaultdict(dict)
    for series, stat in {**KALSHI_SERIES, **KALSHI_EXTRA}.items():
        try:
            mks = fetch_series(series)
        except Exception as e:  # noqa: BLE001
            log(f"  kalshi {series}: {e}")
            continue
        n = 0
        for mk in mks:
            if event_date(mk.get("event_ticker", "")) != day:
                continue
            title = mk.get("title") or ""
            name = title.split(":")[0].strip() if ":" in title else None
            p = mid(mk)
            if not name or p is None:
                continue
            if stat in ("dd", "td"):
                extra[norm_name(name)][stat] = p; n += 1
                continue
            if mk.get("floor_strike") is None:
                continue
            ladders[(norm_name(name), stat)].append((float(mk["floor_strike"]) + 0.5, p))
            n += 1
        counts[series] = n
    log("  kalshi markets on the slate: " + (", ".join(f"{k} {v}" for k, v in counts.items()) or "none"))
    out = defaultdict(dict)
    for (n, stat), pts in ladders.items():
        lad = sorted((int(round(t)), p) for t, p in pts)
        if stat in COUNT_STATS or (lad and lad[-1][0] <= 12):
            kmin = lad[0][0]
            if kmin >= 1 and (kmin == 1 or lad[0][1] >= 0.85) and lad[-1][1] <= 0.15 and all(b[0] == a[0] + 1 for a, b in zip(lad, lad[1:])):
                mean = (kmin - 1) + sum(p for _, p in lad)           # E[X] = sum_k P(X >= k)
            else:
                mean = poisson_fit(lad)
        else:
            med = median_from_ladder(pts)
            mean = med * (1 + SKEW.get(stat, 0.02)) if med is not None else None
        if mean is not None:
            out[n][stat] = round(float(mean), 3)
    return dict(out), dict(extra)


def merge_sources(book: dict, kalshi: dict) -> dict:
    """Mean of the two sources per stat where both exist, else whichever exists."""
    out = {n: dict(v) for n, v in book.items()}
    for n, rec in kalshi.items():
        cur = out.setdefault(n, {})
        for stat, v in rec.items():
            cur[stat] = (cur[stat] + v) / 2 if stat in cur else v
    return out


# ------------------------------------------------------------------ monte carlo
def _nb_cdf_table(mean: float, disp: float, pmax: float = 0.99995) -> np.ndarray:
    """CDF of NegativeBinomial(mean, var = disp*mean) on 0..K (K where the CDF passes pmax). disp <= 1 -> Poisson."""
    mean = max(mean, 1e-3)
    K = int(mean + 12 * math.sqrt(disp * mean) + 10)
    k = np.arange(K + 1)
    if disp <= 1.0001:
        logp = -mean + k * math.log(mean) - np.array([math.lgamma(i + 1) for i in k])
    else:
        r = mean / (disp - 1.0)
        p = 1.0 / disp                                   # P(success); mean = r (1-p)/p
        logp = (np.array([math.lgamma(i + r) for i in k]) - math.lgamma(r) - np.array([math.lgamma(i + 1) for i in k])
                + r * math.log(p) + k * math.log(1 - p))
    cdf = np.cumsum(np.exp(logp))
    return np.minimum(cdf / max(cdf[-1], 1e-12) if cdf[-1] < pmax else cdf, 1.0)


def simulate_player(means: dict, n: int = 20000, rng: np.random.Generator | None = None) -> dict:
    """Correlated per-stat draws -> DK points. Returns proj, percentiles, P(DD), P(TD), per-stat means used."""
    rng = rng or np.random.default_rng(0)
    z_min = rng.standard_normal(n)
    draws = {}
    for stat in STATS:
        m = float(means.get(stat, 0.0) or 0.0)
        if m <= 0:
            draws[stat] = np.zeros(n)
            continue
        rho = RHO[stat]
        z = rho * z_min + math.sqrt(1 - rho * rho) * rng.standard_normal(n)
        u = 0.5 * (1.0 + np.vectorize(math.erf)(z / math.sqrt(2.0)))
        cdf = _nb_cdf_table(m, DISP[stat])
        draws[stat] = np.searchsorted(cdf, u, side="left").astype(np.float64)
    tens = sum((draws[s] >= 10).astype(np.int8) for s in ("pts", "reb", "ast", "stl", "blk"))
    dk = sum(DK[s] * draws[s] for s in STATS) + 1.5 * (tens >= 2) + 3.0 * (tens >= 3)
    q = np.percentile(dk, QS)
    return {"proj": float(dk.mean()), "q": {k: float(v) for k, v in zip(QS, q)},
            "p_dd": float((tens >= 2).mean()), "p_td": float((tens >= 3).mean()),
            "stats": {s: float(draws[s].mean()) for s in STATS}}


def primary_pos(pos_str: str) -> str:
    toks = [t for t in (pos_str or "").split("/") if t]
    for t in toks:
        if t in ("PG", "SG"):
            return "G"
        if t in ("SF", "PF"):
            return "F"
        if t == "C":
            return "C"
    return "F"


def complete_means(means: dict, pos_str: str) -> tuple[dict, bool]:
    """Fill missing secondary stats from position ratios to points; PRA minus known parts fills reb/ast when possible."""
    m = {k: float(v) for k, v in means.items() if k in STATS or k == "pra"}
    pts = m.get("pts")
    if pts is None:
        return {}, False
    partial = False
    if "pra" in m:
        rest = max(m["pra"] - pts, 0.0)
        if "reb" in m and "ast" not in m:
            m["ast"] = max(rest - m["reb"], 0.0); partial = True
        elif "ast" in m and "reb" not in m:
            m["reb"] = max(rest - m["ast"], 0.0); partial = True
        elif "reb" not in m and "ast" not in m:
            f = FILL[primary_pos(pos_str)]
            share = f["reb"] / (f["reb"] + f["ast"])
            m["reb"], m["ast"] = rest * share, rest * (1 - share); partial = True
    f = FILL[primary_pos(pos_str)]
    for s in STATS:
        if s not in m:
            m[s] = pts * f[s]; partial = True
    return {s: m[s] for s in STATS}, partial


def match_players(dk_rows: list[dict], means: dict) -> dict:
    """{dk row index: means} by norm_name, then by first-initial + last-name when unique."""
    by_norm = {norm_name(r.get("Name") or ""): i for i, r in enumerate(dk_rows)}
    by_key = defaultdict(list)
    for i, r in enumerate(dk_rows):
        by_key[name_key(r.get("Name") or "")].append(i)
    out = {}
    for n, rec in means.items():
        i = by_norm.get(n)
        if i is None:
            ks = by_key.get(name_key(n), [])
            i = ks[0] if len(ks) == 1 else None
        if i is not None:
            out[i] = rec
    return out


def build_rows(dk_rows: list[dict], means: dict, sims: int = 20000, seed: int = 0, fallback: dict | None = None) -> tuple[list[list], dict]:
    rng = np.random.default_rng(seed)
    matched = match_players(dk_rows, means)
    rows, n_full, n_partial, n_fb = [], 0, 0, 0
    for i, r in enumerate(dk_rows):
        name = r.get("Name") or ""
        team = ABBR_ALIASES.get(r.get("TeamAbbrev") or "", r.get("TeamAbbrev") or "")
        gi = r.get("Game Info") or ""
        mg = re.match(r"(\w+)@(\w+)", gi)
        opp = (mg.group(2) if team == mg.group(1) else mg.group(1)) if mg else ""
        pos_str = r.get("Roster Position") or r.get("Position") or ""
        rec = matched.get(i)
        if rec and rec.get("pts"):
            m, partial = complete_means(rec, pos_str)
            s = simulate_player(m, sims, rng)
            n_partial += partial; n_full += not partial
            rows.append([name, team, opp, pos_str, r.get("Salary") or "", round(s["proj"], 2)] + [round(s["q"][k], 2) for k in QS]
                        + [round(s["p_dd"], 3), round(s["p_td"], 3)] + [round(m[x], 2) for x in STATS]
                        + [len([k for k in rec if k in STATS or k == "pra"]), "props_partial" if partial else "props_nba"])
        elif fallback and norm_name(name) in fallback:
            n_fb += 1
            rows.append([name, team, opp, pos_str, r.get("Salary") or "", round(float(fallback[norm_name(name)]), 2)] + [""] * 6
                        + ["", ""] + [""] * 7 + [0, "fallback"])
    rows.sort(key=lambda x: -float(x[5]))
    return rows, {"players": len(rows), "full": n_full, "partial": n_partial, "fallback": n_fb, "dk_rows": len(dk_rows),
                  "unmatched_props": len(means) - len(matched)}


def write_csv(path: str, rows: list[list]):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(HEADER)
        w.writerows(rows)


def load_props_json(path: str) -> list:
    """Offline source: {"outcomes": [[player, market, label, price, point, book], ...]} or a saved /odds payload list."""
    d = json.load(open(path, encoding="utf-8"))
    if isinstance(d, dict) and "outcomes" in d:
        return [tuple(o) for o in d["outcomes"]]
    out = []
    for ev in (d if isinstance(d, list) else [d]):
        for book in ev.get("bookmakers", []):
            for m in book.get("markets", []):
                for o in m.get("outcomes", []):
                    out.append((o.get("description", ""), m["key"], o.get("name"), o.get("price"), o.get("point"), book.get("key")))
    return out


def run(dk_path: str, out_path: str, date: str | None = None, props_json: str | None = None, use_odds_api: bool = True,
        use_kalshi: bool = True, markets: list[str] | None = None, max_games: int = 0, sims: int = 20000, seed: int = 0,
        fallback_proj: str | None = None, log=print) -> dict:
    dk_rows = list(csv.DictReader(open(dk_path, encoding="utf-8-sig")))
    games = dk_games(dk_rows)
    starts = [s for _, _, s in games.values() if s]
    day = dt.date.fromisoformat(date) if date else (min(starts).date() if starts else dt.datetime.now(ET).date())
    markets = markets or MARKETS
    info = {"date": day.isoformat(), "games": len(games), "sources": []}
    book_means, kal_means, kal_extra = {}, {}, {}
    if props_json:
        book_means = collect_outcomes(load_props_json(props_json))
        info["sources"].append(f"file {Path(props_json).name} ({len(book_means)} players)")
    else:
        key = os.environ.get("ODDS_API_KEY")
        if use_odds_api and key:
            try:
                outcomes, oi = from_odds_api(key, games, markets, max_games, log)
                book_means = collect_outcomes(outcomes)
                info["sources"].append(f"odds api {oi['events']} games / {oi['credits']} credits ({len(book_means)} players)")
                info["odds_api"] = oi
            except Exception as e:  # noqa: BLE001
                log(f"  odds api failed: {e}")
        elif use_odds_api:
            log("  ODDS_API_KEY not set - sportsbook props skipped")
        if use_kalshi:
            try:
                kal_means, kal_extra = from_kalshi(day, log)
                info["sources"].append(f"kalshi ({len(kal_means)} players)")
            except Exception as e:  # noqa: BLE001
                log(f"  kalshi failed: {e}")
    means = merge_sources(book_means, kal_means)
    fallback = None
    if fallback_proj and os.path.exists(fallback_proj):
        fallback = {norm_name(r.get("name") or r.get("Name") or ""): float(r.get("proj") or 0) for r in csv.DictReader(open(fallback_proj, encoding="utf-8-sig"))}
        fallback = {k: v for k, v in fallback.items() if v > 0}
    rows, stats = build_rows(dk_rows, means, sims, seed, fallback)
    write_csv(out_path, rows)
    info.update(stats)
    log(f"  {stats['players']} projected players ({stats['full']} full props, {stats['partial']} partial, {stats['fallback']} fallback) "
        f"of {stats['dk_rows']} DK rows; {stats['unmatched_props']} prop names unmatched -> {out_path}")
    if kal_extra:
        chk = [(r[0], r[12], kal_extra[norm_name(r[0])]["dd"]) for r in rows if norm_name(r[0]) in kal_extra and "dd" in kal_extra[norm_name(r[0])] and r[12] != ""]
        if chk:
            diff = float(np.mean([abs(a - b) for _, a, b in chk]))
            log(f"  double-double check vs Kalshi KXNBA2D: {len(chk)} players, mean |P(sim) - P(kalshi)| = {diff:.3f}")
            info["dd_check"] = {"n": len(chk), "mean_abs_diff": round(diff, 3)}
    return info


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--dk", required=True, help="DraftKings salary CSV (fetch_dk_salaries.py --sport NBA)")
    ap.add_argument("--out", required=True, help="projections CSV for build_lineups_sport.py --proj")
    ap.add_argument("--date", help="slate date (ET); default: from the DK file's Game Info")
    ap.add_argument("--props-json", help="offline outcomes JSON instead of the live APIs")
    ap.add_argument("--no-odds-api", action="store_true")
    ap.add_argument("--no-kalshi", action="store_true")
    ap.add_argument("--markets", default=",".join(MARKETS), help="Odds API markets (comma list) - fewer = fewer credits")
    ap.add_argument("--max-games", type=int, default=0, help="cap the Odds API per-game calls (credits)")
    ap.add_argument("--sims", type=int, default=20000)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--fallback-proj", help="CSV (name, proj) for players without props; shapes.json gives their curve")
    ap.add_argument("--info-json", help="write the run summary here")
    a = ap.parse_args(argv)
    info = run(a.dk, a.out, a.date, a.props_json, not a.no_odds_api, not a.no_kalshi, [m for m in a.markets.split(",") if m],
               a.max_games, a.sims, a.seed, a.fallback_proj)
    if a.info_json:
        Path(a.info_json).parent.mkdir(parents=True, exist_ok=True)
        json.dump(info, open(a.info_json, "w"), indent=1)
    if info["players"] == 0:
        print("no projected players (no props found for this slate)", file=sys.stderr)
        sys.exit(3)


if __name__ == "__main__":
    main()
