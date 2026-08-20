"""Shell bootstrap — architecture §2.

pywebview must own the main thread; uvicorn therefore runs in a daemon thread
started *before* ``webview.start()``. Getting this backwards produces a window
that never paints on macOS, and is the single most common way to lose a day on
this stack.

Sequence:

    single-instance lock  →  pick an ephemeral port  →  mint a session token
    →  start uvicorn in a thread  →  show a splash  →  poll /health
    →  load the register into the same window
"""

from __future__ import annotations

import logging
import logging.handlers
import sys
import threading
import time
from typing import Any

import httpx
import uvicorn

from app.api.dev_ui import SPLASH_PAGE
from app.api.server import build_app
from app.config import Settings, get_settings
from app.data.migrations import MigrationError
from app.security.local_auth import new_session_token, pick_free_port
from app.security.single_instance import AlreadyRunning, SingleInstanceLock

log = logging.getLogger(__name__)


def configure_logging(settings: Settings) -> None:
    """Rotating structured logs to a file, plus the console during development.

    The diagnostics screen in phase 9 reads these files, so they go somewhere
    stable from the start rather than being retrofitted.
    """
    settings.ensure_directories()

    # A Windows console defaults to a legacy code page, and "₹" is not in it.
    # Without this, the first log line carrying a Money value raises
    # UnicodeEncodeError *inside the logger* — on the one platform this ships
    # to. Reconfiguring is preferred to dropping the symbol, so the file and
    # the console agree on what was written.
    stream = sys.stderr
    if hasattr(stream, "reconfigure"):
        stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]

    handlers: list[logging.Handler] = [
        logging.handlers.RotatingFileHandler(
            settings.log_path, maxBytes=2_000_000, backupCount=5, encoding="utf-8"
        ),
        logging.StreamHandler(stream),
    ]
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        handlers=handlers,
        force=True,
    )


def wait_for_health(port: int, token: str, timeout: float) -> dict[str, Any]:
    """Poll the local service until it reports ready. Raises on timeout."""
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    url = f"http://127.0.0.1:{port}/health"

    while time.monotonic() < deadline:
        try:
            response = httpx.get(url, headers={"Authorization": f"Bearer {token}"}, timeout=2.0)
            if response.status_code == 200:
                body: dict[str, Any] = response.json()
                if body.get("status") == "ready":
                    return body
                last_error = RuntimeError(f"service degraded: {body}")
        except httpx.HTTPError as exc:
            last_error = exc
        time.sleep(0.15)

    raise TimeoutError(
        f"the local service did not become ready within {timeout:.0f}s"
    ) from last_error


def _serve(app: object, port: int) -> None:
    uvicorn.run(
        app,  # type: ignore[arg-type]
        host="127.0.0.1",  # never 0.0.0.0 (architecture §5)
        port=port,
        log_config=None,
        access_log=False,
    )


def main() -> int:
    settings = get_settings()
    configure_logging(settings)

    lock = SingleInstanceLock(settings.lock_path)
    try:
        lock.acquire()
    except AlreadyRunning as exc:
        log.error("%s", exc)
        _fatal(str(exc))
        return 1

    try:
        port = pick_free_port()
        token = new_session_token()

        try:
            app = build_app(token=token, settings=settings)
        except MigrationError as exc:
            log.exception("schema migration failed")
            _fatal(f"The till could not start:\n\n{exc}")
            return 1

        threading.Thread(
            target=_serve, args=(app, port), name="uvicorn", daemon=True
        ).start()

        import webview  # imported late so tests never need a display

        window = webview.create_window(
            settings.window_title,
            html=SPLASH_PAGE,
            fullscreen=settings.fullscreen,
            confirm_close=True,
        )

        def open_register() -> None:
            try:
                health = wait_for_health(port, token, settings.health_timeout_seconds)
            except TimeoutError as exc:
                log.error("health gate failed: %s", exc)
                window.load_html(_diagnostic_page(str(exc), settings))
                return
            log.info(
                "service ready: schema v%s, store %s terminal %s",
                health["schema_version"],
                health["store_code"],
                health["terminal_code"],
            )
            window.load_url(f"http://127.0.0.1:{port}/?t={token}")

        # The splash stays up until /health answers; the webview main loop must
        # already be running, so the gate is polled from a worker.
        webview.start(open_register, private_mode=True)
        return 0
    finally:
        lock.release()


def _diagnostic_page(error: str, settings: Settings) -> str:
    return (
        "<html><body style=\"font:15px system-ui;background:#111318;color:#e8eaed;"
        "padding:48px\"><h2>The till could not start</h2>"
        f"<p style='color:#9aa1ad'>{error}</p>"
        f"<p style='color:#6b7280;font-size:13px'>Log file: {settings.log_path}</p>"
        "</body></html>"
    )


def _fatal(message: str) -> None:
    """Report a startup failure when there is no window to report it in."""
    print(message, file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
