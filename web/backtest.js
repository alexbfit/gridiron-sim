/* Gridiron Sim — backtest report. Reads data/backtest.json written by jobs/backtest.py. */
(async function () {
  const $ = (id) => document.getElementById(id);
  const tt = $("tt");
  const MODELS = [["sim", "Sim", "--s-sim"], ["baseline", "Baseline", "--s-base"], ["naive", "Naive", "--s-naive"]];
  const css = (v) => getComputedStyle(document.documentElement).getPropertyValue(v).trim();

  let d;
  try { d = await (await fetch("data/backtest.json", { cache: "no-cache" })).json(); }
  catch (e) { $("meta").textContent = "No backtest results yet — run jobs/backtest.py."; return; }

  const m = d.meta, all = d.positions.ALL;
  $("meta").textContent = `${m.season} weeks ${m.weeks[0]}–${m.weeks[1]} · ${all.sim.n.toLocaleString()} player-weeks scored · ${m.n_sims.toLocaleString()} sims per week · run ${m.run_at.slice(0, 10)}`;

  // ---- headline tiles
  const tiles = [
    ["Sim MAE", all.sim.mae.toFixed(2), `baseline ${all.baseline.mae.toFixed(2)} · naive ${all.naive.mae.toFixed(2)}`],
    ["Sim correlation", all.sim.r.toFixed(3), `baseline ${all.baseline.r.toFixed(3)} · naive ${all.naive.r.toFixed(3)}`],
    ["Sim bias", (all.sim.bias > 0 ? "+" : "") + all.sim.bias.toFixed(2), `baseline ${all.baseline.bias.toFixed(2)}`],
    ["Actual ≤ p10 / p90", `${Math.round(all.calibration.coverage.p10 * 100)}% / ${Math.round(all.calibration.coverage.p90 * 100)}%`, "target 10% / 90%"],
    ["Player-weeks", all.sim.n.toLocaleString(), `${Object.keys(d.weeks).length} weeks · QB/RB/WR/TE`],
  ];
  $("tiles").innerHTML = tiles.map(([k, v, s]) => `<div class="tile"><div class="k">${k}</div><div class="v">${v}</div><div class="s">${s}</div></div>`).join("");

  // ---- position table
  const order = ["QB", "RB", "WR", "TE", "ALL"].filter(p => d.positions[p]);
  let h = `<tr><th class="l">Pos</th><th>n</th>` + MODELS.map(([, n]) => `<th>${n} MAE</th><th>${n} r</th><th>${n} bias</th>`).join("") + `</tr>`;
  order.forEach(p => {
    const x = d.positions[p];
    const bestMae = Math.min(...MODELS.map(([k]) => x[k].mae)), bestR = Math.max(...MODELS.map(([k]) => x[k].r));
    h += `<tr><td class="l"><b>${p}</b></td><td>${x.sim.n}</td>` + MODELS.map(([k]) =>
      `<td class="${x[k].mae === bestMae ? "best" : ""}">${x[k].mae.toFixed(2)}</td><td class="${x[k].r === bestR ? "best" : ""}">${x[k].r.toFixed(3)}</td><td>${x[k].bias > 0 ? "+" : ""}${x[k].bias.toFixed(2)}</td>`).join("") + `</tr>`;
  });
  $("pos").innerHTML = h;

  // ---- coverage table
  const cov = all.calibration.coverage;
  $("cov").innerHTML = `<tr><th class="l">Quantile</th>${Object.keys(cov).map(k => `<th>${k}</th>`).join("")}</tr>
    <tr><td class="l">Actual ≤ quantile</td>${Object.values(cov).map(v => `<td>${Math.round(v * 100)}%</td>`).join("")}</tr>
    <tr><td class="l">Target</td>${Object.keys(cov).map(k => `<td>${k.slice(1)}%</td>`).join("")}</tr>`;

  // ---- weekly MAE line chart
  const weeks = Object.keys(d.weeks).map(Number).sort((a, b) => a - b);
  const W = 560, H = 260, L = 36, R = 60, T = 12, B = 28;
  const ys = MODELS.flatMap(([k]) => weeks.map(w => d.weeks[w][k].mae));
  const y0 = Math.floor(Math.min(...ys) - 0.5), y1 = Math.ceil(Math.max(...ys) + 0.5);
  const x = (w) => L + (w - weeks[0]) / (weeks[weeks.length - 1] - weeks[0]) * (W - L - R);
  const y = (v) => T + (1 - (v - y0) / (y1 - y0)) * (H - T - B);
  let svg = `<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="Weekly MAE by model">`;
  for (let v = y0; v <= y1; v++) svg += `<line x1="${L}" x2="${W - R}" y1="${y(v)}" y2="${y(v)}" stroke="#2a3140" stroke-width="1"/><text x="${L - 6}" y="${y(v) + 4}" font-size="10" fill="#8b95a7" text-anchor="end">${v}</text>`;
  weeks.forEach(w => svg += `<text x="${x(w)}" y="${H - 8}" font-size="10" fill="#8b95a7" text-anchor="middle">${w}</text>`);
  MODELS.forEach(([k, name, tok]) => {
    const c = css(tok);
    const pts = weeks.map(w => `${x(w)},${y(d.weeks[w][k].mae)}`).join(" ");
    svg += `<polyline points="${pts}" fill="none" stroke="${c}" stroke-width="2" stroke-linejoin="round"/>`;
    weeks.forEach(w => svg += `<circle cx="${x(w)}" cy="${y(d.weeks[w][k].mae)}" r="3" fill="${c}" stroke="#161b22" stroke-width="2"/>`);
    const last = weeks[weeks.length - 1];
    svg += `<text x="${x(last) + 8}" y="${y(d.weeks[last][k].mae) + 4}" font-size="11" fill="#e6e9ef">${name}</text>`;
  });
  svg += `<rect id="wk-hit" x="${L}" y="${T}" width="${W - L - R}" height="${H - T - B}" fill="transparent"/><line id="wk-x" y1="${T}" y2="${H - B}" stroke="#8b95a7" stroke-dasharray="3 3" style="display:none"/></svg>`;
  $("weekly").innerHTML = `<div class="legend">${MODELS.map(([, n, tok]) => `<span style="--c:${css(tok)}">${n}</span>`).join("")}<span style="margin-left:auto">MAE, DK pts</span></div>` + svg;
  const hit = document.getElementById("wk-hit"), xl = document.getElementById("wk-x");
  hit.addEventListener("mousemove", (e) => {
    const r = hit.getBoundingClientRect(); const fx = L + (e.clientX - r.left) / r.width * (W - L - R);
    const w = weeks.reduce((a, b) => Math.abs(x(b) - fx) < Math.abs(x(a) - fx) ? b : a);
    xl.setAttribute("x1", x(w)); xl.setAttribute("x2", x(w)); xl.style.display = "";
    tt.style.display = "block"; tt.style.left = e.clientX + 12 + "px"; tt.style.top = e.clientY + 12 + "px";
    tt.innerHTML = `<b>Week ${w}</b> · ${d.weeks[w].sim.n} players<br>` + MODELS.map(([k, n]) => `${n}: MAE ${d.weeks[w][k].mae.toFixed(2)} · r ${d.weeks[w][k].r.toFixed(2)}`).join("<br>");
  });
  hit.addEventListener("mouseleave", () => { tt.style.display = "none"; xl.style.display = "none"; });

  // ---- PIT histogram
  const hist = all.calibration.pit_hist, n = hist.reduce((a, b) => a + b, 0), exp = n / 10;
  const PW = 560, PH = 220, PL = 36, PR = 12, PT = 12, PB = 28, bw = (PW - PL - PR) / 10;
  const top = Math.max(...hist, exp) * 1.1;
  const py = (v) => PT + (1 - v / top) * (PH - PT - PB);
  let ps = `<svg viewBox="0 0 ${PW} ${PH}" role="img" aria-label="PIT histogram">`;
  hist.forEach((v, i) => {
    const bx = PL + i * bw + 1, bh = PH - PB - py(v);
    ps += `<rect class="pbar" data-i="${i}" x="${bx}" y="${py(v)}" width="${bw - 2}" height="${bh}" rx="3" fill="${css("--s-sim")}"/>`;
    ps += `<text x="${bx + (bw - 2) / 2}" y="${PH - 8}" font-size="10" fill="#8b95a7" text-anchor="middle">${i * 10}–${i * 10 + 10}</text>`;
  });
  ps += `<line x1="${PL}" x2="${PW - PR}" y1="${py(exp)}" y2="${py(exp)}" stroke="#e6e9ef" stroke-dasharray="4 4"/><text x="${PL + 4}" y="${py(exp) - 5}" font-size="10" fill="#e6e9ef">perfect calibration = ${Math.round(exp)} per bin</text>`;
  ps += `</svg>`;
  $("pit").innerHTML = ps;
  document.querySelectorAll(".pbar").forEach(b => {
    b.addEventListener("mousemove", (e) => { const i = +b.dataset.i; tt.style.display = "block"; tt.style.left = e.clientX + 12 + "px"; tt.style.top = e.clientY + 12 + "px";
      tt.innerHTML = `Actual fell in the ${i * 10}–${i * 10 + 10}th percentile of the sim<br><b>${hist[i]}</b> players (${(100 * hist[i] / n).toFixed(1)}%, expect 10%)`; });
    b.addEventListener("mouseleave", () => tt.style.display = "none");
  });
})();
