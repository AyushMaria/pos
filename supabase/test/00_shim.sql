-- Local test shim.
--
-- Recreates just enough of Supabase's managed `auth` schema for the RLS suite
-- to run against a plain Postgres container in CI. It is NEVER applied to a
-- real project — the definitions here mirror Supabase's own so that a policy
-- which passes locally behaves identically in the cloud.
--
-- Applied by tests/conftest.py before supabase/migrations/*.sql.

create schema if not exists auth;

do $$
begin
    if not exists (select 1 from pg_roles where rolname = 'anon') then
        create role anon nologin noinherit;
    end if;
    if not exists (select 1 from pg_roles where rolname = 'authenticated') then
        create role authenticated nologin noinherit;
    end if;
    if not exists (select 1 from pg_roles where rolname = 'service_role') then
        create role service_role nologin noinherit bypassrls;
    end if;
    if not exists (select 1 from pg_roles where rolname = 'supabase_auth_admin') then
        create role supabase_auth_admin nologin noinherit;
    end if;
end
$$;

grant usage on schema public to anon, authenticated, service_role;

create table if not exists auth.users (
    id              uuid primary key default gen_random_uuid(),
    email           text unique,
    raw_app_meta_data jsonb not null default '{}'::jsonb,
    created_at      timestamptz not null default now()
);

-- Verbatim from Supabase: the claims arrive as a GUC set by PostgREST.
create or replace function auth.jwt()
returns jsonb
language sql
stable
as $$
  select coalesce(
    nullif(current_setting('request.jwt.claim',  true), ''),
    nullif(current_setting('request.jwt.claims', true), '')
  )::jsonb;
$$;

create or replace function auth.uid()
returns uuid
language sql
stable
as $$
  select coalesce(
    nullif(current_setting('request.jwt.claim.sub', true), ''),
    (nullif(current_setting('request.jwt.claims', true), '')::jsonb ->> 'sub')
  )::uuid;
$$;

grant usage on schema auth to anon, authenticated, service_role;
grant execute on function auth.jwt() to anon, authenticated, service_role;
grant execute on function auth.uid() to anon, authenticated, service_role;

-- Supabase grants the API roles table access by default and relies on RLS to
-- restrict it; the same must be true here or every policy would look like it
-- works simply because nothing was granted.
--
-- TRUNCATE is in this list deliberately, and was missing until 0020. Supabase
-- grants it, RLS cannot gate it, and its absence here meant the suite started
-- from a safer position than production — so a migration revoking it would
-- have looked like it changed nothing, and the grant itself was invisible.
-- A shim that is kinder than the real thing hides exactly the class of
-- finding it exists to catch.
alter default privileges in schema public
    grant select, insert, update, delete, truncate on tables to authenticated;
alter default privileges in schema public grant select on tables to anon;

-- Known remaining gap: the real project grants `anon` every table privilege,
-- not just select. Nothing in this system acts as `anon` and every policy is
-- `to authenticated`, so the difference does not change a policy outcome —
-- but it is a difference, and it belongs written down rather than discovered.
