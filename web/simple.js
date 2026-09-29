/* GameTime Win — Simple mode.
   Upload a DraftKings entries file (or a salaries file) → pick two players (DraftKings' user-input rule) →
   build with the backtested Sunday settings → reveal the lineups as a card pack → download an upload-ready CSV.
   The optimizer is the same GLPK model as the full Lineup Builder (lineups.js), fixed to the settings that won the
   2024–25 contest backtests: props blend 55%, QB + 2 pass catchers + bring-back, no TE in FLEX, max exposure 35%,
   3 unique players between lineups, candidates ranked by simulated 90th-percentile score. */
import GLPK from "https://cdn.jsdelivr.net/npm/glpk.js@4.0.2/dist/index.js";

const cfg = window.GRIDIRON_CONFIG || {};
const GS = window.GS;
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const f1 = (v) => v == null || Number.isNaN(+v) ? "–" : Number(v).toFixed(1);
const money = (v) => "$" + Math.round(v).toLocaleString();
const sleep = (ms) => new Promise(r => setTimeout(r, ms));
const REDUCED = matchMedia("(prefers-reduced-motion: reduce)").matches;

const CAP = 50000;
const SLOTS = ["QB", "RB", "RB", "WR", "WR", "WR", "TE", "FLEX", "DST"];
const CONF = { propsBlend: 0.55, stack: 2, bringback: true, maxTE: 1, maxTeam: 4, minUniq: 3, ceilW: 0.4, rand: 0.15, poolMult: 3, maxCand: 240, like: 1.15, minEdits: 2 };
const TIERS = [
  { t: "S", name: "Legendary", upto: 0.10 },
  { t: "A", name: "Epic", upto: 0.35 },
  { t: "B", name: "Rare", upto: 0.70 },
  { t: "C", name: "Common", upto: 1.01 },
];
const tierInfo = (t) => TIERS.find(x => x.t === t);

let supa = null, glpk = null;
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
  if (H.includes("CPT")) throw new Error("That's a Showdown file. Simple mode builds NFL Classic lineups (QB, RB, RB, WR, WR, WR, TE, FLEX, DST).");

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
    if ([...rosterPos.values()].some(v => v === "CPT")) throw new Error("That's a Showdown file. Simple mode builds NFL Classic lineups.");
  }

  const iE = H.findIndex(h => /^entry id$/i.test(h));
  if (iE >= 0) {
    const slotCols = [];
    for (let j = iE + 1; j < H.length && slotCols.length < 9; j++) if (SLOTS.includes(H[j])) slotCols.push(j);
    if (slotCols.map(j => H[j]).join(",") !== SLOTS.join(",")) throw new Error("This entries file isn't an NFL Classic contest (expected QB, RB, RB, WR, WR, WR, TE, FLEX, DST columns).");
    const iName = H.indexOf("Contest Name"), iCid = H.indexOf("Contest ID"), iFee = H.indexOf("Entry Fee");
    const entries = rows.slice(1).filter(r => /^\d+$/.test((r[iE] || "").trim())).map(r => ({
      row: r, entryId: r[iE].trim(), contest: (r[iName] || "").trim(), contestId: (r[iCid] || "").trim(), fee: (r[iFee] || "").trim() }));
    if (!entries.length) throw new Error("No entries found in that file. Enter your contests on DraftKings first, then download the entries CSV from Edit Entries.");
    const contests = new Map();
    entries.forEach(e => { const k = e.contestId || e.contest; if (!contests.has(k)) contests.set(k, { key: k, name: e.contest, fee: e.fee, n: 0 }); contests.get(k).n++; });
    return { kind: "entries", name, H, slotCols, lastCol: Math.max(...slotCols), entries, contests: [...contests.values()], ids };
  }
  if (pl && ids.size) return { kind: "salaries", name, ids };
  throw new Error("That doesn't look like a DraftKings file. Upload the entries CSV from My Contests → Edit Entries (or a salaries CSV).");
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
  if (!best || best[1] < Math.min(8, sample.length * 0.3)) throw new Error("We don't have this slate loaded yet. Simple mode covers the NFL main slate once salaries are posted — try again later, or check the slate on DraftKings.");
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
  if (!players.some(p => p.position === "QB") || !players.some(p => p.position === "DST")) throw new Error("This slate doesn't have projections yet. Check back once the week's simulations have run.");
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
function proj(p) {
  let m = p.mean ?? 0;
  const pr = props.get(p.site_player_id);
  if (pr != null && p.mean != null) m = (1 - CONF.propsBlend) * m + CONF.propsBlend * pr;
  if (likes.has(p.site_player_id)) m *= CONF.like;
  return m;
}
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

