"""
Import actual ownership (and exact site points) from a DraftKings contest-standings export.

DK → any contest you entered → "Export Lineups to CSV" / standings download. The file has a
player block on the right: Player, Roster Position, %Drafted, FPTS. FanDuel exports are
similar (Player, Position, Ownership) and are handled best-effort.

Usage:
  python jobs/import_ownership.py data/ownership/DK-2026-02-main_standings.csv
  python jobs/import_ownership.py contest.csv --slate-key DK-2026-02-main
The slate is taken from the filename if it contains a slate key, else matched by player overlap.
Writes slate_ownership; jobs/results.py then uses FPTS as the exact actual.
"""
from __future__ import annotations

import argparse
import csv
import re
from collections import Counter

from common import chunked, fetch_all, get_client, norm_name


def read_rows(path):
    with open(path, encoding="utf-8-sig", newline="") as f:
        rows = list(csv.reader(f))
    if not rows:
        raise SystemExit("empty file")
    header = [h.strip() for h in rows[0]]
    cols = {h.lower(): i for i, h in enumerate(header)}
    pi = next((cols[k] for k in ("player", "player name", "name") if k in cols), None)
    oi = next((cols[k] for k in ("%drafted", "% drafted", "ownership", "own%", "drafted%") if k in cols), None)
    fi = next((cols[k] for k in ("fpts", "points", "fantasy points") if k in cols), None)
    ri = next((cols[k] for k in ("roster position", "position", "pos") if k in cols), None)
    if pi is None or oi is None:
        raise SystemExit(f"could not find Player / %Drafted columns in header: {header}")
    # entries block (left side of a DK standings export): Rank, EntryId, EntryName, TimeRemaining, Points, Lineup
    ei = next((cols[k] for k in ("points", "score") if k in cols), None)
    scores = []
    if ei is not None and ei != fi:
        for r in rows[1:]:
            if len(r) > ei and r[ei].strip():
                try:
                    scores.append(float(r[ei]))
                except ValueError:
                    pass
    read_rows.scores = scores
    out = []
    for r in rows[1:]:
        if len(r) <= max(pi, oi) or not r[pi].strip():
            continue
        own = r[oi].strip().replace("%", "")
        try:
            own = float(own)
        except ValueError:
            continue
        fpts = None
        if fi is not None and len(r) > fi and r[fi].strip():
            try:
                fpts = float(r[fi])
            except ValueError:
                pass
        out.append({"name": r[pi].strip(), "pos": (r[ri].strip() if ri is not None and len(r) > ri else ""),
                    "own": own, "fpts": fpts})
    return out


def pick_slate(client, path, players, slate_key):
    slates = fetch_all(client.table("slates").select("slate_id,slate_key,site,season,week"), order="slate_key")
    if not slate_key:
        m = re.search(r"(DK|FD)-\d{4}-\d{2}-[a-z]+", path)
        slate_key = m.group(0) if m else None
    if slate_key:
        s = next((s for s in slates if s["slate_key"] == slate_key), None)
        if not s:
            raise SystemExit(f"slate {slate_key} not found")
        return s
    names = {norm_name(p["name"]) for p in players}
    best, best_n = None, 0
    for s in slates:
        sal = fetch_all(client.table("slate_salaries").select("player_name").eq("slate_id", s["slate_id"]), order="site_player_id")
        n = sum(1 for r in sal if norm_name(r["player_name"]) in names)
        if n > best_n:
            best, best_n = s, n
    if not best or best_n < 20:
        raise SystemExit("could not match this file to a slate — pass --slate-key")
    print(f"  matched to {best['slate_key']} ({best_n} players overlap)")
    return best


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("csv")
    ap.add_argument("--slate-key")
    ap.add_argument("--contest", default=None)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    players = read_rows(args.csv)
    print(f"{len(players)} players with ownership in {args.csv}")
    client = get_client(need_write=not args.dry_run)
    slate = pick_slate(client, args.csv, players, args.slate_key)
    sal = fetch_all(client.table("slate_salaries").select("site_player_id,player_id,player_name,position")
                    .eq("slate_id", slate["slate_id"]), order="site_player_id")
    by_name = {}
    for r in sal:
        by_name.setdefault(norm_name(r["player_name"]), []).append(r)
    # DST rows in standings are team nicknames ("Ravens"); salaries store the same nickname
    rows, how = [], Counter()
    for p in players:
        cands = by_name.get(norm_name(p["name"]), [])
        if len(cands) > 1 and p["pos"]:
            cands = [c for c in cands if c["position"] == p["pos"].split("/")[0]] or cands
        if not cands:
            how["unmatched"] += 1
            continue
        c = cands[0]
        how["matched"] += 1
        rows.append({"slate_id": slate["slate_id"], "site_player_id": c["site_player_id"], "player_id": c["player_id"],
                     "ownership_pct": p["own"], "fpts": p["fpts"], "contest": args.contest or args.csv.split("/")[-1]})
    print(f"  {dict(how)}")
    if args.dry_run:
        for r in sorted(rows, key=lambda r: -r["ownership_pct"])[:10]:
            print("  ", r["site_player_id"], r["ownership_pct"], r["fpts"])
        print("dry run — no writes")
        return
    for batch in chunked(rows):
        client.table("slate_ownership").upsert(batch, on_conflict="slate_id,site_player_id").execute()
    scores = getattr(read_rows, "scores", [])
    if len(scores) >= 20:                     # contest score distribution → slates.contest_meta (lineup finish estimates)
        import numpy as np
        a = np.array(scores)
        meta = {"name": args.contest or args.csv.split("/")[-1], "entries": int(len(a)),
                "quantiles": [round(float(x), 2) for x in np.percentile(a, range(0, 101))],
                "top": round(float(a.max()), 2), "mean": round(float(a.mean()), 2), "median": round(float(np.median(a)), 2)}
        client.table("slates").update({"contest_meta": meta}).eq("slate_id", slate["slate_id"]).execute()
        print(f"  contest distribution: {meta['entries']} entries, median {meta['median']}, top {meta['top']}")
    print(f"done — {len(rows)} ownership rows for {slate['slate_key']}")


if __name__ == "__main__":
    main()
