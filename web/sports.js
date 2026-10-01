/* GameTime Win — multi-sport engine for the browser (port of jobs/sports/: rules, sim, optimizer).
   Used by Simple Mode for every sport except NFL (which keeps the Supabase slate + NFL model).
   Inputs: a DraftKings entries/salaries file for the sport plus a projections CSV (name, proj [, p25 p50 p75 p85 p95 p99,
   team, opp, line, order]). Missing percentiles come from SHAPES, the per-sport distribution fitted on the SaberSim archive. */

export const RULES = {
  NBA:    { slots: [["PG", ["PG"]], ["SG", ["SG"]], ["SF", ["SF"]], ["PF", ["PF"]], ["C", ["C"]], ["G", ["G"]], ["F", ["F"]], ["UTIL", ["UTIL"]]], minGames: 2, maxTeam: 8, stack: "game", stackN: 3,
            positions: ["PG", "SG", "SF", "PF", "C"] },
  WNBA:   { slots: [["G", ["G"]], ["G", ["G"]], ["F", ["F"]], ["F", ["F"]], ["F", ["F"]], ["UTIL", ["UTIL"]]], minGames: 2, maxTeam: 6, stack: "game", stackN: 2, positions: ["G", "F"] },
  CBB:    { slots: [["G", ["G"]], ["G", ["G"]], ["G", ["G"]], ["F", ["F"]], ["F", ["F"]], ["F", ["F"]], ["UTIL", ["UTIL"]], ["UTIL", ["UTIL"]]], minGames: 2, maxTeam: 8, stack: "game", stackN: 3, positions: ["G", "F"] },
  NHL:    { slots: [["C", ["C"]], ["C", ["C"]], ["W", ["W"]], ["W", ["W"]], ["W", ["W"]], ["D", ["D"]], ["D", ["D"]], ["G", ["G"]], ["UTIL", ["UTIL"]]], minGames: 2, maxTeam: 8, stack: "line", stackN: 3, noOpp: "goalie",
            positions: ["C", "W", "D", "G"] },
  MLB:    { slots: [["P", ["P"]], ["P", ["P"]], ["C", ["C"]], ["1B", ["1B"]], ["2B", ["2B"]], ["3B", ["3B"]], ["SS", ["SS"]], ["OF", ["OF"]], ["OF", ["OF"]], ["OF", ["OF"]]], minGames: 2, maxTeam: 10, maxHittersTeam: 5,
            stack: "hitters", stackN: [5, 3], noOpp: "pitcher", positions: ["P", "C", "1B", "2B", "3B", "SS", "OF"] },
  CFB:    { slots: [["QB", ["QB"]], ["RB", ["RB"]], ["RB", ["RB"]], ["WR", ["WR"]], ["WR", ["WR"]], ["WR", ["WR"]], ["FLEX", ["FLEX"]], ["S-FLEX", ["S-FLEX"]]], minGames: 2, maxTeam: 8, stack: "pass", stackN: 2, bringback: true,
            positions: ["QB", "RB", "WR"] },
  SOCCER: { slots: [["F", ["F"]], ["F", ["F"]], ["M", ["M"]], ["M", ["M"]], ["D", ["D"]], ["D", ["D"]], ["GK", ["GK"]], ["UTIL", ["UTIL"]]], minGames: 2, maxTeam: 8, stack: "team", stackN: 3, positions: ["F", "M", "D", "GK"] },
  GOLF:   { slots: Array(6).fill(["G", ["X"]]), minGames: 1, maxTeam: 6, positions: [] },
  NASCAR: { slots: Array(6).fill(["D", ["X"]]), minGames: 1, maxTeam: 6, positions: [] },
  TEN:    { slots: Array(6).fill(["P", ["X"]]), minGames: 1, maxTeam: 6, noOpp: "match", positions: [] },
  MMA:    { slots: Array(6).fill(["F", ["X"]]), minGames: 1, maxTeam: 6, noOpp: "match", positions: [] },
};
export const SPORT_NAMES = { NFL: "NFL", NBA: "NBA", MLB: "MLB", NHL: "NHL", WNBA: "WNBA", CFB: "College football", CBB: "College basketball", SOCCER: "Soccer", TEN: "Tennis", GOLF: "Golf", NASCAR: "NASCAR", MMA: "MMA" };
const NO_POS = new Set(["GOLF", "NASCAR", "TEN", "MMA"]);

