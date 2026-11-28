/* GameTime Win: shared page shell. Header, footer, theme, toasts, icons, sign-in and the membership gate.
   Every page loads this with <script src="assets/shell.js"></script> right after config.js.
   It never touches projections, the sim or the optimizer. Presentation and access only. */
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
  const LOGO = `<svg class="brand-mark" viewBox="0 0 32 32" aria-hidden="true"><circle cx="16" cy="16" r="16" fill="var(--accent)"/>
    <g transform="rotate(-35 16 16)" fill="none" stroke="var(--accent-ink)" stroke-linecap="round"><ellipse cx="16" cy="16" rx="10" ry="6" stroke-width="1.8"/>
    <path d="M12 16h8M13.5 14.6v2.8M16 14.6v2.8M18.5 14.6v2.8" stroke-width="1.5"/></g></svg>`;
  const BRAND = `${LOGO}<span>GameTime<b class="brand-win">Win</b></span>`;

  // ------------------------------------------------------------ theme (light is the default)
  function applyTheme(t) { root.dataset.theme = t === "dark" ? "dark" : "light"; store.set("gs_theme", root.dataset.theme); }
  applyTheme(store.get("gs_theme") || "light");

  // ------------------------------------------------------------ owner tools
  // Owner status comes from the database (profiles.is_owner), never from the URL. The cached flag only avoids a flash of
  // hidden owner blocks on the next page load; Account.init() rewrites it from the real profile every time.
  const isOwner = () => store.get("gs_owner") === "1";
  if (isOwner()) root.classList.add("owner");

  // ------------------------------------------------------------ pages
  const here = (location.pathname.split("/").pop() || "index.html").toLowerCase();
  // main navigation, in the order a Sunday goes
  const NAV = [
    ["home.html", "Home"],
    ["simple.html", "Lineups", ["simple.html", "lineups.html"]],
    ["stats.html", "Players"],
    ["edges.html", "Props"],
    ["results.html", "Results", ["results.html", "backtest.html"]],
    ["guide.html", "Help"],
  ];
  // second-level tabs, shown under the header on the pages that have siblings
  const SUBNAV = {
    "simple.html": [["simple.html", "Quick build"], ["lineups.html", "Advanced builder"]],
    "lineups.html": [["simple.html", "Quick build"], ["lineups.html", "Advanced builder"]],
    "results.html": [["results.html", "This season"], ["backtest.html", "Projection accuracy"]],
    "backtest.html": [["results.html", "This season"], ["backtest.html", "Projection accuracy"]],
  };
  // Everything a member uses. Signed-out visitors are sent to log in; signed-in visitors without a membership are
  // sent to the payment step. account.html needs a sign-in but not a membership (it is where billing is managed).
  const MEMBER_PAGES = new Set(["home.html", "simple.html", "lineups.html", "stats.html", "edges.html", "results.html", "backtest.html", "guide.html"]);
  const SIGNED_IN_PAGES = new Set([...MEMBER_PAGES, "account.html", "status.html"]);
  const OWNER_PAGES = new Set(["status.html"]);
  const authOn = () => !!cfg.AUTH_ENABLED;
  const gated = () => authOn() && SIGNED_IN_PAGES.has(here);
  if (gated()) {                       // hide the page until the session check finishes (no flash of the tool)
    root.classList.add("gate-pending");
    const st = document.createElement("style"); st.textContent = "html.gate-pending main, html.gate-pending [data-shell-footer], html.gate-pending .subnav{visibility:hidden}"; document.head.appendChild(st);
  }

  const LEGAL = `GameTime Win is an independent research tool. It is not affiliated with, endorsed by or sponsored by DraftKings, FanDuel or the NFL. Projections are estimates. Daily fantasy contests involve risk, and past results do not guarantee future results. You must be of legal age in your state to enter paid contests. Problem gambling? Call 1-800-GAMBLER.`;
  const FOOT_LINKS = `<a href="terms.html">Terms</a><a href="privacy.html">Privacy</a><a href="responsible.html">Responsible play</a>${cfg.CONTACT_EMAIL ? `<a href="mailto:${cfg.CONTACT_EMAIL}">Contact</a>` : ""}`;

  function header() {
    const isApp = SIGNED_IN_PAGES.has(here);
    const nav = isApp ? NAV.map(([h, l, group]) => `<a href="${h}"${(group || [h]).includes(here) ? ' aria-current="page"' : ""}>${l}</a>`).join("") : "";
    return `<div class="inner">
      <a class="brand" href="${isApp ? "home.html" : "index.html"}" aria-label="GameTime Win home">${BRAND}</a>
      ${isApp ? `<nav class="main-nav" id="mainNav" aria-label="Main">${nav}</nav>` : ""}
      <div class="header-actions">
        <span id="accountSlot">${isApp ? "" : `<a class="btn btn-sm" href="signup.html?mode=signin">Log in</a>`}</span>
        ${isApp ? `<button class="btn btn-ghost btn-icon btn-sm menu-btn" id="menuBtn" aria-label="Menu" aria-expanded="false">${icon("menu")}</button>` : ""}
      </div></div>`;
  }
  function subnav() {
    const tabs = SUBNAV[here]; if (!tabs) return null;
    const el = document.createElement("nav"); el.className = "subnav"; el.setAttribute("aria-label", "Section");
    el.innerHTML = `<div class="inner">${tabs.map(([h, l]) => `<a href="${h}"${h === here ? ' aria-current="page"' : ""}>${l}</a>`).join("")}</div>`;
    return el;
  }
  function footer(kind) {
    return `<div class="${kind === "app" ? "app-footer" : "inner"}">${FOOT_LINKS}<div class="legal">${LEGAL}</div></div>`;
  }

  function mount() {
    const h = document.querySelector("[data-shell-header]");
    if (h) { h.classList.add("site-header"); h.innerHTML = header(); const sn = subnav(); if (sn) h.after(sn); }
    const f = document.querySelector("[data-shell-footer]");
    if (f) { const kind = f.dataset.shellFooter || "site"; f.classList.add(kind === "app" ? "app-shell-footer" : "site-footer"); f.innerHTML = footer(kind); }
    document.querySelectorAll("[data-icon]").forEach(el => { el.insertAdjacentHTML("afterbegin", icon(el.dataset.icon)); });
    document.addEventListener("click", (e) => {
      const m = e.target.closest("#menuBtn");
      if (m) { const nav = document.getElementById("mainNav"); const open = !nav.classList.contains("open"); nav.classList.toggle("open", open); m.setAttribute("aria-expanded", open); }
      else if (!e.target.closest("#mainNav")) document.getElementById("mainNav")?.classList.remove("open");
      const ab = e.target.closest("#acctBtn"), menu = document.getElementById("acctMenu");
      if (ab && menu) { menu.hidden = !menu.hidden; ab.setAttribute("aria-expanded", String(!menu.hidden)); }
      else if (menu && !e.target.closest("#acctMenu")) { menu.hidden = true; document.getElementById("acctBtn")?.setAttribute("aria-expanded", "false"); }
      if (e.target.closest("[data-theme-toggle]")) { applyTheme(root.dataset.theme === "dark" ? "light" : "dark"); paintThemeLabel(); }
      if (e.target.closest("[data-logout]")) Account.logout();
    });
    document.addEventListener("keydown", (e) => { if (e.key === "Escape") { const menu = document.getElementById("acctMenu"); if (menu) menu.hidden = true; } });
    Account.init();
  }
  function paintThemeLabel() { document.querySelectorAll("[data-theme-toggle] span").forEach(s => s.textContent = root.dataset.theme === "dark" ? "Light mode" : "Dark mode"); }

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

  // ------------------------------------------------------------ accounts + membership
  // Supabase Auth (Google or an emailed link) plus a `profiles` row kept current by the Stripe webhook (see billing/README.md).
  // One plan: the monthly membership. profiles.is_owner opens everything without a subscription.
  const Account = {
    client: null, user: null, profile: null, _done: null,
    ready: null,   // set right below the object so pages can await it before DOMContentLoaded
    enabled: authOn,
    getClient() {
      if (!this.client && window.supabase && cfg.SUPABASE_URL) this.client = window.supabase.createClient(cfg.SUPABASE_URL, cfg.SUPABASE_ANON_KEY);
      return this.client;
    },
    // pages that don't ship supabase-js themselves get it loaded here
    loadLib() {
      if (window.supabase) return Promise.resolve(true);
      return new Promise(res => { const sc = document.createElement("script"); sc.src = "https://cdn.jsdelivr.net/npm/@supabase/supabase-js@2/dist/umd/supabase.min.js";
        sc.onload = () => res(true); sc.onerror = () => res(false); document.head.appendChild(sc); });
    },
    loginUrl(next) { const n = next || (MEMBER_PAGES.has(here) ? here : ""); return "signup.html?mode=signin" + (n ? "&next=" + encodeURIComponent(n) : ""); },
    payUrl() { return "signup.html?step=pay"; },
    // Membership is required only when AUTH_ENABLED && REQUIRE_SUBSCRIPTION; otherwise every signed-in visitor counts as active.
    gating: () => authOn() && !!cfg.REQUIRE_SUBSCRIPTION,
    owner() { return !!this.profile?.is_owner; },
    subscribed() { const s = this.profile?.subscription_status; return s === "active" || s === "trialing"; },
    active() { return !this.gating() || this.owner() || this.subscribed(); },
    // kept for the builders: "member" when active, "none" otherwise; no lineup cap on the single plan
    plan() { return this.active() ? "member" : "none"; },
    lineupCap() { return Infinity; },
    async loadProfile() {
      const c = this.getClient(); if (!c || !this.user) return null;
      try { const { data: p } = await c.from("profiles").select("*").eq("id", this.user.id).maybeSingle(); this.profile = p || null; } catch (e) { /* table missing */ }
      return this.profile;
    },
    async token() { const { data } = await this.getClient().auth.getSession(); if (!data?.session) throw new Error("Log in first."); return data.session.access_token; },
    async callBilling(fn, body) {
      let res;
      try { res = await fetch(`/.netlify/functions/${fn}`, { method: "POST", headers: { Authorization: "Bearer " + await this.token(), "Content-Type": "application/json" }, body: JSON.stringify(body || {}) }); }
      catch (e) { throw new Error("Couldn't reach the payment service. Check your connection and try again."); }
      const j = await res.json().catch(() => ({}));
      if (res.status === 404) throw new Error("Payments aren't switched on yet. Nothing was charged.");
      if (!res.ok || !j.url) throw new Error(j.error || "Payments aren't switched on yet. Nothing was charged.");
      location.href = j.url;
    },
    checkout() { return this.callBilling("create-checkout", { plan: "member", interval: "monthly" }); },
    portal() { return this.callBilling("billing-portal"); },
    async logout() { try { await this.getClient()?.auth.signOut(); } catch (e) { /* already signed out */ } store.set("gs_owner", null); location.href = "index.html"; },
    paintSlot() {
      const slot = document.getElementById("accountSlot"); if (!slot || !this.user) return;
      const email = this.user.email || "", initial = (email[0] || "?");
      const safe = email.replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
      if (!SIGNED_IN_PAGES.has(here)) { slot.innerHTML = this.active() ? `<a class="btn btn-primary btn-sm" href="home.html">Open GameTime Win</a>` : `<button class="btn btn-sm" type="button" data-logout>Log out</button>`; return; }
      slot.innerHTML = `<button class="acct-btn" id="acctBtn" aria-haspopup="true" aria-expanded="false" aria-label="Account menu"><span class="av">${initial}</span><span class="nm">Account</span>${icon("chevron")}</button>
        <div class="acct-menu" id="acctMenu" hidden>
          <div class="who" title="${safe}">${safe}</div>
          <a href="account.html">${icon("user")}Account and billing</a>
          ${this.owner() ? `<a href="status.html">${icon("clock")}System status</a>` : ""}
          <button type="button" data-theme-toggle>${icon(root.dataset.theme === "dark" ? "sun" : "moon")}<span></span></button>
          <button type="button" data-logout>${icon("logout")}Log out</button>
        </div>`;
      paintThemeLabel();
    },
    async init() {
      if (!this.enabled()) { this._done(); return; }
      (async () => {
        await this.loadLib();
        const c = this.getClient();
        if (!c) { if (gated()) location.replace(this.loginUrl()); return; }      // fail closed: no library, no tool
        const { data } = await c.auth.getSession();
        this.user = data?.session?.user || null;
        if (gated() && !this.user) { location.replace(this.loginUrl()); return; }
        if (this.user) await this.loadProfile();
        store.set("gs_owner", this.owner() ? "1" : null); root.classList.toggle("owner", this.owner());
        if (gated()) {
          if (OWNER_PAGES.has(here) && !this.owner()) { location.replace("home.html"); return; }
          if (MEMBER_PAGES.has(here) && !this.active()) {
            // just back from Stripe: the webhook can take a few seconds to mark the membership active
            if (new URLSearchParams(location.search).get("welcome") === "1") {
              for (let i = 0; i < 10 && !this.active(); i++) { await new Promise(r => setTimeout(r, 2000)); await this.loadProfile(); }
            }
            if (!this.active()) { location.replace(this.payUrl()); return; }
          }
          root.classList.remove("gate-pending");
        }
        this.paintSlot();
        // reload only on a real sign-in / sign-out after the page settled (not the initial session event)
        c.auth.onAuthStateChange((event, session) => {
          if (event === "INITIAL_SESSION" || event === "TOKEN_REFRESHED") return;
          if ((session?.user?.id || null) !== (this.user?.id || null)) location.reload();
        });
      })().catch(e => { console.warn("account init", e); if (gated()) location.replace(this.loginUrl()); }).finally(() => this._done());
    },
  };

  Account.ready = new Promise(r => { Account._done = r; });
  window.GS = { icon, toast, isOwner, store, Account, cfg, MEMBER_PAGES, BRAND, LEGAL, FOOT_LINKS };
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", mount); else mount();
})();
