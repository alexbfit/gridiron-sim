"""
Season ledger + weekly recap: the public, auditable track record behind the Track Record page.

  python jobs/recap.py                       # every scored slate of the latest season -> web/data/ledger.json + recap_<season>_wk<NN>.md
  python jobs/recap.py --season 2026         # one season
  python jobs/recap.py --week 3              # recap only week 3 (the ledger is still rebuilt for the whole season)
  python jobs/recap.py --print               # also print the recap(s) to stdout
  python jobs/recap.py --out-dir /tmp/x      # write somewhere else (tests)

Reads with the anon key (nothing here needs a write): slates.results_meta (jobs/results.py), slates.flashback
(jobs/flashback.py, incl. the `dupes` block and `me` = Alex's real entries), slate_lineups (the 50 GPP lineups and
their realized finishes), slate_board + slate_results (for "what won" and the biggest projection misses) and
news_notes (graded by results.py).

Every number in the ledger comes from the database. Weeks without a Flashback, without a standings file or without
real-money rows simply carry nulls for those columns; the page and the recap print "–" for them.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from collections import Counter
from pathlib import Path

from common import fetch_all, get_client

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "web" / "data"
SOURCE, CONTEST = "claude", "gpp"           # the public portfolio: Claude's Sunday GPP lineups


# ------------------------------------------------------------------ small helpers
def r4(v):
    return None if v is None else round(float(v), 4)


def mean(xs):
    xs = [x for x in xs if x is not None]
    return sum(xs) / len(xs) if xs else None


def pct(v, d=0):
    return "–" if v is None else f"{v * 100:.{d}f}%"


def roi(v):
    return "–" if v is None else f"{'+' if v >= 0 else '−'}{abs(v) * 100:.0f}%"


def f1(v):
    return "–" if v is None else f"{v:.1f}"


def money(v):
    return "–" if v is None else f"${v:,.2f}"


def fnum(v):
    return "–" if v is None else f"{int(v):,}"


# ------------------------------------------------------------------ per-slate pieces
def lineup_block(slate, lineups):
    """Our GPP portfolio for the week: counts, average points, realized finishes. results_meta.lineups when
    present, else straight from the slate_lineups rows (a slate scored before results_meta carried lineups)."""
    g = [L for L in lineups if L["source"] == SOURCE and L["contest"] == CONTEST]
    fb = (slate.get("flashback") or {}).get(f"{SOURCE}/{CONTEST}") or {}
    rm = ((slate.get("results_meta") or {}).get("lineups") or {}).get(SOURCE, {}).get(CONTEST) or {}
    entries = fb.get("entries") or (slate.get("contest_meta") or {}).get("entries")
    acts = [float(L["actual"]) for L in g if L.get("actual") is not None]
    cps = [float(L["contest_pct"]) for L in g if L.get("contest_pct") is not None]
    ranks = [int(L["real_rank"]) for L in g if L.get("real_rank") is not None]
    cons = [float(L["fb_roi_cons"]) for L in g if L.get("fb_roi_cons") is not None]
    top1 = None
    if ranks and entries:
        top1 = sum(1 for r in ranks if r <= max(1, entries * 0.01))
    elif cps:
        top1 = sum(1 for c in cps if c >= 0.99)
    return {
        "n": rm.get("n") or len(g) or None,
        "avg_pts": rm.get("actual") if rm.get("actual") is not None else r4(mean(acts)),
        "best_pts": rm.get("best") if rm.get("best") is not None else (r4(max(acts)) if acts else None),
        "avg_proj": rm.get("proj") if rm.get("proj") is not None else r4(mean([float(L["proj"]) for L in g if L.get("proj") is not None])),
        "beat_pct": rm.get("contest_pct") if rm.get("contest_pct") is not None else r4(mean(cps)),
        "best_beat_pct": rm.get("best_contest_pct") if rm.get("best_contest_pct") is not None else (r4(max(cps)) if cps else None),
        "sim_pct": rm.get("sim_pct") if rm.get("sim_pct") is not None else r4(mean([float(L["sim_pct"]) for L in g if L.get("sim_pct") is not None])),
        "best_rank": fb.get("real_best_rank") if fb.get("real_best_rank") is not None else (min(ranks) if ranks else None),
        "entries": entries,
        "field_median_pts": fb.get("field_median_points") if fb.get("field_median_points") is not None else (slate.get("contest_meta") or {}).get("median"),
        "top1_lineups": top1,
        "cash_pct": fb.get("real_cash") if fb.get("real_cash") is not None else (r4(mean([float(L["real_prize"] or 0) > 0 for L in g])) if any(L.get("real_prize") is not None for L in g) else None),
        "real_roi": fb.get("real_roi"),
        "_lineup_cons": r4(mean(cons)),
    }


def flashback_block(slate, lu):
    fb = (slate.get("flashback") or {}).get(f"{SOURCE}/{CONTEST}") or {}
    c, m = fb.get("consensus") or {}, fb.get("model") or {}
    lineup_cons = lu.pop("_lineup_cons", None)            # mean of the saved per-lineup fb_roi_cons = portfolio ROI
    out = {
        "present": bool(fb),
        "consensus_roi": c.get("roi", lineup_cons),
        "field_mean_roi": c.get("bench_mean"), "field_median_roi": c.get("bench_median"), "field_p90_roi": c.get("bench_p90"),
        "beats_pct": c.get("beats_pct"), "consensus_cash": c.get("cash"), "consensus_top1": c.get("top1"),
        "model_roi": m.get("roi"), "model_beats_pct": m.get("beats_pct"), "model_top1": m.get("top1"),
        "fee": fb.get("fee"), "payout": fb.get("payout"), "sims": fb.get("sims"),
    }
    return out


def dupes_block(slate):
    fb = (slate.get("flashback") or {}).get(f"{SOURCE}/{CONTEST}") or {}
    d = fb.get("dupes")
    if not d:
        return None
    return {"lineups_with_copy": d.get("lineups_with_copy"), "copies_total": d.get("copies_total"), "max_copies": d.get("max_copies"),
            "top20_with_copy": d.get("top20_with_copy"), "winnings_lost_pct": d.get("winnings_lost_pct"), "field_with_copy": d.get("bench_with_copy")}


def real_money_block(slate):
    """Alex's own entries, as the Flashback matched them in the standings export (username prefix). The dollars are
    the contest's payout curve applied to the real ranks at the recorded fee — not a DraftKings account statement."""
    fb = (slate.get("flashback") or {}).get(f"{SOURCE}/{CONTEST}") or {}
    me, fee = fb.get("me"), fb.get("fee")
    if not me or fee is None or me.get("roi") is None or not me.get("entries"):
        return None
    stake = float(me["entries"]) * float(fee)
    return {"entries": me["entries"], "fee": fee, "best_rank": me.get("best_rank"), "mean_pts": me.get("mean_points"),
            "roi": me["roi"], "in": r4(stake), "out": r4(stake * (1 + float(me["roi"]))), "basis": "payout curve on real ranks"}


