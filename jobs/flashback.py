"""
Contest Flashback: re-run the slate against the REAL field and report expected ROI, not just Sunday's result.

  python jobs/flashback.py --slate-key DK-2026-02-main --standings data/ownership/DK-2026-02-main_standings.csv \
      [--contest gpp] [--fee 20] [--payout milly] [--field 20000] [--bench 1000] [--json out.json] [--save]

Why: a GPP is top-heavy, so one Sunday's profit or loss says almost nothing about whether the lineups were good.
The honest scoreboard is: given what the opponents ACTUALLY played (the contest standings export lists every
entry's lineup), how much would our lineups have won on average across the thousands of ways the games could
have gone? That is what SaberSim calls Contest Flashback; this is ours.

What it does
  1. Parses the DraftKings standings export: every entry's lineup (the "Lineup" column: "QB Name RB Name ...")
     and the player block (%Drafted, FPTS). Lineups are matched to slate players by name (+ position).
  2. Loads the slate's stored sim matrix (the last pre-kickoff sim) with the same props blend the build used,
     so every lineup — ours and theirs — is scored in the same simulated worlds.
  3. Samples --field opponent lineups from the real entries and, in every sim, ranks each of our recorded
     lineups against them. Rank -> prize through the contest's payout curve (contest_sim.payout_fn, scaled to
     the real entry count and fee, 15% rake). Exact duplicates of our lineup in the real field split the prize.
  4. Reports per lineup: flashback ROI (expected profit / fee), cash %, top-1 %, mean field percentile — and
     the realized result: actual points, real rank, real prize.
  5. Benchmarks: the same expected ROI for --bench random real entries. The field's own mean is about -rake; where
     our portfolio sits in that distribution ("beats X% of real entries in expectation") is the edge number.
  6. --save writes fb_* columns on slate_lineups and slates.flashback (summary) for the results page.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import re
import sys
from collections import Counter

import numpy as np

import build_lineups as bl
from common import fetch_all, get_client, norm_name
from contest_sim import lineup_totals, payout_fn

POS_RE = re.compile(r"\b(QB|RB|WR|TE|FLEX|DST)\s+")


def parse_standings(path):
    """entries = [(rank, entry_name, points, [(pos, name), ...])], players = {norm_name: {"own": %, "fpts": pts}}."""
    entries, players = [], {}
    with open(path, encoding="utf-8-sig", newline="") as fh:
        rd = csv.reader(fh)
        hdr = [h.strip() for h in next(rd)]
        col = {h.lower(): i for i, h in enumerate(hdr)}
        li, pi_, ni, ri = col.get("lineup"), col.get("points"), col.get("entryname"), col.get("rank")
        pl, po, pf = col.get("player"), col.get("%drafted"), col.get("fpts")
        for r in rd:
            if li is not None and len(r) > li and r[li].strip():
                toks = POS_RE.split(r[li].strip())
                # toks = ['', 'DST', 'Patriots', 'FLEX', 'George Kittle', ...]
                slots = [(toks[k], toks[k + 1].strip()) for k in range(1, len(toks) - 1, 2) if toks[k + 1].strip()]
                try:
                    pts = float(r[pi_] or 0)
                except (ValueError, TypeError):
                    pts = 0.0
                try:
                    rank = int(r[ri])
                except (ValueError, TypeError):
                    rank = len(entries) + 1
                entries.append((rank, r[ni].strip() if ni is not None else "", pts, slots))
            if pl is not None and len(r) > pl and r[pl].strip():
                n = norm_name(r[pl].strip())
                d = players.setdefault(n, {"own": 0.0, "fpts": None})
                try:
                    d["own"] += float((r[po] or "0").rstrip("%") or 0)
                except (ValueError, TypeError, IndexError):
                    pass
                try:
                    d["fpts"] = float(r[pf])
                except (ValueError, TypeError, IndexError):
                    pass
    return entries, players


def match_lineups(entries, rows):
    """Map every entry's 9 (pos, name) slots to site_player_ids. Returns (list of id-tuples aligned with entries or None)."""
    byname = {}
    for r in rows:
        byname.setdefault(norm_name(r["player_name"]), []).append(r)
    cache = {}
    def find(pos, name):
        key = (pos, name)
        if key in cache:
            return cache[key]
        c = byname.get(norm_name(name), [])
        if len(c) > 1 and pos not in ("FLEX", ""):
            c = [r for r in c if r["position"] == pos] or c
        if len(c) > 1 and pos == "FLEX":
            c = [r for r in c if r["position"] in ("RB", "WR", "TE")] or c
        cache[key] = c[0]["site_player_id"] if c else None
        return cache[key]
    out, bad = [], Counter()
    for _, _, _, slots in entries:
        ids = [find(p, n) for p, n in slots]
        if len(ids) != 9 or any(i is None for i in ids) or len(set(ids)) != 9:
            out.append(None)
            for (p, n), i in zip(slots, ids):
                if i is None:
                    bad[n] += 1
            continue
        out.append(tuple(ids))
    return out, bad


