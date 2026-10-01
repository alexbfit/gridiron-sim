# Selling GameTime Win — plans, Stripe setup and launch checklist

Everything here is **off** today. The site runs exactly as before (free, no sign-in) until you flip the
switches in `web/config.js`. Nothing in this folder touches the ingest, the sim, the lineup jobs or the
Sunday tasks (`jobs/` is frozen).

| Piece | Where | State |
|---|---|---|
| Plans, prices, feature lists (`PLANS`, `FREE_LINEUPS`, `FOUNDING`) | `web/config.js` | written — single source of truth |
| Pricing page (3 tiers + Slate Pass, comparison table, FAQ, variance statement) | `web/pricing.html` + `web/pricing.js` + `web/assets/plans.js` | built; CTAs say "Join the free beta" until `BILLING_LIVE` |
| Landing page pricing summary | `web/index.html#pricing` | built |
| Sign-in (email magic link) + account page with plan picker | `web/account.html`, `web/assets/shell.js` | built, `AUTH_ENABLED: false` |
| `profiles` table (`subscription_status`, `plan`, `slate_pass_until`, `is_owner`) + `has_active_plan()` / `current_plan()` | `supabase/pending/012_accounts.sql` | written, **not applied** |
| Stripe Checkout (plan + interval, 7-day trial) / billing portal / webhook (plan + slate pass) | `billing/functions/*.mjs` (no npm deps) | written, unit-tested (`node --test "billing/**/*.test.mjs"`), **not deployed** |
| Free-tier gating: Simple Mode capped at `FREE_LINEUPS` with an upgrade card; full builder paywalled | `web/simple.js`, `data-requires-sub` on `lineups.html`, `GS.Account` in `shell.js` | built, `REQUIRE_SUBSCRIPTION: false` |
| Terms / Privacy / Responsible play (state availability, 1-800-GAMBLER) | `web/terms.html`, `web/privacy.html`, `web/responsible.html` | written; `LEGAL_ENTITY` + `CONTACT_EMAIL` placeholders in config |

## The tier model

| | Free | Starter | Pro | Slate Pass |
|---|---|---|---|---|
| Price | $0 | **$29/mo** or **$99 NFL season pass** | **$59/mo** or **$199 season pass** | **$5 one-time** |
| Lineups per build | 3 per slate (Simple Mode) | 50 | 500 | Starter for 24 h |
| Includes | Simple Mode, public Track Record + backtests, player stats | + full builder, DraftKings entries edit file, 1 PM + 4 PM late-swap files, contest simulator, Sunday news notes | + projections + CSV export, custom projection upload, every sport, Discord | everything in Starter |
| Trial | — | 7 days | 7 days | — |

Rules baked into the copy and the code: **7-day free trial** on Starter/Pro (card up front, charged day 8), **no refunds**,
**no mid-season price changes**, **no annual plan** (the season pass is the long option and never renews), and a
**founding price** that stays locked while the subscription stays active (`FOUNDING: true` shows the badge).

How the tiers map to Stripe:

- Monthly plans are ordinary **monthly recurring** prices.
- Season passes are **yearly recurring** prices. The webhook sets `cancel_at_period_end = true` on them the moment they're
  created, so they cover the whole season through the Super Bowl and never bill again. (A one-time price can't have a
  trial; a yearly price with cancel-at-end can.)
- The Slate Pass is a **one-time** price bought in `mode: "payment"`. The webhook sets `profiles.slate_pass_until = now() + 24h`
  (stacked onto an unexpired pass if they buy two).
- `profiles.plan` is derived from the price id (`billing/functions/utils/plans.mjs`), with `PLAN_BY_PRICE` (JSON) as an
  optional override for legacy prices.

What the front end does with it (`GS.Account` in `web/assets/shell.js`): `plan()` → `"free" | "starter" | "pro"`,
`lineupCap()` → `FREE_LINEUPS` / 50 / 500 / `Infinity`, `active()` → true for an active or trialing subscription, an
unexpired slate pass, or `is_owner`. **All of this reads as "pro / unlimited" while `AUTH_ENABLED` or
`REQUIRE_SUBSCRIPTION` is false**, which is why the live site doesn't change until you flip them.

## Environment variables (Netlify → Site configuration → Environment variables)

| Variable | What |
|---|---|
| `STRIPE_SECRET_KEY` | `sk_test_…` first, `sk_live_…` at launch |
| `STRIPE_WEBHOOK_SECRET` | signing secret of the webhook endpoint (step 6) |
| `STRIPE_PRICE_STARTER_MONTHLY` | Price id, Starter, $29 recurring monthly |
| `STRIPE_PRICE_STARTER_SEASON` | Price id, Starter, $99 recurring **yearly** |
| `STRIPE_PRICE_PRO_MONTHLY` | Price id, Pro, $59 recurring monthly |
| `STRIPE_PRICE_PRO_SEASON` | Price id, Pro, $199 recurring **yearly** |
| `STRIPE_PRICE_SLATE` | Price id, Slate Pass, $5 **one-time** |
| `PLAN_BY_PRICE` (optional) | JSON `{"price_…":"starter"}` for any extra/legacy prices |
| `SUPABASE_URL`, `SUPABASE_ANON_KEY`, `SUPABASE_SERVICE_ROLE_KEY` | Supabase → Project Settings → API |
| `SITE_URL` | `https://gametimewin.com` |

