// POST /.netlify/functions/billing-portal  (Authorization: Bearer <supabase access token>)
// → { url } of the Stripe customer portal (update card, cancel, invoices).
import { env, handle, json, stripe, currentUser, profiles, HttpError } from "./utils/lib.mjs";

export default handle(async (req) => {
  if (req.method !== "POST") throw new HttpError(405, "POST only");
  const user = await currentUser(req);
  const [row] = await profiles("GET", `id=eq.${user.id}&select=stripe_customer_id`);
  if (!row?.stripe_customer_id) throw new HttpError(404, "No billing account yet.");
  const portal = await stripe("billing_portal/sessions", {
    customer: row.stripe_customer_id,
    return_url: `${env("SITE_URL").replace(/\/$/, "")}/account.html`,
  });
  return json(200, { url: portal.url });
});
