"""
Fetch the upcoming DraftKings NFL main-slate salary CSV straight from DraftKings and (optionally) import it,
so the weekly "download DKSalaries.csv, drop it in data/slates/, push" step is no longer needed.

  python jobs/fetch_dk_salaries.py                 # find the next Sunday main slate, write data/slates/DKSalaries_<season>_wk<WW>_main.csv
  python jobs/fetch_dk_salaries.py --import        # ...and import it (import_salaries.py) if that slate key is not in the DB yet
  python jobs/fetch_dk_salaries.py --import --force   # re-import even if the slate exists (DK added players, salary fixes)
  python jobs/fetch_dk_salaries.py --group 154078  # a specific DraftGroupId (e.g. an early-only or Sunday-Monday slate)
  python jobs/fetch_dk_salaries.py --dry-run       # just say which draft group would be used

How the main slate is picked: DraftKings' public lobby feed (https://www.draftkings.com/lobby/getcontests?sport=NFL)
lists every draft group. The main slate is the Classic (ContestTypeId 21) group that starts on the next Sunday at
1:00 PM ET with the most games. The CSV comes from the same endpoint the lobby's "Export to CSV" link uses
(https://www.draftkings.com/lineup/getavailableplayerscsv?contestTypeId=21&draftGroupId=<id>) and has exactly the
columns import_salaries.py expects. Season/week come from the games table (import_salaries.resolve_week), so the
file name matches the manual convention. Nothing here needs a DraftKings login.

Runs in nightly-ingest (non-fatal): from Tuesday on, the coming Sunday's slate lands by itself; the manual
data/slates/ drop still works and simply re-imports the same key.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import subprocess
import sys
import urllib.request
from pathlib import Path
from zoneinfo import ZoneInfo

HERE = Path(__file__).parent
ET = ZoneInfo("America/New_York")
LOBBY = "https://www.draftkings.com/lobby/getcontests?sport=NFL"
CSV = "https://www.draftkings.com/lineup/getavailableplayerscsv?contestTypeId={ct}&draftGroupId={dg}"
# DraftKings returns 403 to unusual User-Agent strings (like ESPN); a plain browser UA is fine.
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0 Safari/537.36", "Accept": "*/*"}


def fetch(url: str, timeout: int = 60) -> bytes:
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def list_groups() -> list[dict]:
    data = json.loads(fetch(LOBBY))
    out = []
    for g in data.get("DraftGroups", []):
        st = g.get("StartDateEst")
        try:
            start = dt.datetime.fromisoformat(st[:19]).replace(tzinfo=ET) if st else None
        except ValueError:
            start = None
        out.append({"id": g.get("DraftGroupId"), "contest_type": g.get("ContestTypeId"), "start": start,
                    "games": g.get("GameCount") or 0, "tag": g.get("DraftGroupTag") or "", "suffix": (g.get("ContestStartTimeSuffix") or "").strip()})
    return out


def pick_main(groups: list[dict], now: dt.datetime | None = None) -> dict | None:
    """Classic slate, next Sunday 1:00 PM ET. The main slate is the group with NO start-time suffix (DraftKings labels the
    others "(Early Only)", "(Sun-Mon)", "(Afternoon Only)" ...) — usually tagged "Featured". Ties: most games, then lowest id.
    Never pick by game count alone: on 9/30/2026 DraftKings added a 14-game "(Sun-Mon)" group mid-week and the old
    most-games rule replaced the 12-game main slate (and every player id) with it."""
    now = now or dt.datetime.now(ET)
    cands = [g for g in groups if g["contest_type"] == 21 and g["start"] and now < g["start"] <= now + dt.timedelta(days=7)
             and g["start"].weekday() == 6 and g["start"].hour == 13 and g["games"] >= 6]
    if not cands:
        return None
    first_sunday = min(g["start"].date() for g in cands)
    cands = [g for g in cands if g["start"].date() == first_sunday]
    plain = [g for g in cands if not g["suffix"]]
    if plain:
        cands = plain
    cands.sort(key=lambda g: (0 if "featured" in g["tag"].lower() else 1, -g["games"], g["id"]))
    return cands[0]


def board_overlap(client, slate_key: str, ids: set[str]) -> tuple[int, int]:
    """(matching ids, board size) between the CSV and the slate already stored under this key."""
    try:
        s = client.table("slates").select("slate_id").eq("slate_key", slate_key).execute().data
        if not s:
            return 0, 0
        rows, start = [], 0
        while True:
            page = client.table("slate_board").select("site_player_id").eq("slate_id", s[0]["slate_id"]).range(start, start + 999).execute().data
            rows += page
            if len(page) < 1000:
                break
            start += 1000
        have = {str(r["site_player_id"]) for r in rows}
        return len(have & ids), len(have)
    except Exception as e:  # noqa: BLE001
        print(f"  (could not read the stored board: {e})", file=sys.stderr)
        return -1, -1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--group", type=int, help="DraftGroupId (default: the next Sunday 1 PM main slate)")
    ap.add_argument("--slate-type", default="main")
    ap.add_argument("--out-dir", default=str(HERE.parent / "data" / "slates"))
    ap.add_argument("--import", dest="do_import", action="store_true", help="import with import_salaries.py when the slate key is new")
    ap.add_argument("--force", action="store_true", help="with --import: re-import an existing slate key")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    # Never on a Sunday (ET) without --group: after 1 PM the "next Sunday" is next week's slate, and importing it
    # would make it the latest slate while the late-swap tasks are still working today's.
    if not args.group and dt.datetime.now(ET).weekday() == 6:
        print("Sunday — skipping the automatic slate fetch (today's slate was imported earlier in the week)")
        return
    groups = list_groups()
    if args.group:
        g = next((x for x in groups if x["id"] == args.group), None) or {"id": args.group, "contest_type": 21, "start": None, "games": None, "tag": ""}
    else:
        g = pick_main(groups)
        if not g:
            print("no upcoming Sunday 1 PM classic slate in the DraftKings lobby yet", file=sys.stderr)
            sys.exit(2)
    print(f"draft group {g['id']}: {g['games']} games, starts {g['start']:%a %m/%d %I:%M %p ET}" if g["start"] else f"draft group {g['id']}")

    raw = fetch(CSV.format(ct=g["contest_type"] or 21, dg=g["id"]))
    text = raw.decode("utf-8-sig")
    lines = [l for l in text.splitlines() if l.strip()]
    if not lines or not lines[0].startswith("Position,"):
        print("unexpected CSV header: " + (lines[0][:80] if lines else "(empty)"), file=sys.stderr)
        sys.exit(1)
    n_players = len(lines) - 1
    dates = sorted({m.group(1) for l in lines[1:] for m in [re.search(r"\s(\d\d/\d\d/\d{4})\s", l)] if m})
    games = {m.group(1) for l in lines[1:] for m in [re.search(r",(\w+@\w+)\s\d\d/", l)] if m}
    print(f"  {n_players} players, {len(games)} games, dates {', '.join(dates)}")
    if n_players < 100 or len(games) < 6:
        print("too small for a main slate — not written", file=sys.stderr)
        sys.exit(1)

    # season / week from the games table (same rule as the importer)
    sys.path.insert(0, str(HERE))
    import import_salaries as imp  # noqa: E402
    from common import get_client  # noqa: E402
    import csv as _csv, io
    players = imp.parse_dk(list(_csv.DictReader(io.StringIO(text))))
    client = get_client(need_write=False)
    season, week, _ = imp.resolve_week(client, players)
    slate_key = f"DK-{season}-{week:02d}-{args.slate_type}"
    out = Path(args.out_dir) / f"DKSalaries_{season}_wk{week:02d}_{args.slate_type}.csv"

    if args.dry_run:
        print(f"dry run — would write {out} ({slate_key})")
        return
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text if text.endswith("\n") else text + "\n", encoding="utf-8")
    print(f"wrote {out}")

    if not args.do_import:
        return
    existing = client.table("slates").select("slate_id,n_players,imported_at").eq("slate_key", slate_key).execute().data
    if existing and not args.force:
        e = existing[0]
        if (e.get("n_players") or 0) == n_players:
            print(f"{slate_key} already imported ({e['n_players']} players, {e['imported_at'][:16]}) — nothing to do (use --force to re-import)")
            return
        # Same key, different player set: only re-import when it is the SAME draft group (DK added/removed a few players).
        # A different group (other games, all-new ids) would replace the board Alex's contests and entries file point at.
        ids = {str(p.get("site_player_id") or p.get("id") or "") for p in players}
        match, have = board_overlap(client, slate_key, ids)
        if have > 0 and match < 0.5 * have:
            print(f"{slate_key} exists with {have} players but only {match} of them are in draft group {g['id']} — this is a DIFFERENT "
                  f"draft group (other games / new player ids). Not replacing it. Use --group <id> --force if that is really wanted.", file=sys.stderr)
            sys.exit(3)
        print(f"{slate_key} exists with {e.get('n_players')} players, DraftKings now lists {n_players} (same draft group, {match}/{have} ids match) — re-importing")
    cmd = [sys.executable, str(HERE / "import_salaries.py"), str(out), "--slate-type", args.slate_type]
    print("+", " ".join(cmd), flush=True)
    res = subprocess.run(cmd, cwd=HERE, text=True, capture_output=True)
    print(res.stdout)
    if res.returncode != 0:
        print(res.stderr, file=sys.stderr)
        sys.exit(res.returncode)
    print(f"imported {slate_key}; project.py / simulate.py --latest pick it up next")


if __name__ == "__main__":
    main()
