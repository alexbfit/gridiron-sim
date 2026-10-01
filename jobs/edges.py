"""
Prop / prediction-market edge screener (research) -> web/data/edges.json + web/edges.html.

For every live Kalshi NFL player market on the latest slate's gameday (every rung of every ladder, yes AND no
side) and, when ODDS_API_KEY is set, every sportsbook Over/Under player prop from The Odds API, compare the
market's probability with OUR probability for the same event and rank by expected value.

  python jobs/edges.py --out web/data/edges.json            # latest slate (workflows run this from the repo root)
  python jobs/edges.py --slate-key DK-2026-04-main --all     # keep every rung, not just |edge| >= 3 pts
  python jobs/edges.py --fit-cv                              # also print the market-implied CV per market (tuning aid)

OUR PROBABILITY — how a stat distribution is built when no per-stat sim draws exist
-----------------------------------------------------------------------------------
The stored sim matrix (slates.sim_meta.url) holds DraftKings POINTS per sim only; the per-stat draws (pass yds,
rec yds, receptions, TDs ...) are never stored. What IS stored, per player:
  * slate_projections[method=sim].components.stats — the sim's MEAN stat line (passing_yards, passing_tds,
    rushing_yards, receiving_yards, receptions, rushing_tds, receiving_tds), unconditional on activity
    (a Questionable player's means are already scaled by his ACTIVE_PROB in simulate.py);
  * slate_projections[method=props].components.lines / td_prob — the median sportsbook line per market and the
    de-vigged anytime-TD probability, plus `src` saying whether a sportsbook ("odds") or only Kalshi fed it.
So each stat gets a parametric distribution around a blended mean:
  mean  = PROPS_W * props_mean + (1 - PROPS_W) * sim_mean   when a SPORTSBOOK line exists (same 0.85 pull the
          lineup builder uses, which backtested best) -> confidence "props+sim".
          Without a sportsbook line (every refresh but Sunday 7 AM) the only independent input is the sim, whose
          stat means are known to be weaker than market lines (props backtest: r .48 vs .35). A belief that
          ignores the market entirely would overstate every disagreement, so sim-only players are SHRUNK half-way
          toward the Kalshi-implied mean stored in the Kalshi-only props row: mean = (1 - SIM_ONLY_SHRINK) * sim
          + SIM_ONLY_SHRINK * kalshi_mean (--shrink, default 0.5; 0 = pure sim). This is partly circular — the
          Kalshi line is the market being screened — which is why those rows carry confidence "sim-only" and
          the page says so. Sim means are divided by ACTIVE_PROB[status] so the distribution is conditional on
          playing (Kalshi settles at the pre-game fair price if a player is active but never takes a snap, and the
          whole screen is moot if he is out). |edge| >= BIG_EDGE is flagged "large-disagreement": in practice that
          is news the sim has not seen (a starter ruled out, a depth-chart change), not a mispriced market.
  shape = lognormal for yardage (right-skewed, positive; median line -> mean via exp(sigma^2 / 2)),
          Poisson for counts (receptions, passing TDs; anytime TD = P(Poisson(rush_td + rec_td) >= 1)).
  CV    = SHAPES[market][position], a tunable table. There is no in-repo history of per-stat outcomes to fit
          these on, so the defaults are the well-known NFL per-game dispersions for players who play: a QB's
          passing yards run ~N(240, 75) -> CV ~0.30; a lead RB's rushing yards ~N(65, 35) -> ~0.55; a WR's
          receiving yards are the widest (~60 +/- 40 -> ~0.65, TE ~0.70, RB receiving ~0.80). The multi-sport
          engine's jobs/sports/shapes.json is DK-points ratios, not per-stat, so it is not used here.
          `--fit-cv` fits a lognormal to each Kalshi ladder and prints the median market-implied CV per
          market x position next to the table value, which is how to re-tune it. On the 2026 week-4 ladders the
          market implied QB pass yds 0.30 (table 0.30 — the method checks out), RB rush 0.64, QB rush 1.03,
          WR rec 0.86, TE rec 0.82, RB rec 1.02; the table below sits between the textbook values and those
          fits because the mids of wide tail quotes (2c bid / 8c ask) inflate the implied tails.
  dispersion scale: a player whose simulated DK points are unusually spread for his position (usage in doubt,
          committee backfield) gets a proportionally wider yardage distribution: cv *= clip(player_cv / position
          median cv, DISP_CLIP), computed on non-zero draws of the stored matrix so a Questionable player's zero
          mass is not double counted. DISP_SCALE = False turns this off.

MARKET PROBABILITY, EV, STAKE
  Kalshi: mid of yes bid / yes ask (MAX_SPREAD from kalshi.py; wider = no real market). The side you would
          actually buy pays its ASK plus Kalshi's taker fee, 0.07 * price * (1 - price) per contract (~1.75c at
          50c, i.e. ~3.5% of the stake) — so EV uses the ask + fee, not the mid.
  Sportsbook: Over/Under de-vigged within one book at the same point (p_over / (p_over + p_under)); EV at the
          book's actual price. Anytime-TD "Yes" is one-sided -> TD_DEVIG from props.py.
  edge  = (our_p - market_p) * 100, in probability points, for the side we would take.
  ev    = our_p * payout_per_$1 - 1   (Kalshi: 1 / (ask + fee); book: decimal odds).
  kelly = KELLY_FRAC * max(0, (p*b - q) / b), capped at KELLY_CAP (2% of bankroll) — a fractional-Kelly
          SUGGESTION on a research screen, not advice.
Only rows with |edge| >= --min-edge (default 3 pts) and a usable "our" input are written.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import os
import sys
from collections import Counter, defaultdict

import numpy as np

from common import fetch_all, get_client, norm_name

# ------------------------------------------------------------------ tunables
PROPS_W = 0.85                    # weight on the sportsbook-implied mean when a sportsbook line exists
SIM_ONLY_SHRINK = 0.5             # sim-only players: weight on the Kalshi-implied mean (see docstring)
BIG_EDGE = 25.0                   # |edge| above this = the sim is missing news, flagged on the page
ACTIVE_PROB = {"OUT": 0.0, "IR": 0.0, "O": 0.0, "D": 0.5, "Q": 0.9}   # mirrors simulate.py
MIN_EDGE = 3.0                    # probability points
KELLY_FRAC, KELLY_CAP = 0.25, 0.02
KALSHI_FEE = 0.07                 # taker fee = KALSHI_FEE * p * (1 - p) per $1 contract (series fee_type "quadratic")
TD_DEVIG = 0.88                   # one-sided anytime-TD "Yes" price (props.py)
DISP_SCALE, DISP_CLIP = True, (0.8, 1.3)
MIN_MEAN = {"yards": 5.0, "count": 0.2, "td": 0.03}   # below this our input is too thin to price a rung
# market -> distribution family and CV by position ("*" = fallback). CVs are for the stat GIVEN the player plays.
SHAPES = {
    "player_pass_yds":      {"dist": "lognormal", "cv": {"QB": 0.30, "*": 0.45}},
    "player_rush_yds":      {"dist": "lognormal", "cv": {"RB": 0.60, "QB": 0.85, "WR": 0.95, "TE": 0.95, "*": 0.75}},
    "player_reception_yds": {"dist": "lognormal", "cv": {"WR": 0.75, "TE": 0.75, "RB": 0.90, "QB": 1.0, "*": 0.80}},
    "player_receptions":    {"dist": "poisson"},
    "player_pass_tds":      {"dist": "poisson"},
    "anytime_td":           {"dist": "poisson"},          # rungs are "1+" (and occasionally "2+") touchdowns
}
# market -> (sim components.stats key(s), props components.lines key)
STAT_KEYS = {
    "player_pass_yds": (("passing_yards",), "pass_yds"),
    "player_pass_tds": (("passing_tds",), "pass_tds"),
    "player_rush_yds": (("rushing_yards",), "rush_yds"),
    "player_reception_yds": (("receiving_yards",), "reception_yds"),
    "player_receptions": (("receptions",), "receptions"),
    "anytime_td": (("rushing_tds", "receiving_tds"), None),
}
MARKET_LABEL = {"player_pass_yds": "Pass yds", "player_pass_tds": "Pass TDs", "player_rush_yds": "Rush yds",
                "player_reception_yds": "Rec yds", "player_receptions": "Receptions", "anytime_td": "Anytime TD"}
KALSHI_WEB = "https://kalshi.com/markets/{series}/{slug}/{event}"
SERIES_SLUG = {"KXNFLPASSYDS": "pro-football-passing-yards", "KXNFLPASSTDS": "pro-football-passing-touchdowns",
               "KXNFLRSHYDS": "pro-football-rushing-yards", "KXNFLRECYDS": "pro-football-receiving-yards",
               "KXNFLREC": "pro-football-receptions", "KXNFLTD": "pro-football-touchdown-scorer"}


# ------------------------------------------------------------------ probability math (unit-tested)
def _phi(z: float) -> float:
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def lognormal_params(mean: float, cv: float) -> tuple[float, float]:
    """(mu, sigma) of a lognormal with the given MEAN and coefficient of variation."""
    s2 = math.log(1.0 + cv * cv)
    return math.log(mean) - s2 / 2.0, math.sqrt(s2)


def lognormal_sf(mean: float, cv: float, t: float) -> float:
    """P(X > t) for lognormal X with mean `mean` and CV `cv`."""
    if mean <= 0:
        return 0.0
    if t <= 0:
        return 1.0
    mu, s = lognormal_params(mean, cv)
    return 1.0 - _phi((math.log(t) - mu) / s)


def median_to_mean(median: float, cv: float) -> float:
    """A lognormal's mean from its median: median * exp(sigma^2 / 2)."""
    return median * math.sqrt(1.0 + cv * cv)


