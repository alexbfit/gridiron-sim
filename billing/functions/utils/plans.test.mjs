// Unit tests for the billing plan mapping + the webhook's pure helpers. Run: node --test "billing/**/*.test.mjs"  (also run by tests/test_cli.py)
import { test } from "node:test";
import assert from "node:assert/strict";
import { priceEnvName, checkoutMode, planForPrice, slatePassEnd, TRIAL_DAYS } from "./plans.mjs";
import { subscriptionPatch, slatePassPatch, isSeasonPrice, verify } from "../stripe-webhook.mjs";
import { parsePlan } from "../create-checkout.mjs";
import crypto from "node:crypto";

const ENV = {
  STRIPE_PRICE_STARTER_MONTHLY: "price_sm", STRIPE_PRICE_STARTER_SEASON: "price_ss",
  STRIPE_PRICE_PRO_MONTHLY: "price_pm", STRIPE_PRICE_PRO_SEASON: "price_ps", STRIPE_PRICE_SLATE: "price_slate",
};

test("plan + interval → env var name", () => {
  assert.equal(priceEnvName("starter", "monthly"), "STRIPE_PRICE_STARTER_MONTHLY");
  assert.equal(priceEnvName("pro", "season"), "STRIPE_PRICE_PRO_SEASON");
  assert.equal(priceEnvName("slate", "whatever"), "STRIPE_PRICE_SLATE");
  assert.throws(() => priceEnvName("gold", "monthly"), /Unknown plan/);
  assert.throws(() => priceEnvName("pro", "annual"), /Unknown interval/);
});

test("checkout mode: subscriptions for plans, payment for the slate pass; 7-day trial", () => {
  assert.equal(checkoutMode("starter"), "subscription");
  assert.equal(checkoutMode("pro"), "subscription");
  assert.equal(checkoutMode("slate"), "payment");
  assert.equal(TRIAL_DAYS, 7);
});

test("price id → plan via env vars, PLAN_BY_PRICE override, unknown → null", () => {
  assert.deepEqual(planForPrice("price_sm", ENV), { plan: "starter", interval: "monthly" });
  assert.deepEqual(planForPrice("price_ps", ENV), { plan: "pro", interval: "season" });
  assert.deepEqual(planForPrice("price_slate", ENV), { plan: "slate", interval: "once" });
  assert.equal(planForPrice("price_old", ENV), null);
  assert.equal(planForPrice(null, ENV), null);
  const withOverride = { ...ENV, PLAN_BY_PRICE: JSON.stringify({ price_old: "pro", price_legacy: { plan: "starter", interval: "season" } }) };
  assert.deepEqual(planForPrice("price_old", withOverride), { plan: "pro", interval: "monthly" });
  assert.deepEqual(planForPrice("price_legacy", withOverride), { plan: "starter", interval: "season" });
  assert.equal(planForPrice("x", { ...ENV, PLAN_BY_PRICE: "{not json" }), null);
  assert.equal(isSeasonPrice("price_ss", ENV), true);
  assert.equal(isSeasonPrice("price_sm", ENV), false);
});

test("subscription → profiles patch carries plan, period end, cancel flag", () => {
  const now = new Date("2026-10-01T12:00:00Z");
  const sub = { id: "sub_1", customer: "cus_1", status: "trialing", cancel_at_period_end: false,
    items: { data: [{ price: { id: "price_pm" }, current_period_end: 1760000000 }] }, metadata: { supabase_user_id: "u1", plan: "pro" } };
  const p = subscriptionPatch(sub, ENV, now);
  assert.equal(p.plan, "pro");
  assert.equal(p.price_id, "price_pm");
  assert.equal(p.subscription_status, "trialing");
  assert.equal(p.current_period_end, new Date(1760000000 * 1000).toISOString());
  assert.equal(p.cancel_at_period_end, false);
  assert.equal(p.updated_at, now.toISOString());
  // unknown price falls back to the metadata plan; canceled clears the plan
  const unknown = subscriptionPatch({ ...sub, items: { data: [{ price: { id: "price_?" } }] } }, ENV, now);
  assert.equal(unknown.plan, "pro");
  assert.equal(unknown.current_period_end, null);
  assert.equal(subscriptionPatch({ ...sub, status: "canceled" }, ENV, now).plan, null);
});

test("slate pass: 24h from now, or stacked onto an unexpired pass", () => {
  const now = new Date("2026-10-04T15:00:00Z");
  assert.equal(slatePassEnd(now).toISOString(), "2026-10-05T15:00:00.000Z");
  assert.equal(slatePassPatch(null, now).slate_pass_until, "2026-10-05T15:00:00.000Z");
  assert.equal(slatePassPatch("2026-10-01T00:00:00Z", now).slate_pass_until, "2026-10-05T15:00:00.000Z");   // expired: ignored
  assert.equal(slatePassPatch("2026-10-05T10:00:00Z", now).slate_pass_until, "2026-10-06T10:00:00.000Z");   // live: extended
});

test("create-checkout parses { plan, interval } and defaults", async () => {
  const req = (body) => new Request("http://x", { method: "POST", body: body == null ? undefined : body });
  assert.deepEqual(await parsePlan(req(JSON.stringify({ plan: "pro", interval: "season" }))), { plan: "pro", interval: "season", envName: "STRIPE_PRICE_PRO_SEASON" });
  assert.deepEqual(await parsePlan(req(JSON.stringify({ plan: "slate" }))), { plan: "slate", interval: "once", envName: "STRIPE_PRICE_SLATE" });
  assert.deepEqual(await parsePlan(req(null)), { plan: "starter", interval: "monthly", envName: "STRIPE_PRICE_STARTER_MONTHLY" });
  await assert.rejects(parsePlan(req(JSON.stringify({ plan: "vip" }))), /Unknown plan/);
  await assert.rejects(parsePlan(req("not json")), /must be JSON/);
});

test("webhook signature verification", () => {
  const secret = "whsec_test", payload = '{"id":"evt_1"}', t = Math.floor(Date.now() / 1000);
  const sig = crypto.createHmac("sha256", secret).update(`${t}.${payload}`).digest("hex");
  assert.doesNotThrow(() => verify(payload, `t=${t},v1=${sig}`, secret));
  assert.throws(() => verify(payload, `t=${t},v1=${"0".repeat(64)}`, secret), /Bad signature/);
  assert.throws(() => verify(payload, `t=${t - 1000},v1=${sig}`, secret), /Stale signature/);
  assert.throws(() => verify(payload, "", secret), /Missing signature/);
});