def accuracy_block(slate):
    rm = slate.get("results_meta") or {}
    bm = rm.get("by_method") or {}
    return {"n": rm.get("n"), "r": rm.get("r"), "mae": rm.get("mae"), "bias": rm.get("bias"), "coverage": rm.get("coverage"),
            "by_method": {k: {"r": v.get("r"), "mae": v.get("mae"), "bias": v.get("bias"), "n": v.get("n")} for k, v in bm.items()},
            "actual_source": rm.get("actual_source")}


def news_block(notes):
    graded = [n for n in notes if n.get("helped") is not None]
    if not graded:
        return None
    hi = [n for n in graded if n.get("confidence") is not None and float(n["confidence"]) >= 0.7]
    return {"graded": len(graded), "helped": sum(1 for n in graded if n["helped"]), "helped_pct": r4(mean([bool(n["helped"]) for n in graded])),
            "hi_conf_graded": len(hi), "hi_conf_helped_pct": r4(mean([bool(n["helped"]) for n in hi])) if hi else None,
            "notes_filed": len(notes)}


def what_won(lineups, board, results, top_n=10):
    """The game / QB stack behind the week's best lineups: the QB game that appears most among our top-N GPP
    lineups by actual points. None when there is nothing to derive it from."""
    g = [L for L in lineups if L["source"] == SOURCE and L["contest"] == CONTEST and L.get("actual") is not None]
    if not g or not board:
        return None
    b = {r["site_player_id"]: r for r in board}
    act = {r["site_player_id"]: r.get("actual") for r in results}
    g.sort(key=lambda L: -float(L["actual"]))
    top = g[:top_n]
    games, qbs = Counter(), Counter()
    for L in top:
        qb = next((b[i] for i in (L.get("player_ids") or []) if i in b and b[i]["position"] == "QB"), None)
        if qb:
            games[qb.get("game_id") or f"{qb['team']}"] += 1
            qbs[qb["site_player_id"]] += 1
    if not games:
        return None
    game, n_game = games.most_common(1)[0]
    qid, n_qb = qbs.most_common(1)[0]
    qb = b[qid]
    mates = Counter()
    for L in top:
        ids = L.get("player_ids") or []
        if qid in ids:
            for i in ids:
                if i in b and i != qid and b[i].get("team") == qb["team"] and b[i]["position"] in ("WR", "TE", "RB"):
                    mates[i] += 1
    mate = b[mates.most_common(1)[0][0]]["player_name"] if mates else None
    matchup = f"{qb.get('team')} vs {qb.get('opponent')}" if qb.get("opponent") else qb.get("team")
    line = (f"{qb['player_name']} ({qb['team']}) stacks" + (f" with {mate}" if mate else "")
            + f" — {n_qb} of our top {len(top)} lineups" + (f"; {qb['player_name']} scored {float(act[qid]):.1f}" if act.get(qid) is not None else ""))
    return {"game_id": game, "matchup": matchup, "qb": qb["player_name"], "qb_team": qb["team"], "qb_actual": act.get(qid),
            "stack_partner": mate, "top_lineups": len(top), "lineups_in_game": n_game, "lineups_with_qb": n_qb, "line": line}


