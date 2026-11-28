// Plan ↔ Stripe price mapping for the billing functions. Pure functions, no I/O, unit-tested in plans.test.mjs.
//
// There is one plan: the monthly membership ($99 a month, no trial).
//   STRIPE_PRICE_MEMBER_MONTHLY   the Stripe Price id (recurring, monthly)
// Legacy: STRIPE_PRICE_ID is honoured as a fallback for the same price.
// Optional: PLAN_BY_PRICE = JSON {"price_abc":"member"} to map extra prices (e.g. an old price kept for existing members).

export const PLANS = ["member"];
export const INTERVALS = ["monthly"];
export const TRIAL_DAYS = 0;

// The env var name that holds the Stripe price id for a plan + interval. Throws on anything unexpected.
export function priceEnvName(plan = "member", interval = "monthly") {
  if (!PLANS.includes(plan)) throw new Error(`Unknown plan "${plan}". The only plan is member.`);
  if (!INTERVALS.includes(interval)) throw new Error(`Unknown interval "${interval}". The membership is monthly.`);
  return `STRIPE_PRICE_${plan.toUpperCase()}_${interval.toUpperCase()}`;
}

// The configured price id for the membership, or null when billing has not been set up.
export function memberPrice(env = process.env) {
  return env.STRIPE_PRICE_MEMBER_MONTHLY || env.STRIPE_PRICE_ID || null;
}

// price_id → { plan, interval }. Returns null when the price is not one of ours.
export function planForPrice(priceId, env = process.env) {
  if (!priceId) return null;
  if (priceId === memberPrice(env)) return { plan: "member", interval: "monthly" };
  if (env.PLAN_BY_PRICE) {
    try {
      const v = JSON.parse(env.PLAN_BY_PRICE)[priceId];
      if (typeof v === "string") return { plan: v, interval: "monthly" };
      if (v && typeof v === "object" && v.plan) return { plan: v.plan, interval: v.interval || "monthly" };
    } catch (e) { /* malformed override: ignore */ }
  }
  return null;
}
