/* GameTime Win — pricing page. Renders PLANS from config.js (via assets/plans.js); no product logic here. */
(function () {
  const cfg = window.GRIDIRON_CONFIG || {}, GP = window.GTW_PLANS, GS = window.GS;
  if (!GP) return;
  const { PLANS, NOTES, esc } = GP;
  const $ = (id) => document.getElementById(id);
  let interval = "monthly";

  function renderPlans() {
    const cards = GP.cards(interval) + (PLANS.slate ? GP.card(PLANS.slate, interval) : "");
    $("plans").innerHTML = cards;
    GP.wireCheckout($("plans"));
  }

  function renderCompare() {
    const yes = `<span class="yes">${GS.icon("check")}</span>`, no = `<span class="no">—</span>`;
    const F = PLANS.free || {}, S = PLANS.starter || {}, P = PLANS.pro || {};
    const price = (p) => interval === "season" && p.price?.season ? `${p.price.season} <small class="muted">/ season</small>` : `${p.price?.monthly || "$0"}${p.key === "free" ? "" : ' <small class="muted">/ mo</small>'}`;
    const rows = [
      ["Lineups per build", `<b>${cfg.FREE_LINEUPS ?? F.lineups ?? 3}</b> per slate`, `<b>${S.lineups || 50}</b>`, `<b>${P.lineups || 500}</b>`],
      ["Simple Mode (drop in your DraftKings file, pick 2, build)", yes, yes, yes],
      ["Public Track Record and backtests", yes, yes, yes],
      ["Player stats and simulated ranges", yes, yes, yes],
      ["Full Lineup Builder (locks, exposure, stacks, settings)", no, yes, yes],
      ["DraftKings entries edit file (fills every entry)", no, yes, yes],
      ["1 PM and 4 PM late-swap files", no, yes, yes],
      ["Contest simulator", no, yes, yes],
      ["Sunday news notes", no, yes, yes],
      ["Projections view and CSV export", no, no, yes],
      ["Upload your own projections", no, no, yes],
      ["Every sport (NBA, MLB, NHL, golf, more)", no, no, yes],
      ["Private Discord", no, no, yes],
      ["7-day free trial", no, yes, yes],
      ["Founding price lock", no, cfg.FOUNDING ? yes : no, cfg.FOUNDING ? yes : no],
    ];
    $("compareTable").innerHTML = `<thead><tr><th>Feature</th><th>${esc(F.name || "Free")}<br>${price(F)}</th><th class="hl">${esc(S.name || "Starter")}<br>${price(S)}</th><th>${esc(P.name || "Pro")}<br>${price(P)}</th></tr></thead>
      <tbody>${rows.map(r => `<tr><td>${r[0]}</td><td>${r[1]}</td><td>${r[2]}</td><td>${r[3]}</td></tr>`).join("")}</tbody>`;
  }

  function renderCta() {
    const free = PLANS.free;
    if (!cfg.BILLING_LIVE) {
      $("ctaHead").textContent = "Paid plans open soon. The beta is free.";
      $("ctaSub").textContent = "Join now and your founding price is locked in the day paid plans launch.";
      $("ctaBtn").innerHTML = (cfg.AUTH_ENABLED ? `<a class="btn btn-primary btn-lg" href="signup.html">Create free account <span data-icon="arrow"></span></a>` : `<a class="btn btn-primary btn-lg" href="index.html#join">Join the free beta <span data-icon="arrow"></span></a>`);
    } else if (free) {
      $("ctaBtn").innerHTML = GP.ctaHTML(free, interval, "btn btn-lg btn-primary");
    }
    if (document.readyState !== "loading") document.querySelectorAll("#ctaBtn [data-icon]").forEach(el => el.insertAdjacentHTML("afterbegin", GS.icon(el.dataset.icon)));   // shell.js fills these itself before DOMContentLoaded
  }

  function render() { renderPlans(); renderCompare(); renderCta(); }

  $("intervalSeg").addEventListener("click", (e) => {
    const b = e.target.closest("button"); if (!b) return;
    interval = b.dataset.interval;
    $("intervalSeg").querySelectorAll("button").forEach(x => x.setAttribute("aria-pressed", String(x === b)));
    $("saveNote").textContent = interval === "season" ? (NOTES.season || "Season pass runs through the Super Bowl.") : "Season pass: pay once, play through the Super Bowl";
    render();
  });

  if (!cfg.BILLING_LIVE) {
    $("heroSub").textContent = "These are the plans that open when the beta ends. Beta players keep the founding price. Until then everything below is free — join the beta and start building.";
  }
  render();
})();