def misses(results, k=3):
    """Biggest over-projections (relevant players who played) and biggest under-projections — the honest 'what missed'."""
    rel = [r for r in results if r.get("played") and r.get("proj_mean") is not None and float(r["proj_mean"]) >= 10 and r.get("actual") is not None]
    if not rel:
        return {"over": [], "under": []}
    for r in rel:
        r["_d"] = float(r["actual"]) - float(r["proj_mean"])
    rel.sort(key=lambda r: r["_d"])
    fmt = lambda r: {"player": r["player_name"], "pos": r["position"], "team": r.get("team"), "proj": r4(r["proj_mean"]), "actual": r4(r["actual"]), "diff": r4(r["_d"])}
    return {"over": [fmt(r) for r in rel[:k]], "under": [fmt(r) for r in reversed(rel[-k:])]}


# ------------------------------------------------------------------ build
def week_row(client, slate):
    sid = slate["slate_id"]
    lineups = fetch_all(client.table("slate_lineups").select("*").eq("slate_id", sid), order=["source", "contest", "idx"])
    try:
        notes = fetch_all(client.table("news_notes").select("*").eq("slate_id", sid), order="id")
    except Exception:
        notes = []
    try:
        board = fetch_all(client.table("slate_board").select("site_player_id,player_name,position,team,opponent,game_id").eq("slate_id", sid), order="site_player_id")
    except Exception:
        board = []
    try:
        results = fetch_all(client.table("slate_results").select("site_player_id,player_name,position,team,proj_mean,actual,played").eq("slate_id", sid), order="site_player_id")
    except Exception:
        results = []
    lu = lineup_block(slate, lineups)
    fb = flashback_block(slate, lu)
    row = {
        "season": slate["season"], "week": slate["week"], "slate_key": slate["slate_key"], "site": slate.get("site"),
        "scored_at": (slate.get("results_meta") or {}).get("scored_at"),
        "lineups": lu, "flashback": fb, "dupes": dupes_block(slate), "real_money": real_money_block(slate),
        "accuracy": accuracy_block(slate), "news": news_block(notes),
        "what_won": what_won(lineups, board, results), "misses": misses(results),
    }
    lu["avg_vs_field_median"] = r4(lu["avg_pts"] - lu["field_median_pts"]) if lu["avg_pts"] is not None and lu["field_median_pts"] is not None else None
    row["recap"] = f"recap_{slate['season']}_wk{int(slate['week']):02d}.md"
    return row


