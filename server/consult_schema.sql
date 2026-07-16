-- Consultation Scribe module. Separate from intake_sessions (the reception
-- chatbot) — the scribe is a passive listener that transcribes a live
-- doctor-patient consultation and stores an AI-generated clinical summary.
--
-- Run this once in the Supabase SQL editor for your project.
create table if not exists consultation_notes (
  id          uuid primary key default gen_random_uuid(),
  created_at  timestamptz not null default now(),
  status      text not null default 'summarized',      -- summarized | failed
  duration_s  integer,                                  -- consultation length in seconds
  transcript  jsonb not null default '[]'::jsonb,       -- raw utterance segments, in order
  summary     jsonb not null default '{}'::jsonb,       -- structured clinical note (see scribe/summarizer.py)
  model       text                                      -- which LLM produced the summary
);

-- Same posture as intake_sessions: the server reaches the DB only with the
-- service_role key. Enable RLS with no public policies -> deny-all from the
-- browser/anon key.
alter table consultation_notes enable row level security;
