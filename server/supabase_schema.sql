-- Run this once in the Supabase SQL editor for your project.
create table if not exists intake_sessions (
  id          uuid primary key default gen_random_uuid(),
  created_at  timestamptz not null default now(),
  status      text not null default 'in_progress',   -- in_progress | completed | abandoned
  patient     jsonb not null default '{}'::jsonb,
  transcript  jsonb not null default '[]'::jsonb,     -- full turn-by-turn log (audit + debugging)
  flags       jsonb not null default '[]'::jsonb
);

-- PoC talks to the DB only from the backend with the service_role key.
-- Enable RLS with no public policies -> deny-all from the browser/anon key.
alter table intake_sessions enable row level security;