async function generate(n, onProgress) {
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
  const N = all.length, r = N <= 1 ? 0 : i / N;
  const tier = TIERS.find(x => r < x.upto).t;
  const keys = all.map(x => x.key), hi = Math.max(...keys), lo = Math.min(...keys);
  const rating = N <= 1 || hi === lo ? 99 : Math.round(72 + 27 * (L.key - lo) / (hi - lo));
  const qb = ps.find(p => p.position === "QB");
  const mates = ps.filter(p => p.team === qb.team && p !== qb && p.position !== "DST").map(p => lastName(p.player_name));
  return { ...L, rank: i + 1, tier, rating, qb, mates, slots: assignSlots(ps), salary: ps.reduce((s, p) => s + p.salary, 0), uses: [] };
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
    text = [SLOTS, ...lineups.map(L => L.slots.map(([, p]) => p.site_player_id))].map(r => r.join(",")).join("\r\n") + "\r\n";
    fname = "GameTimeWin_" + (slate.slate_key || "week" + slate.week) + "_" + lineups.length + ".csv";
  }
  const a = document.createElement("a");
  a.href = URL.createObjectURL(new Blob([text], { type: "text/csv" })); a.download = fname; a.click();
  setTimeout(() => URL.revokeObjectURL(a.href), 1500);
  GS.toast(file.kind === "entries" ? "Downloaded — upload it on DraftKings under Edit Entries." : "Downloaded — upload it in the contest lobby under Upload Lineups.", "ok");
}

// ============================================================ UI: steps
function step(name) {
  const order = ["upload", "pick", "build", "reveal"], at = order.indexOf(name);
  document.querySelectorAll(".sm-steps li").forEach(li => { const i = order.indexOf(li.dataset.step);
    li.toggleAttribute("aria-current", i === at); if (i === at) li.setAttribute("aria-current", "step"); li.classList.toggle("done", i < at); });
  $("scrUpload").hidden = name !== "upload"; $("scrPick").hidden = name !== "pick"; $("scrBuild").hidden = name !== "build"; $("scrReveal").hidden = name !== "reveal";
  window.scrollTo({ top: 0, behavior: REDUCED ? "auto" : "smooth" });
}

// ============================================================ UI: upload
async function handleFile(f) {
  if (!f) return;
  const err = $("uploadError"); err.hidden = true;
  const drop = $("drop"); drop.classList.add("busy"); $("drop").querySelector(".drop-title").textContent = "Reading your file…";
  try {
    if (f.size > 20e6) throw new Error("That file is too large to be a DraftKings CSV.");
    file = parseDK(await f.text(), f.name);
    drop.querySelector(".drop-title").textContent = "Finding your slate…";
    if (!supa && !FIXTURE) throw new Error("The site isn't connected to its database right now. Try again in a minute.");
    const s = await findSlate(file.ids);
    await loadSlate(s);
    likes = new Set(); fades = new Set();
    nWanted = file.kind === "entries" ? Math.max(...file.contests.map(c => c.n)) : 20;
    renderPick(); step("pick");
  } catch (e) {
    console.error(e); err.textContent = e.message || String(e); err.hidden = false;
  } finally {
    drop.classList.remove("busy"); drop.querySelector(".drop-title").textContent = "Drop your DKEntries.csv here";
    $("file").value = "";
  }
}

