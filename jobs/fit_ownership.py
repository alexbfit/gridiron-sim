"""
Fit the lineup builder's ownership heuristic to actual ownership.

The builder estimates ownership with a softmax over value and projection z-scores within each
position:  own_i = min(cap, 100 * slots_pos * exp(b*z_value + c*z_proj) / sum).
This script grid-searches (b, c, cap) against every slate that has real ownership imported
(slate_results.own_actual) and stores the best fit in model_params['ownership_model'], which
the builder reads on load. Falls back to the defaults (1.4, 0.8, 60) with < 100 data points.

  python jobs/fit_ownership.py [--dry-run]
"""
from __future__ import annotations

import argparse
import datetime as dt
import itertools

import numpy as np

from common import fetch_all, get_client
from results import heuristic_ownership

DEFAULT = {"b": 1.4, "c": 0.8, "cap": 60.0}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    client = get_client(need_write=not args.dry_run)
    try:
        rows = fetch_all(client.table("slate_results").select("slate_id,site_player_id,position,salary,proj_mean,own_actual")
                         .not_.is_("own_actual", "null").not_.is_("proj_mean", "null"), order=["slate_id", "site_player_id"])
    except Exception as e:
        print(f"slate_results unavailable ({e}) — run 005_results_ownership.sql")
        return
    by_slate = {}
    for r in rows:
        by_slate.setdefault(r["slate_id"], []).append(r)
    n = len(rows)
    print(f"{n} player rows with actual ownership across {len(by_slate)} slate(s)")
    if n < 100:
        print("not enough data to fit — builder keeps the defaults")
        return

    def loss(b, c, cap):
        err, cnt = 0.0, 0
        for rs in by_slate.values():
            h = heuristic_ownership(rs, b, c, cap)
            for r in rs:
                if r["site_player_id"] in h:
                    err += (h[r["site_player_id"]] - float(r["own_actual"])) ** 2
                    cnt += 1
        return err / max(cnt, 1)

    base = loss(**DEFAULT)
    best, best_l = dict(DEFAULT), base
    for b, c, cap in itertools.product(np.arange(0.4, 3.01, 0.2), np.arange(0.0, 2.01, 0.2), (40.0, 50.0, 60.0, 75.0)):
        l = loss(b, c, cap)
        if l < best_l:
            best, best_l = {"b": round(float(b), 2), "c": round(float(c), 2), "cap": cap}, l
    print(f"default RMSE {base ** 0.5:.2f} pts of ownership → fitted RMSE {best_l ** 0.5:.2f} with {best}")
    payload = {**best, "rmse": round(best_l ** 0.5, 2), "default_rmse": round(base ** 0.5, 2), "n": n,
               "slates": len(by_slate), "fitted_at": dt.datetime.now(dt.timezone.utc).isoformat()}
    if args.dry_run:
        print("dry run —", payload)
        return
    client.table("model_params").upsert({"param_key": "ownership_model", "param_value": payload,
                                         "updated_at": payload["fitted_at"]}, on_conflict="param_key").execute()
    print("saved model_params.ownership_model")


if __name__ == "__main__":
    main()
