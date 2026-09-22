"""Application assembly.

Everything the routers need is built once here and hung on ``app.state``: the
database, the session store, the auth service. Nothing constructs its own
dependencies at request time, which is what makes the whole API testable
against a temp SQLite file with no window and no network.
"""

from __future__ import annotations

import logging
import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app import __version__
from app.api import admin as admin_router
from app.api import auth as auth_router
from app.api import catalog as catalog_router
from app.api import events as events_router
from app.api import health as health_router
from app.api import inventory as inventory_router
from app.api import overrides as overrides_router
from app.api import register as register_router
from app.api import reports as reports_router
from app.api import shifts as shifts_router
from app.api import sync as sync_router
from app.api.dev_ui import DEV_LOGIN_PAGE
from app.config import Settings, get_settings
from app.data.db import Database
from app.data.migrations import migrate
from app.data.repositories.audit import AuditRepository
from app.data.repositories.catalog import CatalogRepository
from app.data.repositories.inventory import InventoryRepository
from app.data.repositories.outbox import OutboxRepository
from app.data.repositories.sales import SalesRepository
from app.data.repositories.shifts import ShiftRepository
from app.data.repositories.terminal import TerminalRepository
from app.data.repositories.unknown_scans import UnknownScanRepository
from app.data.repositories.users import CachedUserRepository
from app.security import keychain
from app.security import snapshot_mac as keychain_mac
from app.security.local_auth import HostGuardMiddleware, SessionTokenMiddleware
from app.services.admin_service import AdminService
from app.services.auth_service import AuthService, SessionStore
from app.services.cart_service import CartService
from app.services.inventory_service import InventoryService
from app.services.payment_providers import default_registry
from app.services.sale_service import SaleService
from app.services.shift_service import ShiftService
from app.services.supabase_auth import InvalidCredentials, SupabaseAuthClient
from app.sync.engine import SyncEngine
from app.sync.payloads import PayloadBuilder
from app.sync.puller import Puller
from app.sync.pusher import Pusher
from app.sync.revocations import RevocationChecker, RevocationSweep
from app.sync.tokens import TokenRefresher

log = logging.getLogger(__name__)

#: Body fields whose submitted value must never come back in an error.
#:
#: A 422 still names the field and says what was wrong with it; only the value
#: is dropped. Matched case-insensitively against every part of the error's
#: `loc`, so a nested `{"credentials": {"pin": ...}}` is covered too.
SENSITIVE_FIELDS = frozenset({"pin", "password", "secret", "token", "refresh_token"})

UI_DIR = Path(__file__).resolve().parent.parent / "ui"


def _snapshot_key(settings: Settings) -> bytes:
    """The terminal's sealing key, or a throwaway one.

    A till with no usable credential store must still open — the alternative
    is a shop that cannot trade because a Windows profile moved. What it loses
    is the offline cache: a random key seals nothing anybody can read back, so
    every cached identity fails verification and the terminal asks for an
    online sign-in.

    That is the safe direction and a bad surprise, so it is an error in the
    log rather than a warning. It is also the one failure here that a person
    can fix.
    """
    try:
        return keychain_mac.mac_key(settings.store_code, settings.terminal_code)
    except keychain.KeychainUnavailable as exc:
        log.error(
            "no OS credential store, so the offline identity cache cannot be "
            "sealed or read: %s. The till will work online and will refuse "
            "offline sign-in until this is fixed.",
            exc,
        )
        return secrets.token_bytes(32)