// median of (percentile / projection) by sport and position bucket, fitted on 2,100 archived slates (jobs/sports/shapes.json)
const SHAPES = {
  NBA: { X: [0.739, 0.974, 1.235, 1.385, 1.647, 1.957] }, WNBA: { X: [0.727, 0.973, 1.244, 1.4, 1.679, 2.01] }, CBB: { X: [0.75, 0.979, 1.227, 1.369, 1.618, 1.92] },
  NHL: { S: [0.358, 0.748, 1.406, 1.871, 2.729, 3.943], G: [0.349, 1.02, 1.645, 1.944, 2.505, 3.072] },
  MLB: { P: [0.0, 0.329, 1.957, 2.881, 4.38, 5.903], H: [0.282, 0.742, 1.49, 2.025, 2.926, 4.092] },
  SOCCER: { X: [0.519, 0.854, 1.273, 1.643, 2.41, 3.461] },
  CFB: { RB: [0.424, 0.761, 1.322, 1.744, 2.6, 3.709], QB: [0.682, 0.951, 1.259, 1.452, 1.8, 2.222], WR: [0.064, 0.657, 1.374, 2.03, 3.234, 4.927] },
  GOLF: { X: [0.557, 0.976, 1.344, 1.498, 1.773, 2.119] }, NASCAR: { X: [0.488, 0.98, 1.475, 1.792, 2.287, 2.768] },
  TEN: { X: [0.549, 1.024, 1.389, 1.541, 1.675, 1.795] }, MMA: { X: [0.37, 0.936, 1.457, 1.788, 2.151, 2.486] },
};
const QP = [0.02, 0.25, 0.50, 0.75, 0.85, 0.95, 0.99, 0.999];

export const normName = (s) => String(s || "").toLowerCase().replace(/\b(jr|sr|ii|iii|iv)\b\.?/g, "").replace(/[^a-z0-9]/g, "");

function bucket(sport, p) {
  if (sport === "MLB") return p.pos.has("P") ? "P" : "H";
  if (sport === "NHL") return p.pos.has("G") ? "G" : "S";
  if (sport === "CFB") return ["QB", "RB", "WR"].find(t => p.pos.has(t)) || "WR";
  return "X";
}
export function fillQuantiles(sport, p) {
  if (p.q && p.q.some(v => v > 0)) return;
  const sh = SHAPES[sport] || {}; const s = sh[bucket(sport, p)] || sh.X || Object.values(sh)[0] || [0.6, 0.95, 1.3, 1.5, 1.85, 2.3];
  p.q = s.map(f => p.proj * f);
}
function curve(p) {
  const q = p.q, lo = Math.min(q[0], q[0] - Math.max(q[1] - q[0], 0.25 * p.proj));
  const hi = q[5] + 0.5 * Math.max(q[5] - q[4], 0.1 * p.proj);
  const vals = [lo, ...q, hi]; for (let i = 1; i < vals.length; i++) vals[i] = Math.max(vals[i], vals[i - 1]);
  return vals;
}
const interp = (u, vals) => { if (u <= QP[0]) return vals[0]; if (u >= QP[QP.length - 1]) return vals[vals.length - 1];
  let i = 1; while (QP[i] < u) i++; const t = (u - QP[i - 1]) / (QP[i] - QP[i - 1]); return vals[i - 1] + t * (vals[i] - vals[i - 1]); };
