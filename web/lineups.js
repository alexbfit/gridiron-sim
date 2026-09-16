/* Gridiron Sim — lineup builder. Reads slate_board from Supabase, optimizes with GLPK in-browser. */
import GLPK from "https://cdn.jsdelivr.net/npm/glpk.js@4.0.2/dist/index.js";

const cfg = window.GRIDIRON_CONFIG || {};
const $ = (id) => document.getElementById(id);
const statusEl = $("status");

const SITES = {
  DK: { cap: 50000, slots: ["QB", "RB", "RB", "WR", "WR", "WR", "TE", "FLEX", "DST"],
        min: { QB: 1, RB: 2, WR: 3, TE: 1, DST: 1 }, flex: 7, maxTeamRule: 8, minGames: 2, defLabel: "DST" },
  FD: { cap: 60000, slots: ["QB", "RB", "RB", "WR", "WR", "WR", "TE", "FLEX", "DEF"],
        min: { QB: 1, RB: 2, WR: 3, TE: 1, DST: 1 }, flex: 7, maxTeamRule: 4, minGames: 2, defLabel: "DEF" },
};

let supa, glpk;
let slates = [], slate = null, players = [], byId = new Map();
let locks = new Set(), excludes = new Set(), overrides = new Map(), ownOverrides = new Map();
let ownership = new Map();       // site_player_id -> projected ownership % (heuristic unless overridden)
let ownModel = { b: 1.4, c: 0.8, cap: 60 };
let external = new Map();        // site_player_id -> { mean, own } from an imported projection file
let blend = 0;                   // 0..1 weight on external projections   // replaced by model_params.ownership_model once fitted to real ownership
const SLOTS_PER_POS = { QB: 1.0, RB: 2.4, WR: 3.4, TE: 1.2, DST: 1.0 };
let lineups = [];
let sim = null;                  // { n, index: Map(site_player_id -> Float32Array) }
let sortKey = "mean", sortAsc = false;

function setStatus(msg, err) { statusEl.textContent = msg; statusEl.classList.toggle("error", !!err); }
const f1 = (v) => v == null ? "–" : Number(v).toFixed(1);
const f2 = (v) => v == null ? "–" : Number(v).toFixed(2);
const pct = (v) => v == null ? "–" : Math.round(Number(v) * 100) + "%";
const money = (v) => "$" + Number(v).toLocaleString();

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

// ---------------------------------------------------------------- data
async function loadSlates() {
  const { data, error } = await supa.from("slates").select("*").order("imported_at", { ascending: false }).limit(50);
  if (error) throw error;
  slates = data.filter(s => s.slate_key);
  try {
    const { data: mp } = await supa.from("model_params").select("param_value").eq("param_key", "ownership_model").maybeSingle();
    if (mp?.param_value?.b != null) ownModel = { b: +mp.param_value.b, c: +mp.param_value.c, cap: +mp.param_value.cap || 60, fitted: mp.param_value };
  } catch (e) { /* defaults */ }
  if (!slates.length) throw new Error("No slates imported yet. Drop a salary CSV into data/slates/ and push.");
  $("slate").replaceChildren(...slates.map(s => new Option(`${s.site} · ${s.season} wk ${s.week} · ${s.slate_type} (${s.n_players})`, s.slate_id)));
}

async function loadBoard() {
  slate = slates.find(s => s.slate_id === $("slate").value);
  setStatus("Loading slate…");
  const rows = await fetchAll(supa.from("slate_board").select("*").eq("slate_id", slate.slate_id).order("site_player_id"));
  players = rows.map(r => ({ ...r, mean: r.mean == null ? null : Number(r.mean),
    p85: Number(r.p85), p15: Number(r.p15), floor: Number(r.floor), ceiling: Number(r.ceiling), stdev: Number(r.stdev),
    salary: Number(r.salary), boom_prob: Number(r.boom_prob), bust_prob: Number(r.bust_prob) }));
  byId = new Map(players.map(p => [p.site_player_id, p]));
  locks.clear(); excludes.clear(); overrides.clear(); ownOverrides.clear(); lineups = []; sim = null;
  external = new Map();
  try {
    const ext = await fetchAll(supa.from("slate_projections").select("site_player_id,mean,components").eq("slate_id", slate.slate_id).eq("method", "external").order("site_player_id"));
    ext.forEach(r => external.set(r.site_player_id, { mean: Number(r.mean), own: r.components?.ownership == null ? null : Number(r.components.ownership) }));
  } catch (e) { /* none */ }
  const bl = $("blendWrap"); bl.style.display = external.size ? "" : "none";
  if (external.size) { $("blendSrc").textContent = `${external.size} players`; blend = (+$("blend").value || 0) / 100; } else blend = 0;
  estimateOwnership();
  renderResults();
  render();
  loadSim().catch(e => { console.warn("sim matrix unavailable", e); render(); });
}

