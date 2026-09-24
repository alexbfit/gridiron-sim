/* Gridiron Sim — shared page shell: header, footer, theme, owner mode, toasts, icons, (optional) accounts.
   Every page loads this with <script src="assets/shell.js"></script> right after config.js.
   It never touches projections, the sim or the optimizer — presentation only. */
(function () {
  const cfg = window.GRIDIRON_CONFIG || {};
  const root = document.documentElement;
  const store = {
    get(k) { try { return localStorage.getItem(k); } catch (e) { return null; } },
    set(k, v) { try { v == null ? localStorage.removeItem(k) : localStorage.setItem(k, v); } catch (e) { /* private mode */ } },
  };

  // ------------------------------------------------------------ icons (24px stroke set)
  const P = {
    lock: '<rect x="4" y="11" width="16" height="10" rx="2"/><path d="M8 11V7a4 4 0 0 1 8 0v4"/>',
    unlock: '<rect x="4" y="11" width="16" height="10" rx="2"/><path d="M8 11V7a4 4 0 0 1 7.8-1.2"/>',
    ban: '<circle cx="12" cy="12" r="9"/><path d="m5.6 5.6 12.8 12.8"/>',
    x: '<path d="M18 6 6 18M6 6l12 12"/>',
    search: '<circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/>',
    download: '<path d="M12 3v12m0 0-4.5-4.5M12 15l4.5-4.5"/><path d="M4 17v2a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-2"/>',
    sun: '<circle cx="12" cy="12" r="4"/><path d="M12 2v2m0 16v2M4.9 4.9l1.4 1.4m11.4 11.4 1.4 1.4M2 12h2m16 0h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/>',
    moon: '<path d="M20.5 14.5A8.5 8.5 0 0 1 9.5 3.5a8.5 8.5 0 1 0 11 11Z"/>',
    menu: '<path d="M4 7h16M4 12h16M4 17h16"/>',
    spark: '<path d="M12 3v4m0 10v4M3 12h4m10 0h4M6.3 6.3l2.4 2.4m6.6 6.6 2.4 2.4m0-11.4-2.4 2.4m-6.6 6.6-2.4 2.4"/>',
    info: '<circle cx="12" cy="12" r="9"/><path d="M12 11v5m0-8.5v.5"/>',
    alert: '<path d="M10.3 3.9 2.4 17.5A2 2 0 0 0 4.1 20.5h15.8a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0Z"/><path d="M12 9v4m0 3.5v.5"/>',
    check: '<path d="m5 12.5 4.5 4.5L19 7.5"/>',
    chart: '<path d="M4 20V10m6 10V4m6 16v-7m4 7H3"/>',
    user: '<circle cx="12" cy="8" r="4"/><path d="M4 21a8 8 0 0 1 16 0"/>',
    arrow: '<path d="M5 12h14m-6-6 6 6-6 6"/>',
    sliders: '<path d="M4 6h10m4 0h2M4 12h4m4 0h8M4 18h12m4 0h0"/><circle cx="16" cy="6" r="2"/><circle cx="10" cy="12" r="2"/><circle cx="18" cy="18" r="2"/>',
    layers: '<path d="m12 3 9 5-9 5-9-5 9-5Z"/><path d="m3 13 9 5 9-5"/>',
    trophy: '<path d="M8 21h8m-4-4v4M7 4h10v5a5 5 0 0 1-10 0V4Z"/><path d="M17 5h3v2a4 4 0 0 1-4 4M7 5H4v2a4 4 0 0 0 4 4"/>',
    book: '<path d="M4 5a2 2 0 0 1 2-2h13v16H6a2 2 0 0 0-2 2V5Z"/><path d="M4 19a2 2 0 0 1 2-2h13"/>',
    table: '<rect x="3" y="4" width="18" height="16" rx="2"/><path d="M3 10h18M9 10v10"/>',
    dice: '<rect x="4" y="4" width="16" height="16" rx="3"/><circle cx="9" cy="9" r="1.2" fill="currentColor"/><circle cx="15" cy="15" r="1.2" fill="currentColor"/><circle cx="15" cy="9" r="1.2" fill="currentColor"/><circle cx="9" cy="15" r="1.2" fill="currentColor"/>',
    clock: '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
    refresh: '<path d="M20 11a8 8 0 0 0-14.3-4.9L4 8m0-5v5h5M4 13a8 8 0 0 0 14.3 4.9L20 16m0 5v-5h-5"/>',
    copy: '<rect x="8" y="8" width="12" height="12" rx="2"/><path d="M16 8V6a2 2 0 0 0-2-2H6a2 2 0 0 0-2 2v8a2 2 0 0 0 2 2h2"/>',
    trash: '<path d="M4 7h16M10 11v6m4-6v6M6 7l1 13h10l1-13M9 7V4h6v3"/>',
    star: '<path d="m12 3 2.7 5.6 6.1.8-4.5 4.2 1.1 6-5.4-2.9-5.4 2.9 1.1-6L3.2 9.4l6.1-.8L12 3Z"/>',
    shield: '<path d="M12 3 4.5 6v6c0 4.5 3.2 8 7.5 9 4.3-1 7.5-4.5 7.5-9V6L12 3Z"/><path d="m9 12 2 2 4-4"/>',
    zap: '<path d="M13 2 4 14h7l-1 8 9-12h-7l1-8Z"/>',
    logout: '<path d="M15 4h3a2 2 0 0 1 2 2v12a2 2 0 0 1-2 2h-3M10 17l-5-5 5-5M5 12h11"/>',
    chevron: '<path d="m6 9 6 6 6-6"/>',
    plus: '<path d="M12 5v14M5 12h14"/>',
    minus: '<path d="M5 12h14"/>',
    eye: '<path d="M2 12s3.6-7 10-7 10 7 10 7-3.6 7-10 7S2 12 2 12Z"/><circle cx="12" cy="12" r="3"/>',
  };
  const icon = (name, cls = "icon") => `<svg class="${cls}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${P[name] || ""}</svg>`;
  const LOGO = `<svg class="brand-mark" viewBox="0 0 32 32" aria-hidden="true">
    <rect width="32" height="32" rx="9" fill="var(--accent)"/>
    <path d="M10 7v9.5h12V7M16 16.5V26" fill="none" stroke="var(--accent-ink)" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round"/></svg>`;

  // ------------------------------------------------------------ theme
  function applyTheme(t) { root.dataset.theme = t; store.set("gs_theme", t); document.querySelectorAll("[data-theme-toggle]").forEach(b => b.innerHTML = icon(t === "light" ? "moon" : "sun")); }
  applyTheme(store.get("gs_theme") || root.dataset.theme || "dark");

  // ------------------------------------------------------------ owner mode (Alex's own tools: Sunday-task lineups, news agents, real entries).
  // Not a security boundary — just keeps subscriber pages clean. Turn on once with ?owner=1, off with ?owner=0.
  const q = new URLSearchParams(location.search);
  if (q.has("owner")) store.set("gs_owner", q.get("owner") === "1" ? "1" : null);
  const isOwner = () => store.get("gs_owner") === "1";
  if (isOwner()) root.classList.add("owner");

  // ------------------------------------------------------------ header / footer
  const APP_NAV = [
    ["lineups.html", "Lineup Builder"],
    ["stats.html", "Player Stats"],
    ["results.html", "Track Record"],
    ["backtest.html", "Accuracy"],
    ["guide.html", "Guide"],
  ];
  const here = (location.pathname.split("/").pop() || "index.html").toLowerCase();

  function header(kind) {
    const nav = kind === "marketing"
      ? [["#how", "How it works"], ["#features", "Features"], ["#record", "Track record"], ["#pricing", "Pricing"], ["#faq", "FAQ"]]
      : APP_NAV;
    const cta = kind === "marketing"
      ? `<a class="btn btn-primary btn-sm" href="lineups.html">Open the builder ${icon("arrow")}</a>`
      : `<a class="btn btn-ghost btn-sm hide-sm" href="index.html">Home</a>`;
    return `<div class="inner">
      <a class="brand" href="index.html" aria-label="Gridiron Sim home">${LOGO}<span>Gridiron Sim</span><small class="owner-flag" title="Owner tools visible (?owner=0 to hide)">Owner</small></a>
      <nav class="main-nav" id="mainNav" aria-label="Main">${nav.map(([h, l]) => `<a href="${h}"${h.toLowerCase() === here ? ' aria-current="page"' : ""}>${l}</a>`).join("")}</nav>
      <div class="header-actions">
        <span id="accountSlot"></span>
        <button class="btn btn-ghost btn-icon btn-sm" data-theme-toggle aria-label="Toggle light / dark theme" title="Light / dark"></button>
        ${cta}
        <button class="btn btn-ghost btn-icon btn-sm menu-btn" id="menuBtn" aria-label="Menu" aria-expanded="false">${icon("menu")}</button>
      </div></div>`;
  }
  function footer(kind) {
    const legal = `Gridiron Sim is an independent research tool and is not affiliated with, endorsed by or sponsored by DraftKings, FanDuel or the NFL. Projections are estimates; daily fantasy contests involve risk and past results do not guarantee future results. Play within your means — must be of legal age in your state to enter paid contests. Problem gambling? Call 1-800-GAMBLER.`;
    if (kind === "app") return `<div class="app-footer">${legal}</div>`;
    return `<div class="inner">
      <div><a class="brand" href="index.html">${LOGO}<span>Gridiron Sim</span></a>
        <p style="margin-top:10px;max-width:320px">NFL game simulations and a lineup optimizer for DraftKings &amp; FanDuel, built on 10,000 simulated games per slate.</p></div>
      <div><h4>Product</h4><a href="lineups.html">Lineup Builder</a><a href="stats.html">Player Stats</a><a href="results.html">Track Record</a><a href="backtest.html">Accuracy</a></div>
      <div><h4>Learn</h4><a href="guide.html">Quick-start guide</a><a href="index.html#how">How it works</a><a href="index.html#faq">FAQ</a></div>
      <div><h4>Account</h4><a href="account.html">Sign in</a><a href="index.html#pricing">Pricing</a></div>
      <div class="legal">${legal}</div></div>`;
  }

  function mount() {
    const h = document.querySelector("[data-shell-header]");
    if (h) { h.classList.add("site-header"); h.innerHTML = header(h.dataset.shellHeader || "app"); }
    const f = document.querySelector("[data-shell-footer]");
    if (f) { const kind = f.dataset.shellFooter || "site"; f.classList.add(kind === "app" ? "app-shell-footer" : "site-footer"); f.innerHTML = footer(kind); }
    document.querySelectorAll("[data-icon]").forEach(el => { el.insertAdjacentHTML("afterbegin", icon(el.dataset.icon)); });
    applyTheme(root.dataset.theme);
    document.addEventListener("click", (e) => {
      const t = e.target.closest("[data-theme-toggle]");
      if (t) applyTheme(root.dataset.theme === "light" ? "dark" : "light");
      const m = e.target.closest("#menuBtn");
      if (m) { const nav = document.getElementById("mainNav"); const open = !nav.classList.contains("open"); nav.classList.toggle("open", open); m.setAttribute("aria-expanded", open); }
      else if (!e.target.closest("#mainNav")) document.getElementById("mainNav")?.classList.remove("open");
    });
    Account.init();
  }

  // ------------------------------------------------------------ toasts
  function toast(msg, kind = "") {
    let box = document.querySelector(".toasts");
    if (!box) { box = document.createElement("div"); box.className = "toasts"; box.setAttribute("role", "status"); box.setAttribute("aria-live", "polite"); document.body.appendChild(box); }
    const t = document.createElement("div"); t.className = "toast " + kind;
    t.innerHTML = icon(kind === "error" ? "alert" : kind === "ok" ? "check" : "info");
    const body = document.createElement("div"); body.textContent = String(msg); t.appendChild(body);   // plain text: never HTML
    box.appendChild(t);
    setTimeout(() => { t.style.transition = "opacity .3s"; t.style.opacity = "0"; setTimeout(() => t.remove(), 300); }, kind === "error" ? 6500 : 4200);
  }

  // ------------------------------------------------------------ accounts + subscription (OFF until config.AUTH_ENABLED = true)
  // Uses Supabase Auth (email magic link) and a `profiles` row written by the Stripe webhook (see billing/README.md).
  const Account = {
    client: null, user: null, profile: null, _done: null,
    ready: null,   // set right below the object so pages can await it before DOMContentLoaded
    enabled: () => !!cfg.AUTH_ENABLED,
    getClient() {
      if (!this.client && window.supabase && cfg.SUPABASE_URL) this.client = window.supabase.createClient(cfg.SUPABASE_URL, cfg.SUPABASE_ANON_KEY);
      return this.client;
    },
    active() {
      if (!this.enabled() || !cfg.REQUIRE_SUBSCRIPTION) return true;
      if (this.profile?.is_owner) return true;   // owner comes from the database, not from ?owner=1
      const s = this.profile?.subscription_status;
      return s === "active" || s === "trialing";
    },
    async init() {
      const slot = document.getElementById("accountSlot");
      if (!this.enabled()) { this._done(); return; }
      const c = this.getClient(); if (!c) { this._done(); return; }
      (async () => {
        const { data } = await c.auth.getSession();
        this.user = data?.session?.user || null;
        if (this.user) {
          try { const { data: p } = await c.from("profiles").select("*").eq("id", this.user.id).maybeSingle(); this.profile = p; } catch (e) { /* table not created yet */ }
        }
        if (slot) slot.innerHTML = this.user
          ? `<a class="btn btn-ghost btn-sm" href="account.html" title="${this.user.email}">${icon("user")}<span class="hide-sm">Account</span></a>`
          : `<a class="btn btn-outline btn-sm" href="account.html">Sign in</a>`;
        this.gate();
        // reload only on a real sign-in / sign-out after the page settled (not the initial session event)
        c.auth.onAuthStateChange((event, session) => {
          if (event === "INITIAL_SESSION" || event === "TOKEN_REFRESHED") return;
          if ((session?.user?.id || null) !== (this.user?.id || null)) location.reload();
        });
      })().catch(e => console.warn("account init", e)).finally(() => this._done());
    },
    // pages mark premium areas with data-requires-sub; when billing is on and the visitor has no active plan they see a paywall instead
    gate() {
      if (this.active()) return;
      document.querySelectorAll("[data-requires-sub]").forEach(el => {
        el.classList.add("paywalled");
        const w = document.createElement("div"); w.className = "paywall";
        w.innerHTML = `<div class="card card-pad" style="max-width:440px;text-align:center">
          <div class="empty" style="padding:8px 0 0"><div class="icon-wrap">${icon("zap")}</div><h3>${this.user ? "Start your subscription" : "Sign in to build lineups"}</h3>
          <p>${this.user ? "Your account doesn't have an active plan yet." : "Create a free account or sign in to continue."}</p></div>
          <a class="btn btn-primary btn-block" style="margin-top:16px" href="account.html">${this.user ? "See plans" : "Sign in"}</a></div>`;
        el.appendChild(w);
      });
    },
  };

  Account.ready = new Promise(r => { Account._done = r; });
  window.GS = { icon, toast, isOwner, store, Account, cfg };
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", mount); else mount();
})();
