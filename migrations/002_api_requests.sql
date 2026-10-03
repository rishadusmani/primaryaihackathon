-- Per-request API metering behind GET /v1/usage and the /dashboard page.
-- One row per authenticated agent request (HTTP API or MCP tool call).
-- Separate from usage_events, which holds only billable document events.

create table if not exists canon.api_requests (
    seq bigint generated always as identity primary key,
    ts text not null,
    account_id text not null references canon.accounts(id) on delete cascade,
    channel text not null,
    operation text not null,
    status integer not null,
    latency_ms double precision not null,
    bytes_in integer not null default 0,
    bytes_out integer not null default 0,
    patient_id text,
    llm_input_tokens integer not null default 0,
    llm_output_tokens integer not null default 0
);
create index if not exists api_requests_account on canon.api_requests(account_id, ts);

alter table canon.api_requests enable row level security;
