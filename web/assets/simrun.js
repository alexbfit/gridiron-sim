/* GameTime Win: the build screen. A full-page view shown while lineups are being built.
   It plays through the stored simulations (the 10,000 games are simulated on our servers each morning; the browser
   holds a sample of every player's simulated scores), shows the best games it finds for the top players from those
   real draws, then tracks the optimizer's real progress and reveals the lineups when both the work and the
   minimum showing time are done.

   GS.SimRun.open({ label, nSims, nLineups, games: [{ away, home }], players: [{ name, pos, team, mean, best, draws }], canSkip })
     -> { progress(fraction 0..1, text), finish() -> Promise, close() }                                                  */
(function () {
  const GS = window.GS || (window.GS = {});
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const sleep = (ms) => new Promise(r => setTimeout(r, ms));
  const CSS = `
.simrun { position: fixed; inset: 0; z-index: 400; background: var(--bg); color: var(--text); overflow: auto; opacity: 0; transition: opacity .25s ease; }
.simrun.on { opacity: 1; }
.simrun-in { max-width: 1040px; margin: 0 auto; padding: clamp(20px, 5vh, 56px) 24px 32px; min-height: 100%; display: flex; flex-direction: column; gap: 22px; }
.sr-top { display: flex; justify-content: space-between; align-items: flex-end; gap: 20px; flex-wrap: wrap; }
.sr-label { color: var(--muted); font-size: 15px; font-weight: 600; }
.sr-title { font: 600 clamp(26px, 4vw, 38px)/1.1 var(--font-display); margin-top: 4px; min-height: 1.1em; }
.sr-count { text-align: right; }
.sr-count b { display: block; font: 600 clamp(40px, 7vw, 68px)/1 var(--font-display); font-variant-numeric: tabular-nums; letter-spacing: -.01em; }
.sr-count span { color: var(--muted); font-size: 14px; }
.sr-bar { height: 6px; border-radius: 999px; background: var(--surface-3); overflow: hidden; }
.sr-bar i { display: block; height: 100%; width: 0; background: var(--accent); border-radius: 999px; }

/* the field: one lane per game on the slate */
.sr-field { position: relative; border-radius: var(--radius-lg); overflow: hidden; background: var(--surface-3); padding: 14px 0;
  background-image: repeating-linear-gradient(90deg, transparent 0 calc(10% - 2px), var(--surface) calc(10% - 2px) 10%); }
.sr-lane { position: relative; height: 22px; display: flex; align-items: center; }
.sr-lane + .sr-lane { margin-top: 3px; }
.sr-lane span { position: relative; z-index: 2; margin-left: 12px; padding: 1px 8px; border-radius: 999px; background: var(--surface); font: 600 12.5px var(--font); color: var(--text-2); white-space: nowrap; }
.sr-lane i { position: absolute; left: 0; top: 50%; width: 12px; height: 8px; margin-top: -4px; border-radius: 50%; background: var(--accent); opacity: .9;
  animation: sr-drive var(--d, 2.4s) ease-in-out var(--delay, 0s) infinite alternate; }
.sr-lane em { position: absolute; left: 0; right: 0; top: 50%; height: 1px; background: color-mix(in srgb, var(--accent) 22%, transparent); }
@keyframes sr-drive { from { left: var(--a, 16%); } to { left: var(--b, 88%); } }
.simrun.settled .sr-lane i { animation-play-state: paused; opacity: .35; }

.sr-panels { display: grid; grid-template-columns: minmax(0, 1.15fr) minmax(0, 1fr); gap: 18px; align-items: start; }
.sr-panel { background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius-lg); padding: 16px 18px; min-height: 236px; }
.sr-panel h2 { font: 600 18px var(--font-display); margin-bottom: 8px; }
.sr-panel p.sr-note { color: var(--muted); font-size: 14.5px; }
.sr-feed { list-style: none; margin: 0; padding: 0; font-size: 14.5px; font-variant-numeric: tabular-nums; }
.sr-feed[hidden], .sr-players[hidden] { display: none; }
.sr-feed li { display: grid; grid-template-columns: 104px minmax(0, 1fr); gap: 10px; padding: 5px 0; border-top: 1px solid var(--border); color: var(--text-2); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.sr-feed li:first-child { border-top: 0; color: var(--text); }
.sr-feed li b { font-weight: 600; color: var(--muted); }
.sr-feed li span { overflow: hidden; text-overflow: ellipsis; }
.sr-players { list-style: none; margin: 0; padding: 0; }
.sr-players li { display: grid; grid-template-columns: 40px minmax(0, 1fr) 96px 64px; gap: 10px; align-items: center; padding: 6px 0; border-top: 1px solid var(--border);
  opacity: 0; transform: translateY(4px); transition: opacity .3s ease, transform .3s ease; }
.sr-players li:first-child { border-top: 0; }
.sr-players li.in { opacity: 1; transform: none; }
.sr-players .nm { font-weight: 600; font-size: 15px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.sr-players .nm small { color: var(--muted); font-weight: 400; margin-left: 6px; font-size: 12.5px; }
.sr-players .best { text-align: right; font: 600 17px var(--font-display); font-variant-numeric: tabular-nums; }
.sr-players .best small { display: block; font: 400 11.5px var(--font); color: var(--muted); }
.sr-hist { display: flex; align-items: flex-end; gap: 2px; height: 26px; }
.sr-hist i { flex: 1; background: var(--accent); opacity: .75; border-radius: 1px; height: 0; transition: height .5s ease; }
.sr-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(18px, 1fr)); gap: 5px; margin-top: 12px; }
.sr-grid i { aspect-ratio: 1; border-radius: 4px; background: var(--surface-3); transition: background .25s ease; }
.sr-grid i.on { background: var(--accent); }
.sr-steps { list-style: none; margin: -8px 0 0; padding: 0; display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 10px; font-size: 14.5px; color: var(--muted); }
.sr-steps li { display: flex; align-items: center; gap: 8px; padding-top: 4px; }
.sr-steps li b { width: 18px; height: 18px; border-radius: 50%; flex: none; border: 2px solid var(--border-strong); display: grid; place-items: center; font-size: 11px; color: transparent; }
.sr-steps li.now { color: var(--text); font-weight: 600; }
.sr-steps li.now b { border-color: var(--accent); border-right-color: transparent; animation: spin .8s linear infinite; }
.sr-steps li.done b { background: var(--accent); border-color: var(--accent); color: var(--accent-ink); }
.sr-skip { align-self: flex-end; }
@media (max-width: 760px) {
  .sr-panels { grid-template-columns: 1fr; } .sr-panel { min-height: 0; }
  .sr-steps { grid-template-columns: 1fr 1fr; } .sr-count { text-align: left; }
  .sr-lane:nth-child(n+7) { display: none; }
  .sr-players li { grid-template-columns: 40px minmax(0, 1fr) 60px; } .sr-hist { display: none; }
}
@media (prefers-reduced-motion: reduce) { .sr-lane i, .sr-steps li.now b { animation: none; } .sr-hist i, .sr-players li, .simrun { transition: none; } }`;

  // the shape of a player's simulated scores; the top 1% of draws are folded into the last bar so one freak game doesn't flatten it
  function histogram(draws, bins = 14) {
    if (!draws || draws.length < 20) return null;
    const sorted = Float32Array.from(draws).sort(), hi = sorted[Math.floor(0.99 * (sorted.length - 1))], lo = sorted[0];
    if (!(hi > lo)) return null;
    const h = new Array(bins).fill(0);
    for (let i = 0; i < draws.length; i++) h[Math.max(0, Math.min(bins - 1, Math.floor((draws[i] - lo) / (hi - lo) * bins)))]++;
    const mx = Math.max(...h);
    return h.map(v => v / mx);
  }

  function open(o) {
    const reduced = matchMedia("(prefers-reduced-motion: reduce)").matches;
    const nSims = o.nSims || 10000, nL = Math.max(1, o.nLineups || 1), players = (o.players || []).slice(0, 8), games = (o.games || []).slice(0, 13);
    // how long each stage shows at minimum (ms); the lineup stage also waits for the real work
    const T = reduced ? { play: 500, best: 500, build: 300, rank: 300 } : { play: 4200, best: 3000, build: 1400, rank: 1100 };
    if (!document.getElementById("simrunCss")) { const st = document.createElement("style"); st.id = "simrunCss"; st.textContent = CSS; document.head.appendChild(st); }
    const el = document.createElement("div");
    el.className = "simrun"; el.setAttribute("role", "dialog"); el.setAttribute("aria-modal", "true"); el.setAttribute("aria-label", "Building your lineups");
    el.innerHTML = `<div class="simrun-in">
      <div class="sr-top">
        <div><div class="sr-label">${esc(o.label || "")}</div><h1 class="sr-title" id="srTitle" aria-live="polite"></h1></div>
        <div class="sr-count"><b id="srNum">0</b><span>of ${nSims.toLocaleString()} simulated games</span></div>
      </div>
      <div class="sr-bar"><i id="srBar"></i></div>
      <ol class="sr-steps" id="srSteps">
        <li><b>✓</b>Play every game</li><li><b>✓</b>Score every player</li><li><b>✓</b>Build the lineups</li><li><b>✓</b>Rank them</li>
      </ol>
      ${games.length ? `<div class="sr-field" aria-hidden="true">${games.map((g, i) => {
        const a = 14 + (i * 37) % 30, b = 62 + (i * 53) % 34;
        return `<div class="sr-lane"><em></em><span>${esc(g.away)} at ${esc(g.home)}</span><i style="--a:${a}%;--b:${b}%;--d:${(1.5 + (i * 0.37) % 1.4).toFixed(2)}s;--delay:-${(i * 0.41 % 2).toFixed(2)}s"></i></div>`;
      }).join("")}</div>` : ""}
      <div class="sr-panels">
        <section class="sr-panel"><h2 id="srLeft">Games as they finish</h2>
          <ul class="sr-feed" id="srFeed"></ul>
          <ul class="sr-players" id="srPlayers" hidden>${players.map(p => `<li>
            <span class="pos ${esc(["QB", "RB", "WR", "TE"].includes(p.pos) ? p.pos : "DST")}">${esc(p.pos)}</span>
            <span class="nm">${esc(p.name)}<small>${esc(p.team || "")}</small></span>
            <span class="sr-hist">${(histogram(p.draws) || []).map(() => "<i></i>").join("")}</span>
            <span class="best"><span data-best>${Number(p.best || 0).toFixed(1)}</span><small>best day</small></span></li>`).join("")}</ul>
          ${players.some(p => p.draws) ? "" : `<p class="sr-note" id="srWait">Reading every player's simulated scores.</p>`}
        </section>
        <section class="sr-panel"><h2>Your lineups</h2>
          <p class="sr-note" id="srNote">Waiting for the games to finish.</p>
          <div class="sr-grid" id="srGrid">${Array.from({ length: Math.min(nL, 150) }, () => "<i></i>").join("")}</div>
        </section>
      </div>
      ${o.canSkip ? `<button class="btn btn-ghost btn-sm sr-skip" type="button" id="srSkip">Skip</button>` : ""}
    </div>`;
    document.body.appendChild(el);
    const prevOverflow = document.body.style.overflow; document.body.style.overflow = "hidden";
    requestAnimationFrame(() => el.classList.add("on"));

    const $ = (id) => el.querySelector("#" + id);
    const steps = [...$("srSteps").children], cells = [...$("srGrid").children], rows = [...$("srPlayers").children];
    const hists = players.map(p => histogram(p.draws));
    let work = 0, workText = "", workDone = false, skipped = false, closed = false, resolveDone;
    const done = new Promise(r => { resolveDone = r; });
    const stage = (i, title) => { steps.forEach((li, k) => { li.className = k < i ? "done" : k === i ? "now" : ""; }); $("srTitle").textContent = title; };
    const bar = (f) => { $("srBar").style.width = (Math.max(0, Math.min(1, f)) * 100).toFixed(1) + "%"; };
    const animate = (ms, fn) => new Promise(res => {
      const t0 = performance.now();
      const tick = (t) => { if (closed) return res(); const f = skipped ? 1 : Math.min(1, (t - t0) / ms); fn(f); if (f < 1) requestAnimationFrame(tick); else res(); };
      requestAnimationFrame(tick);
    });
    const ease = (f) => 1 - Math.pow(1 - f, 2.2);

    (async () => {
      // 1. play every game: the counter runs up to the number of simulations
      stage(0, `Playing out ${nSims.toLocaleString()} games`);
      const feed = $("srFeed"), withDraws = players.filter(p => p.draws && p.draws.length);
      let lastFeed = 0;
      await animate(T.play, (f) => {
        const g = Math.round(ease(f) * nSims);
        $("srNum").textContent = g.toLocaleString(); bar(0.4 * f);
        // every so often, show one of the stored games: who scored what in it
        if (withDraws.length && g - lastFeed > nSims / 28 && g > 0) {
          lastFeed = g;
          const k = (g * 7919) % withDraws[0].draws.length;
          const top = withDraws.map(p => [p.name.split(" ").slice(-1)[0], p.draws[k % p.draws.length] * (p.scale || 1)]).sort((a, b) => b[1] - a[1]).slice(0, 3);
          const li = document.createElement("li");
          li.innerHTML = `<b>Game ${g.toLocaleString()}</b><span>${top.map(([n, v]) => `${esc(n)} ${v.toFixed(1)}`).join(", ")}</span>`;
          feed.prepend(li); while (feed.children.length > 7) feed.lastChild.remove();
        }
      });
      $("srNum").textContent = nSims.toLocaleString(); el.classList.add("settled");
      // 2. score every player: the top players' simulated scores, from the stored draws
      stage(1, "Finding every player's best games");
      if (rows.length) { $("srLeft").textContent = "Best games found"; feed.hidden = true; $("srPlayers").hidden = false; await sleep(30); }
      const per = rows.length ? T.best / rows.length : T.best;
      for (let i = 0; i < rows.length && !closed; i++) {
        rows[i].classList.add("in");
        const h = hists[i]; if (h) [...rows[i].querySelectorAll(".sr-hist i")].forEach((b, k) => { b.style.height = Math.max(6, Math.round(h[k] * 100)) + "%"; });
        bar(0.4 + 0.2 * (i + 1) / rows.length);
        if (!skipped) await sleep(per);
      }
      if (!rows.length && !skipped) await sleep(T.best);
      // 3. build: follows the optimizer's real progress
      stage(2, `Building ${nL} lineup${nL > 1 ? "s" : ""}`);
      const t3 = performance.now();
      while (!closed && !(workDone && (skipped || performance.now() - t3 >= T.build))) {
        const shown = workDone ? Math.min(1, (performance.now() - t3) / T.build) : Math.min(work, 0.97);
        const on = Math.round(shown * cells.length); cells.forEach((c, k) => c.classList.toggle("on", k < on));
        $("srNote").textContent = workText || `Testing combinations against every simulated game.`;
        bar(0.6 + 0.3 * shown);
        await sleep(80);
      }
      cells.forEach(c => c.classList.add("on"));
      // 4. rank
      stage(3, "Ranking your lineups");
      $("srNote").textContent = `${nL} lineup${nL > 1 ? "s" : ""} built. Putting the strongest first.`;
      await animate(T.rank, (f) => bar(0.9 + 0.1 * f));
      steps.forEach(li => { li.className = "done"; });
      close(); resolveDone();
    })();

    function close() {
      if (closed) return; closed = true;
      el.classList.remove("on"); document.body.style.overflow = prevOverflow;
      setTimeout(() => el.remove(), 260);
    }
    $("srSkip")?.addEventListener("click", () => { skipped = true; });
    return {
      progress(f, text) { work = Math.max(work, Math.min(1, f || 0)); if (text) workText = text; },
      finish() { workDone = true; return done; },      // resolves once the screen has played through and closed
      close() { close(); resolveDone(); },             // on an error: drop the screen at once
    };
  }

  GS.SimRun = { open };
})();
