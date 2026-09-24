/* Gridiron Sim — lineup builder.
   Reads slate_board + the sim matrix from Supabase and optimizes with GLPK in the browser.
   The projection blend, ownership model, LP, candidate pool and selection are unchanged from the
   original builder (same math as jobs/build_lineups.py); this file adds the product UI around them. */
import GLPK from "https://cdn.jsdelivr.net/npm/glpk.js@4.0.2/dist/index.js";

const cfg = window.GRIDIRON_CONFIG || {};
const GS = window.GS;
const icon = (n) => GS.icon(n);
const $ = (id) => document.getElementById(id);
const statusEl = $("status");

const SITES = {
  DK: { name: "DraftKings", cap: 50000, slots: ["QB", "RB", "RB", "WR", "WR", "WR", "TE", "FLEX", "DST"],
        min: { QB: 1, RB: 2, WR: 3, TE: 1, DST: 1 }, flex: 7, maxTeamRule: 8, minGames: 2, defLabel: "DST", minEdits: 2 },
  FD: { name: "FanDuel", cap: 60000, slots: ["QB", "RB", "RB", "WR", "WR", "WR", "TE", "FLEX", "DEF"],
        min: { QB: 1, RB: 2, WR: 3, TE: 1, DST: 1 }, flex: 7, maxTeamRule: 4, minGames: 2, defLabel: "DEF" },
};

let supa, glpk;
let slates = [], slate = null, players = [], byId = new Map();
let locks = new Set(), excludes = new Set(), overrides = new Map(), ownOverrides = new Map();
let ownership = new Map();       // site_player_id -> projected ownership % (heuristic unless overridden)
let ownModel = { b: 1.4, c: 0.8, cap: 60 };   // replaced by model_params.ownership_model once fitted to real ownership
let external = new Map();        // site_player_id -> { mean, own } from an imported projection file
let blend = 0;                   // 0..1 weight on external projections
const SLOTS_PER_POS = { QB: 1.0, RB: 2.4, WR: 3.4, TE: 1.2, DST: 1.0 };
let lineups = [];
let minExp = new Map(), maxExpP = new Map();   // per-player exposure bounds (fraction), from the Min/Max columns
let stackRules = new Map();                   // "T:<team>" | "G:<game_id>" -> { min, max } share of lineups whose QB stack comes from there
let stackMode = "teams";                      // Team stacks view: "teams" | "games"
let lastExposure = new Map();                 // site_player_id -> fraction of the last build
let candPool = [];                            // every candidate lineup from the last build (the "pool"); re-select picks from it
let poolExposure = new Map();                 // site_player_id -> fraction of the candidate pool (the sims' own preference)
let candOpts = null;                          // options the pool was built with (contest, n, maxExp)
let view = "players";                         // "players" | "stacks" | "lineups"
const qbOf = (L) => L.ids.map(id => byId.get(id)).find(p => p && p.position === "QB");
const ruleKeys = (qb) => qb ? ["T:" + qb.team, "G:" + qb.game_id] : [];
const ruleMax = (k) => { const r = stackRules.get(k); return r && r.max != null && r.max < 1 ? r.max : null; };
const qbIdsFor = (key) => players.filter(p => p.position === "QB" && ("T:" + p.team === key || "G:" + p.game_id === key)).map(p => p.site_player_id);
const gameLabel = (gid) => { const p = players.find(x => x.game_id === gid); const m = /^(\S+)/.exec(p?.game_info || ""); return m ? m[1].replace("@", " @ ") : gid; };
const ruleLabel = (k) => k.startsWith("T:") ? `${k.slice(2)} stacks` : `${gameLabel(k.slice(2))} stacks`;
let colSet = "core";                          // "core" | "range" | "port"
let onlyMine = false;
let sim = null;                  // { n, index: Map(site_player_id -> Float32Array) }
let propsMap = new Map();        // site_player_id -> { mean, lines, td, sources } from jobs/props.py (method = 'props')
let propsBlend = 0.85;           // weight on the props projection when a player has one (CLI default --props 0.85)
const gameLogCache = new Map();  // player_id -> rows of player_game_stats
let sortKey = "mean", sortAsc = false;
let building = false;
let customized = false;          // user changed a preset-controlled setting
let buildProg = { k: 0, total: 1 };

function setStatus(msg, err) { statusEl.textContent = msg; statusEl.classList.toggle("error", !!err); }
const f1 = (v) => v == null || Number.isNaN(+v) ? "–" : Number(v).toFixed(1);
const f2 = (v) => v == null || Number.isNaN(+v) ? "–" : Number(v).toFixed(2);
const pct = (v) => v == null || Number.isNaN(+v) ? "–" : Math.round(Number(v) * 100) + "%";
const money = (v) => "$" + Number(v).toLocaleString();
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const posCls = (pos) => ["QB", "RB", "WR", "TE"].includes(pos) ? pos : "DST";
const kickoff = (p) => { const m = /(\d{1,2}:\d{2}[AP]M)/.exec(p.game_info || ""); return m ? m[1].replace(/^0/, "").replace(/([AP])M/, " $1M") : ""; };
const injTag = (p) => {
  const rep = p.injury_report ? { Out: "OUT", Doubtful: "D", Questionable: "Q" }[p.injury_report] || "" : "";
  const SEV = { OUT: 3, IR: 3, O: 3, D: 2, Q: 1 }, site = (p.status || "").toUpperCase();
  const s = (SEV[site] || 0) >= (SEV[rep] || 0) ? site : rep;   // most severe of the site tag and the official report
  if (s) return { t: s, out: ["OUT", "O", "IR"].includes(s), title: [p.injury_report ? `Official report: ${p.injury_report}` : "", p.primary_injury || "", p.practice_status || ""].filter(Boolean).join(" · ") };
  if (p.practice_status && /Did Not|Limited/.test(p.practice_status)) return { t: p.practice_status.startsWith("Did") ? "DNP" : "LP", out: false, title: `Practice: ${p.practice_status}${p.primary_injury ? ` (${p.primary_injury})` : ""} — no game designation yet` };
  return null;
};

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

// ---------------------------------------------------------------- persistence (per viewer, per slate)
const saveKey = () => slate ? "gs_picks_" + slate.slate_key : null;
let saveTimer = null, pendingSave = null;
function savePicks() {
  const k = saveKey(); if (!k) return;
  pendingSave = { k, v: JSON.stringify({ locks: [...locks], excludes: [...excludes], overrides: [...overrides], ownOverrides: [...ownOverrides], minExp: [...minExp], maxExp: [...maxExpP], stackRules: [...stackRules] }) };
  clearTimeout(saveTimer);
  saveTimer = setTimeout(flushPicks, 250);
}
function flushPicks() { clearTimeout(saveTimer); if (pendingSave) { GS.store.set(pendingSave.k, pendingSave.v); pendingSave = null; } }
function restorePicks() {
  const k = saveKey(); if (!k) return;
  try {
    const s = JSON.parse(GS.store.get(k) || "null"); if (!s) return;
    const ok = (id) => byId.has(id);
    (s.locks || []).filter(ok).forEach(id => locks.add(id));
    (s.excludes || []).filter(ok).forEach(id => excludes.add(id));
    (s.overrides || []).filter(([id]) => ok(id)).forEach(([id, v]) => overrides.set(id, v));
    (s.ownOverrides || []).filter(([id]) => ok(id)).forEach(([id, v]) => ownOverrides.set(id, v));
    (s.minExp || []).filter(([id]) => ok(id)).forEach(([id, v]) => minExp.set(id, v));
    (s.maxExp || []).filter(([id]) => ok(id)).forEach(([id, v]) => maxExpP.set(id, v));
    (Array.isArray(s.stackRules) ? s.stackRules : []).forEach(e => { if (Array.isArray(e) && typeof e[0] === "string" && e[1] && typeof e[1] === "object") stackRules.set(e[0], { min: +e[1].min || 0, max: e[1].max == null ? null : +e[1].max }); });
  } catch (e) { /* ignore */ }
}
const SETTING_IDS = ["contest", "nLineups", "stack", "bringback", "maxExp", "exclQ", "minSalary", "maxTeam", "minUniq", "rand", "fade", "maxOwn", "poolMult", "propsBlend", "blend"];
function saveSettings() { GS.store.set("gs_settings", JSON.stringify({ v: Object.fromEntries(SETTING_IDS.map(id => [id, $(id).value])), customized })); }
function restoreSettings() {
  try {
    const s = JSON.parse(GS.store.get("gs_settings") || "null"); if (!s) return false;
    SETTING_IDS.forEach(id => {
      const v = s.v?.[id]; if (v == null) return;
      const el = $(id);
      if (el.tagName === "SELECT") { if ([...el.options].some(o => o.value === String(v))) el.value = v; }
      else if (Number.isFinite(+v)) el.value = v;
    });
    customized = !!s.customized; return true;
  } catch (e) { return false; }
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
  if (!slates.length) throw new Error("No slates are available yet — check back when this week's salaries are posted.");
  const label = (s) => `${SITES[s.site]?.name || s.site} · Week ${s.week}${s.season !== new Date().getFullYear() ? " " + s.season : ""} · ${s.slate_type === "main" ? "Main slate" : (s.slate_type || "").replace(/^\w/, c => c.toUpperCase())}`;
  $("slate").replaceChildren(...slates.map(s => new Option(`${label(s)}  (${s.n_players} players)`, s.slate_id)));
}

async function loadBoard() {
  flushPicks();
  slate = slates.find(s => s.slate_id === $("slate").value);
  document.body.dataset.site = slate.site;
  setStatus("Loading players…");
  $("poolWrap").classList.add("loading");
  renderSkeleton();
  const rows = await fetchAll(supa.from("slate_board").select("*").eq("slate_id", slate.slate_id).order("site_player_id"));
  players = rows.map(r => ({ ...r, mean: r.mean == null ? null : Number(r.mean),
    p85: Number(r.p85), p15: Number(r.p15), floor: Number(r.floor), ceiling: Number(r.ceiling), stdev: Number(r.stdev),
    salary: Number(r.salary), boom_prob: Number(r.boom_prob), bust_prob: Number(r.bust_prob) }));
  byId = new Map(players.map(p => [p.site_player_id, p]));
  locks.clear(); excludes.clear(); overrides.clear(); ownOverrides.clear(); minExp.clear(); maxExpP.clear(); stackRules.clear();
  lineups = []; candPool = []; lastExposure = new Map(); poolExposure = new Map(); sim = null;
  restorePicks();
  external = new Map();
  try {
    const ext = await fetchAll(supa.from("slate_projections").select("site_player_id,mean,components").eq("slate_id", slate.slate_id).eq("method", "external").order("site_player_id"));
    ext.forEach(r => external.set(r.site_player_id, { mean: Number(r.mean), own: r.components?.ownership == null ? null : Number(r.components.ownership) }));
  } catch (e) { /* none */ }
  propsMap = new Map();
  try {
    const pr = await fetchAll(supa.from("slate_projections").select("site_player_id,mean,components").eq("slate_id", slate.slate_id).eq("method", "props").order("site_player_id"));
    pr.forEach(r => propsMap.set(r.site_player_id, { mean: Number(r.mean), lines: r.components?.lines || {}, td: r.components?.td_prob, sources: r.components?.sources || [] }));
  } catch (e) { /* none */ }
  $("propsWrap").style.display = propsMap.size ? "" : "none";
  if (propsMap.size) { $("propsSrc").textContent = `Market lines for ${propsMap.size} players`; propsBlend = Math.max(0, Math.min(100, +$("propsBlend").value || 0)) / 100; } else propsBlend = 0;
  $("blendWrap").style.display = external.size ? "" : "none";
  if (external.size) { $("blendSrc").textContent = `${external.size} players`; blend = (+$("blend").value || 0) / 100; } else blend = 0;
  estimateOwnership();
  $("poolWrap").classList.remove("loading");
  renderSlateMeta();
  renderResults();
  render();
  $("generate").disabled = false;
  loadSim().catch(e => { console.warn("sim matrix unavailable", e); renderSlateMeta(); render(); });
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
  // per-player percentiles straight from the stored draws (25th..99th)
  const qs = [0.25, 0.5, 0.75, 0.85, 0.95, 0.99];
  players.forEach(p => {
    const a = index.get(p.site_player_id);
    if (!a) return;
    const sorted = Float32Array.from(a).sort();
    p.q = Object.fromEntries(qs.map(q => ["q" + Math.round(q * 100), sorted[Math.min(sorted.length - 1, Math.floor(q * sorted.length))]]));
  });
  renderSlateMeta();
  render();
}

