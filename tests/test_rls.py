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
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from app.domain import permissions as perms
from app.domain.unlisted import UNLISTED_PRODUCT_ID
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


def test_a_sale_may_be_posted_on_behalf_of_another_cashier(pg: Any) -> None:
    """A guarantee 0009 gave up on purpose. Read the reasoning before restoring it.

    This test used to assert the opposite, on the grounds that
    `cashier_id = auth.uid()` is what makes the audit trail mean anything. It
    is a fair principle and it was unenforceable here.

    A terminal takes sales offline all day, often across a shift change, and
    pushes the backlog under whichever session is signed in when the network
    returns. `sync_push` sends up to 200 envelopes as one transaction, so a
    single sale rung up by anybody else aborted the whole batch. Found on a
    real till: four queued sales, one by C001 and three by M001. As M001,
    C001's row was refused and took the other three with it; as C001, the
    reverse. Neither cashier could drain the queue, and because the pusher
    treats 403 as transient it retried forever and never quarantined, so
    /sync/failures — the one place built to show a refusal — stayed empty.

    What still holds is below and in the tests either side of this one: the
    caller must belong to the store and hold `sale.create`. What is given up
    is attribution, which the terminal composes anyway — it is trusted to
    report who was at the counter exactly as it is trusted to report the
    total.

    The real repair is a per-terminal identity, so a push authenticates as the
    machine and `cashier_id` goes back to being pure provenance. That arrives
    with device registration in phase 9. Until then this is the trade, and it
    is written down here rather than left as an absence.
    """
    affected = rowcount_as(
        pg, claims(CASHIER_ID, perms.CASHIER),
        """
        insert into public.sales
            (id, store_id, terminal_id, cashier_id, status, client_created_at)
        values (gen_random_uuid(), %s,
                '018f0000-0000-7000-8000-000000000200', %s, 'completed', now())
        """,
        (STORE_ID, MANAGER_ID),
    )

    assert affected == 1


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


# ── sync_push, against a real Postgres — architecture §9.2 ──────────────────
#
# The idempotency claim in the pusher's tests rests on `ON CONFLICT DO
# NOTHING`, which a fake server can only pretend to have. These assert it
# where it actually lives.

TERMINAL_UUID = "018f0000-0000-7000-8000-000000000200"
PRODUCT_ID = "018f0000-0000-7000-8000-000000001002"
UNCODED_PRODUCT_ID = "019400aa-0000-7000-8000-0000000000a1"


def _as(pg: Any, role: str, sql: str, params: tuple = ()) -> list[tuple]:
    """Run one statement as a role, carrying the claims that role would."""
    cur = pg.cursor()
    cur.execute("set local role authenticated")
    cur.execute(
        "select set_config('request.jwt.claims', %s, true)",
        (claims(CASHIER_ID, role),),
    )
    cur.execute(sql, params)
    return list(cur.fetchall())


def _sale_envelope(sale_id: str, *, schema_version: int = 3) -> str:
    return json.dumps(
        [
            {
                "schema_version": schema_version,
                "entity": "sale",
                "op": "insert",
                "id": sale_id,
                "client_seq": 1,
                "data": {
                    "id": sale_id,
                    "store_id": STORE_ID,
                    "terminal_id": TERMINAL_UUID,
                    "session_id": None,
                    "receipt_no": f"ST01-T1-{sale_id[-6:]}",
                    "cashier_id": CASHIER_ID,
                    "type": "sale",
                    "status": "completed",
                    "subtotal": 3740,
                    "discount_total": 0,
                    "tax_total": 570,
                    "rounding_adjustment": -40,
                    "grand_total": 3700,
                    "original_sale_id": None,
                    "client_created_at": "2026-08-21T10:00:00+00:00",
                    "lines": [
                        {
                            "id": f"018f0000-0000-7000-8000-0000000{sale_id[-5:]}",
                            "sale_id": sale_id,
                            "line_no": 1,
                            "product_id": PRODUCT_ID,
                            "description": "Milk 1L",
                            "qty_milli": 1000,
                            "unit_price": 3740,
                            "discount_amount": 0,
                            "tax_amount": 570,
                            "line_total": 3740,
                            "tax_code": "GST18",
                            "tax_rate_bp": 1800,
                        }
                    ],
                    "stock_ledger": [
                        {
                            "id": f"018f0000-0000-7000-8000-0000001{sale_id[-5:]}",
                            "store_id": STORE_ID,
                            "product_id": PRODUCT_ID,
                            "delta_milli": -1000,
                            "reason": "sale",
                            "ref_type": "sale",
                            "ref_id": sale_id,
                            "occurred_at": "2026-08-21T10:00:00+00:00",
                            "terminal_id": TERMINAL_UUID,
                            "user_id": CASHIER_ID,
                        }
                    ],
                },
            }
        ]
    )


def _push(pg: Any, envelope: str, actor: str = CASHIER_ID, role: str = perms.CASHIER):
    cur = pg.cursor()
    cur.execute("set local role authenticated")
    cur.execute("select set_config('request.jwt.claims', %s, true)",
                (claims(actor, role),))
    cur.execute("select public.sync_push(%s::jsonb)", (envelope,))
    return cur


def test_a_pushed_sale_lands_whole(pg: Any) -> None:
    sale_id = "018f0000-0000-7000-8000-00000000a001"
    with pg.transaction(force_rollback=True):
        _push(pg, _sale_envelope(sale_id))

        cur = pg.cursor()
        cur.execute("select count(*) from public.sales where id = %s", (sale_id,))
        assert cur.fetchone()[0] == 1
        cur.execute("select count(*) from public.sale_lines where sale_id = %s", (sale_id,))
        assert cur.fetchone()[0] == 1
        cur.execute("select count(*) from public.stock_ledger where ref_id = %s", (sale_id,))
        assert cur.fetchone()[0] == 1


def test_pushing_the_same_sale_twice_changes_nothing(pg: Any) -> None:
    """The claim the whole sync design rests on. A dropped acknowledgement
    makes the terminal re-send; the re-send must not be a second sale."""
    sale_id = "018f0000-0000-7000-8000-00000000a002"
    with pg.transaction(force_rollback=True):
        _push(pg, _sale_envelope(sale_id))
        _push(pg, _sale_envelope(sale_id))

        cur = pg.cursor()
        cur.execute("select count(*) from public.sales where id = %s", (sale_id,))
        assert cur.fetchone()[0] == 1
        cur.execute("select count(*) from public.sale_lines where sale_id = %s", (sale_id,))
        assert cur.fetchone()[0] == 1
        cur.execute(
            "select coalesce(sum(delta_milli), 0) from public.stock_ledger "
            " where ref_id = %s",
            (sale_id,),
        )
        # The one that would hurt: stock counted twice for a sale made once.
        assert cur.fetchone()[0] == -1000


def test_the_ledger_maintains_stock_levels(pg: Any) -> None:
    """Deltas in, running total out — the terminal never sends a level (§9.4)."""
    sale_id = "018f0000-0000-7000-8000-00000000a003"
    with pg.transaction(force_rollback=True):
        cur = pg.cursor()
        cur.execute(
            "select coalesce(on_hand, 0) from public.stock_levels "
            " where store_id = %s and product_id = %s",
            (STORE_ID, PRODUCT_ID),
        )
        row = cur.fetchone()
        before = row[0] if row else 0

        _push(pg, _sale_envelope(sale_id))

        cur.execute(
            "select on_hand from public.stock_levels "
            " where store_id = %s and product_id = %s",
            (STORE_ID, PRODUCT_ID),
        )
        assert cur.fetchone()[0] == before - 1000


def test_an_internal_code_is_the_one_the_till_will_read(pg: Any) -> None:
    """The 21 format is defined in `app/domain/barcode.py` and assigned in SQL.

    Two implementations of one format, which is a place to drift. A code the
    cloud hands out that the parser refuses is a product nobody can sell, and
    nothing would notice until somebody scanned a shelf label at a counter.
    """
    from app.domain.barcode import internal_barcode, parse

    with pg.transaction(force_rollback=True):
        cur = pg.cursor()
        cur.execute("select nextval('pos.internal_item_code_seq')")
        (item_code,) = cur.fetchone()
        cur.execute(
            "select setval('pos.internal_item_code_seq', %s, false)", (item_code,)
        )
        cur.execute("select pos.next_internal_barcode()")
        (generated,) = cur.fetchone()

        assert generated == internal_barcode(item_code), (
            "SQL and the domain disagree about the 21 format"
        )

        scan = parse(generated)
        assert scan.symbology == "INTERNAL"
        assert scan.lookup_key == generated
        assert not scan.carries_quantity, "21 is identity; 22 is a weighing"


def test_assigning_a_code_needs_a_catalogue_permission(pg: Any) -> None:
    """`assign_internal_barcode` is `security invoker`, so its insert is
    checked by `product_barcodes_write` exactly as a direct write would be.

    Only the sequence is privileged, and a sequence can write nothing. A
    cashier calling this burns one code out of ten billion and is refused.
    """
    with pg.transaction(force_rollback=True):
        pg.cursor().execute(
            "insert into public.products (id, sku, name, tax_code) "
            "values (%s, 'SKU-UNCODED', 'Uncoded thing', 'GST18')",
            (UNCODED_PRODUCT_ID,),
        )

        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            _as(
                pg,
                perms.CASHIER,
                "select public.assign_internal_barcode(%s)",
                (UNCODED_PRODUCT_ID,),
            )


