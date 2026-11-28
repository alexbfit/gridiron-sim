/* GameTime Win: Quick build.
   Upload a DraftKings entries file (or a salaries file), choose two players (DraftKings' user-input rule),
   build, and download an upload-ready CSV. Uses the same optimizer as the Advanced builder (lineups.js)
   with the Sunday settings fixed. */
import GLPK from "https://cdn.jsdelivr.net/npm/glpk.js@4.0.2/dist/index.js";
import * as SP from "./sports.js";

const cfg = window.GRIDIRON_CONFIG || {};
const GS = window.GS;
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const f1 = (v) => v == null || Number.isNaN(+v) ? "–" : Number(v).toFixed(1);
const money = (v) => "$" + Math.round(v).toLocaleString();
const sleep = (ms) => new Promise(r => setTimeout(r, ms));

const CAP = 50000;
const SLOTS = ["QB", "RB", "RB", "WR", "WR", "WR", "TE", "FLEX", "DST"];
const CONF = { propsBlend: 0.55, stack: 2, bringback: true, maxTE: 1, maxTeam: 4, minUniq: 3, ceilW: 0.4, rand: 0.15, poolMult: 3, maxCand: 240, like: 1.15, minEdits: 2 };
let supa = null, glpk = null;
let sport = "NFL";               // NFL = the site's own slate + sim; anything else = sports.js engine from a projections CSV
let dk = null, projFile = null;  // non-NFL: parsed DK file (players + entries) and whether projections are loaded
let spSim = null;
let file = null;                 // parsed upload
let slate = null, players = [], byId = new Map(), props = new Map(), sim = null, simPromise = null;
let likes = new Set(), fades = new Set();
let nWanted = 20;
let lineups = [];
let pickPos = "ALL";

