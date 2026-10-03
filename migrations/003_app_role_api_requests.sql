-- Give the least-privilege API role (002_app_role.sql) access to the request-metering
-- table from 002_api_requests.sql. Without this, /v1/usage (the /dashboard data) returns
-- 503 and per-request metering silently records nothing.
-- Apply after 001_init.sql, 002_api_requests.sql and 002_app_role.sql.

grant select, insert on canon.api_requests to canon_app;
grant usage on all sequences in schema canon to canon_app;

drop policy if exists canon_app_all on canon.api_requests;
create policy canon_app_all on canon.api_requests for all to canon_app using (true) with check (true);