def test_a_manager_can_assign_a_code(pg: Any) -> None:
    with pg.transaction(force_rollback=True):
        pg.cursor().execute(
            "insert into public.products (id, sku, name, tax_code) "
            "values (%s, 'SKU-UNCODED-2', 'Another uncoded', 'GST18')",
            (UNCODED_PRODUCT_ID,),
        )

        rows = _as(
            pg,
            perms.MANAGER,
            "select public.assign_internal_barcode(%s)",
            (UNCODED_PRODUCT_ID,),
        )

        assert rows[0][0].startswith("21")


def test_a_received_delivery_reconciles(pg: Any) -> None:
    """Phase 6's exit criterion, where the trigger that decides it lives.

    `stock_levels` is derived from `stock_ledger` by `stock_ledger_apply`, so
    the sum of a product's deltas must equal its level — for every product,
    always. 0008 shipped a second trigger over the same function and every
    sale moved stock twice; this is the general form of the check that caught
    it, and the reason phase 6 could safely add three more ways of writing to
    that ledger.

    Deliberately not a terminal-side test. The terminal has no such trigger:
    its level is a pulled cache and its ledger is what this till did, so the
    gap between them is the queue rather than a fault.
    """
    movement_id = "019300aa-0000-7000-8000-00000000e001"
    envelope = json.dumps(
        [
            {
                "schema_version": 3,
                "entity": "stock_movement",
                "op": "insert",
                "id": movement_id,
                "client_seq": 1,
                "data": {
                    "id": movement_id,
                    "store_id": STORE_ID,
                    "product_id": PRODUCT_ID,
                    "delta_milli": 24_000,
                    "reason": "receipt",
                    "ref_type": "receipt",
                    "occurred_at": "2026-08-24T09:00:00+00:00",
                    "terminal_id": TERMINAL_UUID,
                    "user_id": CASHIER_ID,
                },
            }
        ]
    )

    with pg.transaction(force_rollback=True):
        _push(pg, envelope, role=perms.INVENTORY)

        cur = pg.cursor()
        cur.execute(
            """
            SELECT COALESCE(s.on_hand, 0), COALESCE(SUM(l.delta_milli), 0)
              FROM public.stock_ledger l
              LEFT JOIN public.stock_levels s
                     ON s.product_id = l.product_id AND s.store_id = l.store_id
             WHERE l.product_id = %s
             GROUP BY s.on_hand
            """,
            (PRODUCT_ID,),
        )
        level, ledger = cur.fetchone()

        assert ledger == 24_000, "the delivery did not land whole"
        assert level == ledger, f"level {level} disagrees with ledger {ledger}"


def test_a_stock_movement_needs_a_stock_permission(pg: Any) -> None:
    """`sale.create` is not enough to receive a delivery. Selling and
    restocking are different acts of trust and the matrix separates them."""
    movement_id = "019300aa-0000-7000-8000-00000000e002"
    envelope = json.dumps(
        [
            {
                "schema_version": 3,
                "entity": "stock_movement",
                "op": "insert",
                "id": movement_id,
                "client_seq": 1,
                "data": {
                    "id": movement_id,
                    "store_id": STORE_ID,
                    "product_id": PRODUCT_ID,
                    "delta_milli": 1_000,
                    "reason": "receipt",
                    "ref_type": "receipt",
                    "occurred_at": "2026-08-24T09:00:00+00:00",
                    "terminal_id": TERMINAL_UUID,
                    "user_id": CASHIER_ID,
                },
            }
        ]
    )

    with (
        pytest.raises(psycopg.errors.InsufficientPrivilege),
        pg.transaction(force_rollback=True),
    ):
        _push(pg, envelope, role=perms.CASHIER)


def test_sync_push_is_not_a_way_around_rls(pg: Any) -> None:
    """`security invoker`, so the RPC is exactly as privileged as its caller.

    A `security definer` here would be a hole straight through the only real
    trust boundary in the system.

    The probe is a store the caller does not belong to. It used to be a sale
    attributed to another cashier, which 0009 deliberately stopped refusing —
    see test_a_sale_may_be_posted_on_behalf_of_another_cashier. Store scoping
    is the guarantee that survived, and it is the one worth probing: a push
    that could cross stores would let any till read and write every shop's
    takings.
    """
    sale_id = "018f0000-0000-7000-8000-00000000a004"
    envelope = json.loads(_sale_envelope(sale_id))
    envelope[0]["data"]["store_id"] = OTHER_STORE_ID  # not the caller's store

    with (
        pytest.raises(psycopg.errors.InsufficientPrivilege),
        pg.transaction(force_rollback=True),
    ):
        _push(pg, json.dumps(envelope))


def test_sync_push_still_needs_the_permission(pg: Any) -> None:
    """The other half of what 0009 left standing.

    Dropping the actor check left `in_store` and `sale.create`. If the
    permission were not enforced, 0009 would have removed the policy rather
    than narrowed it.
    """
    sale_id = "018f0000-0000-7000-8000-00000000a006"

    # `inventory` is signed in and in the store, and holds no `sale.create`.
    with (
        pytest.raises(psycopg.errors.InsufficientPrivilege),
        pg.transaction(force_rollback=True),
    ):
        _push(pg, _sale_envelope(sale_id), role=perms.INVENTORY)


def test_an_outdated_terminal_is_refused_by_name(pg: Any) -> None:
    """Architecture §17. The terminal is told to update, not told its data is
    invalid — those call for completely different actions."""
    sale_id = "018f0000-0000-7000-8000-00000000a005"
    with (
        pytest.raises(psycopg.errors.RaiseException) as refused,
        pg.transaction(force_rollback=True),
    ):
        _push(pg, _sale_envelope(sale_id, schema_version=2))

    assert "outdated_terminal" in str(refused.value)


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


# ── Items the catalogue does not have — phase 6, slice 5 ────────────────────


def test_the_placeholder_product_can_hold_a_sale_line(pg: Any) -> None:
    """The claim the whole design rests on, and only Postgres can settle it.

    `sale_lines.product_id` is `not null references public.products(id)`. The
    obvious design — invent a local product row and point the line at it —
    fails that key, and it fails *after* the customer has paid: the sale is
    taken, printed, and then quarantined hours later by the pusher. So the
    line points at one placeholder that 0014 puts in the catalogue, and this
    proves the reference actually holds.
    """
    sale_id = "018f0000-0000-7000-8000-00000000b101"
    envelope = json.dumps(
        [
            {
                "schema_version": 3,
                "entity": "sale",
                "op": "insert",
                "id": sale_id,
                "client_seq": 1,
                "data": {
                    "id": sale_id,
                    "store_id": STORE_ID,
                    "terminal_id": TERMINAL_UUID,
                    "session_id": None,
                    "receipt_no": "ST01-T1-000401",
                    "cashier_id": CASHIER_ID,
                    "type": "sale",
                    "status": "completed",
                    "subtotal": 19500,
                    "discount_total": 0,
                    "tax_total": 2089,
                    "rounding_adjustment": 0,
                    "grand_total": 19500,
                    "original_sale_id": None,
                    "client_created_at": "2026-08-24T09:00:00+00:00",
                    "lines": [
                        {
                            "id": "018f0000-0000-7000-8000-00000000b102",
                            "sale_id": sale_id,
                            "line_no": 1,
                            "product_id": UNLISTED_PRODUCT_ID,
                            # The item's real identity, carried on the line.
                            "description": "Kissan Mixed Fruit Jam 500g",
                            "barcode_scanned": "8901999000014",
                            "qty_milli": 1000,
                            "unit_price": 19500,
                            "discount_amount": 0,
                            "tax_amount": 2089,
                            "line_total": 19500,
                            "tax_code": "GST12",
                            "tax_rate_bp": 1200,
                        }
                    ],
                    # No stock ledger: the placeholder does not track stock,
                    # and it could not meaningfully - one row stands in for
                    # many unrelated products.
                    "stock_ledger": [],
                },
            }
        ]
    )

    with pg.transaction(force_rollback=True):
        _push(pg, envelope)

        cur = pg.cursor()
        cur.execute(
            "select description, barcode_scanned from public.sale_lines "
            "where sale_id = %s",
            (sale_id,),
        )
        assert cur.fetchall() == [
            ("Kissan Mixed Fruit Jam 500g", "8901999000014")
        ]


def test_the_placeholder_does_not_accumulate_stock(pg: Any) -> None:
    """One row standing in for many unrelated products, so a stock level on it
    would be the sum of things with nothing to do with each other."""
    cur = pg.cursor()
    cur.execute(
        "select track_stock from public.products where id = %s",
        (UNLISTED_PRODUCT_ID,),
    )
    assert cur.fetchone() == (False,)


def test_the_placeholder_cannot_be_given_a_barcode(pg: Any) -> None:
    """`assign_internal_codes.sql` gave it one on the first live run.

    A code makes a product scannable. The placeholder stands in for many real
    products, so a scan of it is meaningless at best and - the day somebody
    gives it a price - a ringable line reading "Unlisted item" at whatever
    that price is, bypassing the whole design. The script now skips it; this
    is the fence for when somebody re-runs an older copy.
    """
    with (
        pytest.raises(psycopg.errors.RaiseException),
        pg.transaction(force_rollback=True),
    ):
        cur = pg.cursor()
        cur.execute(
            "insert into public.product_barcodes "
            "(product_id, barcode, symbology, pack_size, is_primary) "
            "values (%s, '2100000118670', 'INTERNAL', 1, true)",
            (UNLISTED_PRODUCT_ID,),
        )


