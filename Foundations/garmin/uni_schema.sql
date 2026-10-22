-- University tasks and the alerts already sent for them. Paste into Supabase → SQL Editor → Run (once).
-- Row Level Security is on with no policies, so only the server-side secret key can read or write.

-- Anything with a deadline: Moodle events and assignments or exams found in uni mails.
create table if not exists uni_tasks (
    key         text primary key,
    source      text not null,
    kind        text not null,
    title       text not null,
    course      text,
    due         timestamptz,
    url         text,
    first_seen  timestamptz not null default now(),
    done        boolean not null default false,
    done_at     timestamptz
);

-- Every push sent (and every mail read), so nothing is pushed twice.
create table if not exists uni_alerts (
    key         text primary key,
    created_at  timestamptz not null default now()
);

alter table uni_tasks enable row level security;
alter table uni_alerts enable row level security;