async function loadSim() {
  const url = slate.sim_meta?.url;
  if (!url) return;
  const res = await fetch(url, { cache: "no-cache" });
  if (!res.ok) throw new Error("sim fetch " + res.status);
  let text;
  if (typeof DecompressionStream === "function" && !res.headers.get("content-encoding")) {
    text = await new Response(res.body.pipeThrough(new DecompressionStream("gzip"))).text();
  } else {
    text = await res.text();
  }
  const data = JSON.parse(text);
  const index = new Map();
  data.players.forEach((id, i) => index.set(id, Float32Array.from(data.scores[i])));
  sim = { n: data.n, index, generated_at: data.generated_at };
  render();
}

function lineupSim(ids) {
  if (!sim) return null;
  const tot = new Float32Array(sim.n);
  let covered = 0;
  ids.forEach(id => { const a = sim.index.get(id); if (a) { covered++; for (let i = 0; i < sim.n; i++) tot[i] += a[i]; }
    else { const m = proj(byId.get(id)); for (let i = 0; i < sim.n; i++) tot[i] += m; } });
  const sorted = Float32Array.from(tot).sort();
  const q = (p) => sorted[Math.min(sim.n - 1, Math.floor(p * sim.n))];
  return { p10: q(0.10), p50: q(0.50), p90: q(0.90), p98: q(0.98), covered };
}

function proj(p) {
  if (overrides.has(p.site_player_id)) return overrides.get(p.site_player_id);
  const ext = external.get(p.site_player_id);
  const own_ = p.mean ?? 0;
  if (ext && blend > 0) return p.mean == null ? ext.mean : (1 - blend) * own_ + blend * ext.mean;
  return own_;
}
function own(p) {
  if (ownOverrides.has(p.site_player_id)) return ownOverrides.get(p.site_player_id);
  const ext = external.get(p.site_player_id);
  if (ext && ext.own != null && blend > 0) return ext.own;
  return ownership.get(p.site_player_id) ?? 0;
}

// Heuristic projected ownership: within each position, the field chases value (pts per $1k) and
// raw projection; a softmax over those turns them into shares of the position's roster slots.
function estimateOwnership() {
  ownership = new Map();
  for (const pos of Object.keys(SLOTS_PER_POS)) {
    const pool = players.filter(p => p.position === pos && (p.mean ?? 0) > 0 && !["OUT","IR","O"].includes((p.status || "").toUpperCase()));
    if (!pool.length) continue;
    const val = pool.map(p => proj(p) / (p.salary / 1000)), pr = pool.map(p => proj(p));
    const z = (a) => { const m = a.reduce((s, v) => s + v, 0) / a.length, sd = Math.sqrt(a.reduce((s, v) => s + (v - m) ** 2, 0) / a.length) || 1; return a.map(v => (v - m) / sd); };
    const zv = z(val), zp = z(pr);
    const w = pool.map((p, i) => Math.exp(ownModel.b * zv[i] + ownModel.c * zp[i]) * (["Q","D"].includes((p.status || "").toUpperCase()) ? 0.7 : 1));
    const tot = w.reduce((s, v) => s + v, 0);
    pool.forEach((p, i) => ownership.set(p.site_player_id, Math.min(ownModel.cap, 100 * SLOTS_PER_POS[pos] * w[i] / tot)));
  }
}

