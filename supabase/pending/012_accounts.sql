-- 012_accounts.sql — subscriber accounts (NOT applied yet; run it only when you're ready to turn on sign-in).
-- Adds one table. Touches nothing the ingest / sim / lineup jobs use.
--
-- profiles: one row per signed-in user, created automatically on sign-up.
-- The Stripe webhook (billing/functions/stripe-webhook.mjs, service-role key) keeps the subscription columns current.
-- Users can read their own row only; nobody but the service role can write the subscription fields.
-- If a `profiles` table already exists in the project, rename one of them first (create table if not exists would skip).

create table if not exists profiles (
  id                    uuid primary key references auth.users (id) on delete cascade,
  email                 text,
  stripe_customer_id    text unique,
  subscription_id       text,
  subscription_status   text not null default 'none',   -- none | trialing | active | past_due | canceled | unpaid | incomplete
  price_id              text,
  current_period_end    timestamptz,
  cancel_at_period_end  boolean not null default false,
  is_owner              boolean not null default false,   -- set true on your own row by hand: skips the paywall
  created_at            timestamptz not null default now(),
  updated_at            timestamptz not null default now()
);

alter table profiles enable row level security;
drop policy if exists "profiles read own" on profiles;
create policy "profiles read own" on profiles for select using (auth.uid() = id);
-- no insert/update/delete policies: only the trigger below and the service role write this table

create or replace function handle_new_user() returns trigger
language plpgsql security definer set search_path = public as $$
begin
  insert into public.profiles (id, email) values (new.id, new.email)
  on conflict (id) do update set email = excluded.email;
  return new;
end $$;

-- keep the email current when a user changes it
create or replace function handle_user_email() returns trigger
language plpgsql security definer set search_path = public as $$
begin
  update public.profiles set email = new.email, updated_at = now() where id = new.id;
  return new;
end $$;
drop trigger if exists on_auth_user_email on auth.users;
create trigger on_auth_user_email after update of email on auth.users
  for each row execute function handle_user_email();

drop trigger if exists on_auth_user_created on auth.users;
create trigger on_auth_user_created after insert on auth.users
  for each row execute function handle_new_user();

-- helper for later (paywalling data with RLS): true when the caller has a live plan
create or replace function has_active_plan() returns boolean
language sql stable security definer set search_path = public as $$
  select exists (select 1 from profiles where id = auth.uid() and subscription_status in ('active', 'trialing'));
$$;