function lineupSim(ids) {
  if (!sim) return null;
  const tot = new Float32Array(sim.n);
  let covered = 0;
  ids.forEach(id => { const a = sim.index.get(id); if (a) { covered++; const f = simScale(byId.get(id)); for (let i = 0; i < sim.n; i++) tot[i] += a[i] * f; }
    else { const m = proj(byId.get(id)); for (let i = 0; i < sim.n; i++) tot[i] += m; } });
  const sorted = Float32Array.from(tot).sort();
  const q = (p) => sorted[Math.min(sim.n - 1, Math.floor(p * sim.n))];
  return { p10: q(0.10), p50: q(0.50), p90: q(0.90), p98: q(0.98), covered };
}

function proj(p) {
  if (overrides.has(p.site_player_id)) return overrides.get(p.site_player_id);
  const ext = external.get(p.site_player_id);
  let own_ = p.mean ?? 0;
  const pr = propsMap.get(p.site_player_id);
  if (pr && propsBlend > 0 && p.mean != null) own_ = (1 - propsBlend) * own_ + propsBlend * pr.mean;   // same blend as build_lineups --props
  if (ext && blend > 0) return p.mean == null ? ext.mean : (1 - blend) * own_ + blend * ext.mean;
  return own_;
}
// factor that maps a player's stored sim draws onto his current projection (override or props blend), like the CLI rescales
function simScale(p) {
  const base = p.mean;
  if (base == null || base <= 0.5) return 1;
  return Math.min(4, Math.max(0.25, proj(p) / base));
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

// ---------------------------------------------------------------- slate header
function renderSlateMeta() {
  if (!slate) return;
  const chips = [];
  const times = players.map(kickoff).filter(Boolean);
  const games = new Set(players.map(p => p.game_id).filter(Boolean)).size;
  chips.push(`<span class="badge">${games} games</span>`);
  if (players.length) {
    const first = players.map(p => p.game_info).filter(Boolean).sort((a, b) => new Date(a.replace(/^.*? /, "").replace(" ET", "")) - new Date(b.replace(/^.*? /, "").replace(" ET", "")))[0];
    const t = first ? kickoff({ game_info: first }) : times[0];
    if (t) chips.push(`<span class="badge" title="First kickoff">${icon("clock")} Locks ${t} ET</span>`);
  }
  const sm = slate.sim_meta;
  if (sim) chips.push(`<span class="badge ok" title="Every lineup is scored across the same simulated games, so stacks and correlation are priced in"><span class="dot"></span>${(sm?.n_sims || sim.n).toLocaleString()} simulations${sm?.run_at ? " · updated " + relTime(sm.run_at) : ""}</span>`);
  else if (sm?.url) chips.push(`<span class="badge"><span class="spinner" style="width:10px;height:10px;border-width:1.5px"></span> Loading simulations…</span>`);
  else chips.push(`<span class="badge warn">Summary projections only</span>`);
  if (propsMap.size) chips.push(`<span class="badge info" title="Sportsbook and prediction-market player props blended into projections">Market lines · ${propsMap.size} players</span>`);
  chips.push(`<span class="badge owner-flag" title="Ownership model">${ownModel.fitted ? "Ownership fitted" : "Ownership heuristic"}</span>`);
  $("slateMeta").innerHTML = chips.join("");
}
function relTime(iso) {
  const d = new Date(iso), s = (Date.now() - d) / 1000;
  if (s < 3600) return Math.max(1, Math.round(s / 60)) + " min ago";
  if (s < 86400) return Math.round(s / 3600) + "h ago";
  return d.toLocaleDateString(undefined, { weekday: "short", month: "short", day: "numeric" }) + " " + d.toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" });
}

// ---------------------------------------------------------------- pool table
const COLS = [
  { key: "act", label: "", cls: "act-cell", nosort: true, left: true },
  { key: "player_name", label: "Player", left: true },
  { key: "position", label: "Pos", left: true, cls: "col-opt" },
  { key: "salary", label: "Salary", fmt: money },
  { key: "mean", label: "Proj", title: "Projected fantasy points — click to edit. Your edits carry into every lineup." },
  { key: "value", label: "Value", title: "Projected points per $1,000 of salary", cls: "col-opt" },
  { key: "own", label: "Own%", title: "Projected ownership — click to edit" , cls: "col-opt" },
  { key: "floor", label: "Floor", fmt: f1, set: "core", title: "10th percentile outcome", cls: "col-opt" },
  { key: "ceiling", label: "Ceiling", fmt: f1, set: "core", title: "90th percentile outcome", cls: "col-opt" },
  { key: "boom_prob", label: "Boom", fmt: pct, set: "core", title: "Chance of a big game for his salary", cls: "col-opt" },
  { key: "bust_prob", label: "Bust", fmt: pct, set: "core", title: "Chance of a dud for his salary", cls: "col-opt" },
  { key: "q25", label: "25th", sim: true, set: "range", cls: "col-opt" }, { key: "q50", label: "Median", sim: true, set: "range", cls: "col-opt" },
  { key: "q75", label: "75th", sim: true, set: "range", cls: "col-opt" }, { key: "q85", label: "85th", sim: true, set: "range", cls: "col-opt" },
  { key: "q95", label: "95th", sim: true, set: "range", cls: "col-opt" }, { key: "q99", label: "99th", sim: true, set: "range", cls: "col-opt" },
  ...[["attempts", "Pass att"], ["passing_yards", "Pass yds"], ["passing_tds", "Pass TD"], ["interceptions", "INT"], ["carries", "Rush att"],
      ["rushing_yards", "Rush yds"], ["rushing_tds", "Rush TD"], ["targets", "Tgt"], ["receptions", "Rec"], ["receiving_yards", "Rec yds"], ["receiving_tds", "Rec TD"]]
    .map(([k, l]) => ({ key: "st_" + k, stat: k, label: l, set: "stats", cls: "col-opt", title: "Average in the simulations" })),
  { key: "minExp", label: "Min %", set: "port", title: "Force this player into at least this share of lineups. Blank = none.", cls: "col-opt" },
  { key: "maxExp", label: "Max %", set: "port", title: "Cap this player's share of lineups. Blank = the global max exposure.", cls: "col-opt" },
  { key: "exp", label: "Exposure", set: "port", title: "Share of your last build", cls: "col-opt" },
  { key: "lev", label: "Leverage", set: "port", title: "Exposure minus projected ownership. Positive = you're over the field.", cls: "col-opt" },
  { key: "pool", label: "Pool %", set: "port", title: "How often the player appears across the whole candidate pool — the sims' own preference.", cls: "col-opt" },
];
const activeCols = () => COLS.filter(c => (!c.set || c.set === colSet) && (!c.sim || sim));

function visiblePlayers() {
  const pos = $("posFilter").value, q = $("search").value.trim().toLowerCase();
  return players.filter(p => (pos === "ALL" || p.position === pos) &&
    (!q || p.player_name.toLowerCase().includes(q) || (p.team || "").toLowerCase().includes(q)) &&
    (!onlyMine || locks.has(p.site_player_id) || excludes.has(p.site_player_id) || overrides.has(p.site_player_id) || ownOverrides.has(p.site_player_id) || minExp.has(p.site_player_id) || maxExpP.has(p.site_player_id)));
}

function renderSkeleton() {
  const tbody = document.querySelector("#pool tbody");
  document.querySelector("#pool thead").innerHTML = "";
  tbody.innerHTML = Array.from({ length: 12 }, () => `<tr><td colspan="8"><div class="skeleton" style="height:18px;width:${40 + Math.random() * 50}%"></div></td></tr>`).join("");
}

function render() {
  renderPicks();
  const smb = $("stackModeBar"); if (smb) smb.hidden = view !== "stacks";
  const cs = $("colSeg"); if (cs) cs.hidden = view === "stacks";
  const pc = $("posChips"); if (pc) pc.hidden = view === "stacks";
  if (view === "stacks") { renderStacks(); return; }
  if (view !== "players") return;
  const thead = document.querySelector("#pool thead"), tbody = document.querySelector("#pool tbody");
  const cols = activeCols();
  const tr = document.createElement("tr");
  cols.forEach(c => {
    const th = document.createElement("th"); th.textContent = c.label; if (c.title) th.title = c.title;
    if (c.left) th.classList.add("left");
    if (c.cls) th.classList.add(...c.cls.split(" "));
    if (!c.nosort) {
      th.classList.add("sortable");
      if (c.key === sortKey) th.classList.add("sorted", sortAsc ? "asc" : "desc");
      th.addEventListener("click", () => { if (sortKey === c.key) sortAsc = !sortAsc; else { sortKey = c.key; sortAsc = !!c.left; } render(); });
    }
    tr.appendChild(th);
  });
  thead.replaceChildren(tr);

  const rows = visiblePlayers().map(p => ({ p, mean: proj(p), value: p.salary ? proj(p) / (p.salary / 1000) : 0, own: own(p),
    exp: 100 * (lastExposure.get(p.site_player_id) || 0), lev: 100 * (lastExposure.get(p.site_player_id) || 0) - own(p),
    pool: 100 * (poolExposure.get(p.site_player_id) || 0),
    minExp: minExp.get(p.site_player_id), maxExp: maxExpP.get(p.site_player_id) }));
  const kv = (r) => sortKey === "mean" ? r.mean : sortKey === "value" ? r.value : sortKey === "own" ? r.own
    : sortKey === "exp" ? r.exp : sortKey === "lev" ? r.lev : sortKey === "pool" ? r.pool : sortKey === "minExp" ? r.minExp : sortKey === "maxExp" ? r.maxExp
    : sortKey.startsWith("q") ? (r.p.q ? r.p.q[sortKey] * simScale(r.p) : null)
    : sortKey.startsWith("st_") ? (r.p.components?.stats?.[sortKey.slice(3)] ?? null) : r.p[sortKey];
  rows.sort((a, b) => {
    const ka = kv(a), kb = kv(b);
    let c = (ka == null) - (kb == null) || (typeof ka === "number" ? ka - kb : String(ka ?? "").localeCompare(String(kb ?? "")));
    return sortAsc ? c : -c;
  });
  const maxVal = Math.max(1, ...rows.map(r => r.value));
  const frag = document.createDocumentFragment();
  rows.forEach(({ p, mean, value, exp, lev, pool: poolPct }) => {
    const r = document.createElement("tr");
    const id = p.site_player_id;
    if (locks.has(id)) r.classList.add("locked");
    if (excludes.has(id)) r.classList.add("excluded");
    cols.forEach(c => {
      const td = document.createElement("td");
      if (c.left) td.classList.add("left");
      if (c.cls) td.classList.add(...c.cls.split(" "));
      if (c.key === "act") {
        td.innerHTML = `<button class="act lock${locks.has(id) ? " on" : ""}" data-act="lock" aria-pressed="${locks.has(id)}" title="${locks.has(id) ? "Unlock" : "Lock into every lineup"}" aria-label="Lock ${esc(p.player_name)}">${icon("lock")}</button><button class="act excl${excludes.has(id) ? " on" : ""}" data-act="excl" aria-pressed="${excludes.has(id)}" title="${excludes.has(id) ? "Include again" : "Exclude from all lineups"}" aria-label="Exclude ${esc(p.player_name)}">${icon("ban")}</button>`;
        td.querySelectorAll(".act").forEach(b => b.addEventListener("click", () => toggle(b.dataset.act, id)));
      } else if (c.key === "player_name") {
        const inj = injTag(p);
        td.innerHTML = `<div class="pcell"><span class="pname" data-card="${esc(id)}">${esc(p.player_name)}${inj ? ` <span class="inj${inj.out ? " out" : ""}" title="${esc(inj.title)}">${esc(inj.t)}</span>` : ""}</span>
          <span class="psub"><b>${esc(p.team || "")}</b> ${p.opponent ? "vs " + esc(p.opponent) : ""}${kickoff(p) ? " · " + kickoff(p) : ""}</span></div>`;
        td.querySelector(".pname").addEventListener("click", () => openCard(id));
      } else if (c.key === "position") {
        td.innerHTML = `<span class="pos ${posCls(p.position)}">${esc(p.position)}</span>`;
      } else if (c.key === "mean") {
        const inp = document.createElement("input"); inp.type = "number"; inp.step = "0.1"; inp.className = "edit proj";
        inp.value = mean.toFixed(1); inp.setAttribute("aria-label", "Projection for " + p.player_name);
        if (overrides.has(id)) { inp.classList.add("overridden"); inp.title = `Your projection (model: ${f1(p.mean)})`; }
        inp.addEventListener("change", () => { const v = parseFloat(inp.value); const back = overrides.has(id) ? (v === (p.mean ?? 0) || v === +(p.mean ?? 0).toFixed(1)) : v === +mean.toFixed(1);
          if (Number.isNaN(v) || back) overrides.delete(id); else overrides.set(id, v); picksChanged(); });   // compare with what was shown, so a 0.01 nudge always registers
        td.appendChild(inp);
      } else if (c.key === "own") {
        const inp = document.createElement("input"); inp.type = "number"; inp.step = "1"; inp.min = "0"; inp.max = "100"; inp.className = "edit";
        inp.value = Math.round(own(p)); inp.setAttribute("aria-label", "Ownership for " + p.player_name);
        if (ownOverrides.has(id)) inp.classList.add("overridden");
        inp.addEventListener("change", () => { const v = parseFloat(inp.value); if (Number.isNaN(v)) ownOverrides.delete(id); else ownOverrides.set(id, v); picksChanged(); });
        td.appendChild(inp);
      } else if (c.key === "value") {
        td.innerHTML = `<span class="valbar"><i style="width:${Math.round(28 * Math.max(0, value) / maxVal)}px"></i>${f2(value)}</span>`;
      } else if (c.key === "minExp" || c.key === "maxExp") {
        const m = c.key === "minExp" ? minExp : maxExpP;
        const inp = document.createElement("input"); inp.type = "number"; inp.step = "5"; inp.min = "0"; inp.max = "100"; inp.className = "edit"; inp.placeholder = "–";
        if (m.has(id)) { inp.value = Math.round(100 * m.get(id)); inp.classList.add("overridden"); }
        inp.addEventListener("change", () => { const v = parseFloat(inp.value); if (Number.isNaN(v)) m.delete(id); else m.set(id, Math.max(0, Math.min(100, v)) / 100); savePicks(); setTimeout(candPool.length ? selectFromPool : render, 0); });
        td.appendChild(inp);
      } else if (c.key === "exp") {
        td.textContent = lastExposure.size ? Math.round(exp) + "%" : "–"; if (!lastExposure.size) td.classList.add("dim");
      } else if (c.key === "lev") {
        td.textContent = lastExposure.size ? (lev > 0 ? "+" : "") + Math.round(lev) : "–";
        if (lastExposure.size && Math.abs(lev) >= 10) td.classList.add(lev > 0 ? "pos-diff" : "neg-diff");
        if (!lastExposure.size) td.classList.add("dim");
      } else if (c.key === "pool") {
        td.textContent = poolExposure.size ? Math.round(poolPct) + "%" : "–"; if (!poolExposure.size) td.classList.add("dim");
      } else if (c.stat) {
        const v = p.components?.stats?.[c.stat];
        td.textContent = v == null ? "–" : (/_tds$|interceptions/.test(c.stat) ? Number(v).toFixed(2) : Number(v).toFixed(1));
        if (v == null) td.classList.add("dim");
      } else if (c.key.startsWith("q")) {
        td.textContent = p.q ? f1(p.q[c.key] * simScale(p)) : "–";
      } else {
        td.textContent = c.fmt ? c.fmt(p[c.key]) : (p[c.key] ?? "–");
      }
      r.appendChild(td);
    });
    frag.appendChild(r);
  });
  if (!rows.length) {
    const r = document.createElement("tr");
    r.innerHTML = `<td colspan="${cols.length}" class="left"><div class="empty" style="padding:28px"><h3>No players match</h3><p>${onlyMine ? "You haven't locked, excluded or edited anyone yet." : "Try a different position or search."}</p></div></td>`;
    frag.appendChild(r);
  }
  tbody.replaceChildren(frag);
  const nSim = players.filter(p => p.method === "sim").length;
  setStatus(`${rows.length} of ${players.length} players` + (nSim ? "" : " · baseline projections") + (colSet === "range" && !sim ? " · simulated ranges appear once the simulations load" : "")
    + (colSet === "port" && !lastExposure.size ? " · build lineups to fill exposure and leverage" : "")
    + (colSet === "stats" && !players.some(p => p.components?.stats) ? " · stat lines appear after the next simulation run" : ""));
  renderObjHint();
}

function renderObjHint() {
  $("objHint").textContent = ($("contest").value === "cash"
    ? "Cash lineups maximize projected points with a tilt toward safe floors (0.8 × projection + 0.2 × floor), then keep the lineups with the best simulated median."
    : "Tournament lineups maximize a ceiling-tilted score (0.6 × projection + 0.4 × 85th percentile) with randomness for variety and your stacking rules, then keep the lineups with the best simulated 90th percentile.")
    + (GS.isOwner() ? (ownModel.fitted ? ` Ownership model fitted to ${ownModel.fitted.slates} contest(s).` : " Ownership is a heuristic.") : "");
}

function toggle(kind, id) {
  const set = kind === "lock" ? locks : excludes, other = kind === "lock" ? excludes : locks;
  if (set.has(id)) set.delete(id); else { set.add(id); other.delete(id); }
  picksChanged();
}
// DraftKings community guidelines: a third-party optimizer may only build after the user has
// locked, excluded, set exposure on, or edited the projection of at least N players.
function editedPlayers() { return new Set([...locks, ...excludes, ...overrides.keys(), ...minExp.keys(), ...maxExpP.keys()]).size; }
function gateNeed() { const need = (slate && SITES[slate.site]?.minEdits) || 0; return Math.max(0, need - editedPlayers()); }
function renderGate() {
  const el = $("buildGate"); if (!el || !slate) return;
  const need = SITES[slate.site]?.minEdits || 0, left = gateNeed();
  el.hidden = !left;
  if (left) el.innerHTML = `${icon("info")}<span>${esc(SITES[slate.site].name)} requires you to lock, exclude, set Min/Max % on, or edit the projection of <b>at least ${need} players</b> before building (${need - left} of ${need} done). Even a 0.01 projection change counts.</span>`;
  $("generate").classList.toggle("gated", !!left);
}

function picksChanged() { estimateOwnership(); savePicks(); render(); }

function renderPicks() {
  const bar = $("picksBar");
  const n = locks.size + excludes.size + overrides.size + stackRules.size;
  $("mineCount").textContent = n + ownOverrides.size + minExp.size + maxExpP.size;
  renderGate();
  if (!n) { bar.hidden = true; return; }
  bar.hidden = false;
  const tag = (cls, id, label, what) => `<span class="pick-tag ${cls}">${label}<button data-rm="${what}" data-id="${esc(id)}" aria-label="Remove">${icon("x")}</button></span>`;
  bar.innerHTML = [...locks].map(id => tag("lock", id, `${icon("lock")} ${esc(byId.get(id)?.player_name)}`, "lock"))
    .concat([...excludes].map(id => tag("excl", id, `${icon("ban")} ${esc(byId.get(id)?.player_name)}`, "excl")))
    .concat([...overrides].map(([id, v]) => tag("", id, `${esc(byId.get(id)?.player_name)} → ${f1(v)}`, "ovr")))
    .concat([...stackRules].map(([k, r]) => tag("", k, `${esc(ruleLabel(k))} ${r.min ? Math.round(100 * r.min) + "%" : "0"}–${r.max != null ? Math.round(100 * r.max) + "%" : "100%"}`, "rule"))).join("")
    + `<button class="btn btn-ghost btn-sm" id="clearPicks">Clear all</button>`;
  bar.querySelectorAll("[data-rm]").forEach(b => b.addEventListener("click", () => {
    const id = b.dataset.id; ({ lock: locks, excl: excludes, ovr: overrides, rule: stackRules })[b.dataset.rm].delete(id); picksChanged();
    if (b.dataset.rm === "rule" && candPool.length) selectFromPool();
  }));
  $("clearPicks").addEventListener("click", () => { locks.clear(); excludes.clear(); overrides.clear(); ownOverrides.clear(); minExp.clear(); maxExpP.clear(); stackRules.clear(); picksChanged(); GS.toast("Cleared your locks, excludes and edits."); });
}

// ---------------------------------------------------------------- team stacks view (teams / games, with exposure rules)
function stackExposure(key) {
  if (!lineups.length) return null;
  return lineups.filter(L => ruleKeys(qbOf(L)).includes(key)).length / lineups.length;
}
function ruleInputs(key) {
  const r = stackRules.get(key) || {};
  const v = (x) => x == null ? "" : Math.round(100 * x);
  return [`<input class="edit${r.min ? " overridden" : ""}" type="number" min="0" max="100" step="5" placeholder="–" data-rule="${esc(key)}" data-which="min" value="${r.min ? v(r.min) : ""}" aria-label="Minimum % for ${esc(ruleLabel(key))}">`,
          `<input class="edit${r.max != null ? " overridden" : ""}" type="number" min="0" max="100" step="5" placeholder="–" data-rule="${esc(key)}" data-which="max" value="${r.max != null ? v(r.max) : ""}" aria-label="Maximum % for ${esc(ruleLabel(key))}">`];
}
function wireRuleInputs(tbody) {
  tbody.querySelectorAll("[data-rule]").forEach(inp => inp.addEventListener("change", () => {
    const k = inp.dataset.rule, r = { ...(stackRules.get(k) || { min: 0, max: null }) };
    const v = parseFloat(inp.value);
    if (inp.dataset.which === "min") r.min = Number.isNaN(v) ? 0 : Math.max(0, Math.min(100, v)) / 100;
    else r.max = Number.isNaN(v) ? null : Math.max(0, Math.min(100, v)) / 100;
    if (r.max != null && r.min > r.max) r.min = r.max;
    if (!r.min && r.max == null) stackRules.delete(k); else stackRules.set(k, r);
    savePicks(); syncControls();
    if (candPool.length) selectFromPool(); else render();
  }));
}

function renderStacks() {
  const thead = document.querySelector("#pool thead"), tbody = document.querySelector("#pool tbody");
  const q = $("search").value.trim().toLowerCase();
  const modeBar = `<div class="seg seg-compact stack-mode" role="group" aria-label="Group stacks by"><button type="button" data-sm="teams" aria-pressed="${stackMode === "teams"}">By team</button><button type="button" data-sm="games" aria-pressed="${stackMode === "games"}">By game</button></div>`;
  const chip = (m) => `<span class="stack-p"><span class="pos ${posCls(m.position)}" style="min-width:24px;height:16px;font-size:9.5px;margin-right:4px">${esc(m.position)}</span><b>${esc(m.player_name)}</b> ${f1(proj(m))}</span>`;
  const expCell = (x) => `<td class="${x == null ? "dim" : ""}">${x == null ? "–" : Math.round(100 * x) + "%"}</td>`;
  const live = (p) => (p.mean || 0) > 0 && !excludes.has(p.site_player_id);

  if (stackMode === "games") {
    const gids = [...new Set(players.map(p => p.game_id).filter(Boolean))];
    const rows = gids.map(gid => {
      const ps = players.filter(p => p.game_id === gid && live(p) && p.position !== "DST").sort((a, b) => proj(b) - proj(a));
      const qbs = [...new Set(ps.map(p => p.team))].map(t => ps.find(p => p.team === t && p.position === "QB")).filter(Boolean);
      return { gid, label: gameLabel(gid), time: kickoff(ps[0] || {}), ps, qbs, top: ps.filter(p => p.position !== "QB").slice(0, 4), proj: ps.slice(0, 8).reduce((s, p) => s + proj(p), 0) };
    }).filter(r => !q || r.label.toLowerCase().includes(q)).sort((a, b) => b.proj - a.proj);
    thead.innerHTML = `<tr><th class="left">Game</th><th class="left col-opt">Quarterbacks</th><th class="left">Top players</th><th title="Sum of the 8 highest projections in the game">Top-8 proj</th><th title="Share of your lineups whose QB stack comes from this game">Stack exp</th><th title="Force at least this share of lineups to stack this game">Min %</th><th title="Cap the share of lineups stacking this game">Max %</th></tr>`;
    tbody.innerHTML = rows.map(r => { const [mi, ma] = ruleInputs("G:" + r.gid); return `<tr>
      <td class="left"><div class="pcell"><span class="pname" style="cursor:default">${esc(r.label)}</span><span class="psub">${esc(r.time)}</span></div></td>
      <td class="left col-opt"><div class="stack-players">${r.qbs.map(chip).join("")}</div></td>
      <td class="left"><div class="stack-players">${r.top.map(chip).join("")}</div></td>
      <td><b>${f1(r.proj)}</b></td>${expCell(stackExposure("G:" + r.gid))}<td>${mi}</td><td>${ma}</td></tr>`; }).join("");
    wireRuleInputs(tbody);
    setStatus(`${rows.length} games · Min % / Max % control how many lineups take their QB stack from each game` + (lineups.length ? "" : " · exposures fill in after a build"));
  } else {
    const teams = [...new Set(players.filter(p => p.position === "QB" && live(p)).map(p => p.team))];
    const rows = teams.map(team => {
      const qb = players.filter(p => p.team === team && p.position === "QB" && live(p)).sort((a, b) => proj(b) - proj(a))[0];
      const mates = players.filter(p => p.team === team && ["WR","TE"].includes(p.position) && live(p)).sort((a, b) => proj(b) - proj(a));
      const opp = players.filter(p => p.team === qb.opponent && ["RB","WR","TE"].includes(p.position) && (p.mean || 0) > 0).sort((a, b) => proj(b) - proj(a)).slice(0, 2);
      const two = mates.slice(0, 2);
      return { team, qb, mates, opp, two, proj: proj(qb) + two.reduce((s, m) => s + proj(m), 0), own: own(qb) + two.reduce((s, m) => s + own(m), 0) };
    }).filter(r => !q || r.qb.player_name.toLowerCase().includes(q) || r.team.toLowerCase().includes(q)).sort((a, b) => b.proj - a.proj);
    thead.innerHTML = `<tr><th class="left">Team · QB</th><th>QB proj</th><th class="left">Top pass catchers</th><th>QB + 2 proj</th><th title="Share of your lineups whose QB is from this team">Stack exp</th><th title="Force at least this share of lineups to stack this team">Min %</th><th title="Cap the share of lineups stacking this team">Max %</th><th></th><th class="col-opt">QB + 2 own</th><th class="left col-opt">Bring-back options</th></tr>`;
    tbody.innerHTML = rows.map(r => {
      const locked = locks.has(r.qb.site_player_id) && r.two.every(m => locks.has(m.site_player_id));
      const [mi, ma] = ruleInputs("T:" + r.team);
      return `<tr>
        <td class="left"><div class="pcell"><span class="pname" data-card="${esc(r.qb.site_player_id)}"><b style="color:var(--muted);font-weight:700">${esc(r.team)}</b> ${esc(r.qb.player_name)}</span><span class="psub">vs ${esc(r.qb.opponent)}${kickoff(r.qb) ? " · " + kickoff(r.qb) : ""}</span></div></td>
        <td>${f1(proj(r.qb))}</td>
        <td class="left"><div class="stack-players">${r.mates.slice(0, 3).map(chip).join("")}</div></td>
        <td><b>${f1(r.proj)}</b></td>
        ${expCell(stackExposure("T:" + r.team))}<td>${mi}</td><td>${ma}</td>
        <td><button class="btn btn-sm ${locked ? "btn-primary" : ""}" data-stack="${esc(r.qb.site_player_id)}" title="Lock this QB and his top two pass catchers into every lineup">${icon("lock")} ${locked ? "Locked" : "Lock"}</button></td>
        <td class="col-opt">${Math.round(r.own)}%</td>
        <td class="left col-opt"><div class="stack-players">${r.opp.map(chip).join("")}</div></td></tr>`;
    }).join("") || `<tr><td colspan="10"><div class="empty"><h3>No quarterbacks match</h3></div></td></tr>`;
    tbody.querySelectorAll("[data-card]").forEach(el => el.addEventListener("click", () => openCard(el.dataset.card)));
    tbody.querySelectorAll("[data-stack]").forEach(b => b.addEventListener("click", () => {
      const r = rows.find(x => x.qb.site_player_id === b.dataset.stack);
      const ids = [r.qb, ...r.two].map(p => p.site_player_id);
      const all = ids.every(id => locks.has(id));
      ids.forEach(id => { if (all) locks.delete(id); else { locks.add(id); excludes.delete(id); } });
      picksChanged();
      GS.toast(all ? `Unlocked the ${r.team} stack.` : `Locked ${r.qb.player_name} + ${r.two.map(m => m.player_name).join(" + ")}.`, all ? "" : "ok");
    }));
    wireRuleInputs(tbody);
    setStatus(`${rows.length} teams · Min % / Max % control how many lineups stack each team's QB (with your stacking rule)` + (lineups.length ? "" : " · exposures fill in after a build"));
  }
  const bar = $("stackModeBar"); if (bar) { bar.innerHTML = modeBar; bar.hidden = false;
    bar.querySelectorAll("[data-sm]").forEach(b => b.addEventListener("click", () => { stackMode = b.dataset.sm; render(); })); }
}

// ---------------------------------------------------------------- optimizer
function buildLP(pool, opts, prior, exposureBlocked, forced = null) {
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
  if (forced && byId.has(forced)) st.push({ name: "stand", vars: [{ name: "x" + forced, coef: 1 }], bnds: { type: glpk.GLP_FX, lb: 1, ub: 1 } });
  if (opts.maxOwn > 0) st.push({ name: "maxown", vars: pool.map(p => ({ name: "x" + p.site_player_id, coef: own(p) })), bnds: { type: glpk.GLP_UP, lb: 0, ub: opts.maxOwn } });

  // locks / exposure blocks as rows (GLPK resets column bounds to [0,1] for binaries)
  pool.forEach(p => {
    const id = p.site_player_id;
    if (locks.has(id)) st.push({ name: "lock_" + id, vars: [{ name: "x" + id, coef: 1 }], bnds: { type: glpk.GLP_FX, lb: 1, ub: 1 } });
    else if (exposureBlocked.has(id)) st.push({ name: "block_" + id, vars: [{ name: "x" + id, coef: 1 }], bnds: { type: glpk.GLP_FX, lb: 0, ub: 0 } });
  });
  return { name: "lineup", objective: { direction: glpk.GLP_MAX, name: "obj", vars }, subjectTo: st, binaries: vars.map(v => v.name) };
}

function gauss() { let u = 0, v = 0; while (!u) u = Math.random(); while (!v) v = Math.random(); return Math.sqrt(-2 * Math.log(u)) * Math.cos(2 * Math.PI * v); }

function setProgress(k, total) {
  const bar = $("progress");
  if (total == null) { bar.hidden = true; $("generate").querySelector(".gen-label").textContent = buildLabel(); return; }
  buildProg = { k, total };
  bar.hidden = false; bar.firstElementChild.style.width = Math.round(100 * k / total) + "%";
  $("generate").querySelector(".gen-label").textContent = `Building… ${k} / ${total}`;
  $("mBuild").textContent = `Building… ${Math.round(100 * k / total)}%`;
}

async function generate() {
  if (building || !slate) return;
  const left = gateNeed();
  if (left) {
    renderGate(); $("buildGate")?.scrollIntoView({ block: "nearest" });
    GS.toast(`${SITES[slate.site].name} requires your input on ${left} more player${left > 1 ? "s" : ""} before building: lock, exclude, set Min/Max %, or edit a projection.`, "error");
    return;
  }
  const contest = $("contest").value, n = Math.max(1, Math.min(150, +$("nLineups").value || 1));
  const opts = {
    minSalary: +$("minSalary").value || 0, maxTeam: +$("maxTeam").value || 4,
    stack: contest === "gpp" ? +$("stack").value : 0, bringback: contest === "gpp" && $("bringback").value === "1",
    maxExp: (+$("maxExp").value || 100) / 100, minUniq: +$("minUniq").value || 1,
    rand: contest === "gpp" ? (+$("rand").value || 0) / 100 : 0, exclQ: $("exclQ").value === "1",
    fade: contest === "gpp" ? (+$("fade").value || 0) / 100 : 0,
    maxOwn: contest === "gpp" ? (+$("maxOwn").value || 0) : 0,
    poolMult: sim ? Math.max(1, Math.min(5, +$("poolMult").value || 1)) : 1,
  };
  const nCand = Math.min(300, n * opts.poolMult);
  const SEV = { OUT: 3, IR: 3, O: 3, D: 2, Q: 1 };
  const eff = (p) => { const a = (p.status || "").toUpperCase(), b = ({ Out: "OUT", Doubtful: "D", Questionable: "Q" }[p.injury_report] || ""); return (SEV[a] || 0) >= (SEV[b] || 0) ? a : b; };
  const pool = players.filter(p => !excludes.has(p.site_player_id) && p.mean != null && proj(p) > 0 &&
    !["OUT","IR","O"].includes(eff(p)) &&
    !(opts.exclQ && ["Q","D"].includes(eff(p))));
  if (!pool.some(p => p.position === "DST")) { GS.toast("This slate has no defenses with projections yet.", "error"); return; }

  building = true; buildProg = { k: 0, total: nCand }; document.body.classList.add("building");
  lineups = []; $("generate").disabled = true; $("mBuild").disabled = true;
  closeSettings();
  setView("lineups");
  const usage = new Map(); const blocked = new Set();
  const capFor = (id) => Math.ceil((maxExpP.has(id) ? maxExpP.get(id) : opts.maxExp) * nCand);
  // stands: players with a Min% get forced into that share of the candidates first, then the pool fills normally
  const stands = [...minExp.entries()].filter(([id, f]) => f > 0 && pool.some(p => p.site_player_id === id)).sort((a, b) => b[1] - a[1]);
  const plan = [];
  stands.forEach(([id, f]) => { for (let i = 0; i < Math.ceil(f * nCand); i++) plan.push(id); });
  // stack minimums: force the team's (or the game's two teams') best QB into that share of the candidates
  const topQB = (team) => pool.filter(p => p.position === "QB" && p.team === team).sort((a, b) => proj(b) - proj(a))[0]?.site_player_id;
  [...stackRules].filter(([, r]) => r.min > 0).forEach(([k, r]) => {
    const qbsK = k.startsWith("T:") ? [topQB(k.slice(2))] : [...new Set(pool.filter(p => p.game_id === k.slice(2)).map(p => p.team))].map(topQB);
    const ids = qbsK.filter(Boolean); if (!ids.length) return;
    for (let i = 0; i < Math.ceil(r.min * nCand); i++) plan.push(ids[i % ids.length]);
  });
  while (plan.length < nCand) plan.push(null);
  const stackUse = new Map();
  let stopped = null;
  try {
    for (let k = 0; k < nCand; k++) {
      setProgress(k, nCand);
      const forced = plan[k];
      const jitter = new Map(pool.map(p => [p.site_player_id, opts.rand ? 1 + opts.rand * gauss() * (p.stdev / Math.max(p.mean, 1)) : 1]));
      opts.score = (p) => {
        const m = proj(p);
        const base = contest === "cash" ? 0.8 * m + 0.2 * p.floor : 0.6 * m + 0.4 * (p.p85 * (m / Math.max(p.mean, 0.1)));
        // ownership fade: at 100% fade a 30%-owned player gives up ~1.8 pts of score
        return base * jitter.get(p.site_player_id) - opts.fade * 0.06 * own(p);
      };
      const lp = buildLP(pool, opts, lineups.map(L => L.ids), blocked, forced);
      const res = await glpk.solve(lp, { msglev: glpk.GLP_MSG_OFF, tmlim: 20 });
      if (![glpk.GLP_OPT, glpk.GLP_FEAS].includes(res.result.status)) {
        stopped = k;
        break;
      }
      const ids = Object.entries(res.result.vars).filter(([, v]) => v > 0.5).map(([name]) => name.slice(1));
      const L = summarize(ids); lineups.push(L);
      ids.forEach(id => { const u = (usage.get(id) || 0) + 1; usage.set(id, u); if (!locks.has(id) && u >= capFor(id)) blocked.add(id); });
      ruleKeys(qbOf(L)).forEach(key => { const u = (stackUse.get(key) || 0) + 1; stackUse.set(key, u); const mx = ruleMax(key);
        if (mx != null && u >= Math.ceil(mx * nCand)) qbIdsFor(key).forEach(id => { if (!locks.has(id)) blocked.add(id); }); });
      if (k % 10 === 9) renderBuilding(k + 1, nCand);
      await new Promise(r => setTimeout(r));
    }
    // rank by the sim metric that matters for the contest
    if (sim && lineups.length > 1) {
      const key = contest === "cash" ? "p50" : "p90";
      lineups.sort((a, b) => (b.sim?.[key] ?? b.proj) - (a.sim?.[key] ?? a.proj));
    }
    candPool = lineups.slice(); candOpts = { contest, n, maxExp: opts.maxExp };
    poolExposure = new Map();
    candPool.forEach(L => L.ids.forEach(id => poolExposure.set(id, (poolExposure.get(id) || 0) + 1 / candPool.length)));
    selectFromPool(true);
    // top-up: if the exposure caps left the selection short, solve extra candidates that respect the caps of the
    // lineups already kept (players at their cap are blocked), so max exposure is never broken to fill the count.
    const topUpBudget = (n - lineups.length) * 3 + 5;
    for (let extra = 0; lineups.length < n && extra < topUpBudget && candPool.length < 400; extra++) {
      setProgress(Math.min(nCand, nCand - 1), nCand);
      await new Promise(r => setTimeout(r));
      const used = new Map(); lineups.forEach(L => L.ids.forEach(id => used.set(id, (used.get(id) || 0) + 1)));
      const capN = (id) => Math.max(1, Math.ceil((maxExpP.has(id) ? maxExpP.get(id) : opts.maxExp) * n));
      const blockNow = new Set([...used].filter(([id, u]) => !locks.has(id) && u >= capN(id)).map(([id]) => id));
      const sUsed = new Map(); lineups.forEach(L => ruleKeys(qbOf(L)).forEach(k => sUsed.set(k, (sUsed.get(k) || 0) + 1)));
      [...sUsed].forEach(([k, u]) => { const mx = ruleMax(k); if (mx != null && u >= Math.max(1, Math.ceil(mx * n))) qbIdsFor(k).forEach(id => { if (!locks.has(id)) blockNow.add(id); }); });
      const jitter = new Map(pool.map(p => [p.site_player_id, opts.rand ? 1 + opts.rand * gauss() * (p.stdev / Math.max(p.mean, 1)) : 1]));
      opts.score = (p) => {
        const m = proj(p);
        const base = contest === "cash" ? 0.8 * m + 0.2 * p.floor : 0.6 * m + 0.4 * (p.p85 * (m / Math.max(p.mean, 0.1)));
        return base * jitter.get(p.site_player_id) - opts.fade * 0.06 * own(p);
      };
      const res = await glpk.solve(buildLP(pool, opts, candPool.map(L => L.ids), blockNow), { msglev: glpk.GLP_MSG_OFF, tmlim: 20 });
      if (![glpk.GLP_OPT, glpk.GLP_FEAS].includes(res.result.status)) break;
      const ids = Object.entries(res.result.vars).filter(([, v]) => v > 0.5).map(([name]) => name.slice(1));
      candPool.push(summarize(ids));
      selectFromPool(true);
    }
    poolExposure = new Map();
    candPool.forEach(L => L.ids.forEach(id => poolExposure.set(id, (poolExposure.get(id) || 0) + 1 / candPool.length)));
    building = false; document.body.classList.remove("building");
    selectFromPool();
    if (!lineups.length) GS.toast("No valid lineup fits these rules — try unlocking a player or loosening stacking / exposure.", "error");
    else if (lineups.length < n) GS.toast(`Built ${lineups.length} of ${n} lineups — the rules ran out of room${stopped != null ? " (uniqueness / exposure / stacking)" : ""}. Loosen max exposure or min unique players to get more.`, "error");
    else GS.toast(`${lineups.length} lineup${lineups.length > 1 ? "s" : ""} ready${sim && candPool.length > lineups.length ? `, picked from ${candPool.length} candidates by simulated ${contest === "cash" ? "median" : "ceiling"}` : ""}.`, "ok");
  } catch (e) { GS.toast("Optimizer error: " + (e.message || e), "error"); console.error(e); }
  building = false; document.body.classList.remove("building");
  setProgress(null);
  $("generate").disabled = false; $("mBuild").disabled = false; $("mBuild").textContent = buildLabel();
  $("exportBtn").disabled = !lineups.length;
}

// pick the final set from the candidate pool: Min% stands first, then best-ranked, honouring Max% caps.
// Changing Min%/Max% re-runs this instantly (no rebuild).
function selectFromPool(quiet = false) {
  if (!candPool.length || !candOpts) return;
  const n = candOpts.n, pool = candPool;
  const stands = [...minExp.entries()].filter(([id, f]) => f > 0 && pool.some(L => L.ids.includes(id))).sort((a, b) => b[1] - a[1]);
  const used = new Map(), kept = [], short = [];
  const capN = (id) => Math.max(1, Math.ceil((maxExpP.has(id) ? maxExpP.get(id) : candOpts.maxExp) * n));
  const sUsed = new Map();
  const capS = (k) => { const mx = ruleMax(k); return mx == null ? Infinity : Math.max(1, Math.ceil(mx * n)); };
  const fits = (L, need) => L.ids.every(id => locks.has(id) || id === need || (used.get(id) || 0) < capN(id)) && !L.ids.some(id => excludes.has(id))
    && ruleKeys(qbOf(L)).every(k => (sUsed.get(k) || 0) < capS(k));
  const take = (L) => { kept.push(L); L.ids.forEach(id => used.set(id, (used.get(id) || 0) + 1)); ruleKeys(qbOf(L)).forEach(k => sUsed.set(k, (sUsed.get(k) || 0) + 1)); };
  for (const [k, r] of [...stackRules].filter(([, r]) => r.min > 0).sort((a, b) => b[1].min - a[1].min)) {
    const want = Math.ceil(r.min * n);
    for (const L of pool) { if ((sUsed.get(k) || 0) >= want || kept.length >= n) break; if (!kept.includes(L) && ruleKeys(qbOf(L)).includes(k) && fits(L)) take(L); }
    if ((sUsed.get(k) || 0) < want) short.push(`${ruleLabel(k)} ${sUsed.get(k) || 0}/${want}`);
  }
  for (const [id, f] of stands) {
    const want = Math.ceil(f * n);
    for (const L of pool) { if ((used.get(id) || 0) >= want || kept.length >= n) break; if (!kept.includes(L) && L.ids.includes(id) && fits(L, id)) take(L); }
    if ((used.get(id) || 0) < want) short.push(`${byId.get(id).player_name} ${used.get(id) || 0}/${want}`);
  }
  for (const L of pool) { if (kept.length >= n) break; if (!kept.includes(L) && fits(L)) take(L); }
  lineups = kept;
  lastExposure = new Map();
  lineups.forEach(L => L.ids.forEach(id => lastExposure.set(id, (lastExposure.get(id) || 0) + 1 / lineups.length)));
  if (quiet) return;
  renderResults(); render();
  $("exportBtn").disabled = !lineups.length;
  $("luCount").textContent = lineups.length;
  if (short.length) GS.toast(`Couldn't reach your minimums: ${short.join(", ")}. Raise the candidate pool × (Fine-tune) and build again.`, "error");
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

// ---------------------------------------------------------------- lineups view
function renderBuilding(k, total) {
  $("results").innerHTML = `<div class="empty"><div class="icon-wrap"><span class="spinner"></span></div><h3>Building your lineups…</h3>
    <p>${k} of ${total} candidates solved${sim ? " — each one is scored across the simulated games" : ""}.</p></div>`;
}

function renderResults() {
  const box = $("results");
  $("luCount").textContent = lineups.length;
  if (building) { renderBuilding(buildProg.k, buildProg.total); return; }
  if (!lineups.length) {
    box.innerHTML = `<div class="empty"><div class="icon-wrap">${icon("layers")}</div><h3>No lineups yet</h3>
      <p>Choose your contest on the left, lock or exclude anyone you feel strongly about, then press <b>Build lineups</b>.</p>
      <button class="btn btn-primary" style="margin-top:16px" id="emptyBuild">Build lineups</button></div>`;
    $("emptyBuild")?.addEventListener("click", generate);
    return;
  }
  const site = SITES[slate.site];
  const N = lineups.length, avg = (f) => lineups.reduce((s, L) => s + f(L), 0) / N;
  const exposure = new Map();
  lineups.forEach(L => L.ids.forEach(id => exposure.set(id, (exposure.get(id) || 0) + 1)));
  const hasSim = lineups.some(L => L.sim);
  const tiles = [
    ["Lineups", N, candOpts ? `${candOpts.contest === "cash" ? "Cash" : "Tournament"}${candPool.length > N ? ` · best of ${candPool.length}` : ""}` : ""],
    ["Avg projection", f1(avg(L => L.proj)), "fantasy points"],
    hasSim ? ["Avg sim median", f1(avg(L => L.sim?.p50 ?? L.proj)), "50th percentile"] : ["Avg floor", f1(avg(L => L.floor)), "sum of floors"],
    hasSim ? ["Avg sim ceiling", f1(avg(L => L.sim?.p90 ?? L.ceil)), "90th percentile"] : ["Avg ceiling", f1(avg(L => L.ceil)), "sum of 85th pct"],
    ["Avg salary", money(Math.round(avg(L => L.salary))), `of ${money(site.cap)}`],
    ["Players used", exposure.size, `avg own ${Math.round(avg(L => L.own))}% per lineup`],
  ];
  const lo = Math.min(...lineups.map(L => L.sim?.p10 ?? L.floor)), hi = Math.max(...lineups.map(L => L.sim?.p98 ?? L.ceil));
  const xp = (v) => Math.max(0, Math.min(100, 100 * (v - lo) / Math.max(1, hi - lo)));
  const slotKeys = ["QB", "RB1", "RB2", "WR1", "WR2", "WR3", "TE", "FLEX", site.defLabel];
  const slotLabel = (k) => k.replace(/\d$/, "");
  const cards = lineups.map((L, i) => `
    <article class="lu-card">
      <div class="lu-card-head"><span class="lu-num">#${i + 1}</span>
        <span class="small muted">${money(L.salary)} · own ${Math.round(L.own)}%</span>
        <div class="lu-proj"><b>${f1(L.proj)}</b><span>${L.sim ? `ceiling ${f1(L.sim.p90)}` : "projected"}</span></div></div>
      <table>${slotKeys.map(k => { const p = L.slots[k]; if (!p) return ""; const inj = injTag(p); return `<tr>
        <td class="slot">${slotLabel(k)}</td>
        <td class="pl"><span class="pname" data-card="${esc(p.site_player_id)}">${esc(p.player_name)}</span><span class="tm">${esc(p.team)}</span>${locks.has(p.site_player_id) ? ` <span title="Locked" style="color:var(--accent)">${icon("lock").replace('class="icon"', 'class="icon" style="width:11px;height:11px;vertical-align:-1px"')}</span>` : ""}${inj ? ` <span class="inj${inj.out ? " out" : ""}">${esc(inj.t)}</span>` : ""}</td>
        <td class="muted">${money(p.salary)}</td><td><b>${f1(proj(p))}</b></td></tr>`; }).join("")}</table>
      ${L.sim ? `<div class="lu-foot"><span>Sim range</span><span>10th <b>${f1(L.sim.p10)}</b></span><span>median <b>${f1(L.sim.p50)}</b></span><span>90th <b>${f1(L.sim.p90)}</b></span></div>
        <div class="sim-range" style="margin-top:10px" title="10th–90th percentile of simulated totals; tick = median"><i style="left:${xp(L.sim.p10)}%;right:${100 - xp(L.sim.p90)}%"></i><u style="left:${xp(L.sim.p50)}%"></u></div>` : ""}
    </article>`).join("");
  const top = [...exposure.entries()].sort((a, b) => b[1] - a[1]).slice(0, 30);
  const expRows = top.map(([id, c]) => { const p = byId.get(id), e = 100 * c / N, o = own(p); return `<div class="exp-row" title="${esc(p.player_name)}: in ${c} of ${N} lineups (${Math.round(e)}%) · projected ownership ${Math.round(o)}%">
      <span class="nm" data-card="${esc(id)}"><span class="pos ${posCls(p.position)}" style="min-width:26px;height:16px;font-size:9.5px;margin-right:6px">${esc(p.position)}</span>${esc(p.player_name)}</span><span class="pc">${Math.round(e)}%</span>
      <span class="track"><i style="width:${e}%"></i><u style="left:${Math.min(99, o)}%"></u></span></div>`; }).join("");
  box.innerHTML = `
    <div class="res-head"><div><h2>Your lineups</h2><p class="muted small" style="margin-top:2px">${esc(site.name)} · Week ${slate.week} · ${candOpts?.contest === "cash" ? "ranked by simulated median" : "ranked by simulated ceiling"}${hasSim ? "" : " (projection totals — simulations not loaded)"}</p></div>
      <div class="toolbar"><button class="btn" id="rebuildBtn">${icon("refresh")} Rebuild</button><button class="btn btn-primary" id="exportBtn2">${icon("download")} Download CSV for ${esc(site.name)}</button></div></div>
    <div class="res-tiles tiles">${tiles.map(([k, v, s]) => `<div class="tile"><div class="k">${k}</div><div class="v">${v}</div><div class="s">${s}</div></div>`).join("")}</div>
    <div class="callout upload-help">${icon("info")}<div><b>Upload to ${esc(site.name)}:</b> download the CSV, then in ${esc(site.name)} open your contest → <b>Upload lineups</b> (or <b>Edit lineups → Upload</b> for existing entries) and pick the file. Each row is one lineup, in the order shown here.</div></div>
    <div class="res-grid">
      <div class="lu-cards">${cards}</div>
      <aside class="card exposure"><div class="card-head"><h3>Exposure</h3><span class="muted small">top ${top.length} of ${exposure.size}</span></div>
        <div class="exp-legend"><span><i></i>your exposure</span><span><u></u>projected ownership</span></div>
        <div class="exp-list">${expRows}</div></aside>
    </div>`;
  box.querySelectorAll("[data-card]").forEach(el => el.addEventListener("click", () => openCard(el.dataset.card)));
  $("exportBtn2").addEventListener("click", exportCSV);
  $("rebuildBtn").addEventListener("click", generate);
}

// ---------------------------------------------------------------- player card
function histogramSVG(a, mean) {
  const sorted = Float32Array.from(a).sort();
  const n = sorted.length, q = (p) => sorted[Math.min(n - 1, Math.floor(p * n))];
  const hi = Math.max(q(0.995), mean * 2, 10), lo = 0;
  const bins = 28, w = (hi - lo) / bins, counts = new Array(bins).fill(0);
  for (let i = 0; i < n; i++) { const b = Math.min(bins - 1, Math.max(0, Math.floor((sorted[i] - lo) / w))); counts[b]++; }
  const W = 720, H = 210, padL = 8, padR = 8, padT = 34, padB = 24, plotH = H - padT - padB, plotW = W - padL - padR;
  const maxC = Math.max(...counts) || 1, bw = plotW / bins;
  const x = (v) => padL + (v - lo) / (hi - lo) * plotW;
  const p10 = q(0.1), p50 = q(0.5), p90 = q(0.9);
  const cs = getComputedStyle(document.documentElement);
  const cLow = cs.getPropertyValue("--danger").trim(), cMid = cs.getPropertyValue("--info").trim(), cHigh = cs.getPropertyValue("--accent").trim();
  const cText = cs.getPropertyValue("--text").trim(), cMuted = cs.getPropertyValue("--muted").trim(), cWarn = cs.getPropertyValue("--warn").trim();
  const bars = counts.map((c, i) => {
    const left = lo + i * w, right = left + w, mid = (left + right) / 2;
    const fill = mid < p10 ? cLow : mid > p90 ? cHigh : cMid;
    const h = plotH * c / maxC;
    return `<rect fill="${fill}" fill-opacity=".8" x="${(padL + i * bw + 1).toFixed(1)}" y="${(padT + plotH - h).toFixed(1)}" width="${Math.max(1, bw - 2).toFixed(1)}" height="${h.toFixed(1)}" rx="2"><title>${left.toFixed(0)}–${right.toFixed(0)} pts: ${(100 * c / n).toFixed(1)}% of simulations</title></rect>`;
  }).join("");
  const mark = (v, label, color, dy = 0) => `<line x1="${x(v).toFixed(1)}" y1="${4 + dy}" x2="${x(v).toFixed(1)}" y2="${padT + plotH}" stroke="${color}" stroke-width="1.5" stroke-dasharray="3 3"/><text x="${(x(v) + 4).toFixed(1)}" y="${12 + dy}" fill="${color}" font-size="11" font-weight="600">${label} ${v.toFixed(1)}</text>`;
  const ticks = []; for (let t = 0; t <= hi; t += hi > 40 ? 10 : 5) ticks.push(`<text x="${x(t).toFixed(1)}" y="${H - 6}" fill="${cMuted}" font-size="11" text-anchor="middle">${t}</text>`);
  return `<svg class="hist" viewBox="0 0 ${W} ${H}" role="img" aria-label="Distribution of simulated fantasy points">
    ${bars}${mark(p50, "median", cText)}${mark(mean, "proj", cWarn, Math.abs(x(mean) - x(p50)) < 80 ? 14 : 0)}${ticks.join("")}</svg>
    <div class="hist-legend"><span><b>${n.toLocaleString()}</b> simulated games</span><span><i style="background:${cLow}"></i>worst 10% (under ${p10.toFixed(1)})</span><span><i style="background:${cMid}"></i>middle 80%</span><span><i style="background:${cHigh}"></i>best 10% (over ${p90.toFixed(1)})</span></div>`;
}

async function gameLog(p) {
  if (!p.player_id || p.position === "DST") return [];
  if (gameLogCache.has(p.player_id)) return gameLogCache.get(p.player_id);
  let rows = [];
  try {
    const { data } = await supa.from("player_game_stats")
      .select("season,week,team,opponent_team,dk_points,fd_points,attempts,passing_yards,passing_tds,interceptions,carries,rushing_yards,rushing_tds,targets,receptions,receiving_yards,receiving_tds")
      .eq("player_id", p.player_id).eq("season_type", "REG").in("season", [slate.season, slate.season - 1])
      .order("season", { ascending: false }).order("week", { ascending: false }).limit(8);
    rows = (data || []).filter(r => !(r.season === slate.season && r.week >= slate.week));
  } catch (e) { rows = []; }
  gameLogCache.set(p.player_id, rows);
  return rows;
}

let cardId = null;
async function openCard(id) {
  const p = byId.get(id); if (!p) return;
  cardId = id;
  const modal = $("playerCard"), body = $("cardBody");
  modal.hidden = false; document.body.style.overflow = "hidden";
  const a = sim?.index.get(id), m = proj(p);
  const pr = propsMap.get(id);
  const inj = injTag(p);
  const pts = slate.site === "FD" ? "fd_points" : "dk_points";
  const tiles = [
    ["Projection", f1(m), overrides.has(id) ? `your edit (model ${f1(p.mean)})` : (pr && propsBlend > 0 ? "sim + betting markets" : "game simulation")],
    ["Salary", money(p.salary), `${f2(m / (p.salary / 1000))} pts per $1k`],
    ["Floor / Ceiling", `${f1(p.floor)} / ${f1(p.ceiling)}`, "10th / 90th percentile"],
    ["Projected own", Math.round(own(p)) + "%", ownOverrides.has(id) ? "your number" : "of the field"],
  ];
  if (p.q) tiles.push(["Median", f1(p.q.q50 * simScale(p)), "50th percentile"], ["Big game", f1(p.q.q95 * simScale(p)), "95th percentile"]);
  tiles.push(["Boom / Bust", `${pct(p.boom_prob)} / ${pct(p.bust_prob)}`, "for his salary"]);
  if (lastExposure.size) tiles.push(["Your exposure", Math.round(100 * (lastExposure.get(id) || 0)) + "%", `leverage ${(100 * (lastExposure.get(id) || 0) - own(p) > 0 ? "+" : "")}${Math.round(100 * (lastExposure.get(id) || 0) - own(p))}`]);
  const scaled = a ? (() => { const f = simScale(p); return f === 1 ? a : Float32Array.from(a, v => v * f); })() : null;
  let html = `<div class="pc-head"><div class="pc-avatar" style="--pc:var(--pos-${posCls(p.position).toLowerCase()})">${esc(p.position)}</div>
      <div><h3 id="pcName">${esc(p.player_name)}</h3>
      <div class="pc-meta"><b style="color:var(--text-2)">${esc(p.team)}</b> vs ${esc(p.opponent)}${kickoff(p) ? ` · ${kickoff(p)} ET` : ""}${inj ? ` <span class="inj${inj.out ? " out" : ""}" title="${esc(inj.title)}">${esc(inj.t)}</span> <span class="small">${esc(inj.title)}</span>` : ""}</div></div></div>
    <div class="pc-body">
      <div class="pc-actions">
        <button class="btn btn-sm ${locks.has(id) ? "btn-primary" : ""}" data-pc="lock">${icon("lock")} ${locks.has(id) ? "Locked" : "Lock"}</button>
        <button class="btn btn-sm ${excludes.has(id) ? "btn-danger" : ""}" data-pc="excl">${icon("ban")} ${excludes.has(id) ? "Excluded" : "Exclude"}</button>
      </div>
      <div class="pc-tiles" style="margin-top:14px">${tiles.map(([k, v, s]) => `<div class="tile"><div class="k">${k}</div><div class="v">${v}</div><div class="s">${s}</div></div>`).join("")}</div>
      <h4>Simulated outcomes</h4>
      ${scaled ? histogramSVG(scaled, m) : `<p class="hint">Simulations aren't loaded for this slate — summary numbers only.</p>`}`;
  const st = p.components?.stats;
  if (st) {
    const L = [["attempts", "Pass att", 1], ["passing_yards", "Pass yds", 1], ["passing_tds", "Pass TD", 2], ["interceptions", "INT", 2], ["carries", "Rush att", 1], ["rushing_yards", "Rush yds", 1], ["rushing_tds", "Rush TD", 2], ["targets", "Targets", 1], ["receptions", "Rec", 1], ["receiving_yards", "Rec yds", 1], ["receiving_tds", "Rec TD", 2]]
      .filter(([k]) => st[k] != null && (st[k] >= 0.05));
    if (L.length) html += `<h4>Simulated stat line <span style="font-weight:400;text-transform:none;letter-spacing:0">(average across the simulations)</span></h4><div class="lines">${L.map(([k, l, d]) => `<span class="line">${l} <b>${Number(st[k]).toFixed(d)}</b></span>`).join("")}</div>`;
  }
  if (a) {
    const mates = players.filter(o => o.game_id === p.game_id && o.site_player_id !== id && sim.index.has(o.site_player_id) && (o.mean || 0) >= 1);
    const corr = (x, y) => { const n = Math.min(x.length, y.length); let mx = 0, my = 0; for (let i = 0; i < n; i++) { mx += x[i]; my += y[i]; } mx /= n; my /= n;
      let sxy = 0, sxx = 0, syy = 0; for (let i = 0; i < n; i++) { const dx = x[i] - mx, dy = y[i] - my; sxy += dx * dy; sxx += dx * dx; syy += dy * dy; } return sxx && syy ? sxy / Math.sqrt(sxx * syy) : 0; };
    const cr = mates.map(o => ({ o, r: corr(a, sim.index.get(o.site_player_id)) })).sort((x, y) => y.r - x.r);
    if (cr.length) {
      const row = ({ o, r }) => `<tr><td><span class="pos ${posCls(o.position)}" style="min-width:26px;height:16px;font-size:9.5px">${esc(o.position)}</span></td><td class="pl"><span class="pname" data-card="${esc(o.site_player_id)}">${esc(o.player_name)}</span> <span class="muted small">${esc(o.team)}</span></td>
        <td class="${r >= 0.1 ? "pos-diff" : r <= -0.1 ? "neg-diff" : "muted"}">${r >= 0 ? "+" : ""}${r.toFixed(2)}</td><td class="muted">${money(o.salary)}</td><td>${f1(proj(o))}</td></tr>`;
      html += `<h4>Correlation with ${esc(p.player_name.split(" ").slice(-1)[0])} <span style="font-weight:400;text-transform:none;letter-spacing:0">(same game, from the simulations)</span></h4>
        <div class="table-wrap" style="max-height:260px"><table class="data corr"><thead><tr><th></th><th class="left">Player</th><th title="+1 = always score big together, 0 = unrelated, −1 = one's good day is the other's bad day">Corr</th><th>Salary</th><th>Proj</th></tr></thead><tbody>${cr.map(row).join("")}</tbody></table></div>
        <p class="hint" style="margin-top:6px">Positive = they tend to have big days together (stack them). Negative = one's good game usually means a worse one for the other.</p>`;
    }
  }
  if (pr && (Object.keys(pr.lines).length || pr.td != null)) {
    const lab = { pass_yds: "Pass yds", pass_tds: "Pass TD", pass_interceptions: "INT", rush_yds: "Rush yds", reception_yds: "Rec yds", receptions: "Receptions" };
    html += `<h4>Betting market lines</h4><div class="lines">${Object.entries(pr.lines).map(([k, v]) => `<span class="line">${lab[k] || esc(k)} <b>${Number(v).toFixed(1)}</b></span>`).join("")}${pr.td != null ? `<span class="line">Anytime TD <b>${Math.round(100 * pr.td)}%</b></span>` : ""}</div>`;
  }
  html += `<h4>Recent games</h4><div id="cardLog" class="hint">Loading…</div></div>`;
  body.innerHTML = html;
  body.querySelectorAll("[data-pc]").forEach(b => b.addEventListener("click", () => { toggle(b.dataset.pc, id); openCard(id); }));
  body.querySelectorAll("table.corr [data-card]").forEach(el => el.addEventListener("click", () => openCard(el.dataset.card)));
  const rows = await gameLog(p);
  const logEl = document.getElementById("cardLog"); if (!logEl || cardId !== id) return;
  if (!rows.length) { logEl.textContent = p.position === "DST" ? "Game logs are shown for skill players." : "No games on record."; return; }
  const maxPts = Math.max(...rows.map(r => Number(r[pts]) || 0), 1);
  const stat = (r) => p.position === "QB" ? `${r.attempts ?? 0} att · ${r.passing_yards ?? 0} yds · ${r.passing_tds ?? 0} TD · ${r.interceptions ?? 0} INT · ${r.carries ?? 0}-${r.rushing_yards ?? 0} rush`
    : p.position === "RB" ? `${r.carries ?? 0}-${r.rushing_yards ?? 0} rush · ${r.rushing_tds ?? 0} TD · ${r.receptions ?? 0}/${r.targets ?? 0} for ${r.receiving_yards ?? 0}`
    : `${r.receptions ?? 0}/${r.targets ?? 0} for ${r.receiving_yards ?? 0} yds · ${r.receiving_tds ?? 0} TD${(r.carries || 0) ? ` · ${r.carries}-${r.rushing_yards ?? 0} rush` : ""}`;
  const avgPts = rows.reduce((s, r) => s + (Number(r[pts]) || 0), 0) / rows.length;
  logEl.className = "";
  logEl.innerHTML = `<table class="log"><thead><tr><th>Game</th><th>${slate.site === "FD" ? "FD" : "DK"} pts</th><th>Stat line</th></tr></thead><tbody>`
    + rows.map(r => `<tr><td class="nowrap">Wk ${r.week}${r.season !== slate.season ? ` '${String(r.season).slice(2)}` : ""} · ${r.opponent_team ? "vs " + esc(r.opponent_team) : ""}</td><td class="nowrap"><span class="bar" style="width:${Math.round(70 * (Number(r[pts]) || 0) / maxPts)}px"></span><b>${f1(r[pts])}</b></td><td class="muted">${stat(r)}</td></tr>`).join("")
    + `</tbody></table><p class="hint" style="margin-top:8px">Last ${rows.length} games: average ${f1(avgPts)} · this week's projection ${f1(m)}</p>`;
}
function closeCard() { $("playerCard").hidden = true; cardId = null; document.body.style.overflow = ""; }

// ---------------------------------------------------------------- export
function exportCSV() {
  if (!lineups.length) return;
  const site = SITES[slate.site];
  const header = site.slots.join(",");
  const slotKeys = ["QB", "RB1", "RB2", "WR1", "WR2", "WR3", "TE", "FLEX", site.defLabel];
  const lines = lineups.map(L => slotKeys.map(k => L.slots[k]?.site_player_id ?? "").join(","));
  try {   // remember what was exported in this browser
    const key = "gs_lineups_" + slate.slate_key;
    const prev = JSON.parse(localStorage.getItem(key) || "[]");
    const now = new Date().toISOString();
    const add = lineups.map(L => ({ ids: L.ids, proj: +L.proj.toFixed(1), exported_at: now }));
    localStorage.setItem(key, JSON.stringify(prev.concat(add).slice(-300)));
  } catch (e) { /* storage unavailable */ }
  // owner only: record them in the DB (source = web) so Monday's scoring grades them on the Track Record page.
  // Subscribers' exports stay in their own browser and never mix into the scoreboard.
  if (GS.isOwner()) {
    const payload = lineups.map(L => ({ ids: L.ids, proj: +L.proj.toFixed(1), own: Math.round(L.own),
      p10: L.sim ? +L.sim.p10.toFixed(1) : null, p50: L.sim ? +L.sim.p50.toFixed(1) : null, p90: L.sim ? +L.sim.p90.toFixed(1) : null }));
    supa.rpc("save_lineups", { p_slate_key: slate.slate_key, p_source: "web", p_contest: $("contest").value, p_lineups: payload, p_replace: false })
      .then(({ error }) => { if (error) console.warn("save_lineups:", error.message); else GS.toast("Saved to the scoreboard for Monday's grading.", "ok"); });
  }
  const blob = new Blob([header + "\n" + lines.join("\n") + "\n"], { type: "text/csv" });
  const a = document.createElement("a"); a.href = URL.createObjectURL(blob);
  a.download = `${slate.slate_key}_${$("contest").value}_${lineups.length}.csv`; a.click(); setTimeout(() => URL.revokeObjectURL(a.href), 1000);
  GS.toast(`Downloaded ${lineups.length} lineup${lineups.length > 1 ? "s" : ""} — upload the file in ${site.name}.`, "ok");
}

// ---------------------------------------------------------------- settings / presets
function presetFor(contest, n) {
  if (contest === "cash") return { stack: 0, bringback: 0, maxExp: n > 1 ? 70 : 100, minUniq: 2, rand: 0, fade: 0, maxOwn: 0, poolMult: 3 };
  if (n <= 1) return { stack: 1, bringback: 0, maxExp: 100, minUniq: 1, rand: 15, fade: 0, maxOwn: 0, poolMult: 4 };
  if (n <= 3) return { stack: 1, bringback: 0, maxExp: 70, minUniq: 3, rand: 15, fade: 0, maxOwn: 0, poolMult: 3 };
  if (n <= 20) return { stack: 1, bringback: 1, maxExp: 40, minUniq: 3, rand: 15, fade: 0, maxOwn: 0, poolMult: 3 };
  if (n <= 50) return { stack: 1, bringback: 1, maxExp: 35, minUniq: 4, rand: 15, fade: 0, maxOwn: 0, poolMult: 3 };
  return { stack: 1, bringback: 1, maxExp: 30, minUniq: 4, rand: 20, fade: 0, maxOwn: 0, poolMult: 2 };
}
function applyPreset(force) {
  if (customized && !force) { syncControls(); return; }
  const p = presetFor($("contest").value, +$("nLineups").value || 1);
  Object.entries(p).forEach(([k, v]) => { $(k).value = v; });
  customized = false;
  syncControls();
}
function markCustom() { customized = true; syncControls(); }

function pressed(groupId, attr, val) { document.querySelectorAll(`#${groupId} button`).forEach(b => b.setAttribute("aria-pressed", String(b.dataset[attr] === String(val)))); }
function buildLabel() { const n = +$("nLineups").value || 1; return `Build ${n} lineup${n > 1 ? "s" : ""}`; }
function syncControls() {
  const contest = $("contest").value, n = +$("nLineups").value || 1;
  document.body.classList.toggle("is-gpp", contest === "gpp");
  document.body.classList.toggle("single", n <= 1);
  pressed("contestSeg", "v", contest);
  pressed("entriesSeg", "n", n);
  pressed("stackSeg", "v", $("stack").value);
  $("bringbackSw").checked = $("bringback").value === "1";
  $("exclQSw").checked = $("exclQ").value === "1";
  $("maxExpRange").value = $("maxExp").value; $("maxExpVal").textContent = $("maxExp").value + "%";
  $("entriesHint").textContent = contest === "cash" ? "lineups (1 is typical for cash)" : n === 1 ? "single entry" : `${n}-max contest`;
  if (!building) { $("generate").querySelector(".gen-label").textContent = buildLabel(); $("mBuild").textContent = buildLabel(); }
  const bits = [contest === "cash" ? "Cash" : "Tournament", `${n} lineup${n > 1 ? "s" : ""}`];
  if (contest === "gpp") bits.push(["No stack", "QB + 1", "QB + 2"][+$("stack").value] + ($("bringback").value === "1" ? " + bring-back" : ""));
  if (n > 1) bits.push(`max ${$("maxExp").value}%`);
  if (locks.size) bits.push(`${locks.size} locked`);
  $("buildSummary").innerHTML = bits.map(b => `<span class="badge">${esc(b)}</span>`).join("") + (customized ? `<span class="badge warn" title="You changed settings the preset controls">Custom</span>` : "");
  const adv = ["minSalary", "maxTeam", "minUniq", "rand", "fade", "maxOwn", "poolMult"].filter(id => customized && $(id).value !== String(presetFor(contest, n)[id] ?? $(id).defaultValue)).length;
  $("advCount").textContent = adv ? `· ${adv} changed` : "";
  renderObjHint();
  saveSettings();
}

function setView(v) {
  view = v;
  document.querySelectorAll(".ws-tab").forEach(t => t.setAttribute("aria-selected", String(t.dataset.view === v)));
  $("poolPane").hidden = v === "lineups";
  $("lineupPane").hidden = v !== "lineups";
  if (v === "lineups") renderResults(); else render();
}
function openSettings() { document.body.classList.add("settings-open"); $("settingsToggle").setAttribute("aria-expanded", "true"); }
function closeSettings() { document.body.classList.remove("settings-open"); $("settingsToggle").setAttribute("aria-expanded", "false"); }

function wire() {
  $("contestSeg").addEventListener("click", (e) => { const b = e.target.closest("button"); if (!b) return;
    $("contest").value = b.dataset.v;
    if (b.dataset.v === "cash" && +$("nLineups").value > 10) $("nLineups").value = 1;
    applyPreset(true); });
  $("entriesSeg").addEventListener("click", (e) => { const b = e.target.closest("button"); if (!b) return; $("nLineups").value = b.dataset.n; applyPreset(true); });
  document.querySelectorAll(".stepper [data-step]").forEach(b => b.addEventListener("click", () => {
    $("nLineups").value = Math.max(1, Math.min(150, (+$("nLineups").value || 1) + +b.dataset.step)); applyPreset(); }));
  $("nLineups").addEventListener("change", () => { $("nLineups").value = Math.max(1, Math.min(150, Math.round(+$("nLineups").value || 1))); applyPreset(); });
  $("stackSeg").addEventListener("click", (e) => { const b = e.target.closest("button"); if (!b) return; $("stack").value = b.dataset.v; markCustom(); });
  $("bringbackSw").addEventListener("change", () => { $("bringback").value = $("bringbackSw").checked ? "1" : "0"; markCustom(); });
  $("exclQSw").addEventListener("change", () => { $("exclQ").value = $("exclQSw").checked ? "1" : "0"; syncControls(); });
  $("maxExpRange").addEventListener("input", () => { $("maxExp").value = $("maxExpRange").value; markCustom(); });
  ["minSalary", "maxTeam", "minUniq", "rand", "fade", "maxOwn", "poolMult"].forEach(id => $(id).addEventListener("change", markCustom));
  $("resetSettings").addEventListener("click", () => { applyPreset(true); GS.toast("Settings reset to the preset for this contest."); });
  $("propsBlend").addEventListener("change", () => { propsBlend = propsMap.size ? Math.max(0, Math.min(100, +$("propsBlend").value || 0)) / 100 : 0; estimateOwnership(); render(); saveSettings(); });
  $("blend").addEventListener("input", () => { blend = (+$("blend").value || 0) / 100; estimateOwnership(); render(); saveSettings(); });

  $("posChips").addEventListener("click", (e) => { const b = e.target.closest("button"); if (!b) return; $("posFilter").value = b.dataset.pos; pressed("posChips", "pos", b.dataset.pos); render(); });
  $("search").addEventListener("input", render);
  $("onlyMine").addEventListener("click", () => { onlyMine = !onlyMine; $("onlyMine").setAttribute("aria-pressed", onlyMine); render(); });
  $("colSeg").addEventListener("click", (e) => { const b = e.target.closest("button"); if (!b) return; colSet = b.dataset.cols; pressed("colSeg", "cols", colSet); render(); });
  document.querySelectorAll(".ws-tab").forEach(t => t.addEventListener("click", () => setView(t.dataset.view)));

  $("generate").addEventListener("click", generate);
  $("mBuild").addEventListener("click", generate);
  $("exportBtn").addEventListener("click", exportCSV);
  $("settingsToggle").addEventListener("click", () => document.body.classList.contains("settings-open") ? closeSettings() : openSettings());
  $("mSettings").addEventListener("click", openSettings);
  document.addEventListener("click", (e) => { if (document.body.classList.contains("settings-open") && !e.target.closest("#settings,#settingsToggle,#mSettings")) closeSettings(); });

  $("cardClose").addEventListener("click", closeCard);
  document.querySelector("#playerCard .modal-back").addEventListener("click", closeCard);
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") { closeCard(); closeSettings(); } });
}

// ---------------------------------------------------------------- init
async function init() {
  wire();
  if (!restoreSettings()) applyPreset(true); else syncControls();
  if (!cfg.SUPABASE_URL || cfg.SUPABASE_URL.includes("YOUR-PROJECT")) { setStatus("Set SUPABASE_URL and SUPABASE_ANON_KEY in web/config.js", true); return; }
  supa = window.supabase.createClient(cfg.SUPABASE_URL, cfg.SUPABASE_ANON_KEY);
  try {
    [glpk] = await Promise.all([GLPK(), loadSlates()]);
    await loadBoard();
  } catch (e) {
    setStatus("Error: " + (e.message || e), true); console.error(e);
    document.querySelector("#pool tbody").innerHTML = `<tr><td><div class="empty"><div class="icon-wrap">${icon("alert")}</div><h3>Couldn't load the slate</h3><p>${esc(e.message || e)}</p></div></td></tr>`;
    return;
  }
  $("slate").addEventListener("change", () => { loadBoard().catch(e => GS.toast("Couldn't load that slate: " + (e.message || e), "error")); });
}
init();
