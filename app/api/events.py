"""The event channel — architecture §4, §5.

Commands are HTTP; anything the *service* initiates goes over this WebSocket.
Phase 3 has little to say yet — a heartbeat and connectivity — but the channel
and its security properties are established now so that phase 4's payment
updates and phase 5's `sync.status` have somewhere to arrive.

Two checks matter on the upgrade, and browsers cannot be relied upon to make
them for us:

  * the **session token**, which a browser cannot send as a header on a
    WebSocket upgrade, so it arrives as a query parameter (§5);
  * the **Origin**, because the Host allow-list middleware does not run on the
    upgrade path in the same way, and a page on another origin can otherwise
    open a socket to loopback.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from dataclasses import dataclass, field
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.security.local_auth import verify_session_token, websocket_origin_allowed

log = logging.getLogger(__name__)

router = APIRouter(tags=["events"])

#: Closing codes, so the UI can tell "wrong token" from "server went away".
POLICY_VIOLATION = 1008


@dataclass
class EventHub:
    """Every connected client for this terminal.

    A till has one window, so this is nearly always a single connection — the
    second is the customer-facing display in phase 4.
    """

    clients: set[WebSocket] = field(default_factory=set)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def register(self, socket: WebSocket) -> None:
        async with self._lock:
            self.clients.add(socket)

    async def drop(self, socket: WebSocket) -> None:
        async with self._lock:
            self.clients.discard(socket)

    async def broadcast(self, event: str, payload: dict[str, Any]) -> None:
        """Send to everyone still listening.

        A dead socket is dropped rather than allowed to raise into whatever
        was publishing the event — a printer error must not take down the
        sale that caused it.
        """
        message = json.dumps({"event": event, "payload": payload})
        async with self._lock:
            targets = list(self.clients)

        for socket in targets:
            try:
                await socket.send_text(message)
            except (WebSocketDisconnect, RuntimeError):
                await self.drop(socket)


@router.websocket("/events")
async def events(socket: WebSocket) -> None:
    token: str = socket.app.state.session_token
    presented = socket.query_params.get("t")

    if not websocket_origin_allowed(socket.headers.get("origin")):
        await socket.close(code=POLICY_VIOLATION, reason="bad_origin")
        return

    if not verify_session_token(token, presented):
        await socket.close(code=POLICY_VIOLATION, reason="unauthorized")
        return

    hub: EventHub = socket.app.state.events
    await socket.accept()
    await hub.register(socket)
    log.debug("event client connected")

    try:
        await socket.send_text(
            json.dumps({"event": "connectivity", "payload": {"online": True}})
        )
        while True:
            # The client says nothing; this is how a disconnect is noticed.
            await socket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        with contextlib.suppress(Exception):
            await hub.drop(socket)
        log.debug("event client disconnected")