// ---------------------------------------------------------------- pool table
const COLS = [
  { key: "lock", label: "🔒", w: 34 }, { key: "excl", label: "✕", w: 34 },
  { key: "player_name", label: "Player", left: true },
  { key: "position", label: "Pos", left: true }, { key: "team", label: "Team", left: true },
  { key: "opponent", label: "Opp", left: true }, { key: "status", label: "Inj", left: true },
  { key: "salary", label: "Salary", fmt: money },
  { key: "mean", label: "Proj", fmt: f1, edit: true },
  { key: "value", label: "Val", fmt: f2 },
  { key: "floor", label: "Floor", fmt: f1 }, { key: "ceiling", label: "Ceil", fmt: f1 },
  { key: "boom_prob", label: "Boom", fmt: pct }, { key: "bust_prob", label: "Bust", fmt: pct },
  { key: "own", label: "Own%", edit: true },
  { key: "games_used", label: "G" },
];

function visiblePlayers() {
  const pos = $("posFilter").value, q = $("search").value.trim().toLowerCase();
  return players.filter(p => (pos === "ALL" || p.position === pos) &&
    (!q || p.player_name.toLowerCase().includes(q) || (p.team || "").toLowerCase().includes(q)));
}

function render() {
  const thead = document.querySelector("#pool thead"), tbody = document.querySelector("#pool tbody");
  const tr = document.createElement("tr");
  COLS.forEach(c => {
    const th = document.createElement("th"); th.textContent = c.label;
    if (c.left) th.classList.add("left");
    if (c.key === sortKey) th.classList.add("sorted", sortAsc ? "asc" : "x");
    th.addEventListener("click", () => { if (sortKey === c.key) sortAsc = !sortAsc; else { sortKey = c.key; sortAsc = !!c.left; } render(); });
    tr.appendChild(th);
  });
  thead.replaceChildren(tr);

  const rows = visiblePlayers().map(p => ({ p, mean: proj(p), value: p.salary ? proj(p) / (p.salary / 1000) : 0, own: own(p) }));
  rows.sort((a, b) => {
    const ka = sortKey === "mean" ? a.mean : sortKey === "value" ? a.value : sortKey === "own" ? a.own : a.p[sortKey];
    const kb = sortKey === "mean" ? b.mean : sortKey === "value" ? b.value : sortKey === "own" ? b.own : b.p[sortKey];
    let c = (ka == null) - (kb == null) || (typeof ka === "number" ? ka - kb : String(ka ?? "").localeCompare(String(kb ?? "")));
    return sortAsc ? c : -c;
  });
  const frag = document.createDocumentFragment();
  rows.forEach(({ p, mean, value }) => {
    const r = document.createElement("tr");
    const id = p.site_player_id;
    if (locks.has(id)) r.classList.add("locked");
    if (excludes.has(id)) r.classList.add("excluded");
    COLS.forEach(c => {
      const td = document.createElement("td");
      if (c.left) td.classList.add("left");
      if (c.key === "lock" || c.key === "excl") {
        const cb = document.createElement("input"); cb.type = "checkbox";
        const set = c.key === "lock" ? locks : excludes, other = c.key === "lock" ? excludes : locks;
        cb.checked = set.has(id);
        cb.addEventListener("change", () => { if (cb.checked) { set.add(id); other.delete(id); } else set.delete(id); render(); });
        td.appendChild(cb);
      } else if (c.key === "mean") {
        const inp = document.createElement("input"); inp.type = "number"; inp.step = "0.1"; inp.className = "projedit";
        inp.value = mean.toFixed(1);
        if (overrides.has(id)) inp.classList.add("overridden");
        inp.addEventListener("change", () => { const v = parseFloat(inp.value); if (Number.isNaN(v) || v === (p.mean ?? 0)) overrides.delete(id); else overrides.set(id, v); render(); });
        td.appendChild(inp);
      } else if (c.key === "own") {
        const inp = document.createElement("input"); inp.type = "number"; inp.step = "1"; inp.min = "0"; inp.max = "100"; inp.className = "projedit";
        inp.value = Math.round(own(p));
        if (ownOverrides.has(id)) inp.classList.add("overridden");
        inp.addEventListener("change", () => { const v = parseFloat(inp.value); if (Number.isNaN(v)) ownOverrides.delete(id); else ownOverrides.set(id, v); render(); });
        td.appendChild(inp);
      } else if (c.key === "value") {
        td.textContent = f2(value);
      } else if (c.key === "position") {
        const s = document.createElement("span"); s.className = "pos " + (["QB","RB","WR","TE"].includes(p.position) ? p.position : "other"); s.textContent = p.position; td.appendChild(s);
      } else if (c.key === "status") {
        const rep = p.injury_report ? { Out: "OUT", Doubtful: "D", Questionable: "Q" }[p.injury_report] || "" : "";
        td.textContent = p.status || (rep ? rep + "*" : "");
        if (!p.status && !rep && p.practice_status && /Did Not|Limited/.test(p.practice_status)) { td.textContent = p.practice_status.startsWith("Did") ? "DNP" : "LP"; td.classList.add("inj"); td.title = `Practice: ${p.practice_status}${p.primary_injury ? ` (${p.primary_injury})` : ""} — no game designation yet`; }
        if (p.status || rep) { td.classList.add("inj"); td.title = (p.injury_report ? `Official report: ${p.injury_report}` : "") + (p.primary_injury ? ` (${p.primary_injury})` : "") + (p.practice_status ? ` · ${p.practice_status}` : "") + (rep && !p.status ? " — * from the NFL report, not the site" : ""); }
      } else {
        td.textContent = c.fmt ? c.fmt(p[c.key]) : (p[c.key] ?? "–");
      }
      r.appendChild(td);
    });
    frag.appendChild(r);
  });
  tbody.replaceChildren(frag);
  const nSim = players.filter(p => p.method === "sim").length;
  setStatus(`${slate.site} · ${slate.season} wk ${slate.week} · ${rows.length} players · ${nSim ? nSim + " sim-projected" : "baseline projections"}`
    + (sim ? ` · sim matrix ${sim.n} sims loaded` : "") + ` · ${locks.size} locked · ${excludes.size} excluded`);
  $("objHint").textContent = (ownModel.fitted ? `Ownership model fitted to ${ownModel.fitted.slates} contest(s). ` : "Ownership is a heuristic until a contest standings file is imported. ") + ($("contest").value === "cash"
    ? "Cash: maximizes projected points with a floor tilt (0.8·proj + 0.2·floor)."
    : "GPP: maximizes ceiling-tilted score (0.6·proj + 0.4·p85) minus an ownership fade, random jitter for diversity, stacks enforced.");
}