def season_totals(rows):
    cons = [r["flashback"]["consensus_roi"] for r in rows]
    fmean = [r["flashback"]["field_mean_roi"] for r in rows]
    above = [r for r in rows if r["lineups"]["avg_vs_field_median"] is not None]
    cons_above = [r for r in rows if r["flashback"]["consensus_roi"] is not None and r["flashback"]["field_median_roi"] is not None]
    best = [(r["lineups"]["best_rank"], r["lineups"]["entries"], r["week"]) for r in rows if r["lineups"]["best_rank"] is not None]
    best.sort(key=lambda t: (t[0] / t[1]) if t[1] else t[0])
    rm = [r["real_money"] for r in rows if r["real_money"]]
    top1 = [r["lineups"]["top1_lineups"] for r in rows if r["lineups"]["top1_lineups"] is not None]
    beat = [r["lineups"]["beat_pct"] for r in rows]
    fbw = [r for r in rows if r["flashback"]["present"]]
    return {
        "weeks": len(rows), "weeks_with_flashback": len(fbw), "lineups": sum(r["lineups"]["n"] or 0 for r in rows),
        "consensus_roi": r4(mean(cons)), "field_mean_roi": r4(mean(fmean)),
        "consensus_roi_vs_field": r4(mean(cons) - mean(fmean)) if mean(cons) is not None and mean(fmean) is not None else None,
        "weeks_above_field_median": sum(1 for r in above if r["lineups"]["avg_vs_field_median"] > 0), "weeks_with_field_median": len(above),
        "weeks_consensus_above_field_median": sum(1 for r in cons_above if r["flashback"]["consensus_roi"] > r["flashback"]["field_median_roi"]),
        "weeks_consensus_scored": len(cons_above),
        "beats_pct": r4(mean([r["flashback"]["beats_pct"] for r in rows])), "avg_beat_pct": r4(mean(beat)),
        "top1_lineups": sum(top1) if top1 else None,
        "best_finish": {"rank": best[0][0], "entries": best[0][1], "week": best[0][2]} if best else None,
        "real_money": {"in": r4(sum(m["in"] for m in rm)), "out": r4(sum(m["out"] for m in rm)), "weeks": len(rm),
                       "roi": r4(sum(m["out"] for m in rm) / sum(m["in"] for m in rm) - 1) if sum(m["in"] for m in rm) else None,
                       "basis": "payout curve on real ranks"} if rm else None,
    }


# ------------------------------------------------------------------ recap text
def honest_read(row, totals):
    lu, fb, acc = row["lineups"], row["flashback"], row["accuracy"]
    lines = []
    if fb["consensus_roi"] is not None and fb["field_mean_roi"] is not None:
        gap = fb["consensus_roi"] - fb["field_mean_roi"]
        if fb["beats_pct"] is not None:
            lines.append(f"On market projections the construction alone was expected to return {roi(fb['consensus_roi'])} against a field averaging "
                         f"{roi(fb['field_mean_roi'])} — {'ahead of' if gap > 0 else 'behind'} the typical entry, better than {pct(fb['beats_pct'])} of real entries in expectation. "
                         f"That is {'a good' if gap > 0.1 else 'a weak' if gap < 0 else 'an ordinary'} construction week, not proof of anything.")
    elif lu["avg_vs_field_median"] is not None:
        lines.append("No Contest Flashback for this week, so only realized points are available — one Sunday's result, nothing about expected value.")
    if lu["avg_vs_field_median"] is not None:
        lines.append(f"Realized: our average lineup scored {f1(lu['avg_pts'])} against a field median of {f1(lu['field_median_pts'])} "
                     f"({'+' if lu['avg_vs_field_median'] >= 0 else ''}{lu['avg_vs_field_median']:.1f}); "
                     f"{'that beats the median entry' if lu['avg_vs_field_median'] > 0 else 'the median entry did better'}"
                     + (f", and the best lineup finished {fnum(lu['best_rank'])} of {fnum(lu['entries'])}" if lu["best_rank"] else "") + ". Variance is huge in a GPP; one week does not separate skill from luck.")
    if fb["model_roi"] is not None and fb["consensus_roi"] is not None:
        d = fb["model_roi"] - fb["consensus_roi"]
        lines.append(f"Model view ({roi(fb['model_roi'])}) {'above' if d > 0 else 'below'} consensus view ({roi(fb['consensus_roi'])}): "
                     f"{'our projections liked these lineups more than the market did — expect that gap to shrink as projections regress' if d > 0.25 else 'the model and the market roughly agree on these lineups'}."
                     + (f" Projection accuracy this week: r {acc['r']:.2f}, MAE {acc['mae']:.1f} DK points." if acc.get("r") is not None and acc.get("mae") is not None else ""))
    elif acc.get("r") is not None:
        lines.append(f"Projection accuracy this week: r {acc['r']:.2f}, MAE {acc['mae']:.1f} DK points on {acc['n']} relevant players.")
    if totals and totals["weeks"] >= 1:
        lines.append(f"Season so far: {totals['weeks']} scored week{'s' if totals['weeks'] != 1 else ''}, consensus ROI {roi(totals['consensus_roi'])} vs field {roi(totals['field_mean_roi'])}, "
                     f"{totals['weeks_above_field_median']}/{totals['weeks_with_field_median']} weeks above the field median on points. Give it a full season before reading anything into it.")
    return lines[:4]