def poisson_ge(lam: float, k: int) -> float:
    """P(X >= k) for X ~ Poisson(lam)."""
    if k <= 0:
        return 1.0
    if lam <= 0:
        return 0.0
    term, cdf = math.exp(-lam), 0.0
    for i in range(k):
        if i:
            term *= lam / i
        cdf += term
    return max(0.0, 1.0 - cdf)


def devig(p_over: float, p_under: float) -> float:
    """Fair P(over) from two implied probabilities that sum to > 1 (multiplicative de-vig)."""
    return p_over / (p_over + p_under)


def kalshi_fee(price: float) -> float:
    return KALSHI_FEE * price * (1.0 - price)


def ev_per_dollar(our_p: float, payout: float) -> float:
    """payout = what $1 staked returns if the bet wins (stake included). EV per $1 staked."""
    return our_p * payout - 1.0


def kelly(our_p: float, payout: float, frac: float = KELLY_FRAC, cap: float = KELLY_CAP) -> float:
    """Fractional Kelly stake as a share of bankroll; b = net odds = payout - 1."""
    b = payout - 1.0
    if b <= 0:
        return 0.0
    f = (our_p * b - (1.0 - our_p)) / b
    return float(min(cap, max(0.0, f) * frac))


def over_prob(market: str, pos: str, mean: float, threshold: float, cv_scale: float = 1.0) -> tuple[float, float | None]:
    """(P(stat > threshold), cv used). threshold is the market's continuous cut (49.5 for "50+"; a book's 49.5)."""
    shape = SHAPES[market]
    if shape["dist"] == "lognormal":
        cvs = shape["cv"]
        cv = cvs.get(pos, cvs["*"]) * cv_scale
        return lognormal_sf(mean, cv, threshold), cv
    if shape["dist"] == "poisson":
        return poisson_ge(mean, int(math.floor(threshold)) + 1), None
    raise ValueError(shape["dist"])


