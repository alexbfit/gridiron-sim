"""
Pre-lock checklist: every input the Sunday build needs, checked in one place, loudly.

  python jobs/preflight.py                  # mode picked from the clock (Sat evening / Sun morning / Sun after the build)
  python jobs/preflight.py --mode sun       # Sunday inputs: slate, sim, sportsbook props, lines, weather, injuries, news
  python jobs/preflight.py --mode lineups   # + the 9:30 build saved its lineups
  python jobs/preflight.py --mode sat       # Saturday night: tomorrow's slate, sim, Kalshi props, the Sat news sweep
  python jobs/preflight.py --skip news      # (the 7 AM GitHub run: the Sunday news sweep hasn't run yet)
  python jobs/preflight.py --at 2026-10-04T08:50 --slate-key DK-2026-04-main   # replay a moment (testing)

Read-only (anon key is enough). Each check prints PASS / WARN / FAIL with the fix. Exit code 1 when any FAIL,
0 otherwise, so a GitHub step or a Claude task can fail loudly. --json writes the result for the run log.

Why it exists: every Sunday so far lost something silently — props that never landed (9/20-9/26, props.py crashed
on an argument), props wiped after kickoff (9/27), in-game lines stored as the week's lines, a Saturday news sweep
that failed with no logs, GitHub cron skipping the 7 AM refresh. Each was found by hand, after the fact.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
UTC = dt.timezone.utc
DEFAULT_URL = "https://hmckvrmsgmdsuyjsuvcq.supabase.co"
DEFAULT_ANON = "sb_publishable_45X4VDNlLYDp9yJERH40oA_yeTF4Jcz"      # public read-only key (also in web/config.js)

MIN_BOOK_PROPS = 150        # the Sunday build's BOOKS threshold
MIN_KALSHI_PROPS = 80       # Saturday night: Kalshi ladders only
MIN_SLATE_PLAYERS = 300
MIN_SIM_PLAYERS = 250


class Rest:
    def __init__(self, url=None, key=None):
        self.url = (url or os.environ.get("SUPABASE_URL") or DEFAULT_URL).rstrip("/")
        self.key = key or os.environ.get("SUPABASE_ANON_KEY") or os.environ.get("SUPABASE_SERVICE_ROLE_KEY") or DEFAULT_ANON

    def get(self, path, params=None, count=False):
        q = urllib.parse.urlencode(params or {}, safe=",.()*:>=<")
        req = urllib.request.Request(f"{self.url}/rest/v1/{path}?{q}", headers={
            "apikey": self.key, "Authorization": f"Bearer {self.key}", **({"Prefer": "count=exact", "Range": "0-0"} if count else {})})
        with urllib.request.urlopen(req, timeout=30) as r:
            if count:
                cr = r.headers.get("Content-Range") or "*/0"
                return int(cr.split("/")[-1] or 0)
            return json.load(r)

    def head_ok(self, url):
        try:
            req = urllib.request.Request(url, method="HEAD")
            with urllib.request.urlopen(req, timeout=30) as r:
                return 200 <= r.status < 400
        except Exception:
            return False


def parse_ts(s):
    if not s:
        return None
    s = s.replace("Z", "+00:00")
    if "." in s:                                     # PostgREST gives 6 fractional digits, sometimes fewer
        head, tail = s.split(".", 1)
        frac, rest = (tail[:tail.index("+")], tail[tail.index("+"):]) if "+" in tail else (tail, "")
        s = f"{head}.{frac[:6].ljust(6, '0')}{rest}"
    t = dt.datetime.fromisoformat(s)
    return t if t.tzinfo else t.replace(tzinfo=UTC)


def fmt_et(t):
    return t.astimezone(ET).strftime("%a %-I:%M %p ET") if t else "never"


class Checklist:
    def __init__(self):
        self.items = []

    def add(self, level, name, detail, fix=""):
        self.items.append({"level": level, "check": name, "detail": detail, "fix": fix})

    def ok(self, name, detail):
        self.add("PASS", name, detail)

    def warn(self, name, detail, fix=""):
        self.add("WARN", name, detail, fix)

    def fail(self, name, detail, fix=""):
        self.add("FAIL", name, detail, fix)

    @property
    def status(self):
        lv = {i["level"] for i in self.items}
        return "failed" if "FAIL" in lv else "warn" if "WARN" in lv else "ok"


def pick_mode(now):
    et = now.astimezone(ET)
    if et.weekday() == 6:                           # Sunday
        return "lineups" if et.hour * 60 + et.minute >= 10 * 60 else "sun"
    return "sat"


def run(rest, mode, now, slate_key=None, skip=()):
    c = Checklist()
    et_now = now.astimezone(ET)
    if mode == "sat":
        target_day = (et_now + dt.timedelta(days=(6 - et_now.weekday()) % 7 or 7)).date() if et_now.weekday() != 6 else et_now.date()
    else:
        target_day = et_now.date()
    day_start = dt.datetime.combine(target_day, dt.time(0, 0), ET)

    # ---- slate
    params = {"select": "slate_id,slate_key,season,week,game_ids,sim_meta,imported_at", "order": "imported_at.desc", "limit": "1"}
    if slate_key:
        params["slate_key"] = f"eq.{slate_key}"
    slates = rest.get("slates", params)
    if not slates:
        c.fail("slate", "no slate in the database", "run Actions > nightly-ingest (fetch_dk_salaries --import) or drop the DK CSV in data/slates/ and push")
        return c, None
    s = slates[0]
    gids = s.get("game_ids") or []
    games = rest.get("games", {"select": "game_id,gameday,gametime,home_team,away_team,spread_line,total_line,roof,forecast_at,home_score",
                               "game_id": f"in.({','.join(gids)})"}) if gids else []
    days = sorted({g["gameday"] for g in games if g.get("gameday")})
    n_players = rest.get("slate_salaries", {"select": "site_player_id", "slate_id": f"eq.{s['slate_id']}"}, count=True)
    if str(target_day) not in days:
        c.fail("slate", f"latest slate {s['slate_key']} plays {', '.join(days) or '?'}, not {target_day}",
               "the nightly fetches next Sunday's DK slate (fetch_dk_salaries --import); run Actions > nightly-ingest or drop the DK CSV in data/slates/ and push")
    elif len(games) < 8 or n_players < MIN_SLATE_PLAYERS:
        c.warn("slate", f"{s['slate_key']}: {len(games)} games, {n_players} players — smaller than a main slate", "check the DK file is the Classic main slate")
    else:
        c.ok("slate", f"{s['slate_key']}: {len(games)} games, {n_players} players, all on {target_day}")

    # ---- game lines (a missing line breaks implied totals, the RB rule, the QB gap and the sim's game script)
    no_line = [g["game_id"] for g in games if g.get("spread_line") is None or g.get("total_line") is None]
    if no_line:
        (c.fail if len(no_line) > 1 else c.warn)("game lines", f"{len(no_line)} game(s) without spread/total: {', '.join(no_line)}",
                                                "run Actions > gameday-refresh (odds.py) — check ODDS_API_KEY credits")
    else:
        c.ok("game lines", f"spread + total on all {len(games)} games")

    # ---- sim
    sm = s.get("sim_meta") or {}
    run_at = parse_ts(sm.get("run_at"))
    fresh_after = (dt.datetime.combine(target_day, dt.time(6, 30), ET) if mode in ("sun", "lineups")
                   else now - dt.timedelta(hours=30))
    if not run_at:
        c.fail("sim", "no sim for this slate", "run Actions > gameday-refresh")
    elif run_at < fresh_after:
        c.fail("sim", f"last sim {fmt_et(run_at)} — older than {fmt_et(fresh_after)}",
               "the refresh didn't run: Actions > gameday-refresh" + (" with props=true" if mode != "sat" else ""))
    else:
        n_sim = rest.get("slate_projections", {"select": "site_player_id", "slate_id": f"eq.{s['slate_id']}", "method": "eq.sim"}, count=True)
        url = sm.get("url")
        if n_sim < MIN_SIM_PLAYERS:
            c.fail("sim", f"sim {fmt_et(run_at)} but only {n_sim} players projected", "re-run Actions > gameday-refresh; check the simulate step log")
        elif not url or not rest.head_ok(url):
            c.fail("sim", f"sim {fmt_et(run_at)}, {n_sim} players, but the sim matrix file is missing", "re-run the simulate step (Storage 'sims' upload)")
        else:
            c.ok("sim", f"{fmt_et(run_at)}, {n_sim} players, matrix reachable")

    # ---- props
    if "props" not in skip:
        props = rest.get("slate_projections", {"select": "site_player_id,components,updated_at",
                                               "slate_id": f"eq.{s['slate_id']}", "method": "eq.props", "limit": "3000"})
        newest = max((parse_ts(r["updated_at"]) for r in props), default=None)
        if mode == "sat":
            if len(props) < MIN_KALSHI_PROPS:
                c.warn("props", f"{len(props)} props rows (Kalshi ladders post Tue-Sat)",
                       "not fatal tonight — the Sunday 7 AM run pulls sportsbook props; check props.py --source kalshi in the Sat 6:30 PM log")
            else:
                c.ok("props", f"{len(props)} rows (newest {fmt_et(newest)})")
        else:
            since = dt.datetime.combine(target_day, dt.time(6, 0), ET)
            books = [r for r in props if "odds" in (((r.get("components") or {}).get("src")) or []) and parse_ts(r["updated_at"]) >= since]
            if len(books) < MIN_BOOK_PROPS:
                c.fail("props", f"{len(books)} sportsbook-sourced rows since 6 AM ({len(props)} total, newest {fmt_et(newest)}) — need {MIN_BOOK_PROPS}",
                       "run Actions > gameday-refresh with props=true (costs ~60-85 Odds API credits); without it the build falls back to sim + Kalshi")
            else:
                c.ok("props", f"{len(books)} sportsbook-sourced rows this morning ({len(props)} total)")
                qb_gap = [r for r in books if (r.get("components") or {}).get("qb_gap") is not None]
                if not qb_gap:
                    c.warn("QB gap", "no QB row carries components.qb_gap", "props.py ran without jobs/qbgap.py (c50dd63) or its context failed — see the 7 AM props log")
                else:
                    c.ok("QB gap", f"applied to {len(qb_gap)} QBs")

    # ---- weather (outdoor games only)
    outdoor = [g for g in games if (g.get("roof") or "outdoors") in ("outdoors", "open")]
    stale = [g["game_id"] for g in outdoor if not g.get("forecast_at") or parse_ts(g["forecast_at"]) < now - dt.timedelta(hours=26)]
    if stale:
        c.warn("weather", f"{len(stale)}/{len(outdoor)} outdoor games without a forecast in the last 26 h", "weather.py failed (Open-Meteo 429s?) — the sim runs without wind; flag windy games by hand")
    else:
        c.ok("weather", f"forecasts on all {len(outdoor)} outdoor games")

    # ---- injury reports
    inj = rest.get("injury_reports", {"select": "updated_at", "order": "updated_at.desc", "limit": "1"})
    inj_at = parse_ts(inj[0]["updated_at"]) if inj else None
    if not inj_at or inj_at < now - dt.timedelta(hours=14):
        c.warn("injuries", f"injury reports last updated {fmt_et(inj_at)}", "ingest.py / inactives.py failing — the build still web-checks every Q player")
    else:
        c.ok("injuries", f"updated {fmt_et(inj_at)}")

    # ---- news sweeps
    if "news" not in skip:
        runs = {}
        for r in rest.get("news_notes", {"select": "run,created_at", "slate_id": f"eq.{s['slate_id']}", "limit": "2000"}):
            runs.setdefault(r["run"], []).append(parse_ts(r["created_at"]))
        picks = {r["run"] for r in rest.get("upset_picks", {"select": "run", "slate_id": f"eq.{s['slate_id']}", "limit": "200"})}
        sat_ok = "sat" in runs or "sat" in picks
        sun_ok = "sun" in runs or "sun" in picks
        sat_due = dt.datetime.combine(target_day - dt.timedelta(days=1), dt.time(19, 45), ET)   # sweep 7:00 PM, ~20-40 min
        if mode == "sat" and now < sat_due:
            c.ok("news (Sat)", f"not due until {fmt_et(sat_due)}")
        elif mode == "sat":
            if sat_ok:
                c.ok("news (Sat)", f"{len(runs.get('sat', []))} notes, picks {'yes' if 'sat' in picks else 'no'}")
            else:
                c.fail("news (Sat)", "the Saturday news sweep saved nothing for this slate",
                       "open the 'team news sweep (Sat)' task run; re-run it now (it takes ~15 min) — Sunday falls back to the 8:15 AM sweep only")
        else:
            if sun_ok:
                c.ok("news (Sun)", f"{len(runs.get('sun', []))} notes" + ("" if sat_ok else " (the Sat sweep saved nothing)"))
            elif sat_ok:
                c.warn("news (Sun)", f"no Sunday sweep yet — Saturday's {len(runs.get('sat', []))} notes will be used",
                       "if it's past 8:45 AM, re-run the 'team news sweep (Sun)' task")
            else:
                c.fail("news", "neither news sweep saved anything for this slate", "re-run 'team news sweep (Sun)' before 9:30, or the build runs with no news notes")

    # ---- the 9:30 build
    if mode == "lineups":
        lus = rest.get("slate_lineups", {"select": "contest,idx,created_at,note", "slate_id": f"eq.{s['slate_id']}", "source": "eq.claude", "limit": "500"})
        gpp = [l for l in lus if l["contest"] == "gpp" and parse_ts(l["created_at"]) >= dt.datetime.combine(target_day, dt.time(8, 0), ET)]
        if len(gpp) < 50:
            c.fail("lineups", f"{len(gpp)} GPP lineups saved this morning (need 50)",
                   "the 9:30 build didn't finish or its --save failed: open the 'Sunday lineups' task run; re-run it before 1 PM")
        else:
            newest = max(parse_ts(l["created_at"]) for l in gpp)
            c.ok("lineups", f"{len(gpp)} GPP lineups saved {fmt_et(newest)}")
        if not [l for l in lus if l["contest"] == "cash"]:
            c.warn("cash benchmark", "no cash benchmark lineup saved", "not played — only Flashback's cash reference is missing")

    # ---- scheduled-task run log (017_task_runs.sql)
    if "tasks" not in skip:
        expect = {"sat": ["news-sat"], "sun": ["news-sat", "news-sun"], "lineups": ["news-sat", "news-sun", "sunday-build"]}[mode]
        since = dt.datetime.combine(target_day - dt.timedelta(days=1), dt.time(12, 0), ET)
        try:
            rows = rest.get("task_runs", {"select": "task,status,summary,started_at,finished_at", "started_at": f"gte.{since.astimezone(UTC).isoformat()}",
                                          "order": "started_at.desc", "limit": "200"})
        except urllib.error.HTTPError as e:
            rows = None
            if e.code in (404, 400):
                c.warn("task log", "task_runs table not found", "run supabase/pending/017_task_runs.sql in the Supabase SQL editor")
            else:
                c.warn("task log", f"task_runs unreadable ({e.code})")
        if rows is not None:
            for t in expect:
                if mode == "sun" and t == "news-sun" and et_now.hour * 60 + et_now.minute < 8 * 60 + 40:
                    continue                                   # the Sunday sweep starts 8:15 and takes ~20 min
                if t == "news-sat" and now < sat_due:
                    continue
                mine = [r for r in rows if r["task"] == t]
                if not mine:
                    c.warn(f"task {t}", "no run logged", "the task didn't start (or ran without logging) — check it in Claude > Scheduled tasks")
                    continue
                last = mine[0]
                if last["status"] == "ok":
                    c.ok(f"task {t}", f"ok {fmt_et(parse_ts(last['finished_at'] or last['started_at']))} — {(last.get('summary') or '')[:90]}")
                elif last["status"] == "started":
                    c.warn(f"task {t}", f"started {fmt_et(parse_ts(last['started_at']))}, never finished", "the run died mid-way — open it in Claude > Scheduled tasks")
                else:
                    (c.fail if last["status"] == "failed" else c.warn)(f"task {t}", f"{last['status']}: {(last.get('summary') or '')[:120]}")
    return c, s


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[1] if __doc__ else None)
    ap.add_argument("--mode", choices=["auto", "sat", "sun", "lineups"], default="auto")
    ap.add_argument("--slate-key")
    ap.add_argument("--at", help="pretend it is this ET time (ISO), for testing")
    ap.add_argument("--skip", action="append", default=[], choices=["props", "news", "tasks"])
    ap.add_argument("--json", help="write the checklist as JSON")
    ap.add_argument("--log", metavar="TASK", help="also record the result in task_runs under this task name (jobs/runlog.py)")
    ap.add_argument("--no-fail", action="store_true", help="exit 0 even when a check fails")
    args = ap.parse_args(argv)

    now = dt.datetime.fromisoformat(args.at).replace(tzinfo=ET) if args.at else dt.datetime.now(UTC)
    mode = pick_mode(now) if args.mode == "auto" else args.mode
    rest = Rest()
    try:
        c, slate = run(rest, mode, now, args.slate_key, set(args.skip))
    except Exception as e:                           # Supabase down / network: that IS the loud failure
        c, slate = Checklist(), None
        c.fail("database", f"could not read Supabase: {e}", "check https://status.supabase.com and the project dashboard")

    width = max(len(i["check"]) for i in c.items)
    print(f"Pre-lock checklist ({mode}) — {fmt_et(now)} — {slate['slate_key'] if slate else 'no slate'}")
    for i in c.items:
        print(f"  {i['level']:<4}  {i['check']:<{width}}  {i['detail']}" + (f"\n        -> {i['fix']}" if i["fix"] and i["level"] != "PASS" else ""))
    n_fail = sum(i["level"] == "FAIL" for i in c.items)
    n_warn = sum(i["level"] == "WARN" for i in c.items)
    verdict = "ALL GREEN" if c.status == "ok" else f"{n_fail} FAIL, {n_warn} WARN"
    print(f"RESULT: {verdict}")
    if args.json:
        json.dump({"mode": mode, "at": now.isoformat(), "slate": slate["slate_key"] if slate else None, "status": c.status,
                   "items": c.items}, open(args.json, "w"), indent=1)
    if args.log:
        try:
            import runlog
            fails = [i["check"] for i in c.items if i["level"] == "FAIL"]
            warns = [i["check"] for i in c.items if i["level"] == "WARN"]
            summary = verdict + (f" — fail: {', '.join(fails)}" if fails else "") + (f" — warn: {', '.join(warns)}" if warns else "")
            runlog.log(args.log, c.status, summary, {"mode": mode, "items": c.items})
        except Exception as e:
            print(f"(run log not written: {e})", file=sys.stderr)
    return 1 if (n_fail and not args.no_fail) else 0


if __name__ == "__main__":
    sys.exit(main())