def recap_md(row, totals):
    lu, fb, d, rm, acc, nw = row["lineups"], row["flashback"], row["dupes"], row["real_money"], row["accuracy"], row["news"]
    L = [f"# Week {row['week']} recap — {row['season']} {row['site'] or ''} main slate", "",
         f"_{lu['n'] or 0} GPP lineups built before kickoff, graded against the real DraftKings field"
         + (f" ({fnum(lu['entries'])} entries)" if lu["entries"] else "") + (f". Scored {row['scored_at'][:10]}." if row.get("scored_at") else ".") + "_", "",
         "## Headline numbers", "",
         f"- Average lineup: **{f1(lu['avg_pts'])} pts** vs field median {f1(lu['field_median_pts'])}"
         + (f" ({'+' if lu['avg_vs_field_median'] >= 0 else ''}{lu['avg_vs_field_median']:.1f})" if lu["avg_vs_field_median"] is not None else ""),
         f"- Beat **{pct(lu['beat_pct'])}** of real entries on average" + (f" (best lineup {pct(lu['best_beat_pct'])})" if lu["best_beat_pct"] is not None else ""),
         f"- Best finish: **{fnum(lu['best_rank'])}** of {fnum(lu['entries'])}" + (f" · {lu['top1_lineups']} lineup{'s' if lu['top1_lineups'] != 1 else ''} in the top 1%" if lu["top1_lineups"] is not None else ""),
         ]
    if fb["present"]:
        L += [f"- Flashback consensus ROI: **{roi(fb['consensus_roi'])}** vs field mean {roi(fb['field_mean_roi'])} (median {roi(fb['field_median_roi'])}) · beats {pct(fb['beats_pct'])} of real entries in expectation",
              f"- Model-view ROI: {roi(fb['model_roi'])} · realized ROI: {roi(lu['real_roi'])} · cashed {pct(lu['cash_pct'])} of lineups"]
    else:
        L += ["- Flashback: not run for this week (no standings export), so no expected-value numbers"]
    if d:
        L += [f"- Duplicates: {d['lineups_with_copy']}/{lu['n']} lineups had an exact copy in the field ({d['copies_total']} copies) · "
              f"winnings lost to splits {pct(d['winnings_lost_pct'])} · {pct(d['field_with_copy'])} of a random field sample was duplicated"]
    if rm:
        L += [f"- Real money (payout curve on real ranks): {rm['entries']} entr{'y' if rm['entries'] == 1 else 'ies'} × ${rm['fee']:g} = {money(rm['in'])} in → **{money(rm['out'])}** out ({roi(rm['roi'])}) · best rank {fnum(rm['best_rank'])}"]
    if acc.get("r") is not None:
        bm = acc.get("by_method") or {}
        L += [f"- Projections: r {acc['r']:.3f}, MAE {acc['mae']:.2f}, bias {acc['bias']:+.2f} on {acc['n']} relevant players"
              + (" · " + " · ".join(f"{k} MAE {v['mae']:.2f}" for k, v in bm.items() if v.get("mae") is not None) if bm else "")]
    if nw:
        L += [f"- News agents: {nw['helped']}/{nw['graded']} graded adjustments beat the sim ({pct(nw['helped_pct'])})"
              + (f"; confidence ≥ .7: {pct(nw['hi_conf_helped_pct'])} of {nw['hi_conf_graded']}" if nw["hi_conf_helped_pct"] is not None else "")]
    L += ["", "## What won", ""]
    L += [row["what_won"]["line"] + "." if row["what_won"] else "Not derivable this week (no lineup/board data)."]
    L += ["", "## What missed", ""]
    m = row["misses"]
    if m["over"] or m["under"]:
        L += ["Biggest over-projections (relevant players who played):", ""]
        L += [f"- {x['player']} ({x['pos']}, {x['team']}): projected {x['proj']:.1f}, scored {x['actual']:.1f} ({x['diff']:+.1f})" for x in m["over"]]
        L += ["", "Biggest under-projections:", ""]
        L += [f"- {x['player']} ({x['pos']}, {x['team']}): projected {x['proj']:.1f}, scored {x['actual']:.1f} ({x['diff']:+.1f})" for x in m["under"]]
    else:
        L += ["No player-level results stored for this week."]
    L += ["", "## Honest read", ""]
    L += [f"- {s}" for s in honest_read(row, totals)]
    L += ["", f"_Every number above is read from the database by `jobs/recap.py`; nothing is typed in. Methodology on the Track Record page._", ""]
    return "\n".join(L)


