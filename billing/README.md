# Selling Gridiron Sim — launch checklist

Everything here is **off** today. The site runs exactly as before (free, no sign-in) until you flip the
switches in `web/config.js`. Nothing in this folder touches the ingest, the sim, the lineup jobs or the
Sunday tasks.

| Piece | Where | State |
|---|---|---|
| Sign-in (email magic link) + account page | `web/account.html`, `web/assets/shell.js` | built, `AUTH_ENABLED: false` |
| `profiles` table (subscription status per user) | `supabase/pending/012_accounts.sql` | written, **not applied** |
| Stripe Checkout / billing portal / webhook | `billing/functions/*.mjs` (no npm deps) | written + unit-tested with mocks, **not deployed** |
| Paywall overlay on the builder | `data-requires-sub` on `lineups.html` | built, `REQUIRE_SUBSCRIPTION: false` |
| Pricing copy | `PLAN` in `web/config.js` | placeholder ($19) — your call |

## Turning it on (about an hour, in this order)

1. **Supabase → SQL editor:** run `supabase/pending/012_accounts.sql`, then move it to `supabase/migrations/`.
2. **Supabase → Authentication → Providers → Email:** enable it (magic link). Under *URL configuration* set the
   Site URL to `https://gridiron-sim.netlify.app` and add `https://gridiron-sim.netlify.app/account.html` as a redirect.
   For real volume, set up custom SMTP (Supabase's built-in mailer is rate-limited to a few emails an hour).
3. **Stripe:** create a Product ("Gridiron Sim Pro") with a monthly recurring Price → copy the `price_…` id.
   Turn on the customer portal (Settings → Billing → Customer portal).
4. **Netlify → Site configuration → Environment variables:** add
   `STRIPE_SECRET_KEY`, `STRIPE_PRICE_ID`, `STRIPE_WEBHOOK_SECRET` (step 6), `SUPABASE_URL`, `SUPABASE_ANON_KEY`,
   `SUPABASE_SERVICE_ROLE_KEY`, `SITE_URL=https://gridiron-sim.netlify.app`.
5. **Deploy the functions:** move `billing/functions/` to `netlify/functions/` (Netlify picks that folder up
   automatically — no build step or packages needed) and push.
6. **Stripe → Developers → Webhooks:** add `https://gridiron-sim.netlify.app/.netlify/functions/stripe-webhook`
   with events `checkout.session.completed`, `customer.subscription.created`, `customer.subscription.updated`,
   `customer.subscription.deleted`. Copy the signing secret into `STRIPE_WEBHOOK_SECRET`.
7. **`web/config.js`:** set `AUTH_ENABLED: true`, `BILLING_LIVE: true`, edit `PLAN`, and when you want the paywall,
   `REQUIRE_SUBSCRIPTION: true`. Push. Test end-to-end in Stripe **test mode** first (card 4242 4242 4242 4242).

Your own access: after you sign in once, set `is_owner = true` on your row in `profiles` (Supabase table editor) —
that skips the paywall. Separately, visiting any page with `?owner=1` shows your private sections (Sunday-task
lineups, Flashback, news agents) and records your builder exports for Monday grading; `?owner=0` turns it off.
`?owner=1` never unlocks the paywall.

## Before charging money — decisions only you can make

- **The paywall is front-end only.** All slate data is readable with the public Supabase key and the sim files
  are in a public bucket, so a technical user could pull projections without paying. Closing that means
  RLS policies on `slate_projections` / `slate_board` / `slate_results` using `has_active_plan()` and a private
  `sims` bucket with signed URLs. **Careful:** the Sunday Claude tasks read those tables with the same public key,
  so they would need their own credentials first. Worth doing once there are paying users; not before.
- **Data licensing.** Check the terms for commercial use of each source: The Odds API (the free tier is for
  personal use — a paid plan is likely needed), Kalshi market data, nflverse (CC-BY: credit it), weather
  (Open-Meteo's free API is non-commercial; their commercial plan is cheap), and DraftKings salary files.
- **Legal.** Terms of service + privacy policy pages, and whether DFS-tool subscriptions need anything in the
  states you'll market to. Keep the responsible-play line that's already in the footer.
- **Price.** `PLAN.price` is a placeholder.
