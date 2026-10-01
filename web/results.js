/* GameTime Win — results page: slate_results + results_meta, your exported lineups from localStorage. */
(async function () {
  const cfg = window.GRIDIRON_CONFIG || {};
  const $ = (id) => document.getElementById(id);
  const esc = (v) => String(v ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const safeUrl = (u) => /^https?:\/\//i.test(String(u || "")) ? esc(u) : "";
  const f1 = (v) => v == null ? "–" : Number(v).toFixed(1);
  const money = (v) => "$" + Number(v).toLocaleString();
  let supa, slates = [], slate = null, rows = [], sim = null, dbLineups = [], sortKey = "actual", sortAsc = false;
  let newsNotes = [], newsAll = [], upsets = [];

  async function fetchAll(query) {
    const page = 1000; let from = 0, out = [];
    for (;;) { const { data, error } = await query.range(from, from + page - 1); if (error) throw error; out = out.concat(data); if (data.length < page) return out; from += page; }
  }

  async function loadSlates() {
    const { data, error } = await supa.from("slates").select("*").not("results_meta", "is", null).order("season", { ascending: false }).order("week", { ascending: false });
    if (error) throw error;
    slates = data;
    if (!slates.length) { $("status").textContent = "No completed slates scored yet — results appear the morning after a slate's games finish."; return false; }
    $("slate").replaceChildren(...slates.map(s => new Option(`${s.site === "DK" ? "DraftKings" : s.site === "FD" ? "FanDuel" : s.site} · ${s.season} Week ${s.week} · ${s.slate_type === "main" ? "Main slate" : s.slate_type}`, s.slate_id)));
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
    try { newsNotes = await fetchAll(supa.from("news_notes").select("*").eq("slate_id", slate.slate_id).order("id")); } catch (e) { newsNotes = []; }
    try { newsAll = await fetchAll(supa.from("news_notes").select("slate_id,run,adjustment,helped,graded_at,confidence").not("graded_at", "is", null).order("id")); } catch (e) { newsAll = []; }
    try { upsets = await fetchAll(supa.from("upset_picks").select("*").order("id")); } catch (e) { upsets = []; }
    renderTiles(); renderSources(); renderMine(); renderLineupSeason(); renderFlashback(); renderNews(); renderUpsets(); renderTable(); renderTrend();
  }

  function renderTiles() {
    const m = slate.results_meta || {};
    $("status").textContent = `${slate.season} Week ${slate.week} · ${m.n} DFS-relevant players graded · actual points from ${m.actual_source === "contest" ? "DraftKings contest results (exact)" : "box scores (DST approximate)"} · scored ${(m.scored_at || "").slice(0, 10)}`;
    const tiles = [
      ["MAE", f1(m.mae), "DK pts, relevant players"],
      ["Correlation", m.r?.toFixed(3) ?? "–", "projection vs actual"],
      ["Bias", (m.bias > 0 ? "+" : "") + f1(m.bias), "projection − actual"],
      ["Actual ≤ p10 / p90", m.coverage ? `${Math.round(m.coverage.p10 * 100)}% / ${Math.round(m.coverage.p90 * 100)}%` : "–", "target 10% / 90%"],
      ["Ownership", m.has_ownership ? "actual" : "estimated", m.has_ownership ? "from contest results" : "projected"],
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
  const roi = (v) => v == null ? "–" : (v >= 0 ? "+" : "−") + Math.round(Math.abs(v) * 100) + "%";

  function renderFlashback() {
    const tiles = $("fbTiles"), box = $("fbSeason");
    const fb = slate.flashback || {};
    const keys = Object.keys(fb);
    if (!keys.length) {
      tiles.innerHTML = "";
      box.innerHTML = `<p class="lead">No flashback for this slate yet — it runs when a DraftKings contest standings export (the one with the <b>Lineup</b> column) is pushed to <code>data/ownership/</code>.</p>`;
    } else {
      tiles.innerHTML = keys.map(k => { const [src, con] = k.split("/"), f = fb[k], c = f.consensus || {}, m = f.model || {};
        return `<div class="tile" style="grid-column:1/-1"><div class="k">${SRC[src] || src} · ${con.toUpperCase()} · ${f.n} lineups vs ${Number(f.entries).toLocaleString()} real entries · ${f.payout} curve, $${f.fee}</div></div>
        <div class="tile"><div class="k">Flashback ROI (consensus)</div><div class="v">${roi(c.roi)}</div><div class="s">field mean ${roi(c.bench_mean)} · beats ${pct(c.beats_pct)} of real entries</div></div>
        <div class="tile"><div class="k">Cash / top-1% (consensus)</div><div class="v">${pct(c.cash)} / ${c.top1 != null ? (c.top1 * 100).toFixed(1) + "%" : "–"}</div><div class="s">per lineup, on average</div></div>
        <div class="tile"><div class="k">Flashback ROI (model view)</div><div class="v">${roi(m.roi)}</div><div class="s">cash ${pct(m.cash)} · top-1% ${m.top1 != null ? (m.top1 * 100).toFixed(1) + "%" : "–"} · beats ${pct(m.beats_pct)}</div></div>
        <div class="tile"><div class="k">Realized</div><div class="v">${roi(f.real_roi)}</div><div class="s">cash ${pct(f.real_cash)} · best rank ${Number(f.real_best_rank).toLocaleString()} · avg ${f1(f.real_points)} vs field median ${f1(f.field_median_points)}</div></div>`
        + (f.me ? `<div class="tile"><div class="k">Your real entries</div><div class="v">${roi(f.me.roi)}</div><div class="s">${f.me.entries} entered · best rank ${Number(f.me.best_rank).toLocaleString()} · avg ${f1(f.me.mean_points)} pts</div></div>` : "")
        + (f.dupes ? `<div class="tile"><div class="k" title="exact copies of our lineups among the real entries — a prize is split across copies">Duplicated in the field</div><div class="v">${f.dupes.lineups_with_copy}/${f.n}</div><div class="s">#1–20: ${f.dupes.top20_with_copy} copied (${f.dupes.top20_copies} copies) · winnings lost to splits ${pct(f.dupes.winnings_lost_pct)} · field sample ${pct(f.dupes.bench_with_copy)} duplicated</div></div>` : ""); }).join("");
    }
    // season table across slates
    const agg = {};
    slates.forEach(s => Object.entries(s.flashback || {}).forEach(([k, f]) => {
      const a = agg[k] = agg[k] || { weeks: 0, cons: [], model: [], real: [], beats: [], cash: [], above: 0, best: Infinity };
      a.weeks += 1; a.cons.push(f.consensus?.roi); a.model.push(f.model?.roi); a.real.push(f.real_roi); a.beats.push(f.consensus?.beats_pct); a.cash.push(f.consensus?.cash);
      if (f.consensus && f.consensus.roi > f.consensus.bench_median) a.above += 1; a.best = Math.min(a.best, f.real_best_rank ?? Infinity);
    }));
    const ks = Object.keys(agg);
    if (!ks.length) return;
    const mean = (xs) => { const v = xs.filter(x => x != null); return v.length ? v.reduce((a, b) => a + b, 0) / v.length : null; };
    box.innerHTML = `<table class="m"><tr><th class="l">Lineups</th><th>weeks</th><th>flashback ROI (consensus)</th><th>beats field</th><th>cash rate</th><th>model-view ROI</th><th>realized ROI</th><th>weeks above field median</th><th>best real finish</th></tr>` +
      ks.map(k => { const [src, con] = k.split("/"), a = agg[k];
        return `<tr><td class="l"><b>${SRC[src] || src}</b> · ${con.toUpperCase()}</td><td>${a.weeks}</td><td><b>${roi(mean(a.cons))}</b></td><td>${pct(mean(a.beats))}</td><td>${pct(mean(a.cash))}</td><td>${roi(mean(a.model))}</td><td>${roi(mean(a.real))}</td><td>${a.above} / ${a.weeks}</td><td>${a.best === Infinity ? "–" : a.best.toLocaleString()}</td></tr>`; }).join("") + `</table>
      <p class="lead" style="margin-top:6px">Reading it: the <b>consensus</b> column is the number to watch — it is the expected return of our construction against the real field with nobody's opinions in it, and the field averages about −15%. If it sits above the field's median week after week, the lineups are built better than the average entry. <b>Model-view</b> minus <b>realized</b>, over many weeks, is how over-confident our projections are.</p>`;
  }

  function renderMine() {
    const box = $("mine");
    const byId = new Map(rows.map(r => [r.site_player_id, r]));
    const cm = slate.contest_meta;
    // DB lineups (Claude's Sunday lineups + builder exports) plus any browser-only exports not in the DB
    let mine = dbLineups.map(L => ({ source: L.source, contest: L.contest, idx: L.idx, ids: L.player_ids, proj: L.proj, note: L.note,
      actual: L.actual, sim_pct: L.sim_pct, contest_pct: L.contest_pct, fb: L.fb_roi_cons != null ? { cons: Number(L.fb_roi_cons), model: L.fb_roi != null ? Number(L.fb_roi) : null, cash: L.fb_cash_cons != null ? Number(L.fb_cash_cons) : null, rank: L.real_rank, prize: L.real_prize != null ? Number(L.real_prize) : null } : null }));
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
        ${L.fb ? `<div class="lu-head" style="margin-top:2px"><span title="expected ROI vs the real field on market projections (construction only)">flashback <b>${roi(L.fb.cons)}</b></span>${L.fb.model != null ? ` <span title="expected ROI on our own projections">model ${roi(L.fb.model)}</span>` : ""}${L.fb.cash != null ? ` <span>cash ${pct(L.fb.cash)}</span>` : ""}${L.fb.rank ? ` <span title="real finish among all entries">rank ${Number(L.fb.rank).toLocaleString()}${L.fb.prize ? ` · $${Math.round(L.fb.prize)}` : ""}</span>` : ""}</div>` : ""}
        ${sp != null ? `<div class="bar"><i style="width:${Math.round(sp * 100)}%"></i></div>` : ""}
        <table class="lu">${ps.map(p => `<tr><td><span class="pos ${["QB","RB","WR","TE"].includes(p.position) ? p.position : "other"}">${esc(p.position)}</span></td><td class="left">${esc(p.player_name)}</td><td>${f1(p.proj_mean)}</td><td><b>${f1(p.actual)}</b></td></tr>`).join("")}</table>
        ${L.note ? `<div class="muted" style="font-size:11px;margin-top:4px">${esc(L.note)}</div>` : ""}
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

  function renderNews() {
    const el = $("news");
    const graded = newsNotes.filter(n => n.graded_at && n.helped != null);
    const seasonG = newsAll.filter(n => n.helped != null);
    const pct = (a) => a.length ? Math.round(100 * a.filter(n => n.helped).length / a.length) + "%" : "–";
    const hi = (a) => a.filter(n => Number(n.confidence) >= 0.7);
    let html = `<div class="tiles"><div class="tile"><div class="k">This slate</div><div class="v">${pct(graded)}</div><div class="s">${graded.length} adjustments graded · conf ≥ .7: ${pct(hi(graded))}</div></div>`
      + `<div class="tile"><div class="k">Season to date</div><div class="v">${pct(seasonG)}</div><div class="s">${seasonG.length} adjustments · conf ≥ .7: ${pct(hi(seasonG))}</div></div></div>`;
    if (!newsNotes.length) { el.innerHTML = html + `<p class="lead">No notes filed for this slate.</p>`; return; }
    const runs = [...new Set(newsNotes.map(n => n.run))];
    const latest = runs.includes("sun") ? "sun" : runs[runs.length - 1];
    const list = newsNotes.filter(n => n.run === latest).sort((a, b) => Number(b.confidence) - Number(a.confidence));
    html += `<div class="table-wrap"><table class="m"><thead><tr><th class="left">Player</th><th>Team</th><th class="left">Change</th><th>Adj</th><th>Conf</th><th>Proj</th><th>Actual</th><th>Helped</th><th class="left">Source</th></tr></thead><tbody>`
      + list.map(n => `<tr><td class="left">${esc(n.player_name)}${n.site_player_id ? "" : " <span class='muted'>(unmatched)</span>"}</td><td>${esc(n.team ?? "–")}</td><td class="left">${esc(n.change)}</td>`
        + `<td>${esc(n.adjustment)}${n.value != null ? " " + esc(n.value) : ""}</td><td>${Number(n.confidence).toFixed(2)}</td><td>${f1(n.proj_at_note)}</td><td>${f1(n.actual)}</td>`
        + `<td>${n.helped == null ? "–" : n.helped ? "✓" : "✗"}</td><td class="left">${safeUrl(n.source_url) ? `<a href="${safeUrl(n.source_url)}" target="_blank" rel="noopener">${esc(n.source_name || "link")}</a>` : esc(n.source_name || "–")}</td></tr>`).join("")
      + `</tbody></table></div><p class="lead">Run: ${latest} · ${list.length} notes</p>`;
    el.innerHTML = html;
  }

  function renderUpsets() {
    const el = $("upsets");
    const mine = upsets.filter(u => u.slate_id === slate.slate_id);
    const graded = upsets.filter(u => u.graded_at && u.brier_agent != null && u.brier_market != null);
    const mean = (a, k) => a.length ? (a.reduce((s, u) => s + Number(u[k]), 0) / a.length).toFixed(3) : "–";
    const corr = (a) => a.length ? `${a.filter(u => u.correct).length}/${a.filter(u => u.correct != null).length}` : "–";
    const ups = graded.filter(u => u.upset);
    let html = `<div class="tiles"><div class="tile"><div class="k">Brier — agents vs market</div><div class="v">${mean(graded, "brier_agent")} vs ${mean(graded, "brier_market")}</div><div class="s">${graded.length} games graded, season to date (lower is better)</div></div>`
      + `<div class="tile"><div class="k">Straight-up picks</div><div class="v">${corr(graded)}</div><div class="s">market favourite would be ${graded.filter(u => u.market_home_prob != null && ((Number(u.market_home_prob) >= 0.5) === (u.winner === u.home_team))).length}/${graded.filter(u => u.winner && u.winner !== "TIE").length}</div></div>`
      + `<div class="tile"><div class="k">Upset calls</div><div class="v">${ups.length ? ups.filter(u => u.correct).length + "/" + ups.length : "–"}</div><div class="s">times the agents backed the underdog</div></div></div>`;
    if (!mine.length) { el.innerHTML = html + `<p class="lead">No picks for this slate.</p>`; return; }
    const runs = [...new Set(mine.map(u => u.run))]; const latest = runs.includes("sun") ? "sun" : runs[runs.length - 1];
    const list = mine.filter(u => u.run === latest).sort((a, b) => Number(b.upset) - Number(a.upset) || Math.abs(Number(b.agent_home_prob) - Number(b.market_home_prob ?? 0.5)) - Math.abs(Number(a.agent_home_prob) - Number(a.market_home_prob ?? 0.5)));
    html += `<div class="table-wrap"><table class="m"><thead><tr><th class="left">Game</th><th>Spread</th><th>Market home %</th><th>Agent home %</th><th>Pick</th><th class="left">Why</th><th>Result</th></tr></thead><tbody>`
      + list.map(u => `<tr${u.upset ? ' style="font-weight:600"' : ""}><td class="left">${esc(u.away_team)} @ ${esc(u.home_team)}</td><td>${u.spread_line == null ? "–" : (Number(u.spread_line) > 0 ? "-" : "+") + Math.abs(Number(u.spread_line))}</td>`
        + `<td>${u.market_home_prob == null ? "–" : Math.round(100 * Number(u.market_home_prob)) + "%"}</td><td>${Math.round(100 * Number(u.agent_home_prob))}%</td><td>${esc(u.pick)}${u.upset ? " ⚡" : ""}</td>`
        + `<td class="left">${esc(u.reasoning || "")}</td><td>${u.winner ? (u.correct ? "✓ " : "✗ ") + esc(u.winner) : "–"}</td></tr>`).join("")
      + `</tbody></table></div>`;
    el.innerHTML = html;
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

  // ---- season ledger (web/data/ledger.json, written by jobs/recap.py from the database)
  const signed = (v, d = 1) => v == null ? "–" : (v > 0 ? "+" : "") + Number(v).toFixed(d);
  const dollars = (v) => v == null ? "–" : "$" + Number(v).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  const num = (v) => v == null ? "–" : Number(v).toLocaleString();
  const upDown = (v) => v == null ? "" : v > 0 ? "up" : v < 0 ? "down" : "";

  function mdToHtml(md) {
    // the recaps are our own files (headings, bullets, bold, italics); everything is escaped first
    const out = []; let list = null;
    const inline = (s) => esc(s).replace(/\*\*(.+?)\*\*/g, "<b>$1</b>").replace(/(^|[^*])\*([^*]+)\*(?!\*)/g, "$1<i>$2</i>").replace(/`([^`]+)`/g, "<code>$1</code>").replace(/(^|\s)_(.+?)_(?=\s|$)/g, "$1<em>$2</em>");
    const flush = () => { if (list) { out.push(`<ul>${list.join("")}</ul>`); list = null; } };
    md.split("\n").forEach(line => {
      const l = line.trimEnd();
      if (/^- /.test(l)) { (list = list || []).push(`<li>${inline(l.slice(2))}</li>`); return; }
      flush();
      if (!l) return;
      if (/^# /.test(l)) out.push(`<h3>${inline(l.slice(2))}</h3>`);
      else if (/^## /.test(l)) out.push(`<h4>${inline(l.slice(3))}</h4>`);
      else out.push(`<p>${inline(l)}</p>`);
    });
    flush();
    return out.join("");
  }

  async function renderLedger() {
    const box = $("ledger");
    if (!box) return;
    let d;
    try { const res = await fetch("data/ledger.json", { cache: "no-cache" }); if (!res.ok) throw new Error(res.status); d = await res.json(); }
    catch (e) { box.hidden = true; return; }
    const weeks = (d.weeks || []).slice().sort((a, b) => a.week - b.week), t = d.totals;
    if (!weeks.length || !t) { box.hidden = true; return; }
    box.hidden = false;
    $("ledgerMeta").textContent = `${d.season} · ${t.weeks} week${t.weeks === 1 ? "" : "s"} · ${num(t.lineups)} lineups · built ${String(d.built_at || "").slice(0, 10)}`;
    const tiles = [
      ["Consensus ROI vs field", `${roi(t.consensus_roi)} <span style="font-size:14px;color:var(--muted)">vs ${roi(t.field_mean_roi)}</span>`, `${t.consensus_roi_vs_field == null ? "expected return on market projections" : signed(t.consensus_roi_vs_field * 100, 0) + " pts vs the field's mean expected return"} · ${t.weeks_with_flashback}/${t.weeks} weeks replayed`, upDown(t.consensus_roi_vs_field)],
      ["Weeks above field median", t.weeks_with_field_median ? `${t.weeks_above_field_median} / ${t.weeks_with_field_median}` : "–", "average lineup points vs the median real entry", ""],
      ["Lineups in top 1%", t.top1_lineups == null ? "–" : String(t.top1_lineups), `of ${num(t.lineups)} lineups entered · realized finishes`, ""],
      ["Best real finish", t.best_finish ? num(t.best_finish.rank) : "–", t.best_finish ? `of ${num(t.best_finish.entries)} · week ${t.best_finish.week}` : "no standings file imported yet", ""],
    ];
    if (t.real_money) tiles.push(["Real money in → out", `${dollars(t.real_money.in)} → ${dollars(t.real_money.out)}`, `${roi(t.real_money.roi)} · ${t.real_money.weeks} week${t.real_money.weeks === 1 ? "" : "s"} · payout curve on real ranks`, upDown(t.real_money.roi)]);
    $("ledgerTiles").innerHTML = tiles.map(([k, v, s, c]) => `<div class="tile"><div class="k">${k}</div><div class="v ${c}">${v}</div><div class="s">${s}</div></div>`).join("");
    const rowHtml = (w) => { const L = w.lineups || {}, F = w.flashback || {}, D = w.dupes, R = w.real_money;
      return `<tr>
        <td class="l"><a href="#" data-recap="${esc(w.recap)}" data-week="${w.week}">Week ${w.week}</a> <span class="muted">· ${L.n ?? "–"}</span></td>
        <td title="average lineup points minus the field's median entry">${f1(L.avg_pts)} <span class="muted">(${signed(L.avg_vs_field_median)})</span></td>
        <td title="share of real entries beaten, averaged over our lineups">${pct(L.beat_pct)}${L.best_beat_pct != null ? ` <span class="muted">· best ${pct(L.best_beat_pct)}</span>` : ""}</td>
        <td class="${F.consensus_roi != null && F.field_mean_roi != null && F.consensus_roi > F.field_mean_roi ? "best" : ""}" title="Flashback consensus ROI vs the field's mean expected ROI">${F.present ? `${roi(F.consensus_roi)} <span class="muted">vs ${roi(F.field_mean_roi)}</span>` : "–"}</td>
        <td title="share of real entries our portfolio beats in expectation">${pct(F.beats_pct)}</td>
        <td title="lineups that finished in the top 1% · best real rank">${L.top1_lineups ?? "–"}${L.best_rank ? ` <span class="muted">· #${num(L.best_rank)}</span>` : ""}</td>
        <td title="lineups with an exact copy in the field · winnings lost to splits">${D ? `${D.lineups_with_copy}/${L.n} <span class="muted">· ${pct(D.winnings_lost_pct)}</span>` : "–"}</td>
        <td title="owner's entries in this contest: stake → payout-curve return">${R ? `${dollars(R.in)} → ${dollars(R.out)}` : "–"}</td>
        <td class="l"><a href="data/${esc(w.recap)}" data-recap="${esc(w.recap)}" data-week="${w.week}">recap</a></td></tr>`; };
    $("ledgerTable").innerHTML = `<tr><th class="l">Week · lineups</th><th>avg pts (vs median)</th><th>beat % of field</th><th>consensus ROI vs field</th><th>beats field</th><th>top 1% · best</th><th>dupes</th><th>real $</th><th class="l"></th></tr>` + weeks.map(rowHtml).join("");
    const caveats = [];
    if (weeks.some(w => !w.flashback?.present)) caveats.push("weeks without a Contest Flashback (no standings export) show realized points only");
    if (!weeks.some(w => w.dupes)) caveats.push("duplicate counts start with the first Flashback run that records them");
    if (t.real_money) caveats.push("real $ counts only the contests with an imported standings file, at the payout curve — not a DraftKings statement");
    $("ledgerFine").textContent = caveats.length ? "Notes: " + caveats.join("; ") + "." : "";
    // latest recap inline, switchable
    const recapBox = $("ledgerRecap"); const cache = new Map();
    async function showRecap(w) {
      let md = cache.get(w.recap);
      if (md == null) { try { const r = await fetch("data/" + w.recap, { cache: "no-cache" }); md = r.ok ? await r.text() : ""; } catch (e) { md = ""; } cache.set(w.recap, md); }
      recapBox.innerHTML = `<div class="recap-nav">${weeks.map(x => `<span class="badge ${x.week === w.week ? "active" : ""}" data-week="${x.week}">Week ${x.week}</span>`).join("")}</div>`
        + (md ? `<div class="recap">${mdToHtml(md)}</div>` : `<p class="lead" style="margin-top:8px">Recap for week ${w.week} not published yet.</p>`);
      recapBox.querySelectorAll(".recap-nav .badge").forEach(b => b.addEventListener("click", () => showRecap(weeks.find(x => x.week === Number(b.dataset.week)))));
    }
    $("ledgerTable").querySelectorAll("a[data-recap]").forEach(a => a.addEventListener("click", (ev) => { ev.preventDefault(); showRecap(weeks.find(x => x.week === Number(a.dataset.week))); recapBox.scrollIntoView({ behavior: "smooth", block: "nearest" }); }));
    await showRecap(weeks[weeks.length - 1]);
  }

  async function init() {
    renderLedger().catch(e => { console.warn("ledger", e); const b = $("ledger"); if (b) b.hidden = true; });
    if (!cfg.SUPABASE_URL || cfg.SUPABASE_URL.includes("YOUR-PROJECT")) { $("status").textContent = "Set SUPABASE_URL and SUPABASE_ANON_KEY in web/config.js"; return; }
    supa = window.supabase.createClient(cfg.SUPABASE_URL, cfg.SUPABASE_ANON_KEY);
    try { if (!(await loadSlates())) return; await loadSlate(); }
    catch (e) { $("status").textContent = "Error: " + (e.message || e); console.error(e); return; }
    $("slate").addEventListener("change", loadSlate);
    ["posFilter", "search"].forEach(id => $(id).addEventListener("input", renderTable));
  }
  init();
})();
