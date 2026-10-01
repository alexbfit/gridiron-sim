/* GameTime Win — prop edges research screen. Reads data/edges.json written by jobs/edges.py (gameday-refresh). */
(async function () {
  const $ = (id) => document.getElementById(id);
  const esc = (v) => String(v ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const safeUrl = (u) => /^https:\/\/(www\.)?kalshi\.com\//i.test(String(u || "")) ? esc(u) : "";
  const pct = (v) => v == null ? "–" : Math.round(v * 100) + "%";
  const BIG = 25;

  let d;
  try {
    const res = await fetch("data/edges.json", { cache: "no-cache" });
    if (!res.ok) throw new Error(res.status);
    d = await res.json();
  } catch (e) {
    $("meta").textContent = "No edge screen published yet — it is written by the gameday refresh once Kalshi posts the week's player markets (usually Wednesday).";
    $("rows").innerHTML = "";
    document.querySelector(".table-wrap").innerHTML = `<div class="empty"><h3>Nothing to screen yet</h3><p>Check back after the next data refresh.</p></div>`;
    return;
  }

  const rows = (d.rows || []).map(r => ({ ...r, flags: r.flags || [] }));
  const st = d.stats || {};
  const when = d.generated_at ? new Date(d.generated_at) : null;
  const whenTxt = when ? when.toLocaleString(undefined, { weekday: "short", month: "short", day: "numeric", hour: "numeric", minute: "2-digit" }) : "–";
  $("meta").textContent = `${d.season ?? ""} Week ${d.week ?? ""} · games on ${d.gameday} · ${d.sources?.sportsbook ? "Kalshi + sportsbook props" : "Kalshi only (sportsbook props land Sunday morning)"} · snapshot ${whenTxt}`;

  const nBig = rows.filter(r => Math.abs(r.edge) >= BIG).length;
  const nHi = rows.filter(r => r.confidence === "props+sim").length;
  const tiles = [
    ["Kalshi rungs matched", `${(st.kalshi_matched ?? 0).toLocaleString()} / ${(st.kalshi_rungs ?? 0).toLocaleString()}`, `${st.players_matched ?? 0} slate players`],
    ["Sides with an edge ≥ 3", rows.length.toLocaleString(), `${nBig} are 25+ pt gaps (hidden by default)`],
    ["Sportsbook-backed", nHi.toLocaleString(), nHi ? "rows with a book line behind the model" : "none until the Sunday props pull"],
    ["Without a real quote", (st.kalshi_no_market ?? 0).toLocaleString(), "spread wider than 25¢ — skipped"],
  ];
  $("tiles").innerHTML = tiles.map(([k, v, s]) => `<div class="tile"><div class="k">${k}</div><div class="v">${v}</div><div class="s">${s}</div></div>`).join("");

  // shapes table in the methodology
  const labels = { player_pass_yds: "Passing yards", player_rush_yds: "Rushing yards", player_reception_yds: "Receiving yards", player_receptions: "Receptions", player_pass_tds: "Passing TDs", anytime_td: "Touchdowns" };
  const fits = d.implied_cv || {};
  $("shapes").innerHTML = `<tr><th>Market</th><th>Shape</th><th>CV by position (table)</th><th>Market-implied CV (this week)</th></tr>` +
    Object.entries(d.shapes || {}).map(([m, s]) => {
      const shape = typeof s === "string" ? s : "lognormal";
      const cv = typeof s === "string" ? "–" : Object.entries(s).filter(([p]) => p !== "*").map(([p, v]) => `${p} ${v}`).join(" · ");
      const imp = Object.entries(fits).filter(([k]) => k.startsWith(m + "/")).map(([k, v]) => `${k.split("/")[1]} ${v.market_cv}`).join(" · ");
      return `<tr><td>${labels[m] || m}</td><td>${shape}</td><td>${cv}</td><td>${imp || "–"}</td></tr>`;
    }).join("");

  // filters
  const markets = [...new Set(rows.map(r => r.market))];
  $("market").append(...markets.map(m => new Option(labels[m] || m, m)));
  const teams = [...new Set(rows.map(r => r.team))].sort();
  $("team").append(...teams.map(t => new Option(t, t)));
  let sortKey = "ev", sortAsc = false;
  const COLS = [["player", "Player", "l"], ["team", "Team", "l"], ["market_label", "Market", "l"], ["rung", "Rung", "l"], ["side", "Side", "l"],
                ["our_p", "Ours"], ["market_p", "Market"], ["price", "Ask"], ["edge", "Edge"], ["ev", "EV / $1"], ["kelly", "Kelly"], ["source", "Source", "l"]];

  function visible() {
    const mk = $("market").value, tm = $("team").value, pos = $("pos").value, min = Number($("minEdge").value) || 0;
    const q = $("search").value.trim().toLowerCase(), hideBig = $("hideBig").checked, hiOnly = $("hiOnly").checked;
    return rows.filter(r => (mk === "ALL" || r.market === mk) && (tm === "ALL" || r.team === tm) && (pos === "ALL" || r.pos === pos)
      && Math.abs(r.edge) >= min && (!hideBig || Math.abs(r.edge) < BIG) && (!hiOnly || r.confidence === "props+sim")
      && (!q || r.player.toLowerCase().includes(q)));
  }

  function render() {
    const list = visible().sort((a, b) => {
      const x = a[sortKey], y = b[sortKey];
      const c = typeof x === "number" && typeof y === "number" ? x - y : String(x ?? "").localeCompare(String(y ?? ""));
      return sortAsc ? c : -c;
    });
    $("count").textContent = `${list.length.toLocaleString()} of ${rows.length.toLocaleString()} sides`;
    const thead = `<tr>${COLS.map(([k, l, cls]) => `<th class="sortable ${cls || ""}" data-k="${k}">${l}${sortKey === k ? (sortAsc ? " ▲" : " ▼") : ""}</th>`).join("")}</tr>`;
    const body = list.length ? list.map(r => {
      const flags = (r.confidence !== "props+sim" ? `<span class="flag low" title="No sportsbook line informed the model">sim-only</span>` : `<span class="flag hi" title="A sportsbook line informed the model">book</span>`)
        + (Math.abs(r.edge) >= BIG ? `<span class="flag big" title="25+ point disagreement — the model is probably missing news">large gap</span>` : "");
      const url = safeUrl(r.url);
      const src = r.source === "kalshi" ? (url ? `<a class="link" href="${url}" target="_blank" rel="noopener">Kalshi ↗</a>` : "Kalshi") : esc(r.source.replace("book:", ""));
      const price = r.source === "kalshi" ? (r.price * 100).toFixed(0) + "¢" : (r.price > 0 ? "+" : "") + r.price;
      const status = r.inputs?.status ? ` <span class="badge warn" title="Injury designation">${esc(r.inputs.status)}</span>` : "";
      const tip = `our mean ${r.inputs?.our_mean ?? "–"} (${esc(r.inputs?.mean_src ?? "")})${r.inputs?.cv ? ", CV " + r.inputs.cv : ""}${r.spread != null ? ", spread " + Math.round(r.spread * 100) + "¢" : ""}`;
      return `<tr title="${esc(tip)}"><td class="l"><span class="pos ${esc(r.pos)}" style="color:var(--pc);font-weight:700;font-size:11px;margin-right:6px">${esc(r.pos)}</span>${esc(r.player)}${status}${flags}</td>
        <td class="l">${esc(r.team)}</td><td class="l">${esc(r.market_label)}</td><td class="l">${esc(r.rung)}</td><td class="l">${esc(r.side)}</td>
        <td>${pct(r.our_p)}</td><td>${pct(r.market_p)}</td><td>${price}</td><td class="${r.edge > 0 ? "edge-pos" : ""}">${r.edge > 0 ? "+" : ""}${r.edge.toFixed(1)}</td>
        <td class="${r.ev > 0 ? "ev-pos" : ""}">${r.ev > 0 ? "+" : ""}${(r.ev * 100).toFixed(1)}%</td><td>${(r.kelly * 100).toFixed(1)}%</td><td class="l">${src}</td></tr>`;
    }).join("") : `<tr><td colspan="${COLS.length}" class="l" style="text-align:center;color:var(--muted);padding:28px">Nothing matches these filters.</td></tr>`;
    $("rows").querySelector("thead").innerHTML = thead;
    $("rows").querySelector("tbody").innerHTML = body;
    $("rows").querySelectorAll("th.sortable").forEach(th => th.onclick = () => { const k = th.dataset.k; if (sortKey === k) sortAsc = !sortAsc; else { sortKey = k; sortAsc = ["player", "team", "market_label", "rung", "side", "source"].includes(k); } render(); });
  }

  ["market", "team", "pos", "minEdge", "search", "hideBig", "hiOnly"].forEach(id => $(id).addEventListener("input", render));
  render();
})();
