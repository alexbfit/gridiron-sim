"""In-memory stand-ins for Supabase, fed from a frozen fixture (tests/fixtures/<name>/*.json.gz).

FakeClient mimics the supabase-py calls the jobs make (table().select().eq().in_().order().range().execute(),
update/insert/upsert/delete, rpc). FakeRest mimics the plain-REST reads in preflight.py / ext_projections.py
(PostgREST query strings: eq. / in.() / gte. / lte. / gt. / lt. / is.null, order, limit, count).
Writes land in memory and in `db.writes` / `db.rpc_calls` so tests can assert on them. No network.
"""
from __future__ import annotations

import copy
import gzip
import json
from pathlib import Path

FIX = Path(__file__).resolve().parent / "fixtures"


class FakeDB:
    def __init__(self, name="wk3", matrix_path=None):
        self.dir = FIX / name
        self.tables = {}
        for p in self.dir.glob("*.json.gz"):
            if p.name.startswith("matrix"):
                continue
            with gzip.open(p, "rt", encoding="utf-8") as f:
                self.tables[p.name[:-len(".json.gz")]] = json.load(f)
        self.matrix_file = self.dir / "matrix.json.gz"
        for s in self.tables.get("slates", []):
            s.setdefault("sim_meta", {})
            s["sim_meta"]["url"] = (matrix_path or self.matrix_file).resolve().as_uri()
        # slate_board carries mean from the sim; keep it consistent with the fixture's sim rows
        self.writes, self.rpc_calls = [], []

    def rows(self, table):
        return self.tables.setdefault(table, [])


def _cmp(a, b):
    """PostgREST compares typed values; the fixture stores JSON, so compare numbers as numbers, else as strings."""
    try:
        return float(a), float(b)
    except (TypeError, ValueError):
        return str(a), str(b)


class _Result:
    def __init__(self, data, count=None):
        self.data, self.count = data, count


class FakeQuery:
    def __init__(self, db, table):
        self.db, self.table = db, table
        self.filters, self.orders = [], []
        self.lo, self.hi, self.lim = None, None, None
        self.action, self.payload = "select", None

    # --- builders
    def select(self, cols="*", count=None, **kw):
        return self

    def _f(self, fn):
        self.filters.append(fn)
        return self

    def eq(self, c, v):
        return self._f(lambda r: str(r.get(c)) == str(v) if not isinstance(v, bool) else r.get(c) == v)

    def neq(self, c, v):
        return self._f(lambda r: str(r.get(c)) != str(v))

    def in_(self, c, vals):
        vs = {str(v) for v in vals}
        return self._f(lambda r: str(r.get(c)) in vs)

    def is_(self, c, v):
        return self._f(lambda r: (r.get(c) is None) if str(v).lower() == "null" else r.get(c) is not None)

    def gte(self, c, v):
        return self._f(lambda r: r.get(c) is not None and _cmp(r.get(c), v)[0] >= _cmp(r.get(c), v)[1])

    def lte(self, c, v):
        return self._f(lambda r: r.get(c) is not None and _cmp(r.get(c), v)[0] <= _cmp(r.get(c), v)[1])

    def gt(self, c, v):
        return self._f(lambda r: r.get(c) is not None and _cmp(r.get(c), v)[0] > _cmp(r.get(c), v)[1])

    def lt(self, c, v):
        return self._f(lambda r: r.get(c) is not None and _cmp(r.get(c), v)[0] < _cmp(r.get(c), v)[1])

    def order(self, c, desc=False, **kw):
        self.orders.append((c, desc))
        return self

    def limit(self, n):
        self.lim = n
        return self

    def range(self, a, b):
        self.lo, self.hi = a, b
        return self

    def update(self, payload):
        self.action, self.payload = "update", payload
        return self

    def insert(self, payload, **kw):
        self.action, self.payload = "insert", payload
        return self

    def upsert(self, payload, **kw):
        self.action, self.payload = "upsert", payload
        return self

    def delete(self):
        self.action = "delete"
        return self

    # --- run
    def _match(self):
        return [r for r in self.db.rows(self.table) if all(f(r) for f in self.filters)]

    def execute(self):
        if self.action == "select":
            rows = self._match()
            for c, desc in reversed(self.orders):
                rows.sort(key=lambda r: (r.get(c) is None, _cmp(r.get(c), 0)[0]), reverse=desc)
            if self.lo is not None:
                rows = rows[self.lo:self.hi + 1]
            if self.lim is not None:
                rows = rows[:self.lim]
            return _Result(copy.deepcopy(rows))
        self.db.writes.append((self.table, self.action, copy.deepcopy(self.payload)))
        if self.action == "update":
            hit = self._match()
            for r in hit:
                r.update(copy.deepcopy(self.payload))
            return _Result(hit)
        if self.action in ("insert", "upsert"):
            rows = self.payload if isinstance(self.payload, list) else [self.payload]
            self.db.rows(self.table).extend(copy.deepcopy(rows))
            return _Result(rows)
        if self.action == "delete":
            hit = self._match()
            self.db.tables[self.table] = [r for r in self.db.rows(self.table) if r not in hit]
            return _Result(hit)


class _Rpc:
    def __init__(self, db, name, params):
        self.db, self.name, self.params = db, name, params

    def execute(self):
        self.db.rpc_calls.append((self.name, copy.deepcopy(self.params)))
        if self.name == "save_lineups":
            return _Result(len(self.params.get("p_lineups") or []))
        if self.name == "log_task_run":
            return _Result(1)
        return _Result(None)


class FakeClient:
    def __init__(self, db):
        self.db = db

    def table(self, name):
        return FakeQuery(self.db, name)

    def rpc(self, name, params):
        return _Rpc(self.db, name, params)


class FakeRest:
    """preflight.Rest / ext_projections look-alike."""
    def __init__(self, db):
        self.db = db

    def get(self, path, params=None, count=False):
        params = dict(params or {})
        q = FakeQuery(self.db, path)
        for k, v in params.items():
            if k in ("select",):
                continue
            if k == "order":
                for part in v.split(","):
                    col, *d = part.split(".")
                    q.order(col, desc=("desc" in d))
                continue
            if k == "limit":
                q.limit(int(v))
                continue
            op, _, val = v.partition(".")
            if op == "in":
                q.in_(k, [x.strip() for x in val.strip("()").split(",") if x.strip()])
            elif op == "is":
                q.is_(k, val)
            else:
                getattr(q, {"eq": "eq", "gte": "gte", "lte": "lte", "gt": "gt", "lt": "lt", "neq": "neq"}[op])(k, val)
        rows = q.execute().data
        return len(rows) if count else rows

    def head_ok(self, url):
        return url.startswith("file://") and Path(url[len("file://"):]).exists()
