"""
Score a completed slate: projection vs actual for every player, calibration of the sim
distribution, and (if a contest standings file was imported) actual ownership.

Runs from the nightly ingest for any slate whose games are all final and not yet scored:
  python jobs/results.py --all
Or one slate (re-score with --force):
  python jobs/results.py --slate-key DK-2026-02-main [--force]

Writes slate_results (one row per player) and slates.results_meta (summary).
Actual points: contest FPTS when imported (exact, incl. DST), else recomputed from
player_game_stats (DST approximated from opponent sacks / INTs / points allowed).
"""
from __future__ import annotations

import argparse
import datetime as dt
import gzip
import json
import math
import urllib.request

import numpy as np

from common import chunked, fetch_all, get_client

RELEVANT = {"QB": 12.0, "RB": 8.0, "WR": 8.0, "TE": 5.0, "DST": 4.0}
PA_TIERS = [(0, 10), (6, 7), (13, 4), (20, 1), (27, 0), (34, -1), (10 ** 9, -4)]
SLOTS_PER_POS = {"QB": 1.0, "RB": 2.4, "WR": 3.4, "TE": 1.2, "DST": 1.0}


def heuristic_ownership(rows, b=1.4, c=0.8, cap=60.0):
    """Same softmax the lineup builder uses (value + projection z-scores within position)."""
    out = {}
    for pos, slots in SLOTS_PER_POS.items():
        pool = [r for r in rows if r["position"] == pos and (r["proj_mean"] or 0) > 0]
        if not pool:
            continue
        val = np.array([r["proj_mean"] / (r["salary"] / 1000) for r in pool])
        pr = np.array([r["proj_mean"] for r in pool])
        z = lambda a: (a - a.mean()) / (a.std() or 1)
        w = np.exp(b * z(val) + c * z(pr))
        for r, wi in zip(pool, w):
            out[r["site_player_id"]] = float(min(cap, 100 * slots * wi / w.sum()))
    return out


