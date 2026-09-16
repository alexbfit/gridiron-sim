"""
Slate-day pipeline: import one or more salary CSVs, then project + simulate each slate.

  python jobs/pipeline.py data/slates/DKSalaries_2026_wk02_main.csv [more.csv ...]
  python jobs/pipeline.py data/ownership/DK-2026-02-main_standings.csv    # ownership → results → fit
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

    keys, own_keys = [], []
    for csv in args.csvs:
        path = Path(csv).resolve()
        stem = path.stem.lower()
        if "projections" in str(path.parent).lower():
            run("import_projections.py", str(path))
            continue
        if "ownership" in str(path.parent).lower() or "standings" in stem or "contest" in stem:
            out = run("import_ownership.py", str(path))
            for line in out.splitlines():
                if line.startswith("done — ") and " for " in line:
                    own_keys.append(line.rsplit(" for ", 1)[1].strip())
            continue
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
        run("simulate.py", "--slate-key", k)
    for k in own_keys:
        run("results.py", "--slate-key", k, "--force")
    if own_keys:
        run("fit_ownership.py")
    if not keys and not own_keys:
        print("nothing to do")


if __name__ == "__main__":
    main()
