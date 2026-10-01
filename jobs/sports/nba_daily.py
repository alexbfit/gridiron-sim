"""One command for an NBA slate day: DK salaries -> props -> projections -> 20 DK lineups -> data/sports/nba/<date>/.

    python jobs/sports/nba_daily.py                         # today's (ET) main slate, live DraftKings + props
    python jobs/sports/nba_daily.py --date 2026-10-21
    python jobs/sports/nba_daily.py --dk DK.csv --props-json props.json --out-dir /tmp/x    # fully offline (tests)

Steps (each one a separate module, so any of them can be re-run by hand):
  1. jobs/sports/fetch_dk.py        the main Classic slate CSV            -> <out>/DKSalaries_NBA_<date>.csv
  2. jobs/sports/props_nba.py       props (Odds API + Kalshi) -> MC       -> <out>/projections.csv (+ projections_info.json)
  3. jobs/build_lineups_sport.py    --sport NBA with the NBA settings     -> <out>/lineups.csv (DK upload) + lineups.txt
  4. <out>/README.md                a short slate report
  5. Supabase sport_projections (method 'props_nba') - only with SUPABASE_SERVICE_ROLE_KEY set AND the table from
     supabase/pending/018_sport_slates.sql applied; otherwise skipped silently.

Lineup settings come from the multisport backtest (data/sports_backtest/SUMMARY.txt, 348 NBA slates replayed in the
real DK fields): for 20-max contests the p98 ranking (+71% ROI) beat the game stack (+46%) and base p90 (+29%), and
p98 was the only mode above water in the flagship (+2.6%); the stack was second there. Default here: --rank p98 with
a 3-player game stack, exposure cap .35, min 3 unique players between lineups - all overridable.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
JOBS = HERE.parent
ROOT = JOBS.parent
ET = ZoneInfo("America/New_York")
sys.path.insert(0, str(JOBS))

DEFAULTS = dict(n=20, rank="p98", stack="3", max_exp=0.35, min_uniq=3, sims=5000, candidates=4)


def today_et() -> str:
    return dt.datetime.now(ET).date().isoformat()


def step_fetch(date: str, out_dir: Path, dk: str | None, group: int | None, log) -> Path:
    dst = out_dir / f"DKSalaries_NBA_{date}.csv"
    if dk:
        text = Path(dk).read_text(encoding="utf-8-sig")
        dst.write_text(text, encoding="utf-8")
        log(f"[1/4] salaries: copied {dk}")
        return dst
    from sports.fetch_dk import run as fetch_run
    log("[1/4] salaries: DraftKings lobby")
    p = fetch_run("NBA", date=date, group=group, out_path=str(dst))
    if not p:
        raise SystemExit("no salary file")
    return dst


def step_props(date: str, dk_csv: Path, out_dir: Path, a, log) -> tuple[Path, dict]:
    from sports.props_nba import run as props_run
    out = out_dir / "projections.csv"
    log("[2/4] projections from props")
    info = props_run(str(dk_csv), str(out), date=date, props_json=a.props_json, use_odds_api=not a.no_odds_api,
                     use_kalshi=not a.no_kalshi, max_games=a.max_games, sims=a.props_sims, seed=a.seed,
                     fallback_proj=a.fallback_proj, log=lambda *x: log("   ", *x))
    json.dump(info, open(out_dir / "projections_info.json", "w"), indent=1)
    if info.get("players", 0) < 16:
        raise SystemExit(f"only {info.get('players', 0)} projected players - not enough for lineups")
    return out, info


def step_lineups(dk_csv: Path, proj: Path, out_dir: Path, a, log) -> tuple[Path, str]:
    out = out_dir / "lineups.csv"
    cmd = [sys.executable, str(JOBS / "build_lineups_sport.py"), "--sport", "NBA", "--dk", str(dk_csv), "--proj", str(proj),
           "--n", str(a.n), "--sims", str(a.sims), "--rank", a.rank, "--max-exp", str(a.max_exp), "--min-uniq", str(a.min_uniq),
           "--candidates", str(a.candidates), "--out", str(out)]
    if a.stack and a.stack not in ("0", "none"):
        cmd += ["--stack", a.stack]
    if a.seed is not None:
        cmd += ["--seed", str(a.seed)]
    for x in a.lock:
        cmd += ["--lock", x]
    for x in a.exclude:
        cmd += ["--exclude", x]
    log("[3/4] lineups: " + " ".join(cmd[1:]))
    r = subprocess.run(cmd, text=True, capture_output=True, cwd=str(ROOT))
    (out_dir / "lineups.txt").write_text(r.stdout + ("\n--- stderr ---\n" + r.stderr if r.stderr else ""), encoding="utf-8")
    if r.returncode != 0 or not out.exists():
        log(r.stderr[-2000:])
        raise SystemExit(f"build_lineups_sport failed ({r.returncode})")
    return out, r.stdout


def step_report(date: str, out_dir: Path, dk_csv: Path, proj: Path, info: dict, lineups_txt: str, a) -> Path:
    rows = list(csv.DictReader(open(proj, encoding="utf-8")))
    top = sorted(rows, key=lambda r: -float(r["proj"] or 0))[:15]
    n_lineups = max(0, sum(1 for _ in open(out_dir / "lineups.csv")) - 1)
    md = [f"# NBA {date} - props projections + {n_lineups} DK lineups", "",
          f"- DK file: `{dk_csv.name}` ({info.get('dk_rows')} rows, {info.get('games')} games)",
          f"- Sources: {', '.join(info.get('sources') or []) or 'none'}",
          f"- Projected: {info.get('players')} players ({info.get('full')} full props, {info.get('partial')} partial, {info.get('fallback')} fallback)",
          f"- Builder: rank {a.rank}, game stack {a.stack or 'none'}, max exposure {a.max_exp}, min uniq {a.min_uniq}, {a.sims} sims",
          ""]
    if info.get("dd_check"):
        md.append(f"- Double-double check vs Kalshi: {info['dd_check']['n']} players, mean |dP| {info['dd_check']['mean_abs_diff']}")
        md.append("")
    md += ["## Top projections", "", "| player | team | pos | salary | proj | p85 | p99 | P(DD) | value |", "|---|---|---|---|---|---|---|---|---|"]
    for r in top:
        sal = float(r.get("salary") or 0)
        val = float(r["proj"]) / (sal / 1000) if sal else 0
        md.append(f"| {r['name']} | {r['team']} | {r['pos']} | {int(sal) if sal else ''} | {float(r['proj']):.1f} | {r['p85'] or ''} | {r['p99'] or ''} | {r['p_dd'] or ''} | {val:.2f} |")
    md += ["", "## Lineups", "", "```", lineups_txt.strip()[:6000], "```", ""]
    p = out_dir / "README.md"
    p.write_text("\n".join(md), encoding="utf-8")
    return p


def step_supabase(date: str, proj: Path, log) -> str:
    """Upsert the projections as method 'props_nba' when a service key is set and sport_projections exists."""
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
    url = (os.environ.get("SUPABASE_URL") or "").rstrip("/")
    if not key or not url:
        return "skipped (no service key)"
    rows = []
    for r in csv.DictReader(open(proj, encoding="utf-8")):
        def f(k):
            v = r.get(k)
            return float(v) if v not in (None, "") else None
        rows.append({"sport": "NBA", "slate_date": date, "player_name": r["name"], "team": r.get("team") or None, "opp": r.get("opp") or None,
                     "proj": f("proj"), "p25": f("p25"), "p50": f("p50"), "p75": f("p75"), "p85": f("p85"), "p95": f("p95"), "p99": f("p99"),
                     "source": r.get("source") or "props_nba", "updated_at": dt.datetime.now(dt.timezone.utc).isoformat()})
    req = urllib.request.Request(f"{url}/rest/v1/sport_projections?on_conflict=sport,slate_date,player_name,source", data=json.dumps(rows).encode(),
                                 method="POST", headers={"apikey": key, "Authorization": f"Bearer {key}", "Content-Type": "application/json",
                                                         "Prefer": "resolution=merge-duplicates,return=minimal"})
    try:
        with urllib.request.urlopen(req, timeout=60):
            return f"upserted {len(rows)} rows"
    except urllib.error.HTTPError as e:
        if e.code in (404, 406):
            return "skipped (sport_projections table missing - apply supabase/pending/018_sport_slates.sql)"
        body = e.read().decode(errors="replace")[:300]
        log(f"   supabase write failed: {e.code} {body}")
        return f"failed ({e.code})"
    except Exception as e:  # noqa: BLE001
        log(f"   supabase write failed: {e}")
        return "failed"


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", help="slate date (ET), default today")
    ap.add_argument("--out-dir", help="default data/sports/nba/<date>/")
    ap.add_argument("--dk", help="use this DK salary CSV instead of fetching")
    ap.add_argument("--group", type=int, help="DraftKings DraftGroupId to fetch instead of the main slate")
    ap.add_argument("--props-json", help="offline props outcomes JSON (skips the live APIs)")
    ap.add_argument("--no-odds-api", action="store_true")
    ap.add_argument("--no-kalshi", action="store_true")
    ap.add_argument("--max-games", type=int, default=0, help="cap Odds API per-game calls")
    ap.add_argument("--fallback-proj", help="CSV (name, proj) for players without props")
    ap.add_argument("--props-sims", type=int, default=20000)
    ap.add_argument("--n", type=int, default=DEFAULTS["n"])
    ap.add_argument("--rank", default=DEFAULTS["rank"], help="p98 (default) | p90 | mean | p50; '' = default")
    ap.add_argument("--stack", default=DEFAULTS["stack"], help="game stack size (0 = none)")
    ap.add_argument("--max-exp", type=float, default=DEFAULTS["max_exp"])
    ap.add_argument("--min-uniq", type=int, default=DEFAULTS["min_uniq"])
    ap.add_argument("--sims", type=int, default=DEFAULTS["sims"])
    ap.add_argument("--candidates", type=int, default=DEFAULTS["candidates"])
    ap.add_argument("--lock", action="append", default=[])
    ap.add_argument("--exclude", action="append", default=[])
    ap.add_argument("--seed", type=int)
    ap.add_argument("--no-supabase", action="store_true")
    a = ap.parse_args(argv)
    a.rank = a.rank or DEFAULTS["rank"]
    if a.rank not in ("p90", "p98", "mean", "p50"):
        ap.error(f"--rank must be p90 | p98 | mean | p50, not {a.rank!r}")
    date = a.date or today_et()
    out_dir = Path(a.out_dir) if a.out_dir else ROOT / "data" / "sports" / "nba" / date
    out_dir.mkdir(parents=True, exist_ok=True)
    log = lambda *x: print(*x, flush=True)
    log(f"NBA daily {date} -> {out_dir}")
    dk_csv = step_fetch(date, out_dir, a.dk, a.group, log)
    proj, info = step_props(date, dk_csv, out_dir, a, log)
    lineups, txt = step_lineups(dk_csv, proj, out_dir, a, log)
    rep = step_report(date, out_dir, dk_csv, proj, info, txt, a)
    log(f"[4/4] report {rep}")
    if not a.no_supabase:
        log("supabase: " + step_supabase(date, proj, log))
    n = max(0, sum(1 for _ in open(lineups)) - 1)
    log(f"done: {info.get('players')} projections, {n} lineups in {out_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