function gauss() { let u = 0, v = 0; while (!u) u = Math.random(); while (!v) v = Math.random(); return Math.sqrt(-2 * Math.log(u)) * Math.cos(2 * Math.PI * v); }
const Phi = (z) => 0.5 * (1 + erf(z / Math.SQRT2));
function erf(x) { const s = Math.sign(x); x = Math.abs(x); const t = 1 / (1 + 0.3275911 * x);
  return s * (1 - (((((1.061405429 * t - 1.453152027) * t) + 1.421413741) * t - 0.284496736) * t + 0.254829592) * t * Math.exp(-x * x)); }

/* Correlated draws: one shared factor per group (MLB team hitters .12, NHL line .25, NHL goalie vs opposing skaters -.2,
   tennis/MMA opponents -.85) plus an independent part, mapped through each player's quantile curve. Means match `proj`. */
export function simulate(sport, players, n = 3000) {
  const groups = new Map(), gkey = (p) => {
    if (sport === "MLB") return p.pos.has("P") ? null : "t:" + p.team;
    if (sport === "NHL") return p.pos.has("G") ? null : (p.line ? `l:${p.team}|${p.line}` : "t:" + p.team);
    return null;
  };
  const rho = { MLB: 0.12, NHL: 0.25 }[sport] || 0;
  const factor = (k) => { if (!groups.has(k)) groups.set(k, Float32Array.from({ length: n }, gauss)); return groups.get(k); };
  const draws = new Map(), zs = new Map();
  const byName = new Map(players.map(p => [p.name, p]));
  for (const p of players) {
    const vals = curve(p), z = new Float32Array(n), g = gkey(p);
    const F = g ? factor(g) : null, a = Math.sqrt(rho), b = Math.sqrt(1 - rho);
    for (let i = 0; i < n; i++) z[i] = F ? a * F[i] + b * gauss() : gauss();
    if ((sport === "TEN" || sport === "MMA") && p.opp) { const o = zs.get(byName.get(p.opp)); if (o) for (let i = 0; i < n; i++) z[i] = -0.85 * o[i] + Math.sqrt(1 - 0.85 * 0.85) * z[i]; }
    if (sport === "NHL" && p.pos.has("G")) { const T = groups.get("t:" + p.opp); if (T) for (let i = 0; i < n; i++) z[i] = -0.2 * T[i] + Math.sqrt(1 - 0.04) * z[i]; }
    zs.set(p, z);
    const d = new Float32Array(n); let m = 0;
    for (let i = 0; i < n; i++) { d[i] = interp(Phi(z[i]), vals); m += d[i]; }
    const shift = p.proj - m / n;
    for (let i = 0; i < n; i++) d[i] = Math.max(d[i] + shift, vals[0] + Math.min(shift, 0));
    draws.set(p.id, d);
  }
  return { n, draws };
}
export function lineupQuantiles(sim, ids) {
  const tot = new Float32Array(sim.n);
  ids.forEach(id => { const a = sim.draws.get(id); if (a) for (let i = 0; i < sim.n; i++) tot[i] += a[i]; });
  const s = tot.sort(), q = (x) => s[Math.min(sim.n - 1, Math.floor(x * sim.n))];
  return { p10: q(0.10), p50: q(0.50), p90: q(0.90), p98: q(0.98), mean: s.reduce((a, b) => a + b, 0) / sim.n };
}

/* Hall's condition: for every subset S of slots, players eligible for any slot in S >= |S| (exact slot feasibility
   with one binary per player). Deduplicated by eligible-player set. */
export function hallConstraints(players, slots) {
  const tok = players.map(p => slots.map(([, t]) => t.some(x => p.pos.has(x))));
  const best = new Map(); const ns = slots.length;
  for (let mask = 1; mask < (1 << ns); mask++) {
    const need = mask.toString(2).split("1").length - 1;
    const elig = []; for (let k = 0; k < players.length; k++) { for (let j = 0; j < ns; j++) if ((mask >> j & 1) && tok[k][j]) { elig.push(k); break; } }
    const key = elig.join(","); if ((best.get(key)?.need || 0) < need) best.set(key, { elig, need });
  }
  return [...best.values()];
}
export function stackGroup(sport, p) {
  const r = RULES[sport];
  if (r.stack === "hitters") return p.pos.has("P") ? null : p.team;
  if (r.stack === "line") return p.pos.has("G") || !p.line ? null : `${p.team}|${p.line}`;
  if (r.stack === "game") return p.game || null;
  if (r.stack === "pass") return p.pos.has("QB") ? null : p.team;
  if (r.stack === "team") return p.team || null;
  return null;
}