// ---------------------------------------------------------------- optimizer
function buildLP(pool, opts, prior, exposureBlocked) {
  const site = SITES[slate.site];
  const vars = pool.map(p => ({ name: "x" + p.site_player_id, coef: opts.score(p) }));
  const st = [];
  const sum = (filter, coef = 1) => pool.filter(filter).map(p => ({ name: "x" + p.site_player_id, coef }));
  st.push({ name: "salary", vars: sum(() => true).map(v => ({ ...v, coef: byId.get(v.name.slice(1)).salary })), bnds: { type: glpk.GLP_DB, lb: opts.minSalary, ub: site.cap } });
  st.push({ name: "roster", vars: sum(() => true), bnds: { type: glpk.GLP_FX, lb: 9, ub: 9 } });
  st.push({ name: "qb", vars: sum(p => p.position === "QB"), bnds: { type: glpk.GLP_FX, lb: 1, ub: 1 } });
  st.push({ name: "dst", vars: sum(p => p.position === "DST"), bnds: { type: glpk.GLP_FX, lb: 1, ub: 1 } });
  st.push({ name: "rb", vars: sum(p => p.position === "RB"), bnds: { type: glpk.GLP_DB, lb: 2, ub: 3 } });
  st.push({ name: "wr", vars: sum(p => p.position === "WR"), bnds: { type: glpk.GLP_DB, lb: 3, ub: 4 } });
  st.push({ name: "te", vars: sum(p => p.position === "TE"), bnds: { type: glpk.GLP_DB, lb: 1, ub: 2 } });
  st.push({ name: "flex", vars: sum(p => ["RB","WR","TE"].includes(p.position)), bnds: { type: glpk.GLP_FX, lb: 7, ub: 7 } });

  const teams = [...new Set(pool.map(p => p.team))];
  const maxTeam = Math.min(opts.maxTeam, site.maxTeamRule);
  teams.forEach(t => st.push({ name: "team_" + t, vars: sum(p => p.team === t), bnds: { type: glpk.GLP_UP, lb: 0, ub: maxTeam } }));
  const games = [...new Set(pool.map(p => p.game_id).filter(Boolean))];
  games.forEach(g => st.push({ name: "game_" + g, vars: sum(p => p.game_id === g), bnds: { type: glpk.GLP_UP, lb: 0, ub: 9 - 1 } }));

  // stacks: for each QB, catchers on same team >= n * xQB ; bring-back >= xQB
  pool.filter(p => p.position === "QB").forEach(qb => {
    if (opts.stack > 0) {
      const v = sum(p => p.team === qb.team && ["WR","TE"].includes(p.position));
      v.push({ name: "x" + qb.site_player_id, coef: -opts.stack });
      st.push({ name: "stack_" + qb.site_player_id, vars: v, bnds: { type: glpk.GLP_LO, lb: 0, ub: 0 } });
    }
    if (opts.bringback) {
      const v = sum(p => p.team === qb.opponent && ["WR","TE","RB"].includes(p.position));
      v.push({ name: "x" + qb.site_player_id, coef: -1 });
      st.push({ name: "bb_" + qb.site_player_id, vars: v, bnds: { type: glpk.GLP_LO, lb: 0, ub: 0 } });
    }
  });
  // uniqueness vs prior lineups
  prior.forEach((L, i) => st.push({ name: "uniq_" + i, vars: L.map(id => ({ name: "x" + id, coef: 1 })), bnds: { type: glpk.GLP_UP, lb: 0, ub: 9 - opts.minUniq } }));

  // locks / exposure blocks as rows (GLPK resets column bounds to [0,1] for binaries)
  pool.forEach(p => {
    const id = p.site_player_id;
    if (locks.has(id)) st.push({ name: "lock_" + id, vars: [{ name: "x" + id, coef: 1 }], bnds: { type: glpk.GLP_FX, lb: 1, ub: 1 } });
    else if (exposureBlocked.has(id)) st.push({ name: "block_" + id, vars: [{ name: "x" + id, coef: 1 }], bnds: { type: glpk.GLP_FX, lb: 0, ub: 0 } });
  });
  return { name: "lineup", objective: { direction: glpk.GLP_MAX, name: "obj", vars }, subjectTo: st, binaries: vars.map(v => v.name) };
}