def score_vs_field(C, Fs, entries, pay, dups):
    """C (nc, S) candidate totals; Fs (nf, S) field totals sorted ascending per sim. -> per-candidate stats."""
    nf, S = Fs.shape
    below = np.empty(C.shape, dtype=np.int32)
    for s in range(S):
        below[:, s] = np.searchsorted(Fs[:, s], C[:, s], side="left")
    res = []
    bucket = entries / nf
    bucket_mean = pay(np.arange(1, int(np.ceil(bucket)) + 1)).mean()
    for k in range(C.shape[0]):
        beaten = below[k] / nf
        rank = np.maximum(1.0, (1.0 - beaten) * entries)
        prize = pay(rank)
        top = beaten >= 1.0 - 0.5 / nf
        prize[top] = bucket_mean
        prize = prize / dups[k]
        res.append({"ev": float(prize.mean()), "p_cash": float((prize > 0).mean()), "p_top1": float((beaten >= 0.99).mean()),
                    "p_top01": float((beaten >= 0.999).mean()), "field_pct": float(beaten.mean())})
    return res


def consensus_matrix(rows, matrix):
    """Same sim draws, but every player's mean moved to the MARKET line: his props projection where one exists, else
    the salary-implied line for his position (fit on the board). Removes our model's opinion so the flashback measures
    lineup construction (stacks, uniqueness, leverage vs the real field) alone."""
    out = {}
    fit = {}
    for pos in ("QB", "RB", "WR", "TE", "DST"):
        ps = [r for r in rows if r["position"] == pos and (r["mean"] or 0) >= 3 and bl.eff_status(r) not in bl.OUT_STATUSES]
        if len(ps) >= 8:
            fit[pos] = np.polyfit([r["salary"] for r in ps], [r["mean"] for r in ps], 1)
    for r in rows:
        i = r["site_player_id"]
        a = matrix.get(i)
        if a is None:
            continue
        m = float(r["mean"] or 0)
        if bl.eff_status(r) in bl.OUT_STATUSES or m <= 0.5:
            out[i] = a
            continue
        pv = r.get("props")
        if pv is None:
            target = max(0.0, float(np.polyval(fit[r["position"]], r["salary"]))) if r["position"] in fit else m
        else:
            target = float(pv)
        out[i] = a * np.float32(min(max(target / m, 0.25), 4.0))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--slate-key", required=True)
    ap.add_argument("--standings", required=True, help="DK contest standings export (with the Lineup column)")
    ap.add_argument("--contest", default="gpp", choices=["gpp", "cash"])
    ap.add_argument("--source", default="claude")
    ap.add_argument("--fee", type=float, default=20.0)
    ap.add_argument("--payout", default="milly", choices=["milly", "gpp", "se"])
    ap.add_argument("--entries", type=int, default=None, help="contest size (default: entries in the file)")
    ap.add_argument("--field", type=int, default=20000, help="real entries sampled as the field")
    ap.add_argument("--bench", type=int, default=1000, help="real entries scored as candidates for the benchmark distribution")
    ap.add_argument("--props", type=float, default=0.85, help="props blend used at build time (rescales the stored sim)")
    ap.add_argument("--me", default=None, help="your DK username: report your real entries' finishes too")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--json")
    ap.add_argument("--save", action="store_true")
    args = ap.parse_args()
    rng = np.random.default_rng(args.seed)

    client = get_client(need_write=args.save)
    slate, rows, ext, own_model, matrix = bl.load_board(client, args.slate_key)
    if matrix is None:
        raise SystemExit("no stored sim for this slate")
    byid = {r["site_player_id"]: r for r in rows}
    raw_rows = [dict(r) for r in rows]                       # board before the props blend (for the consensus view)
    bl.apply_props(rows, matrix, args.props, ())
    proj = lambda i: float(byid[i]["mean"] or 0) if i in byid else 0.0
    n_sims = len(next(iter(matrix.values())))

    entries, players = parse_standings(args.standings)
    if not entries:
        raise SystemExit("no entries with a Lineup column in the standings file")
    ids_all, bad = match_lineups(entries, rows)
    ok = [k for k, x in enumerate(ids_all) if x is not None]
    n_entries = args.entries or len(entries)
    print(f"{slate['slate_key']} · {len(entries):,} entries in file ({len(ok):,} parsed, {len(entries) - len(ok)} unmatched"
          + (f"; worst: {', '.join(f'{n} x{c}' for n, c in bad.most_common(3))}" if bad else "") + f") · {n_sims} sims", file=sys.stderr)
    fpts = {r["site_player_id"]: (players.get(norm_name(r["player_name"])) or {}).get("fpts") for r in rows}
    field_scores = np.array(sorted((e[2] for e in entries), reverse=True))
    pay = payout_fn(args.payout, n_entries, args.fee)

    # our recorded lineups
    ours = fetch_all(client.table("slate_lineups").select("*").eq("slate_id", slate["slate_id"]).eq("source", args.source)
                     .eq("contest", args.contest), order="idx")
    if not ours:
        raise SystemExit(f"no recorded {args.contest} lineups (source {args.source}) for this slate")
    our_ids = [tuple(L["player_ids"]) for L in ours]

    # field sample, benchmark sample, exact-duplicate counts in the real field
    pick = rng.choice(ok, size=min(args.field, len(ok)), replace=False)
    field_ids = [ids_all[k] for k in pick]
    bpick = rng.choice(ok, size=min(args.bench, len(ok)), replace=False)
    bench_ids = [ids_all[k] for k in bpick]
    real_sets = Counter(frozenset(ids_all[k]) for k in ok)
    our_dups = np.array([1.0 + real_sets.get(frozenset(ids), 0) for ids in our_ids])
    bench_dups = np.array([float(max(real_sets.get(frozenset(ids), 1), 1)) for ids in bench_ids])   # a real entry counts itself

    def view(mtx):
        Fs = np.sort(lineup_totals(field_ids, mtx, proj, n_sims), axis=0)
        st = score_vs_field(lineup_totals(our_ids, mtx, proj, n_sims), Fs, n_entries, pay, our_dups)
        bs = score_vs_field(lineup_totals(bench_ids, mtx, proj, n_sims), Fs, n_entries, pay, bench_dups)
        bench_roi = np.array([s["ev"] / args.fee - 1 for s in bs])
        port = float(np.mean([s["ev"] for s in st])) / args.fee - 1
        return st, {"roi": port, "cash": float(np.mean([s["p_cash"] for s in st])), "top1": float(np.mean([s["p_top1"] for s in st])),
                    "field_pct": float(np.mean([s["field_pct"] for s in st])), "best_roi": max(s["ev"] for s in st) / args.fee - 1,
                    "bench_mean": float(bench_roi.mean()), "bench_median": float(np.median(bench_roi)), "bench_p90": float(np.percentile(bench_roi, 90)),
                    "beats_pct": float((bench_roi < port).mean())}

    st_model, model = view(matrix)
    st_cons, cons = view(consensus_matrix(raw_rows, matrix))

    # realized
    per = []
    for L, ids, sm, sc, d in zip(ours, our_ids, st_model, st_cons, our_dups):
        actual = round(float(sum(fpts.get(i) or 0.0 for i in ids)), 2)
        rank = int(np.searchsorted(-field_scores, -actual, side="left")) + 1
        prize = float(pay([rank])[0]) / d
        per.append({"idx": L["idx"], "proj": float(L["proj"] or 0),
                    "fb_roi": sm["ev"] / args.fee - 1, "fb_cash": sm["p_cash"], "fb_top1": sm["p_top1"], "fb_field_pct": sm["field_pct"],
                    "fb_roi_cons": sc["ev"] / args.fee - 1, "fb_cash_cons": sc["p_cash"], "fb_top1_cons": sc["p_top1"], "fb_field_pct_cons": sc["field_pct"],
                    "dups": int(d - 1), "actual": actual, "rank": rank, "prize": prize, "real_roi": prize / args.fee - 1,
                    "players": [byid[i]["player_name"] for i in ids]})
    real_port = float(np.mean([p["real_roi"] for p in per]))
    summary = {
        "contest": args.contest, "source": args.source, "n": len(per), "entries": n_entries, "fee": args.fee, "payout": args.payout,
        "field_sampled": len(field_ids), "bench": len(bench_ids), "sims": n_sims, "props": args.props,
        "model": model, "consensus": cons,
        "real_roi": real_port, "real_best_rank": min(p["rank"] for p in per), "real_cash": float(np.mean([p["prize"] > 0 for p in per])),
        "real_points": float(np.mean([p["actual"] for p in per])), "field_median_points": float(np.median(field_scores)),
        "at": dt.datetime.now(dt.timezone.utc).isoformat(),
    }
    if args.me:
        mine = [(e[0], e[2]) for e in entries if e[1].lower().startswith(args.me.lower())]
        if mine:
            summary["me"] = {"entries": len(mine), "best_rank": min(r for r, _ in mine), "mean_points": float(np.mean([p for _, p in mine])),
                             "roi": float(np.mean([pay([r])[0] for r, _ in mine]) / args.fee - 1)}

    print(f"flashback {args.contest} x{len(per)} vs {n_entries:,} real entries ({args.payout} curve, ${args.fee:g})", file=sys.stderr)
    print(f"  model view      ROI {model['roi']:+.0%}  cash {model['cash']:.0%}  top-1% {model['top1']:.1%}  | field mean {model['bench_mean']:+.0%}, "
          f"p90 {model['bench_p90']:+.0%}  -> beats {model['beats_pct']:.0%} of real entries   (our projections are right: lineups + edge)", file=sys.stderr)
    print(f"  consensus view  ROI {cons['roi']:+.0%}  cash {cons['cash']:.0%}  top-1% {cons['top1']:.1%}  | field mean {cons['bench_mean']:+.0%}, "
          f"p90 {cons['bench_p90']:+.0%}  -> beats {cons['beats_pct']:.0%} of real entries   (market projections: construction only)", file=sys.stderr)
    print(f"  realized        ROI {real_port:+.0%}  cash {summary['real_cash']:.0%}  best rank {summary['real_best_rank']:,}  "
          f"avg {summary['real_points']:.1f} pts vs field median {summary['field_median_points']:.1f}", file=sys.stderr)
    if "me" in summary:
        m = summary["me"]; print(f"  your real entries ({m['entries']}): best rank {m['best_rank']:,}, ROI {m['roi']:+.0%}", file=sys.stderr)
    print(f"{'#':>3} {'proj':>6} {'model':>7} {'cons':>7} {'cash':>5} {'top1%':>6} {'dup':>3} | {'actual':>7} {'rank':>7} {'prize':>7}", file=sys.stderr)
    for p in sorted(per, key=lambda p: -p["fb_roi_cons"]):
        print(f"{p['idx']:>3} {p['proj']:6.1f} {p['fb_roi']:+7.0%} {p['fb_roi_cons']:+7.0%} {p['fb_cash_cons']:5.0%} {p['fb_top1_cons']:6.2%} {p['dups']:>3} | "
              f"{p['actual']:7.2f} {p['rank']:7,} {p['prize']:7.0f}", file=sys.stderr)

    if args.json:
        json.dump({"slate": slate["slate_key"], "summary": summary, "lineups": per}, open(args.json, "w"), indent=1)
    if args.save:
        for p in per:
            client.table("slate_lineups").update({
                "fb_roi": round(p["fb_roi"], 4), "fb_cash": round(p["fb_cash"], 4), "fb_top1": round(p["fb_top1"], 5),
                "fb_roi_cons": round(p["fb_roi_cons"], 4), "fb_cash_cons": round(p["fb_cash_cons"], 4), "fb_top1_cons": round(p["fb_top1_cons"], 5),
                "real_rank": p["rank"], "real_prize": round(p["prize"], 2), "fb_at": summary["at"]}) \
                .eq("slate_id", slate["slate_id"]).eq("source", args.source).eq("contest", args.contest).eq("idx", p["idx"]).execute()
        rnd = lambda d: {k: (round(v, 4) if isinstance(v, float) else (rnd(v) if isinstance(v, dict) else v)) for k, v in d.items()}
        fb = dict(slate.get("flashback") or {})
        fb[f"{args.source}/{args.contest}"] = rnd(summary)
        client.table("slates").update({"flashback": fb}).eq("slate_id", slate["slate_id"]).execute()
        print(f"saved flashback for {len(per)} lineups", file=sys.stderr)


if __name__ == "__main__":
    main()
