"""Read one slate of the SaberSim all-sports archive (DFS DATA/_sabersim_allsports) into the engine's shape.

Layout (see the archive README):  <root>/<SPORT>/<season>/players_<SPORT>_<date>.csv, meta_<SPORT>_<date>.json,
field_<SPORT>_<date>_<kind>.npz (every real entry of a DK contest: roster indexes, points, rank, payout).

A loaded slate is a dict:
  sport, date, players: [ {i, uuid, base, name, salary, pos (set of slot tokens), team, opp, game, proj, q: {25,50,75,85,95,99},
                            actual, own (real %), status, line, order, cpt} ... ],  meta
Players with no projection (SaberSim shows 0 for scratches / non-starters) keep proj 0 and are never rostered.
"""
from __future__ import annotations

import csv
import glob
import json
import os
import re

import numpy as np

from .rules import NO_POSITIONS

DEFAULT_ROOT = os.environ.get("SS_ALLSPORTS", os.path.expanduser("~/ssa"))
QS = (25, 50, 75, 85, 95, 99)


def _f(x, default=0.0):
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def slate_dates(sport: str, root: str = DEFAULT_ROOT) -> list[str]:
    out = []
    for p in glob.glob(f"{root}/{sport}/*/players_{sport}_*.csv"):
        out.append(re.search(r"_(\d{4}-\d{2}-\d{2})\.csv$", p).group(1))
    return sorted(out)


def _path(root, sport, date, kind):
    hits = glob.glob(f"{root}/{sport}/*/{kind}_{sport}_{date}.*")
    return hits[0] if hits else None


def load_slate(sport: str, date: str, root: str = DEFAULT_ROOT) -> dict:
    pf = _path(root, sport, date, "players")
    if not pf:
        raise FileNotFoundError(f"{sport} {date}: no players file under {root}")
    rows = list(csv.DictReader(open(pf, encoding="utf-8")))
    mf = _path(root, sport, date, "meta")
    meta = json.load(open(mf)) if mf else {}
    players = []
    for r in rows:
        uuid = r["uuid"]
        cpt = uuid.endswith("CPT")
        pos_s = r.get("pos") or ""
        if sport in NO_POSITIONS:
            pos = {"CPT"} if cpt else {"X"}
            if sport == "F1" and not cpt and (r.get("starting_pos") or "") in ("", "—", "-"):
                pos = {"CNSTR"}          # constructor rows have no grid position
        else:
            pos = set(p for p in pos_s.split("/") if p) or {"X"}
            if cpt:
                pos = {"CPT"}
        opp = (r.get("opp") or "").strip()
        home = opp.startswith("vs")
        opp_team = re.sub(r"^(vs|at)\s+", "", opp).strip()
        team = (r.get("team") or "").strip()
        if team and opp_team:
            game = "@".join(sorted([team, opp_team]))
        else:
            game = ""
        own = {k[4:]: _f(v) for k, v in r.items() if k.startswith("own_") and v not in ("", None)}
        q = {k: _f(r.get(f"dk_{k}_percentile")) for k in QS}
        players.append({
            "i": int(r["idx"]), "uuid": uuid, "base": uuid[:-3] if cpt else uuid, "cpt": cpt,
            "name": r["name"], "salary": int(_f(r.get("price"))), "pos": pos, "pos_str": pos_s,
            "team": team, "opp": opp_team, "home": home, "game": game,
            "proj": max(0.0, _f(r.get("ssProj"))), "q": q,
            "actual": _f(r.get("dk_actual"), None) if r.get("dk_actual") not in ("", None) else _f(r.get("actual"), None),
            "own": own, "ss_own": _f(r.get("ssOwn"), None) if r.get("ssOwn") not in ("", None) else None,
            "status": r.get("status") or "", "line": (r.get("linePosition") or "").strip(),
            "order": (r.get("order") or "").strip(), "tee": r.get("tee_time") or "",
        })
    return {"sport": sport, "date": date, "players": players, "meta": meta, "root": root}


def field_kinds(slate: dict) -> list[str]:
    root, sport, date = slate["root"], slate["sport"], slate["date"]
    return sorted(re.search(r"_([a-z0-9]+)\.npz$", p).group(1) for p in glob.glob(f"{root}/{sport}/*/field_{sport}_{date}_*.npz"))


class Field:
    """A real DK contest field: points of every entry + the real payout curve, so a lineup can be placed in it."""

    def __init__(self, slate: dict, kind: str):
        root, sport, date = slate["root"], slate["sport"], slate["date"]
        p = glob.glob(f"{root}/{sport}/*/field_{sport}_{date}_{kind}.npz")[0]
        z = np.load(p, allow_pickle=True)
        self.kind = kind
        self.pidx = z["pidx"]
        self.points = np.sort(z["points"].astype(np.float64))[::-1]
        self.n = len(self.points)
        self.payout = z["payout"].astype(np.float64)
        self.rank = z["rank"]
        c = (slate["meta"].get("contests") or {}).get(kind) or {}
        dk = c.get("dk") or {}
        self.fee = float(c.get("fee") or dk.get("entryFee") or 0.0)
        self.name = c.get("name") or dk.get("name") or kind
        self.max_entries = int(dk.get("maximumEntriesPerUser") or c.get("max") or 0)
        tiers = dk.get("payout_tiers") or []
        # prize by rank from the DK tiers; fall back to the field's own payout column (rank -> prize)
        self.prize_by_rank = np.zeros(self.n + 2)
        if tiers:
            for lo, hi, v in tiers:
                lo, hi = int(lo), min(int(hi), self.n)
                if lo <= self.n:
                    self.prize_by_rank[lo:hi + 1] = float(v)
        else:
            order = np.argsort(z["rank"])
            for r, v in zip(z["rank"][order], self.payout[order]):
                if 1 <= r <= self.n:
                    self.prize_by_rank[int(r)] = max(self.prize_by_rank[int(r)], float(v))

    def place(self, score: float) -> tuple[int, float, float]:
        """rank, prize ($, ties split), percentile of the field beaten (1.0 = beat everyone)."""
        above = int(np.searchsorted(-self.points, -score, side="left"))      # entries strictly above
        tied = int(np.searchsorted(-self.points, -score, side="right")) - above
        rank = above + 1
        if tied > 0:          # the real field already holds `tied` entries at this score; we join the split
            prize = float(self.prize_by_rank[rank:rank + tied + 1].sum() / (tied + 1))
        else:
            prize = float(self.prize_by_rank[rank]) if rank <= self.n else 0.0
        pct = 1.0 - above / self.n
        return rank, prize, pct
