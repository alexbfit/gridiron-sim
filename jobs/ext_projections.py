"""
Pick the Sunday projection setup and write the outside-projection file the builder / late swap read.

  SS=$(python ext_projections.py --fresh-since 06:00)      # 9:30 build: sportsbook props must be from this morning
  SS=$(python ext_projections.py)                          # late swaps: props are frozen at kickoff, any age counts
  python ext_projections.py --slate-key DK-2026-03-main --setup props --out-dir /tmp   # force a setup (testing)

Prints ONE line on stdout — the extra build_lineups / late_swap flags ($SS) — and a status line on stderr.
The rules are the ones the three Sunday tasks carried as pasted scripts until 9/29 (same numbers, now tested):

  1. SaberSim file ../data/projections/DK-<season>-<WW>-main_sabersim.csv exists
       -> RB implied-total adjustment on 'SS Proj' -> <out>/ss_rbadj.csv
       -> "--ext-file <out>/ss_rbadj.csv --ext-sim 1.0"                                    (setup: sabersim)
  2. else >= 150 sportsbook-sourced props rows (components.src has 'odds'; with --fresh-since, updated after
     that ET time today) -> props file with the RB rule -> <out>/props_rbadj.csv (Name, Team, Pos, Proj)
       -> "--props 0 --props-missing= --ext-file <out>/props_rbadj.csv --ext-sim 0.55
           --ext-missing QB=0.15,RB=0.8,WR=0.4,TE=0.5 --max-te 1 --min-uniq 3"               (setup: props)
     (fewer than 100 players in the file -> falls through to 3)
  3. else "" — props + sim defaults, weakest setup                                          (setup: none)

RB implied-total rule (adopted 9/26): RB proj x clip(1 + 0.0115 * (team implied total - 22.5), 0.85, 1.15),
implied = total/2 +/- spread/2 (nflverse spread_line > 0 = home favoured).
Exit code 0 always when a setup was chosen (even 'none'); 1 only if the database can't be read.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import os
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

from preflight import Rest, parse_ts

ET = ZoneInfo("America/New_York")
ALIAS = {"LAR": "LA", "JAC": "JAX", "WSH": "WAS"}
MIN_BOOKS = 150
MIN_PROPS_FILE = 100
RB_K, RB_BASE, RB_LO, RB_HI = 0.0115, 22.5, 0.85, 1.15
PROPS_FLAGS = "--props 0 --props-missing= --ext-file {f} --ext-sim 0.55 --ext-missing QB=0.15,RB=0.8,WR=0.4,TE=0.5 --max-te 1 --min-uniq 3"
SABERSIM_FLAGS = "--ext-file {f} --ext-sim 1.0"
REPO = Path(__file__).resolve().parent.parent


def implied_totals(games):
    imp = {}
    for g in games:
        if g.get("spread_line") is None or g.get("total_line") is None:
            continue
        t, s = float(g["total_line"]), float(g["spread_line"])
        imp[g["home_team"]] = t / 2 + s / 2
        imp[g["away_team"]] = t / 2 - s / 2
    return imp


def rb_factor(implied):
    return min(RB_HI, max(RB_LO, 1 + RB_K * (implied - RB_BASE)))


def sabersim_file(season, week, root=REPO):
    p = root / "data" / "projections" / f"DK-{season}-{int(week):02d}-main_sabersim.csv"
    return p if p.exists() else None


def write_sabersim(src, imp, out):
    rows = list(csv.DictReader(open(src, encoding="utf-8-sig", newline="")))
    n = 0
    for r in rows:
        it = imp.get(ALIAS.get(r.get("Team"), r.get("Team")))
        if r.get("Pos") == "RB" and it is not None and r.get("SS Proj"):
            r["SS Proj"] = f"{float(r['SS Proj']) * rb_factor(it):.2f}"
            n += 1
    with open(out, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    return len(rows), n


def write_props(board, props, imp, out):
    rows, n = [], 0
    for p in props:
        b = board.get(p["site_player_id"])
        if not b or p.get("mean") is None:
            continue
        proj = float(p["mean"])
        it = imp.get(ALIAS.get(b["team"], b["team"]))
        if b["position"] == "RB" and it is not None:
            proj *= rb_factor(it)
            n += 1
        rows.append({"Name": b["player_name"], "Team": b["team"], "Pos": b["position"], "Proj": f"{proj:.2f}"})
    with open(out, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["Name", "Team", "Pos", "Proj"])
        w.writeheader()
        w.writerows(rows)
    return len(rows), n


def choose(rest, slate_key=None, setup="auto", out_dir="/tmp", fresh_since=None, now=None, root=REPO):
    """Returns (flags, info dict)."""
    params = {"select": "slate_id,slate_key,season,week", "order": "imported_at.desc", "limit": "1"}
    if slate_key:
        params["slate_key"] = f"eq.{slate_key}"
    s = rest.get("slates", params)[0]
    season, week = int(s["season"]), int(s["week"])
    games = rest.get("games", {"select": "home_team,away_team,spread_line,total_line", "season": f"eq.{season}", "week": f"eq.{week}"})
    imp = implied_totals(games)
    info = {"slate": s["slate_key"], "teams_with_lines": len(imp)}
    out_dir = Path(out_dir)

    ss = sabersim_file(season, week, root)
    if setup in ("auto", "sabersim") and ss:
        out = out_dir / "ss_rbadj.csv"
        try:
            n_rows, n_rb = write_sabersim(ss, imp, out)
            info.update(setup="sabersim", file=str(out), source=str(ss), players=n_rows, rbs_adjusted=n_rb)
            return SABERSIM_FLAGS.format(f=out), info
        except Exception as e:                               # unadjusted SaberSim beats no SaberSim
            info.update(setup="sabersim", file=str(ss), note=f"RB adjustment failed ({e}); unadjusted SaberSim file used")
            return SABERSIM_FLAGS.format(f=ss), info
    if setup == "sabersim":
        raise SystemExit(f"no SaberSim file for {season} week {week}")

    props = rest.get("slate_projections", {"select": "site_player_id,mean,components,updated_at", "slate_id": f"eq.{s['slate_id']}",
                                           "method": "eq.props", "limit": "3000"})
    cutoff = None
    if fresh_since:
        hh, mm = (int(x) for x in fresh_since.split(":"))
        today = (now or dt.datetime.now(ET)).astimezone(ET).date()
        cutoff = dt.datetime.combine(today, dt.time(hh, mm), ET)
    books = [r for r in props if "odds" in (((r.get("components") or {}).get("src")) or [])
             and (cutoff is None or parse_ts(r["updated_at"]) >= cutoff)]
    info.update(props_rows=len(props), book_rows=len(books), books=len(books) >= MIN_BOOKS)
    if setup in ("auto", "props") and (len(books) >= MIN_BOOKS or setup == "props"):
        board = {r["site_player_id"]: r for r in rest.get("slate_board", {"select": "site_player_id,player_name,team,position",
                                                                           "slate_id": f"eq.{s['slate_id']}", "limit": "3000"})}
        out = out_dir / "props_rbadj.csv"
        n_rows, n_rb = write_props(board, props, imp, out)
        info.update(file=str(out), players=n_rows, rbs_adjusted=n_rb)
        if n_rows >= MIN_PROPS_FILE:
            info["setup"] = "props"
            return PROPS_FLAGS.format(f=out), info
        info["note"] = f"props file has only {n_rows} players (< {MIN_PROPS_FILE})"
    info["setup"] = "none"
    return "", info


def main(argv=None):
    ap = argparse.ArgumentParser(description="Choose the Sunday projection setup; print the extra builder flags.")
    ap.add_argument("--slate-key")
    ap.add_argument("--setup", choices=["auto", "sabersim", "props", "none"], default="auto")
    ap.add_argument("--out-dir", default="/tmp")
    ap.add_argument("--fresh-since", metavar="HH:MM", help="count only sportsbook props updated after this ET time today (9:30 build: 06:00)")
    ap.add_argument("--json", help="also write the decision as JSON")
    args = ap.parse_args(argv)
    if args.setup == "none":
        print("")
        print("setup: none (forced)", file=sys.stderr)
        return 0
    try:
        flags, info = choose(Rest(), args.slate_key, args.setup, args.out_dir, args.fresh_since)
    except SystemExit:
        raise
    except Exception as e:
        print(f"ext_projections: could not read the database: {e}", file=sys.stderr)
        return 1
    desc = {"sabersim": "SaberSim 1.0", "props": "props-only (sportsbook props 55% + sim)", "none": "props + sim defaults (NO sportsbook props — weakest)"}[info["setup"]]
    bits = [f"{k}={v}" for k, v in info.items() if k not in ("setup", "slate")]
    print(f"setup: {desc} — {info['slate']} — " + ", ".join(bits), file=sys.stderr)
    print(flags)
    if args.json:
        json.dump({"flags": flags, **info}, open(args.json, "w"), indent=1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
