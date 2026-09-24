// POST /.netlify/functions/create-checkout  (Authorization: Bearer <supabase access token>)
// → { url } of a Stripe Checkout page for the monthly plan.
import { env, handle, json, stripe, currentUser, profiles, HttpError } from "./utils/lib.mjs";

export default handle(async (req) => {
  if (req.method !== "POST") throw new HttpError(405, "POST only");
  const user = await currentUser(req);
  const [row] = await profiles("GET", `id=eq.${user.id}&select=stripe_customer_id,subscription_status`);
  if (row && ["active", "trialing"].includes(row.subscription_status)) throw new HttpError(409, "You already have an active plan.");

  let customer = row?.stripe_customer_id;
  if (!customer) {
    customer = (await stripe("customers", { email: user.email, metadata: { supabase_user_id: user.id } })).id;
    await profiles("POST", "on_conflict=id", [{ id: user.id, email: user.email, stripe_customer_id: customer }]);
  }
  const site = env("SITE_URL").replace(/\/$/, "");
  const session = await stripe("checkout/sessions", {
    mode: "subscription",
    customer,
    client_reference_id: user.id,
    line_items: { 0: { price: env("STRIPE_PRICE_ID"), quantity: 1 } },
    allow_promotion_codes: "true",
    subscription_data: { metadata: { supabase_user_id: user.id } },
    success_url: `${site}/account.html?checkout=success`,
    cancel_url: `${site}/account.html?checkout=cancel`,
  });
  return json(200, { url: session.url });
});