def test_the_sql_and_python_agree_on_the_placeholder(pg: Any) -> None:
    """Two copies of one UUID, in two languages. This is the seam."""
    cur = pg.cursor()
    cur.execute("select pos.unlisted_product()::text")
    assert cur.fetchone()[0] == UNLISTED_PRODUCT_ID


def test_a_cashier_may_file_a_code_that_matched_nothing(pg: Any) -> None:
    """`sale.create`, not `product.edit`.

    The cashier who met the unknown item is the only person who can record it,
    and they are holding up a queue. Requiring a manager here means the item
    gets rung up as something else at roughly the right price instead — wrong
    stock, wrong tax, and no record that anything was ever missing.
    """
    scan_id = "019300aa-0000-7000-8000-00000000f001"
    envelope = json.dumps(
        [
            {
                "schema_version": 3,
                "entity": "unknown_scan",
                "op": "insert",
                "id": scan_id,
                "client_seq": 1,
                "data": {
                    "id": scan_id,
                    "store_id": STORE_ID,
                    "barcode": "8901999000014",
                    "scanned_at": "2026-08-24T09:00:00+00:00",
                    "terminal_id": TERMINAL_UUID,
                    "resolved": False,
                },
            }
        ]
    )

    with pg.transaction(force_rollback=True):
        _push(pg, envelope)

        cur = pg.cursor()
        cur.execute(
            "select barcode, resolved from public.unknown_scans where id = %s",
            (scan_id,),
        )
        assert cur.fetchone() == ("8901999000014", False)


def test_an_unknown_scan_stays_inside_its_store(pg: Any) -> None:
    """A till cannot file a miss against a shop it does not work in. The same
    fence every other entity sits behind, checked here because `sync_push` is
    `security invoker` and would otherwise be the way around it."""
    scan_id = "019300aa-0000-7000-8000-00000000f002"
    envelope = json.dumps(
        [
            {
                "schema_version": 3,
                "entity": "unknown_scan",
                "op": "insert",
                "id": scan_id,
                "client_seq": 1,
                "data": {
                    "id": scan_id,
                    "store_id": "018f0000-0000-7000-8000-0000000009ff",
                    "barcode": "8901999000014",
                    "scanned_at": "2026-08-24T09:00:00+00:00",
                    "terminal_id": TERMINAL_UUID,
                    "resolved": False,
                },
            }
        ]
    )

    with (
        pytest.raises(psycopg.errors.Error),
        pg.transaction(force_rollback=True),
    ):
        _push(pg, envelope)


def test_repo_root_is_sane() -> None:
    assert (Path(REPO_ROOT) / "supabase").is_dir()


# ── Slice 6: closing the queue, and editing the catalogue ──────────────────
#
# Everything below tests a policy that did not exist before 0016/0017. The
# shape of the bug they prevent is always the same: with RLS on, a missing
# policy is not an error. The statement succeeds, touches nothing, and the
# screen above it looks like it worked. So these assert on rowcount, not on
# an exception.

SCAN_ID = "019400bb-0000-7000-8000-0000000000e1"


def _an_unknown_scan(cur: Any, store_id: str = STORE_ID) -> None:
    cur.execute(
        """
        insert into public.unknown_scans
            (id, store_id, barcode, scanned_at, terminal_id, resolved)
        values (%s, %s, '8906110944741', now(), %s, false)
        """,
        (SCAN_ID, store_id, TERMINAL_UUID),
    )


def test_a_manager_may_close_an_unknown_scan(pg: Any) -> None:
    """The policy 0016 adds, and the reason it had to be added.

    Before it, this update matched zero rows for everybody — including the
    only person the queue screen is built for.
    """
    with pg.transaction(force_rollback=True):
        cur = pg.cursor()
        _an_unknown_scan(cur)
        cur.execute("set local role authenticated")
        cur.execute(
            "select set_config('request.jwt.claims', %s, true)",
            (claims(MANAGER_ID, perms.MANAGER),),
        )
        cur.execute(
            "update public.unknown_scans "
            "   set resolved = true, resolution = 'dismissed' where id = %s",
            (SCAN_ID,),
        )
        assert cur.rowcount == 1


def test_a_cashier_cannot_close_an_unknown_scan(pg: Any) -> None:
    """Files them, does not close them. Working the queue is catalogue work,
    which is why the plan moved it out of slice 5."""
    with pg.transaction(force_rollback=True):
        cur = pg.cursor()
        _an_unknown_scan(cur)
        cur.execute("set local role authenticated")
        cur.execute(
            "select set_config('request.jwt.claims', %s, true)",
            (claims(CASHIER_ID, perms.CASHIER),),
        )
        cur.execute(
            "update public.unknown_scans "
            "   set resolved = true, resolution = 'dismissed' where id = %s",
            (SCAN_ID,),
        )
        # Zero, and no exception: RLS filters the row out before 0021's
        # trigger ever sees it, so the refusal that lands is the policy's.
        assert cur.rowcount == 0


def test_an_unknown_scan_in_another_store_cannot_be_closed(pg: Any) -> None:
    with pg.transaction(force_rollback=True):
        cur = pg.cursor()
        cur.execute(
            "insert into public.stores (id, code, name) values (%s, 'OTHER', 'Other') "
            "on conflict (id) do nothing",
            (OTHER_STORE_ID,),
        )
        _an_unknown_scan(cur, store_id=OTHER_STORE_ID)
        cur.execute("set local role authenticated")
        cur.execute(
            "select set_config('request.jwt.claims', %s, true)",
            (claims(MANAGER_ID, perms.MANAGER),),
        )
        cur.execute(
            "update public.unknown_scans "
            "   set resolved = true, resolution = 'dismissed' where id = %s",
            (SCAN_ID,),
        )
        # Zero, and no exception: RLS filters the row out before 0021's
        # trigger ever sees it, so the refusal that lands is the policy's.
        assert cur.rowcount == 0


def test_a_scan_records_what_was_scanned(pg: Any) -> None:
    """0016's trigger. An RLS policy cannot compare old to new, so `with
    check` alone would have let a resolve quietly rewrite the barcode."""
    with pg.transaction(force_rollback=True):
        cur = pg.cursor()
        _an_unknown_scan(cur)
        cur.execute("set local role authenticated")
        cur.execute(
            "select set_config('request.jwt.claims', %s, true)",
            (claims(MANAGER_ID, perms.MANAGER),),
        )
        with pytest.raises(psycopg.Error, match="only how it was closed"):
            cur.execute(
                "update public.unknown_scans set resolved = true, barcode = '1' "
                "where id = %s",
                (SCAN_ID,),
            )


# ── 0021: a close that says what it was ───────────────────────────────────
#
# The bug these exist for did not raise, did not 403 and did not return an
# empty result. The queue's third button set `resolved` and nothing else, so
# the entry closed, the catalogue stayed as it was, and the screen said
# "Nothing waiting. Every scan found a product." One code went round that
# loop five times.
#
# The screen has been changed too, but a screen is not a boundary. These
# assert the part that cannot be clicked past.


def test_closing_a_scan_has_to_say_how(pg: Any) -> None:
    """The old statement, exactly as the old button sent it."""
    with pg.transaction(force_rollback=True):
        cur = pg.cursor()
        _an_unknown_scan(cur)
        _as_manager(cur)
        with pytest.raises(psycopg.Error, match="closing a scan has to say how"):
            cur.execute(
                "update public.unknown_scans set resolved = true where id = %s",
                (SCAN_ID,),
            )


def test_a_scan_cannot_be_called_catalogued_while_the_code_is_on_nothing(
    pg: Any,
) -> None:
    """The claim is checked rather than believed.

    This is the one refusal in the whole feature that had to live in the
    database. A client that skips the barcode and posts the resolve is not a
    hypothetical: it is what the product itself did for a fortnight.
    """
    with pg.transaction(force_rollback=True):
        cur = pg.cursor()
        _an_unknown_scan(cur)
        _as_manager(cur)
        with pytest.raises(psycopg.Error, match="is not on any product yet"):
            cur.execute(
                "update public.unknown_scans "
                "   set resolved = true, resolution = 'catalogued' where id = %s",
                (SCAN_ID,),
            )


def test_cataloguing_is_allowed_the_moment_the_code_is_attached(pg: Any) -> None:
    """And in that order, which is the order the screen uses: the barcode
    lands first, then the entry closes. A resolve is a statement about the
    catalogue, so it can only be true after the catalogue has changed."""
    with pg.transaction(force_rollback=True):
        cur = pg.cursor()
        _an_unknown_scan(cur)
        _as_manager(cur)
        cur.execute(
            "insert into public.product_barcodes "
            "(product_id, barcode, symbology, pack_size, is_primary) "
            "values (%s, '8906110944741', 'EAN13', 1, false)",
            (PRODUCT_ID,),
        )
        cur.execute(
            "update public.unknown_scans "
            "   set resolved = true, resolution = 'catalogued' where id = %s",
            (SCAN_ID,),
        )
        assert cur.rowcount == 1

        # The action name 0019 already used, kept so the rows written before
        # today stay comparable with the ones written after it.
        assert len(_audit(cur, "scan.resolved", SCAN_ID)) == 1


def test_giving_up_on_a_scan_is_recorded_as_giving_up(pg: Any) -> None:
    """The distinction 0021 exists to make.

    Before it, this row and the one above were the same row, and no report
    could count how much of the queue had actually been catalogued. A torn
    label is a fine reason to dismiss an entry; five of them against the same
    biscuit packet is a shop with a hole in its catalogue.
    """
    with pg.transaction(force_rollback=True):
        cur = pg.cursor()
        _an_unknown_scan(cur)
        _as_manager(cur)
        cur.execute(
            "update public.unknown_scans "
            "   set resolved = true, resolution = 'dismissed' where id = %s",
            (SCAN_ID,),
        )
        assert cur.rowcount == 1

        rows = _audit(cur, "scan.dismissed", SCAN_ID)
        assert len(rows) == 1
        assert str(rows[0][0]) == MANAGER_ID
        assert not _audit(cur, "scan.resolved", SCAN_ID)


