// POST /.netlify/functions/stripe-webhook  — Stripe → keeps profiles.subscription_* / plan / slate_pass_until current.
// Stripe dashboard → Developers → Webhooks → add endpoint https://<site>/.netlify/functions/stripe-webhook
// with events: checkout.session.completed, customer.subscription.created, customer.subscription.updated, customer.subscription.deleted
//
// What it writes:
//   subscriptions (Starter / Pro, monthly or season)  → subscription_status, price_id, plan ("starter" | "pro"), current_period_end,
//                                                       cancel_at_period_end. Season-pass prices get cancel_at_period_end = true so they
//                                                       cover the season and never renew.
//   one-time Slate Pass (checkout mode "payment")      → slate_pass_until = now + 24h (never shortens an existing pass).
import crypto from "node:crypto";
import { env, handle, json, stripe, profiles, HttpError } from "./utils/lib.mjs";
import { planForPrice, slatePassEnd } from "./utils/plans.mjs";

export function verify(payload, header, secret, now = Date.now()) {
  const parts = Object.fromEntries((header || "").split(",").map(kv => kv.split("=")));
  const t = parts.t, sigs = (header || "").split(",").filter(p => p.startsWith("v1=")).map(p => p.slice(3));
  if (!t || !sigs.length) throw new HttpError(400, "Missing signature");
  if (Math.abs(now / 1000 - Number(t)) > 300) throw new HttpError(400, "Stale signature");
  const expected = crypto.createHmac("sha256", secret).update(`${t}.${payload}`).digest("hex");
  const exp = Buffer.from(expected);
  const ok = sigs.some(s => { const got = Buffer.from(s); return got.length === exp.length && crypto.timingSafeEqual(got, exp); });
  if (!ok) throw new HttpError(400, "Bad signature");
}

// Build the profiles patch for a Stripe subscription object (pure; tested).
export function subscriptionPatch(sub, environment = process.env, now = new Date()) {
  const priceId = sub.items?.data?.[0]?.price?.id || null;
  const mapped = planForPrice(priceId, environment);
  const plan = mapped?.plan || sub.metadata?.plan || null;
  const periodEnd = sub.current_period_end || sub.items?.data?.[0]?.current_period_end;
  return {
    stripe_customer_id: sub.customer,
    subscription_id: sub.id,
    subscription_status: sub.status,
    price_id: priceId,
    plan: ["canceled", "unpaid", "incomplete_expired"].includes(sub.status) ? null : plan,
    current_period_end: periodEnd ? new Date(1000 * periodEnd).toISOString() : null,
    cancel_at_period_end: !!sub.cancel_at_period_end,
    updated_at: now.toISOString(),
  };
}

export const isSeasonPrice = (priceId, environment = process.env) => planForPrice(priceId, environment)?.interval === "season";

async function syncSubscription(sub) {
  // Season passes are yearly Stripe subscriptions that must not renew: flip cancel_at_period_end once, right after creation.
  const priceId = sub.items?.data?.[0]?.price?.id;
  if (isSeasonPrice(priceId) && !sub.cancel_at_period_end && ["active", "trialing"].includes(sub.status)) {
    try { sub = await stripe(`subscriptions/${sub.id}`, { cancel_at_period_end: "true" }); } catch (e) { /* keep going with what we have */ }
  }
  const userId = sub.metadata?.supabase_user_id;
  const patch = subscriptionPatch(sub);
  if (userId) await profiles("POST", "on_conflict=id", [{ id: userId, ...patch }]);
  else await profiles("PATCH", `stripe_customer_id=eq.${encodeURIComponent(sub.customer)}`, patch);
}

// One-time purchase (Slate Pass): extend slate_pass_until to now + 24h, or 24h past the current pass if it hasn't ended yet.
export function slatePassPatch(existingUntil, now = new Date()) {
  const base = existingUntil && new Date(existingUntil).getTime() > now.getTime() ? new Date(existingUntil) : now;
  return { slate_pass_until: slatePassEnd(base).toISOString(), updated_at: now.toISOString() };
}

async function grantSlatePass(session) {
  const userId = session.client_reference_id || session.metadata?.supabase_user_id;
  const customer = session.customer;
  const q = userId ? `id=eq.${userId}` : `stripe_customer_id=eq.${encodeURIComponent(customer)}`;
  const [row] = await profiles("GET", `${q}&select=id,slate_pass_until`);
  const patch = slatePassPatch(row?.slate_pass_until);
  if (userId) await profiles("POST", "on_conflict=id", [{ id: userId, stripe_customer_id: customer || undefined, ...patch }]);
  else if (customer) await profiles("PATCH", q, patch);
}

export default handle(async (req) => {
  if (req.method !== "POST") throw new HttpError(405, "POST only");
  const payload = await req.text();
  verify(payload, req.headers.get("stripe-signature"), env("STRIPE_WEBHOOK_SECRET"));
  const event = JSON.parse(payload);
  const obj = event.data?.object || {};
  switch (event.type) {
    case "checkout.session.completed":
      if (obj.mode === "subscription" && obj.subscription) await syncSubscription(await stripe(`subscriptions/${obj.subscription}`, {}, "GET"));
      else if (obj.mode === "payment" && obj.payment_status === "paid" && (obj.metadata?.plan === "slate" || !obj.metadata?.plan)) await grantSlatePass(obj);
      break;
    case "customer.subscription.created":
    case "customer.subscription.updated":
    case "customer.subscription.deleted":
      // Stripe doesn't guarantee event order — always sync from the subscription's current state, not the event copy
      await syncSubscription(await stripe(`subscriptions/${obj.id}`, {}, "GET"));
      break;
    default: break;
  }
  return json(200, { received: true });
});
