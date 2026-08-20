-- 0004_access_token_hook — architecture §11.2.
--
-- Stamps `store_ids` and `permissions` into the JWT at login so that every RLS
-- policy is a claim lookup rather than a per-row join against
-- user_store_roles. With a 1-hour token TTL a revocation is live within the
-- hour, which is the trade being made for that speed.
--
-- Enable it in the dashboard: Authentication → Hooks → Customize Access Token
-- (JWT) Claims → public.custom_access_token_hook.

create or replace function public.custom_access_token_hook(event jsonb)
returns jsonb
language plpgsql
stable
set search_path = public
as $$
declare
    claims       jsonb;
    user_uuid    uuid;
    perms        jsonb;
    store_ids    jsonb;
    emp_status   text;
begin
    user_uuid := (event ->> 'user_id')::uuid;

    select status into emp_status
      from public.employees
     where user_id = user_uuid;

    -- A suspended or terminated employee gets a valid token carrying no
    -- authority at all. Every policy then denies them, and the terminal's
    -- next sync purges the offline snapshot (architecture §11.4).
    if emp_status is distinct from 'active' then
        perms     := '[]'::jsonb;
        store_ids := '[]'::jsonb;
    else
        select coalesce(jsonb_agg(distinct rp.permission_key), '[]'::jsonb)
          into perms
          from public.user_store_roles usr
          join public.role_permissions rp on rp.role_key = usr.role_key
         where usr.user_id = user_uuid;

        select coalesce(jsonb_agg(distinct usr.store_id::text), '[]'::jsonb)
          into store_ids
          from public.user_store_roles usr
         where usr.user_id = user_uuid;
    end if;

    claims := coalesce(event -> 'claims', '{}'::jsonb);
    if claims -> 'app_metadata' is null then
        claims := jsonb_set(claims, '{app_metadata}', '{}'::jsonb);
    end if;

    claims := jsonb_set(claims, '{app_metadata,permissions}', perms);
    claims := jsonb_set(claims, '{app_metadata,store_ids}', store_ids);

    return jsonb_set(event, '{claims}', claims);
end;
$$;

-- The hook runs as supabase_auth_admin and must be unreachable by clients:
-- a client that could call it could not change its own claims, but it could
-- enumerate the roster.
grant usage on schema public to supabase_auth_admin;
grant execute on function public.custom_access_token_hook(jsonb)
    to supabase_auth_admin;
revoke execute on function public.custom_access_token_hook(jsonb)
    from authenticated, anon, public;

grant select on public.employees, public.user_store_roles, public.role_permissions
    to supabase_auth_admin;

-- supabase_auth_admin reads these tables from inside the hook, where no JWT
-- exists yet, so it needs to bypass RLS on them.
create policy auth_admin_reads_employees on public.employees
    for select to supabase_auth_admin using (true);
create policy auth_admin_reads_user_store_roles on public.user_store_roles
    for select to supabase_auth_admin using (true);
create policy auth_admin_reads_role_permissions on public.role_permissions
    for select to supabase_auth_admin using (true);