def test_an_outcome_cannot_be_hung_on_an_open_scan(pg: Any) -> None:
    """An entry that is waiting has not been catalogued or dismissed,
    whatever a column says. Refused rather than quietly ignored: a write that
    disappears is the failure this whole migration is about."""
    with pg.transaction(force_rollback=True):
        cur = pg.cursor()
        _an_unknown_scan(cur)
        _as_manager(cur)
        with pytest.raises(psycopg.Error, match="still open has no outcome"):
            cur.execute(
                "update public.unknown_scans set resolution = 'catalogued' "
                " where id = %s",
                (SCAN_ID,),
            )


def test_resolving_cannot_reach_back_into_a_sold_line(pg: Any) -> None:
    """The plan's requirement 3, and it needs no code to hold.

    A line sold against the unlisted placeholder records what was charged and
    what the cashier typed. Cataloguing the item next week does not rewrite
    that, because `sale_lines` has no update policy for anyone to use.
    """
    line_id = "019400bb-0000-7000-8000-0000000000f1"
    with pg.transaction(force_rollback=True):
        cur = pg.cursor()
        _held_sale(cur)
        cur.execute(
            """
            insert into public.sale_lines
                (id, sale_id, line_no, product_id, barcode_scanned, description,
                 qty_milli, unit_price, line_total)
            values (%s, %s, 1, %s, '8906110944741', 'Loose supari packet',
                    1000, 4500, 4500)
            """,
            (line_id, REVIEW_SALE_ID, UNLISTED_PRODUCT_ID),
        )

        cur.execute("set local role authenticated")
        cur.execute(
            "select set_config('request.jwt.claims', %s, true)",
            (claims(MANAGER_ID, perms.MANAGER),),
        )
        cur.execute(
            "update public.sale_lines set product_id = %s where id = %s",
            (PRODUCT_ID, line_id),
        )
        assert cur.rowcount == 0

        # And it is still pointed where it was. Asserting only on rowcount
        # would pass just as well against an empty table, which is how this
        # test read before the line above existed.
        cur.execute("reset role")
        cur.execute("select product_id, description from public.sale_lines where id = %s",
                    (line_id,))
        product_id, description = cur.fetchone()
        assert str(product_id) == UNLISTED_PRODUCT_ID
        assert description == "Loose supari packet"


def test_a_manager_may_withdraw_a_barcode(pg: Any) -> None:
    """0017. Removing a code is a soft delete, which is an UPDATE — and
    `product_barcodes` had insert and select only."""
    with pg.transaction(force_rollback=True):
        cur = pg.cursor()
        cur.execute("set local role authenticated")
        cur.execute(
            "select set_config('request.jwt.claims', %s, true)",
            (claims(MANAGER_ID, perms.MANAGER),),
        )
        cur.execute(
            "update public.product_barcodes set deleted_at = now() "
            "where product_id = %s and deleted_at is null",
            (PRODUCT_ID,),
        )
        assert cur.rowcount >= 1


def test_a_manager_may_open_a_price(pg: Any) -> None:
    with pg.transaction(force_rollback=True):
        cur = pg.cursor()
        cur.execute("set local role authenticated")
        cur.execute(
            "select set_config('request.jwt.claims', %s, true)",
            (claims(MANAGER_ID, perms.MANAGER),),
        )
        cur.execute(
            "insert into public.product_prices (product_id, store_id, price) "
            "values (%s, %s, 4500)",
            (PRODUCT_ID, STORE_ID),
        )
        assert cur.rowcount == 1


def test_a_cashier_may_not_open_a_price(pg: Any) -> None:
    with pg.transaction(force_rollback=True):
        cur = pg.cursor()
        cur.execute("set local role authenticated")
        cur.execute(
            "select set_config('request.jwt.claims', %s, true)",
            (claims(CASHIER_ID, perms.CASHIER),),
        )
        with pytest.raises(psycopg.Error):
            cur.execute(
                "insert into public.product_prices (product_id, store_id, price) "
                "values (%s, %s, 4500)",
                (PRODUCT_ID, STORE_ID),
            )


def test_slice_6_did_not_widen_the_margin_grant(pg: Any) -> None:
    """0017 grants insert and update on `product_prices` — but not on `cost`.

    The column grant from 0003 is the only thing keeping margin away from a
    till, and a slice that adds price editing is exactly where it would get
    widened by accident.
    """
    with pg.transaction(force_rollback=True):
        cur = pg.cursor()
        cur.execute("set local role authenticated")
        cur.execute(
            "select set_config('request.jwt.claims', %s, true)",
            (claims(MANAGER_ID, perms.MANAGER),),
        )
        # The type is the SQLSTATE: psycopg raises InsufficientPrivilege for
        # 42501, and the message itself does not carry the code.
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            cur.execute(
                "insert into public.product_prices (product_id, store_id, price, cost) "
                "values (%s, %s, 4500, 3000)",
                (PRODUCT_ID, STORE_ID),
            )


def _below_reorder(cur: Any, product_id: str, store_id: str = STORE_ID) -> None:
    cur.execute(
        """
        insert into public.stock_levels (store_id, product_id, on_hand, reorder_point)
        values (%s, %s, 1, 10)
        on conflict (store_id, product_id)
        do update set on_hand = 1, reorder_point = 10
        """,
        (store_id, product_id),
    )


def test_low_stock_stays_inside_your_own_store(pg: Any) -> None:
    """The view is `security_invoker`. Without that it would run as its owner
    and become a hole around `stock_levels_select`."""
    with pg.transaction(force_rollback=True):
        cur = pg.cursor()
        cur.execute(
            "insert into public.stores (id, code, name) values (%s, 'OTHER', 'Other') "
            "on conflict (id) do nothing",
            (OTHER_STORE_ID,),
        )
        _below_reorder(cur, PRODUCT_ID, store_id=OTHER_STORE_ID)
        cur.execute("set local role authenticated")
        cur.execute(
            "select set_config('request.jwt.claims', %s, true)",
            (claims(MANAGER_ID, perms.MANAGER),),
        )
        cur.execute("select count(*) from public.low_stock")
        assert cur.fetchone()[0] == 0


def test_the_unlisted_placeholder_never_reaches_the_reorder_list(pg: Any) -> None:
    """Even if something puts a level row against it.

    The placeholder stands in for many products, so its "stock" is the sum of
    unrelated things. It is `track_stock false`, which the view filters on —
    and rooting the view at `stock_levels` rather than at `products` means the
    ordinary case never gets that far anyway.
    """
    with pg.transaction(force_rollback=True):
        cur = pg.cursor()
        _below_reorder(cur, UNLISTED_PRODUCT_ID)
        cur.execute("set local role authenticated")
        cur.execute(
            "select set_config('request.jwt.claims', %s, true)",
            (claims(MANAGER_ID, perms.MANAGER),),
        )
        cur.execute(
            "select count(*) from public.low_stock where product_id = %s",
            (UNLISTED_PRODUCT_ID,),
        )
        assert cur.fetchone()[0] == 0

# ── 0018: reorder points ──────────────────────────────────────────────────


def _a_level(cur: object, store_id: str = STORE_ID, on_hand: int = 24_000) -> None:
    cur.execute(  # type: ignore[attr-defined]
        """
        insert into public.stock_levels (store_id, product_id, on_hand, reorder_point)
        values (%s, %s, %s, 0)
        on conflict (store_id, product_id)
        do update set on_hand = excluded.on_hand, reorder_point = 0
        """,
        (store_id, PRODUCT_ID, on_hand),
    )


def test_a_manager_may_set_a_reorder_point(pg: Any) -> None:
    with pg.transaction(force_rollback=True):
        cur = pg.cursor()
        _a_level(cur)
        cur.execute("set local role authenticated")
        cur.execute(
            "select set_config('request.jwt.claims', %s, true)",
            (claims(MANAGER_ID, perms.MANAGER),),
        )
        cur.execute(
            "update public.stock_levels set reorder_point = 5000 "
            "where product_id = %s and store_id = %s",
            (PRODUCT_ID, STORE_ID),
        )
        assert cur.rowcount == 1


def test_a_cashier_may_not_set_a_reorder_point(pg: Any) -> None:
    """`product.edit`, not `product.read`. Deciding when to reorder is
    catalogue work, and a till has no business doing it."""
    with pg.transaction(force_rollback=True):
        cur = pg.cursor()
        _a_level(cur)
        cur.execute("set local role authenticated")
        cur.execute(
            "select set_config('request.jwt.claims', %s, true)",
            (claims(CASHIER_ID, perms.CASHIER),),
        )
        cur.execute(
            "update public.stock_levels set reorder_point = 5000 "
            "where product_id = %s and store_id = %s",
            (PRODUCT_ID, STORE_ID),
        )
        assert cur.rowcount == 0


