"""
Slate-day pipeline: import one or more salary CSVs, then project each slate.

  python jobs/pipeline.py data/slates/DKSalaries_2026_wk02_main.csv [more.csv ...]
  python jobs/pipeline.py --reproject DK-2026-02-main       # projections only
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).parent


def run(*args):
    cmd = [sys.executable, *args]
    print("+", " ".join(str(a) for a in cmd), flush=True)
    res = subprocess.run(cmd, cwd=HERE, capture_output=True, text=True)
    print(res.stdout)
    if res.returncode != 0:
        print(res.stderr, file=sys.stderr)
        raise SystemExit(res.returncode)
    return res.stdout


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("csvs", nargs="*")
    ap.add_argument("--reproject", metavar="SLATE_KEY")
    ap.add_argument("--slate-type", default="main")
    args = ap.parse_args()

    keys = []
    for csv in args.csvs:
        path = Path(csv).resolve()
        stem = path.stem.lower()
        slate_type = args.slate_type
        for t in ("showdown", "primetime", "early", "afternoon", "sunday", "main"):
            if t in stem:
                slate_type = t
                break
        out = run("import_salaries.py", str(path), "--slate-type", slate_type)
        for line in out.splitlines():
            if line.startswith("done — slate "):
                keys.append(line.split()[3])
    if args.reproject:
        keys.append(args.reproject)
    for k in keys:
        run("project.py", "--slate-key", k)
    if not keys:
        print("nothing to do")


if __name__ == "__main__":
    main()
