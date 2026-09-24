// Copy to config.js and fill in from Supabase → Project Settings → API.
// The anon key is safe to ship to the browser (RLS allows public read only).
window.GRIDIRON_CONFIG = {
  SUPABASE_URL: "https://hmckvrmsgmdsuyjsuvcq.supabase.co",
  SUPABASE_ANON_KEY: "sb_publishable_45X4VDNlLYDp9yJERH40oA_yeTF4Jcz",

  // ---- product switches (all off = the site behaves exactly as before: free, no sign-in) ----
  AUTH_ENABLED: false,          // show Sign in / Account (needs supabase/pending/012_accounts.sql + Supabase Auth email set up)
  REQUIRE_SUBSCRIPTION: false,  // paywall the Lineup Builder for visitors without an active plan (needs AUTH_ENABLED)
  BILLING_LIVE: false,          // show the paid plan + Subscribe button (needs the Netlify functions in billing/ + Stripe keys)
  PLAN: {                       // pricing copy used by the landing page and the account page
    name: "Pro",
    price: "$19",
    description: "Everything, every week of the season.",
    headline: "One plan. Every slate.",
    subhead: "Cancel any time from your account page.",
    tag: "Founding price",
  },
};