function gauss() { let u = 0, v = 0; while (!u) u = Math.random(); while (!v) v = Math.random(); return Math.sqrt(-2 * Math.log(u)) * Math.cos(2 * Math.PI * v); }

async function generate() {
  const contest = $("contest").value, n = Math.max(1, Math.min(150, +$("nLineups").value || 1));
  const opts = {
    minSalary: +$("minSalary").value || 0, maxTeam: +$("maxTeam").value || 4,
    stack: contest === "gpp" ? +$("stack").value : 0, bringback: contest === "gpp" && $("bringback").value === "1",
    maxExp: (+$("maxExp").value || 100) / 100, minUniq: +$("minUniq").value || 1,
    rand: contest === "gpp" ? (+$("rand").value || 0) / 100 : 0, exclQ: $("exclQ").value === "1",
    fade: contest === "gpp" ? (+$("fade").value || 0) / 100 : 0,
    poolMult: sim ? Math.max(1, Math.min(5, +$("poolMult").value || 1)) : 1,
  };
  const nCand = Math.min(300, n * opts.poolMult);
  const eff = (p) => (p.status || "").toUpperCase() || ({ Out: "OUT", Doubtful: "D", Questionable: "Q" }[p.injury_report] || "");
  const pool = players.filter(p => !excludes.has(p.site_player_id) && p.mean != null && proj(p) > 0 &&
    !["OUT","IR","O"].includes(eff(p)) &&
    !(opts.exclQ && ["Q","D"].includes(eff(p))));
  if (!pool.some(p => p.position === "DST")) { setStatus("No DST rows with projections in this slate.", true); return; }

  lineups = []; $("generate").disabled = true;
  const usage = new Map(); const blocked = new Set();
  try {
    for (let k = 0; k < nCand; k++) {
      setStatus(`Solving lineup ${k + 1} of ${nCand}…`);
      const jitter = new Map(pool.map(p => [p.site_player_id, opts.rand ? 1 + opts.rand * gauss() * (p.stdev / Math.max(p.mean, 1)) : 1]));
      opts.score = (p) => {
        const m = proj(p);
        const base = contest === "cash" ? 0.8 * m + 0.2 * p.floor : 0.6 * m + 0.4 * (p.p85 * (m / Math.max(p.mean, 0.1)));
        // ownership fade: at 100% fade a 30%-owned player gives up ~1.8 pts of score
        return base * jitter.get(p.site_player_id) - opts.fade * 0.06 * own(p);
      };
      const lp = buildLP(pool, opts, lineups.map(L => L.ids), blocked);
      const res = await glpk.solve(lp, { msglev: glpk.GLP_MSG_OFF, tmlim: 20 });
      if (![glpk.GLP_OPT, glpk.GLP_FEAS].includes(res.result.status)) {
        setStatus(`Stopped at ${k} lineups — no feasible lineup left under these rules (uniqueness/exposure/stack).`, true);
        break;
      }
      const ids = Object.entries(res.result.vars).filter(([, v]) => v > 0.5).map(([name]) => name.slice(1));
      const L = summarize(ids); lineups.push(L);
      ids.forEach(id => { const u = (usage.get(id) || 0) + 1; usage.set(id, u); if (!locks.has(id) && u >= Math.ceil(opts.maxExp * nCand)) blocked.add(id); });
      if (k % 5 === 4 || k === nCand - 1) renderResults();
      await new Promise(r => setTimeout(r));
    }
    // rank by the sim metric that matters for the contest, keep the best n
    if (sim && lineups.length > 1) {
      const key = contest === "cash" ? "p50" : "p90";
      lineups.sort((a, b) => (b.sim?.[key] ?? b.proj) - (a.sim?.[key] ?? a.proj));
    }
    // keep the best n while still honouring the exposure cap on the final set
    const cap = Math.max(1, Math.ceil(opts.maxExp * n)), used = new Map(), kept = [];
    for (const L of lineups) {
      if (kept.length >= n) break;
      if (L.ids.every(id => locks.has(id) || (used.get(id) || 0) < cap)) {
        kept.push(L); L.ids.forEach(id => used.set(id, (used.get(id) || 0) + 1));
      }
    }
    lineups = kept;
    renderResults();
  } catch (e) { setStatus("Optimizer error: " + (e.message || e), true); console.error(e); }
  $("generate").disabled = false; $("exportBtn").disabled = !lineups.length;
  if (lineups.length) setStatus(`${lineups.length} lineup${lineups.length > 1 ? "s" : ""} built · avg proj ${f1(lineups.reduce((s, L) => s + L.proj, 0) / lineups.length)}`
    + (sim ? ` · ranked by sim ${contest === "cash" ? "median" : "90th pct"}` : ""));
}