def compute_results(slate, sal, best, own, actual, matrix, dst_fn=lambda team: None):
    """Pure scoring step: salaries + chosen projections + ownership + actual box scores -> rows, summary."""
    sid, site = slate["slate_id"], slate["site"]
    pts_col = "dk_points" if site == "DK" else "fd_points"
    rows = []
    for s in sal:
        p = best.get(s["site_player_id"])
        o = own.get(s["site_player_id"])
        if s["position"] == "DST":
            a, src, played = (o["fpts"], "contest", True) if o and o.get("fpts") is not None else (dst_fn(s["team"]), "stats_approx", True)
        else:
            r = actual.get(s["player_id"])
            if o and o.get("fpts") is not None:
                a, src = float(o["fpts"]), "contest"
            elif r:
                a, src = float(r[pts_col] or 0), "stats"
            else:
                a, src = 0.0, "stats"
            played = bool(r and (float(r["attempts"] or 0) + float(r["carries"] or 0) + float(r["targets"] or 0)) > 0)
        if a is None:
            continue
        pit = None
        if matrix is not None and s["site_player_id"] in matrix and played:
            pit = round(float((matrix[s["site_player_id"]] <= a).mean()), 3)
        rows.append({
            "slate_id": sid, "site_player_id": s["site_player_id"], "player_id": s["player_id"],
            "player_name": s["player_name"], "position": s["position"], "team": s["team"], "salary": s["salary"],
            "method": p["method"] if p else None,
            "proj_mean": float(p["mean"]) if p else None, "proj_p10": float(p["floor"]) if p else None,
            "proj_p50": float(p["median"]) if p else None, "proj_p90": float(p["ceiling"]) if p else None,
            "actual": round(a, 2), "actual_source": src, "played": played, "pit": pit,
            "own_actual": float(o["ownership_pct"]) if o else None, "own_heuristic": None,
            "computed_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        })
    heur = heuristic_ownership([r for r in rows if r["proj_mean"] is not None])
    for r in rows:
        r["own_heuristic"] = round(heur.get(r["site_player_id"], 0.0), 1)

    scored = [r for r in rows if r["played"] and r["proj_mean"] is not None and r["proj_mean"] >= RELEVANT.get(r["position"], 8)]
    a = np.array([r["actual"] for r in scored]); pm = np.array([r["proj_mean"] for r in scored])
    meta = {"n": int(len(scored)), "scored_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "mae": round(float(np.abs(pm - a).mean()), 2) if len(a) else None,
            "r": round(float(np.corrcoef(pm, a)[0, 1]), 3) if len(a) > 2 else None,
            "bias": round(float((pm - a).mean()), 2) if len(a) else None,
            "coverage": {"p10": round(float(np.mean([r["actual"] <= r["proj_p10"] for r in scored])), 3),
                         "p90": round(float(np.mean([r["actual"] <= r["proj_p90"] for r in scored])), 3)} if len(a) else None,
            "has_ownership": bool(own), "actual_source": "contest" if own else "stats"}
    by_pos = {}
    for pos in ("QB", "RB", "WR", "TE", "DST"):
        rs = [r for r in scored if r["position"] == pos]
        if len(rs) >= 3:
            pa_ = np.array([r["proj_mean"] for r in rs]); aa = np.array([r["actual"] for r in rs])
            by_pos[pos] = {"n": len(rs), "mae": round(float(np.abs(pa_ - aa).mean()), 2), "bias": round(float((pa_ - aa).mean()), 2)}
    meta["by_pos"] = by_pos
    return rows, meta


def method_comparison(rows, by_method):
    """Score every projection source on the same players: sim, baseline, external, and a 50/50
    sim+external blend. Only DFS-relevant players who played, same set for every source."""
    scored = [r for r in rows if r["played"] and r["proj_mean"] is not None and r["proj_mean"] >= RELEVANT.get(r["position"], 8)]
    sources = dict(by_method)
    if "sim" in sources and "external" in sources:
        sources["blend50"] = {k: 0.5 * sources["sim"][k] + 0.5 * sources["external"][k]
                              for k in sources["sim"] if k in sources["external"]
                              and sources["sim"][k] is not None and sources["external"][k] is not None}
    out = {}
    for name, m in sources.items():
        pairs = [(m[r["site_player_id"]], r["actual"]) for r in scored if m.get(r["site_player_id"]) is not None]
        if len(pairs) < 10:
            continue
        p = np.array([x for x, _ in pairs]); a = np.array([y for _, y in pairs])
        out[name] = {"n": len(pairs), "mae": round(float(np.abs(p - a).mean()), 2),
                     "r": round(float(np.corrcoef(p, a)[0, 1]), 3), "bias": round(float((p - a).mean()), 2)}
    return out


def slate_complete(client, slate):
    ids = slate.get("game_ids") or []
    if not ids:
        return False
    games = fetch_all(client.table("games").select("game_id,home_score,away_score").in_("game_id", ids), order="game_id")
    return len(games) == len(ids) and all(g["home_score"] is not None for g in games)


def dst_actual(client, slate, team):
    """Approximate DST points from the opponent's box score (no fumbles / return TDs)."""
    opp_rows = fetch_all(client.table("team_game_stats").select("team,opponent_team,sacks,interceptions")
                         .eq("season", slate["season"]).eq("week", slate["week"]).eq("opponent_team", team), order="team")
    if not opp_rows:
        return None
    opp = opp_rows[0]
    g = fetch_all(client.table("games").select("home_team,away_team,home_score,away_score")
                  .eq("season", slate["season"]).eq("week", slate["week"]).or_(f"home_team.eq.{team},away_team.eq.{team}"), order="game_id")
    if not g or g[0]["home_score"] is None:
        return None
    pa = g[0]["away_score"] if g[0]["home_team"] == team else g[0]["home_score"]
    tier = next(v for upper, v in PA_TIERS if pa <= upper)
    return float(opp["sacks"] or 0) + 2 * float(opp["interceptions"] or 0) + tier


def score_slate(client, slate, write=True):
    sid, site = slate["slate_id"], slate["site"]
    pts_col = "dk_points" if site == "DK" else "fd_points"
    sal = fetch_all(client.table("slate_salaries").select("*").eq("slate_id", sid), order="site_player_id")
    proj = fetch_all(client.table("slate_projections").select("*").eq("slate_id", sid), order=["site_player_id", "method"])
    best, by_method = {}, {}
    for p in proj:                                   # prefer sim over baseline for the headline
        by_method.setdefault(p["method"], {})[p["site_player_id"]] = float(p["mean"]) if p["mean"] is not None else None
        cur = best.get(p["site_player_id"])
        if cur is None or (p["method"] == "sim" and cur["method"] != "sim"):
            best[p["site_player_id"]] = p
    own = {r["site_player_id"]: r for r in fetch_all(client.table("slate_ownership").select("*").eq("slate_id", sid), order="site_player_id")}
    pids = [s["player_id"] for s in sal if s["player_id"] and not s["player_id"].startswith("DST_")]
    actual = {}
    for batch in chunked(pids, 200):
        for r in fetch_all(client.table("player_game_stats").select(f"player_id,{pts_col},attempts,carries,targets")
                           .in_("player_id", batch).eq("season", slate["season"]).eq("week", slate["week"]).eq("season_type", "REG"),
                           order="player_id"):
            actual[r["player_id"]] = r

    # sim matrix for PIT
    matrix = None
    url = (slate.get("sim_meta") or {}).get("url")
    if url:
        try:
            raw = urllib.request.urlopen(url, timeout=60).read()
            data = json.loads(gzip.decompress(raw) if raw[:2] == b"\x1f\x8b" else raw)
            matrix = {pid: np.array(sc) for pid, sc in zip(data["players"], data["scores"])}
        except Exception as e:
            print(f"  (sim matrix unavailable: {e})")

    rows, meta = compute_results(slate, sal, best, own, actual, matrix, dst_fn=lambda team: dst_actual(client, slate, team))
    meta["by_method"] = method_comparison(rows, by_method)
    print(f"  {slate['slate_key']}: {len(rows)} players, {meta['n']} scored · MAE {meta['mae']} r {meta['r']} bias {meta['bias']} · cov {meta['coverage']}")
    if write:
        for batch in chunked(rows):
            client.table("slate_results").upsert(batch, on_conflict="slate_id,site_player_id").execute()
        client.table("slates").update({"results_meta": meta}).eq("slate_id", sid).execute()
    return rows, meta


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--slate-key")
    ap.add_argument("--all", action="store_true", help="score every completed slate not yet scored")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    client = get_client(need_write=not args.dry_run)
    q = client.table("slates").select("*")
    slates = (q.eq("slate_key", args.slate_key) if args.slate_key else q).execute().data
    done = 0
    for s in slates:
        if not args.force and not args.slate_key and s.get("results_meta"):
            continue
        if not slate_complete(client, s):
            if args.slate_key:
                print(f"{s['slate_key']}: games not final yet")
            continue
        score_slate(client, s, write=not args.dry_run)
        done += 1
    if not done:
        print("nothing to score")


if __name__ == "__main__":
    main()
