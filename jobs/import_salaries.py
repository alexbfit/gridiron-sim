"""
Import a DraftKings or FanDuel salary export into Supabase (slates + slate_salaries),
mapping each player to their nflverse player_id.

Usage:
  python jobs/import_salaries.py data/slates/DKSalaries.csv            # site auto-detected
  python jobs/import_salaries.py FDSalaries.csv --site FD --slate-type main
  python jobs/import_salaries.py DKSalaries.csv --dry-run              # no writes, prints summary

Slate key = "<SITE>-<season>-<week:02d>-<slate_type>", e.g. DK-2026-02-main.
Re-importing the same key replaces that slate's salaries.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import difflib
import json
import re
import sys
from collections import Counter, defaultdict

from common import DST_NAMES, chunked, fetch_all, get_client, name_key, norm_name, norm_team


# ------------------------------------------------------------------ parsing
def read_csv(path: str) -> list[dict]:
    with open(path, encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def detect_site(rows: list[dict]) -> str:
    cols = set(rows[0].keys())
    if "Name + ID" in cols or "TeamAbbrev" in cols:
        return "DK"
    if "Nickname" in cols or "Injury Indicator" in cols:
        return "FD"
    raise SystemExit("Could not detect site from columns: " + ", ".join(cols))


def parse_dk(rows: list[dict]) -> list[dict]:
    out = []
    for r in rows:
        pos = r["Position"].strip()
        team = norm_team(r["TeamAbbrev"])
        gi = r.get("Game Info", "").strip()               # "CAR@ATL 09/20/2026 01:00PM ET"
        m = re.match(r"(\w+)@(\w+)\s+(\d\d/\d\d/\d{4})", gi)
        away, home, date = (norm_team(m.group(1)), norm_team(m.group(2)), m.group(3)) if m else (None, None, None)
        opp = home if team == away else away
        out.append({
            "site_player_id": r["ID"].strip(),
            "player_name": r["Name"].strip(),
            "position": pos,
            "roster_positions": r.get("Roster Position", "").strip(),
            "team": team,
            "opponent": opp,
            "home": home, "away": away,
            "game_date": dt.datetime.strptime(date, "%m/%d/%Y").date() if date else None,
            "salary": int(float(r["Salary"])),
            "avg_points": float(r["AvgPointsPerGame"]) if r.get("AvgPointsPerGame") else None,
            "status": (r.get("Status") or "").strip() or None,
            "game_info": gi,
        })
    return out


def parse_fd(rows: list[dict]) -> list[dict]:
    out = []
    for r in rows:
        pos = r["Position"].strip()
        if pos in ("D", "DEF"):
            pos = "DST"
        team = norm_team(r["Team"])
        opp = norm_team(r.get("Opponent"))
        game = (r.get("Game") or "").strip()              # "CAR@ATL"
        m = re.match(r"(\w+)@(\w+)", game)
        away, home = (norm_team(m.group(1)), norm_team(m.group(2))) if m else (None, None)
        name = (r.get("Nickname") or f"{r.get('First Name','')} {r.get('Last Name','')}").strip()
        out.append({
            "site_player_id": r["Id"].strip(),
            "player_name": name,
            "position": pos,
            "roster_positions": (r.get("Roster Position") or pos).strip(),
            "team": team,
            "opponent": opp,
            "home": home, "away": away,
            "game_date": None,
            "salary": int(float(r["Salary"])),
            "avg_points": float(r["FPPG"]) if r.get("FPPG") else None,
            "status": (r.get("Injury Indicator") or "").strip() or None,
            "game_info": game,
        })
    return out


# ------------------------------------------------------------------ slate week
def resolve_week(client, players: list[dict]) -> tuple[int, int, dict]:
    """Find (season, week) whose schedule best matches the slate's games.
    Returns season, week, and {(away, home): game_id}."""
    pairs = {(p["away"], p["home"]) for p in players if p["away"] and p["home"]}
    dates = {p["game_date"] for p in players if p["game_date"]}
    today = dt.date.today()
    lo = (min(dates) if dates else today) - dt.timedelta(days=10)
    hi = (max(dates) if dates else today) + dt.timedelta(days=14)
    games = fetch_all(
        client.table("games").select("game_id,season,week,home_team,away_team,gameday")
        .gte("gameday", lo.isoformat()).lte("gameday", hi.isoformat()),
        order="game_id",
    )
    score = Counter()
    for g in games:
        if (g["away_team"], g["home_team"]) in pairs:
            score[(g["season"], g["week"])] += 1
    if not score:
        raise SystemExit("No games in the schedule match this slate — has the ingest run for this season?")
    (season, week), n = score.most_common(1)[0]
    game_ids = {(g["away_team"], g["home_team"]): g["game_id"]
                for g in games if g["season"] == season and g["week"] == week}
    print(f"  slate resolved to season {season} week {week} ({n}/{len(pairs)} games matched)")
    return season, week, game_ids


# ------------------------------------------------------------------ player matching
def load_player_index(client, season: int) -> list[dict]:
    rows = fetch_all(
        client.table("player_game_stats")
        .select("player_id,player_name,position,team,season,week")
        .in_("season", [season, season - 1])
        .in_("position", ["QB", "RB", "WR", "TE"]),
        order=["player_id", "season", "week"],
    )
    latest: dict[str, dict] = {}
    for r in rows:
        k = r["player_id"]
        if k not in latest or (r["season"], r["week"]) > (latest[k]["season"], latest[k]["week"]):
            latest[k] = r
    return list(latest.values())


def match_players(players: list[dict], index: list[dict]) -> Counter:
    by_name_team = defaultdict(list)
    by_name_pos = defaultdict(list)
    by_key_team_pos = defaultdict(list)
    by_team_pos = defaultdict(list)
    for p in index:
        n = norm_name(p["player_name"])
        by_name_team[(n, p["team"])].append(p)
        by_name_pos[(n, p["position"])].append(p)
        by_key_team_pos[(name_key(p["player_name"]), p["team"], p["position"])].append(p)
        by_team_pos[(p["team"], p["position"])].append(p)

    how = Counter()
    for sp in players:
        sp["player_id"] = None
        sp["matched_by"] = None
        if sp["position"] == "DST":
            sp["player_id"] = f"DST_{sp['team']}"
            sp["matched_by"] = "dst"
            how["dst"] += 1
            continue
        n = norm_name(sp["player_name"])
        cands = by_name_team.get((n, sp["team"]))
        if cands:
            sp["player_id"], sp["matched_by"] = cands[0]["player_id"], "name+team"
        else:
            cands = by_name_pos.get((n, sp["position"]))
            if cands and len(cands) == 1:
                sp["player_id"], sp["matched_by"] = cands[0]["player_id"], "name+pos"
            else:
                cands = by_key_team_pos.get((name_key(sp["player_name"]), sp["team"], sp["position"]))
                if cands and len(cands) == 1:
                    sp["player_id"], sp["matched_by"] = cands[0]["player_id"], "initial+team+pos"
                else:
                    pool = by_team_pos.get((sp["team"], sp["position"]), [])
                    names = {norm_name(p["player_name"]): p for p in pool}
                    close = difflib.get_close_matches(n, list(names), n=1, cutoff=0.85)
                    if close:
                        sp["player_id"], sp["matched_by"] = names[close[0]]["player_id"], "fuzzy"
        how[sp["matched_by"] or "UNMATCHED"] += 1
    return how


# ------------------------------------------------------------------ main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("csv")
    ap.add_argument("--site", choices=["DK", "FD"])
    ap.add_argument("--slate-type", default="main")
    ap.add_argument("--slate-name")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    rows = read_csv(args.csv)
    site = args.site or detect_site(rows)
    players = parse_dk(rows) if site == "DK" else parse_fd(rows)
    print(f"{site}: {len(players)} players in {args.csv}")

    client = get_client(need_write=not args.dry_run)
    season, week, game_ids = resolve_week(client, players)
    for p in players:
        p["game_id"] = game_ids.get((p["away"], p["home"]))

    index = load_player_index(client, season)
    how = match_players(players, index)
    print("  matching:", dict(how))
    unmatched = [p for p in players if not p["player_id"]]
    if unmatched:
        print("  unmatched (no game history — rookies/practice squad are expected here):")
        for p in unmatched[:40]:
            print(f"    {p['position']:3} {p['team']:4} {p['player_name']} (${p['salary']}, {p['status'] or 'ok'})")
        if len(unmatched) > 40:
            print(f"    ... and {len(unmatched) - 40} more")

    slate_key = f"{site}-{season}-{week:02d}-{args.slate_type}"
    slate = {
        "slate_key": slate_key,
        "site": site, "season": season, "week": week,
        "slate_type": args.slate_type,
        "slate_name": args.slate_name or f"{site} Week {week} {args.slate_type}",
        # only the games actually on this slate (a main slate excludes TNF / MNF) — this is what
        # "slate started" / "slate complete" checks look at
        "game_ids": sorted({p["game_id"] for p in players if p["game_id"]}),
        "n_players": len(players),
        "imported_at": dt.datetime.now(dt.timezone.utc).isoformat(),
    }
    sal_rows = [{
        "site_player_id": p["site_player_id"], "player_id": p["player_id"],
        "player_name": p["player_name"], "position": p["position"],
        "roster_positions": p["roster_positions"], "team": p["team"], "opponent": p["opponent"],
        "game_id": p["game_id"], "salary": p["salary"], "avg_points": p["avg_points"],
        "status": p["status"], "game_info": p["game_info"], "matched_by": p["matched_by"],
    } for p in players]

    if args.dry_run:
        out = {"slate": slate, "salaries": sal_rows}
        path = f"slate_{slate_key}.json"
        with open(path, "w") as f:
            json.dump(out, f, indent=1, default=str)
        print(f"dry run — wrote {path}")
        return

    res = client.table("slates").upsert(slate, on_conflict="slate_key").execute()
    slate_id = res.data[0]["slate_id"]
    client.table("slate_salaries").delete().eq("slate_id", slate_id).execute()
    for batch in chunked([{**r, "slate_id": slate_id} for r in sal_rows]):
        client.table("slate_salaries").insert(batch).execute()
    print(f"done — slate {slate_key} ({slate_id}) with {len(sal_rows)} players")


if __name__ == "__main__":
    main()