def build_app(
    *,
    token: str,
    settings: Settings | None = None,
    db: Database | None = None,
    run_migrations: bool = True,
    mac_key: bytes | None = None,
) -> FastAPI:
    """Assemble the application.

    `mac_key` seals the offline identity cache and is injected for the same
    reason `db` and `token` are: a test that let the real OS credential store
    decide would seal with one key and read with another, and every cached
    identity would fail verification for reasons that have nothing to do with
    what was being tested.
    """
    settings = settings or get_settings()
    if db is None:
        settings.ensure_directories()
        db = Database(settings.db_path)
    if run_migrations:
        migrate(db)

    users = CachedUserRepository(
        db, sealer=keychain_mac.SnapshotSealer(mac_key or _snapshot_key(settings))
    )
    audit = AuditRepository(db)
    catalog = CatalogRepository(db)
    inventory = InventoryRepository(db)
    unknown_scans = UnknownScanRepository(db)
    sales = SalesRepository(db)
    terminal = TerminalRepository(db)
    sessions = SessionStore()
    cloud = (
        SupabaseAuthClient(settings.supabase_url, settings.supabase_anon_key)
        if settings.cloud_configured
        else None
    )
    if cloud is None:
        log.warning(
            "no Supabase project configured — login will use the local cache only"
        )

    outbox = OutboxRepository(db)
    engine = _build_sync_engine(db, outbox, sessions, settings)
    if engine is not None and cloud is not None:
        # The refresher, wired here for the same reason the revocation sweep
        # is: it needs the auth client and the session store, and `app.sync`
        # may import neither. Everything crosses as a callable.
        refresher = TokenRefresher(
            exchange=cloud.refresh,
            current_token=lambda: sessions.access_token,
            replace_token=sessions.replace_token,
            store_code=settings.store_code,
            terminal_code=settings.terminal_code,
            is_rejection=lambda exc: isinstance(exc, InvalidCredentials),
        )
        engine.tokens = refresher
        engine.pusher.renew = refresher.renew
    if engine is not None:
        # Built here rather than inside `_build_sync_engine` because it needs
        # the user repository and the session store, and the engine is
        # deliberately ignorant of both: `app.sync` and `app.services` are
        # independent siblings in the import contract, so the session crosses
        # that line as two callables rather than as an import.
        engine.revocations = RevocationSweep(
            checker=RevocationChecker(
                base_url=settings.supabase_url,
                anon_key=settings.supabase_anon_key,
                token_provider=lambda: sessions.access_token,
                store_code=settings.store_code,
            ),
            users=users,
            current_user_id=lambda: (
                sessions.current.user_id if sessions.current else None
            ),
            on_self_revoked=sessions.mark_revoked,
        )

    @asynccontextmanager
    async def lifespan(instance: FastAPI) -> AsyncIterator[None]:
        # Started inside the running loop, not at construction: the engine
        # owns an asyncio task and a wake event, and both need a loop to
        # belong to. A test client that never enters the context therefore
        # never starts a background task, which is what keeps the suite quiet.
        if engine is not None:
            engine.publish = instance.state.events.broadcast
            if engine.revocations is not None:
                engine.revocations.publish = instance.state.events.broadcast
            engine.start()
        yield
        if engine is not None:
            await engine.stop()
        if cloud is not None:
            await cloud.aclose()

    app = FastAPI(
        title="Retail POS — local service",
        version=__version__,
        # The local API is not a public surface; the schema is served only so
        # that the TypeScript types can be generated from it.
        docs_url=None,
        redoc_url=None,
        lifespan=lifespan,
    )

    app.state.settings = settings
    app.state.db = db
    app.state.outbox = outbox
    app.state.sync = engine
    app.state.sessions = sessions
    app.state.session_token = token
    app.state.events = events_router.EventHub()
    app.state.catalog = catalog
    app.state.unknown_scans = unknown_scans
    cart_service = CartService(
        catalog, unknown_scans, terminal_code=settings.terminal_code
    )
    app.state.cart_service = cart_service
    shift_service = ShiftService(ShiftRepository(db), terminal_code=settings.terminal_code)
    app.state.shift_service = shift_service
    app.state.sale_service = SaleService(
        carts=cart_service,
        sales=sales,
        terminal=terminal,
        providers=default_registry(),
        settings=settings,
        open_shift=shift_service.require_open,
    )
    app.state.inventory_service = InventoryService(
        catalog, inventory, terminal_code=settings.terminal_code
    )
    # Catalogue admin reaches Supabase under the signed-in user's own
    # token, so RLS decides. Built even when no project is configured:
    # it refuses with "needs the internet", which is the true reason
    # and a better screen than a missing route (phase 6 decision 2).
    app.state.admin_service = AdminService(
        settings.supabase_url, settings.supabase_anon_key, sessions
    )
    app.state.auth_service = AuthService(
        users=users,
        sessions=sessions,
        cloud=cloud,
        store_code=settings.store_code,
        terminal_code=settings.terminal_code,
        settings=settings,
        audit=audit,
    )

    @app.exception_handler(RequestValidationError)
    async def _validation_error(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        """422 without handing back what was sent.

        FastAPI's default handler puts the offending value in `input`, so a
        PIN one character short comes straight back in the response body:

            {"type": "string_too_short", "loc": ["body", "pin"],
             "input": "123", ...}

        Nothing reads it today — the UI only looks at `detail` when it is a
        string, and a 422's is a list — and nothing logs it, because uvicorn
        runs with `access_log=False`. That is the absence of a mistake rather
        than a defence against one, and phase 9 adds a diagnostics screen that
        reads log files.

        Written now because `/overrides/authorize` is about to become the
        second route that takes a PIN, and a supervisor's PIN is the credential
        most likely to be typed over somebody's shoulder and reused. One route
        with this exposure is a thing to fix; two is a pattern.

        The location and the reason survive, so a 422 still says which field
        was wrong and why. Only the value goes.
        """
        errors = []
        for error in exc.errors():
            scrubbed = dict(error)
            if any(str(part).lower() in SENSITIVE_FIELDS for part in error.get("loc", ())):
                scrubbed.pop("input", None)
            # `ctx` can carry the value back for some error types, and is only
            # ever bounds and patterns for the rest.
            scrubbed.pop("url", None)
            errors.append(jsonable_encoder(scrubbed))
        return JSONResponse(status_code=422, content={"detail": errors})

    # Order matters: the outermost middleware runs first, so a request from a
    # forged host is rejected before the token is even looked at.
    app.add_middleware(SessionTokenMiddleware, token=token)
    app.add_middleware(HostGuardMiddleware)
    # No CORS middleware, on purpose (architecture §5).

    app.include_router(health_router.router)
    app.include_router(admin_router.router)
    app.include_router(auth_router.router)
    app.include_router(catalog_router.router)
    app.include_router(events_router.router)
    app.include_router(inventory_router.router)
    app.include_router(overrides_router.router)
    app.include_router(register_router.router)
    app.include_router(reports_router.router)
    app.include_router(shifts_router.router)
    app.include_router(sync_router.router)

    _mount_ui(app)

    return app


def _build_sync_engine(
    db: Database,
    outbox: OutboxRepository,
    sessions: SessionStore,
    settings: Settings,
) -> SyncEngine | None:
    """The engine, or None when there is no cloud to sync with.

    A terminal with no Supabase project is a perfectly valid development
    setup, and returning None rather than a disabled engine keeps the
    "is there a cloud?" question in one place instead of inside every method.
    """
    if not settings.cloud_configured:
        return None

    return SyncEngine(
        outbox=outbox,
        pusher=Pusher(
            outbox,
            PayloadBuilder(db, terminal_id=settings.terminal_id),
            base_url=settings.supabase_url,
            anon_key=settings.supabase_anon_key,
            # RLS runs as whoever is signed in, so a push carries the
            # cashier's own rights and nothing more (architecture §11.2).
            token_provider=lambda: sessions.access_token,
        ),
        puller=Puller(
            db,
            base_url=settings.supabase_url,
            anon_key=settings.supabase_anon_key,
            token_provider=lambda: sessions.access_token,
        ),
    )


def _mount_ui(app: FastAPI) -> None:
    """Serve the built React bundle, or a usable stand-in if it isn't built.

    The fallback exists so that ``python -m app.main`` works on a clean
    checkout without a Node toolchain. It is not the product — phase 3 replaces
    it with the real register screen.
    """
    index = UI_DIR / "index.html"
    if index.exists():
        app.mount("/", StaticFiles(directory=str(UI_DIR), html=True), name="ui")
        return

    log.warning("no UI build found at %s — serving the development login page", UI_DIR)

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    def dev_index() -> HTMLResponse:
        return HTMLResponse(DEV_LOGIN_PAGE)
