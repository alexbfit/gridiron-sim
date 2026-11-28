// Checkout and webhook, end to end against a faked Stripe + Supabase (global fetch is replaced; nothing leaves the machine).
import test from "node:test";
import assert from "node:assert/strict";
import crypto from "node:crypto";

const ENV = { STRIPE_SECRET_KEY: "sk_test_x", STRIPE_WEBHOOK_SECRET: "whsec_x", STRIPE_PRICE_MEMBER_MONTHLY: "price_member",
  SUPABASE_URL: "https://db.example", SUPABASE_ANON_KEY: "anon", SUPABASE_SERVICE_ROLE_KEY: "service", SITE_URL: "https://gametimewin.com/" };
Object.assign(process.env, ENV);

function fakeWorld({ profile = null, tokenOk = true } = {}) {
  const calls = [], rows = profile ? [profile] : [];
  globalThis.fetch = async (url, opt = {}) => {
    const u = String(url), body = opt.body ? String(opt.body) : "";
    calls.push({ u, method: opt.method || "GET", body });
    const ok = (j, status = 200) => new Response(JSON.stringify(j), { status });
    if (u.endsWith("/auth/v1/user")) return tokenOk ? ok({ id: "user-1", email: "a@example.com" }) : ok({}, 401);
    if (u.includes("/rest/v1/profiles")) {
      if ((opt.method || "GET") === "GET") return ok(rows);
      const b = JSON.parse(body), patch = Array.isArray(b) ? b[0] : b;
      if (rows[0]) Object.assign(rows[0], patch); else rows.push({ ...patch });
      return ok(rows);
    }
    if (u.endsWith("/v1/customers")) return ok({ id: "cus_1" });
    if (u.endsWith("/v1/checkout/sessions")) return ok({ url: "https://checkout.stripe.com/c/pay/cs_test_1" });
    if (u.includes("/v1/subscriptions/sub_1")) return ok({ id: "sub_1", customer: "cus_1", status: "active", cancel_at_period_end: false,
      current_period_end: 1798761600, metadata: { supabase_user_id: "user-1", plan: "member" }, items: { data: [{ price: { id: "price_member" } }] } });
    return ok({ error: { message: "unexpected " + u } }, 500);
  };
  return { calls, rows };
}
const post = (body, headers = { authorization: "Bearer tok" }) => new Request("https://gametimewin.com/.netlify/functions/x", { method: "POST", headers, body: body == null ? undefined : JSON.stringify(body) });

test("checkout: a new member gets a Stripe subscription session for the $99 price, with no trial", async () => {
  const w = fakeWorld();
  const { default: checkout } = await import("../functions/create-checkout.mjs");
  const res = await checkout(post({ plan: "member", interval: "monthly" }));
  assert.equal(res.status, 200);
  assert.equal((await res.json()).url, "https://checkout.stripe.com/c/pay/cs_test_1");
  const s = new URLSearchParams(w.calls.find(c => c.u.endsWith("/checkout/sessions")).body);
  assert.equal(s.get("mode"), "subscription");
  assert.equal(s.get("line_items[0][price]"), "price_member");
  assert.equal(s.get("customer"), "cus_1");
  assert.equal(s.get("success_url"), "https://gametimewin.com/home.html?welcome=1");
  assert.equal(s.get("cancel_url"), "https://gametimewin.com/signup.html?step=pay&checkout=cancel");
  assert.equal(s.get("subscription_data[metadata][supabase_user_id]"), "user-1");
  assert.ok(![...s.keys()].some(k => k.includes("trial")), "no trial parameters");
  assert.equal(w.rows[0].stripe_customer_id, "cus_1");
});

test("checkout: refuses without a log-in, for an unknown plan, and for someone already paying", async () => {
  const { default: checkout } = await import("../functions/create-checkout.mjs");
  fakeWorld({ tokenOk: false });
  assert.equal((await checkout(post({}))).status, 401);
  fakeWorld();
  assert.equal((await checkout(post({}, {}))).status, 401);
  assert.equal((await checkout(post({ plan: "pro" }))).status, 400);
  assert.equal((await checkout(new Request("https://x/y", { method: "GET" }))).status, 405);
  const w = fakeWorld({ profile: { id: "user-1", stripe_customer_id: "cus_1", subscription_status: "active" } });
  assert.equal((await checkout(post({}))).status, 409);
  assert.ok(!w.calls.some(c => c.u.includes("checkout/sessions")), "no second checkout session");
});

test("webhook: a signed subscription event marks the profile active; a bad signature changes nothing", async () => {
  const { default: hook } = await import("../functions/stripe-webhook.mjs");
  const payload = JSON.stringify({ type: "customer.subscription.created", data: { object: { id: "sub_1" } } });
  const sign = (secret) => { const t = Math.floor(Date.now() / 1000); return `t=${t},v1=${crypto.createHmac("sha256", secret).update(`${t}.${payload}`).digest("hex")}`; };
  const req = (sig) => new Request("https://x/y", { method: "POST", headers: { "stripe-signature": sig }, body: payload });

  let w = fakeWorld({ profile: { id: "user-1", stripe_customer_id: "cus_1", subscription_status: null } });
  assert.equal((await hook(req(sign("whsec_wrong")))).status, 400);
  assert.equal(w.rows[0].subscription_status, null);

  w = fakeWorld({ profile: { id: "user-1", stripe_customer_id: "cus_1", subscription_status: null } });
  assert.equal((await hook(req(sign("whsec_x")))).status, 200);
  assert.equal(w.rows[0].subscription_status, "active");
  assert.equal(w.rows[0].plan, "member");
  assert.equal(w.rows[0].current_period_end, new Date(1798761600 * 1000).toISOString());
});