/* Build the GLPK model for one lineup. pool = players with proj > 0 (and not faded); score(p) = objective. */
export function buildLP(glpk, sport, pool, score, prior, blocked, opt = {}) {
  const r = RULES[sport], slots = r.slots, ns = slots.length, CAP = 50000;
  const x = (p) => "x" + p.id;
  const vars = pool.map(p => ({ name: x(p), coef: score(p) }));
  const sum = (f, coef = 1) => pool.filter(f).map(p => ({ name: x(p), coef: typeof coef === "function" ? coef(p) : coef }));
  const st = [
    { name: "salary", vars: sum(() => true, p => p.salary), bnds: { type: glpk.GLP_DB, lb: 0, ub: CAP } },
    { name: "roster", vars: sum(() => true), bnds: { type: glpk.GLP_FX, lb: ns, ub: ns } },
  ];
  if (!slots.every(s => s[1].join() === slots[0][1].join()))
    hallConstraints(pool, slots).forEach((h, i) => { if (h.elig.length < pool.length) st.push({ name: "hall" + i, vars: h.elig.map(k => ({ name: x(pool[k]), coef: 1 })), bnds: { type: glpk.GLP_LO, lb: h.need, ub: 0 } }); });
  const teams = [...new Set(pool.map(p => p.team).filter(Boolean))];
  if (!NO_POS.has(sport)) {
    teams.forEach(t => st.push({ name: "team_" + t, vars: sum(p => p.team === t), bnds: { type: glpk.GLP_UP, lb: 0, ub: r.maxTeam } }));
    const games = [...new Set(pool.map(p => p.game).filter(Boolean))];
    if (r.minGames >= 2 && games.length >= 2) games.forEach(g => st.push({ name: "game_" + g, vars: sum(p => p.game === g), bnds: { type: glpk.GLP_UP, lb: 0, ub: ns - 1 } }));
    if (r.maxHittersTeam) teams.forEach(t => st.push({ name: "hit_" + t, vars: sum(p => p.team === t && !p.pos.has("P")), bnds: { type: glpk.GLP_UP, lb: 0, ub: r.maxHittersTeam } }));
  }
  if (r.noOpp === "pitcher") pool.filter(p => p.pos.has("P")).forEach(pt => { const hs = sum(p => p.team === pt.opp && !p.pos.has("P")); if (hs.length) { hs.push({ name: x(pt), coef: 5 }); st.push({ name: "noopp_" + pt.id, vars: hs, bnds: { type: glpk.GLP_UP, lb: 0, ub: 5 } }); } });
  if (r.noOpp === "goalie") pool.filter(p => p.pos.has("G")).forEach(g => { const sk = sum(p => p.team === g.opp && !p.pos.has("G")); if (sk.length) { sk.push({ name: x(g), coef: 8 }); st.push({ name: "noopp_" + g.id, vars: sk, bnds: { type: glpk.GLP_UP, lb: 0, ub: 8 } }); } });
  if (r.noOpp === "match") { const byName = new Map(pool.map(p => [p.name, p])); pool.forEach(p => { const o = byName.get(p.opp); if (o && o.id > p.id) st.push({ name: "match_" + p.id, vars: [{ name: x(p), coef: 1 }, { name: x(o), coef: 1 }], bnds: { type: glpk.GLP_UP, lb: 0, ub: 1 } }); }); }
  const binaries = vars.map(v => v.name);
  const stacks = opt.stacks ?? (r.stackN ? [].concat(r.stackN) : []);
  if (stacks.length && r.stack) {
    const groups = new Map(); pool.forEach(p => { const g = stackGroup(sport, p); if (g) groups.set(g, (groups.get(g) || []).concat(p)); });
    const ys = [];
    stacks.forEach((k, j) => {
      const row = [];
      for (const [g, ps] of groups) {
        if (ps.length < k) continue;
        const y = `y${j}_${ys.length}`; ys.push({ j, g, y }); binaries.push(y); vars.push({ name: y, coef: 0 });
        st.push({ name: "stk_" + y, vars: ps.map(p => ({ name: x(p), coef: 1 })).concat([{ name: y, coef: -k }]), bnds: { type: glpk.GLP_LO, lb: 0, ub: 0 } });
        row.push({ name: y, coef: 1 });
        if (r.stack === "pass") { const qbs = pool.filter(p => p.team === g && p.pos.has("QB")); if (qbs.length) st.push({ name: "qb_" + y, vars: qbs.map(p => ({ name: x(p), coef: 1 })).concat([{ name: y, coef: -1 }]), bnds: { type: glpk.GLP_LO, lb: 0, ub: 0 } }); }
        if (r.bringback && j === 0) { const opp = ps.find(p => p.opp)?.opp; const bb = pool.filter(p => p.team === opp && !p.pos.has("QB")); if (bb.length) st.push({ name: "bb_" + y, vars: bb.map(p => ({ name: x(p), coef: 1 })).concat([{ name: y, coef: -1 }]), bnds: { type: glpk.GLP_LO, lb: 0, ub: 0 } }); }
      }
      if (row.length) st.push({ name: "onestack" + j, vars: row, bnds: { type: glpk.GLP_FX, lb: 1, ub: 1 } });
    });
    for (const g of groups.keys()) { const ones = ys.filter(y => y.g === g); if (ones.length > 1) st.push({ name: "distinct_" + g, vars: ones.map(y => ({ name: y.y, coef: 1 })), bnds: { type: glpk.GLP_UP, lb: 0, ub: 1 } }); }
  }
  prior.forEach((ids, i) => st.push({ name: "uniq_" + i, vars: ids.map(id => ({ name: "x" + id, coef: 1 })), bnds: { type: glpk.GLP_UP, lb: 0, ub: ns - (opt.minUniq || 3) } }));
  pool.forEach(p => { if (blocked.has(p.id)) st.push({ name: "block_" + p.id, vars: [{ name: x(p), coef: 1 }], bnds: { type: glpk.GLP_FX, lb: 0, ub: 0 } }); });
  return { name: "lineup", objective: { direction: glpk.GLP_MAX, name: "obj", vars }, subjectTo: st, binaries };
}

