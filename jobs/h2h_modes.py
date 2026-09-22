"""Head-to-head of GPP selection modes on a scored slate (needs a contest standings file imported).
  python jobs/h2h_modes.py DK-2026-02-main [seeds...]
Builds 20 GPP lineups per mode with the slate's stored sim + fitted/heuristic ownership, then grades each lineup with the
real contest FPTS and the real field distribution. One week is noisy — compare over several.

Week-2 head-to-head: build 20 GPP lineups under several selection modes using ONLY what was known Sunday morning
(Thursday sim, heuristic ownership, same overrides), then grade against the real Milly Maker field."""
import json, subprocess, sys, csv, numpy as np, os
sys.path.insert(0, ".")
import results
from common import get_client, fetch_all
c = get_client()
SLATE = sys.argv[1] if len(sys.argv) > 1 and not sys.argv[1].isdigit() else "DK-2026-02-main"
s = c.table("slates").select("*").eq("slate_key", SLATE).execute().data[0]
if not s.get("contest_meta"): raise SystemExit("no contest standings imported for this slate")
own = {r["site_player_id"]: r for r in fetch_all(c.table("slate_ownership").select("*").eq("slate_id", s["slate_id"]), order="site_player_id")}
cm = s["contest_meta"]
pay = __import__("contest_sim").payout_fn("milly", cm["entries"])
q = np.array(cm["quantiles"])
def grade(ids):
    a = sum(float(own[i]["fpts"]) for i in ids if i in own and own[i]["fpts"] is not None)
    cp = results.contest_percentile(a, cm)
    rank = max(1.0, (1 - cp) * cm["entries"])
    return a, cp, float(pay([rank])[0])
OV = ['--set', 'Jaxon Smith-Njigba=15', '--set', 'Justin Jefferson=14.5'] if SLATE == 'DK-2026-02-main' else []   # the overrides used on the day
MODES = {
 "A default (Sunday's)":      ['--stack', '1', '--bringback', '--max-exp', '0.5', '--min-uniq', '3', '--fade', '0.4', '--candidates', '6'],
 "B ev milly":                ['--stack', '1', '--bringback', '--max-exp', '0.5', '--min-uniq', '3', '--fade', '0.4', '--objective', 'ev', '--payout', 'milly', '--entries', str(cm['entries']), '--fee', '20'],
 "C ev + exp.35 uniq4":       ['--stack', '1', '--bringback', '--max-exp', '0.35', '--min-uniq', '4', '--fade', '0.4', '--objective', 'ev', '--payout', 'milly', '--entries', str(cm['entries']), '--fee', '20'],
 "D ev + exp.35 + maxown130": ['--stack', '1', '--bringback', '--max-exp', '0.35', '--min-uniq', '4', '--fade', '0.4', '--objective', 'ev', '--payout', 'milly', '--entries', str(cm['entries']), '--fee', '20', '--max-own', '130'],
 "E ev + stack2 rb":          ['--stack', '2', '--stack-rb', '--bringback', '--max-exp', '0.35', '--min-uniq', '4', '--fade', '0.4', '--objective', 'ev', '--payout', 'milly', '--entries', str(cm['entries']), '--fee', '20'],
}
seeds = [int(x) for x in sys.argv[1:] if x.isdigit()] or [7, 11, 23]
summary = {}
for name, args in MODES.items():
    rows = []
    for seed in seeds:
        out = f"/tmp/h2h_{name[0]}_{seed}.json"
        r = subprocess.run([sys.executable, "build_lineups.py", "--slate-key", SLATE, "--contest", "gpp", "--n", "20", "--seed", str(seed), "--json", out, *args, *OV], capture_output=True, text=True)
        if r.returncode: print(name, seed, "FAILED", r.stderr[-300:]); continue
        L = json.load(open(out))
        g = [grade(x["ids"]) for x in L]
        acts = [a for a, _, _ in g]; cps = [cpp for _, cpp, _ in g]; prizes = [p for _, _, p in g]
        rows.append((np.mean(acts), max(acts), np.mean(cps), max(cps), sum(1 for x in cps if x >= 0.99), sum(1 for x in cps if x >= 0.9), sum(prizes) - 20 * len(L), np.mean([x["proj"] for x in L])))
    a = np.array(rows)
    summary[name] = a.mean(0)
    print(f"{name:<28} proj {a[:,7].mean():6.1f} | actual avg {a[:,0].mean():6.1f} best {a[:,1].mean():6.1f} | field avg {a[:,2].mean()*100:4.0f}% best {a[:,3].mean()*100:4.0f}% | top1% {a[:,4].mean():.1f} top10% {a[:,5].mean():.1f} of 20 | net $ {a[:,6].mean():+7.0f}   (seeds {len(rows)})", flush=True)