function summarize(ids) {
  const ps = ids.map(id => byId.get(id));
  const order = { QB: 0, RB: 1, WR: 2, TE: 3, DST: 5 };
  const slots = assignSlots(ps);
  return { ids, slots, sim: lineupSim(ids), salary: ps.reduce((s, p) => s + p.salary, 0), proj: ps.reduce((s, p) => s + proj(p), 0),
    own: ps.reduce((s, p) => s + own(p), 0),
    ceil: ps.reduce((s, p) => s + p.p85, 0), floor: ps.reduce((s, p) => s + p.floor, 0),
    players: ps.sort((a, b) => order[a.position] - order[b.position] || b.salary - a.salary) };
}

function assignSlots(ps) {
  const site = SITES[slate.site];
  const take = (pos, n) => ps.filter(p => p.position === pos).sort((a, b) => b.salary - a.salary).slice(0, n);
  const used = new Set();
  const out = {};
  const put = (slot, p) => { out[slot] = p; used.add(p.site_player_id); };
  put("QB", take("QB", 1)[0]); put(site.defLabel, take("DST", 1)[0]);
  take("RB", 2).forEach((p, i) => put("RB" + (i + 1), p));
  take("WR", 3).forEach((p, i) => put("WR" + (i + 1), p));
  put("TE", take("TE", 1)[0]);
  put("FLEX", ps.find(p => !used.has(p.site_player_id) && ["RB","WR","TE"].includes(p.position)));
  return out;
}

