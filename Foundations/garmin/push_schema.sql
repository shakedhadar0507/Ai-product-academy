-- Push notification devices for Shaked Hub. Paste into Supabase → SQL Editor → Run (once).
-- Row Level Security is on with no policies, so only the server-side secret key can read or write.

create table if not exists push_subscriptions (
    endpoint      text primary key,
    subscription  jsonb not null,
    created_at    timestamptz not null default now()
);

alter table push_subscriptions enable row level security;