def test_a_reorder_point_stops_at_the_store_boundary(pg: Any) -> None:
    with pg.transaction(force_rollback=True):
        cur = pg.cursor()
        cur.execute(
            "insert into public.stores (id, code, name) values (%s, 'OTHER', 'Other') "
            "on conflict (id) do nothing",
            (OTHER_STORE_ID,),
        )
        _a_level(cur, store_id=OTHER_STORE_ID)
        cur.execute("set local role authenticated")
        cur.execute(
            "select set_config('request.jwt.claims', %s, true)",
            (claims(MANAGER_ID, perms.MANAGER),),
        )
        cur.execute(
            "update public.stock_levels set reorder_point = 5000 "
            "where product_id = %s and store_id = %s",
            (PRODUCT_ID, OTHER_STORE_ID),
        )
        assert cur.rowcount == 0


def test_on_hand_is_still_unwritable_by_anyone(pg: Any) -> None:
    """The whole reason 0018 revokes before it grants.

    Supabase had already granted `authenticated` table-wide privileges here,
    so adding an UPDATE policy alone would have handed every manager the power
    to write a level the ledger never agreed to — and
    `scripts/reconcile_stock.sql`, phase 6's exit criterion, would start
    returning rows with nothing to explain them.

    A column-level privilege is checked before RLS, so this fails at the grant
    layer whatever the policy says. psycopg carries SQLSTATE as the exception
    type; 42501 is not in the message.
    """
    with pg.transaction(force_rollback=True):
        cur = pg.cursor()
        _a_level(cur)
        cur.execute("set local role authenticated")
        cur.execute(
            "select set_config('request.jwt.claims', %s, true)",
            (claims(MANAGER_ID, perms.MANAGER),),
        )
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            cur.execute(
                "update public.stock_levels set on_hand = 999999 "
                "where product_id = %s and store_id = %s",
                (PRODUCT_ID, STORE_ID),
            )


def test_a_level_cannot_be_conjured_from_nothing(pg: Any) -> None:
    """No INSERT, deliberately. A row that arrives without a ledger entry
    behind it is an `on_hand` nobody can account for."""
    with pg.transaction(force_rollback=True):
        cur = pg.cursor()
        cur.execute("set local role authenticated")
        cur.execute(
            "select set_config('request.jwt.claims', %s, true)",
            (claims(MANAGER_ID, perms.MANAGER),),
        )
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            cur.execute(
                "insert into public.stock_levels "
                "(store_id, product_id, on_hand, reorder_point) "
                "values (%s, %s, 5000, 10)",
                (STORE_ID, UNCODED_PRODUCT_ID),
            )


def test_setting_a_reorder_point_moves_the_pull_cursor(pg: Any) -> None:
    """The puller pages `stock_levels` by `updated_at`.

    Only `apply_stock_delta` ever advanced it, so without the trigger in 0018
    a reorder point would change the cloud and never reach a till: the row's
    cursor would not move and the pull would step over it.

    The row is seeded with a literal old timestamp rather than a backdated
    UPDATE, because a BEFORE UPDATE trigger fires on that too and would stamp
    the very value the test was trying to set. And `now()` is the transaction
    timestamp, constant for the whole transaction, so the comparison has to be
    against something that is not `now()` at all.
    """
    stale = "2020-01-01T00:00:00+00:00"
    with pg.transaction(force_rollback=True):
        cur = pg.cursor()
        cur.execute(
            "delete from public.stock_levels where product_id = %s and store_id = %s",
            (PRODUCT_ID, STORE_ID),
        )
        cur.execute(
            "insert into public.stock_levels "
            "(store_id, product_id, on_hand, reorder_point, updated_at) "
            "values (%s, %s, 24000, 0, %s)",
            (STORE_ID, PRODUCT_ID, stale),
        )

        cur.execute("set local role authenticated")
        cur.execute(
            "select set_config('request.jwt.claims', %s, true)",
            (claims(MANAGER_ID, perms.MANAGER),),
        )
        # Deliberately does not name updated_at: the client cannot write it,
        # and the point is that it moves anyway.
        cur.execute(
            "update public.stock_levels set reorder_point = 5000 "
            "where product_id = %s and store_id = %s",
            (PRODUCT_ID, STORE_ID),
        )
        cur.execute("reset role")
        cur.execute(
            "select updated_at = now() from public.stock_levels "
            "where product_id = %s and store_id = %s",
            (PRODUCT_ID, STORE_ID),
        )
        assert cur.fetchone()[0] is True


def test_a_negative_reorder_point_is_refused(pg: Any) -> None:
    with pg.transaction(force_rollback=True):
        cur = pg.cursor()
        _a_level(cur)
        cur.execute("set local role authenticated")
        cur.execute(
            "select set_config('request.jwt.claims', %s, true)",
            (claims(MANAGER_ID, perms.MANAGER),),
        )
        with pytest.raises(psycopg.errors.CheckViolation):
            cur.execute(
                "update public.stock_levels set reorder_point = -1 "
                "where product_id = %s and store_id = %s",
                (PRODUCT_ID, STORE_ID),
            )



# ── 0019: the catalogue leaves a trail ────────────────────────────────────


def _as_manager(cur: Any) -> None:
    cur.execute("set local role authenticated")
    cur.execute(
        "select set_config('request.jwt.claims', %s, true)",
        (claims(MANAGER_ID, perms.MANAGER),),
    )


def _audit(cur: Any, action: str, entity_id: Any = None) -> list[Any]:
    """Audit rows for one action, read back as owner rather than as the caller.

    `entity_id` is not optional in spirit. Seeding the database creates
    products, barcodes and prices, and those inserts fire these triggers like
    any other — so the log is never empty and counting every row for an action
    answers a different question than the one a test is asking.
    """
    cur.execute("reset role")
    cur.execute(
        "select actor_id, entity, entity_id, before_json, after_json, store_id "
        "from public.audit_log "
        " where action = %s and (%s::uuid is null or entity_id = %s::uuid) "
        " order by occurred_at",
        (action, entity_id, entity_id),
    )
    return cur.fetchall()


def test_creating_a_product_leaves_a_trail(pg: Any) -> None:
    with pg.transaction(force_rollback=True):
        cur = pg.cursor()
        _as_manager(cur)
        cur.execute(
            "insert into public.products (sku, name, tax_code) "
            "values ('SKU-AUDIT-1', 'Audited thing', 'GST0') returning id"
        )
        made = cur.fetchone()[0]
        rows = _audit(cur, "product.created", made)
        assert len(rows) == 1
        actor, entity, _entity_id, before, after, store = rows[0]
        assert str(actor) == MANAGER_ID
        assert entity == "products"
        assert before is None
        assert after["sku"] == "SKU-AUDIT-1"
        # `products` has no store column; the trail records where the editor was.
        assert str(store) == STORE_ID


def test_withdrawing_a_barcode_is_named_as_a_withdrawal(pg: Any) -> None:
    """A soft delete and an edit are the same UPDATE to Postgres.

    `tg_argv[1]` exists so the log says which one a person did.
    """
    with pg.transaction(force_rollback=True):
        cur = pg.cursor()
        _as_manager(cur)
        cur.execute(
            "update public.product_barcodes set deleted_at = now() "
            "where product_id = %s and deleted_at is null",
            (PRODUCT_ID,),
        )
        assert cur.rowcount > 0
        assert len(_audit(cur, "barcode.withdrawn")) == cur.rowcount
        assert _audit(cur, "barcode.updated") == []


def test_cost_never_reaches_the_audit_trail(pg: Any) -> None:
    """0003 revoked `product_prices.cost` and 0005 made one guarded route to
    it. An audit row carrying the whole `NEW` record would have been a second,
    unguarded one — readable by anyone who may read the log at all."""
    with pg.transaction(force_rollback=True):
        cur = pg.cursor()
        cur.execute(
            "insert into public.product_prices (product_id, store_id, price, cost) "
            "values (%s, %s, 4500, 3000) returning id",
            (PRODUCT_ID, STORE_ID),
        )
        made = cur.fetchone()[0]
        rows = _audit(cur, "price.opened", made)
        assert len(rows) == 1
        after = rows[0][4]
        assert after["price"] == 4500
        assert "cost" not in after


def test_a_cashier_cannot_read_the_trail(pg: Any) -> None:
    with pg.transaction(force_rollback=True):
        cur = pg.cursor()
        cur.execute(
            "insert into public.products (sku, name, tax_code) "
            "values ('SKU-AUDIT-2', 'Another', 'GST0')"
        )
        cur.execute("set local role authenticated")
        cur.execute(
            "select set_config('request.jwt.claims', %s, true)",
            (claims(CASHIER_ID, perms.CASHIER),),
        )
        cur.execute("select count(*) from public.audit_log")
        assert cur.fetchone()[0] == 0


def test_a_store_less_row_is_readable_by_someone_who_may_read_the_log(pg: Any) -> None:
    """`audit_log_insert` always allowed a null store; `audit_log_select` did
    not, and `pos.in_store(null)` is false — so a catalogue edit would have
    been written and then invisible to everybody. 0019 mirrors the two."""
    with pg.transaction(force_rollback=True):
        cur = pg.cursor()
        marker = "018f0000-0000-7000-8000-0000000000aa"
        cur.execute(
            "insert into public.audit_log "
            "(id, store_id, action, entity, entity_id, occurred_at) "
            "values (gen_random_uuid(), null, 'product.updated', 'products', %s, now())",
            (marker,),
        )
        cur.execute("set local role authenticated")
        cur.execute(
            "select set_config('request.jwt.claims', %s, true)",
            (claims(MANAGER_ID, perms.MANAGER),),
        )
        cur.execute(
            "select count(*) from public.audit_log "
            " where store_id is null and entity_id = %s::uuid",
            (marker,),
        )
        assert cur.fetchone()[0] == 1


