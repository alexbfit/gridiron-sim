// POST /.netlify/functions/stripe-webhook  — Stripe → keeps profiles.subscription_* current.
// Stripe dashboard → Developers → Webhooks → add endpoint https://<site>/.netlify/functions/stripe-webhook
// with events: checkout.session.completed, customer.subscription.created, customer.subscription.updated, customer.subscription.deleted
import crypto from "node:crypto";
import { env, handle, json, stripe, profiles, HttpError } from "./utils/lib.mjs";

function verify(payload, header, secret) {
  const parts = Object.fromEntries((header || "").split(",").map(kv => kv.split("=")));
  const t = parts.t, sigs = (header || "").split(",").filter(p => p.startsWith("v1=")).map(p => p.slice(3));
  if (!t || !sigs.length) throw new HttpError(400, "Missing signature");
  if (Math.abs(Date.now() / 1000 - Number(t)) > 300) throw new HttpError(400, "Stale signature");
  const expected = crypto.createHmac("sha256", secret).update(`${t}.${payload}`).digest("hex");
  const exp = Buffer.from(expected);
  const ok = sigs.some(s => { const got = Buffer.from(s); return got.length === exp.length && crypto.timingSafeEqual(got, exp); });
  if (!ok) throw new HttpError(400, "Bad signature");
}

async function syncSubscription(sub) {
  const userId = sub.metadata?.supabase_user_id;
  const patch = {
    stripe_customer_id: sub.customer,
    subscription_id: sub.id,
    subscription_status: sub.status,
    price_id: sub.items?.data?.[0]?.price?.id || null,
    current_period_end: (sub.current_period_end || sub.items?.data?.[0]?.current_period_end)
      ? new Date(1000 * (sub.current_period_end || sub.items.data[0].current_period_end)).toISOString() : null,
    cancel_at_period_end: !!sub.cancel_at_period_end,
    updated_at: new Date().toISOString(),
  };
  if (userId) await profiles("POST", "on_conflict=id", [{ id: userId, ...patch }]);
  else await profiles("PATCH", `stripe_customer_id=eq.${encodeURIComponent(sub.customer)}`, patch);
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
