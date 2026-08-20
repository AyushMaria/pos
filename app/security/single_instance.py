"""Single-instance lock — architecture §14.

Two tills running against one register would duplicate receipt sequences and
double-count the drawer, so the second launch must refuse to start rather than
race the first. The lock is an exclusive OS-level lock on a file, which the
kernel releases even if the process is killed — a PID file would not survive
that honestly.
"""

from __future__ import annotations

import os
from pathlib import Path
from types import TracebackType

if os.name == "nt":  # pragma: no cover - platform split
    import msvcrt
else:  # pragma: no cover - platform split
    import fcntl


class AlreadyRunning(RuntimeError):
    """Another till is already open on this machine."""


#: Windows locks a byte range, and a locked byte cannot even be read. The lock
#: therefore sits well past the start of the file so that the PID at offset 0
#: stays readable — an operator looking at a stuck till needs to know which
#: process is holding the register.
_LOCK_OFFSET = 1024


class SingleInstanceLock:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._handle: object | None = None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.touch(exist_ok=True)
        # r+b, not a+b: append mode ignores seek() on write, which would grow
        # the file by one PID per launch instead of replacing it.
        handle = open(self.path, "r+b")  # noqa: SIM115 - held for process lifetime
        try:
            if os.name == "nt":
                handle.seek(_LOCK_OFFSET)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            handle.close()
            raise AlreadyRunning(
                "A till is already open on this machine. Switch to that window."
            ) from exc

        # Only now that the lock is held is it safe to stamp the file.
        handle.seek(0)
        handle.write(str(os.getpid()).encode().ljust(64))
        handle.flush()
        self._handle = handle

    def release(self) -> None:
        handle = self._handle
        if handle is None:
            return
        self._handle = None
        try:
            if os.name == "nt":
                handle.seek(_LOCK_OFFSET)  # type: ignore[attr-defined]
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)  # type: ignore[attr-defined]
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)  # type: ignore[attr-defined]
        except OSError:
            # Releasing is best-effort; process exit frees the lock regardless.
            pass
        finally:
            handle.close()  # type: ignore[attr-defined]

    def __enter__(self) -> SingleInstanceLock:
        self.acquire()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.release()
