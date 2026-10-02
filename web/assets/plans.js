/* GameTime Win — plan helpers shared by pricing.html, index.html (#pricing) and account.html.
   Reads PLANS / FREE_LINEUPS / BILLING_LIVE from config.js. Presentation + checkout wiring only; nothing here touches the sim.
   Load after config.js and shell.js. */
(function () {
  const cfg = window.GRIDIRON_CONFIG || {};
  const PLANS = cfg.PLANS || {};
  const NOTES = cfg.PRICING_NOTES || {};
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const icon = (n) => (window.GS && window.GS.icon) ? window.GS.icon(n) : "";

  const PAID = ["starter", "pro"];
  const order = ["free", "starter", "pro"].filter(k => PLANS[k]);

  function priceLine(plan, interval) {
    const p = plan.price || {};
    if (plan.key === "slate") return { big: p.once || "", small: "one slate · 24 hours" };
    if (plan.key === "free") return { big: p.monthly || "$0", small: "forever" };
    return interval === "season" ? { big: p.season || "", small: "NFL season pass" } : { big: p.monthly || "", small: "/ month" };
  }

  // What the CTA should do for a plan right now.
  //  - billing off → everyone joins the free beta (index.html#join)
  //  - billing on, free plan → account.html (sign in) or simple.html
  //  - billing on, paid plan → checkout (handled by the page via data-checkout attributes)
  function cta(plan, interval) {
    if (!cfg.BILLING_LIVE) return cfg.AUTH_ENABLED ? { label: "Create free account", href: "signup.html" } : { label: "Join the free beta", href: "index.html" };
    if (plan.key === "free") return { label: "Start free", href: cfg.AUTH_ENABLED ? "account.html" : "simple.html" };
    const trial = plan.trialDays && plan.key !== "slate";
    return { label: plan.key === "slate" ? "Buy a Slate Pass" : (trial ? `Start ${plan.trialDays}-day free trial` : `Choose ${plan.name}`), checkout: { plan: plan.key, interval: plan.key === "slate" ? "once" : interval } };
  }

  function ctaHTML(plan, interval, cls = "btn btn-block") {
    const c = cta(plan, interval);
    const primary = plan.featured || plan.key === "slate" ? " btn-primary" : "";
    if (c.href) return `<a class="${cls}${primary}" href="${esc(c.href)}">${esc(c.label)}</a>`;
    return `<button type="button" class="${cls}${primary}" data-checkout-plan="${esc(c.checkout.plan)}" data-checkout-interval="${esc(c.checkout.interval)}">${esc(c.label)}</button>`;
  }

  function card(plan, interval, opts = {}) {
    const pr = priceLine(plan, interval);
    const founding = cfg.FOUNDING && PAID.includes(plan.key) ? `<span class="badge gold plan-founding" title="${esc(NOTES.founding || "")}">${icon("star")} Founding price</span>` : "";
    const feats = (plan.features || []).map(f => `<li>${icon("check")}<span>${esc(f)}</span></li>`).join("");
    const trial = plan.trialDays && cfg.BILLING_LIVE ? `<p class="plan-fine">${plan.trialDays}-day free trial · cancel any time · no refunds</p>` : (plan.key === "slate" ? `<p class="plan-fine">One-time payment · no refunds</p>` : "");
    return `<article class="card plan${plan.featured ? " featured" : ""}${plan.key === "slate" ? " plan-slate" : ""}" data-plan="${esc(plan.key)}">
      ${plan.featured ? `<span class="tag badge ok">Most popular</span>` : ""}
      <h3>${esc(plan.name)}</h3>
      <p class="desc">${esc(plan.tagline || "")}</p>
      <div class="price">${esc(pr.big)} <small>${esc(pr.small)}</small></div>
      ${founding}
      ${opts.compact ? "" : `<ul class="checklist">${feats}</ul>`}
      ${ctaHTML(plan, interval)}
      ${opts.compact ? "" : trial}
    </article>`;
  }

  function cards(interval, opts = {}) {
    return order.map(k => card(PLANS[k], interval, opts)).join("");
  }

  // Wire every [data-checkout-plan] button on the page to the create-checkout function.
  // Signed-out visitors go to account.html first; the intended plan is remembered so account.html can continue.
  function wireCheckout(root = document) {
    root.querySelectorAll("[data-checkout-plan]").forEach(btn => {
      if (btn.dataset.wired) return; btn.dataset.wired = "1";
      btn.addEventListener("click", async () => {
        const plan = btn.dataset.checkoutPlan, interval = btn.dataset.checkoutInterval;
        try { localStorage.setItem("gs_intent", JSON.stringify({ plan, interval, at: Date.now() })); } catch (e) { /* private mode */ }
        const Account = window.GS && window.GS.Account;
        if (!cfg.AUTH_ENABLED || !Account) { location.href = "account.html"; return; }
        await Account.ready;
        if (!Account.user) { location.href = `account.html?plan=${encodeURIComponent(plan)}&interval=${encodeURIComponent(interval)}`; return; }
        btn.disabled = true;
        try { await checkout(plan, interval); }
        catch (e) { window.GS.toast(e.message || "Checkout isn't available right now.", "error"); btn.disabled = false; }
      });
    });
  }

  async function checkout(plan, interval) {
    const Account = window.GS.Account, c = Account.getClient();
    const { data } = await c.auth.getSession();
    if (!data?.session) throw new Error("Sign in first.");
    const res = await fetch("/.netlify/functions/create-checkout", {
      method: "POST",
      headers: { Authorization: "Bearer " + data.session.access_token, "Content-Type": "application/json" },
      body: JSON.stringify({ plan, interval }),
    });
    const j = await res.json().catch(() => ({}));
    if (!res.ok || !j.url) throw new Error(j.error || "Billing is not set up yet.");
    try { localStorage.removeItem("gs_intent"); } catch (e) { /* ignore */ }
    location.href = j.url;
  }

  function intent() {
    const q = new URLSearchParams(location.search);
    if (q.get("plan")) return { plan: q.get("plan"), interval: q.get("interval") || "monthly" };
    try { const j = JSON.parse(localStorage.getItem("gs_intent") || "null"); if (j && Date.now() - j.at < 36e5) return j; } catch (e) { /* ignore */ }
    return null;
  }

  window.GTW_PLANS = { PLANS, NOTES, order, priceLine, cta, ctaHTML, card, cards, wireCheckout, checkout, intent, esc };
})();