// ============================================================ UI: pick
function renderPick() {
  const games = new Set(players.map(p => p.game_id).filter(Boolean)).size;
  $("pickKicker").textContent = `DraftKings · Week ${slate.week} · ${games} games`;
  if (file.kind === "entries") {
    const totalE = file.entries.length;
    $("entriesSum").innerHTML = file.contests.map(c => `<span class="chip-c">${esc(shortContest(c.name))} <b>${c.n}</b> ${c.n === 1 ? "entry" : "entries"}</span>`).join("")
      + `<span class="chip-c">${totalE} entries → <b>${nWanted}</b> lineups${file.contests.length > 1 ? ", best ones play in every contest" : ""}</span>`;
    $("countRow").hidden = true;
  } else {
    $("entriesSum").innerHTML = `<span class="chip-c">Salaries file · no entries — choose how many lineups below</span>`;
    $("countRow").hidden = false;
    pressed("nSeg", "n", String(nWanted));
  }
  renderGrid(); renderDock();
}
function pressed(groupId, attr, val) { $(groupId).querySelectorAll("button").forEach(b => b.setAttribute("aria-pressed", String(b.dataset[attr] === val))); }
function pickList() {
  const q = $("pickSearch").value.trim().toLowerCase();
  const byProj = (a, b) => proj(b) - proj(a);
  if (q) return players.filter(p => p.player_name.toLowerCase().includes(q) || String(p.team).toLowerCase() === q).sort(byProj).slice(0, 36);
  if (pickPos !== "ALL") return players.filter(p => p.position === pickPos).sort(byProj).slice(0, 24);
  const take = { QB: 4, RB: 6, WR: 8, TE: 3, DST: 3 };
  return Object.entries(take).flatMap(([pos, k]) => players.filter(p => p.position === pos).sort(byProj).slice(0, k));
}
function renderGrid() {
  const list = pickList();
  $("pickGrid").innerHTML = list.length ? list.map(p => {
    const id = p.site_player_id, st = likes.has(id) ? "like" : fades.has(id) ? "fade" : "";
    const init = p.position === "DST" ? p.team : p.player_name.split(" ").map(w => w[0]).join("").slice(0, 2);
    return `<button type="button" class="pk ${st}" data-id="${esc(id)}" style="--pc:var(--pos-${p.position === "DST" ? "dst" : p.position.toLowerCase()})" aria-pressed="${st ? "true" : "false"}" aria-label="${esc(p.player_name)}, ${esc(p.position)}, ${st || "no pick"}">
      <span class="av">${esc(init)}</span><span class="nm">${esc(p.player_name)}</span>
      <span class="pj">${f1(proj(p))}<small>pts</small></span>
      <span class="mt">${esc(p.position)} · ${esc(p.team)} · $${(p.salary / 1000).toFixed(1)}K</span>
      <span class="st">${st === "like" ? "LIKE" : st === "fade" ? "FADE" : ""}</span></button>`;
  }).join("") : `<div class="pick-empty">No players match that search.</div>`;
}
function cyclePick(id) {
  if (likes.has(id)) { likes.delete(id); fades.add(id); }
  else if (fades.has(id)) fades.delete(id);
  else likes.add(id);
  renderGrid(); renderDock();
  const el = $("pickGrid").querySelector(`[data-id="${CSS.escape(id)}"]`); el?.classList.add("pop"); el?.focus({ preventScroll: true });
}
function renderDock() {
  const picks = [...likes].map(id => ["like", id]).concat([...fades].map(id => ["fade", id]));
  $("dockPicks").innerHTML = picks.length ? picks.map(([k, id]) => `<span class="dp ${k}">${k === "like" ? "▲" : "✕"} ${esc(byId.get(id)?.player_name || id)}</span>`).join("") : `<span class="muted">No picks yet</span>`;
  const left = CONF.minEdits - picks.length;
  $("buildBtn").disabled = left > 0;
  $("buildLabel").textContent = left > 0 ? `Pick ${left} more player${left > 1 ? "s" : ""}` : `Build ${nWanted} lineup${nWanted > 1 ? "s" : ""} ⚡`;
}

