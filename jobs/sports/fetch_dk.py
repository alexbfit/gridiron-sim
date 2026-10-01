"""Fetch a DraftKings salary CSV for a non-NFL sport (NBA first) from the public lobby, no login.

    python jobs/fetch_dk_salaries.py --sport NBA                  # today's (ET) NBA main Classic slate
    python jobs/fetch_dk_salaries.py --sport NBA --date 2026-10-21 --dry-run
    python jobs/sports/fetch_dk.py NBA --date 2026-10-21 --list   # print every draft group in the lobby

How the NBA main slate is picked (pick_main): DraftKings' lobby JSON (https://www.draftkings.com/lobby/getcontests?sport=NBA)
lists every draft group with ContestTypeId, StartDateEst, GameCount and a ContestStartTimeSuffix. The Classic game
type id is discovered from the lobby's GameTypes list (Name == "Classic" for the sport; NBA has been 70 for years and
that is the fallback when no Classic contest is posted yet - e.g. in preseason). The main slate is the Classic group
that starts on the requested date in the evening (ET), has NO suffix (DraftKings labels the others "(Early Only)",
"(Late Night)", "(Turbo)", "(DAL @ LAL)" ...) and the most games; ties go to the "Featured" tag, then the lowest id.
When no plain group exists yet the biggest Classic group of that evening is used and a note is printed.

The CSV is the lobby's "Export to CSV" link (lineup/getavailableplayerscsv?contestTypeId=<ct>&draftGroupId=<id>):
columns Position, Name + ID, Name, ID, Roster Position, Salary, Game Info, TeamAbbrev, AvgPointsPerGame - exactly what
jobs/build_lineups_sport.py --dk reads. Written to data/slates/DKSalaries_<SPORT>_<date>.csv.
"""
from __future__ import annotations

import argparse
import datetime as dt
import os
import re
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from fetch_dk_salaries import CSV, fetch, list_groups, lobby  # noqa: E402

ET = ZoneInfo("America/New_York")
ROOT = Path(__file__).resolve().parents[2]
CLASSIC_FALLBACK = {"NBA": 70, "NHL": 1, "MLB": 1, "WNBA": 1, "CBB": 1}   # DraftKings' long-standing NBA Classic type id is 70
MIN_GAMES = {"NBA": 2}


def classic_type_id(sport: str, data: dict) -> int:
    """ContestTypeId of the sport's Classic game type from the lobby's GameTypes (fallback: the known id)."""
    ids = [g.get("GameTypeId") for g in data.get("GameTypes", []) if (g.get("Name") or "").strip().lower() == "classic"]
    sport_ids = {g.get("SportId") for g in data.get("DraftGroups", []) if g.get("SportId") is not None}
    same_sport = [g.get("GameTypeId") for g in data.get("GameTypes", []) if (g.get("Name") or "").strip().lower() == "classic"
                  and g.get("SportId") in sport_ids]
    if same_sport:
        return int(same_sport[0])
    if len(ids) == 1:
        return int(ids[0])
    return CLASSIC_FALLBACK.get(sport, 1)


def pick_main(groups: list[dict], date: dt.date, classic: int, sport: str = "NBA") -> dict | None:
    """The sport's main Classic slate of `date` (ET): evening start, no start-time suffix, most games."""
    cands = [g for g in groups if g["contest_type"] == classic and g["start"] and g["start"].astimezone(ET).date() == date
             and g["games"] >= MIN_GAMES.get(sport, 2)]
    if not cands:
        return None
    evening = [g for g in cands if g["start"].astimezone(ET).hour >= 17]
    if evening:
        cands = evening
    plain = [g for g in cands if not g["suffix"]]
    if plain:
        cands = plain
    else:
        print("  no plain (no-suffix) Classic group for that evening yet - using the biggest one", file=sys.stderr)
    cands.sort(key=lambda g: (0 if "featured" in (g["tag"] or "").lower() else 1, -g["games"], g["id"]))
    return cands[0]


