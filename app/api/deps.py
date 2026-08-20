"""FastAPI dependencies.

The permission dependency here is the second of the three enforcement points
(architecture §11.1): React hides the control, this rejects the request, and
RLS rejects the sync. Only the third is security — but this one is what makes
the audit trail honest and the error messages useful.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status

from app.config import Settings
from app.data.db import Database
from app.data.repositories.catalog import CatalogRepository
from app.domain.identity import Session, utcnow
from app.services.auth_service import AuthService, SessionStore
from app.services.cart_service import CartService
from app.services.sale_service import SaleService


def get_settings_dep(request: Request) -> Settings:
    settings: Settings = request.app.state.settings
    return settings


def get_db(request: Request) -> Database:
    db: Database = request.app.state.db
    return db


def get_session_store(request: Request) -> SessionStore:
    store: SessionStore = request.app.state.sessions
    return store


def get_auth_service(request: Request) -> AuthService:
    service: AuthService = request.app.state.auth_service
    return service


def get_catalog_repository(request: Request) -> CatalogRepository:
    repo: CatalogRepository = request.app.state.catalog
    return repo


def get_cart_service(request: Request) -> CartService:
    service: CartService = request.app.state.cart_service
    return service


def get_sale_service(request: Request) -> SaleService:
    service: SaleService = request.app.state.sale_service
    return service


def current_session(
    sessions: Annotated[SessionStore, Depends(get_session_store)],
) -> Session:
    session = sessions.current
    if session is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "not_signed_in")
    return session


CurrentSession = Annotated[Session, Depends(current_session)]


def require(permission: str) -> Callable[..., Session]:
    """Dependency factory: reject the request unless the session may do this."""

    def dep(session: CurrentSession) -> Session:
        if not session.allows(permission, now=utcnow()):
            raise HTTPException(status.HTTP_403_FORBIDDEN, "permission_denied")
        return session

    return dep
