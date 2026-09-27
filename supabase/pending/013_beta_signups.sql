-- 013: beta email signups for gametimewin.com (index.html form).
-- Visitors (anon key) can INSERT only. Nobody can read the list through the public API;
-- view it in Supabase → Table Editor → beta_signups, or export CSV from there.
create table if not exists public.beta_signups (
  id uuid primary key default gen_random_uuid(),
  email text not null check (char_length(email) between 5 and 254 and email ~* '^[^@[:space:]]+@[^@[:space:]]+\.[^@[:space:]]{2,}$'),
  source text check (source is null or char_length(source) <= 40),
  referrer text check (referrer is null or char_length(referrer) <= 300),
  utm text check (utm is null or char_length(utm) <= 200),
  created_at timestamptz not null default now()
);
create unique index if not exists beta_signups_email_key on public.beta_signups (lower(email));

alter table public.beta_signups enable row level security;
drop policy if exists "anyone can join the beta" on public.beta_signups;
create policy "anyone can join the beta" on public.beta_signups for insert to anon, authenticated with check (true);
grant insert on public.beta_signups to anon, authenticated;
revoke select, update, delete on public.beta_signups from anon, authenticated;
