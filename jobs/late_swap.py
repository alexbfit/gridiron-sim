"""
Late swap: fix the recorded lineups after inactives (11:30 AM ET) and before the late window (4:05 / 4:25 PM ET).

  # quick swap (defense): pull ruled-out players, best 1-for-1 replacement
  python jobs/late_swap.py --exclude "Player A" --exclude "Player B" [--set "Player C=6"] \
      --out /tmp/DK_week3_swapped.csv --json /tmp/swap.json [--entries DK_entries.csv]

  # full swap (offense): quick swap first, then re-solve every lineup's OPEN slots on the latest board
  python jobs/late_swap.py --full --exclude "Player A" --out ... --json ...

What it does
  1. Loads the slate board (+ props blend, like build_lineups) and the lineups recorded by the Sunday build
     (slate_lineups, source 'claude'; --contest gpp/cash/all).
  2. Freezes every player whose game has kicked off (ET) — DraftKings does not allow swapping them.
  3. QUICK SWAP: for each lineup that contains an --exclude'd player (ruled out / inactive) or a player the board
     marks OUT whose game has NOT started, finds the best replacement: same position (or any RB/WR/TE when the
     slot is effectively the FLEX), not already in the lineup, game not started, fits the salary cap, respects max
     players per team, and maximizes the blended projection (tie-break: sim p90). --set lowers/raises a
     projection before the choice (e.g. a QB on a pitch count).
  4. FULL SWAP (--full): for every lineup, the started players are locked in place and the open slots are
     re-solved with the builder's MIP on the CURRENT board (fresh sim after inactives, props, weather): a few
     candidates per lineup (the deterministic best plus jittered variants, like the Sunday build), ranked by sim
     p90 (gpp) / p50 (cash) with the quick-swapped original kept as a candidate. A lineup only changes when the
     best candidate beats the original by --gain points on that key. Stack / bring-back / team-limit rules and a
     portfolio exposure cap (--max-exp) and --min-uniq between the final lineups apply, so the whole set is
     re-diversified around the news, not just the lineups that had a scratched player. Prints the exposure
     delta (who you got on / off) so you can sanity-check the direction.
  5. Writes the full lineup set in DraftKings' upload column order (unchanged lineups included) and a JSON report of
     every swap. With --entries (the CSV DraftKings gives you under "Edit multiple lineups" -> Download), it writes an
     EDIT file instead: the same rows with Entry ID / Contest fields preserved, so the upload edits your live entries.

The nightly scoring grades the ORIGINAL saved lineups unless --save is passed, which replaces them with the swapped
versions (append a note); the swap report keeps the originals for reference either way. save_lineups refuses saves
after the slate's first kickoff, so only the pre-lock run can --save.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import random
import sys
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import numpy as np

import build_lineups as bl
from common import fetch_all, get_client, norm_name

ET = ZoneInfo("America/New_York")


def kickoff_et(g):
    t = (g.get("gametime") or "13:00")[:5]
    return dt.datetime.fromisoformat(f"{g['gameday']} {t}").replace(tzinfo=ET)


def lineup_key(ids, matrix, proj, byid, contest):
    """Ranking key: sim p90 for gpp, p50 for cash (projection sum without a matrix)."""
    if not matrix:
        return sum(proj(byid[i]) for i in ids)
    n = len(next(iter(matrix.values())))
    tot = np.zeros(n, dtype=np.float32)
    for i in ids:
        a = matrix.get(i)
        tot += a if a is not None else np.float32(proj(byid[i]))
    return float(np.percentile(tot, 50 if contest == "cash" else 90))


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
    # full swap
    ap.add_argument("--full", action="store_true", help="re-solve every lineup's open slots on the current board (after the quick swap)")
    ap.add_argument("--gain", type=float, default=1.5, help="full: change a lineup only if the best candidate beats it by this many p90/p50 points")
    ap.add_argument("--cands", type=int, default=6, help="full: candidates solved per lineup (1 deterministic + jittered)")
    ap.add_argument("--rand", type=float, default=0.15, help="full: projection jitter for the extra candidates (builder default)")
    ap.add_argument("--stack", type=int, default=1, help="full/gpp: QB + this many WR/TE from his team")
    ap.add_argument("--bringback", action="store_true", default=True, help="full/gpp: one opponent RB/WR/TE with the QB stack (default on)")
    ap.add_argument("--no-bringback", dest="bringback", action="store_false")
    ap.add_argument("--max-exp", type=float, default=0.35, help="full: max share of the final lineups any OPEN player may be in")
    ap.add_argument("--min-uniq", type=int, default=4, help="full: final lineups must differ from each other by this many players")
    ap.add_argument("--seed", type=int, default=7)
    args = ap.parse_args()
    random.seed(args.seed); np.random.seed(args.seed)

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

    # blended projection (same as the builder): props pull applied to the board + matrix, then overrides
    overrides = {find(s.split("=")[0])["site_player_id"]: float(s.split("=")[1]) for s in args.set}
    n_blend = bl.apply_props(rows, matrix, args.props, overrides)
    def proj(r):
        i = r["site_player_id"]
        if i in overrides:
            return overrides[i]
        return float(r["mean"] or 0)
    def p90(r):
        a = matrix.get(r["site_player_id"]) if matrix else None
        if a is None:
            return proj(r)
        f = proj(r) / float(r["mean"]) if r["mean"] and float(r["mean"]) > 0.5 and r["site_player_id"] in overrides else 1.0
        return float(np.percentile(a, 90)) * f

    # games and the clock
    now = dt.datetime.fromisoformat(args.now).replace(tzinfo=ET) if args.now else dt.datetime.now(ET)
    games = {g["game_id"]: g for g in fetch_all(client.table("games").select("game_id,gameday,gametime,home_score")
                                                .in_("game_id", list(slate.get("game_ids") or [])), order="game_id")}
    started = {gid for gid, g in games.items() if kickoff_et(g) <= now or (g.get("home_score") is not None and not args.now)}
    next_ko = min((kickoff_et(g) for g in games.values() if g["game_id"] not in started), default=None)
    def locked(r):
        return r.get("game_id") in started
    excludes = {find(n)["site_player_id"] for n in args.exclude}
    out_status = {r["site_player_id"] for r in rows if bl.eff_status(r) in bl.OUT_STATUSES}
    dead = excludes | out_status
    print(f"{slate['slate_key']} · {now:%a %I:%M %p} ET · {len(started)}/{len(games)} games started"
          + (f" · next kickoff {next_ko:%I:%M %p}" if next_ko else " · slate over") +
          f" · {len(excludes)} excluded by you, {len(out_status)} marked OUT on the board · props blend on {n_blend} players", file=sys.stderr)

    # recorded lineups
    q = client.table("slate_lineups").select("*").eq("slate_id", slate["slate_id"]).eq("source", args.source)
    if args.contest != "all":
        q = q.eq("contest", args.contest)
    saved = fetch_all(q, order=["contest", "idx"])
    if not saved:
        raise SystemExit("no recorded lineups for this slate (the Sunday build saves them with --save)")

    # ---------------------------------------------------------------- quick swap
    report, new_sets = [], []
    for L in saved:
        ids = list(L["player_ids"])
        ps = [byid[i] for i in ids if i in byid]
        if len(ps) != 9:
            report.append({"contest": L["contest"], "idx": L["idx"], "error": "lineup has players not on the board"}); new_sets.append((L, ids)); continue
        bad = [p for p in ps if p["site_player_id"] in dead]
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
            cands = [r for r in rows if r["position"] in allowed and r["site_player_id"] not in ids and r["site_player_id"] not in dead
                     and not locked(r) and r["salary"] <= cap_room
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
                           "proj_before": round(sum(proj(byid[i]) for i in L["player_ids"] if i in byid and byid[i]["site_player_id"] not in dead), 1),
                           "proj_after": round(sum(proj(byid[i]) for i in ids), 1)})

    n_changed = sum(1 for r in report if r.get("swaps") and any(s.get("in") for s in r["swaps"]))
    n_stuck = sum(1 for r in report for s in r.get("swaps", []) if s.get("in") is None)
    print(f"quick swap: {len(saved)} lineups · {n_changed} changed · {n_stuck} swaps impossible", file=sys.stderr)
    for r in report:
        for s in r.get("swaps", []):
            print(f"  #{r['idx']:>2} {r['contest']}: {s['out']} -> {s['in'] or '(none: ' + s.get('reason', '') + ')'}"
                  + (f"  proj {s['out_proj']} -> {s['in_proj']}, ${int(s['in_salary']):,}" if s.get("in") else ""), file=sys.stderr)
    quick_sets = [(L, list(ids)) for L, ids in new_sets]

    # ---------------------------------------------------------------- full swap
    full_report = []
    if args.full:
        open_pool = [r for r in rows if not locked(r) and r["site_player_id"] not in dead and r["mean"] is not None and proj(r) > 0]
        n_final = len(new_sets)
        cap_exp = max(1, int(np.ceil(args.max_exp * n_final)))
        usage, finals = {}, []
        # process the strongest lineups first so the exposure cap bites the weak ones
        order = sorted(range(len(new_sets)), key=lambda k: -lineup_key(new_sets[k][1], matrix, proj, byid, new_sets[k][0]["contest"]))
        results = [None] * len(new_sets)
        for k in order:
            L, ids = new_sets[k]
            contest = L["contest"]
            locks = [i for i in ids if i in byid and locked(byid[i])]
            if any(i in dead and not locked(byid[i]) for i in ids if i in byid) or len(locks) == 9:
                results[k] = ids; continue           # unfixable quick swap, or nothing open
            pool = [byid[i] for i in locks] + [r for r in open_pool if r["site_player_id"] not in locks]
            # uniqueness counts OPEN slots only (locked players are shared by force): differ by min(--min-uniq, half the open slots)
            n_open = 9 - len(locks)
            m_uniq = min(args.min_uniq, max(1, n_open // 2))
            opts = SimpleNamespace(min_salary=0, max_team=args.max_team, contest=contest, stack=args.stack, stack_rb=False,
                                   bringback=args.bringback, max_own=0, min_uniq=9 - n_open + m_uniq)
            prior_open = lambda: [[i for i in f if i not in locks] for f in finals]
            blocked = {i for i, u in usage.items() if u >= cap_exp}
            base_key = lineup_key(ids, matrix, proj, byid, contest)
            cands = []
            for c in range(max(1, args.cands)):
                score = {}
                for p in pool:
                    m = proj(p)
                    base = 0.8 * m + 0.2 * (p["floor"] or 0) if contest == "cash" else 0.6 * m + 0.4 * (p["p85"] or m)
                    jit = 1 + (args.rand * random.gauss(0, 1) * ((p["stdev"] or 5) / max(m, 1)) if c > 0 and contest == "gpp" else 0)
                    score[p["site_player_id"]] = base * jit
                sol = bl.solve(pool, score, site, opts, prior_open() + [[i for i in x if i not in locks] for x in cands], blocked, set(locks))
                if not sol:
                    break
                cands.append(sol)
            best, best_key = ids, base_key
            for sol in cands:
                kk = lineup_key(sol, matrix, proj, byid, contest)
                if kk > best_key:
                    best, best_key = sol, kk
            # keep the original unless the gain is real; the original also has to respect the exposure cap / uniqueness
            orig_ok = (all(usage.get(i, 0) < cap_exp or i in locks for i in ids)
                       and all(sum(1 for i in ids if i not in locks and i in f) <= n_open - m_uniq for f in finals))
            if best is not ids and (best_key - base_key >= args.gain or not orig_ok):
                chosen = best
            else:
                chosen = ids                                   # gain too small (or nothing feasible beat it): stand pat
            if chosen is not ids:
                full_report.append({"contest": contest, "idx": L["idx"],
                                    "out": [byid[i]["player_name"] for i in ids if i not in chosen],
                                    "in": [byid[i]["player_name"] for i in chosen if i not in ids],
                                    "key_before": round(base_key, 1), "key_after": round(best_key, 1),
                                    "proj_before": round(sum(proj(byid[i]) for i in ids), 1), "proj_after": round(sum(proj(byid[i]) for i in chosen), 1)})
            results[k] = chosen
            finals.append(chosen)
            for i in chosen:
                if i not in locks:
                    usage[i] = usage.get(i, 0) + 1
        new_sets = [(L, results[k]) for k, (L, _) in enumerate(new_sets)]
        full_report.sort(key=lambda r: (r["contest"], r["idx"]))
        key_name = "p90 (gpp) / p50 (cash)"
        print(f"full swap: {len(full_report)}/{n_final} lineups rebuilt (gain >= {args.gain:g} {key_name}; {len(open_pool)} open players; "
              f"exposure cap {cap_exp}/{n_final}; min-uniq {args.min_uniq})", file=sys.stderr)
        for r in full_report:
            print(f"  #{r['idx']:>2} {r['contest']}: -{', '.join(r['out'])}  +{', '.join(r['in'])}  key {r['key_before']} -> {r['key_after']}", file=sys.stderr)
        # exposure delta vs the quick-swapped set
        before, after = {}, {}
        for (_, a), (_, b) in zip(quick_sets, new_sets):
            for i in a: before[i] = before.get(i, 0) + 1
            for i in b: after[i] = after.get(i, 0) + 1
        delta = sorted(((after.get(i, 0) - before.get(i, 0), i) for i in set(before) | set(after)), key=lambda t: -abs(t[0]))
        moves = [(d, i) for d, i in delta if d != 0]
        if moves:
            print("exposure delta (lineups): " + ", ".join(f"{byid[i]['player_name']} {d:+d}" for d, i in moves[:14]), file=sys.stderr)

    n_total_changed = sum(1 for L, ids in new_sets if set(ids) != set(L["player_ids"]))
    print(f"{n_total_changed}/{len(new_sets)} lineups differ from the recorded set", file=sys.stderr)

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
                   "full": bool(args.full), "changed": n_total_changed,
                   "lineups": [{"contest": L["contest"], "idx": L["idx"], "ids": ids, "original_ids": list(L["player_ids"]),
                                "players": [byid[i]["player_name"] for i in ids], "proj": round(sum(proj(byid[i]) for i in ids), 1)} for L, ids in new_sets],
                   "swaps": report, "full_swaps": full_report}, open(args.json, "w"), indent=1)
    if args.save and n_total_changed:
        payload = [{"ids": ids, "proj": round(sum(proj(byid[i]) for i in ids), 1), "own": L.get("own"), "p10": L.get("p10"), "p50": L.get("p50"), "p90": L.get("p90"),
                    "note": f"{L.get('note') or ''} | {args.note}"[:500]} for L, ids in new_sets]
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
