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

-- 5. Create Documents table for Financial Schedules & Workpapers
create table if not exists public.documents (
  id text primary key,
  user_id uuid references auth.users(id) on delete cascade,
  company_name text not null default 'ABC (Pvt) Ltd',
  name text not null,
  file_path text not null,
  file_size bigint not null default 0,
  doc_type text not null check (doc_type in (
    'Financial Statements', 'Trial Balance', 'General Ledger',
    'Fixed Assets', 'Previous CIT', 'Bank Statements', 'Other'
  )),
  status text not null check (status in ('processing', 'processed', 'review_required', 'missing')),
  ai_confidence_percent integer default 95,
  extracted_data jsonb default '{}'::jsonb,
  gdrive_file_id text,
  gdrive_view_link text,
  uploaded_at timestamp with time zone default timezone('utc'::text, now()) not null
);

-- Enable RLS on documents
alter table public.documents enable row level security;

drop policy if exists "Allow authenticated users to read documents" on public.documents;
create policy "Allow authenticated users to read documents"
  on public.documents for select
  to authenticated
  using (true);

drop policy if exists "Allow users to insert own documents" on public.documents;
create policy "Allow users to insert own documents"
  on public.documents for insert
  to authenticated
  with check (auth.uid() = user_id or user_id is null);

drop policy if exists "Allow users to delete own documents" on public.documents;
create policy "Allow users to delete own documents"
  on public.documents for delete
  to authenticated
  using (auth.uid() = user_id or user_id is null);

drop policy if exists "Service role has full access on documents" on public.documents;
create policy "Service role has full access on documents"
  on public.documents for all
  to service_role
  using (true)
  with check (true);

-- 6. Create Invitations table for Auditor & Finance Team engagements
create table if not exists public.invitations (
  id text primary key,
  company_name text not null default 'ABC (Pvt) Ltd',
  invite_type text not null check (invite_type in ('AUDITOR', 'FINANCE_TEAM')),
  email text not null,
  name text,
  firm_name text,
  role text,
  can_sign_returns boolean default false,
  status text not null default 'PENDING' check (status in ('PENDING', 'ACCEPTED', 'REVOKED')),
  created_at timestamp with time zone default timezone('utc'::text, now()) not null
);

-- Enable RLS on invitations
alter table public.invitations enable row level security;

drop policy if exists "Allow authenticated users to read invitations" on public.invitations;
create policy "Allow authenticated users to read invitations"
  on public.invitations for select
  to authenticated
  using (true);

drop policy if exists "Allow users to insert invitations" on public.invitations;
create policy "Allow users to insert invitations"
  on public.invitations for insert
  to authenticated
  with check (true);

drop policy if exists "Service role has full access on invitations" on public.invitations;
create policy "Service role has full access on invitations"
  on public.invitations for all
  to service_role
  using (true)
  with check (true);

-- 7. Create Discussion Threads & Messages for Business <-> Auditor Clarifications
create table if not exists public.discussion_threads (
  id text primary key,
  company_name text not null default 'ABC Holdings (Pvt) Ltd',
  topic text not null,
  category text default 'General Audit Inquiry',
  status text not null default 'Open' check (status in ('Open', 'Closed')),
  last_message text,
  last_updated timestamp with time zone default timezone('utc'::text, now()) not null,
  auditor_email text,
  created_by uuid references auth.users(id) on delete set null,
  created_at timestamp with time zone default timezone('utc'::text, now()) not null
);

create table if not exists public.discussion_messages (
  id text primary key,
  thread_id text references public.discussion_threads(id) on delete cascade not null,
  sender text not null,
  sender_role text not null check (sender_role in ('Company', 'Auditor')),
  text text not null,
  user_id uuid references auth.users(id) on delete set null,
  created_at timestamp with time zone default timezone('utc'::text, now()) not null
);

-- Enable RLS
alter table public.discussion_threads enable row level security;
alter table public.discussion_messages enable row level security;

create policy "Allow all authenticated users threads" on public.discussion_threads for all to authenticated using (true) with check (true);
create policy "Service role full access threads" on public.discussion_threads for all to service_role using (true) with check (true);

create policy "Allow all authenticated users messages" on public.discussion_messages for all to authenticated using (true) with check (true);
create policy "Service role full access messages" on public.discussion_messages for all to service_role using (true) with check (true);

-- Verify table creation
select * from public.profiles limit 5;


