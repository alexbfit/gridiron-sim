// Copy to config.js and fill in from Supabase > Project Settings > API. The anon key is safe to ship to the browser.
window.GRIDIRON_CONFIG = {
  SUPABASE_URL: "https://YOUR-PROJECT.supabase.co",
  SUPABASE_ANON_KEY: "YOUR-ANON-KEY",

  // ---- access ----
  AUTH_ENABLED: true,           // every tool page needs a log-in
  AUTH_PROVIDERS: ["google", "apple", "discord", "facebook"],   // a button shows only once that provider is enabled in Supabase > Authentication > Providers. An emailed link is always offered.
  REQUIRE_SUBSCRIPTION: true,   // a log-in alone is not enough: the tools need an active membership (or profiles.is_owner)

  // ---- the one plan ----
  // Display copy only. The Stripe price id lives in the Netlify env var STRIPE_PRICE_MEMBER_MONTHLY.
  PLAN: {
    key: "member", name: "Membership", price: "$99", interval: "month",
    includes: [
      "Lineups for every Sunday main slate, ready to upload",
      "Player projections with best-case and worst-case ranges",
      "Prop prices compared with our numbers",
      "Every week's results, graded after the games",
    ],
  },

  // ---- business identity (placeholders until the LLC is formed) ----
  LEGAL_ENTITY: "GameTime Win",             // shown on terms / privacy pages
  CONTACT_EMAIL: "hello@gametimewin.com",   // support + legal contact
};
