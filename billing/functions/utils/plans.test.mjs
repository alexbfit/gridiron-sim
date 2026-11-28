// Unit tests for the billing plan mapping + the webhook's pure helpers. Run: node --test "billing/**/*.test.mjs"  (also run by tests/test_cli.py)
import { test } from "node:test";
import assert from "node:assert/strict";
import { priceEnvName, memberPrice, planForPrice, TRIAL_DAYS } from "./plans.mjs";
import { subscriptionPatch, verify } from "../stripe-webhook.mjs";
import { parsePlan } from "../create-checkout.mjs";
import crypto from "node:crypto";

const ENV = { STRIPE_PRICE_MEMBER_MONTHLY: "price_member" };

test("one plan: member, monthly, no trial", () => {
  assert.equal(priceEnvName("member", "monthly"), "STRIPE_PRICE_MEMBER_MONTHLY");
  assert.equal(priceEnvName(), "STRIPE_PRICE_MEMBER_MONTHLY");
  assert.throws(() => priceEnvName("pro", "monthly"), /Unknown plan/);
  assert.throws(() => priceEnvName("member", "annual"), /Unknown interval/);
  assert.equal(TRIAL_DAYS, 0);
});

test("configured price: the member env var, the legacy STRIPE_PRICE_ID, or null", () => {
  assert.equal(memberPrice(ENV), "price_member");
  assert.equal(memberPrice({ STRIPE_PRICE_ID: "price_old" }), "price_old");
  assert.equal(memberPrice({}), null);
});

test("price id → plan via env var, PLAN_BY_PRICE override, unknown → null", () => {
  assert.deepEqual(planForPrice("price_member", ENV), { plan: "member", interval: "monthly" });
  assert.equal(planForPrice("price_other", ENV), null);
  assert.equal(planForPrice(null, ENV), null);
  const withOverride = { ...ENV, PLAN_BY_PRICE: JSON.stringify({ price_old: "member" }) };
  assert.deepEqual(planForPrice("price_old", withOverride), { plan: "member", interval: "monthly" });
  assert.equal(planForPrice("x", { ...ENV, PLAN_BY_PRICE: "{not json" }), null);
});

test("subscription → profiles patch carries plan, period end, cancel flag", () => {
  const now = new Date("2026-12-01T12:00:00Z");
  const sub = { id: "sub_1", customer: "cus_1", status: "active", cancel_at_period_end: false,
    items: { data: [{ price: { id: "price_member" }, current_period_end: 1760000000 }] }, metadata: { supabase_user_id: "u1", plan: "member" } };
  const p = subscriptionPatch(sub, ENV, now);
  assert.equal(p.plan, "member");
  assert.equal(p.price_id, "price_member");
  assert.equal(p.subscription_status, "active");
  assert.equal(p.current_period_end, new Date(1760000000 * 1000).toISOString());
  assert.equal(p.cancel_at_period_end, false);
  assert.equal(p.updated_at, now.toISOString());
  // unknown price falls back to the metadata plan; canceled clears the plan
  const unknown = subscriptionPatch({ ...sub, items: { data: [{ price: { id: "price_?" } }] } }, ENV, now);
  assert.equal(unknown.plan, "member");
  assert.equal(unknown.current_period_end, null);
  assert.equal(subscriptionPatch({ ...sub, status: "canceled" }, ENV, now).plan, null);
  assert.equal(subscriptionPatch({ ...sub, cancel_at_period_end: true }, ENV, now).cancel_at_period_end, true);
});

test("checkout body: empty or {plan:'member'} is fine, anything else is a 400", async () => {
  const req = (body) => new Request("https://x.test/", { method: "POST", body });
  assert.deepEqual(await parsePlan(req("")), { plan: "member", interval: "monthly" });
  assert.deepEqual(await parsePlan(req(JSON.stringify({ plan: "member", interval: "monthly" }))), { plan: "member", interval: "monthly" });
  await assert.rejects(() => parsePlan(req(JSON.stringify({ plan: "pro" }))), (e) => e.status === 400);
  await assert.rejects(() => parsePlan(req("{nope")), (e) => e.status === 400);
});

test("webhook signature: good passes, tampered or stale fails", () => {
  const secret = "whsec_test", payload = JSON.stringify({ id: "evt_1" }), t = Math.floor(Date.now() / 1000);
  const sig = crypto.createHmac("sha256", secret).update(`${t}.${payload}`).digest("hex");
  assert.doesNotThrow(() => verify(payload, `t=${t},v1=${sig}`, secret));
  assert.throws(() => verify(payload + " ", `t=${t},v1=${sig}`, secret), /Bad signature/);
  assert.throws(() => verify(payload, `t=${t - 3600},v1=${sig}`, secret), /Stale signature/);
  assert.throws(() => verify(payload, "", secret), /Missing signature/);
});
