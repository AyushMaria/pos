"""Application assembly.

Everything the routers need is built once here and hung on ``app.state``: the
database, the session store, the auth service. Nothing constructs its own
dependencies at request time, which is what makes the whole API testable
against a temp SQLite file with no window and no network.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from app import __version__
from app.api import auth as auth_router
from app.api import catalog as catalog_router
from app.api import events as events_router
from app.api import health as health_router
from app.api import register as register_router
from app.api import reports as reports_router
from app.api import sync as sync_router
from app.api.dev_ui import DEV_LOGIN_PAGE
from app.config import Settings, get_settings
from app.data.db import Database
from app.data.migrations import migrate
from app.data.repositories.catalog import CatalogRepository
from app.data.repositories.outbox import OutboxRepository
from app.data.repositories.sales import SalesRepository
from app.data.repositories.terminal import TerminalRepository
from app.data.repositories.users import CachedUserRepository
from app.security.local_auth import HostGuardMiddleware, SessionTokenMiddleware
from app.services.auth_service import AuthService, SessionStore
from app.services.cart_service import CartService
from app.services.payment_providers import default_registry
from app.services.sale_service import SaleService
from app.services.supabase_auth import SupabaseAuthClient
from app.sync.engine import SyncEngine
from app.sync.payloads import PayloadBuilder
from app.sync.puller import Puller
from app.sync.pusher import Pusher

log = logging.getLogger(__name__)

UI_DIR = Path(__file__).resolve().parent.parent / "ui"


def build_app(
    *,
    token: str,
    settings: Settings | None = None,
    db: Database | None = None,
    run_migrations: bool = True,
) -> FastAPI:
    settings = settings or get_settings()
    if db is None:
        settings.ensure_directories()
        db = Database(settings.db_path)
    if run_migrations:
        migrate(db)

    users = CachedUserRepository(db)
    catalog = CatalogRepository(db)
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

    @asynccontextmanager
    async def lifespan(instance: FastAPI) -> AsyncIterator[None]:
        # Started inside the running loop, not at construction: the engine
        # owns an asyncio task and a wake event, and both need a loop to
        # belong to. A test client that never enters the context therefore
        # never starts a background task, which is what keeps the suite quiet.
        if engine is not None:
            engine.publish = instance.state.events.broadcast
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
    cart_service = CartService(catalog)
    app.state.cart_service = cart_service
    app.state.sale_service = SaleService(
        carts=cart_service,
        sales=sales,
        terminal=terminal,
        providers=default_registry(),
        settings=settings,
    )
    app.state.auth_service = AuthService(
        users=users,
        sessions=sessions,
        cloud=cloud,
        store_code=settings.store_code,
        terminal_code=settings.terminal_code,
        settings=settings,
    )

    # Order matters: the outermost middleware runs first, so a request from a
    # forged host is rejected before the token is even looked at.
    app.add_middleware(SessionTokenMiddleware, token=token)
    app.add_middleware(HostGuardMiddleware)
    # No CORS middleware, on purpose (architecture §5).

    app.include_router(health_router.router)
    app.include_router(auth_router.router)
    app.include_router(catalog_router.router)
    app.include_router(events_router.router)
    app.include_router(register_router.router)
    app.include_router(reports_router.router)
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