def build(client, season=None):
    slates = fetch_all(client.table("slates").select("*"), order=["season", "week", "slate_key"])
    scored = [s for s in slates if s.get("results_meta")]
    if not scored:
        # a slate whose lineups were graded still counts (results_meta missing, lineups scored)
        lined = {L["slate_id"] for L in fetch_all(client.table("slate_lineups").select("slate_id,scored_at"), order=["slate_id", "idx"]) if L.get("scored_at")}
        scored = [s for s in slates if s["slate_id"] in lined]
    if season is None:
        season = max((s["season"] for s in scored), default=None)
    scored = [s for s in scored if s["season"] == season]
    rows = [week_row(client, s) for s in scored]
    totals = season_totals(rows) if rows else None
    return {"season": season, "built_at": dt.datetime.now(dt.timezone.utc).isoformat(), "source": f"{SOURCE}/{CONTEST}",
            "weeks": rows, "totals": totals}


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--season", type=int)
    ap.add_argument("--week", type=int, help="write the recap for this week only")
    ap.add_argument("--out-dir", default=str(OUT_DIR))
    ap.add_argument("--print", action="store_true", dest="print_", help="print the recap(s) to stdout")
    args = ap.parse_args(argv)
    client = get_client(need_write=False)
    ledger = build(client, args.season)
    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    if not ledger["weeks"]:
        print("recap: no scored slates yet", file=sys.stderr)
        (out / "ledger.json").write_text(json.dumps(ledger, indent=1))
        return 0
    prev = out / "ledger.json"
    if prev.exists():                                   # keep the old timestamp when nothing else changed (no churn commits)
        try:
            old = json.loads(prev.read_text())
            if {k: v for k, v in old.items() if k != "built_at"} == {k: v for k, v in ledger.items() if k != "built_at"}:
                ledger["built_at"] = old["built_at"]
        except Exception:
            pass
    prev.write_text(json.dumps(ledger, indent=1))
    t = ledger["totals"]
    print(f"ledger {ledger['season']}: {t['weeks']} weeks · consensus ROI {roi(t['consensus_roi'])} vs field {roi(t['field_mean_roi'])} · "
          f"{t['weeks_above_field_median']}/{t['weeks_with_field_median']} weeks above field median · top-1% lineups {t['top1_lineups']}"
          + (f" · best finish {fnum(t['best_finish']['rank'])}/{fnum(t['best_finish']['entries'])}" if t["best_finish"] else ""), file=sys.stderr)
    for row in ledger["weeks"]:
        if args.week is not None and row["week"] != args.week:
            continue
        md = recap_md(row, t)
        (out / row["recap"]).write_text(md)
        print(f"  wrote {row['recap']}", file=sys.stderr)
        if args.print_:
            print(md)
    return 0


if __name__ == "__main__":
    sys.exit(main())