function renderResults() {
  const box = $("results");
  if (!lineups.length) { box.innerHTML = '<p class="hint">Lock players you want, exclude ones you don\'t, set the rules, then Generate.</p>'; return; }
  const exposure = new Map();
  lineups.forEach(L => L.ids.forEach(id => exposure.set(id, (exposure.get(id) || 0) + 1)));
  const top = [...exposure.entries()].sort((a, b) => b[1] - a[1]).slice(0, 12)
    .map(([id, c]) => `${byId.get(id).player_name} ${Math.round(100 * c / lineups.length)}%`).join(" · ");
  box.innerHTML = `<p class="hint">Exposure: ${top}</p>` + lineups.map((L, i) => `
    <div class="lineup">
      <div class="lu-head"><b>#${i + 1}</b> <span>${money(L.salary)}</span> <span>proj <b>${f1(L.proj)}</b></span> <span title="sum of projected ownership">own ${Math.round(L.own)}%</span>${L.sim
        ? ` <span title="lineup total across simulated games">sim p10 ${f1(L.sim.p10)} · <b>p50 ${f1(L.sim.p50)}</b> · p90 ${f1(L.sim.p90)} · p98 ${f1(L.sim.p98)}</span>`
        : ` <span>floor ${f1(L.floor)}</span> <span>p85 ${f1(L.ceil)}</span>`}</div>
      <table class="lu">${L.players.map(p => `<tr><td><span class="pos ${["QB","RB","WR","TE"].includes(p.position) ? p.position : "other"}">${p.position}</span></td>
        <td class="left">${p.player_name}${p.status ? ` <em class="inj">${p.status}</em>` : ""}</td><td class="left">${p.team} v ${p.opponent}</td><td>${money(p.salary)}</td><td>${f1(proj(p))}</td></tr>`).join("")}</table>
    </div>`).join("");
}

function exportCSV() {
  const site = SITES[slate.site];
  const header = site.slots.join(",");
  const slotKeys = ["QB", "RB1", "RB2", "WR1", "WR2", "WR3", "TE", "FLEX", site.defLabel];
  const lines = lineups.map(L => slotKeys.map(k => L.slots[k]?.site_player_id ?? "").join(","));
  try {   // remember what was exported so the Results page can score it after the games
    const key = "gs_lineups_" + slate.slate_key;
    const prev = JSON.parse(localStorage.getItem(key) || "[]");
    const now = new Date().toISOString();
    const add = lineups.map(L => ({ ids: L.ids, proj: +L.proj.toFixed(1), exported_at: now }));
    localStorage.setItem(key, JSON.stringify(prev.concat(add).slice(-300)));
  } catch (e) { /* storage unavailable */ }
  const blob = new Blob([header + "\n" + lines.join("\n") + "\n"], { type: "text/csv" });
  const a = document.createElement("a"); a.href = URL.createObjectURL(blob);
  a.download = `${slate.slate_key}_lineups.csv`; a.click(); URL.revokeObjectURL(a.href);
}

// ---------------------------------------------------------------- init
async function init() {
  if (!cfg.SUPABASE_URL || cfg.SUPABASE_URL.includes("YOUR-PROJECT")) { setStatus("Set SUPABASE_URL and SUPABASE_ANON_KEY in web/config.js", true); return; }
  supa = window.supabase.createClient(cfg.SUPABASE_URL, cfg.SUPABASE_ANON_KEY);
  try {
    [glpk] = await Promise.all([GLPK(), loadSlates()]);
    await loadBoard();
  } catch (e) { setStatus("Error: " + (e.message || e), true); console.error(e); return; }
  $("slate").addEventListener("change", loadBoard);
  ["posFilter", "search"].forEach(id => $(id).addEventListener("input", render));
  $("contest").addEventListener("change", render);
  $("blend").addEventListener("input", () => { blend = (+$("blend").value || 0) / 100; estimateOwnership(); render(); });
  $("generate").addEventListener("click", generate);
  $("exportBtn").addEventListener("click", exportCSV);
  $("clearBtn").addEventListener("click", () => { locks.clear(); excludes.clear(); overrides.clear(); ownOverrides.clear(); estimateOwnership(); render(); });
}
init();