def test_a_sale_does_not_write_a_reorder_point_audit_row(pg: Any) -> None:
    """The `when` clause is the whole safety of the `stock_levels` trigger.

    `apply_stock_delta` upserts `on_hand` and `updated_at` on every ledger
    row, so without it every sale in the shop would land in the audit log and
    bury the thing the log is for.
    """
    with pg.transaction(force_rollback=True):
        cur = pg.cursor()
        cur.execute(
            "insert into public.stock_ledger "
            "(id, store_id, product_id, delta_milli, reason, occurred_at) "
            "values (gen_random_uuid(), %s, %s, -1000, 'sale', now())",
            (STORE_ID, PRODUCT_ID),
        )
        assert _audit(cur, "stock.reorder_point_set") == []

        _as_manager(cur)
        cur.execute(
            "update public.stock_levels set reorder_point = 5000 "
            "where product_id = %s and store_id = %s",
            (PRODUCT_ID, STORE_ID),
        )
        assert len(_audit(cur, "stock.reorder_point_set")) == 1


# ── 0020: privileges nothing uses ─────────────────────────────────────────


def test_the_audit_log_cannot_be_emptied(pg: Any) -> None:
    """TRUNCATE is the one privilege RLS cannot gate — no policy is consulted.

    The test shim granted everything *except* truncate until 0020, so this
    started from a kinder position than production and the real grant stayed
    invisible. The shim now mirrors it, which is what gives this test meaning.
    """
    with pg.transaction(force_rollback=True):
        cur = pg.cursor()
        _as_manager(cur)
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            cur.execute("truncate public.audit_log")


def test_the_ledger_cannot_be_emptied_either(pg: Any) -> None:
    with pg.transaction(force_rollback=True):
        cur = pg.cursor()
        _as_manager(cur)
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            cur.execute("truncate public.stock_ledger")


def test_an_audit_row_cannot_be_rewritten(pg: Any) -> None:
    """Append-only at the privilege layer, not only by the absence of a policy.

    A log the grant system says may be rewritten is a log with a caveat.
    """
    with pg.transaction(force_rollback=True):
        cur = pg.cursor()
        _as_manager(cur)
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            cur.execute("update public.audit_log set action = 'nothing.happened'")


def test_a_trigger_function_is_not_on_the_public_api(pg: Any) -> None:
    """`apply_stock_delta` is SECURITY DEFINER and lives in `public`, so
    PostgREST published it at /rest/v1/rpc/. A trigger needs no EXECUTE grant
    to fire, so taking it away costs nothing."""
    with pg.transaction(force_rollback=True):
        cur = pg.cursor()
        cur.execute(
            "select has_function_privilege('authenticated', "
            "'public.apply_stock_delta()', 'EXECUTE')"
        )
        assert cur.fetchone()[0] is False


def test_the_ledger_still_applies_after_the_revoke(pg: Any) -> None:
    """The point of the test above: the trigger fires regardless."""
    with pg.transaction(force_rollback=True):
        cur = pg.cursor()
        cur.execute(
            "select coalesce(on_hand, 0) from public.stock_levels "
            "where product_id = %s and store_id = %s",
            (PRODUCT_ID, STORE_ID),
        )
        row = cur.fetchone()
        before = row[0] if row else 0

        cur.execute("set local role authenticated")
        cur.execute(
            "select set_config('request.jwt.claims', %s, true)",
            (claims(CASHIER_ID, perms.CASHIER),),
        )
        cur.execute("reset role")
        cur.execute(
            "insert into public.stock_ledger "
            "(id, store_id, product_id, delta_milli, reason, occurred_at) "
            "values (gen_random_uuid(), %s, %s, 7000, 'receipt', now())",
            (STORE_ID, PRODUCT_ID),
        )
        cur.execute(
            "select on_hand from public.stock_levels "
            "where product_id = %s and store_id = %s",
            (PRODUCT_ID, STORE_ID),
        )
        assert cur.fetchone()[0] == before + 7000



# ── The matrix, at the boundary — phase 7 slice 1 ───────────────────────────
#
# `tests/test_permission_matrix.py` asserts the Python and FastAPI layers and
# explains why the three live apart. This is the layer that is actually
# security: React hiding a button and FastAPI returning 403 are both UX, and
# if the terminal is compromised these policies are what still holds.
#
# The policies are read out of `pg_policies` rather than transcribed. A list
# of tables written by hand here would drift exactly the way this file's own
# list of SQL files did before it was made to discover itself.

PERM_IN_POLICY = re.compile(r"has_perm\('([a-z.]+)'::text\)")


def _policy_expressions(pg: Any) -> list[tuple[str, str, str]]:
    """(table, policy, the whole expression) for every policy in `public`."""
    return [
        (table, policy, f"{qual or ''} {check or ''}")
        for table, policy, qual, check in pg.execute(
            "select tablename, policyname, qual, with_check from pg_policies "
            "where schemaname = 'public'"
        ).fetchall()
    ]


def _key_gated_tables(pg: Any) -> dict[str, str]:
    """Tables whose readability is exactly one permission key.

    Three kinds of SELECT policy are deliberately excluded, because for them
    "holds the key" is not the whole condition and a flat assertion would be
    wrong rather than strict:

    * anything naming `auth.uid()` — `employees`, `user_store_roles`,
      `register_sessions` and `sales` all let a person read their own row
      without the key, which existing tests here assert on purpose;
    * tables carrying more than one permissive SELECT policy, since Postgres
      ORs them together and the effective rule is neither one alone;
    * policies naming several keys, which mean "any of these" — a different
      claim, and the subject of the two tests at the end of this file.
    """
    by_table: dict[str, list[tuple[str, str]]] = {}
    for table, policy, qual, in_check in pg.execute(
        "select tablename, policyname, qual, with_check from pg_policies "
        "where schemaname = 'public' and cmd = 'SELECT'"
    ).fetchall():
        by_table.setdefault(table, []).append((policy, f"{qual or ''} {in_check or ''}"))

    gated: dict[str, str] = {}
    for table, policies in by_table.items():
        if len(policies) != 1:
            continue
        _policy, expression = policies[0]
        if "uid()" in expression:
            continue
        keys = set(PERM_IN_POLICY.findall(expression))
        if len(keys) == 1:
            gated[table] = next(iter(keys))
    return gated


def test_every_permission_named_in_a_policy_is_a_real_key(pg: Any) -> None:
    """A policy naming a key the matrix does not have never fires.

    `pos.has_perm` is a containment test against the token's claim array, so a
    typo — or a key renamed in `app/domain/permissions.py` and not here —
    returns false for everybody, for ever, with no error. The table quietly
    becomes unreadable and nothing says so.
    """
    unknown = {
        policy: sorted(set(PERM_IN_POLICY.findall(expression)) - perms.ALL_PERMISSIONS)
        for _table, policy, expression in _policy_expressions(pg)
        if set(PERM_IN_POLICY.findall(expression)) - perms.ALL_PERMISSIONS
    }
    assert not unknown, (
        "policies naming permission keys that are not in the matrix:\n  "
        + "\n  ".join(f"{policy}: {keys}" for policy, keys in unknown.items())
    )


def test_the_discovery_is_not_vacuous(pg: Any) -> None:
    """The regex above is load-bearing; assert it still finds the schema.

    Every assertion derived from `pg_policies` passes trivially if the pattern
    stops matching — a change in how Postgres renders a policy would turn this
    whole section green and empty.
    """
    gated = _key_gated_tables(pg)
    assert gated.get("products") == perms.PRODUCT_READ
    assert gated.get("audit_log") == perms.USER_MANAGE
    assert len(gated) >= 8, f"only {len(gated)} key-gated tables found: {gated}"


@pytest.mark.parametrize("role", perms.ROLES)
def test_select_matrix_at_the_boundary(pg: Any, role: str) -> None:
    """A role without the key reads nothing from a table that requires it.

    A denied SELECT under RLS is an empty set rather than an error, so the
    assertion in the deny direction is "zero rows". The allow direction is not
    asserted here: a table can be empty for honest reasons, and "may read" is
    already covered by the named tests above.
    """
    user_id = {
        perms.CASHIER: CASHIER_ID,
        perms.SUPERVISOR: SUPERVISOR_ID,
    }.get(role, MANAGER_ID)
    held = perms.permissions_for(frozenset({role}))

    for table, key in sorted(_key_gated_tables(pg).items()):
        if key in held:
            continue
        rows = run_as(pg, claims(user_id, role), f"select count(*) from public.{table}")
        assert rows[0][0] == 0, (
            f"rls layer: {role} does not hold {key} but read {rows[0][0]} rows "
            f"from public.{table}"
        )


# ── What the matrix found on its first run ──────────────────────────────────
#
# The phase 7 plan predicts one finding here: that `stock_ledger_insert`
# "accepts a row if the caller holds **any** of `sale.create`,
# `stock.receive`, `stock.count`, `stock.adjust`. It never compares the row's
# `reason` to the key. A cashier can already push an adjustment today."
#
# Half of that is out of date and half of it is real, which is the argument
# for writing the test before trusting the document. `0012` already tied the
# `sale.create` disjunct to `ref_type = 'sale'`, so the cashier is refused.
# The other three disjuncts are still unconditional, so the hole moved rather
# than closed: it belongs to `inventory` now, not to `cashier`.


