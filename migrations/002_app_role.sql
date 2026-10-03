-- Least-privilege role for the hosted API (DATABASE_URL), instead of `postgres`.
-- Its password is set outside migrations:
--   alter role canon_app with login password '<generated>';
-- Connect through the transaction pooler as user `canon_app.<project-ref>`.

do $$
begin
    if not exists (select 1 from pg_roles where rolname = 'canon_app') then
        create role canon_app nologin;
    end if;
end;
$$;

grant usage on schema canon to canon_app;
grant select, insert, update on canon.accounts, canon.api_keys, canon.usage_events, canon.stripe_events,
    canon.patients, canon.patient_keys, canon.documents to canon_app;
grant select, insert on canon.audit to canon_app;
grant usage on all sequences in schema canon to canon_app;

-- RLS is on for every table; tenancy is enforced by the app, so the API role sees all rows.
do $$
declare t text;
begin
    foreach t in array array['accounts', 'api_keys', 'usage_events', 'stripe_events',
                             'patients', 'patient_keys', 'documents', 'audit'] loop
        execute format('drop policy if exists canon_app_all on canon.%I', t);
        execute format('create policy canon_app_all on canon.%I for all to canon_app using (true) with check (true)', t);
    end loop;
end;
$$;
