// POST /.netlify/functions/create-checkout  (Authorization: Bearer <supabase access token>)
// → { url } of a Stripe Checkout page for the monthly membership (price from STRIPE_PRICE_MEMBER_MONTHLY). No trial:
//   the first month is charged at checkout and the subscription renews monthly until cancelled.
// Body (JSON, optional): { plan: "member", interval: "monthly" }. Anything else is rejected.
import { env, handle, json, stripe, currentUser, profiles, HttpError } from "./utils/lib.mjs";
import { priceEnvName, memberPrice } from "./utils/plans.mjs";

export async function parsePlan(req) {
  let body = {};
  try { const t = await req.text(); if (t) body = JSON.parse(t); } catch (e) { throw new HttpError(400, "Body must be JSON."); }
  const plan = String(body.plan || "member").toLowerCase(), interval = String(body.interval || "monthly").toLowerCase();
  try { priceEnvName(plan, interval); } catch (e) { throw new HttpError(400, e.message); }
  return { plan, interval };
}

export default handle(async (req) => {
  if (req.method !== "POST") throw new HttpError(405, "POST only");
  const user = await currentUser(req);
  const { plan, interval } = await parsePlan(req);
  const price = memberPrice();
  if (!price) throw new HttpError(500, "Payments aren't switched on yet (missing STRIPE_PRICE_MEMBER_MONTHLY). Nothing was charged.");

  const [row] = await profiles("GET", `id=eq.${user.id}&select=stripe_customer_id,subscription_status`);
  if (row && ["active", "trialing"].includes(row.subscription_status)) throw new HttpError(409, "You already have an active membership. Manage it from your account page.");

  let customer = row?.stripe_customer_id;
  if (!customer) {
    customer = (await stripe("customers", { email: user.email, metadata: { supabase_user_id: user.id } })).id;
    await profiles("POST", "on_conflict=id", [{ id: user.id, email: user.email, stripe_customer_id: customer }]);
  }
  const site = env("SITE_URL").replace(/\/$/, "");
  const meta = { supabase_user_id: user.id, plan, interval };
  const session = await stripe("checkout/sessions", {
    mode: "subscription",
    customer,
    client_reference_id: user.id,
    line_items: { 0: { price, quantity: 1 } },
    allow_promotion_codes: "true",
    metadata: meta,
    subscription_data: { metadata: meta },
    success_url: `${site}/home.html?welcome=1`,
    cancel_url: `${site}/signup.html?step=pay&checkout=cancel`,
  });
  return json(200, { url: session.url });
});