def fit_lognormal_cv(points: list[tuple[float, float]]) -> float | None:
    """Market-implied CV: least-squares fit of a lognormal survival curve to [(threshold, P(X > threshold))]."""
    pts = [(t, p) for t, p in points if t > 0 and 0.02 < p < 0.98]
    if len(pts) < 3:
        return None
    best, best_err = None, None
    for cv in (x / 100 for x in range(10, 151, 2)):
        s2 = math.log(1 + cv * cv); s = math.sqrt(s2)
        # closed-form mu for this sigma (least squares on the probit scale)
        z = [math.log(t) - s * _inv_phi(1 - p) for t, p in pts]
        mu = sum(z) / len(z)
        err = sum((1 - _phi((math.log(t) - mu) / s) - p) ** 2 for t, p in pts)
        if best_err is None or err < best_err:
            best, best_err = cv, err
    return best


def _inv_phi(p: float) -> float:
    """Inverse standard normal CDF (Acklam's rational approximation, |err| < 1e-9)."""
    p = min(max(p, 1e-9), 1 - 1e-9)
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
    q = p - 0.5; r = q * q
    return (((((a[0] * r + a[1]) * r + a[2]) * r + a[3]) * r + a[4]) * r + a[5]) * q / (((((b[0] * r + b[1]) * r + b[2]) * r + b[3]) * r + b[4]) * r + 1)


