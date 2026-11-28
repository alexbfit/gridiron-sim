# Membership billing: $99 a month through Stripe

One plan. No free tier, no trial, no passes. A visitor lands on `web/index.html`, signs up (`web/signup.html`, Google or an
emailed link), pays on Stripe Checkout, and comes back to `web/home.html`. Nothing in this folder touches the ingest,
the sim, the lineup jobs or the Sunday tasks.

| Piece | Where |
|---|---|
| Price shown on the site, plan bullet points | `web/config.js` (`PLAN`) |
| Switches | `web/config.js`: `AUTH_ENABLED: true`, `REQUIRE_SUBSCRIPTION: true` |
| Sign-up, log-in, payment step | `web/signup.html` |
| Session check, membership gate, account menu | `web/assets/shell.js` (`GS.Account`) |
| Account page (status, renewal date, Manage billing) | `web/account.html` |
| `profiles` table (`subscription_status`, `current_period_end`, `is_owner`, ...) | `supabase/pending/012_accounts.sql` (applied) |
| Checkout / billing portal / webhook | `billing/functions/*.mjs`, deployed by `[functions]` in `netlify.toml` |
| Tests | `node --test "billing/**/*.test.mjs"` (also run by the Python suite). Keep test files out of `billing/functions/`: Netlify treats every file at the top of that folder as a function |

## How access is decided

`GS.Account.active()` is true when the signed-in user's `profiles` row has `subscription_status` of `active` (or
`trialing`), or `is_owner = true`. Pages in `MEMBER_PAGES` (shell.js) send a signed-out visitor to log in and a
signed-in visitor without a membership to the payment step. `account.html` needs a log-in only, so someone whose card
failed can still reach Manage billing.

The Stripe webhook is the only thing that writes `subscription_status`. After Checkout the browser returns to
`home.html?welcome=1`, and the gate waits up to 20 seconds for the webhook before sending the visitor back to pay.

## Set-up, in order

1. **Make yourself the owner first.** Supabase, Table editor, `profiles`: find your row (log in to the site once if it
   isn't there) and set `is_owner` to true. Do this before the new site goes live or you will meet your own paywall.
2. **Supabase, Authentication, URL configuration:** Site URL `https://gametimewin.com`; add
   `https://gametimewin.com/account.html` to the redirect list. Under Providers, Email must be on; Google is optional
   (a "Continue with Google" button appears on its own once it is enabled). Set up custom SMTP: the built-in mailer
   only sends a few emails an hour.
3. **Stripe (test mode first):** Products, Add product: "GameTime Win Membership", price **$99.00, recurring, monthly**.
   Copy the price id (`price_...`). Settings, Billing, Customer portal: turn it on, allow cancelling and updating the card.
4. **Netlify, Site configuration, Environment variables:**

   | Variable | What |
   |---|---|
   | `STRIPE_SECRET_KEY` | `sk_test_...` first, `sk_live_...` at launch |
   | `STRIPE_PRICE_MEMBER_MONTHLY` | the `price_...` id from step 3 |
   | `STRIPE_WEBHOOK_SECRET` | signing secret from step 6 |
   | `SUPABASE_URL`, `SUPABASE_ANON_KEY`, `SUPABASE_SERVICE_ROLE_KEY` | Supabase, Project settings, API |
   | `SITE_URL` | `https://gametimewin.com` |

5. **Push.** Netlify publishes `web/` and builds the three functions from `billing/functions/` (no packages to install).
6. **Stripe, Developers, Webhooks:** add `https://gametimewin.com/.netlify/functions/stripe-webhook` with the events
   `checkout.session.completed`, `customer.subscription.created`, `customer.subscription.updated`,
   `customer.subscription.deleted`. Copy the signing secret into `STRIPE_WEBHOOK_SECRET` and redeploy.
7. **Test in test mode** with card `4242 4242 4242 4242` on a second email address: sign up, pay, land on Home; Account
   shows "Membership active" and a renewal date; Manage billing opens the portal; cancel there and the account page
   says the membership is ending. Then delete the test customer.
8. **Go live:** repeat step 3 in live mode, swap `STRIPE_SECRET_KEY` and `STRIPE_PRICE_MEMBER_MONTHLY` to the live
   values, add the live webhook and its secret, redeploy. Buy one month yourself with a real card and cancel it.

Until step 4 is done the "Continue to payment" button says payments aren't switched on and charges nothing.

## Before charging money: decisions only you can make

- **The paywall is front-end only.** All slate data is readable with the public Supabase key and the sim files are in a
  public bucket, so a technical user could pull projections without paying. Closing that means RLS policies using
  `has_active_plan()` and a private `sims` bucket with signed URLs. The Sunday tasks read those tables with the same
  public key, so they need their own credentials first.
- **Data licensing.** Check commercial-use terms for each source: The Odds API (the free tier is personal use), Kalshi
  market data, nflverse (CC-BY: credit it), weather (Open-Meteo's free API is non-commercial), DraftKings salary files.
- **Claims.** Don't advertise winnings. One week is most of the season's profit so far.
- **Terms.** `web/terms.html` sections 6 to 9 were rewritten for the single plan (no refunds, cancel any time, 30 days'
  notice of a price change). Have a lawyer read them before launch.
- **Entity and taxes.** Form the LLC before taking live payments; consider Stripe Tax.
