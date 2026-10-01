// Plan ↔ Stripe price mapping for the billing functions. Pure functions, no I/O, unit-tested in plans.test.mjs.
//
// Environment variables (one Stripe Price each):
//   STRIPE_PRICE_STARTER_MONTHLY   recurring, monthly
//   STRIPE_PRICE_STARTER_SEASON    recurring, YEARLY — the "NFL season pass". The webhook sets cancel_at_period_end on it,
//                                  so it covers the season through the Super Bowl and never renews.
//   STRIPE_PRICE_PRO_MONTHLY       recurring, monthly
//   STRIPE_PRICE_PRO_SEASON        recurring, yearly (same treatment)
//   STRIPE_PRICE_SLATE             one-time ($5 Slate Pass — 24 hours of Starter; sets profiles.slate_pass_until)
// Optional: PLAN_BY_PRICE = JSON {"price_abc":"starter", ...} to override / extend the mapping (e.g. legacy prices).

export const PLANS = ["starter", "pro"];
export const INTERVALS = ["monthly", "season"];
export const SLATE_PASS_HOURS = 24;

// The env var name that holds the Stripe price id for a plan + interval. Throws on anything unexpected.
export function priceEnvName(plan, interval) {
  if (plan === "slate") return "STRIPE_PRICE_SLATE";
  if (!PLANS.includes(plan)) throw new Error(`Unknown plan "${plan}". Choose starter, pro or slate.`);
  if (!INTERVALS.includes(interval)) throw new Error(`Unknown interval "${interval}". Choose monthly or season.`);
  return `STRIPE_PRICE_${plan.toUpperCase()}_${interval.toUpperCase()}`;
}

// Checkout mode: Starter / Pro (monthly or season) are Stripe subscriptions with a 7-day trial; the slate pass is a one-time payment.
export function checkoutMode(plan) {
  return plan === "slate" ? "payment" : "subscription";
}
export const TRIAL_DAYS = 7;

// price_id → { plan, interval } using the env vars above (and PLAN_BY_PRICE if set). Returns null when unknown.
export function planForPrice(priceId, env = process.env) {
  if (!priceId) return null;
  for (const plan of [...PLANS, "slate"]) {
    const intervals = plan === "slate" ? ["once"] : INTERVALS;
    for (const interval of intervals) {
      const name = plan === "slate" ? "STRIPE_PRICE_SLATE" : priceEnvName(plan, interval);
      if (env[name] && env[name] === priceId) return { plan, interval };
    }
  }
  if (env.PLAN_BY_PRICE) {
    try {
      const m = JSON.parse(env.PLAN_BY_PRICE);
      const v = m[priceId];
      if (typeof v === "string") return { plan: v, interval: v === "slate" ? "once" : "monthly" };
      if (v && typeof v === "object" && v.plan) return { plan: v.plan, interval: v.interval || "monthly" };
    } catch (e) { /* malformed override: ignore */ }
  }
  return null;
}

export function slatePassEnd(now = new Date()) {
  return new Date(now.getTime() + SLATE_PASS_HOURS * 3600 * 1000);
}