# ------------------------------------------------------------------ our inputs
def kalshi_web_url(mk: dict) -> str:
    series = (mk.get("event_ticker") or "").split("-")[0]
    return KALSHI_WEB.format(series=series.lower(), slug=SERIES_SLUG.get(series, series.lower()), event=(mk.get("event_ticker") or "").lower())


def player_inputs(sal: list[dict], sim_rows: list[dict], props_rows: list[dict], matrix: dict | None,
                  shrink: float = SIM_ONLY_SHRINK) -> dict:
    """{norm_name: {name, team, pos, site_player_id, status, means: {market: (mean, src)}, cv_scale}}.
    means are conditional on playing; src is "props+sim" when a sportsbook line informed it, "sim+kalshi" when the
    sim was shrunk toward a Kalshi-only line, "sim" when the sim is all there is."""
    by_pid = {s["site_player_id"]: s for s in sal}
    sim = {r["site_player_id"]: r for r in sim_rows}
    props = {r["site_player_id"]: r for r in props_rows}
    # dispersion scale: player DK CV (non-zero draws) relative to his position's median
    cvs, scale = {}, {}
    if DISP_SCALE and matrix:
        for pid, x in matrix.items():
            s = by_pid.get(pid)
            if s is None:
                continue
            nz = x[x > 0]
            if nz.size >= 50 and nz.mean() > 0:
                cvs[pid] = (s["position"], float(nz.std() / nz.mean()))
        med = {}
        for pos in {p for p, _ in cvs.values()}:
            vals = [c for p, c in cvs.values() if p == pos]
            med[pos] = float(np.median(vals)) if vals else None
        for pid, (pos, c) in cvs.items():
            if med.get(pos):
                scale[pid] = float(np.clip(c / med[pos], *DISP_CLIP))
    out = {}
    for pid, s in by_pid.items():
        if s["position"] not in ("QB", "RB", "WR", "TE"):
            continue
        sr, pr = sim.get(pid), props.get(pid)
        stats = ((sr or {}).get("components") or {}).get("stats") or {}
        status = (((sr or {}).get("components") or {}).get("status") or s.get("status") or "").upper()
        p_act = ACTIVE_PROB.get(status, 1.0)
        if p_act <= 0 or (not stats and not pr):
            continue
        comp = (pr or {}).get("components") or {}
        src = comp.get("src") or []
        book = pr is not None and "odds" in src
        kalshi_only = pr is not None and not book and "kalshi" in src and shrink > 0
        lines, td_prob = comp.get("lines") or {}, comp.get("td_prob")
        means = {}
        for market, (skeys, pkey) in STAT_KEYS.items():
            sim_mean = sum(float(stats.get(k) or 0.0) for k in skeys) / p_act if stats else None
            props_mean = None
            if book or kalshi_only:
                if market == "anytime_td" and td_prob:
                    props_mean = -math.log(max(1e-9, 1.0 - float(td_prob)))      # Poisson rate with P(>=1) = td_prob
                elif pkey and lines.get(pkey) is not None:
                    v = float(lines[pkey])
                    if SHAPES[market]["dist"] == "lognormal":
                        cvs_ = SHAPES[market]["cv"]
                        v = median_to_mean(v, cvs_.get(s["position"], cvs_["*"]))
                    props_mean = v
            if props_mean is not None and sim_mean is not None and book:
                means[market] = (PROPS_W * props_mean + (1 - PROPS_W) * sim_mean, "props+sim")
            elif props_mean is not None and sim_mean is not None:
                means[market] = ((1 - shrink) * sim_mean + shrink * props_mean, "sim+kalshi")
            elif props_mean is not None and book:
                means[market] = (props_mean, "props")
            elif sim_mean is not None:
                means[market] = (sim_mean, "sim")
        out[norm_name(s["player_name"])] = {"name": s["player_name"], "team": s["team"], "pos": s["position"], "site_player_id": pid,
                                            "status": status or None, "means": means, "cv_scale": scale.get(pid, 1.0),
                                            "props_src": comp.get("src") if pr else None}
    return out