// ============================================================ UI: forge
const STAGES = [
  [0.00, "Loading 10,000 simulated games"],
  [0.06, "Blending Vegas lines and player props"],
  [0.18, "Stacking quarterbacks with their receivers"],
  [0.45, "Hunting for tournament ceilings"],
  [0.75, "Balancing exposure across your entries"],
  [0.97, "Ranking every lineup by its ceiling"],
];
function Forge() {
  const t0 = performance.now(); let shown = 0, target = 0, stage = -1, raf = 0;
  const names = players.slice().sort((a, b) => proj(b) - proj(a)).slice(0, 40);
  const tk = names.map(p => `<span>${esc(p.player_name)} <b>${f1(proj(p))}</b></span>`).join("");
  $("ticker").innerHTML = tk + tk;
  $("forgeFill").style.width = "0%"; $("forgeNum").textContent = "0"; $("scrBuild").classList.remove("done");
  const loop = () => {
    shown += (target - shown) * 0.08;
    $("forgeNum").textContent = Math.round(shown * 10000).toLocaleString();
    $("forgeFill").style.width = (shown * 100).toFixed(1) + "%";
    const s = STAGES.reduce((acc, [at], i) => shown >= at ? i : acc, 0);
    if (s !== stage) { stage = s; const h = $("forgeStage"); h.textContent = STAGES[s][1]; h.classList.remove("swap"); void h.offsetWidth; h.classList.add("swap"); }
    raf = requestAnimationFrame(loop);
  };
  raf = requestAnimationFrame(loop);
  return {
    set(frac, sub) { target = Math.max(target, Math.min(1, frac)); if (sub) $("forgeSub").textContent = sub; },
    async done() {
      const minMs = REDUCED ? 300 : 4200, left = minMs - (performance.now() - t0);
      target = 1; if (left > 0) await sleep(left);
      while (shown < 0.995) await sleep(50);
      cancelAnimationFrame(raf); $("forgeNum").textContent = "10,000"; $("forgeFill").style.width = "100%";
      $("forgeStage").textContent = "Sealing your pack";
      $("scrBuild").classList.add("done");
      await sleep(REDUCED ? 50 : 700);
    },
    stop() { cancelAnimationFrame(raf); },
  };
}

async function build() {
  if (likes.size + fades.size < CONF.minEdits) return;
  step("build");
  const forge = Forge();
  try {
    forge.set(0.03, "Waking up the optimizer…");
    await simPromise;
    forge.set(0.06, sim ? `${sim.n.toLocaleString()} stored game scripts per player loaded` : "Simulations unavailable — ranking by projection");
    lineups = await generate(nWanted, (k, total, phase) => {
      if (phase === "rank") forge.set(0.97, `Scoring ${total} candidates across every simulated game…`);
      else forge.set(0.06 + 0.9 * k / total, `Solving candidate ${k + 1} of ${total}`);
    });
    if (!lineups.length) throw new Error("No valid lineup fits your picks — try fading fewer players.");
    assignEntries();
    await forge.done();
    showPack();
  } catch (e) {
    forge.stop(); console.error(e);
    GS.toast("Build failed: " + (e.message || e), "error");
    step("pick");
  }
}

// ============================================================ UI: reveal
function showPack() {
  step("reveal");
  $("packStage").hidden = false; $("pull").hidden = true; $("binder").hidden = true;
  const pk = $("pack"); pk.className = "pack"; pk.disabled = false;
  $("packCount").textContent = lineups.length;
  $("packWeek").textContent = `Week ${slate.week} · ${new Set(lineups.map(L => L.tier)).has("S") ? "Legendary inside" : "Fresh pack"}`;
}
async function openPack() {
  const pk = $("pack"); if (pk.disabled) return; pk.disabled = true;
  if (!REDUCED) { pk.classList.add("shake"); await sleep(1000); }
  pk.classList.remove("shake"); pk.classList.add("open");
  flash(false);
  const r = pk.getBoundingClientRect(); burst(r.left + r.width / 2, r.top + r.height / 3, 70, ["#ffcf3f", "#fff3b0", "#4fe07e"]);
  await sleep(REDUCED ? 100 : 1000);
  $("packStage").hidden = true;
  startPulls();
}

let pulls = [], pullAt = 0;
function startPulls() {
  // one card per tier, weakest tier first, so the pack builds up to the best lineup
  pulls = ["C", "B", "A", "S"].map(t => lineups.find(L => L.tier === t)).filter(Boolean);
  if (pulls[pulls.length - 1] !== lineups[0]) pulls.push(lineups[0]);
  pullAt = 0;
  $("pull").hidden = false;
  showPull();
}
async function showPull() {
  const L = pulls[pullAt], last = pullAt === pulls.length - 1;
  const label = $("pullLabel");
  label.className = "pull-label"; label.removeAttribute("style");
  label.textContent = last ? "Your best pull" : `Card ${pullAt + 1} of ${pulls.length}`;
  $("pullNext").textContent = last ? `See all ${lineups.length} lineups` : "Next card";
  $("pullNext").disabled = true; $("pullSkip").hidden = last;
  const slot = $("pullSlot");
  slot.innerHTML = cardHTML(L, true);
  const card = slot.querySelector(".lcard");
  await sleep(REDUCED ? 0 : 450);
  card.classList.remove("facedown");
  await sleep(REDUCED ? 0 : 380);
  const info = tierInfo(L.tier);
  label.textContent = info.name;
  label.style.setProperty("--tc", `var(--tier-${L.tier.toLowerCase()})`); label.classList.add("tier-banner");
  const r = card.getBoundingClientRect(), cx = r.left + r.width / 2, cy = r.top + r.height / 2;
  if (L.tier === "S") { flash(true); burst(cx, cy, 160, ["#ffcf3f", "#fff3b0", "#ff8a3d", "#ffffff"]); setTimeout(() => burst(cx, cy - 60, 90, ["#ffcf3f", "#4fe07e", "#7cb4ff"]), 380); }
  else if (L.tier === "A") burst(cx, cy, 70, ["#c08bff", "#e6d4ff", "#7cb4ff"]);
  else if (L.tier === "B") burst(cx, cy, 30, ["#5fb4ff", "#c9e6ff"]);
  $("pullNext").disabled = false; $("pullNext").focus();
}
function nextPull() { if (pullAt < pulls.length - 1) { pullAt++; showPull(); } else showBinder(); }

