// POST /.netlify/functions/create-checkout  (Authorization: Bearer <supabase access token>)
// Body (JSON): { plan: "starter" | "pro" | "slate", interval: "monthly" | "season" }   (interval ignored for slate)
// → { url } of a Stripe Checkout page.
//   starter / pro  → mode "subscription", 7-day free trial, price from STRIPE_PRICE_<PLAN>_<INTERVAL>
//   slate          → mode "payment" (one-time $5 Slate Pass), price from STRIPE_PRICE_SLATE
// Legacy: a body without `plan` (or no body) behaves like { plan: "starter", interval: "monthly" }; if STRIPE_PRICE_ID is
// still the only price configured, it is used for that case.
import { env, handle, json, stripe, currentUser, profiles, HttpError } from "./utils/lib.mjs";
import { priceEnvName, checkoutMode, TRIAL_DAYS } from "./utils/plans.mjs";

export async function parsePlan(req) {
  let body = {};
  try { const t = await req.text(); if (t) body = JSON.parse(t); } catch (e) { throw new HttpError(400, "Body must be JSON: { plan, interval }"); }
  const plan = String(body.plan || "starter").toLowerCase();
  const interval = plan === "slate" ? "once" : String(body.interval || "monthly").toLowerCase();
  let envName;
  try { envName = priceEnvName(plan, interval); } catch (e) { throw new HttpError(400, e.message); }
  return { plan, interval, envName };
}

export default handle(async (req) => {
  if (req.method !== "POST") throw new HttpError(405, "POST only");
  const user = await currentUser(req);
  const { plan, interval, envName } = await parsePlan(req);
  const mode = checkoutMode(plan);
  const price = process.env[envName] || (plan === "starter" && interval === "monthly" ? process.env.STRIPE_PRICE_ID : null);
  if (!price) throw new HttpError(500, `Billing is not configured (missing ${envName}).`);

  const [row] = await profiles("GET", `id=eq.${user.id}&select=stripe_customer_id,subscription_status,plan`);
  const subscribed = row && ["active", "trialing"].includes(row.subscription_status);
  if (subscribed) throw new HttpError(409, `You already have an active ${row.plan || ""} plan. Change plans from Manage billing on your account page.`);

  let customer = row?.stripe_customer_id;
  if (!customer) {
    customer = (await stripe("customers", { email: user.email, metadata: { supabase_user_id: user.id } })).id;
    await profiles("POST", "on_conflict=id", [{ id: user.id, email: user.email, stripe_customer_id: customer }]);
  }
  const site = env("SITE_URL").replace(/\/$/, "");
  const params = {
    mode,
    customer,
    client_reference_id: user.id,
    line_items: { 0: { price, quantity: 1 } },
    allow_promotion_codes: "true",
    metadata: { supabase_user_id: user.id, plan, interval },
    success_url: `${site}/account.html?checkout=success`,
    cancel_url: `${site}/account.html?checkout=cancel`,
  };
  if (mode === "subscription") {
    params.subscription_data = { trial_period_days: TRIAL_DAYS, metadata: { supabase_user_id: user.id, plan, interval } };
    params.payment_method_collection = "always";   // card up front; charged on day 8 unless they cancel
  } else {
    params.payment_intent_data = { metadata: { supabase_user_id: user.id, plan, interval } };
  }
  const session = await stripe("checkout/sessions", params);
  return json(200, { url: session.url });
});