def usable(market: str, mean: float) -> bool:
    kind = "td" if market == "anytime_td" else ("count" if SHAPES[market]["dist"] == "poisson" else "yards")
    return mean >= MIN_MEAN[kind]


# ------------------------------------------------------------------ markets
def fetch_kalshi_rungs(gameday: dt.date) -> tuple[list[dict], Counter]:
    """Every open Kalshi rung for games on `gameday`: [{name, market, series, threshold, yes_bid, yes_ask, ticker, event_ticker, title, url}]."""
    import kalshi
    out, counts = [], Counter()
    for series, market in kalshi.SERIES.items():
        try:
            mks = kalshi.fetch_series(series)
        except Exception as e:
            print(f"  kalshi {series}: {e}")
            continue
        for mk in mks:
            if kalshi.event_date(mk.get("event_ticker", "")) != gameday:
                continue
            title = mk.get("title") or ""
            name = title.split(":")[0].strip() if ":" in title else None
            try:
                b, a = float(mk["yes_bid_dollars"]), float(mk["yes_ask_dollars"])
            except (KeyError, TypeError, ValueError):
                continue
            if not name or mk.get("floor_strike") is None:
                continue
            counts[series] += 1
            out.append({"name": name, "norm": norm_name(name), "market": market, "series": series, "threshold": float(mk["floor_strike"]),
                        "yes_bid": b, "yes_ask": a, "ticker": mk.get("ticker"), "event_ticker": mk.get("event_ticker"), "title": title,
                        "url": kalshi_web_url(mk)})
    return out, counts


def fetch_book_rungs(key: str, slate_games: list[dict]) -> list[dict]:
    """Sportsbook Over/Under player props via The Odds API (props.py's helpers), one row per (player, market, point, book)."""
    import props as P
    events, remaining, _ = P.api_get("events", {"apiKey": key})
    want = {(g["home_team"], g["away_team"]) for g in slate_games}
    picked = [ev for ev in events if (P.NAME_TO_ABBR.get(ev["home_team"]), P.NAME_TO_ABBR.get(ev["away_team"])) in want]
    need = len(picked) * len(P.MARKETS) + 10
    if remaining is not None and int(remaining) < need:
        print(f"  sportsbook props skipped: {remaining} Odds API credits left, need ~{need}")
        return []
    out = []
    for ev in picked:
        try:
            data, remaining, _ = P.api_get(f"events/{ev['id']}/odds", {"apiKey": key, "regions": "us", "markets": ",".join(P.MARKETS), "oddsFormat": "american"})
        except Exception as e:
            print(f"  {ev.get('away_team')} @ {ev.get('home_team')}: {e}")
            continue
        for book in data.get("bookmakers", []):
            for m in book.get("markets", []):
                mk = "anytime_td" if m["key"] == "player_anytime_td" else m["key"]
                if mk not in SHAPES:
                    continue
                pairs = defaultdict(dict)
                for o in m.get("outcomes", []):
                    if o.get("price") in (None, ""):
                        continue
                    pairs[(o.get("description", ""), o.get("point"))][o.get("name")] = float(o["price"])
                for (player, point), sides in pairs.items():
                    if mk == "anytime_td" and "Yes" in sides:
                        out.append({"name": player, "norm": norm_name(player), "market": mk, "book": book["key"], "threshold": 0.5,
                                    "side": "yes", "price": sides["Yes"], "market_p": P.implied(sides["Yes"]) * TD_DEVIG, "url": None})
                    elif "Over" in sides and "Under" in sides and point not in (None, ""):
                        po, pu = P.implied(sides["Over"]), P.implied(sides["Under"])
                        fair = devig(po, pu)
                        for side, price, mp in (("over", sides["Over"], fair), ("under", sides["Under"], 1 - fair)):
                            out.append({"name": player, "norm": norm_name(player), "market": mk, "book": book["key"], "threshold": float(point),
                                        "side": side, "price": price, "market_p": mp, "url": None})
    print(f"  sportsbook: {len(out)} priced sides from {len(picked)} events · credits remaining {remaining}")
    return out


