// Contest simulator: expected ROI, win rate and cash rate for lineups in a DFS contest.
//
// 1. Payouts   — a DraftKings-style payout curve from entry fee, entrants, prize pool and 1st prize
//                (power law from 1st place down to the min cash; the cash line is solved so the payouts
//                add up to the prize pool). Cash games (double-ups / 50-50s) pay a flat amount.
// 2. Field     — a sample of opponent lineups drawn from projected ownership: QB first, the usual share
//                of QB stacks and bring-backs, the rest by ownership within the salary cap. The weights
//                are re-fitted a few times so each player's exposure in the field matches his ownership.
// 3. Simulate  — every stored simulation draw is one version of the slate.
//                Two views, as in jobs/flashback.py: the MODEL view scores players with our projections (it
//                believes its own numbers, so it is optimistic — week 2: model +1178% vs realized −85%); the
//                MARKET view moves each player to the betting-market line (props, else the salary-implied line)
//                and so measures construction only (stacks, uniqueness, leverage). Market is the headline. In each draw the field and
//                your lineups are scored with the same player outcomes (so correlations carry through),
//                your finish is read off the field's distribution (with an exponential tail fit above
//                the sample's top scores, so a 3,000-lineup sample can stand in for a 250,000-entry
//                contest), and the payout for that finish is paid out.
//
// Pure functions, no DOM; lineups.js supplies players, ownership and the per-player sim draws.

export const PRESETS = [
  { key: "mass3",  label: "Large GPP · $3",        type: "gpp",  fee: 3,  entrants: 294000, pool: 750000,  first: 100000 },
  { key: "milly",  label: "Millionaire · $20",     type: "gpp",  fee: 20, entrants: 176000, pool: 3000000, first: 1000000 },
  { key: "mid20",  label: "Mid GPP · $20",         type: "gpp",  fee: 20, entrants: 10000,  pool: 170000,  first: 20000 },
  { key: "small5", label: "Small GPP · $5",        type: "gpp",  fee: 5,  entrants: 1000,   pool: 4250,    first: 750 },
  { key: "dbl",    label: "Double-up · $10",       type: "cash", fee: 10, entrants: 1000,   pool: 8900,    first: 20 },
];

export function mulberry32(seed) {
  let a = seed >>> 0;
  return () => { a = (a + 0x6D2B79F5) >>> 0; let t = a; t = Math.imul(t ^ (t >>> 15), t | 1); t ^= t + Math.imul(t ^ (t >>> 7), t | 61); return ((t ^ (t >>> 14)) >>> 0) / 4294967296; };
}

// ---------------------------------------------------------------- payouts
// Returns { pay: Float64Array (index = rank, 1-based), cashLine, minCash, first, total, rake }
export function buildPayouts({ type = "gpp", fee, entrants, pool, first, minCashMult = 2, cashFrac = 0.22 }) {
  const N = Math.max(2, Math.round(entrants));
  if (type === "cash") {
    // double-up: everyone in the top cashLine gets the same prize; the pool decides how many get paid
    const prize = Math.max(fee * 1.01, first || fee * 2);
    const cashLine = Math.max(1, Math.min(N - 1, Math.floor(pool / prize)));
    const pay = new Float64Array(cashLine + 1).fill(prize); pay[0] = 0;
    return { pay, cashLine, minCash: prize, first: prize, total: prize * cashLine, rake: 1 - pool / (fee * N) };
  }
  // DK-style curve: everyone in the top ~22% gets at least the min cash; above that a power law climbs
  // to the 1st-place prize. The exponent is solved so the payouts add up to the prize pool.
  const m = Math.max(fee * 1.2, fee * minCashMult);
  const F1 = Math.max(first || pool * 0.15, m * 2);
  let rc = Math.max(2, Math.round(cashFrac * N));
  if (rc * m + (F1 - m) > pool) rc = Math.max(2, Math.floor((pool - (F1 - m)) / m));
  const total = (a) => { let s = 0; for (let r = 1; r <= rc; r++) s += Math.pow(r, -a); return rc * m + (F1 - m) * s; };
  let lo = 0.2, hi = 6;
  for (let it = 0; it < 50; it++) { const mid = (lo + hi) / 2; if (total(mid) > pool) lo = mid; else hi = mid; }
  const a = (lo + hi) / 2;
  const pay = new Float64Array(rc + 1);
  let s = 0; for (let r = 1; r <= rc; r++) { pay[r] = m + (F1 - m) * Math.pow(r, -a); s += pay[r]; }
  const k = pool / s; for (let r = 1; r <= rc; r++) pay[r] *= k;   // exact prize-pool total
  return { pay, cashLine: rc, minCash: pay[rc], first: pay[1], total: pool, rake: 1 - pool / (fee * N) };
}

