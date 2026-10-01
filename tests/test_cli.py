"""Cheap, broad checks: every job compiles, every command the GitHub workflows run parses, the pre-lock checklist
passes on a good Sunday and fails loudly on a broken one, and the web JavaScript parses.

The workflow check exists because props.py rejected the `--source` flag the workflows passed it for a week
(9/20-9/26) and every Sunday props pull died with "unrecognized arguments" while the job still showed green.
"""
import argparse
import py_compile
import re
import runpy
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import JOBS, ROOT, run_main

WORKFLOWS = sorted((ROOT / ".github" / "workflows").glob("*.yml"))
SAMPLE = {"github.event.inputs.seasons": "2025", "github.event.inputs.reproject": "DK-2026-03-main",
          "steps.changed.outputs.files": "data/slates/DKSalaries_2026_wk03_main.csv"}


@pytest.mark.parametrize("path", sorted(JOBS.glob("*.py")) + sorted((ROOT / "tests").glob("*.py")), ids=lambda p: p.name)
def test_compiles(path, tmp_path):
    py_compile.compile(str(path), cfile=str(tmp_path / "x.pyc"), doraise=True)


def workflow_commands():
    out = []
    for wf in WORKFLOWS:
        for line in wf.read_text().splitlines():
            for m in re.finditer(r"python3? (jobs/[\w_]+\.py)([^|;&\n]*)", line):
                args = re.sub(r"\$\{\{\s*([^}]+?)\s*\}\}", lambda g: SAMPLE.get(g.group(1), "x"), m.group(2))
                args = re.sub(r"\$\{?[A-Za-z_][A-Za-z0-9_]*\}?", "ok", args)        # shell vars ($S = ok/warn/failed)
                args = args.split("#")[0].strip().rstrip("\\").strip()
                out.append((wf.name, m.group(1), args))
    return out


class _Parsed(BaseException):          # not Exception: jobs that catch everything (inactives.py) must not swallow it
    pass


@pytest.mark.parametrize("wf,script,args", workflow_commands(), ids=lambda v: v if isinstance(v, str) and len(v) < 40 else None)
def test_workflow_command_parses(wf, script, args, monkeypatch, db):
    """Run the script's own argparse on the exact arguments the workflow passes; stop right after parsing."""
    orig = argparse.ArgumentParser.parse_args
    seen = {}

    def parse_then_stop(self, a=None, ns=None):
        res = orig(self, a, ns)                       # argparse errors -> SystemExit(2) -> test fails below
        seen["ok"] = True
        raise _Parsed()

    monkeypatch.setattr(argparse.ArgumentParser, "parse_args", parse_then_stop)
    monkeypatch.setattr(sys, "argv", [script] + shlex.split(args))
    try:
        runpy.run_path(str(ROOT / script), run_name="__main__")
    except _Parsed:
        pass
    except SystemExit as e:
        pytest.fail(f"{wf}: `python {script} {args}` exited {e.code} while parsing its arguments")
    assert seen.get("ok"), f"{wf}: {script} never reached argument parsing"


# ------------------------------------------------------------------ pre-lock checklist
def test_preflight_green_on_week3_morning(db, monkeypatch, capsys):
    rc = run_main("preflight", ["--slate-key", "DK-2026-03-main", "--at", "2026-09-27T09:00", "--mode", "sun", "--skip", "tasks"], monkeypatch)
    out = capsys.readouterr().out
    assert rc == 0, out
    assert "FAIL" not in out.split("RESULT:")[0]
    for check in ("slate", "game lines", "sim", "props", "weather", "news (Sun)"):
        assert re.search(rf"PASS\s+{re.escape(check)}", out), f"{check} not PASS:\n{out}"


def test_preflight_fails_loudly_without_props(db, monkeypatch, capsys):
    db.tables["slate_projections"] = [r for r in db.rows("slate_projections") if r["method"] != "props"]
    rc = run_main("preflight", ["--slate-key", "DK-2026-03-main", "--at", "2026-09-27T09:00", "--mode", "sun", "--skip", "tasks"], monkeypatch)
    out = capsys.readouterr().out
    assert rc == 1 and re.search(r"FAIL\s+props", out) and "gameday-refresh with props=true" in out


def test_preflight_fails_on_missing_lines_and_lineups(db, monkeypatch, capsys):
    g = db.rows("games")
    for r in g[:3]:
        r["spread_line"] = None
    db.tables["slate_lineups"] = []
    rc = run_main("preflight", ["--slate-key", "DK-2026-03-main", "--at", "2026-09-27T10:50", "--mode", "lineups", "--skip", "tasks"], monkeypatch)
    out = capsys.readouterr().out
    assert rc == 1 and re.search(r"FAIL\s+lineups", out)
    # only games on this slate count; the fixture's first rows may be off-slate (TNF/MNF)
    slate_games = set(db.rows("slates")[0]["game_ids"])
    if any(r["game_id"] in slate_games for r in g[:3]):
        assert re.search(r"(FAIL|WARN)\s+game lines", out)


def test_preflight_saturday_sweep(db, monkeypatch, capsys):
    db.tables["news_notes"] = [r for r in db.rows("news_notes") if r["run"] != "sat"]
    db.tables["upset_picks"] = [r for r in db.rows("upset_picks") if r["run"] != "sat"]
    rc = run_main("preflight", ["--slate-key", "DK-2026-03-main", "--at", "2026-09-26T21:30", "--mode", "sat", "--skip", "tasks"], monkeypatch)
    out = capsys.readouterr().out
    assert rc == 1 and re.search(r"FAIL\s+news \(Sat\)", out)            # week 3's real Saturday sweep did fail