def american_payout(price: float) -> float:
    return 1 + (price / 100 if price > 0 else 100 / -price)


# ------------------------------------------------------------------ evaluation
def evaluate(kalshi_rungs: list[dict], book_rungs: list[dict], players: dict, min_edge: float = MIN_EDGE,
             kelly_frac: float = KELLY_FRAC) -> tuple[list[dict], dict]:
    """Rows for every priced side whose |edge| >= min_edge and whose "our" input exists; stats for the report."""
    import kalshi as K
    rows, st = [], Counter()
    names_matched = set()

    def our(pl, market, threshold):
        mean_src = pl["means"].get(market)
        if not mean_src or not usable(market, mean_src[0]):
            return None
        mean, src = mean_src
        p, cv = over_prob(market, pl["pos"], mean, threshold, pl["cv_scale"] if SHAPES[market]["dist"] == "lognormal" else 1.0)
        return p, mean, src, cv

    def flags(src, edge):
        f = [] if src == "props+sim" else ["sim-only"]
        if abs(edge) >= BIG_EDGE:
            f.append("large-disagreement")
        return f

    def rung_label(market, threshold, side):
        if market == "anytime_td" and threshold < 1:
            return "1+ TD" if side in ("yes", "over") else "no TD"
        if SHAPES[market]["dist"] == "poisson":
            k = int(math.floor(threshold)) + 1
            return f"{k}+" if side in ("yes", "over") else f"fewer than {k}"
        if float(threshold).is_integer():
            return f"{int(threshold)}{'+' if side in ('yes', 'over') else ' or fewer'}"
        return f"{'over' if side in ('yes', 'over') else 'under'} {threshold:g}"

    for r in kalshi_rungs:
        st["kalshi_rungs"] += 1
        pl = players.get(r["norm"])
        if not pl:
            st["kalshi_unmatched"] += 1
            continue
        names_matched.add(r["norm"])
        st["kalshi_matched"] += 1
        b, a = r["yes_bid"], r["yes_ask"]
        if b <= 0 or a >= 1 or a - b > K.MAX_SPREAD:
            st["kalshi_no_market"] += 1
            continue
        o = our(pl, r["market"], r["threshold"])
        if o is None:
            st["kalshi_no_input"] += 1
            continue
        p_yes, mean, src, cv = o
        mid = (a + b) / 2
        for side, our_p, market_p, price in (("yes", p_yes, mid, a), ("no", 1 - p_yes, 1 - mid, 1 - b)):
            edge = (our_p - market_p) * 100
            if edge < min_edge:
                continue
            payout = 1.0 / (price + kalshi_fee(price))
            rows.append({"player": pl["name"], "team": pl["team"], "pos": pl["pos"], "market": r["market"], "market_label": MARKET_LABEL[r["market"]],
                         "rung": rung_label(r["market"], r["threshold"], side), "threshold": r["threshold"], "side": side,
                         "our_p": round(our_p, 4), "market_p": round(market_p, 4), "price": round(price, 2), "spread": round(a - b, 2),
                         "edge": round(edge, 1), "ev": round(ev_per_dollar(our_p, payout), 4), "kelly": round(kelly(our_p, payout, kelly_frac), 4),
                         "source": "kalshi", "ticker": r["ticker"], "url": r["url"], "confidence": "props+sim" if src == "props+sim" else "sim-only",
                         "flags": flags(src, edge),
                         "inputs": {"our_mean": round(mean, 2), "mean_src": src, "cv": round(cv, 3) if cv else None, "status": pl["status"]}})
    for r in book_rungs:
        st["book_sides"] += 1
        pl = players.get(r["norm"])
        if not pl:
            st["book_unmatched"] += 1
            continue
        names_matched.add(r["norm"])
        st["book_matched"] += 1
        o = our(pl, r["market"], r["threshold"])
        if o is None:
            st["book_no_input"] += 1
            continue
        p_over, mean, src, cv = o
        our_p = p_over if r["side"] in ("over", "yes") else 1 - p_over
        edge = (our_p - r["market_p"]) * 100
        if edge < min_edge:
            continue
        payout = american_payout(r["price"])
        rows.append({"player": pl["name"], "team": pl["team"], "pos": pl["pos"], "market": r["market"], "market_label": MARKET_LABEL[r["market"]],
                     "rung": rung_label(r["market"], r["threshold"], r["side"]), "threshold": r["threshold"], "side": r["side"],
                     "our_p": round(our_p, 4), "market_p": round(r["market_p"], 4), "price": r["price"], "spread": None,
                     "edge": round(edge, 1), "ev": round(ev_per_dollar(our_p, payout), 4), "kelly": round(kelly(our_p, payout, kelly_frac), 4),
                     "source": f"book:{r['book']}", "ticker": None, "url": None, "confidence": "props+sim" if src == "props+sim" else "sim-only",
                     "flags": flags(src, edge),
                     "inputs": {"our_mean": round(mean, 2), "mean_src": src, "cv": round(cv, 3) if cv else None, "status": pl["status"]}})
    rows.sort(key=lambda x: -x["ev"])
    st["players_matched"] = len(names_matched)
    return rows, dict(st)