/* players -> roster slots (backtracking; Hall made it feasible) */
export function assignSlots(sport, ps) {
  const slots = RULES[sport].slots, out = Array(slots.length).fill(null), used = new Set();
  const order = [...Array(slots.length).keys()].sort((a, b) => ps.filter(p => slots[a][1].some(t => p.pos.has(t))).length - ps.filter(p => slots[b][1].some(t => p.pos.has(t))).length);
  const go = (i) => { if (i === order.length) return true; const j = order[i];
    for (const p of ps) { if (used.has(p) || !slots[j][1].some(t => p.pos.has(t))) continue; used.add(p); out[j] = p; if (go(i + 1)) return true; used.delete(p); out[j] = null; }
    return false; };
  go(0);
  return slots.map(([name], j) => [name, out[j]]);
}

/* DraftKings file for a non-NFL sport: the player list (ID, Name, Roster Position, Salary, TeamAbbrev, Game Info) and,
   for an entries file, the entry rows whose slot columns match the sport's roster. */
export function parseDKSport(rows, sport) {
  const r = RULES[sport];
  let pl = null;
  for (let i = 0; i < rows.length && !pl; i++) { const c = rows[i].findIndex(x => x.trim() === "Name + ID"); if (c >= 0) pl = { row: i, hdr: rows[i].map(s => s.trim()), col: c }; }
  if (!pl) throw new Error("That doesn't look like a DraftKings file (no player list found).");
  const at = (label) => pl.hdr.indexOf(label, Math.max(0, pl.col - 2));
  const iId = at("ID"), iName = at("Name"), iRP = at("Roster Position"), iSal = at("Salary"), iTeam = at("TeamAbbrev"), iGI = at("Game Info");
  const players = [];
  for (let i = pl.row + 1; i < rows.length; i++) {
    const row = rows[i], id = (row[iId] || "").trim(); if (!/^\d+$/.test(id)) continue;
    const rp = (row[iRP] || "").trim(); if (rp === "CPT" || rp.includes("CPT")) throw new Error("That's a Showdown / Captain file. Simple mode builds Classic lineups.");
    const team = (row[iTeam] || "").trim(), gi = row[iGI] || "", m = /^(\w+)@(\w+)/.exec(gi.trim());
    const opp = m ? (team === m[1] ? m[2] : m[1]) : "";
    const pos = new Set(NO_POS.has(sport) ? ["X"] : rp.split("/").filter(Boolean));
    players.push({ id, name: (row[iName] || "").trim(), pos, posStr: NO_POS.has(sport) ? r.slots[0][0] : rp, salary: +row[iSal] || 0, team, opp, game: team && opp ? [team, opp].sort().join("@") : "", gameInfo: gi, proj: 0, q: null, line: "", order: "" });
  }
  if (!players.length) throw new Error("No players found in that file.");
  const H = rows[0].map(s => s.trim()), iE = H.findIndex(h => /^entry id$/i.test(h));
  let entries = null, slotCols = null;
  if (iE >= 0) {
    const want = r.slots.map(s => s[0]); slotCols = [];
    for (let j = iE + 1; j < H.length && slotCols.length < want.length; j++) if (H[j] === want[slotCols.length]) slotCols.push(j);
    if (slotCols.length !== want.length) throw new Error(`This entries file isn't a ${SPORT_NAMES[sport]} Classic contest (expected ${want.join(", ")} columns).`);
    const iN = H.indexOf("Contest Name"), iC = H.indexOf("Contest ID"), iF = H.indexOf("Entry Fee");
    entries = rows.slice(1).filter(x => /^\d+$/.test((x[iE] || "").trim())).map(x => ({ row: x, entryId: x[iE].trim(), contest: (x[iN] || "").trim(), contestId: (x[iC] || "").trim(), fee: (x[iF] || "").trim() }));
    if (!entries.length) throw new Error("No entries found in that file.");
  }
  return { players, entries, slotCols, H, lastCol: slotCols ? Math.max(...slotCols) : 0 };
}

