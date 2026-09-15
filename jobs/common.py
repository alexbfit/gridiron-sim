"""Shared helpers for gridiron-sim jobs: Supabase access, name/team normalisation."""
from __future__ import annotations

import os
import re
import sys
import unicodedata

# DK / FD abbreviations -> nflverse abbreviations
TEAM_ALIASES = {
    "LAR": "LA", "JAC": "JAX", "WSH": "WAS", "OAK": "LV", "SD": "LAC", "STL": "LA",
    "GNB": "GB", "KAN": "KC", "NWE": "NE", "NOR": "NO", "SFO": "SF", "TAM": "TB",
}

DST_NAMES = {
    "ARI": "Cardinals", "ATL": "Falcons", "BAL": "Ravens", "BUF": "Bills", "CAR": "Panthers",
    "CHI": "Bears", "CIN": "Bengals", "CLE": "Browns", "DAL": "Cowboys", "DEN": "Broncos",
    "DET": "Lions", "GB": "Packers", "HOU": "Texans", "IND": "Colts", "JAX": "Jaguars",
    "KC": "Chiefs", "LA": "Rams", "LAC": "Chargers", "LV": "Raiders", "MIA": "Dolphins",
    "MIN": "Vikings", "NE": "Patriots", "NO": "Saints", "NYG": "Giants", "NYJ": "Jets",
    "PHI": "Eagles", "PIT": "Steelers", "SEA": "Seahawks", "SF": "49ers", "TB": "Buccaneers",
    "TEN": "Titans", "WAS": "Commanders",
}

_SUFFIXES = {"jr", "sr", "ii", "iii", "iv", "v"}


def norm_team(t: str | None) -> str | None:
    if not t:
        return t
    t = t.strip().upper()
    return TEAM_ALIASES.get(t, t)


def norm_name(name: str) -> str:
    """'Kenneth Walker III' -> 'kenneth walker'; 'D.J. Moore' -> 'dj moore'."""
    s = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    s = s.lower().replace("'", "").replace(".", "").replace("-", " ")
    s = re.sub(r"[^a-z0-9 ]", " ", s)
    parts = [p for p in s.split() if p and p not in _SUFFIXES]
    return " ".join(parts)


def name_key(name: str) -> str:
    """Even looser: first initial + last name ('k walker')."""
    parts = norm_name(name).split()
    if len(parts) < 2:
        return " ".join(parts)
    return parts[0][0] + " " + parts[-1]


def get_client(need_write: bool = False):
    """Supabase client. Uses the service-role key when present, else the anon key
    (reads only). Exits if a write is needed and no service key is set."""
    from supabase import create_client

    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY") or os.environ.get("SUPABASE_ANON_KEY")
    if not url or not key:
        print("Set SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY (or SUPABASE_ANON_KEY for reads)", file=sys.stderr)
        sys.exit(1)
    if need_write and not os.environ.get("SUPABASE_SERVICE_ROLE_KEY"):
        print("SUPABASE_SERVICE_ROLE_KEY is required to write", file=sys.stderr)
        sys.exit(1)
    return create_client(url, key)


def fetch_all(query, order: str | list[str] = "id", page: int = 1000) -> list[dict]:
    """Page through a PostgREST query (default cap is 1000 rows).
    `order` MUST make row order deterministic (primary key columns) — without it
    PostgREST returns overlapping pages and rows go missing."""
    for col in ([order] if isinstance(order, str) else order):
        query = query.order(col)
    out, start = [], 0
    while True:
        res = query.range(start, start + page - 1).execute()
        rows = res.data or []
        out.extend(rows)
        if len(rows) < page:
            return out
        start += page


def chunked(seq, n=500):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]