let tierFilter = "ALL";
function showBinder() {
  $("pull").hidden = true; $("binder").hidden = false;
  const counts = Object.fromEntries(TIERS.map(t => [t.t, lineups.filter(L => L.tier === t.t).length]));
  const avgSal = lineups.reduce((s, L) => s + L.salary, 0) / lineups.length;
  $("binderSub").textContent = `${lineups.length} lineups · Week ${slate.week} · average salary ${money(avgSal)} · ranked by simulated ceiling`;
  $("tierTabs").innerHTML = [["ALL", "All", lineups.length], ...TIERS.map(t => [t.t, t.name, counts[t.t]])].filter(([, , c]) => c > 0)
    .map(([t, nm, c]) => `<button type="button" data-t="${t}" aria-pressed="${t === tierFilter}">${t === "ALL" ? "" : `<i>${t}</i>`}${nm} <small>${c}</small></button>`).join("");
  $("uploadNote").innerHTML = file.kind === "entries"
    ? `<span aria-hidden="true">ⓘ</span><div><b>To upload:</b> on DraftKings go to <b>My Contests → Upcoming → Edit Entries</b>, choose <b>Upload CSV</b>, and pick the downloaded file. It fills all ${file.entries.length} of your entries${file.contests.length > 1 ? " — your top lineups go into every contest" : ""}. Any lineups already in those entries are replaced.</div>`
    : `<span aria-hidden="true">ⓘ</span><div><b>To upload:</b> open your contest on DraftKings and choose <b>Upload Lineups</b>, then pick the downloaded file. Each row is one lineup, best first.</div>`;
  renderCards();
}
function renderCards() {
  const list = lineups.filter(L => tierFilter === "ALL" || L.tier === tierFilter);
  $("cardGrid").innerHTML = list.map((L, i) => cardHTML(L, false, i)).join("");
}

function cardHTML(L, facedown, i = 0) {
  const info = tierInfo(L.tier);
  const rows = L.slots.map(([slot, p]) => `<li class="${likes.has(p.site_player_id) ? "liked" : ""}"><span class="pos ${p.position === "DST" ? "DST" : p.position}">${slot}</span>
    <span class="nm">${esc(p.player_name)}<small>${esc(p.team)}</small></span><span class="sal">${money(p.salary)}</span></li>`).join("");
  const uses = L.uses.length ? `<div class="lc-uses">Plays in ${L.uses.map(u => `${esc(u.name)}${u.n > 1 ? " ×" + u.n : ""}`).join(" · ")}</div>` : "";
  const stack = `${lastName(L.qb.player_name)}${L.mates.length ? " + " + L.mates.join(", ") : ""}`;
  return `<article class="lcard${facedown ? " facedown" : ""}" data-tier="${L.tier}" style="--i:${Math.min(i, 30)}" aria-label="Lineup ${L.rank}, ${info.name} tier">
    <div class="lc-back" aria-hidden="true"></div>
    <div class="lc-front">
      <div class="lc-head"><div class="lc-tier">${L.tier}</div>
        <div class="lc-title"><b title="${esc(stack)}">${esc(stack)}</b><span>${info.name} · #${L.rank}</span></div>
        <div class="lc-rating"><b>${L.rating}</b><span>rating</span></div></div>
      <ul class="lc-list">${rows}</ul>
      <div class="lc-stats">
        <div><b>${f1(L.sim ? L.sim.p90 : L.proj)}</b><span>${L.sim ? "ceiling" : "projected"}</span></div>
        <div><b>${f1(L.sim ? L.sim.p50 : L.proj)}</b><span>${L.sim ? "median" : "points"}</span></div>
        <div><b>${money(L.salary)}</b><span>salary</span></div>
      </div>${uses}
    </div></article>`;
}

