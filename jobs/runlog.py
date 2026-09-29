"""
Run log for scheduled jobs (table task_runs, RPC log_task_run — supabase/pending/017_task_runs.sql).

  python jobs/runlog.py --task sunday-build --status started
  python jobs/runlog.py --task sunday-build --status ok --summary "50 GPP lineups saved; props-only setup; 2 overrides"
  python jobs/runlog.py --task gameday-refresh --status warn --source github --summary "failed steps: weather"
  python jobs/runlog.py --show                  # last 30 runs, newest first
  python jobs/runlog.py --show --task news-sat  # one task

Start and finish of one run share a run id: GITHUB_RUN_ID(-attempt) on GitHub, else RUNLOG_ID if set, else the
ET date (one run per task per day — a same-day re-run updates the same row). A run that logs 'started' and never
finishes stays 'started', which is exactly how a task that died mid-way shows up.

Never fatal: a logging failure (017 not applied yet, network) prints a note and exits 0 unless --strict.
Works with the anon key (Claude tasks) or the service key (GitHub).
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import urllib.error
import urllib.request
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
DEFAULT_URL = "https://hmckvrmsgmdsuyjsuvcq.supabase.co"
DEFAULT_ANON = "sb_publishable_45X4VDNlLYDp9yJERH40oA_yeTF4Jcz"
STATUSES = ("started", "ok", "warn", "failed", "skipped")


def _conn():
    url = (os.environ.get("SUPABASE_URL") or DEFAULT_URL).rstrip("/")
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY") or os.environ.get("SUPABASE_ANON_KEY") or DEFAULT_ANON
    return url, key


def default_run_id():
    if os.environ.get("GITHUB_RUN_ID"):
        return f"gh-{os.environ['GITHUB_RUN_ID']}-{os.environ.get('GITHUB_RUN_ATTEMPT', '1')}"
    if os.environ.get("RUNLOG_ID"):
        return os.environ["RUNLOG_ID"]
    return dt.datetime.now(ET).strftime("%Y-%m-%d")


def default_source():
    return "github" if os.environ.get("GITHUB_ACTIONS") == "true" else "claude"


def log(task, status, summary=None, details=None, run_id=None, source=None, strict=False):
    """Returns the row id, or None if the log could not be written."""
    if status not in STATUSES:
        raise ValueError(f"status must be one of {STATUSES}")
    url, key = _conn()
    body = json.dumps({"p_task": task, "p_run_id": run_id or default_run_id(), "p_status": status,
                       "p_summary": (summary or None) and str(summary)[:500], "p_details": details,
                       "p_source": source or default_source()}).encode()
    req = urllib.request.Request(f"{url}/rest/v1/rpc/log_task_run", data=body, method="POST",
                                 headers={"apikey": key, "Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        msg = e.read().decode(errors="replace")[:300]
        note = ("task_runs / log_task_run not found — run supabase/pending/017_task_runs.sql"
                if e.code == 404 or "log_task_run" in msg and "does not exist" in msg else f"HTTP {e.code}: {msg}")
    except Exception as e:                                   # network, DNS, timeout
        note = str(e)
    print(f"(run log not written for {task}: {note})", file=sys.stderr)
    if strict:
        raise SystemExit(1)
    return None


def show(task=None, limit=30):
    url, key = _conn()
    q = f"select=task,status,source,summary,started_at,finished_at&order=started_at.desc&limit={limit}"
    if task:
        q += f"&task=eq.{task}"
    req = urllib.request.Request(f"{url}/rest/v1/task_runs?{q}", headers={"apikey": key, "Authorization": f"Bearer {key}"})
    rows = json.load(urllib.request.urlopen(req, timeout=20))
    for r in rows:
        t = dt.datetime.fromisoformat(r["started_at"].replace("Z", "+00:00")).astimezone(ET)
        print(f"{t:%a %m/%d %I:%M %p}  {r['task']:<18} {r['status']:<8} {r['source']:<7} {(r.get('summary') or '')[:110]}")
    return rows


def main(argv=None):
    ap = argparse.ArgumentParser(description="Write (or show) the scheduled-job run log.")
    ap.add_argument("--task")
    ap.add_argument("--status", choices=STATUSES)
    ap.add_argument("--summary")
    ap.add_argument("--details-file", help="JSON file stored as details")
    ap.add_argument("--run-id")
    ap.add_argument("--source", choices=["claude", "github", "manual"])
    ap.add_argument("--strict", action="store_true", help="exit 1 if the log can't be written")
    ap.add_argument("--show", action="store_true")
    ap.add_argument("--limit", type=int, default=30)
    args = ap.parse_args(argv)
    if args.show:
        show(args.task, args.limit)
        return 0
    if not args.task or not args.status:
        ap.error("--task and --status are required (or --show)")
    details = json.load(open(args.details_file)) if args.details_file and os.path.exists(args.details_file) else None
    rid = log(args.task, args.status, args.summary, details, args.run_id, args.source, args.strict)
    if rid is not None:
        print(f"logged {args.task} {args.status} (row {rid})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