// ---------------------------------------------------------------- field
// players: [{ pos: "QB"|"RB"|"WR"|"TE"|"DST", team, opp, salary, own (0-100) }]
// slots:   e.g. ["QB","RB","RB","WR","WR","WR","TE","FLEX","DST"]
// returns { idx: Int32Array(size * slots.length), size, width, exposure: Float64Array }
// Stack rates are from real DK fields (jobs/field_stats.py: 81–87% of lineups QB-stacked, 38–40% with a bring-back).
export function buildField({ players, slots, cap, minSalary = 0, size = 3000, seed = 7, stackRate = 0.85, stack2Rate = 0.35, bringRate = 0.47, rounds = 4, maxTries = 25 }) {
  const rng = mulberry32(seed);
  const P = players.length, W = slots.length;
  const byPos = { QB: [], RB: [], WR: [], TE: [], DST: [] };
  players.forEach((p, i) => { if (p.own >= 0.05) byPos[p.pos]?.push(i); });   // ignore ~0%-owned players
  const flexPos = ["RB", "WR", "TE"];
  const minSal = {}; Object.keys(byPos).forEach(k => { minSal[k] = Math.min(...byPos[k].map(i => players[i].salary), Infinity); });
  minSal.FLEX = Math.min(minSal.RB, minSal.WR, minSal.TE);
  const byTeam = new Map(); players.forEach((p, i) => { if (p.own < 0.05) return; if (!byTeam.has(p.team)) byTeam.set(p.team, []); byTeam.get(p.team).push(i); });
  const flexList = [...byPos.RB, ...byPos.WR, ...byPos.TE];
  const target = Float64Array.from(players, p => Math.max(0, p.own) / 100);
  const w = Float64Array.from(target, t => Math.max(t, 0.0005));
  const eligible = (i, slot) => slot === "FLEX" ? flexPos.includes(players[i].pos) : players[i].pos === slot;

  const pick = (cands, used, budget) => {
    let tot = 0; for (const i of cands) if (!used.has(i) && players[i].salary <= budget) tot += w[i];
    if (tot <= 0) return -1;
    let r = rng() * tot;
    for (const i of cands) { if (used.has(i) || players[i].salary > budget) continue; r -= w[i]; if (r <= 0) return i; }
    return -1;
  };

  const one = (out, o) => {
    for (let attempt = 0; attempt < maxTries; attempt++) {
      const open = slots.slice(); const used = new Set(); const got = new Array(W).fill(-1); let sal = 0;
      const reserve = () => open.reduce((s, sl) => s + (minSal[sl] ?? 0), 0);
      const place = (i) => {   // put player i in his own position slot, else FLEX
        let k = open.indexOf(players[i].pos); if (k < 0 && flexPos.includes(players[i].pos)) k = open.indexOf("FLEX");
        if (k < 0) return false;
        const slotIdx = slots.findIndex((s, j) => s === open[k] && got[j] < 0);
        got[slotIdx] = i; used.add(i); sal += players[i].salary; open.splice(k, 1); return true;
      };
      const qb = pick(byPos.QB, used, cap - sal - (reserve() - minSal.QB)); if (qb < 0) continue; place(qb);
      const mates = (byTeam.get(players[qb].team) || []).filter(i => ["WR", "TE", "RB"].includes(players[i].pos));
      const tryAdd = (cands) => { const fits = cands.filter(i => open.includes(players[i].pos) || open.includes("FLEX")); const i = pick(fits, used, cap - sal - (reserve() - minSal.FLEX)); if (i >= 0) place(i); };
      if (rng() < stackRate) {
        tryAdd(mates.filter(i => players[i].pos !== "RB"));
        if (rng() < stack2Rate) tryAdd(mates);
        if (rng() < bringRate) tryAdd((byTeam.get(players[qb].opp) || []).filter(i => ["WR", "TE", "RB"].includes(players[i].pos)));
      }
      // fill the rest in random order, FLEX last
      const rest = open.filter(s => s !== "FLEX");
      for (let i = rest.length - 1; i > 0; i--) { const j = Math.floor(rng() * (i + 1)); [rest[i], rest[j]] = [rest[j], rest[i]]; }
      if (open.includes("FLEX")) rest.push("FLEX");
      let ok = true;
      for (const slot of rest) {
        const cands = slot === "FLEX" ? flexList : byPos[slot];
        const i = pick(cands, used, cap - sal - (reserve() - (minSal[slot] ?? 0)));
        if (i < 0 || !place(i)) { ok = false; break; }
      }
      if (!ok || got.some(g => g < 0)) continue;
      const last = attempt === maxTries - 1;
      if (sal < minSalary && !last) continue;
      for (let j = 0; j < W; j++) out[o + j] = got[j];
      return true;
    }
    return false;
  };

  let idx = new Int32Array(size * W), made = 0;
  const exposure = new Float64Array(P);
  for (let round = 0; round < rounds; round++) {
    made = 0; exposure.fill(0);
    for (let f = 0; f < size; f++) if (one(idx, made * W)) made++;
    for (let j = 0; j < made * W; j++) exposure[idx[j]] += 1 / made;
    if (round === rounds - 1) break;
    for (let i = 0; i < P; i++) { const r = (target[i] + 0.002) / (exposure[i] + 0.002); w[i] = Math.max(1e-5, w[i] * Math.min(5, Math.max(0.2, Math.pow(r, 0.8)))); }
  }
  if (made < size) idx = idx.slice(0, made * W);
  return { idx, size: made, width: W, exposure };
}