(`STRIPE_PRICE_ID` from the old single-plan setup is still honoured as a fallback for Starter monthly; you can delete it.)

## Stripe products to create (Products → Add product)

1. **GameTime Win Starter** — two prices: `$29.00 / month` recurring, and `$99.00 / year` recurring (name it "NFL season pass"
   in the price description so it's obvious on invoices).
2. **GameTime Win Pro** — two prices: `$59.00 / month` recurring, and `$199.00 / year` recurring ("NFL season pass").
3. **GameTime Win Slate Pass** — one price: `$5.00` one-time.
4. Settings → Billing → **Customer portal**: turn on; allow cancel, allow switching between the Starter and Pro monthly prices
   (so people can upgrade/downgrade themselves), don't allow quantity changes.
5. Optional: a `FOUNDING` coupon/promotion code if you want to discount beta players rather than just lock their price
   (`allow_promotion_codes` is on in Checkout).

Copy each `price_…` id into the matching env var above. Do all of this in **test mode** first, then repeat in live mode.

## Launch checklist (in this order)

1. **Legal placeholders.** In `web/config.js` set `LEGAL_ENTITY` (e.g. "GameTime Win LLC" once formed) and `CONTACT_EMAIL`
   to a mailbox you read. Re-read `web/terms.html`, `privacy.html`, `responsible.html` once with fresh eyes; the dates at the
   top say October 1, 2026.
2. **Supabase → SQL editor:** run `supabase/pending/012_accounts.sql`, then move it to `supabase/migrations/`.
   (It's idempotent: it adds `plan` / `slate_pass_until` to an existing `profiles` table too.)
3. **Supabase → Authentication → Providers → Email:** enable it (magic link). Under *URL configuration* set the Site URL to
   `https://gametimewin.com` and add `https://gametimewin.com/account.html` as a redirect. Set up custom SMTP (the built-in
   mailer allows a few emails an hour).
4. **Stripe (test mode):** create the three products / five prices above; turn on the customer portal.
5. **Netlify env vars:** add everything in the table above (test keys first).
6. **Deploy the functions:** move `billing/functions/` to `netlify/functions/` (Netlify picks that folder up automatically —
   no build step or packages needed; `utils/` comes along) and push.
7. **Stripe → Developers → Webhooks:** add `https://gametimewin.com/.netlify/functions/stripe-webhook` with events
   `checkout.session.completed`, `customer.subscription.created`, `customer.subscription.updated`,
   `customer.subscription.deleted`. Copy the signing secret into `STRIPE_WEBHOOK_SECRET`.
8. **`web/config.js`:** set `AUTH_ENABLED: true`. Push. Sign in once, then set `is_owner = true` on your row in `profiles`
   (Supabase table editor) so you skip the paywall.
9. **Test end-to-end in test mode** (card `4242 4242 4242 4242`) with `BILLING_LIVE: true` on a branch deploy or locally:
   Starter monthly (should show "Trial" and a renew date 7 days out), a season pass (should show "Ends …" about a year out,
   `cancel_at_period_end = true` in Stripe), a Slate Pass (`slate_pass_until` ≈ now + 24 h; Simple Mode cap lifts),
   cancel from the portal (status → `canceled`, `plan` → null). With `REQUIRE_SUBSCRIPTION: true`, check a fresh account
   sees 3 lineups + the upgrade card in Simple Mode and the paywall on the Lineup Builder.
10. **Email the beta list** a week ahead: plans, prices, founding-price lock, launch date.
11. **Go live:** switch Stripe to live mode (re-create products, swap all `STRIPE_*` env vars to live values, new webhook
    endpoint + secret). In `web/config.js` set `BILLING_LIVE: true` and `REQUIRE_SUBSCRIPTION: true`. Push. Buy a Starter
    monthly yourself with a real card and cancel it to confirm the live path.
12. **Pin the season end.** The season pass is a yearly price with cancel-at-end, so it comfortably covers the Super Bowl.
    If you ever want it to end exactly at the Super Bowl instead, set `cancel_at` on those subscriptions from the Stripe
    dashboard or add a date to the webhook.

## Before charging money — decisions only you can make

- **The paywall is front-end only.** All slate data is readable with the public Supabase key and the sim files are in a
  public bucket, so a technical user could pull projections without paying. Closing that means RLS policies on
  `slate_projections` / `slate_board` / `slate_results` using `has_active_plan()` (or `current_plan()` for Pro-only data),
  and a private `sims` bucket with signed URLs. **Careful:** the Sunday Claude tasks read those tables with the same public
  key, so they would need their own credentials first. Worth doing once there are paying users; not before.
- **Pro features that don't exist as separate pages yet** (projections CSV export, custom projection upload, Discord,
  non-NFL sports in the full builder) are promised on the pricing page. Simple Mode already handles every sport from a
  projections CSV; the rest is roadmap. Either ship them before launch or soften the Pro copy in `config.js`.
- **Data licensing.** Check the terms for commercial use of each source: The Odds API (the free tier is for personal use —
  a paid plan is likely needed), Kalshi market data, nflverse (CC-BY: credit it), weather (Open-Meteo's free API is
  non-commercial; their commercial plan is cheap), and DraftKings salary files.
- **Entity and taxes.** Form the LLC before taking live payments; set Stripe Tax on if you want sales tax handled for the
  states that tax digital subscriptions.
- **Price.** The numbers in `PLANS` came out of the October 2026 market research; they're copy, change them in one place.
