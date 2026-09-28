"""Game-day inactives -> injury_reports, so the sim, the builder and the late-swap tasks all see who is OUT.

nflverse injury reports stop at Friday's designations ("Questionable"), so on Sunday the database kept showing
players as active after they were ruled out (9/27: Adonai Mitchell stayed in 14 entries). This job reads two live
public feeds and upgrades the report for the latest slate's week:
  * Sleeper's player feed (one call, every team): injury_status Out / IR / PUP / Sus / DNR, Doubtful
  * ESPN's game summaries for the slate's games: game-day "Out" entries (includes coach's-decision inactives)
It only ever makes a status MORE severe (Questionable -> Doubtful -> Out), never clears one, and never touches a
player whose game has already kicked off. Run it right after ingest.py (which rewrites the nflverse reports).

  python inactives.py              # latest slate, writes (needs SUPABASE_SERVICE_ROLE_KEY)
  python inactives.py --dry-run    # print what would change (anon key is enough)
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
import urllib.request
from zoneinfo import ZoneInfo

from common import chunked, fetch_all, get_client, norm_name, norm_team

SLEEPER = "https://api.sleeper.app/v1/players/nfl"
ESPN_SB = "https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard?dates={d}"
ESPN_SUM = "https://site.api.espn.com/apis/site/v2/sports/football/nfl/summary?event={e}"
OUT_STATUSES = {"Out", "IR", "PUP", "Sus", "DNR"}
RANK = {None: 0, "": 0, "Probable": 0, "Questionable": 1, "Doubtful": 2, "Out": 3}
ET = ZoneInfo("America/New_York")


def get_json(url, timeout=60):
    # default urllib User-Agent: ESPN (via some proxies) rejects custom agent strings with 403
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.load(r)


def sleeper_statuses(max_age_days: float, as_of: dt.datetime | None = None) -> dict:
    """{(norm_name, team): (status, injury, source)} from Sleeper. With as_of (testing), entries whose news is
    newer than as_of are dropped (an in-game injury at 4 PM must not look like a morning inactive)."""
    now_ms = (as_of or dt.datetime.now(dt.timezone.utc)).timestamp() * 1000
    out = {}
    for p in get_json(SLEEPER, timeout=120).values():
        st, team, name = p.get("injury_status"), p.get("team"), p.get("full_name")
        if not st or not team or not name:
            continue
        if as_of and (p.get("news_updated") or 0) > now_ms:
            continue
        if st in OUT_STATUSES:
            # a plain "Out" must be recent (IR / PUP / suspension are long-term by definition)
            if st == "Out" and (now_ms - (p.get("news_updated") or 0)) > max_age_days * 86400e3:
                continue
            status = "Out"
        elif st == "Doubtful":
            status = "Doubtful"
        else:
            continue
        out[(norm_name(name), norm_team(team))] = (status, p.get("injury_body_part") or st, f"sleeper:{st}")
    return out


def espn_statuses(dates: set[str], teams: set[str]) -> dict:
    """{(norm_name, team): ('Out', injury, 'espn')} from ESPN game summaries on the slate's dates."""
    out = {}
    for d in sorted(dates):
        try:
            events = get_json(ESPN_SB.format(d=d.replace("-", ""))).get("events", [])
        except Exception as e:                       # noqa: BLE001 — a feed hiccup must not stop the refresh
            print(f"  espn scoreboard {d} failed: {e}")
            continue
        for ev in events:
            try:
                summ = get_json(ESPN_SUM.format(e=ev["id"]))
            except Exception as e:                   # noqa: BLE001
                print(f"  espn summary {ev.get('shortName')} failed: {e}")
                continue
            for t in summ.get("injuries", []):
                team = norm_team(t.get("team", {}).get("abbreviation"))
                if team not in teams:
                    continue
                for i in t.get("injuries", []):
                    if i.get("status") == "Out":
                        name = (i.get("athlete") or {}).get("displayName")
                        if name:
                            det = (i.get("details") or {}).get("type") or "Out"
                            out[(norm_name(name), team)] = ("Out", det, "espn")
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--slate-key")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--max-age-days", type=float, default=6.0, help="ignore a Sleeper 'Out' older than this")
    ap.add_argument("--no-espn", action="store_true")
    ap.add_argument("--as-of", help="pretend it is this ET time for the kickoff check (testing), e.g. 2026-09-27T11:40")
    args = ap.parse_args()

    client = get_client(need_write=not args.dry_run)
    q = client.table("slates").select("slate_id,slate_key,season,week,game_ids")
    q = q.eq("slate_key", args.slate_key) if args.slate_key else q.order("imported_at", desc=True).limit(1)
    rows = q.execute().data
    if not rows:
        print("no slate"); return
    slate = rows[0]
    season, week = slate["season"], slate["week"]
    sal = fetch_all(client.table("slate_salaries").select("site_player_id,player_id,player_name,team,position,game_id")
                    .eq("slate_id", slate["slate_id"]), order="site_player_id")
    games = {g["game_id"]: g for g in fetch_all(client.table("games").select("game_id,gameday,gametime")
                                                .in_("game_id", slate.get("game_ids") or []), order="game_id")}
    now_et = dt.datetime.fromisoformat(args.as_of).replace(tzinfo=ET) if args.as_of else dt.datetime.now(ET)

    def kicked_off(gid):
        g = games.get(gid)
        if not g or not g.get("gameday") or not g.get("gametime"):
            return False
        ko = dt.datetime.fromisoformat(f"{g['gameday']}T{g['gametime']}").replace(tzinfo=ET)
        return now_et >= ko

    teams = {norm_team(r["team"]) for r in sal if r.get("team")}
    feed = sleeper_statuses(args.max_age_days, now_et if args.as_of else None)
    print(f"{slate['slate_key']}: sleeper statuses {len(feed)}")
    if not args.no_espn and not args.as_of:          # ESPN's list can't be rewound to an earlier time
        dates = {g["gameday"] for g in games.values() if g.get("gameday")}
        esp = espn_statuses(dates, teams)
        print(f"  espn game-day Out entries: {len(esp)}")
        for k, v in esp.items():                      # ESPN's game-day list wins over a lesser Sleeper status
            if RANK.get(feed.get(k, (None,))[0], 0) < RANK["Out"]:
                feed[k] = v

    existing = {r["player_id"]: r for r in fetch_all(
        client.table("injury_reports").select("player_id,season,week,season_type,team,position,player_name,report_status,practice_status,primary_injury")
        .eq("season", season).eq("week", week).eq("season_type", "REG"), order="player_id")}

    upserts, unmatched, changes = [], [], []
    for r in sal:
        if r.get("position") in ("DST", "DEF"):
            continue
        hit = feed.get((norm_name(r["player_name"]), norm_team(r["team"])))
        if not hit:
            continue
        status, injury, src = hit
        if kicked_off(r.get("game_id")):
            continue
        if not r.get("player_id"):
            unmatched.append(f"{r['player_name']} ({r['team']}) {status}")
            continue
        cur = existing.get(r["player_id"], {})
        if RANK.get(status, 0) <= RANK.get(cur.get("report_status"), 0):
            continue
        upserts.append({
            "player_id": r["player_id"], "season": season, "week": week, "season_type": "REG",
            "team": norm_team(r["team"]), "position": r["position"], "player_name": r["player_name"],
            "report_status": status, "practice_status": cur.get("practice_status"),
            "primary_injury": cur.get("primary_injury") or injury,
        })
        changes.append(f"{r['player_name']} ({r['team']} {r['position']}): {cur.get('report_status') or '-'} -> {status} [{src}]")

    for c in changes:
        print("  " + c)
    for u in unmatched:
        print(f"  (no player_id, not written) {u}")
    print(f"  {len(changes)} slate players upgraded")
    if args.dry_run or not upserts:
        return
    for part in chunked(upserts, 200):
        client.table("injury_reports").upsert(part, on_conflict="player_id,season,week,season_type").execute()
    print(f"  wrote {len(upserts)} injury_reports rows")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:                            # noqa: BLE001 — never fail the refresh over this
        print(f"inactives skipped: {e}", file=sys.stderr)
        sys.exit(0)
