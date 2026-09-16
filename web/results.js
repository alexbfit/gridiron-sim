/* Gridiron Sim — results page: slate_results + results_meta, your exported lineups from localStorage. */
(async function () {
  const cfg = window.GRIDIRON_CONFIG || {};
  const $ = (id) => document.getElementById(id);
  const f1 = (v) => v == null ? "–" : Number(v).toFixed(1);
  const money = (v) => "$" + Number(v).toLocaleString();
  let supa, slates = [], slate = null, rows = [], sim = null, dbLineups = [], sortKey = "actual", sortAsc = false;

  async function fetchAll(query) {
    const page = 1000; let from = 0, out = [];
    for (;;) { const { data, error } = await query.range(from, from + page - 1); if (error) throw error; out = out.concat(data); if (data.length < page) return out; from += page; }
  }

  async function loadSlates() {
    const { data, error } = await supa.from("slates").select("*").not("results_meta", "is", null).order("season", { ascending: false }).order("week", { ascending: false });
    if (error) throw error;
    slates = data;
    if (!slates.length) { $("status").textContent = "No completed slates scored yet — results appear the morning after a slate's games finish."; return false; }
    $("slate").replaceChildren(...slates.map(s => new Option(`${s.site} · ${s.season} wk ${s.week} · ${s.slate_type}`, s.slate_id)));
    return true;
  }

  async function loadSim() {
    sim = null;
    const url = slate.sim_meta?.url; if (!url) return;
    try {
      const res = await fetch(url, { cache: "no-cache" }); if (!res.ok) return;
      const text = (typeof DecompressionStream === "function" && !res.headers.get("content-encoding"))
        ? await new Response(res.body.pipeThrough(new DecompressionStream("gzip"))).text() : await res.text();
      const d = JSON.parse(text); const index = new Map(); d.players.forEach((id, i) => index.set(id, Float32Array.from(d.scores[i])));
      sim = { n: d.n, index };
    } catch (e) { console.warn("sim matrix", e); }
  }

  async function loadSlate() {
    slate = slates.find(s => s.slate_id === $("slate").value);
    rows = await fetchAll(supa.from("slate_results").select("*").eq("slate_id", slate.slate_id).order("site_player_id"));
    rows.forEach(r => { ["proj_mean", "proj_p10", "proj_p50", "proj_p90", "actual", "pit", "own_actual", "own_heuristic", "salary"].forEach(k => { if (r[k] != null) r[k] = Number(r[k]); }); r.diff = r.proj_mean == null ? null : r.actual - r.proj_mean; });
    await loadSim();
    dbLineups = await fetchAll(supa.from("slate_lineups").select("*").eq("slate_id", slate.slate_id).order("source").order("contest").order("idx"));
    renderTiles(); renderSources(); renderMine(); renderLineupSeason(); renderTable(); renderTrend();
  }

  function renderTiles() {
    const m = slate.results_meta || {};
    $("status").textContent = `${slate.site} · ${slate.season} week ${slate.week} · ${m.n} DFS-relevant players scored · actuals from ${m.actual_source === "contest" ? "contest export (exact)" : "box scores (DST approximate)"} · ${(m.scored_at || "").slice(0, 10)}`;
    const tiles = [
      ["MAE", f1(m.mae), "DK pts, relevant players"],
      ["Correlation", m.r?.toFixed(3) ?? "–", "projection vs actual"],
      ["Bias", (m.bias > 0 ? "+" : "") + f1(m.bias), "projection − actual"],
      ["Actual ≤ p10 / p90", m.coverage ? `${Math.round(m.coverage.p10 * 100)}% / ${Math.round(m.coverage.p90 * 100)}%` : "–", "target 10% / 90%"],
      ["Ownership", m.has_ownership ? "actual" : "heuristic", m.has_ownership ? "from contest file" : "import a standings CSV"],
    ];
    const pos = Object.entries(m.by_pos || {}).map(([p, v]) => `${p} ${f1(v.mae)} (${v.bias > 0 ? "+" : ""}${f1(v.bias)})`).join(" · ");
    $("tiles").innerHTML = tiles.map(([k, v, s]) => `<div class="tile"><div class="k">${k}</div><div class="v">${v}</div><div class="s">${s}</div></div>`).join("")
      + (pos ? `<div class="tile" style="grid-column:1/-1"><div class="k">MAE (bias) by position</div><div class="s" style="font-size:13px;color:var(--text)">${pos}</div></div>` : "");
  }

  function renderSources() {
    const names = ["sim", "baseline", "external", "blend50"];
    const week = slate.results_meta?.by_method || {};
    const agg = {};
    slates.forEach(s => Object.entries(s.results_meta?.by_method || {}).forEach(([k, v]) => { const a = agg[k] = agg[k] || { n: 0, mae: 0, r: 0, bias: 0, w: 0 }; a.n += v.n; a.mae += v.mae * v.n; a.r += v.r * v.n; a.bias += v.bias * v.n; a.w += 1; }));
    const present = names.filter(k => week[k] || agg[k]);
    if (!present.length) { $("sources").innerHTML = ""; return; }
    const bestW = Math.min(...present.filter(k => week[k]).map(k => week[k].mae));
    const bestS = Math.min(...present.filter(k => agg[k]).map(k => agg[k].mae / agg[k].n));
    $("sources").innerHTML = `<tr><th class="l">Source</th><th>This week MAE</th><th>r</th><th>bias</th><th>Season MAE</th><th>r</th><th>bias</th><th>weeks</th></tr>` +
      present.map(k => { const w = week[k], a = agg[k];
        return `<tr><td class="l"><b>${k}</b></td>
          <td class="${w && w.mae === bestW ? "best" : ""}">${w ? w.mae.toFixed(2) : "–"}</td><td>${w ? w.r.toFixed(3) : "–"}</td><td>${w ? (w.bias > 0 ? "+" : "") + w.bias.toFixed(2) : "–"}</td>
          <td class="${a && a.mae / a.n === bestS ? "best" : ""}">${a ? (a.mae / a.n).toFixed(2) : "–"}</td><td>${a ? (a.r / a.n).toFixed(3) : "–"}</td><td>${a ? ((a.bias / a.n) > 0 ? "+" : "") + (a.bias / a.n).toFixed(2) : "–"}</td><td>${a ? a.w : "–"}</td></tr>`; }).join("");
  }

  const pct = (v) => v == null ? "–" : Math.round(v * 100) + "%";
  const SRC = { claude: "Claude (Sunday task)", web: "Builder export" };

  function renderMine() {
    const box = $("mine");
    const byId = new Map(rows.map(r => [r.site_player_id, r]));
    const cm = slate.contest_meta;
    // DB lineups (Claude's Sunday lineups + builder exports) plus any browser-only exports not in the DB
    let mine = dbLineups.map(L => ({ source: L.source, contest: L.contest, idx: L.idx, ids: L.player_ids, proj: L.proj, note: L.note,
      actual: L.actual, sim_pct: L.sim_pct, contest_pct: L.contest_pct }));
    try {
      const local = JSON.parse(localStorage.getItem("gs_lineups_" + slate.slate_key) || "[]");
      const seen = new Set(mine.map(L => L.ids.slice().sort().join("|")));
      local.forEach((L, i) => { const k = L.ids.slice().sort().join("|"); if (!seen.has(k)) { seen.add(k); mine.push({ source: "browser", contest: "?", idx: i + 1, ids: L.ids, proj: L.proj }); } });
    } catch (e) {}
    if (!mine.length) { box.innerHTML = `<p class="lead">No lineups recorded for this slate yet. Claude's Sunday lineups and anything you export from the builder are saved automatically and scored here after the games.</p>`; return; }
    const cards = mine.map(L => {
      const ps = L.ids.map(id => byId.get(id)).filter(Boolean);
      const actual = L.actual != null ? Number(L.actual) : ps.reduce((s, p) => s + (p.actual || 0), 0);
      let sp = L.sim_pct != null ? Number(L.sim_pct) : null;
      if (sp == null && sim) {
        const tot = new Float32Array(sim.n);
        L.ids.forEach(id => { const a = sim.index.get(id); if (a) for (let k = 0; k < sim.n; k++) tot[k] += a[k]; else { const m = byId.get(id)?.proj_mean || 0; for (let k = 0; k < sim.n; k++) tot[k] += m; } });
        let below = 0; for (let k = 0; k < sim.n; k++) if (tot[k] <= actual) below++;
        sp = below / sim.n;
      }
      return { L, ps, actual, sp, cp: L.contest_pct != null ? Number(L.contest_pct) : null };
    });
    const groups = new Map();
    cards.forEach(c => { const k = `${c.L.source}|${c.L.contest}`; if (!groups.has(k)) groups.set(k, []); groups.get(k).push(c); });
    box.innerHTML = [...groups.entries()].map(([k, cs]) => {
      const [src, con] = k.split("|");
      const acts = cs.map(c => c.actual), avg = acts.reduce((a, b) => a + b, 0) / cs.length, best = Math.max(...acts);
      const cps = cs.filter(c => c.cp != null).map(c => c.cp);
      const head = `<div class="lu-group"><b>${SRC[src] || src}</b> · ${con.toUpperCase()} · ${cs.length} lineup${cs.length > 1 ? "s" : ""} · avg <b>${f1(avg)}</b>${cs.length > 1 ? ` · best <b>${f1(best)}</b>` : ""}`
        + (cps.length ? ` · would beat <b>${pct(cps.reduce((a, b) => a + b, 0) / cps.length)}</b> of the field on average${cs.length > 1 ? ` (best ${pct(Math.max(...cps))})` : ""}` : (cm ? "" : ` · <span class="muted">import a contest standings file for real finishes</span>`))
        + `</div>`;
      cs.sort((a, b) => b.actual - a.actual);
      return head + `<div class="lu-grid">` + cs.map(({ L, ps, actual, sp, cp }) => `
      <div class="lineup">
        <div class="lu-head"><b>#${L.idx}</b> <span>proj ${f1(L.proj)}</span> <span>actual <b>${f1(actual)}</b></span>${sp != null ? ` <span title="percentile of the lineup's own simulated distribution">sim ${pct(sp)}</span>` : ""}${cp != null ? ` <span title="share of real contest entries this total beat">field ${pct(cp)}</span>` : ""}</div>
        ${sp != null ? `<div class="bar"><i style="width:${Math.round(sp * 100)}%"></i></div>` : ""}
        <table class="lu">${ps.map(p => `<tr><td><span class="pos ${["QB","RB","WR","TE"].includes(p.position) ? p.position : "other"}">${p.position}</span></td><td class="left">${p.player_name}</td><td>${f1(p.proj_mean)}</td><td><b>${f1(p.actual)}</b></td></tr>`).join("")}</table>
        ${L.note ? `<div class="muted" style="font-size:11px;margin-top:4px">${L.note}</div>` : ""}
      </div>`).join("") + `</div>`;
    }).join("");
  }

  function renderLineupSeason() {
    const box = $("luSeason");
    const agg = {};
    slates.forEach(s => Object.entries(s.results_meta?.lineups || {}).forEach(([src, d]) => Object.entries(d).forEach(([con, m]) => {
      const a = agg[`${src}|${con}`] = agg[`${src}|${con}`] || { weeks: 0, n: 0, proj: 0, actual: 0, best: 0, sp: [], cp: [], bcp: [], above: 0, wk: 0 };
      a.weeks += 1; a.n += m.n; a.proj += m.proj; a.actual += m.actual; a.best = Math.max(a.best, m.best);
      if (m.sim_pct != null) a.sp.push(m.sim_pct); if (m.contest_pct != null) { a.cp.push(m.contest_pct); a.wk += 1; if (m.contest_pct >= 0.5) a.above += 1; }
      if (m.best_contest_pct != null) a.bcp.push(m.best_contest_pct);
    })));
    const keys = Object.keys(agg);
    if (!keys.length) { box.innerHTML = `<p class="lead">Season lineup record appears after the first scored week.</p>`; return; }
    const mean = (xs) => xs.length ? xs.reduce((a, b) => a + b, 0) / xs.length : null;
    box.innerHTML = `<table class="m"><tr><th class="l">Lineups</th><th>weeks</th><th>lineups</th><th>avg proj</th><th>avg actual</th><th>best</th><th>avg sim pct</th><th>avg field beaten</th><th>best finish</th><th>weeks above field median</th></tr>` +
      keys.map(k => { const [src, con] = k.split("|"), a = agg[k];
        return `<tr><td class="l"><b>${SRC[src] || src}</b> · ${con.toUpperCase()}</td><td>${a.weeks}</td><td>${a.n}</td><td>${f1(a.proj / a.weeks)}</td><td>${f1(a.actual / a.weeks)}</td><td>${f1(a.best)}</td><td>${pct(mean(a.sp))}</td><td>${pct(mean(a.cp))}</td><td>${pct(a.bcp.length ? Math.max(...a.bcp) : null)}</td><td>${a.wk ? `${a.above} / ${a.wk}` : "–"}</td></tr>`; }).join("") + `</table>
      <p class="lead" style="margin-top:6px">Reading it: <b>avg sim pct</b> near 50% means the sim's lineup distributions are honest (consistently under 50% = over-projecting). <b>avg field beaten</b> comes from imported contest standings — above 50% on cash lineups is the cash line, and GPP <b>best finish</b> is what matters for tournaments. Give it 4–6 weeks before scaling up real entries.</p>`;
  }

  const COLS = [
    { key: "player_name", label: "Player", left: true }, { key: "position", label: "Pos", left: true }, { key: "team", label: "Team", left: true },
    { key: "salary", label: "Salary", fmt: money }, { key: "proj_mean", label: "Proj", fmt: f1 }, { key: "actual", label: "Actual", fmt: f1 },
    { key: "diff", label: "Diff", fmt: (v) => v == null ? "–" : (v > 0 ? "+" : "") + v.toFixed(1) },
    { key: "proj_p10", label: "p10", fmt: f1 }, { key: "proj_p90", label: "p90", fmt: f1 }, { key: "pit", label: "PIT", fmt: (v) => v == null ? "–" : v.toFixed(2) },
    { key: "own", label: "Own%", fmt: (v) => v == null ? "–" : Math.round(v) + "%" },
  ];
  function renderTable() {
    const thead = document.querySelector("#players thead"), tbody = document.querySelector("#players tbody");
    const tr = document.createElement("tr");
    COLS.forEach(c => { const th = document.createElement("th"); th.textContent = c.label; if (c.left) th.classList.add("left"); if (c.key === sortKey) th.classList.add("sorted", sortAsc ? "asc" : "x");
      th.addEventListener("click", () => { if (sortKey === c.key) sortAsc = !sortAsc; else { sortKey = c.key; sortAsc = !!c.left; } renderTable(); }); tr.appendChild(th); });
    thead.replaceChildren(tr);
    const pos = $("posFilter").value, q = $("search").value.trim().toLowerCase();
    const data = rows.filter(r => (pos === "ALL" || r.position === pos) && (!q || r.player_name.toLowerCase().includes(q) || (r.team || "").toLowerCase().includes(q)) && (r.played || r.proj_mean > 0))
      .map(r => ({ ...r, own: r.own_actual ?? r.own_heuristic }));
    data.sort((a, b) => { const x = a[sortKey], y = b[sortKey]; const c = (x == null) - (y == null) || (typeof x === "number" ? x - y : String(x ?? "").localeCompare(String(y ?? ""))); return sortAsc ? c : -c; });
    const frag = document.createDocumentFragment();
    data.forEach(r => {
      const tr = document.createElement("tr");
      COLS.forEach(c => { const td = document.createElement("td"); if (c.left) td.classList.add("left");
        if (c.key === "position") { const s = document.createElement("span"); s.className = "pos " + (["QB","RB","WR","TE"].includes(r.position) ? r.position : "other"); s.textContent = r.position; td.appendChild(s); }
        else { td.textContent = c.fmt ? c.fmt(r[c.key]) : (r[c.key] ?? "–"); if (c.key === "diff" && r.diff != null) td.classList.add(r.diff >= 0 ? "pos-diff" : "neg-diff"); }
        tr.appendChild(td); });
      frag.appendChild(tr);
    });
    tbody.replaceChildren(frag);
  }

  function renderTrend() {
    const items = slates.slice().sort((a, b) => a.season - b.season || a.week - b.week).map(s => { const m = s.results_meta || {}; return `<div class="tile"><div class="k">${s.site} ${s.season} wk ${s.week}</div><div class="v">${f1(m.mae)}</div><div class="s">MAE · r ${m.r?.toFixed(2) ?? "–"} · bias ${m.bias > 0 ? "+" : ""}${f1(m.bias)}</div></div>`; });
    $("trend").innerHTML = items.join("");
  }

  async function init() {
    if (!cfg.SUPABASE_URL || cfg.SUPABASE_URL.includes("YOUR-PROJECT")) { $("status").textContent = "Set SUPABASE_URL and SUPABASE_ANON_KEY in web/config.js"; return; }
    supa = window.supabase.createClient(cfg.SUPABASE_URL, cfg.SUPABASE_ANON_KEY);
    try { if (!(await loadSlates())) return; await loadSlate(); }
    catch (e) { $("status").textContent = "Error: " + (e.message || e); console.error(e); return; }
    $("slate").addEventListener("change", loadSlate);
    ["posFilter", "search"].forEach(id => $(id).addEventListener("input", renderTable));
  }
  init();
})();
