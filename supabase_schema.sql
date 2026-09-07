-- ====================================================================
-- TaxEaseLK: Supabase Schema for Authentication & User Roles
-- Run this in your Supabase Dashboard: SQL Editor -> New query -> Run
-- ====================================================================

-- 1. Create the public profiles table linked to auth.users
create table if not exists public.profiles (
  id uuid references auth.users(id) on delete cascade primary key,
  email text,
  display_name text,
  role text not null check (role in ('COMPANY_ADMIN', 'COMPANY_STAFF', 'AUDITOR_PARTNER', 'AUDITOR_STAFF')),
  category text,
  specialization text,
  created_at timestamp with time zone default timezone('utc'::text, now()) not null,
  updated_at timestamp with time zone default timezone('utc'::text, now()) not null
);

-- 2. Enable Row Level Security (RLS)
alter table public.profiles enable row level security;

-- 3. Create RLS Policies
-- Allow authenticated users to view profiles
drop policy if exists "Allow authenticated users to read profiles" on public.profiles;
create policy "Allow authenticated users to read profiles"
  on public.profiles for select
  to authenticated
  using (true);

-- Allow users to insert their own profile
drop policy if exists "Allow users to insert own profile" on public.profiles;
create policy "Allow users to insert own profile"
  on public.profiles for insert
  to authenticated
  with check (auth.uid() = id);

-- Allow users to update their own profile
drop policy if exists "Allow users to update own profile" on public.profiles;
create policy "Allow users to update own profile"
  on public.profiles for update
  to authenticated
  using (auth.uid() = id);

-- Allow service_role key full unrestricted access
drop policy if exists "Service role has full access" on public.profiles;
create policy "Service role has full access"
  on public.profiles for all
  to service_role
  using (true)
  with check (true);

-- 4. Create Trigger to automatically create profile on signup
create or replace function public.handle_new_user()
returns trigger as $$
begin
  insert into public.profiles (id, email, display_name, role, category, specialization)
  values (
    new.id,
    new.email,
    coalesce(new.raw_user_meta_data->>'display_name', ''),
    coalesce(new.raw_user_meta_data->>'role', 'COMPANY_ADMIN'),
    new.raw_user_meta_data->>'category',
    new.raw_user_meta_data->>'specialization'
  )
  on conflict (id) do update set
    email = excluded.email,
    display_name = coalesce(excluded.display_name, public.profiles.display_name),
    role = coalesce(excluded.role, public.profiles.role),
    category = coalesce(excluded.category, public.profiles.category),
    specialization = coalesce(excluded.specialization, public.profiles.specialization),
    updated_at = now();
  return new;
end;
$$ language plpgsql security definer;

-- Recreate trigger
drop trigger if exists on_auth_user_created on auth.users;
create trigger on_auth_user_created
  after insert on auth.users
  for each row execute function public.handle_new_user();

-- Verify table creation
select * from public.profiles limit 5;
