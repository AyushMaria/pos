"""Retry timing — architecture §9.2.

A till with no connectivity must not hammer a dead network, and a till that
has just reconnected must not wait five minutes to notice. So: exponential,
capped, and reset the moment anything succeeds.

The cap matters more than the curve. A shop that loses its line for a morning
should be pushing within five minutes of it coming back, and 1s→2s→4s…→300s
reaches that cap in about eight failures — roughly ten minutes of trying.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Backoff:
    first_seconds: float = 1.0
    cap_seconds: float = 300.0
    _current: float = field(default=0.0, init=False)

    @property
    def seconds(self) -> float:
        """How long to wait before the next attempt. Zero when healthy."""
        return self._current

    def fail(self) -> float:
        """Record a failure and return the new wait."""
        self._current = (
            self.first_seconds
            if self._current == 0
            else min(self._current * 2, self.cap_seconds)
        )
        return self._current

    def reset(self) -> None:
        """Anything that succeeded means the network is back."""
        self._current = 0.0

    @property
    def is_backing_off(self) -> bool:
        return self._current > 0