def test_a_cashier_cannot_write_an_adjustment(pg: Any) -> None:
    """`0012` tightened `sale.create` to the rows a sale actually writes.

    A regression guard on a hole that is already shut. The plan still names
    this one as open, and the cheapest way to keep a fixed thing fixed is to
    say so in the suite rather than in a document.
    """
    with pytest.raises(Denied):
        run_as(
            pg,
            claims(CASHIER_ID, perms.CASHIER),
            "insert into public.stock_ledger (id, store_id, product_id, "
            "delta_milli, reason, ref_type, occurred_at, terminal_id, user_id) "
            "values (gen_random_uuid(), %s, %s, %s, 'adjustment', 'adjustment', "
            "now(), %s, %s)",
            (STORE_ID, PRODUCT_ID, -5_000, TERMINAL_UUID, CASHIER_ID),
        )


@pytest.mark.xfail(
    reason="phase 7 — stock_ledger_insert ORs three stock keys unconditionally, "
    "so stock.receive is enough to write an adjustment",
    strict=True,
)
def test_receiving_stock_is_not_permission_to_adjust_it(pg: Any) -> None:
    """Holding `stock.receive` should not write a `reason = 'adjustment'` row.

    §11.1 gives `stock.receive` and `stock.count` to the inventory role and
    reserves `stock.adjust` for manager and admin. The reason is written down
    in `app/api/inventory.py`: receiving has a supplier's document behind it,
    and an adjustment has only a sentence somebody typed.

    `0012`'s own comment says "a receipt, a count, an adjustment: a stock
    permission each", but the SQL under that comment is a flat OR of the three
    — so any one of them admits a row of any `ref_type`. The comment describes
    the intended policy and the code does not implement it.

    The claims below carry a seeded employee's id with the inventory role's
    permissions, because the FK on `user_id` wants a real employee and the
    policy never looks at who the row names, only at what the token holds.
    That is the isolation this test wants: one variable, the claim set.

    `xfail(strict=True)` — the day the policy maps reason to key, this starts
    passing and fails the run until the marker comes off with it.
    """
    with pytest.raises(Denied):
        run_as(
            pg,
            claims(MANAGER_ID, perms.INVENTORY),
            "insert into public.stock_ledger (id, store_id, product_id, "
            "delta_milli, reason, ref_type, occurred_at, terminal_id, user_id) "
            "values (gen_random_uuid(), %s, %s, %s, 'adjustment', 'adjustment', "
            "now(), %s, %s)",
            (STORE_ID, PRODUCT_ID, -5_000, TERMINAL_UUID, MANAGER_ID),
        )


# ── What a supervisor may lend — phase 7 slice 3 ────────────────────────────
#
# `app/domain/permissions.py` states the rule: a permission is overridable
# only if the write it authorises is already accepted under the cashier's own
# claim. A 90-second grant lives on the terminal and Postgres never sees it,
# so a key that fails this test produces the worst failure available — the
# sale completes, the customer leaves, and the row quarantines at 2am.
#
# The set is declared there and justified here. Declaring it is unavoidable
# (the terminal cannot read `pg_policies` at a counter), but nothing has to
# take the declaration on trust: each key below is a real write, run under a
# claim that does **not** carry the key, against the real policies.


def _cashier_claims() -> str:
    """A cashier's token. Holds `sale.create` and none of the overridables."""
    return claims(CASHIER_ID, perms.CASHIER)


def _a_sale(cur: Any) -> str:
    """A completed sale belonging to the cashier, to hang lines off."""
    sale_id = "019500aa-0000-7000-8000-00000000f001"
    cur.execute(
        "insert into public.sales (id, store_id, terminal_id, cashier_id, "
        "status, client_created_at) values (%s, %s, %s, %s, 'completed', now())",
        (sale_id, STORE_ID, TERMINAL_UUID, CASHIER_ID),
    )
    return sale_id


def _line_written_under(pg: Any, jwt_claims: str, **overrides: Any) -> int:
    """Insert one sale line as the holder of these claims; return rowcount."""
    with pg.transaction(force_rollback=True):
        cur = pg.cursor()
        sale_id = _a_sale(cur)
        cur.execute("set local role authenticated")
        cur.execute("select set_config('request.jwt.claims', %s, true)", (jwt_claims,))
        cur.execute(
            "insert into public.sale_lines (id, sale_id, line_no, product_id, "
            "description, qty_milli, unit_price, discount_amount, tax_amount, "
            "line_total, overridden_by, override_reason) "
            "values (gen_random_uuid(), %s, 1, %s, 'Probe', 1000, %s, %s, 0, %s, "
            "%s, %s)",
            (
                sale_id,
                PRODUCT_ID,
                overrides.get("unit_price", 1000),
                overrides.get("discount_amount", 0),
                overrides.get("line_total", 1000),
                overrides.get("overridden_by", SUPERVISOR_ID),
                overrides.get("override_reason", "supervisor authorised"),
            ),
        )
        return cur.rowcount


def test_a_discounted_line_pushes_under_the_cashiers_own_claim(pg: Any) -> None:
    """`sale.discount.line` is overridable because RLS never asks for it.

    `sale_lines_insert` asks whether the caller may create a sale, not which
    key justified the discount on it. So a line a supervisor authorised rides
    the outbox under the cashier's token and is accepted — the grant never
    needs to reach Postgres, which is what makes the offline case work.
    """
    assert perms.SALE_DISCOUNT_LINE not in perms.ROLE_PERMISSIONS[perms.CASHIER]
    assert _line_written_under(pg, _cashier_claims(), discount_amount=200) == 1


def test_a_price_override_pushes_under_the_cashiers_own_claim(pg: Any) -> None:
    assert perms.PRICE_OVERRIDE not in perms.ROLE_PERMISSIONS[perms.CASHIER]
    assert _line_written_under(pg, _cashier_claims(), unit_price=1) == 1


def test_a_void_pushes_under_the_cashiers_own_claim(pg: Any) -> None:
    """A void is a new `sales` row referencing the original (§1.4).

    `sales_insert` wants `sale.create` and `cashier_id = auth.uid()`. Both are
    true of the cashier who took the sale and asked for authorisation, so the
    compensating row lands.
    """
    assert perms.SALE_VOID not in perms.ROLE_PERMISSIONS[perms.CASHIER]
    with pg.transaction(force_rollback=True):
        cur = pg.cursor()
        original = _a_sale(cur)
        cur.execute("set local role authenticated")
        cur.execute(
            "select set_config('request.jwt.claims', %s, true)", (_cashier_claims(),)
        )
        cur.execute(
            "insert into public.sales (id, store_id, terminal_id, cashier_id, "
            "status, original_sale_id, client_created_at) "
            "values (gen_random_uuid(), %s, %s, %s, 'voided', %s, now())",
            (STORE_ID, TERMINAL_UUID, CASHIER_ID, original),
        )
        assert cur.rowcount == 1


def test_a_payout_pushes_under_a_colleagues_claim_since_0024(pg: Any) -> None:
    """The counter-example that stopped being one, and why.

    Until 0024, `cash_movements_insert` asked the *pusher* for `cash.payout`
    by name. That was the reason `cash.payout` could not be lent at the
    counter — a cashier's JWT does not carry it, so the row would have been
    refused hours later into the failures queue. It was also a deadlock
    waiting to happen: a supervisor's payout at six, pushed by the morning
    cashier at eight, refused for ever. 0009 found and fixed the identical
    shape on `sales`.

    0024 makes the same trade for the same reason: the actor columns are
    provenance, authorisation happened on the terminal under `require()`,
    and the cloud asks the pusher only to belong to the store and hold
    `sale.create`. So a cashier can now push a payout — attributed, as the
    row says, to whoever made it.

    `cash.payout` stays out of `OVERRIDABLE` for now, but for a different
    reason than before: lending it needs `approved_by` carried into the row
    and the modal wired to the cash screen, which is phase 8 slice 3. The
    policy no longer stands in the way, and this test is what says so.
    """
    assert perms.CASH_PAYOUT not in perms.OVERRIDABLE

    payout = (
        "insert into public.cash_movements (id, session_id, direction, "
        "amount, reason, actor_id, occurred_at) values "
        "(gen_random_uuid(), %s, 'out', 500, 'probe', %s, now())"
    )

    def attempt(jwt_claims: str, actor: str) -> int:
        with pg.transaction(force_rollback=True):
            cur = pg.cursor()
            session_id = "019500aa-0000-7000-8000-00000000f900"
            cur.execute(
                "insert into public.register_sessions (id, store_id, terminal_id, "
                "user_id, opened_at) values (%s, %s, %s, %s, now())",
                (session_id, STORE_ID, TERMINAL_UUID, actor),
            )
            cur.execute("set local role authenticated")
            cur.execute(
                "select set_config('request.jwt.claims', %s, true)", (jwt_claims,)
            )
            cur.execute(payout, (session_id, actor))
            return cur.rowcount

    # The supervisor's own payout, as before.
    assert attempt(claims(SUPERVISOR_ID, perms.SUPERVISOR), SUPERVISOR_ID) == 1
    # A cashier pushing a supervisor's payout — the 0009 deadlock shape.
    assert attempt(_cashier_claims(), SUPERVISOR_ID) == 1
    # And, since provenance is not authorisation, their own.
    assert attempt(_cashier_claims(), CASHIER_ID) == 1


