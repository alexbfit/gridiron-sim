/* Gridiron Sim — sortable stats table backed by Supabase */
(function () {
  const cfg = window.GRIDIRON_CONFIG || {};
  const statusEl = document.getElementById("status");
  const thead = document.querySelector("#stats thead");
  const tbody = document.querySelector("#stats tbody");
  const seasonEl = document.getElementById("season");
  const weekEl = document.getElementById("week");
  const posEl = document.getElementById("position");
  const searchEl = document.getElementById("search");

  // ---- column groups -------------------------------------------------
  // fmt: i = integer, d1 = 1 decimal, pct = percent
  const GROUPS = [
    { name: "", cols: [
      { key: "player_name", label: "Player", left: true, sticky: true },
      { key: "position", label: "Pos", left: true },
      { key: "team", label: "Team", left: true },
      { key: "opponent_team", label: "Opp", left: true, weekOnly: true },
      { key: "games", label: "G", fmt: "i", seasonOnly: true },
    ]},
    { name: "Fantasy", cols: [
      { key: "dk_points", label: "DK", fmt: "d1", hi: true },
      { key: "fd_points", label: "FD", fmt: "d1", hi: true },
      { key: "dk_ppg", label: "DK/G", fmt: "d1", seasonOnly: true },
      { key: "fd_ppg", label: "FD/G", fmt: "d1", seasonOnly: true },
    ]},
    { name: "Passing", pos: ["QB"], cols: [
      { key: "completions", label: "Cmp", fmt: "i" },
      { key: "attempts", label: "Att", fmt: "i" },
      { key: "passing_yards", label: "Yds", fmt: "i" },
      { key: "passing_tds", label: "TD", fmt: "i" },
      { key: "interceptions", label: "INT", fmt: "i" },
      { key: "sacks", label: "Sck", fmt: "i" },
    ]},
    { name: "Rushing", pos: ["QB", "RB", "WR"], cols: [
      { key: "carries", label: "Car", fmt: "i" },
      { key: "rushing_yards", label: "Yds", fmt: "i" },
      { key: "rushing_tds", label: "TD", fmt: "i" },
    ]},
    { name: "Receiving", pos: ["RB", "WR", "TE"], cols: [
      { key: "targets", label: "Tgt", fmt: "i" },
      { key: "receptions", label: "Rec", fmt: "i" },
      { key: "receiving_yards", label: "Yds", fmt: "i" },
      { key: "receiving_tds", label: "TD", fmt: "i" },
      { key: "receiving_air_yards", label: "AirYds", fmt: "i" },
      { key: "target_share", label: "Tgt%", fmt: "pct" },
      { key: "wopr", label: "WOPR", fmt: "d2" },
    ]},
    { name: "Misc", cols: [
      { key: "fumbles_lost", label: "FL", fmt: "i" },
    ]},
  ];

  let rows = [];
  let sortKey = "dk_points";
  let sortAsc = false;
  let supa = null;

  function setStatus(msg, err) {
    statusEl.textContent = msg;
    statusEl.classList.toggle("error", !!err);
  }

  function fmt(v, f) {
    if (v === null || v === undefined || v === "") return "–";
    const n = Number(v);
    if (Number.isNaN(n)) return v;
    if (f === "i") return Math.round(n).toLocaleString();
    if (f === "d1") return n.toFixed(1);
    if (f === "d2") return n.toFixed(2);
    if (f === "pct") return (n * 100).toFixed(1) + "%";
    return v;
  }

  function visibleGroups() {
    const pos = posEl.value;
    const seasonMode = weekEl.value === "all";
    return GROUPS.map(g => ({
      ...g,
      cols: g.cols.filter(c => !(c.weekOnly && seasonMode) && !(c.seasonOnly && !seasonMode)),
    })).filter(g => g.cols.length && (pos === "ALL" || !g.pos || g.pos.includes(pos)));
  }

  function renderHead() {
    const groups = visibleGroups();
    const r1 = document.createElement("tr");
    const r2 = document.createElement("tr");
    groups.forEach(g => {
      const th = document.createElement("th");
      th.className = "group-head";
      th.colSpan = g.cols.length;
      th.textContent = g.name;
      r1.appendChild(th);
      g.cols.forEach(c => {
        const h = document.createElement("th");
        h.textContent = c.label;
        h.dataset.key = c.key;
        if (c.left) h.classList.add("left");
        if (c.sticky) h.classList.add("sticky");
        if (c.key === sortKey) { h.classList.add("sorted"); if (sortAsc) h.classList.add("asc"); }
        h.addEventListener("click", () => {
          if (sortKey === c.key) sortAsc = !sortAsc;
          else { sortKey = c.key; sortAsc = !!c.left; }
          render();
        });
        r2.appendChild(h);
      });
    });
    thead.replaceChildren(r1, r2);
    return groups.flatMap(g => g.cols);
  }

  function filtered() {
    const pos = posEl.value;
    const q = searchEl.value.trim().toLowerCase();
    return rows.filter(r =>
      (pos === "ALL" ? ["QB", "RB", "WR", "TE"].includes(r.position) : r.position === pos) &&
      (!q || (r.player_name || "").toLowerCase().includes(q) || (r.team || "").toLowerCase().includes(q))
    );
  }

  function render() {
    const cols = renderHead();
    const data = filtered().sort((a, b) => {
      let x = a[sortKey], y = b[sortKey];
      const nx = Number(x), ny = Number(y);
      const numeric = x !== null && y !== null && !Number.isNaN(nx) && !Number.isNaN(ny) && typeof x !== "string";
      let cmp;
      if (x === null || x === undefined) cmp = 1;
      else if (y === null || y === undefined) cmp = -1;
      else if (numeric) cmp = nx - ny;
      else cmp = String(x).localeCompare(String(y));
      return sortAsc ? cmp : -cmp;
    });
    const frag = document.createDocumentFragment();
    data.forEach(r => {
      const tr = document.createElement("tr");
      cols.forEach(c => {
        const td = document.createElement("td");
        if (c.left) td.classList.add("left");
        if (c.sticky) td.classList.add("sticky");
        if (c.hi) td.classList.add("hi");
        if (c.key === "position") {
          const s = document.createElement("span");
          const p = r.position || "";
          s.className = "pos " + (["QB", "RB", "WR", "TE"].includes(p) ? p : "other");
          s.textContent = p;
          td.appendChild(s);
        } else {
          td.textContent = c.fmt ? fmt(r[c.key], c.fmt) : (r[c.key] ?? "–");
        }
        tr.appendChild(td);
      });
      frag.appendChild(tr);
    });
    tbody.replaceChildren(frag);
    setStatus(`${data.length} players · ${weekEl.value === "all" ? "season totals" : "week " + weekEl.value} · ${seasonEl.value}`);
  }

  // ---- data ----------------------------------------------------------
  async function fetchAll(query) {
    // PostgREST caps at 1000 rows by default; page through.
    // The query MUST carry an .order() — without it pages overlap and rows go missing.
    const page = 1000;
    let from = 0, out = [];
    for (;;) {
      const { data, error } = await query.range(from, from + page - 1);
      if (error) throw error;
      out = out.concat(data);
      if (data.length < page) break;
      from += page;
    }
    return out;
  }

  async function loadSeasons() {
    const { data, error } = await supa.from("games").select("season").order("season", { ascending: false }).limit(5000);
    if (error) throw error;
    const seasons = [...new Set(data.map(d => d.season))];
    if (!seasons.length) throw new Error("No data yet — run the ingest job first.");
    seasonEl.replaceChildren(...seasons.map(s => new Option(s, s)));
    await loadWeeks();
  }

  async function loadWeeks() {
    const { data, error } = await supa.from("player_game_stats").select("week")
      .eq("season", seasonEl.value).eq("season_type", "REG").order("week", { ascending: false }).limit(5000);
    if (error) throw error;
    const weeks = [...new Set(data.map(d => d.week))];
    weekEl.replaceChildren(new Option("Season total", "all"), ...weeks.map(w => new Option("Week " + w, w)));
    weekEl.value = weeks.length ? String(weeks[0]) : "all";
  }

  async function loadRows() {
    setStatus("Loading…");
    const season = Number(seasonEl.value);
    let data;
    if (weekEl.value === "all") {
      data = await fetchAll(supa.from("player_season_stats").select("*").eq("season", season).eq("season_type", "REG").order("player_id"));
    } else {
      data = await fetchAll(supa.from("player_game_stats").select("*")
        .eq("season", season).eq("week", Number(weekEl.value)).eq("season_type", "REG").order("player_id"));
      data.forEach(r => { r.fumbles_lost = (r.rushing_fumbles_lost || 0) + (r.receiving_fumbles_lost || 0); });
    }
    rows = data;
    render();
  }

  async function init() {
    if (!cfg.SUPABASE_URL || cfg.SUPABASE_URL.includes("YOUR-PROJECT")) {
      setStatus("Set SUPABASE_URL and SUPABASE_ANON_KEY in web/config.js", true);
      return;
    }
    supa = window.supabase.createClient(cfg.SUPABASE_URL, cfg.SUPABASE_ANON_KEY);
    try {
      await loadSeasons();
      await loadRows();
    } catch (e) {
      setStatus("Error: " + (e.message || e), true);
      console.error(e);
    }
    seasonEl.addEventListener("change", async () => { await loadWeeks(); await loadRows(); });
    weekEl.addEventListener("change", loadRows);
    posEl.addEventListener("change", render);
    searchEl.addEventListener("input", render);
  }

  init();
})();
