// Shared helpers for the billing functions. No npm dependencies: Stripe and Supabase are called over HTTPS.
// Environment variables (Netlify → Site configuration → Environment variables):
//   STRIPE_SECRET_KEY, STRIPE_PRICE_ID, STRIPE_WEBHOOK_SECRET,
//   SUPABASE_URL, SUPABASE_ANON_KEY (public key), SUPABASE_SERVICE_ROLE_KEY, SITE_URL (e.g. https://gridiron-sim.netlify.app)

export const env = (k) => {
  const v = process.env[k];
  if (!v) throw new HttpError(500, `Billing is not configured (missing ${k}).`);
  return v;
};

export class HttpError extends Error { constructor(status, message) { super(message); this.status = status; } }

export const json = (status, body) => new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });

export function handle(fn) {
  return async (req) => {
    try { return await fn(req); }
    catch (e) { return json(e.status || 500, { error: e.message || "Server error" }); }
  };
}

// Stripe REST call with form-encoded body (nested keys like line_items[0][price])
export async function stripe(path, params = {}, method = "POST") {
  const body = new URLSearchParams();
  const add = (k, v) => {
    if (v == null) return;
    if (typeof v === "object") Object.entries(v).forEach(([kk, vv]) => add(`${k}[${kk}]`, vv));
    else body.append(k, String(v));
  };
  Object.entries(params).forEach(([k, v]) => add(k, v));
  const res = await fetch(`https://api.stripe.com/v1/${path}${method === "GET" && body.toString() ? "?" + body : ""}`, {
    method,
    headers: { Authorization: `Bearer ${env("STRIPE_SECRET_KEY")}`, "Content-Type": "application/x-www-form-urlencoded" },
    body: method === "GET" ? undefined : body,
  });
  const j = await res.json();
  if (!res.ok) throw new HttpError(502, j?.error?.message || "Stripe error");
  return j;
}

// The signed-in Supabase user behind the request's bearer token
export async function currentUser(req) {
  const token = (req.headers.get("authorization") || "").replace(/^Bearer\s+/i, "");
  if (!token) throw new HttpError(401, "Sign in first.");
  const res = await fetch(`${env("SUPABASE_URL")}/auth/v1/user`, { headers: { apikey: env("SUPABASE_ANON_KEY"), Authorization: `Bearer ${token}` } });
  if (!res.ok) throw new HttpError(401, "Your session expired — sign in again.");
  return res.json();
}

// profiles table via PostgREST with the service-role key (bypasses RLS; server-side only)
export async function profiles(method, query, body) {
  const res = await fetch(`${env("SUPABASE_URL")}/rest/v1/profiles${query ? "?" + query : ""}`, {
    method,
    headers: {
      apikey: env("SUPABASE_SERVICE_ROLE_KEY"), Authorization: `Bearer ${env("SUPABASE_SERVICE_ROLE_KEY")}`,
      "Content-Type": "application/json", Prefer: "return=representation,resolution=merge-duplicates",
    },
    body: body ? JSON.stringify(body) : undefined,
  });
  const j = await res.json().catch(() => null);
  if (!res.ok) throw new HttpError(500, j?.message || "Database error");
  return j;
}
