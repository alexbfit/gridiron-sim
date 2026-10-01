import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
JOBS = ROOT / "jobs"
sys.path.insert(0, str(JOBS))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fakedb import FakeClient, FakeDB, FakeRest  # noqa: E402

# never touch the real project from a test, even if a developer has keys exported
for k in ("SUPABASE_URL", "SUPABASE_SERVICE_ROLE_KEY", "SUPABASE_ANON_KEY", "ODDS_API_KEY"):
    os.environ.pop(k, None)
os.environ["SUPABASE_URL"] = "http://fake.invalid"
os.environ["SUPABASE_ANON_KEY"] = "fake"


@pytest.fixture
def db(monkeypatch):
    """Week-3 slate in memory; every job module's get_client / Rest points at it."""
    d = FakeDB("wk3")
    client = FakeClient(d)
    import common
    monkeypatch.setattr(common, "get_client", lambda need_write=False: client)
    for mod in ("build_lineups", "late_swap", "flashback", "props", "results", "preflight", "ext_projections", "runlog", "recap"):
        try:
            m = __import__(mod)
        except Exception:
            continue
        if hasattr(m, "get_client"):
            monkeypatch.setattr(m, "get_client", lambda need_write=False: client)
        if hasattr(m, "Rest"):
            monkeypatch.setattr(m, "Rest", lambda *a, **k: FakeRest(d))
    d.client = client
    return d


def run_main(module, argv, monkeypatch):
    """Run module.main() with sys.argv = [module] + argv; returns the exit code (0 when main returns normally)."""
    m = __import__(module)
    monkeypatch.setattr(sys, "argv", [module + ".py"] + list(argv))
    try:
        rc = m.main()
    except SystemExit as e:
        rc = e.code if isinstance(e.code, int) else (0 if e.code is None else 1)
    return rc or 0
