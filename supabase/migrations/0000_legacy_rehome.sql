-- 0000_legacy_rehome — make room for the POS schema in a project that already
-- holds the legacy billing tables.
--
-- The POS schema owns `public.products`. The legacy billing app owns a table
-- of the same name with an entirely different shape (`item_name`,
-- `upc_ean_code`, `main_group`, a float `mrp`). Two tables cannot share one
-- name, so before 0001_core runs the legacy pair moves into its own schema.
--
-- Moving rather than dropping, because that catalogue is the only copy of
-- 32,165 real products and their barcodes, and the catalogue migration in
-- `scripts/migrate_legacy_catalog.sql` reads it in place — same database, no
-- export, no network round trip.
--
-- Two properties matter:
--
--   * **A no-op on a fresh project.** At migration 0000 nothing of ours exists
--     yet, so a `public.products` found here can only be the legacy one. On a
--     clean project there is nothing to move and this file does nothing.
--
--   * **Out of the API's reach.** PostgREST exposes `public` (and
--     `graphql_public`). A table in `legacy` is not reachable with the anon
--     key at all, which is the right resting place for a retired system that
--     was never written with this project's RLS in mind.
--
-- Indexes, constraints, owned sequences and any RLS policies travel with the
-- table. Nothing about the data changes.
--
-- The legacy `deduct_stock(bigint, integer)` moves too, and it matters more
-- than it looks. A function body is stored as text and is *not* rewritten
-- when a table it names moves, so a `deduct_stock` left in `public` would go
-- on referring to `public.products` — which by the end of 0001 is the POS
-- table, keyed by uuid, with no `quantity` column at all. It would error
-- rather than corrupt anything, but a retired function aimed at a live table
-- is not a thing to leave lying about.

create schema if not exists legacy;

comment on schema legacy is
    'Retired billing system, moved aside by 0000 so the POS schema can own '
    'public. Read by scripts/migrate_legacy_catalog.sql. Not API-exposed.';

do $$
declare
    candidate text;
    moved     text[] := '{}';
begin
    foreach candidate in array array['products', 'orders']
    loop
        if to_regclass(format('public.%I', candidate)) is not null then
            execute format('alter table public.%I set schema legacy', candidate);
            moved := moved || candidate;
        end if;
    end loop;

    if cardinality(moved) = 0 then
        raise notice '0000_legacy_rehome: nothing to move (fresh project)';
    else
        raise notice '0000_legacy_rehome: moved % to schema legacy', moved;
    end if;
end
$$;

-- Routines, by signature. At migration 0000 nothing of ours exists yet, so
-- anything answering to these names can only belong to the retired system.
do $$
declare
    candidate text;
begin
    foreach candidate in array array['public.deduct_stock(bigint, integer)']
    loop
        if to_regprocedure(candidate) is not null then
            execute format('alter routine %s set schema legacy', candidate);
            raise notice '0000_legacy_rehome: moved routine %', candidate;
        end if;
    end loop;
end
$$;

-- Belt and braces. The schema is not exposed, but an explicit revoke means a
-- future decision to expose it cannot quietly take these tables along.
revoke all on all tables   in schema legacy from anon, authenticated;
revoke all on all routines in schema legacy from anon, authenticated;
revoke usage on schema legacy from anon, authenticated;
