"""
Import third-party projections (4for4, ETR, RotoGrinders, your own spreadsheet...) for a slate.

Any CSV with a player-name column and a projection column works; ownership is optional.
Recognised headers (case-insensitive): name/player, team, pos/position, proj/projection/fpts/points,
own/ownership/%drafted. Rows are matched to the slate by normalised name (+ team/position if given).

  python jobs/import_projections.py data/projections/DK-2026-02-main_4for4.csv [--slate-key ...] [--source 4for4]

Writes slate_projections rows with method = 'external' (mean, and ownership in components).
The builder shows a "Blend external %" control when they exist; the board still prefers the sim.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import re
from collections import Counter

from common import chunked, fetch_all, get_client, norm_name, norm_team

ALIASES = {
    "name": ["name", "player", "player name", "player_name"],
    "team": ["team", "tm", "teamabbrev"],
    "pos": ["pos", "position"],
    "proj": ["proj", "projection", "fpts", "points", "dk proj", "dkproj", "fantasy points", "proj pts", "median"],
    "own": ["own", "own%", "ownership", "proj own", "%drafted", "pown"],
}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("csv")
    ap.add_argument("--slate-key")
    ap.add_argument("--source", default=None)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    with open(args.csv, encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        raise SystemExit("empty file")
    hdr = {h.lower().strip(): h for h in rows[0].keys()}
    col = {k: next((hdr[a] for a in al if a in hdr), None) for k, al in ALIASES.items()}
    if not col["name"] or not col["proj"]:
        raise SystemExit(f"need a name and a projection column; headers: {list(rows[0].keys())}")

    client = get_client(need_write=not args.dry_run)
    key = args.slate_key or (re.search(r"(DK|FD)-\d{4}-\d{2}-[a-z]+", args.csv) or [None])[0]
    if not key:
        key = client.table("slates").select("slate_key").order("imported_at", desc=True).limit(1).execute().data[0]["slate_key"]
    slate = client.table("slates").select("*").eq("slate_key", key).execute().data[0]
    sal = fetch_all(client.table("slate_salaries").select("site_player_id,player_id,player_name,position,team,salary")
                    .eq("slate_id", slate["slate_id"]), order="site_player_id")
    idx = {}
    for s in sal:
        idx.setdefault(norm_name(s["player_name"]), []).append(s)

    out, how = [], Counter()
    for r in rows:
        try:
            proj = float(str(r[col["proj"]]).replace(",", ""))
        except (TypeError, ValueError):
            continue
        cands = idx.get(norm_name(r[col["name"]]), [])
        if len(cands) > 1 and col["team"]:
            cands = [c for c in cands if c["team"] == norm_team(r[col["team"]])] or cands
        if len(cands) > 1 and col["pos"]:
            cands = [c for c in cands if c["position"] == str(r[col["pos"]]).strip().upper()[:3].replace("DEF", "DST")] or cands
        if not cands:
            how["unmatched"] += 1
            continue
        c = cands[0]
        own = None
        if col["own"] and r.get(col["own"]) not in (None, ""):
            try:
                own = float(str(r[col["own"]]).replace("%", ""))
            except ValueError:
                pass
        how["matched"] += 1
        out.append({"slate_id": slate["slate_id"], "site_player_id": c["site_player_id"], "player_id": c["player_id"],
                    "player_name": c["player_name"], "position": c["position"], "team": c["team"], "salary": c["salary"],
                    "method": "external", "mean": round(proj, 2), "median": round(proj, 2),
                    "components": {"source": args.source or args.csv.split("/")[-1], "ownership": own},
                    "updated_at": dt.datetime.now(dt.timezone.utc).isoformat()})
    print(f"{key}: {dict(how)}")
    if args.dry_run:
        print("dry run — no writes")
        return
    client.table("slate_projections").delete().eq("slate_id", slate["slate_id"]).eq("method", "external").execute()
    for batch in chunked(out):
        client.table("slate_projections").insert(batch).execute()
    print(f"done — {len(out)} external projections for {key}")


if __name__ == "__main__":
    main()
