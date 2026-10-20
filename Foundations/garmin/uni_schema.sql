-- University alerts already sent to the phone, so nothing is pushed twice.
-- Paste into Supabase → SQL Editor → Run (once).
-- Row Level Security is on with no policies, so only the server-side secret key can read or write.

create table if not exists uni_alerts (
    key         text primary key,
    created_at  timestamptz not null default now()
);

alter table uni_alerts enable row level security;