def describe(groups: list[dict]) -> str:
    rows = []
    for g in sorted(groups, key=lambda g: (g["start"] or dt.datetime.max.replace(tzinfo=ET), g["id"])):
        st = f"{g['start']:%a %m/%d %I:%M %p ET}" if g["start"] else "?"
        rows.append(f"  group {g['id']:<7} type {g['contest_type']:<4} {st:<22} {g['games']:>2} games  {g['tag'] or '':<9} {g['suffix']}")
    return "\n".join(rows) if rows else "  (no draft groups in the lobby)"


def parse_csv_summary(text: str) -> tuple[int, int, list[str]]:
    lines = [l for l in text.splitlines() if l.strip()]
    n_players = max(0, len(lines) - 1)
    games = {m.group(1) for l in lines[1:] for m in [re.search(r",(\w+@\w+)\s\d\d/", l)] if m}
    dates = sorted({m.group(1) for l in lines[1:] for m in [re.search(r"\s(\d\d/\d\d/\d{4})\s", l)] if m})
    return n_players, len(games), dates


def run(sport: str, date: str | None = None, group: int | None = None, out_dir: str | None = None, dry_run: bool = False,
        list_only: bool = False, out_path: str | None = None) -> Path | None:
    sport = sport.upper()
    day = dt.date.fromisoformat(date) if date else dt.datetime.now(ET).date()
    data = lobby(sport)
    groups = list_groups(sport, data)
    classic = classic_type_id(sport, data)
    print(f"{sport} lobby: {len(groups)} draft groups, Classic contest type {classic}")
    if list_only or not groups:
        print(describe(groups))
        if list_only:
            return None
    if group:
        g = next((x for x in groups if x["id"] == group), None) or {"id": group, "contest_type": classic, "start": None, "games": None, "tag": "", "suffix": ""}
    else:
        g = pick_main(groups, day, classic, sport)
        if not g:
            print(f"no {sport} Classic slate on {day:%a %m/%d} in the DraftKings lobby. Groups seen:\n{describe(groups)}", file=sys.stderr)
            sys.exit(2)
    print(f"draft group {g['id']}: {g['games']} games, starts {g['start']:%a %m/%d %I:%M %p ET}" if g["start"] else f"draft group {g['id']}")
    out = Path(out_path) if out_path else Path(out_dir or ROOT / "data" / "slates") / f"DKSalaries_{sport}_{day.isoformat()}.csv"
    if dry_run:
        print(f"dry run - would fetch contestTypeId={g['contest_type']} draftGroupId={g['id']} and write {out}")
        return None
    raw = fetch(CSV.format(ct=g["contest_type"] or classic, dg=g["id"]))
    text = raw.decode("utf-8-sig")
    if not text.lstrip().startswith("Position,"):
        print("unexpected CSV header: " + text[:80], file=sys.stderr)
        sys.exit(1)
    n_players, n_games, dates = parse_csv_summary(text)
    print(f"  {n_players} players, {n_games} games, dates {', '.join(dates)}")
    if n_games < MIN_GAMES.get(sport, 2):
        print("too small for a main slate - not written", file=sys.stderr)
        sys.exit(1)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text if text.endswith("\n") else text + "\n", encoding="utf-8")
    print(f"wrote {out}")
    return out


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("sport", nargs="?", default="NBA")
    ap.add_argument("--date", help="slate date (ET), default today")
    ap.add_argument("--group", type=int)
    ap.add_argument("--out-dir")
    ap.add_argument("--out", help="exact output path (overrides --out-dir)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--list", action="store_true", help="print every draft group in the lobby and exit")
    a = ap.parse_args(argv)
    run(a.sport, date=a.date, group=a.group, out_dir=a.out_dir, dry_run=a.dry_run, list_only=a.list, out_path=a.out)


if __name__ == "__main__":
    main()