// ============================================================ effects
function flash(gold) {
  if (REDUCED) return;
  const d = document.createElement("div"); d.className = "flash" + (gold ? " gold" : ""); document.body.appendChild(d); setTimeout(() => d.remove(), 950);
}
const FX = { parts: [], raf: 0 };
function burst(x, y, n, colors) {
  if (REDUCED) return;
  const c = $("fx"), dpr = Math.min(2, devicePixelRatio || 1);
  if (c.width !== innerWidth * dpr) { c.width = innerWidth * dpr; c.height = innerHeight * dpr; }
  for (let i = 0; i < n; i++) {
    const a = Math.random() * Math.PI * 2, v = 4 + Math.random() * 9;
    FX.parts.push({ x: x * dpr, y: y * dpr, vx: Math.cos(a) * v * dpr, vy: (Math.sin(a) * v - 5) * dpr, s: (3 + Math.random() * 5) * dpr, r: Math.random() * 6, vr: (Math.random() - 0.5) * 0.4, c: colors[i % colors.length], life: 1 });
  }
  if (!FX.raf) FX.raf = requestAnimationFrame(stepFx);
}
function stepFx() {
  const c = $("fx"), g = c.getContext("2d");
  g.clearRect(0, 0, c.width, c.height);
  FX.parts = FX.parts.filter(p => p.life > 0 && p.y < c.height + 40);
  FX.parts.forEach(p => {
    p.vy += 0.32; p.vx *= 0.985; p.x += p.vx; p.y += p.vy; p.r += p.vr; p.life -= 0.0085;
    g.save(); g.globalAlpha = Math.max(0, Math.min(1, p.life * 1.4)); g.translate(p.x, p.y); g.rotate(p.r); g.fillStyle = p.c; g.fillRect(-p.s / 2, -p.s / 4, p.s, p.s / 2); g.restore();
  });
  FX.raf = FX.parts.length ? requestAnimationFrame(stepFx) : 0;
  if (!FX.raf) g.clearRect(0, 0, c.width, c.height);
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
  window.addEventListener("drop", (e) => { e.preventDefault(); if (!$("scrUpload").hidden) handleFile(e.dataTransfer.files[0]); });

  $("pickGrid").addEventListener("click", (e) => { const b = e.target.closest(".pk"); if (b) cyclePick(b.dataset.id); });
  $("posSeg").addEventListener("click", (e) => { const b = e.target.closest("button"); if (!b) return; pickPos = b.dataset.pos; pressed("posSeg", "pos", pickPos); $("pickSearch").value = ""; renderGrid(); });
  $("pickSearch").addEventListener("input", renderGrid);
  $("nSeg").addEventListener("click", (e) => { const b = e.target.closest("button"); if (!b) return; nWanted = +b.dataset.n; pressed("nSeg", "n", b.dataset.n); renderDock(); });
  $("buildBtn").addEventListener("click", build);

  $("pack").addEventListener("click", openPack);
  $("pullNext").addEventListener("click", nextPull);
  $("pullSkip").addEventListener("click", showBinder);
  $("tierTabs").addEventListener("click", (e) => { const b = e.target.closest("button"); if (!b) return; tierFilter = b.dataset.t; pressed("tierTabs", "t", tierFilter); renderCards(); });
  $("dlBtn").addEventListener("click", downloadCSV);
  $("againBtn").addEventListener("click", () => { lineups = []; file = null; tierFilter = "ALL"; step("upload"); });
  window.addEventListener("resize", () => { const c = $("fx"); c.width = 0; });
}

async function init() {
  wire();
  if (!FIXTURE && cfg.SUPABASE_URL && !cfg.SUPABASE_URL.includes("YOUR-PROJECT")) supa = window.supabase.createClient(cfg.SUPABASE_URL, cfg.SUPABASE_ANON_KEY);
  try { glpk = await GLPK(); }
  catch (e) { console.error(e); const err = $("uploadError"); err.textContent = "Couldn't load the optimizer. Check your connection and reload the page."; err.hidden = false; }
}
init();
