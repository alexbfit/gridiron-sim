// Copy to config.js and fill in from Supabase → Project Settings → API.
// The anon key is safe to ship to the browser (RLS allows public read only).
window.GRIDIRON_CONFIG = {
  SUPABASE_URL: "https://YOUR-PROJECT.supabase.co",
  SUPABASE_ANON_KEY: "YOUR-ANON-KEY",

  // ---- product switches (all off = the site behaves exactly as before: free, no sign-in) ----
  AUTH_ENABLED: false,          // show Sign in / Account (needs supabase/pending/012_accounts.sql + Supabase Auth email set up)
  REQUIRE_SUBSCRIPTION: false,  // gate paid features for visitors without an active plan (needs AUTH_ENABLED)
  BILLING_LIVE: false,          // show real prices + checkout buttons (needs the Netlify functions in billing/ + Stripe keys)

  // ---- business identity (placeholders until the LLC is formed) ----
  LEGAL_ENTITY: "GameTime Win",             // shown on terms / privacy pages; swap for "GameTime Win LLC" once formed
  CONTACT_EMAIL: "hello@gametimewin.com",   // support + legal contact
  FOUNDING: true,                           // show the founding-price lock on paid plans
  FREE_LINEUPS: 3,                          // Free tier: lineups per slate in Simple Mode (the only lineup cap that exists)

  // ---- plans (single source of truth for pricing.html, index.html, account.html and the gating in shell.js) ----
  // Prices are display copy. Stripe price ids live in Netlify env vars (STRIPE_PRICE_<PLAN>_<INTERVAL>); the priceIds
  // here are informational placeholders you can fill in once the Stripe products exist.
  PLANS: {
    free: {
      key: "free", name: "Free", price: { monthly: "$0" }, lineups: 3,
      tagline: "See how the sim thinks before you pay a cent.",
      features: ["Simple Mode, 3 lineups per slate", "Public Track Record and backtests", "Player stats and simulated ranges"],
    },
    starter: {
      key: "starter", name: "Starter", price: { monthly: "$29", season: "$99" }, lineups: 50, trialDays: 7, featured: true,
      tagline: "Everything you need to play a Sunday main slate well.",
      features: ["50 lineups per build", "DraftKings entries edit file (fills your contests)", "1 PM and 4 PM late-swap files", "Contest simulator", "Sunday news notes"],
      priceIds: { monthly: "", season: "" },   // STRIPE_PRICE_STARTER_MONTHLY / STRIPE_PRICE_STARTER_SEASON
    },
    pro: {
      key: "pro", name: "Pro", price: { monthly: "$59", season: "$199" }, lineups: 500, trialDays: 7,
      tagline: "For max-entry players and anyone who runs their own numbers.",
      features: ["500 lineups per build", "Full projections and CSV export", "Upload your own projections", "Every sport: NBA, MLB, NHL, golf and more", "Private Discord"],
      priceIds: { monthly: "", season: "" },   // STRIPE_PRICE_PRO_MONTHLY / STRIPE_PRICE_PRO_SEASON
    },
    slate: {
      key: "slate", name: "Slate Pass", price: { once: "$5" }, lineups: 50,
      tagline: "24 hours of Starter for one slate. No subscription.",
      features: ["Everything in Starter for 24 hours", "One-time payment, nothing recurring"],
      priceIds: { slate: "" },                 // STRIPE_PRICE_SLATE
    },
  },
  PRICING_NOTES: {
    season: "Season pass runs through the Super Bowl.",
    trial: "7-day free trial on Starter and Pro. Cancel before it ends and you pay nothing.",
    refunds: "No refunds. The trial is there so you can decide before you're charged.",
    priceLock: "No mid-season price changes. The price you start the season at is the price you pay all season.",
    founding: "Founding price — locked for as long as you stay subscribed.",
  },
};