// ---------------------------------------------------------------- simulate
// scores:   Array(P) of Float32Array(nSims) — each player's fantasy points in every sim draw
// lineups:  Array of Int32Array(width) player indices (your lineups / the candidate pool)
// returns   { stats: [{ ev, roi, top01, top1, cash, avgPct }], q: Float32Array(nL * nSims) finish quantiles, nSims }
export async function simulate({ scores, field, lineups, entrants, payouts, fee, nSims, onProgress, yieldEvery = 100 }) {
  const P = scores.length, F = field.size, W = field.width, nL = lineups.length, N = entrants;
  const v = new Float32Array(P), fs = new Float32Array(F);
  const q = new Float32Array(nL * nSims);
  const ev = new Float64Array(nL), top01 = new Float64Array(nL), top1 = new Float64Array(nL), cash = new Float64Array(nL), pctSum = new Float64Array(nL);
  const m = Math.max(5, Math.min(40, Math.floor(F / 100)));
  const top1Rank = Math.max(1, 0.01 * N), top01Rank = Math.max(1, 0.001 * N);
  const payAt = (rank) => { const r = Math.max(1, Math.round(rank)); return r < payouts.pay.length ? payouts.pay[r] : 0; };
  for (let s = 0; s < nSims; s++) {
    for (let p = 0; p < P; p++) v[p] = scores[p][s];
    for (let f = 0, o = 0; f < F; f++, o += W) { let t = 0; for (let j = 0; j < W; j++) t += v[field.idx[o + j]]; fs[f] = t; }
    fs.sort();
    const xm = fs[F - m]; let tailMean = 0; for (let k = F - m; k < F; k++) tailMean += fs[k]; tailMean /= m;
    const beta = Math.max(0.5, tailMean - xm);
    for (let l = 0; l < nL; l++) {
      const L = lineups[l]; let sc = 0; for (let j = 0; j < W; j++) sc += v[L[j]];
      let lo = 0, hi = F; while (lo < hi) { const mid = (lo + hi) >> 1; if (fs[mid] <= sc) lo = mid + 1; else hi = mid; }
      const above = F - lo;                              // field lineups that beat this one
      const qq = above >= m ? above / F : Math.min(above >= 1 ? (above + 0.5) / F : 1, (m / F) * Math.exp(-(sc - xm) / beta));
      q[l * nSims + s] = qq;
      const rank = 1 + qq * (N - 1);
      const pay = payAt(rank);
      ev[l] += pay; pctSum[l] += qq;
      if (rank <= top01Rank) top01[l]++;
      if (rank <= top1Rank) top1[l]++;
      if (pay > 0) cash[l]++;
    }
    if (onProgress && s % yieldEvery === yieldEvery - 1) { onProgress((s + 1) / nSims); await new Promise(r => setTimeout(r)); }
  }
  const stats = [];
  for (let l = 0; l < nL; l++) { const e = ev[l] / nSims; stats.push({ ev: e, roi: e / fee - 1, top01: top01[l] / nSims, top1: top1[l] / nSims, cash: cash[l] / nSims, avgPct: pctSum[l] / nSims }); }
  return { stats, q, nSims };
}

// portfolio view of a subset of the simulated lineups (row numbers into the q matrix)
export function portfolio({ rows, q, nSims, entrants, payouts, fee }) {
  const N = entrants, top1Rank = Math.max(1, 0.01 * N), top01Rank = Math.max(1, 0.001 * N);
  const payAt = (rank) => { const r = Math.max(1, Math.round(rank)); return r < payouts.pay.length ? payouts.pay[r] : 0; };
  let evTot = 0, anyCash = 0, anyTop1 = 0, anyTop01 = 0, profitable = 0;
  const totals = new Float64Array(nSims);
  for (let s = 0; s < nSims; s++) {
    let tot = 0, c = false, t1 = false, t01 = false;
    for (const l of rows) { const rank = 1 + q[l * nSims + s] * (N - 1); const pay = payAt(rank); tot += pay; if (pay > 0) c = true; if (rank <= top1Rank) t1 = true; if (rank <= top01Rank) t01 = true; }
    totals[s] = tot; evTot += tot; if (c) anyCash++; if (t1) anyTop1++; if (t01) anyTop01++; if (tot > rows.length * fee) profitable++;
  }
  totals.sort();
  const cost = rows.length * fee, ev = evTot / nSims;
  return { cost, ev, profit: ev - cost, roi: cost ? ev / cost - 1 : 0, anyCash: anyCash / nSims, anyTop1: anyTop1 / nSims, anyTop01: anyTop01 / nSims,
    profitable: profitable / nSims, median: totals[Math.floor(nSims / 2)] - cost };
}