/* projections CSV -> fills proj / percentiles / line / order on the DK players (matched by name) */
export function applyProjections(sport, players, rows) {
  const H = rows[0].map(s => s.trim().toLowerCase());
  const col = (...names) => { for (const n of names) { const i = H.indexOf(n); if (i >= 0) return i; } return -1; };
  const iN = col("name", "player", "player_name"), iP = col("proj", "projection", "fpts", "points", "ssproj");
  if (iN < 0 || iP < 0) throw new Error("The projections CSV needs a 'name' column and a 'proj' column.");
  const iQ = [25, 50, 75, 85, 95, 99].map(k => col("p" + k, `dk_${k}_percentile`)), iL = col("line", "lineposition"), iO = col("order", "batting_order");
  const by = new Map(); rows.slice(1).forEach(r => { if (r[iN]) by.set(normName(r[iN]), r); });
  let matched = 0;
  players.forEach(p => {
    const r = by.get(normName(p.name)); if (!r) { p.proj = 0; p.q = null; return; }
    p.proj = Math.max(0, +r[iP] || 0); matched++;
    p.q = iQ.every(i => i >= 0) ? iQ.map(i => +r[i] || 0) : null;
    if (iL >= 0) p.line = (r[iL] || "").trim(); if (iO >= 0) p.order = (r[iO] || "").trim();
    fillQuantiles(sport, p);
  });
  return matched;
}
