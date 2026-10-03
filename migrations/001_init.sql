-- Canon schema for Postgres (Supabase). Applied once per database.
-- Tables live in a dedicated `canon` schema that is NOT exposed through
-- Supabase's Data API; the app connects as a server-side Postgres role.
-- RLS is enabled with no policies as defense in depth: anon/authenticated
-- roles can read nothing even if the schema were ever exposed.

create schema if not exists canon;
revoke all on schema canon from public, anon, authenticated;

create table if not exists canon.accounts (
    id text primary key,
    name text not null,
    email text,
    status text not null check (status in ('trial', 'active', 'past_due', 'canceled', 'sandbox')),
    stripe_customer_id text unique,
    stripe_subscription_id text,
    created_at text not null
);

create table if not exists canon.api_keys (
    id text primary key,
    account_id text not null references canon.accounts(id) on delete cascade,
    key_hash text not null unique,
    prefix text not null,
    created_at text not null,
    revoked_at text
);
create index if not exists api_keys_account on canon.api_keys(account_id);

create table if not exists canon.usage_events (
    id text primary key,
    account_id text not null references canon.accounts(id) on delete cascade,
    kind text not null,
    quantity integer not null,
    billable integer not null default 1,
    document_id text,
    created_at text not null,
    reported_at text,
    report_error text
);
create index if not exists usage_account on canon.usage_events(account_id, created_at);
create index if not exists usage_unreported on canon.usage_events(created_at) where reported_at is null and billable = 1;

create table if not exists canon.stripe_events (
    id text primary key,
    type text not null,
    received_at text not null
);

create table if not exists canon.patients (
    account_id text not null references canon.accounts(id) on delete cascade,
    id text not null,
    created_at text not null,
    primary key (account_id, id)
);

create table if not exists canon.patient_keys (
    account_id text not null,
    key text not null,
    patient_id text not null,
    primary key (account_id, key, patient_id),
    foreign key (account_id, patient_id) references canon.patients(account_id, id) on delete cascade
);

create table if not exists canon.documents (
    id text primary key,
    account_id text not null,
    patient_id text not null,
    format text not null,
    filename text,
    source_name text,
    received_at text not null,
    document_date text,
    sha256 text not null,
    size integer not null,
    raw bytea not null,
    info text not null,
    items text not null,
    foreign key (account_id, patient_id) references canon.patients(account_id, id) on delete cascade
);
create index if not exists documents_patient on canon.documents(account_id, patient_id);
create unique index if not exists documents_dedupe on canon.documents(account_id, patient_id, sha256);

create table if not exists canon.audit (
    seq bigint generated always as identity primary key,
    id text not null unique,
    ts text not null,
    event text not null,
    actor text not null,
    account_id text,
    patient_id text,
    document_id text,
    detail text not null,
    prev_hash text not null,
    hash text not null
);
create index if not exists audit_account on canon.audit(account_id, seq);

-- Append-only audit log: block updates and deletes at the database level.
create or replace function canon.audit_append_only() returns trigger
language plpgsql set search_path = '' as $$
begin
    raise exception 'canon.audit is append-only';
end;
$$;
drop trigger if exists audit_no_update on canon.audit;
create trigger audit_no_update before update or delete on canon.audit
    for each row execute function canon.audit_append_only();

alter table canon.accounts enable row level security;
alter table canon.api_keys enable row level security;
alter table canon.usage_events enable row level security;
alter table canon.stripe_events enable row level security;
alter table canon.patients enable row level security;
alter table canon.patient_keys enable row level security;
alter table canon.documents enable row level security;
alter table canon.audit enable row level security;
