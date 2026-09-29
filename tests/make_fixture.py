"""
Freeze a finished slate into tests/fixtures/<name>/ so the smoke tests run offline, on every push, against a real Sunday.

  python tests/make_fixture.py                       # DK-2026-03-main -> tests/fixtures/wk3
  python tests/make_fixture.py --slate-key DK-2026-05-main --name wk5 --sims 500

Reads with the public anon key. Keeps: the slate row, slate_board, slate_salaries, props rows, the week's games,
the recorded lineups, the ownership model, injury timestamp, news notes / upset picks (run + time only) and the first
--sims draws of the sim matrix (rounded to 0.1). Everything is gzipped JSON, ~0.5 MB for a 13-game slate.
"""
from __future__ import annotations

import argparse
import gzip
import json
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "jobs"))
from preflight import Rest  # noqa: E402

HERE = Path(__file__).resolve().parent


def dump(path, obj):
    with gzip.open(path, "wt", encoding="utf-8") as f:
        json.dump(obj, f, separators=(",", ":"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--slate-key", default="DK-2026-03-main")
    ap.add_argument("--name", default="wk3")
    ap.add_argument("--sims", type=int, default=500)
    args = ap.parse_args()
    rest = Rest()
    out = HERE / "fixtures" / args.name
    out.mkdir(parents=True, exist_ok=True)

    s = rest.get("slates", {"select": "*", "slate_key": f"eq.{args.slate_key}"})[0]
    sid, season, week = s["slate_id"], s["season"], s["week"]
    tables = {}
    s = dict(s)
    s.pop("results_meta", None); s.pop("flashback", None); s.pop("contest_meta", None)
    url = (s.get("sim_meta") or {}).get("url")
    s["sim_meta"] = {k: v for k, v in (s.get("sim_meta") or {}).items() if k != "url"}
    tables["slates"] = [s]
    page = lambda t, q: rest.get(t, {**q, "limit": "5000"})
    tables["slate_board"] = page("slate_board", {"select": "*", "slate_id": f"eq.{sid}"})
    tables["slate_salaries"] = page("slate_salaries", {"select": "*", "slate_id": f"eq.{sid}"})
    tables["slate_projections"] = page("slate_projections", {"select": "*", "slate_id": f"eq.{sid}", "method": "in.(props,sim)"})
    tables["games"] = page("games", {"select": "*", "season": f"eq.{season}", "week": f"eq.{week}"})
    tables["slate_lineups"] = page("slate_lineups", {"select": "*", "slate_id": f"eq.{sid}", "source": "eq.claude"})
    tables["model_params"] = page("model_params", {"select": "*", "param_key": "eq.ownership_model"})
    tables["injury_reports"] = rest.get("injury_reports", {"select": "updated_at", "order": "updated_at.desc", "limit": "1"})
    tables["news_notes"] = page("news_notes", {"select": "slate_id,run,created_at", "slate_id": f"eq.{sid}"})
    tables["upset_picks"] = page("upset_picks", {"select": "slate_id,run", "slate_id": f"eq.{sid}"})
    for t, rows in tables.items():
        dump(out / f"{t}.json.gz", rows)
        print(f"{t}: {len(rows)} rows")

    raw = urllib.request.urlopen(url, timeout=60).read()
    d = json.loads(gzip.decompress(raw) if raw[:2] == b"\x1f\x8b" else raw)
    d["scores"] = [[round(float(x), 1) for x in row[:args.sims]] for row in d["scores"]]
    d["n"] = args.sims
    dump(out / "matrix.json.gz", d)
    print(f"matrix: {len(d['players'])} players x {args.sims} sims")
    total = sum(p.stat().st_size for p in out.iterdir())
    print(f"wrote {out} ({total / 1e6:.2f} MB)")


if __name__ == "__main__":
    main()