def test_a_shift_close_pushes_under_a_colleagues_claim(pg: Any) -> None:
    """The close is a row (phase 8 decision 1) and the morning cashier pushes
    last night's. `shift_closes_insert` asks for the store and `sale.create`;
    reading it back asks for `report.sales.store`, which a cashier lacks —
    so the read is asserted both ways."""
    session_id = "019500aa-0000-7000-8000-00000000f901"
    close = (
        "insert into public.shift_closes (id, session_id, closed_at, closed_by, "
        "counted_cash, expected_cash, variance, cash_sales, upi_attested, "
        "upi_verified, cash_in, cash_out, rounding, under_review_count, "
        "under_review_total, sales_count) values "
        "(gen_random_uuid(), %s, now(), %s, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0)"
    )
    with pg.transaction(force_rollback=True):
        cur = pg.cursor()
        cur.execute(
            "insert into public.register_sessions (id, store_id, terminal_id, "
            "user_id, opened_at) values (%s, %s, %s, %s, now())",
            (session_id, STORE_ID, TERMINAL_UUID, SUPERVISOR_ID),
        )
        cur.execute("set local role authenticated")
        cur.execute(
            "select set_config('request.jwt.claims', %s, true)", (_cashier_claims(),)
        )
        cur.execute(close, (session_id, SUPERVISOR_ID))
        assert cur.rowcount == 1

        cur.execute("select count(*) from public.shift_closes where session_id = %s", (session_id,))
        assert cur.fetchone()[0] == 0, "a cashier read a close back"

        cur.execute(
            "select set_config('request.jwt.claims', %s, true)",
            (claims(MANAGER_ID, perms.MANAGER),),
        )
        cur.execute("select count(*) from public.shift_closes where session_id = %s", (session_id,))
        assert cur.fetchone()[0] == 1


def test_every_overridable_key_is_a_real_permission() -> None:
    """A typo in `OVERRIDABLE` would grant nothing and say nothing."""
    assert perms.OVERRIDABLE <= perms.ALL_PERMISSIONS
    assert perms.OVERRIDABLE, "nothing is overridable — the modal has no purpose"


def test_nothing_the_cashier_already_holds_is_overridable() -> None:
    """Overriding a key the cashier already has is a contradiction.

    It would also hide a real bug: if `sale.create` were ever listed here, a
    grant would appear to work for reasons that had nothing to do with the
    grant, and the modal would look correct while doing nothing.
    """
    cashier = perms.ROLE_PERMISSIONS[perms.CASHIER]
    assert not (perms.OVERRIDABLE & cashier), sorted(perms.OVERRIDABLE & cashier)


# ── A grant on its way to the cloud — phase 7 slice 3 ───────────────────────
#
# `0022` added the first `sync_push` branch whose subject is an audit row
# rather than a business record. Before it, the terminal could mint a grant
# and had nowhere to send it, so the row stayed local with nothing queued
# against it — unrecorded rather than delayed, and recoverable only by hand.


def _override_envelope(audit_id: str, *, actor: str, approver: str) -> str:
    return json.dumps(
        [
            {
                "schema_version": 3,
                "entity": "override",
                "op": "insert",
                "id": audit_id,
                "client_seq": 1,
                "data": {
                    "id": audit_id,
                    "store_id": STORE_ID,
                    "actor_id": actor,
                    "approver_id": approver,
                    "action": "override.granted",
                    "entity": "permission",
                    "entity_id": None,
                    "after_json": json.dumps(
                        {
                            "permission": "sale.void",
                            "expires_at": "2026-09-17T10:01:30+00:00",
                        }
                    ),
                    "occurred_at": "2026-09-17T10:00:00+00:00",
                },
            }
        ]
    )


def _read_back_as_manager(cur: Any, audit_id: str, columns: str) -> tuple | None:
    """Switch to a manager's claims before reading an audit row.

    The cashier who pushed it cannot read it: `audit_log_select` asks for
    `user.manage`. That asymmetry is deliberate — anyone may write an audit
    row, reading the log is a manager's privilege — and it means a test that
    reads back as the writer sees nothing and reports the push as lost.
    """
    cur.execute("set local role authenticated")
    cur.execute(
        "select set_config('request.jwt.claims', %s, true)",
        (claims(MANAGER_ID, perms.MANAGER),),
    )
    cur.execute(
        f"select {columns} from public.audit_log where id = %s", (audit_id,)
    )
    return cur.fetchone()


def test_a_grant_pushes_under_the_cashiers_own_claim(pg: Any) -> None:
    """The row names the supervisor; the token belongs to the cashier.

    That is the whole shape of the phase 7 decision, at the boundary. The
    supervisor's authority was spent on the terminal and never travels — what
    arrives is a record written by somebody entitled to write it, naming
    somebody else as the person who allowed the thing.

    `audit_log_insert` asks only that the row belongs to the caller's store,
    which is what makes `override` safe to push this way and what made
    `permission` an overridable-safe key under the slice 3 rule.
    """
    audit_id = "019600aa-0000-7000-8000-00000000a001"
    with pg.transaction(force_rollback=True):
        _push(
            pg,
            _override_envelope(audit_id, actor=CASHIER_ID, approver=SUPERVISOR_ID),
        )

        row = _read_back_as_manager(
            pg.cursor(),
            audit_id,
            "actor_id, approver_id, action, entity, entity_id, store_id",
        )

    assert row is not None, "the grant did not land"
    assert str(row[0]) == CASHIER_ID
    assert str(row[1]) == SUPERVISOR_ID
    assert row[2] == "override.granted"
    assert row[3] == "permission"
    assert row[4] is None, "entity_id is a uuid; the permission lives in after_json"
    assert str(row[5]) == STORE_ID


def test_a_grant_names_two_different_people_on_arrival(pg: Any) -> None:
    """An override naming one person twice is not an override.

    Asserted on the far side rather than only where the row is built, because
    this is the column the audit log exists for and the one nothing wrote
    until now.
    """
    audit_id = "019600aa-0000-7000-8000-00000000a002"
    with pg.transaction(force_rollback=True):
        _push(
            pg,
            _override_envelope(audit_id, actor=CASHIER_ID, approver=SUPERVISOR_ID),
        )
        row = _read_back_as_manager(
            pg.cursor(), audit_id, "actor_id <> approver_id"
        )
        assert row is not None and row[0] is True


def test_pushing_the_same_grant_twice_changes_nothing(pg: Any) -> None:
    """`on conflict (id) do nothing`, like every other branch.

    A retry after a network drop must not turn one authorisation into two —
    the audit log would then report an escalation that did not happen, which
    is the same class of lie as failing to report one that did.
    """
    audit_id = "019600aa-0000-7000-8000-00000000a003"
    envelope = _override_envelope(
        audit_id, actor=CASHIER_ID, approver=SUPERVISOR_ID
    )
    with pg.transaction(force_rollback=True):
        _push(pg, envelope)
        _push(pg, envelope)

        row = _read_back_as_manager(pg.cursor(), audit_id, "count(*)")
        assert row is not None and row[0] == 1


def test_a_grant_cannot_be_pushed_into_another_store(pg: Any) -> None:
    """`audit_log_insert` scopes by store like everything else.

    The row carries its own `store_id` precisely because it has no parent to
    take one from, which makes this the one place that check can be made.
    """
    audit_id = "019600aa-0000-7000-8000-00000000a004"
    envelope = json.loads(
        _override_envelope(audit_id, actor=CASHIER_ID, approver=SUPERVISOR_ID)
    )
    envelope[0]["data"]["store_id"] = OTHER_STORE_ID

    with (
        pytest.raises(psycopg.errors.InsufficientPrivilege),
        pg.transaction(force_rollback=True),
    ):
        _push(pg, json.dumps(envelope))


def test_a_manager_can_read_a_grant_and_a_cashier_cannot(pg: Any) -> None:
    """The point of pushing it: slice 5's viewer reads the cloud.

    `audit_log_select` wants `user.manage`, so the person who goes looking for
    "who authorised that void" can see it and the till cannot.
    """
    audit_id = "019600aa-0000-7000-8000-00000000a005"
    with pg.transaction(force_rollback=True):
        _push(
            pg,
            _override_envelope(audit_id, actor=CASHIER_ID, approver=SUPERVISOR_ID),
        )

        cur = pg.cursor()
        for role, user_id, expected in (
            (perms.MANAGER, MANAGER_ID, 1),
            (perms.CASHIER, CASHIER_ID, 0),
        ):
            cur.execute("set local role authenticated")
            cur.execute(
                "select set_config('request.jwt.claims', %s, true)",
                (claims(user_id, role),),
            )
            cur.execute(
                "select count(*) from public.audit_log where id = %s", (audit_id,)
            )
            assert cur.fetchone()[0] == expected, role


def test_a_lockout_pushes_as_an_audit_row(pg: Any) -> None:
    """`0023` widened the override branch rather than adding a second one.

    A lockout and a grant are both audit rows with no parent, so they share a
    branch; the entity names stay apart on the wire because `override` was
    already in terminals' outboxes when lockouts were added.
    """
    audit_id = "019600aa-0000-7000-8000-00000000a010"
    envelope = json.dumps([{
        "schema_version": 3, "entity": "audit", "op": "insert",
        "id": audit_id, "client_seq": 1,
        "data": {
            "id": audit_id, "store_id": STORE_ID,
            "actor_id": SUPERVISOR_ID, "approver_id": None,
            "action": "pin.locked", "entity": "permission", "entity_id": None,
            "after_json": json.dumps({"employee_code": "S001"}),
            "occurred_at": "2026-09-17T23:00:00+00:00",
        },
    }])

    with pg.transaction(force_rollback=True):
        _push(pg, envelope)
        row = _read_back_as_manager(
            pg.cursor(), audit_id, "action, actor_id, approver_id"
        )

    assert row is not None, "the lockout did not land"
    assert row[0] == "pin.locked"
    assert str(row[1]) == SUPERVISOR_ID
    assert row[2] is None, "nobody authorised a lockout"