def implied_cv_report(kalshi_rungs: list[dict], players: dict) -> dict:
    """Median market-implied lognormal CV per (market, position) from the Kalshi ladders — compare with SHAPES."""
    ladders = defaultdict(list)
    for r in kalshi_rungs:
        pl = players.get(r["norm"])
        if not pl or SHAPES[r["market"]]["dist"] != "lognormal":
            continue
        b, a = r["yes_bid"], r["yes_ask"]
        if b <= 0 or a >= 1 or a - b > 0.25:
            continue
        ladders[(r["market"], pl["pos"], r["norm"])].append((r["threshold"], (a + b) / 2))
    fits = defaultdict(list)
    for (market, pos, _), pts in ladders.items():
        cv = fit_lognormal_cv(pts)
        if cv:
            fits[(market, pos)].append(cv)
    return {f"{m}/{p}": {"market_cv": round(float(np.median(v)), 2), "table_cv": SHAPES[m]["cv"].get(p, SHAPES[m]["cv"]["*"]), "n": len(v)}
            for (m, p), v in sorted(fits.items()) if len(v) >= 3}


# ------------------------------------------------------------------ main
def load_inputs(client, slate_key: str | None):
    from build_lineups import load_board
    slate, board, _ext, _own, matrix = load_board(client, slate_key)
    sal = fetch_all(client.table("slate_salaries").select("site_player_id,player_id,player_name,position,team,salary,status")
                    .eq("slate_id", slate["slate_id"]), order="site_player_id")
    sim_rows = fetch_all(client.table("slate_projections").select("site_player_id,mean,stdev,components")
                         .eq("slate_id", slate["slate_id"]).eq("method", "sim"), order="site_player_id")
    props_rows = fetch_all(client.table("slate_projections").select("site_player_id,mean,components")
                           .eq("slate_id", slate["slate_id"]).eq("method", "props"), order="site_player_id")
    games = fetch_all(client.table("games").select("game_id,home_team,away_team,gameday,gametime")
                      .in_("game_id", list(slate.get("game_ids") or [])), order="game_id")
    return slate, sal, sim_rows, props_rows, games, matrix


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--slate-key")
    ap.add_argument("--out", default="web/data/edges.json")
    ap.add_argument("--min-edge", type=float, default=MIN_EDGE, help="probability points (default 3)")
    ap.add_argument("--all", action="store_true", help="keep every priced side (min edge 0)")
    ap.add_argument("--kelly-frac", type=float, default=KELLY_FRAC)
    ap.add_argument("--shrink", type=float, default=SIM_ONLY_SHRINK, help="sim-only players: weight on the Kalshi-implied mean (0 = pure sim)")
    ap.add_argument("--no-books", action="store_true", help="skip The Odds API even if ODDS_API_KEY is set")
    ap.add_argument("--fit-cv", action="store_true", help="print the Kalshi-implied CV per market x position next to the table value")
    ap.add_argument("--top", type=int, default=25)
    args = ap.parse_args()
    min_edge = 0.0 if args.all else args.min_edge

    client = get_client()
    slate, sal, sim_rows, props_rows, games, matrix = load_inputs(client, args.slate_key)
    days = Counter(str(g.get("gameday"))[:10] for g in games if g.get("gameday"))
    if not days:
        raise SystemExit("slate has no games with a gameday")
    gameday = dt.date.fromisoformat(days.most_common(1)[0][0])
    players = player_inputs(sal, sim_rows, props_rows, matrix, args.shrink)
    n_book_props = sum(1 for p in players.values() if p["props_src"] and "odds" in p["props_src"])
    print(f"{slate['slate_key']}: {len(players)} skill players with inputs ({n_book_props} with sportsbook props, "
          f"{len(players) - n_book_props} sim-only) · gameday {gameday}")

    kal, counts = fetch_kalshi_rungs(gameday)
    print("  kalshi rungs on the slate: " + (", ".join(f"{k} {v}" for k, v in counts.items()) or "none"))
    books = []
    key = os.environ.get("ODDS_API_KEY")
    if key and not args.no_books:
        try:
            books = fetch_book_rungs(key, games)
        except Exception as e:
            print(f"  sportsbook props failed: {e}")
    elif not key:
        print("  ODDS_API_KEY not set — Kalshi only")

    rows, st = evaluate(kal, books, players, min_edge, args.kelly_frac)
    fits = implied_cv_report(kal, players) if args.fit_cv else {}
    out = {"slate": slate["slate_key"], "season": slate.get("season"), "week": slate.get("week"), "gameday": gameday.isoformat(),
           "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"), "min_edge": min_edge,
           "sources": {"kalshi": bool(kal), "sportsbook": bool(books)}, "stats": st, "kalshi_counts": dict(counts),
           "shapes": {m: s.get("cv") or s["dist"] for m, s in SHAPES.items()}, "params": {"props_w": PROPS_W, "kelly_frac": args.kelly_frac,
           "kelly_cap": KELLY_CAP, "kalshi_fee": KALSHI_FEE, "disp_scale": DISP_SCALE, "shrink": args.shrink, "big_edge": BIG_EDGE}, "implied_cv": fits, "rows": rows}
    os.makedirs(os.path.dirname(os.path.abspath(args.out)) or ".", exist_ok=True)
    with open(args.out, "w") as fh:
        json.dump(out, fh, separators=(",", ":"))
    print(f"  {st.get('kalshi_matched', 0)}/{st.get('kalshi_rungs', 0)} kalshi rungs matched a slate player ({st.get('players_matched', 0)} players); "
          f"{st.get('kalshi_no_market', 0)} without a real two-sided quote, {st.get('kalshi_no_input', 0)} without a usable input"
          + (f"; {st.get('book_matched', 0)}/{st.get('book_sides', 0)} sportsbook sides matched" if books else ""))
    if fits:
        print("  market-implied CV vs table (median over ladders):")
        for k, v in fits.items():
            print(f"    {k:<28} market {v['market_cv']:.2f}  table {v['table_cv']:.2f}  (n {v['n']})")
    print(f"  {len(rows)} sides with |edge| >= {min_edge:g} pts -> {args.out}")
    print(f"  {'player':<22} {'mkt':<10} {'rung':<12} {'side':<4} {'ours':>5} {'mkt':>5} {'edge':>5} {'EV':>6} {'kelly':>5} {'conf':<9} src")
    for r in rows[:args.top]:
        print(f"  {r['player']:<22} {r['market_label']:<10} {r['rung']:<12} {r['side']:<4} {r['our_p']:5.2f} {r['market_p']:5.2f} {r['edge']:+5.1f} "
              f"{r['ev']:+6.3f} {r['kelly']:5.3f} {r['confidence']:<9} {r['source']}{'  !' if 'large-disagreement' in r['flags'] else ''}")


if __name__ == "__main__":
    main()
