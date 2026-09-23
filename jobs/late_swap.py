"""
Late swap: fix the recorded lineups after inactives (11:30 AM ET) and before the late window (4:05 / 4:25 PM ET).

  python jobs/late_swap.py --exclude "Player A" --exclude "Player B" [--set "Player C=6"] \
      --out /tmp/DK_gpp50_week3_swapped.csv --json /tmp/swap.json [--entries DK_entries.csv]

What it does
  1. Loads the slate board (+ props blend, like build_lineups) and the lineups recorded by the Sunday build
     (slate_lineups, source 'claude'; --contest gpp/cash/all).
  2. Freezes every player whose game has kicked off (ET) — DraftKings does not allow swapping them.
  3. For each lineup that contains an --exclude'd player (ruled out / inactive) whose game has NOT started, finds the
     best replacement: same position (or any RB/WR/TE when the slot is effectively the FLEX), not already in the
     lineup, game not started, fits the salary cap, respects max players per team, and maximizes the blended
     projection (tie-break: sim p90). --set lowers/raises a projection before the choice (e.g. a QB on a pitch count).
  4. Writes the full lineup set in DraftKings' upload column order (unchanged lineups included) and a JSON report of
     every swap. With --entries (the CSV DraftKings gives you under "Edit multiple lineups" -> Download), it writes an
     EDIT file instead: the same rows with Entry ID / Contest fields preserved, so the upload edits your live entries.

The nightly scoring grades the ORIGINAL saved lineups unless --save is passed, which replaces them with the swapped
versions (append a note); the swap report keeps the originals for reference either way.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import sys
from zoneinfo import ZoneInfo

import numpy as np

import build_lineups as bl
from common import fetch_all, get_client, norm_name

ET = ZoneInfo("America/New_York")


def kickoff_et(g):
    t = (g.get("gametime") or "13:00")[:5]
    return dt.datetime.fromisoformat(f"{g['gameday']} {t}").replace(tzinfo=ET)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--slate-key")
    ap.add_argument("--contest", default="gpp", choices=["gpp", "cash", "all"])
    ap.add_argument("--source", default="claude")
    ap.add_argument("--exclude", action="append", default=[], help="player ruled out / inactive (repeatable)")
    ap.add_argument("--set", action="append", default=[], help='"Name=proj" projection override before choosing swaps')
    ap.add_argument("--props", type=float, default=0.85)
    ap.add_argument("--max-team", type=int, default=4)
    ap.add_argument("--now", help="override the clock (ISO, ET) for tests")
    ap.add_argument("--entries", help="DraftKings 'Download entries' CSV — write an edit file for these Entry IDs")
    ap.add_argument("--out", help="CSV path (DK upload layout)")
    ap.add_argument("--json", help="swap report path")
    ap.add_argument("--save", action="store_true", help="replace the recorded lineups with the swapped ones (graded Monday)")
    ap.add_argument("--note", default="late swap")
    args = ap.parse_args()

    client = get_client()
    slate, rows, ext, own_model, matrix = bl.load_board(client, args.slate_key)
    site = bl.SITES[slate["site"]]
    byid = {r["site_player_id"]: r for r in rows}
    byname = {}
    for r in rows:
        byname.setdefault(norm_name(r["player_name"]), []).append(r)

    def find(name):
        c = byname.get(norm_name(name))
        if not c:
            raise SystemExit(f"player not on slate: {name}")
        return c[0]

    # blended projection (same as the builder): props pull + overrides
    overrides = {find(s.split("=")[0])["site_player_id"]: float(s.split("=")[1]) for s in args.set}
    def proj(r):
        i = r["site_player_id"]
        if i in overrides:
            return overrides[i]
        m = float(r["mean"] or 0)
        pv = r.get("props")
        if pv is not None and args.props > 0 and m > 0:
            return (1 - args.props) * m + args.props * float(pv)
        return m
    def p90(r):
        a = matrix.get(r["site_player_id"]) if matrix else None
        if a is None:
            return proj(r)
        f = proj(r) / float(r["mean"]) if r["mean"] and float(r["mean"]) > 0.5 else 1.0
        return float(np.percentile(a, 90)) * f

    # games and the clock
    now = dt.datetime.fromisoformat(args.now).replace(tzinfo=ET) if args.now else dt.datetime.now(ET)
    games = {g["game_id"]: g for g in fetch_all(client.table("games").select("game_id,gameday,gametime,home_score")
                                                .in_("game_id", list(slate.get("game_ids") or [])), order="game_id")}
    started = {gid for gid, g in games.items() if kickoff_et(g) <= now or (g.get("home_score") is not None and not args.now)}
    def locked(r):
        return r.get("game_id") in started
    excludes = {find(n)["site_player_id"] for n in args.exclude}
    out_status = {r["site_player_id"] for r in rows if bl.eff_status(r) in bl.OUT_STATUSES}
    print(f"{slate['slate_key']} · {now:%a %I:%M %p} ET · {len(started)}/{len(games)} games started · "
          f"{len(excludes)} excluded by you, {len(out_status)} marked OUT on the board", file=sys.stderr)

    # recorded lineups
    q = client.table("slate_lineups").select("*").eq("slate_id", slate["slate_id"]).eq("source", args.source)
    if args.contest != "all":
        q = q.eq("contest", args.contest)
    saved = fetch_all(q, order=["contest", "idx"])
    if not saved:
        raise SystemExit("no recorded lineups for this slate (the Sunday build saves them with --save)")

    report, new_sets = [], []
    for L in saved:
        ids = list(L["player_ids"])
        ps = [byid[i] for i in ids if i in byid]
        if len(ps) != 9:
            report.append({"contest": L["contest"], "idx": L["idx"], "error": "lineup has players not on the board"}); new_sets.append((L, ids)); continue
        bad = [p for p in ps if (p["site_player_id"] in excludes or p["site_player_id"] in out_status)]
        swaps = []
        for p in bad:
            if locked(p):
                swaps.append({"out": p["player_name"], "in": None, "reason": "game already started — cannot swap"}); continue
            cur = [byid[i] for i in ids]
            cap_room = site["cap"] - sum(c["salary"] for c in cur if c["site_player_id"] != p["site_player_id"])
            teams = {}
            for c in cur:
                if c["site_player_id"] != p["site_player_id"]:
                    teams[c["team"]] = teams.get(c["team"], 0) + 1
            # position options: same position; a FLEX-eligible slot may take any RB/WR/TE as long as roster mins hold
            pos_counts = {}
            for c in cur:
                if c["site_player_id"] != p["site_player_id"]:
                    pos_counts[c["position"]] = pos_counts.get(c["position"], 0) + 1
            mins = {"QB": 1, "RB": 2, "WR": 3, "TE": 1, "DST": 1}
            allowed = {p["position"]}
            if p["position"] in ("RB", "WR", "TE") and pos_counts.get(p["position"], 0) >= mins.get(p["position"], 0):
                allowed = {"RB", "WR", "TE"}
            cands = [r for r in rows if r["position"] in allowed and r["site_player_id"] not in ids and r["site_player_id"] not in excludes
                     and r["site_player_id"] not in out_status and not locked(r) and r["salary"] <= cap_room
                     and teams.get(r["team"], 0) + 1 <= min(args.max_team, site["max_team"]) and proj(r) > 0]
            if not cands:
                swaps.append({"out": p["player_name"], "in": None, "reason": "no eligible replacement under the cap"}); continue
            best = max(cands, key=lambda r: (round(proj(r), 1), p90(r)))
            ids[ids.index(p["site_player_id"])] = best["site_player_id"]
            swaps.append({"out": p["player_name"], "out_proj": round(proj(p), 1), "in": best["player_name"], "in_proj": round(proj(best), 1),
                          "in_salary": best["salary"], "salary_left": cap_room - best["salary"]})
        new_sets.append((L, ids))
        if swaps:
            report.append({"contest": L["contest"], "idx": L["idx"], "swaps": swaps,
                           "proj_before": round(sum(proj(byid[i]) for i in L["player_ids"] if i in byid and byid[i]["site_player_id"] not in excludes and byid[i]["site_player_id"] not in out_status), 1),
                           "proj_after": round(sum(proj(byid[i]) for i in ids), 1)})

    n_changed = sum(1 for r in report if r.get("swaps") and any(s.get("in") for s in r["swaps"]))
    n_stuck = sum(1 for r in report for s in r.get("swaps", []) if s.get("in") is None)
    print(f"{len(saved)} lineups · {n_changed} changed · {n_stuck} swaps impossible", file=sys.stderr)
    for r in report:
        for s in r.get("swaps", []):
            print(f"  #{r['idx']:>2} {r['contest']}: {s['out']} -> {s['in'] or '(none: ' + s.get('reason', '') + ')'}"
                  + (f"  proj {s['out_proj']} -> {s['in_proj']}, ${int(s['in_salary']):,}" if s.get("in") else ""), file=sys.stderr)

    keys = ["QB", "RB1", "RB2", "WR1", "WR2", "WR3", "TE", "FLEX", site["def"]]
    if args.out:
        with open(args.out, "w", newline="") as f:
            w = csv.writer(f)
            if args.entries:
                # DK edit file: keep Entry ID, Contest Name, Contest ID, Entry Fee from the download, replace the slots in order
                with open(args.entries, encoding="utf-8-sig", newline="") as fh:
                    ent = list(csv.reader(fh))
                hdr = ent[0]
                w.writerow(hdr)
                slots_from = next((i for i, h in enumerate(hdr) if h.strip().upper() == "QB"), 4)
                lineups_iter = iter(new_sets)
                for row in ent[1:]:
                    if not row or not row[0].strip():
                        continue
                    try:
                        L, ids = next(lineups_iter)
                    except StopIteration:
                        break
                    s = bl.assign_slots([byid[i] for i in ids], site)
                    cells = row[:slots_from] + [f"{s[k]['player_name']} ({s[k]['site_player_id']})" for k in keys]
                    w.writerow(cells)
            else:
                w.writerow(site["slots"])
                for L, ids in new_sets:
                    s = bl.assign_slots([byid[i] for i in ids], site)
                    w.writerow([s[k]["site_player_id"] for k in keys])
        print(f"wrote {args.out}", file=sys.stderr)
    if args.json:
        json.dump({"slate": slate["slate_key"], "now": now.isoformat(), "started_games": sorted(started), "excluded": sorted(args.exclude),
                   "lineups": [{"contest": L["contest"], "idx": L["idx"], "ids": ids,
                                "players": [byid[i]["player_name"] for i in ids], "proj": round(sum(proj(byid[i]) for i in ids), 1)} for L, ids in new_sets],
                   "swaps": report}, open(args.json, "w"), indent=1)
    if args.save and n_changed:
        payload = [{"ids": ids, "proj": round(sum(proj(byid[i]) for i in ids), 1), "own": L.get("own"), "p10": L.get("p10"), "p50": L.get("p50"), "p90": L.get("p90"),
                    "note": f"{L.get('note') or ''} | {args.note}"[:500]} for L, ids in new_sets if L["contest"] == (args.contest if args.contest != "all" else L["contest"])]
        try:
            for contest in sorted({L["contest"] for L, _ in new_sets}):
                pl = [x for (L, _), x in zip(new_sets, payload) if L["contest"] == contest]
                n = client.rpc("save_lineups", {"p_slate_key": slate["slate_key"], "p_source": args.source, "p_contest": contest,
                                                "p_lineups": pl, "p_replace": True}).execute().data
                print(f"saved {n} swapped {contest} lineups", file=sys.stderr)
        except Exception as e:
            print(f"save failed (the slate has probably started — the RPC refuses saves after kickoff): {e}", file=sys.stderr)


if __name__ == "__main__":
    main()
