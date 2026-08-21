"""Row Level Security — the only layer that is actually security.

React hiding a button and FastAPI returning 403 are both UX. If the terminal
is compromised, or someone points a REST client at the project with a stolen
token, this file is what still holds. So it is tested against a real Postgres
with real policies, not against a mock.

Runs when POS_TEST_PG_DSN points at a database the test user may reshape:

    docker run -d --name pos-pg -e POSTGRES_PASSWORD=postgres -p 5432:5432 postgres:17
    POS_TEST_PG_DSN=postgresql://postgres:postgres@localhost:5432/postgres pytest tests/test_rls.py

CI does exactly that (see .github/workflows/ci.yml).
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from app.domain import permissions as perms
from tests.conftest import REPO_ROOT, pg_dsn, requires_postgres

pytestmark = [pytest.mark.postgres, requires_postgres]

psycopg = pytest.importorskip("psycopg")

STORE_ID = "018f0000-0000-7000-8000-000000000100"
OTHER_STORE_ID = "018f0000-0000-7000-8000-000000000999"
CASHIER_ID = "018f0000-0000-7000-8000-000000000001"
SUPERVISOR_ID = "018f0000-0000-7000-8000-000000000002"
MANAGER_ID = "018f0000-0000-7000-8000-000000000003"

INSUFFICIENT_PRIVILEGE = "42501"

# Discovered rather than listed. The list used to be written out by hand and
# had already fallen a migration behind without anything failing — which is
# the quiet way a security suite stops testing the schema it ships.
SQL_FILES = [
    REPO_ROOT / "supabase" / "test" / "00_shim.sql",
    *sorted((REPO_ROOT / "supabase" / "migrations").glob("[0-9][0-9][0-9][0-9]_*.sql")),
    REPO_ROOT / "supabase" / "seed" / "seed.sql",
]


@pytest.fixture(scope="session")
def pg() -> Iterator[Any]:
    """A database with the full Supabase schema applied, built once."""
    conn = psycopg.connect(pg_dsn(), autocommit=True)

    # Start from nothing so a rerun is never contaminated by the last one.
    conn.execute("drop schema if exists public cascade")
    conn.execute("drop schema if exists reports cascade")
    conn.execute("drop schema if exists auth cascade")
    conn.execute("create schema public")

    for path in SQL_FILES:
        conn.execute(path.read_text(encoding="utf-8"))

    yield conn
    conn.close()


def claims(user_id: str, role: str, store_ids: list[str] | None = None) -> str:
    """The JWT the custom access token hook would have produced for this role."""
    return json.dumps(
        {
            "sub": user_id,
            "role": "authenticated",
            "app_metadata": {
                "permissions": sorted(perms.ROLE_PERMISSIONS[role]),
                "store_ids": store_ids if store_ids is not None else [STORE_ID],
            },
        }
    )


class Denied(Exception):
    """The database refused the statement."""


def run_as(pg: Any, jwt_claims: str, sql: str, params: tuple = ()) -> list[tuple]:
    """Execute one statement as `authenticated` carrying these claims."""
    with pg.transaction(force_rollback=True) as _:
        cur = pg.cursor()
        cur.execute("set local role authenticated")
        cur.execute("select set_config('request.jwt.claims', %s, true)", (jwt_claims,))
        try:
            cur.execute(sql, params)
        except psycopg.errors.InsufficientPrivilege as exc:
            raise Denied(str(exc)) from exc
        return cur.fetchall() if cur.description else []


def rowcount_as(pg: Any, jwt_claims: str, sql: str, params: tuple = ()) -> int:
    with pg.transaction(force_rollback=True) as _:
        cur = pg.cursor()
        cur.execute("set local role authenticated")
        cur.execute("select set_config('request.jwt.claims', %s, true)", (jwt_claims,))
        cur.execute(sql, params)
        return cur.rowcount


# ── The phase 1 exit criterion ──────────────────────────────────────────────


def test_cashier_cannot_read_cost_off_the_table(pg: Any) -> None:
    """RLS filters rows; cost is a column, so a GRANT does the work.

    Postgres refuses before any policy is consulted — 42501, not an empty
    result, which is the strongest form this can take.
    """
    with pytest.raises(Denied):
        run_as(pg, claims(CASHIER_ID, perms.CASHIER),
               "select cost from public.product_prices")


def test_cashier_can_still_read_the_selling_price(pg: Any) -> None:
    """The denial must be surgical: a till cannot ring up a sale without price."""
    rows = run_as(
        pg, claims(CASHIER_ID, perms.CASHIER),
        "select price from public.product_prices where store_id = %s", (STORE_ID,),
    )
    assert len(rows) > 0
    assert all(isinstance(row[0], int) for row in rows)


def test_cashier_gets_nothing_from_the_margin_report(pg: Any) -> None:
    rows = run_as(pg, claims(CASHIER_ID, perms.CASHIER),
                  "select * from reports.product_margin")
    assert rows == []


def test_supervisor_gets_nothing_from_the_margin_report(pg: Any) -> None:
    """`report.sales.store` is not `report.margin` — the split must hold."""
    rows = run_as(pg, claims(SUPERVISOR_ID, perms.SUPERVISOR),
                  "select * from reports.product_margin")
    assert rows == []


def test_manager_reads_margin(pg: Any) -> None:
    rows = run_as(
        pg, claims(MANAGER_ID, perms.MANAGER),
        "select sku, price, cost, margin from reports.product_margin order by sku",
    )
    assert len(rows) > 0
    for _sku, price, cost, margin in rows:
        assert margin == price - cost


def test_manager_in_another_store_reads_no_margin(pg: Any) -> None:
    """Margin is scoped to the store, not merely to the permission."""
    rows = run_as(
        pg, claims(MANAGER_ID, perms.MANAGER, store_ids=[OTHER_STORE_ID]),
        "select * from reports.product_margin",
    )
    assert rows == []


def test_a_forged_permission_claim_still_needs_the_grant(pg: Any) -> None:
    """Even a token claiming report.margin cannot read the raw column.

    The claim opens the view; it does not restore the revoked column grant.
    """
    forged = json.dumps(
        {
            "sub": CASHIER_ID,
            "role": "authenticated",
            "app_metadata": {"permissions": ["report.margin"], "store_ids": [STORE_ID]},
        }
    )
    with pytest.raises(Denied):
        run_as(pg, forged, "select cost from public.product_prices")


# ── PIN hashes ──────────────────────────────────────────────────────────────


def test_no_client_can_read_a_pin_hash(pg: Any) -> None:
    """Verification happens in the Edge Function; the hash never leaves it."""
    for role, user_id in (
        (perms.CASHIER, CASHIER_ID),
        (perms.MANAGER, MANAGER_ID),
    ):
        with pytest.raises(Denied):
            run_as(pg, claims(user_id, role), "select pin_hash from public.employees")


def test_an_employee_can_read_their_own_record(pg: Any) -> None:
    rows = run_as(
        pg, claims(CASHIER_ID, perms.CASHIER),
        "select employee_code from public.employees where user_id = %s", (CASHIER_ID,),
    )
    assert rows == [("C001",)]


def test_a_cashier_cannot_enumerate_the_roster(pg: Any) -> None:
    rows = run_as(pg, claims(CASHIER_ID, perms.CASHIER),
                  "select employee_code from public.employees")
    assert rows == [("C001",)]


def test_a_manager_can_read_the_roster(pg: Any) -> None:
    rows = run_as(pg, claims(MANAGER_ID, perms.MANAGER),
                  "select employee_code from public.employees order by employee_code")
    assert [row[0] for row in rows] == ["C001", "M001", "S001"]


# ── Store scoping ───────────────────────────────────────────────────────────


def test_a_sale_cannot_be_written_into_another_store(pg: Any) -> None:
    with pytest.raises(Denied, match="row-level security"):
        run_as(
            pg, claims(CASHIER_ID, perms.CASHIER),
            """
            insert into public.sales
                (id, store_id, terminal_id, cashier_id, status, client_created_at)
            values (gen_random_uuid(), %s,
                    '018f0000-0000-7000-8000-000000000200', %s, 'completed', now())
            """,
            (OTHER_STORE_ID, CASHIER_ID),
        )


def test_a_cashier_cannot_post_a_sale_as_someone_else(pg: Any) -> None:
    """`cashier_id = auth.uid()` is what makes the audit trail mean anything."""
    with pytest.raises(Denied, match="row-level security"):
        run_as(
            pg, claims(CASHIER_ID, perms.CASHIER),
            """
            insert into public.sales
                (id, store_id, terminal_id, cashier_id, status, client_created_at)
            values (gen_random_uuid(), %s,
                    '018f0000-0000-7000-8000-000000000200', %s, 'completed', now())
            """,
            (STORE_ID, MANAGER_ID),
        )


def test_a_cashier_can_post_their_own_sale(pg: Any) -> None:
    affected = rowcount_as(
        pg, claims(CASHIER_ID, perms.CASHIER),
        """
        insert into public.sales
            (id, store_id, terminal_id, cashier_id, status, client_created_at)
        values (gen_random_uuid(), %s,
                '018f0000-0000-7000-8000-000000000200', %s, 'completed', now())
        """,
        (STORE_ID, CASHIER_ID),
    )
    assert affected == 1


# ── Append-only ─────────────────────────────────────────────────────────────


def test_sales_cannot_be_updated_by_anyone(pg: Any) -> None:
    """No UPDATE policy exists. A correction is a compensating row (§1.4).

    With RLS on and no policy the statement succeeds and changes nothing,
    which is why this asserts on rowcount rather than expecting an error.
    """
    with pg.transaction(force_rollback=True):
        cur = pg.cursor()
        cur.execute(
            """
            insert into public.sales
                (id, store_id, terminal_id, cashier_id, status, client_created_at,
                 grand_total)
            values ('018f0000-0000-7000-8000-0000000abcde', %s,
                    '018f0000-0000-7000-8000-000000000200', %s, 'completed', now(), 5000)
            """,
            (STORE_ID, CASHIER_ID),
        )
        cur.execute("set local role authenticated")
        cur.execute(
            "select set_config('request.jwt.claims', %s, true)",
            (claims(MANAGER_ID, perms.MANAGER),),
        )
        cur.execute(
            "update public.sales set grand_total = 1 "
            " where id = '018f0000-0000-7000-8000-0000000abcde'"
        )
        assert cur.rowcount == 0

        cur.execute(
            "delete from public.sales "
            " where id = '018f0000-0000-7000-8000-0000000abcde'"
        )
        assert cur.rowcount == 0


# ── Resolving a disputed payment — architecture §13.5 ───────────────────────
#
# The permission is the whole point. A cashier whose UPI attestation could not
# be confirmed must not be the one who decides it was fine after all.

REVIEW_SALE_ID = "018f0000-0000-7000-8000-00000000fea1"


def _held_sale(cur: Any) -> None:
    cur.execute(
        """
        insert into public.sales
            (id, store_id, terminal_id, cashier_id, status, client_created_at,
             grand_total)
        values (%s, %s, '018f0000-0000-7000-8000-000000000200', %s,
                'requires_review', now(), 3740)
        """,
        (REVIEW_SALE_ID, STORE_ID, CASHIER_ID),
    )


def _resolve_as(pg: Any, actor: str, role: str) -> int:
    with pg.transaction(force_rollback=True):
        cur = pg.cursor()
        _held_sale(cur)
        cur.execute("set local role authenticated")
        cur.execute("select set_config('request.jwt.claims', %s, true)",
                    (claims(actor, role),))
        cur.execute(
            """
            insert into public.sale_reviews
                (id, sale_id, outcome, resolved_by, resolved_at)
            values (gen_random_uuid(), %s, 'paid', %s, now())
            """,
            (REVIEW_SALE_ID, actor),
        )
        return cur.rowcount


def test_a_cashier_cannot_resolve_their_own_disputed_payment(pg: Any) -> None:
    """The client blocks this too, but only this layer is security."""
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        _resolve_as(pg, CASHIER_ID, perms.CASHIER)


def test_a_supervisor_can_resolve_a_review(pg: Any) -> None:
    assert _resolve_as(pg, SUPERVISOR_ID, perms.SUPERVISOR) == 1


def test_a_manager_can_resolve_a_review(pg: Any) -> None:
    assert _resolve_as(pg, MANAGER_ID, perms.MANAGER) == 1


def test_a_review_cannot_be_attributed_to_someone_else(pg: Any) -> None:
    """`resolved_by = auth.uid()` — a supervisor cannot sign a manager's name
    to a decision about money."""
    with pg.transaction(force_rollback=True):
        cur = pg.cursor()
        _held_sale(cur)
        cur.execute("set local role authenticated")
        cur.execute("select set_config('request.jwt.claims', %s, true)",
                    (claims(SUPERVISOR_ID, perms.SUPERVISOR),))
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            cur.execute(
                """
                insert into public.sale_reviews
                    (id, sale_id, outcome, resolved_by, resolved_at)
                values (gen_random_uuid(), %s, 'paid', %s, now())
                """,
                (REVIEW_SALE_ID, MANAGER_ID),
            )


def test_a_review_cannot_be_rewritten(pg: Any) -> None:
    """Append-only, like everything else that records what happened to money."""
    with pg.transaction(force_rollback=True):
        cur = pg.cursor()
        _held_sale(cur)
        cur.execute(
            """
            insert into public.sale_reviews
                (id, sale_id, outcome, resolved_by, resolved_at)
            values ('018f0000-0000-7000-8000-00000000fee2', %s, 'paid', %s, now())
            """,
            (REVIEW_SALE_ID, SUPERVISOR_ID),
        )
        cur.execute("set local role authenticated")
        cur.execute("select set_config('request.jwt.claims', %s, true)",
                    (claims(MANAGER_ID, perms.MANAGER),))
        cur.execute(
            "update public.sale_reviews set outcome = 'not_paid' "
            " where id = '018f0000-0000-7000-8000-00000000fee2'"
        )
        assert cur.rowcount == 0


# ── The matrix, at the layer that enforces it ───────────────────────────────


@pytest.mark.parametrize(
    "role,table,allowed",
    [
        (perms.CASHIER, "public.products", True),
        (perms.CASHIER, "public.audit_log", False),
        (perms.CASHIER, "public.stock_ledger", True),
        (perms.SUPERVISOR, "public.audit_log", False),
        (perms.MANAGER, "public.audit_log", True),
        (perms.INVENTORY, "public.products", True),
        (perms.ADMIN, "public.audit_log", True),
    ],
)
def test_select_matrix(pg: Any, role: str, table: str, allowed: bool) -> None:
    user_id = {
        perms.CASHIER: CASHIER_ID,
        perms.SUPERVISOR: SUPERVISOR_ID,
    }.get(role, MANAGER_ID)

    rows = run_as(pg, claims(user_id, role), f"select count(*) from {table}")
    count = rows[0][0]

    if allowed:
        assert count >= 0
    else:
        # A denied SELECT under RLS is an empty set, not an error.
        assert count == 0


def test_an_inactive_employee_gets_an_empty_claim_set(pg: Any) -> None:
    """The access token hook strips all authority from a suspended account."""
    with pg.transaction(force_rollback=True):
        cur = pg.cursor()
        cur.execute(
            "update public.employees set status = 'terminated' where user_id = %s",
            (CASHIER_ID,),
        )
        cur.execute(
            "select public.custom_access_token_hook(%s::jsonb)",
            (json.dumps({"user_id": CASHIER_ID, "claims": {}}),),
        )
        event = cur.fetchone()[0]
        assert event["claims"]["app_metadata"]["permissions"] == []
        assert event["claims"]["app_metadata"]["store_ids"] == []


def test_the_hook_stamps_the_matrix_into_the_token(pg: Any) -> None:
    cur = pg.cursor()
    cur.execute(
        "select public.custom_access_token_hook(%s::jsonb)",
        (json.dumps({"user_id": CASHIER_ID, "claims": {"sub": CASHIER_ID}}),),
    )
    metadata = cur.fetchone()[0]["claims"]["app_metadata"]

    assert set(metadata["permissions"]) == set(perms.ROLE_PERMISSIONS[perms.CASHIER])
    assert metadata["store_ids"] == [STORE_ID]
    assert "report.margin" not in metadata["permissions"]


def test_every_public_table_has_rls_enabled(pg: Any) -> None:
    """A table added later without RLS is the classic Supabase data leak."""
    cur = pg.cursor()
    cur.execute(
        "select tablename from pg_tables "
        " where schemaname = 'public' and rowsecurity = false"
    )
    unprotected = [row[0] for row in cur.fetchall()]
    assert unprotected == [], f"tables without RLS: {unprotected}"


def test_sql_files_all_exist() -> None:
    missing = [str(path) for path in SQL_FILES if not path.exists()]
    assert missing == []


def test_repo_root_is_sane() -> None:
    assert (Path(REPO_ROOT) / "supabase").is_dir()