// ============================================================ CSV
function parseCSV(text) {
  const rows = []; let row = [], cell = "", q = false;
  for (let i = 0; i < text.length; i++) {
    const c = text[i];
    if (q) {
      if (c === '"') { if (text[i + 1] === '"') { cell += '"'; i++; } else q = false; }
      else cell += c;
    } else if (c === '"') q = true;
    else if (c === ",") { row.push(cell); cell = ""; }
    else if (c === "\n" || c === "\r") { if (c === "\r" && text[i + 1] === "\n") i++; row.push(cell); rows.push(row); row = []; cell = ""; }
    else cell += c;
  }
  if (cell !== "" || row.length) { row.push(cell); rows.push(row); }
  return rows;
}
const csvCell = (v) => /[",\n]/.test(String(v)) ? '"' + String(v).replace(/"/g, '""') + '"' : String(v);

// A DraftKings entries file: entry rows on the left (Entry ID, Contest Name, Contest ID, Entry Fee, QB … DST),
// the slate's player list on the right (Position, Name + ID, Name, ID, …). A salaries file is only the player list.
function parseDK(text, name) {
  const rows = parseCSV(text.replace(/^﻿/, "")).filter(r => r.some(c => c.trim() !== ""));
  if (!rows.length) throw new Error("That file is empty.");
  const H = rows[0].map(s => s.trim());
  if (H.includes("CPT")) throw new Error("That's a Showdown file. This page builds Classic lineups.");

  let pl = null;
  for (let i = 0; i < rows.length && !pl; i++) {
    const c = rows[i].findIndex(x => x.trim() === "Name + ID");
    if (c >= 0) pl = { row: i, hdr: rows[i].map(s => s.trim()), col: c };
  }
  const ids = new Set(), rosterPos = new Map();
  if (pl) {
    const at = (label) => pl.hdr.indexOf(label, Math.max(0, pl.col - 2));
    const iId = at("ID"), iRP = at("Roster Position");
    for (let i = pl.row + 1; i < rows.length; i++) {
      const id = (rows[i][iId] || "").trim();
      if (/^\d+$/.test(id)) { ids.add(id); if (iRP >= 0) rosterPos.set(id, (rows[i][iRP] || "").trim()); }
    }
    if ([...rosterPos.values()].some(v => v === "CPT")) throw new Error("That's a Showdown file. This page builds Classic lineups.");
  }

  const iE = H.findIndex(h => /^entry id$/i.test(h));
  if (iE >= 0) {
    const slotCols = [];
    for (let j = iE + 1; j < H.length && slotCols.length < 9; j++) if (SLOTS.includes(H[j])) slotCols.push(j);
    if (slotCols.map(j => H[j]).join(",") !== SLOTS.join(",")) throw new Error("This entries file isn't an NFL Classic contest (expected QB, RB, RB, WR, WR, WR, TE, FLEX, DST columns).");
    const iName = H.indexOf("Contest Name"), iCid = H.indexOf("Contest ID"), iFee = H.indexOf("Entry Fee");
    const entries = rows.slice(1).filter(r => /^\d+$/.test((r[iE] || "").trim())).map(r => ({
      row: r, entryId: r[iE].trim(), contest: (r[iName] || "").trim(), contestId: (r[iCid] || "").trim(), fee: (r[iFee] || "").trim() }));
    if (!entries.length) throw new Error("That file has no entries in it. Enter your contests on DraftKings first, then download the file from Edit Entries.");
    const contests = new Map();
    entries.forEach(e => { const k = e.contestId || e.contest; if (!contests.has(k)) contests.set(k, { key: k, name: e.contest, fee: e.fee, n: 0 }); contests.get(k).n++; });
    return { kind: "entries", name, H, slotCols, lastCol: Math.max(...slotCols), entries, contests: [...contests.values()], ids };
  }
  if (pl && ids.size) return { kind: "salaries", name, ids };
  throw new Error("That isn't a DraftKings file. Use the CSV from My Contests, Edit Entries, Download CSV.");
}
const shortContest = (s) => (s || "Contest").replace(/^NFL\s+/i, "").replace(/\s*\[.*?\]\s*/g, " ").replace(/\s+/g, " ").trim();

// ============================================================ data
async function fetchAll(query) {
  const page = 1000; let from = 0, out = [];
  for (;;) {
    const { data, error } = await query.range(from, from + page - 1);
    if (error) throw error;
    out = out.concat(data);
    if (data.length < page) return out;
    from += page;
  }
}
// local testing hook: ?fixture=test/fixture.json loads a saved slate instead of Supabase (same-origin relative paths only)
const FIXTURE = (() => { const f = new URLSearchParams(location.search).get("fixture"); return f && !/[:\\]|^\/\//.test(f) ? f : null; })();

async function findSlate(ids) {
  if (FIXTURE) { const fx = await (await fetch(FIXTURE)).json(); window.__fx = fx; return fx.slate; }
  const sample = [...ids].slice(0, 80);
  const { data, error } = await supa.from("slate_board").select("slate_id,site_player_id").in("site_player_id", sample);
  if (error) throw error;
  const counts = new Map();
  (data || []).forEach(r => counts.set(r.slate_id, (counts.get(r.slate_id) || 0) + 1));
  const best = [...counts].sort((a, b) => b[1] - a[1])[0];
  if (!best || best[1] < Math.min(8, sample.length * 0.3)) throw new Error("That file is for a slate we don't cover yet. We cover the Sunday main slate once DraftKings posts salaries. Check that your contests are on the main slate.");
  const { data: s, error: e2 } = await supa.from("slates").select("*").eq("slate_id", best[0]).maybeSingle();
  if (e2 || !s) throw e2 || new Error("Couldn't load the slate.");
  if (s.site && s.site !== "DK") throw new Error("That file matched a non-DraftKings slate.");
  return s;
}

async function loadSlate(s) {
  slate = s;
  let rows, pr;
  if (FIXTURE) { rows = window.__fx.board; pr = window.__fx.props; }
  else {
    rows = await fetchAll(supa.from("slate_board").select("*").eq("slate_id", s.slate_id).order("site_player_id"));
    try { pr = await fetchAll(supa.from("slate_projections").select("site_player_id,mean").eq("slate_id", s.slate_id).eq("method", "props").order("site_player_id")); }
    catch (e) { pr = []; }
  }
  props = new Map(pr.map(r => [String(r.site_player_id), Number(r.mean)]));
  const OUT = new Set(["OUT", "IR", "O"]);
  players = rows.map(r => ({ ...r, site_player_id: String(r.site_player_id), mean: r.mean == null ? null : Number(r.mean), p85: Number(r.p85), stdev: Number(r.stdev), salary: Number(r.salary) }))
    .filter(p => p.mean != null && p.salary > 0 && !OUT.has(String(p.status || "").toUpperCase()) && p.injury_report !== "Out")
    .filter(p => !file.ids.size || file.ids.has(p.site_player_id));
  byId = new Map(players.map(p => [p.site_player_id, p]));
  if (!players.some(p => p.position === "QB") || !players.some(p => p.position === "DST")) throw new Error("Projections for this slate aren't ready yet. Try again later today.");
  simPromise = loadSim().catch(e => { console.warn("sim unavailable", e); sim = null; });
}

async function loadSim() {
  if (FIXTURE) { const d = window.__fx.sim; sim = { n: d.n, index: new Map(d.players.map((id, i) => [String(id), Float32Array.from(d.scores[i])])) }; return; }
  const url = slate.sim_meta?.url;
  if (!url) return;
  const res = await fetch(url, { cache: "no-cache" });
  if (!res.ok) throw new Error("sim fetch " + res.status);
  const text = typeof DecompressionStream === "function" && !res.headers.get("content-encoding")
    ? await new Response(res.body.pipeThrough(new DecompressionStream("gzip"))).text() : await res.text();
  const data = JSON.parse(text);
  sim = { n: data.n, index: new Map(data.players.map((id, i) => [String(id), Float32Array.from(data.scores[i])])) };
}

// ============================================================ projections
// the projection shown to the member (no boost for the players they chose)
function baseProj(p) {
  let m = p.mean ?? 0;
  const pr = props.get(p.site_player_id);
  if (pr != null && p.mean != null) m = (1 - CONF.propsBlend) * m + CONF.propsBlend * pr;
  return m;
}
// the projection the builder uses: "more of him" players get a boost
function proj(p) { const m = baseProj(p); return likes.has(p.site_player_id) ? m * CONF.like : m; }
const simScale = (p) => (p.mean == null || p.mean <= 0.5) ? 1 : Math.min(4, Math.max(0.25, proj(p) / p.mean));
function lineupSim(ids) {
  if (!sim) return null;
  const tot = new Float32Array(sim.n);
  ids.forEach(id => { const a = sim.index.get(id), p = byId.get(id);
    if (a) { const f = simScale(p); for (let i = 0; i < sim.n; i++) tot[i] += a[i] * f; }
    else { const m = proj(p); for (let i = 0; i < sim.n; i++) tot[i] += m; } });
  const s = Float32Array.from(tot).sort(), q = (x) => s[Math.min(sim.n - 1, Math.floor(x * sim.n))];
  return { p10: q(0.10), p50: q(0.50), p90: q(0.90) };
}

// ============================================================ optimizer (same model as lineups.js buildLP)
function buildLP(pool, score, prior, blocked) {
  const x = (p) => "x" + p.site_player_id;
  const vars = pool.map(p => ({ name: x(p), coef: score(p) }));
  const sum = (f, coef = 1) => pool.filter(f).map(p => ({ name: x(p), coef: typeof coef === "function" ? coef(p) : coef }));
  const st = [
    { name: "salary", vars: sum(() => true, p => p.salary), bnds: { type: glpk.GLP_DB, lb: 0, ub: CAP } },
    { name: "roster", vars: sum(() => true), bnds: { type: glpk.GLP_FX, lb: 9, ub: 9 } },
    { name: "qb", vars: sum(p => p.position === "QB"), bnds: { type: glpk.GLP_FX, lb: 1, ub: 1 } },
    { name: "dst", vars: sum(p => p.position === "DST"), bnds: { type: glpk.GLP_FX, lb: 1, ub: 1 } },
    { name: "rb", vars: sum(p => p.position === "RB"), bnds: { type: glpk.GLP_DB, lb: 2, ub: 3 } },
    { name: "wr", vars: sum(p => p.position === "WR"), bnds: { type: glpk.GLP_DB, lb: 3, ub: 4 } },
    { name: "te", vars: sum(p => p.position === "TE"), bnds: CONF.maxTE <= 1 ? { type: glpk.GLP_FX, lb: 1, ub: 1 } : { type: glpk.GLP_DB, lb: 1, ub: 2 } },
    { name: "flex", vars: sum(p => ["RB", "WR", "TE"].includes(p.position)), bnds: { type: glpk.GLP_FX, lb: 7, ub: 7 } },
  ];
  [...new Set(pool.map(p => p.team))].forEach(t => st.push({ name: "team_" + t, vars: sum(p => p.team === t), bnds: { type: glpk.GLP_UP, lb: 0, ub: CONF.maxTeam } }));
  [...new Set(pool.map(p => p.game_id).filter(Boolean))].forEach(g => st.push({ name: "game_" + g, vars: sum(p => p.game_id === g), bnds: { type: glpk.GLP_UP, lb: 0, ub: 8 } }));
  pool.filter(p => p.position === "QB").forEach(qb => {
    const s = sum(p => p.team === qb.team && ["WR", "TE"].includes(p.position)); s.push({ name: x(qb), coef: -CONF.stack });
    st.push({ name: "stack_" + qb.site_player_id, vars: s, bnds: { type: glpk.GLP_LO, lb: 0, ub: 0 } });
    if (CONF.bringback) {
      const b = sum(p => p.team === qb.opponent && ["WR", "TE", "RB"].includes(p.position)); b.push({ name: x(qb), coef: -1 });
      st.push({ name: "bb_" + qb.site_player_id, vars: b, bnds: { type: glpk.GLP_LO, lb: 0, ub: 0 } });
    }
  });
  prior.forEach((ids, i) => st.push({ name: "uniq_" + i, vars: ids.map(id => ({ name: "x" + id, coef: 1 })), bnds: { type: glpk.GLP_UP, lb: 0, ub: 9 - CONF.minUniq } }));
  pool.forEach(p => { if (blocked.has(p.site_player_id)) st.push({ name: "block_" + p.site_player_id, vars: [{ name: x(p), coef: 1 }], bnds: { type: glpk.GLP_FX, lb: 0, ub: 0 } }); });
  return { name: "lineup", objective: { direction: glpk.GLP_MAX, name: "obj", vars }, subjectTo: st, binaries: vars.map(v => v.name) };
}
function gauss() { let u = 0, v = 0; while (!u) u = Math.random(); while (!v) v = Math.random(); return Math.sqrt(-2 * Math.log(u)) * Math.cos(2 * Math.PI * v); }

async function generateSport(n, onProgress) {
  const R = SP.RULES[sport], ns = R.slots.length;
  const maxExp = n <= 3 ? 1 : 0.5;
  const pool = players.filter(p => !fades.has(p.site_player_id) && proj(p) > 0);
  onProgress(0, 1, "sim");
  spSim = SP.simulate(sport, pool.map(p => ({ ...p, proj: proj(p) })), 3000);
  const nCand = Math.min(CONF.maxCand, Math.max(n + 6, n * 3)), cand = [], seen = new Set();
  const W = [0, 0.35, 0.7, 1.0];
  for (let k = 0; k < nCand; k++) {
    onProgress(k, nCand);
    const w = W[k % W.length];
    const score = (p) => { const m = proj(p); return (m + w * (Math.max(p.p85, m) - m)) * (1 + CONF.rand * 0.5 * gauss()); };
    const res = await glpk.solve(SP.buildLP(glpk, sport, pool, score, cand.map(L => L.ids), new Set(), { minUniq: CONF.minUniq }), { msglev: glpk.GLP_MSG_OFF, tmlim: 20 });
    if (![glpk.GLP_OPT, glpk.GLP_FEAS].includes(res.result.status)) break;
    const ids = Object.entries(res.result.vars).filter(([nm, v]) => nm[0] === "x" && v > 0.5).map(([nm]) => nm.slice(1)).sort();
    const key = ids.join("|"); if (seen.has(key)) continue; seen.add(key);
    cand.push({ ids });
    await sleep(0);
  }
  onProgress(nCand, nCand, "rank");
  cand.forEach(L => { const q = SP.lineupQuantiles(spSim, L.ids); L.sim = q; L.proj = q.mean; L.key = q.p90; });
  cand.sort((a, b) => b.key - a.key);
  const capN = Math.max(1, Math.ceil(maxExp * n)), used = new Map(), kept = [];
  for (const L of cand) {
    if (kept.length >= n) break;
    if (L.ids.every(id => (used.get(id) || 0) < capN) && kept.every(o => o.ids.filter(id => L.ids.includes(id)).length <= ns - CONF.minUniq)) { kept.push(L); L.ids.forEach(id => used.set(id, (used.get(id) || 0) + 1)); }
  }
  for (const L of cand) { if (kept.length >= n) break; if (!kept.includes(L)) kept.push(L); }
  kept.sort((a, b) => b.key - a.key);
  return kept.map((L, i) => finishLineup(L, i, kept));
}

async function generate(n, onProgress) {
  if (sport !== "NFL") return generateSport(n, onProgress);
  const maxExp = n <= 3 ? 1 : n <= 20 ? 0.4 : 0.35;
  const pool = players.filter(p => !fades.has(p.site_player_id) && proj(p) > 0);
  const nCand = Math.min(CONF.maxCand, Math.max(n + 10, n * CONF.poolMult));
  const cand = [], usage = new Map(), blocked = new Set();
  const capC = Math.max(1, Math.ceil(maxExp * nCand));
  for (let k = 0; k < nCand; k++) {
    onProgress(k, nCand);
    const jit = new Map(pool.map(p => [p.site_player_id, 1 + CONF.rand * gauss() * (p.stdev / Math.max(p.mean, 1))]));
    const score = (p) => { const m = proj(p); return ((1 - CONF.ceilW) * m + CONF.ceilW * p.p85 * (m / Math.max(p.mean, 0.1))) * jit.get(p.site_player_id); };
    const res = await glpk.solve(buildLP(pool, score, cand.map(L => L.ids), blocked), { msglev: glpk.GLP_MSG_OFF, tmlim: 20 });
    if (![glpk.GLP_OPT, glpk.GLP_FEAS].includes(res.result.status)) break;
    const ids = Object.entries(res.result.vars).filter(([, v]) => v > 0.5).map(([nm]) => nm.slice(1));
    cand.push({ ids });
    ids.forEach(id => { const u = (usage.get(id) || 0) + 1; usage.set(id, u); if (u >= capC) blocked.add(id); });
    await sleep(0);
  }
  onProgress(nCand, nCand, "rank");
  await simPromise;
  cand.forEach(L => { L.sim = lineupSim(L.ids); L.proj = L.ids.reduce((s, id) => s + proj(byId.get(id)), 0); L.key = L.sim ? L.sim.p90 : L.proj; });
  cand.sort((a, b) => b.key - a.key);
  // pick the final set best-first under the exposure cap; fill from the rest if the cap leaves it short
  const capN = Math.max(1, Math.ceil(maxExp * n)), used = new Map(), kept = [];
  for (const L of cand) {
    if (kept.length >= n) break;
    if (L.ids.every(id => (used.get(id) || 0) < capN)) { kept.push(L); L.ids.forEach(id => used.set(id, (used.get(id) || 0) + 1)); }
  }
  for (const L of cand) { if (kept.length >= n) break; if (!kept.includes(L)) kept.push(L); }
  kept.sort((a, b) => b.key - a.key);
  return kept.map((L, i) => finishLineup(L, i, kept));
}

function kickoffKey(p) {
  const m = /(\d{2})\/(\d{2})\/(\d{4}) (\d{1,2}):(\d{2})([AP])M/.exec(p.game_info || "");
  if (!m) return 0;
  const h = (+m[4] % 12) + (m[6] === "P" ? 12 : 0);
  return +(m[3] + m[1] + m[2]) * 10000 + h * 100 + +m[5];
}
function assignSlots(ps) {
  const by = (pos) => ps.filter(p => p.position === pos).sort((a, b) => b.salary - a.salary);
  const qb = by("QB")[0], dst = by("DST")[0], te = by("TE"), rb = by("RB"), wr = by("WR");
  const flexPos = rb.length > 2 ? "RB" : wr.length > 3 ? "WR" : "TE";
  // the latest-kickoff player of the FLEX position goes to FLEX, so late swap keeps the most options
  const grp = { RB: rb, WR: wr, TE: te }[flexPos];
  const flex = [...grp].sort((a, b) => kickoffKey(b) - kickoffKey(a))[0];
  const rest = (arr) => arr.filter(p => p !== flex);
  const r = rest(rb), w = rest(wr), t = rest(te);
  return [["QB", qb], ["RB", r[0]], ["RB", r[1]], ["WR", w[0]], ["WR", w[1]], ["WR", w[2]], ["TE", t[0]], ["FLEX", flex], ["DST", dst]];
}
function finishLineup(L, i, all) {
  const ps = L.ids.map(id => byId.get(id));
  if (sport !== "NFL") {
    // headline = the biggest stack (or the top projected player when the sport has no teams)
    const byTeam = new Map(); ps.forEach(p => { if (p.team) byTeam.set(p.team, (byTeam.get(p.team) || []).concat(p)); });
    const big = [...byTeam.values()].sort((a, b) => b.length - a.length)[0] || [];
    const lead = (big.length > 1 ? big : ps.slice().sort((a, b) => proj(b) - proj(a))).slice().sort((a, b) => b.salary - a.salary);
    const qb = lead[0], mates = lead.slice(1, 4).map(p => lastName(p.player_name));
    const slots = SP.assignSlots(sport, ps).map(([slot, p]) => [slot, byId.get(p.id)]);
    return { ...L, rank: i + 1, qb, mates, headline: big.length > 1 ? `${big[0].team} ×${big.length}` : "", slots, salary: ps.reduce((s, p) => s + p.salary, 0), uses: [] };
  }
  const qb = ps.find(p => p.position === "QB");
  const mates = ps.filter(p => p.team === qb.team && p !== qb && p.position !== "DST").map(p => lastName(p.player_name));
  return { ...L, rank: i + 1, qb, mates, slots: assignSlots(ps), salary: ps.reduce((s, p) => s + p.salary, 0), uses: [] };
}
const lastName = (n) => { const parts = String(n).split(" ").filter(w => !/^(jr\.?|sr\.?|ii|iii|iv|v)$/i.test(w)); return parts[parts.length - 1] || n; };

// ============================================================ entries → lineups
function assignEntries() {
  lineups.forEach(L => (L.uses = []));
  if (file.kind !== "entries") return;
  const seen = new Map();
  file.entries.forEach(e => {
    const k = e.contestId || e.contest, i = seen.get(k) || 0; seen.set(k, i + 1);
    const L = lineups[i % lineups.length]; e.lineup = L;
    const u = L.uses.find(x => x.k === k); if (u) u.n++; else L.uses.push({ k, name: shortContest(e.contest), n: 1 });
  });
}
function downloadCSV() {
  let text, fname;
  if (file.kind === "entries") {
    const cols = file.lastCol + 1;
    const head = file.H.slice(0, cols);
    const body = file.entries.map(e => { const r = e.row.slice(0, cols); while (r.length < cols) r.push("");
      file.slotCols.forEach((c, j) => { r[c] = e.lineup.slots[j][1].site_player_id; }); return r; });
    text = [head, ...body].map(r => r.map(csvCell).join(",")).join("\r\n") + "\r\n";
    fname = "DKEntries_GameTimeWin_" + (slate.slate_key || "week" + slate.week) + ".csv";
  } else {
    const slots = sport === "NFL" ? SLOTS : SP.RULES[sport].slots.map(s => s[0]);
    text = [slots, ...lineups.map(L => L.slots.map(([, p]) => p.site_player_id))].map(r => r.join(",")).join("\r\n") + "\r\n";
    fname = "GameTimeWin_" + (slate.slate_key || "week" + slate.week) + "_" + lineups.length + ".csv";
  }
  const a = document.createElement("a");
  a.href = URL.createObjectURL(new Blob([text], { type: "text/csv" })); a.download = fname; a.click();
  setTimeout(() => URL.revokeObjectURL(a.href), 1500);
  GS.toast(file.kind === "entries" ? "Downloaded. Upload it on DraftKings under Edit Entries." : "Downloaded. Upload it on DraftKings under Upload Lineups.", "ok");
}

// ============================================================ UI: steps
function step(name) {
  const order = ["upload", "pick", "reveal"], at = name === "sport" ? 0 : order.indexOf(name);
  document.querySelectorAll(".sm-steps li").forEach(li => { const i = order.indexOf(li.dataset.step);
    li.removeAttribute("aria-current"); if (i === at) li.setAttribute("aria-current", "step"); li.classList.toggle("done", i < at); });
  ["Sport", "Upload", "Pick", "Reveal"].forEach(k => { $("scr" + k).hidden = k.toLowerCase() !== name; });
  window.scrollTo({ top: 0, behavior: "auto" });
}

// ============================================================ UI: upload
async function handleFile(f) {
  if (!f) return;
  const err = $("uploadError"); err.hidden = true;
  const drop = $("drop"); drop.classList.add("busy"); $("drop").querySelector(".drop-title").textContent = "Reading your file";
  try {
    if (f.size > 20e6) throw new Error("That file is too large to be a DraftKings CSV.");
    if (sport !== "NFL") { await handleSportFile(f); return; }
    file = parseDK(await f.text(), f.name);
    drop.querySelector(".drop-title").textContent = "Finding your contests";
    if (!supa && !FIXTURE) throw new Error("We couldn't reach our data. Check your connection and try again.");
    const s = await findSlate(file.ids);
    await loadSlate(s);
    likes = new Set(); fades = new Set();
    nWanted = clampN(file.kind === "entries" ? Math.max(...file.contests.map(c => c.n)) : 20);
    renderPick(); step("pick");
  } catch (e) {
    console.error(e); err.textContent = e.message || String(e); err.hidden = false;
  } finally {
    drop.classList.remove("busy"); if (sport === "NFL") drop.querySelector(".drop-title").textContent = "Drop your DKEntries.csv here";
    $("file").value = "";
  }
}

// non-NFL: the DK file gives the player list, the projections CSV the numbers; build locally with sports.js
async function handleSportFile(f) {
  const rows = parseCSV((await f.text()).replace(/^\ufeff/, "")).filter(r => r.some(c => c.trim() !== ""));
  if (!rows.length) throw new Error("That file is empty.");
  dk = SP.parseDKSport(rows, sport);
  projFile = false;
  file = { kind: dk.entries ? "entries" : "salaries", name: f.name, ids: new Set(dk.players.map(p => p.id)), entries: dk.entries || [], H: dk.H, slotCols: dk.slotCols, lastCol: dk.lastCol, contests: [] };
  if (dk.entries) { const c = new Map(); dk.entries.forEach(e => { const k = e.contestId || e.contest; if (!c.has(k)) c.set(k, { key: k, name: e.contest, fee: e.fee, n: 0 }); c.get(k).n++; }); file.contests = [...c.values()]; }
  const dz = $("drop"); dz.classList.add("ok"); dz.querySelector(".drop-title").textContent = `${SP.SPORT_NAMES[sport]} · ${dk.players.length} players${dk.entries ? ` · ${dk.entries.length} entries` : ""} loaded`;
  $("dropProj").hidden = false; $("dropProj").focus();
}
async function handleProjFile(f) {
  if (!f || !dk) return;
  const err = $("uploadError"); err.hidden = true;
  try {
    const rows = parseCSV((await f.text()).replace(/^\ufeff/, "")).filter(r => r.some(c => c.trim() !== ""));
    const n = SP.applyProjections(sport, dk.players, rows);
    if (n < SP.RULES[sport].slots.length * 2) throw new Error(`Only ${n} of the ${dk.players.length} DraftKings players matched a name in that projections file.`);
    players = dk.players.filter(p => p.proj > 0).map(p => ({ ...p, site_player_id: p.id, player_name: p.name, position: p.posStr.split("/")[0], mean: p.proj, p85: p.q[3], stdev: Math.max(0.1, (p.q[2] - p.q[0]) / 1.35), game_id: p.game, opponent: p.opp }));
    byId = new Map(players.map(p => [p.site_player_id, p]));
    props = new Map(); sim = null; simPromise = Promise.resolve();
    slate = { week: "", slate_key: `${sport}_${new Date().toISOString().slice(0, 10)}`, label: SP.SPORT_NAMES[sport] };
    projFile = true;
    $("dropProj").classList.add("ok"); $("dropProj").querySelector(".drop-title").textContent = `${n} players matched`;
    likes = new Set(); fades = new Set();
    nWanted = clampN(file.kind === "entries" ? Math.max(...file.contests.map(c => c.n)) : 20);
    renderPick(); step("pick");
  } catch (e) { console.error(e); err.textContent = e.message || String(e); err.hidden = false; }
  finally { $("fileProj").value = ""; }
}
function resetUpload() {
  dk = null; projFile = false; spSim = null;
  const dz = $("drop"); dz.classList.remove("ok"); dz.querySelector(".drop-title").textContent = "Drop your DKEntries.csv here";
  const pz = $("dropProj"); pz.hidden = true; pz.classList.remove("ok"); pz.querySelector(".drop-title").textContent = "Now drop your projections file";
  $("uploadError").hidden = true;
}

// ============================================================ UI: pick
function posTabs() {
  const seg = $("posSeg");
  const list = sport === "NFL" ? ["QB", "RB", "WR", "TE", "DST"] : SP.RULES[sport].positions;
  seg.innerHTML = `<button type="button" data-pos="ALL" aria-pressed="true">Top players</button>` + list.map(p => `<button type="button" data-pos="${p}" aria-pressed="false">${p}</button>`).join("");
  pickPos = "ALL"; seg.hidden = !list.length;
}
function renderPick() {
  posTabs();
  const games = new Set(players.map(p => p.game_id).filter(Boolean)).size;
  $("pickKicker").textContent = sport === "NFL" ? `Week ${slate.week}, ${games} games` : `${SP.SPORT_NAMES[sport]}, ${games ? games + " games, " : ""}${players.length} players`;
  if (file.kind === "entries") {
    const totalE = file.entries.length;
    $("entriesSum").innerHTML = `<p><b>${totalE} ${totalE === 1 ? "entry" : "entries"}</b> in ${file.contests.length} ${file.contests.length === 1 ? "contest" : "contests"}. We'll build <b>${nWanted} ${nWanted === 1 ? "lineup" : "lineups"}</b>${file.contests.length > 1 ? " and put the best ones in every contest" : ""}.</p>`
      + `<ul>${file.contests.map(c => `<li>${esc(shortContest(c.name))}<span>${c.n} ${c.n === 1 ? "entry" : "entries"}</span></li>`).join("")}</ul>`;
    $("countRow").hidden = true;
  } else {
    $("entriesSum").innerHTML = `<p>This is a salaries file, so there are no entries to fill. Choose how many lineups you want below the player list.</p>`;
    $("countRow").hidden = false;
    pressed("nSeg", "n", String(nWanted));
  }
  renderGrid(); renderDock();
}
const clampN = (n) => Math.max(1, n);
function pressed(groupId, attr, val) { $(groupId).querySelectorAll("button").forEach(b => b.setAttribute("aria-pressed", String(b.dataset[attr] === val))); }
function pickList() {
  const q = $("pickSearch").value.trim().toLowerCase();
  const byProj = (a, b) => baseProj(b) - baseProj(a);
  if (q) return players.filter(p => p.player_name.toLowerCase().includes(q) || String(p.team).toLowerCase() === q).sort(byProj).slice(0, 36);
  const hasPos = (p, pos) => sport === "NFL" ? p.position === pos : p.pos.has(pos);
  if (pickPos !== "ALL") return players.filter(p => hasPos(p, pickPos)).sort(byProj).slice(0, 24);
  if (sport !== "NFL") {
    const list = SP.RULES[sport].positions;
    if (!list.length) return players.slice().sort(byProj).slice(0, 24);
    const seen = new Set(), out = [];
    list.forEach(pos => players.filter(p => hasPos(p, pos)).sort(byProj).slice(0, Math.ceil(24 / list.length)).forEach(p => { if (!seen.has(p.site_player_id)) { seen.add(p.site_player_id); out.push(p); } }));
    return out;
  }
  const take = { QB: 4, RB: 6, WR: 8, TE: 3, DST: 3 };
  return Object.entries(take).flatMap(([pos, k]) => players.filter(p => p.position === pos).sort(byProj).slice(0, k));
}
function renderGrid() {
  const list = pickList();
  $("pickGrid").innerHTML = list.length ? list.map(p => {
    const id = p.site_player_id, st = likes.has(id) ? "like" : fades.has(id) ? "fade" : "";
    const init = p.position === "DST" ? p.team : p.player_name.split(" ").map(w => w[0]).join("").slice(0, 2);
    const pc = sport === "NFL" ? `var(--pos-${p.position === "DST" ? "dst" : p.position.toLowerCase()})` : "var(--accent)";
    return `<button type="button" class="pk ${st}" data-id="${esc(id)}" style="--pc:${pc}" aria-pressed="${st ? "true" : "false"}" aria-label="${esc(p.player_name)}, ${esc(p.position)}, ${st === "like" ? "more of him" : st === "fade" ? "none of him" : "not chosen"}">
      <span class="av">${esc(init)}</span><span class="nm">${esc(p.player_name)}</span>
      <span class="pj">${f1(baseProj(p))}<small>pts</small></span>
      <span class="mt">${esc(sport === "NFL" ? p.position : p.posStr)}${p.team ? " " + esc(p.team) : ""}, $${(p.salary / 1000).toFixed(1)}K</span>
      <span class="st">${st === "like" ? "More" : st === "fade" ? "None" : ""}</span></button>`;
  }).join("") : `<div class="pick-empty">No players match that search.</div>`;
}
function cyclePick(id) {
  if (likes.has(id)) { likes.delete(id); fades.add(id); }
  else if (fades.has(id)) fades.delete(id);
  else likes.add(id);
  renderGrid(); renderDock();
  const el = $("pickGrid").querySelector(`[data-id="${CSS.escape(id)}"]`); el?.focus({ preventScroll: true });
}
function renderDock() {
  const picks = [...likes].map(id => ["like", id]).concat([...fades].map(id => ["fade", id]));
  $("dockPicks").innerHTML = picks.length ? picks.map(([k, id]) => `<span class="dp ${k}">${k === "like" ? "More" : "None"}: ${esc(byId.get(id)?.player_name || id)}</span>`).join("") : `<span class="muted">No players chosen yet</span>`;
  const left = CONF.minEdits - picks.length;
  $("buildBtn").disabled = left > 0;
  $("buildLabel").textContent = left > 0 ? `Choose ${left} more player${left > 1 ? "s" : ""}` : `Build ${nWanted} lineup${nWanted > 1 ? "s" : ""}`;
}

// ============================================================ UI: the build screen (assets/simrun.js)
function buildScreen() {
  const games = new Map();
  players.forEach(p => { const m = /^([A-Z]+)@([A-Z]+)/.exec(p.game_info || ""); if (m && p.game_id && !games.has(p.game_id)) games.set(p.game_id, { away: m[1], home: m[2] }); });
  const take = sport === "NFL" ? { QB: 2, RB: 2, WR: 3, TE: 1 } : null;
  const top = (take ? Object.entries(take).flatMap(([pos, k]) => players.filter(p => p.position === pos && !fades.has(p.site_player_id)).sort((a, b) => baseProj(b) - baseProj(a)).slice(0, k))
    : players.filter(p => !fades.has(p.site_player_id)).sort((a, b) => baseProj(b) - baseProj(a)).slice(0, 8)).map(p => {
    const d = sim?.index.get(p.site_player_id); let best = p.p85;
    if (d) { const srt = Float32Array.from(d).sort(); best = srt[Math.floor(0.99 * (srt.length - 1))] * simScale(p); }   // his 1-in-100 day
    return { name: p.player_name, pos: sport === "NFL" ? p.position : String(p.position || "").slice(0, 3), team: p.team, best, draws: d || null, scale: simScale(p) };
  });
  return GS.SimRun.open({ label: sport === "NFL" ? `Week ${slate.week}` : SP.SPORT_NAMES[sport], nSims: slate.sim_meta?.n_sims || 10000, nLineups: nWanted, games: [...games.values()], players: top, canSkip: true });
}

async function build() {
  if (likes.size + fades.size < CONF.minEdits) return;
  await simPromise;
  const run = buildScreen();
  try {
    lineups = await generate(nWanted, (k, total, phase) => {
      if (phase === "rank") run.progress(0.97, "Scoring every combination across the simulated games.");
      else run.progress(0.95 * k / total, `Testing combination ${k + 1} of ${total} against every simulated game.`);
    });
    if (!lineups.length) throw new Error("No lineup fits those choices. Remove a few of your \"none\" players and try again.");
    assignEntries();
    run.progress(1); await run.finish();
    showBinder();
  } catch (e) {
    run.close(); console.error(e);
    GS.toast(e.message || "The build didn't finish. Try again.", "error");
    step("pick");
  }
}

// ============================================================ UI: results
function showBinder() {
  step("reveal");
  const avgSal = lineups.reduce((s, L) => s + L.salary, 0) / lineups.length;
  $("binderSub").textContent = `${lineups.length} lineup${lineups.length > 1 ? "s" : ""} for ${sport === "NFL" ? "Week " + slate.week : SP.SPORT_NAMES[sport]}, best first. Average salary ${money(avgSal)}.`;
  $("uploadNote").innerHTML = GS.icon("info") + (file.kind === "entries"
    ? `<div><b>How to upload.</b> On DraftKings go to My Contests, Upcoming, Edit Entries, choose Upload CSV and pick the file you download here. It fills all ${file.entries.length} of your entries${file.contests.length > 1 ? ", with your best lineups in every contest" : ""}. Lineups already in those entries are replaced.</div>`
    : `<div><b>How to upload.</b> Open your contest on DraftKings, choose Upload Lineups and pick the file you download here. Each row is one lineup, best first.</div>`);
  renderCards();
}
function renderCards() { $("cardGrid").innerHTML = lineups.map(L => cardHTML(L)).join(""); }

function cardHTML(L) {
  const rows = L.slots.map(([slot, p]) => `<li class="${likes.has(p.site_player_id) ? "liked" : ""}"><span class="pos ${sport === "NFL" ? (p.position === "DST" ? "DST" : p.position) : "other"}">${slot}</span>
    <span class="nm">${esc(p.player_name)}<small>${esc(p.team)}</small></span><span class="sal">${money(p.salary)}</span></li>`).join("");
  const uses = L.uses.length ? `<div class="lc-uses">Goes into ${L.uses.map(u => `${esc(u.name)}${u.n > 1 ? " (" + u.n + " entries)" : ""}`).join(", ")}</div>` : "";
  const stack = `${lastName(L.qb.player_name)}${L.mates.length ? " + " + L.mates.join(", ") : ""}${L.headline ? ", " + L.headline : ""}`;
  return `<article class="lcard" aria-label="Lineup ${L.rank}">
      <div class="lc-head"><div class="lc-rank">${L.rank}</div><b title="${esc(stack)}">${esc(stack)}</b></div>
      <ul class="lc-list">${rows}</ul>
      <div class="lc-stats">
        <div><b>${f1(L.sim ? L.sim.p90 : L.proj)}</b><span>${L.sim ? "great day" : "projected"}</span></div>
        <div><b>${f1(L.sim ? L.sim.p50 : L.proj)}</b><span>${L.sim ? "typical day" : "points"}</span></div>
        <div><b>${money(L.salary)}</b><span>salary</span></div>
      </div>${uses}</article>`;
}

// ============================================================ wiring
function wire() {
  const drop = $("drop"), input = $("file");
  drop.addEventListener("click", () => input.click());
  drop.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); input.click(); } });
  input.addEventListener("change", () => handleFile(input.files[0]));
  ["dragenter", "dragover"].forEach(ev => drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.add("over"); }));
  ["dragleave", "drop"].forEach(ev => drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.remove("over"); }));
  drop.addEventListener("drop", (e) => handleFile(e.dataTransfer.files[0]));
  window.addEventListener("dragover", (e) => e.preventDefault());
  window.addEventListener("drop", (e) => { e.preventDefault(); if (!$("scrUpload").hidden) (dk && !projFile ? handleProjFile : handleFile)(e.dataTransfer.files[0]); });

  $("pickGrid").addEventListener("click", (e) => { const b = e.target.closest(".pk"); if (b) cyclePick(b.dataset.id); });
  $("posSeg").addEventListener("click", (e) => { const b = e.target.closest("button"); if (!b) return; pickPos = b.dataset.pos; pressed("posSeg", "pos", pickPos); $("pickSearch").value = ""; renderGrid(); });
  $("pickSearch").addEventListener("input", renderGrid);
  $("nSeg").addEventListener("click", (e) => { const b = e.target.closest("button"); if (!b) return; nWanted = +b.dataset.n; pressed("nSeg", "n", b.dataset.n); renderDock(); });
  $("buildBtn").addEventListener("click", build);
  $("sportGrid").addEventListener("click", (e) => { const b = e.target.closest("button"); if (!b) return; sport = b.dataset.sport; pressed("sportGrid", "sport", sport);
    resetUpload(); $("uploadTitle").textContent = sport === "NFL" ? "Upload your DraftKings entries file" : `Upload your DraftKings ${SP.SPORT_NAMES[sport]} file`; step("upload"); });
  $("otherSport").addEventListener("click", (e) => { e.preventDefault(); step("sport"); });
  const dropP = $("dropProj"), inputP = $("fileProj");
  dropP.addEventListener("click", () => inputP.click());
  dropP.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); inputP.click(); } });
  inputP.addEventListener("change", () => handleProjFile(inputP.files[0]));
  ["dragenter", "dragover"].forEach(ev => dropP.addEventListener(ev, (e) => { e.preventDefault(); e.stopPropagation(); dropP.classList.add("over"); }));
  ["dragleave", "drop"].forEach(ev => dropP.addEventListener(ev, (e) => { e.preventDefault(); e.stopPropagation(); dropP.classList.remove("over"); }));
  dropP.addEventListener("drop", (e) => handleProjFile(e.dataTransfer.files[0]));

  $("dlBtn").addEventListener("click", downloadCSV);
  $("againBtn").addEventListener("click", () => { lineups = []; file = null; sport = "NFL"; $("uploadTitle").textContent = "Upload your DraftKings entries file"; resetUpload(); step("upload"); });
}

async function init() {
  wire();
  if (!FIXTURE && cfg.SUPABASE_URL && !cfg.SUPABASE_URL.includes("YOUR-PROJECT")) supa = window.supabase.createClient(cfg.SUPABASE_URL, cfg.SUPABASE_ANON_KEY);
  try { glpk = await GLPK(); }
  catch (e) { console.error(e); const err = $("uploadError"); err.textContent = "The lineup builder didn't load. Check your connection and reload the page."; err.hidden = false; }
}
init();