def test_preflight_sim_stale(db, monkeypatch, capsys):
    db.rows("slates")[0]["sim_meta"]["run_at"] = "2026-09-26T22:40:00+00:00"   # Saturday evening
    rc = run_main("preflight", ["--slate-key", "DK-2026-03-main", "--at", "2026-09-27T09:00", "--mode", "sun", "--skip", "tasks"], monkeypatch)
    out = capsys.readouterr().out
    assert rc == 1 and re.search(r"FAIL\s+sim", out)


# ------------------------------------------------------------------ web
WEB_JS = sorted((ROOT / "web").rglob("*.js"))


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
@pytest.mark.parametrize("path", WEB_JS, ids=lambda p: str(p.relative_to(ROOT)))
def test_web_js_parses(path):
    src = path.read_text(encoding="utf-8")
    is_module = bool(re.search(r"^\s*(import|export)\s", src, re.M))
    cmd = ["node", "--input-type=module", "--check"] if is_module else ["node", "--check", str(path)]
    r = subprocess.run(cmd, input=src if is_module else None, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr[-800:]


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_billing_functions_node_tests():
    """The Stripe plan mapping + webhook helpers under billing/ have their own node:test suite; run it here so CI covers it."""
    tests = sorted((ROOT / "billing").rglob("*.test.mjs"))
    assert tests, "billing/**/*.test.mjs missing"
    r = subprocess.run(["node", "--test", *map(str, tests)], capture_output=True, text=True, cwd=ROOT)
    assert r.returncode == 0, (r.stdout + r.stderr)[-1500:]


# ------------------------------------------------------------------ run log (017_task_runs.sql)
class _Resp:
    def __init__(self, body):
        self.body = body

    def read(self):
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_runlog_payload_and_run_id(monkeypatch):
    import io
    import json as _json
    import runlog
    sent = []

    def fake_urlopen(req, timeout=None):
        sent.append((req.full_url, _json.loads(req.data.decode())))
        return io.BytesIO(b"7")

    monkeypatch.setattr(runlog.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.delenv("GITHUB_RUN_ID", raising=False)
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.delenv("RUNLOG_ID", raising=False)
    assert runlog.log("sunday-build", "started") == 7
    assert runlog.log("sunday-build", "ok", "50 lineups") == 7
    (u1, a), (u2, b) = sent
    assert u1.endswith("/rest/v1/rpc/log_task_run")
    assert a["p_run_id"] == b["p_run_id"] and len(a["p_run_id"]) == 10          # ET date pairs start + finish
    assert a["p_source"] == "claude" and b["p_status"] == "ok" and b["p_summary"] == "50 lineups"
    monkeypatch.setenv("GITHUB_RUN_ID", "123")
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    runlog.log("gameday-refresh", "warn", "soft-failed: weather")
    assert sent[-1][1]["p_run_id"] == "gh-123-1" and sent[-1][1]["p_source"] == "github"


def test_runlog_never_fatal(monkeypatch, capsys):
    import urllib.error
    import runlog

    def boom(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 404, "not found", {}, io_body())

    def io_body():
        import io
        return io.BytesIO(b'{"message":"Could not find the function public.log_task_run"}')

    monkeypatch.setattr(runlog.urllib.request, "urlopen", boom)
    assert runlog.log("news-sat", "started") is None
    assert "017_task_runs.sql" in capsys.readouterr().err
    assert runlog.main(["--task", "news-sat", "--status", "ok"]) == 0


def test_preflight_logs_its_result(db, monkeypatch, capsys):
    import runlog
    got = []
    monkeypatch.setattr(runlog, "log", lambda *a, **k: got.append(a))
    run_main("preflight", ["--slate-key", "DK-2026-03-main", "--at", "2026-09-27T09:00", "--mode", "sun", "--skip", "tasks",
                           "--log", "preflight-sun"], monkeypatch)
    assert got and got[0][0] == "preflight-sun" and got[0][1] in ("ok", "warn")


def test_fetch_dk_picks_the_featured_main_slate_not_the_biggest_group():
    """9/30/2026: DraftKings added a 14-game '(Sun-Mon)' group mid-week; the old most-games rule imported it over the
    12-game main slate and every player id changed. The main slate is the plain (no suffix) Sunday 1 PM group."""
    import datetime as dt
    import fetch_dk_salaries as f
    now = dt.datetime(2026, 9, 30, 5, 0, tzinfo=f.ET)
    sun = dt.datetime(2026, 10, 4, 13, 0, tzinfo=f.ET)
    groups = [
        {"id": 154077, "contest_type": 21, "start": dt.datetime(2026, 10, 1, 20, 15, tzinfo=f.ET), "games": 16, "tag": "", "suffix": "(Thu-Mon)"},
        {"id": 154078, "contest_type": 21, "start": sun, "games": 12, "tag": "Featured", "suffix": ""},
        {"id": 154079, "contest_type": 21, "start": sun, "games": 8, "tag": "", "suffix": "(Early Only)"},
        {"id": 154080, "contest_type": 21, "start": sun, "games": 14, "tag": "", "suffix": "(Sun-Mon)"},
        {"id": 154090, "contest_type": 96, "start": sun, "games": 1, "tag": "", "suffix": ""},
    ]
    assert f.pick_main(groups, now)["id"] == 154078
    # without any plain group (lobby not fully posted yet) fall back to the biggest Sunday 1 PM classic group
    assert f.pick_main([g for g in groups if g["id"] != 154078], now)["id"] == 154080
    # next week's Sunday is ignored while this week's exists
    nxt = dict(groups[1], id=155000, start=sun + dt.timedelta(days=7), games=15)
    assert f.pick_main(groups + [nxt], now)["id"] == 154078
