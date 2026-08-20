"""UUID v7 identifiers — architecture §7.

Time-sortable and collision-free across terminals, which is what makes
client-generated primary keys safe to push into Postgres with
``ON CONFLICT (id) DO NOTHING``.

Python 3.14 ships ``uuid.uuid7``. On older interpreters this module generates
the same layout (RFC 9562 §5.7) rather than pulling in a dependency: 48-bit
big-endian Unix milliseconds, 4-bit version, 12 bits of randomness, 2-bit
variant, 62 bits of randomness.
"""

from __future__ import annotations

import os
import time
import uuid
from collections.abc import Callable

_stdlib_uuid7: Callable[[], uuid.UUID] | None = getattr(uuid, "uuid7", None)


def _fallback_uuid7() -> uuid.UUID:
    unix_ms = time.time_ns() // 1_000_000
    rand = int.from_bytes(os.urandom(10), "big")

    rand_a = rand >> 62 & 0xFFF  # 12 bits
    rand_b = rand & ((1 << 62) - 1)  # 62 bits

    value = (unix_ms & ((1 << 48) - 1)) << 80
    value |= 0x7 << 76  # version 7
    value |= rand_a << 64
    value |= 0b10 << 62  # variant 10xx
    value |= rand_b
    return uuid.UUID(int=value)


def uuid7() -> uuid.UUID:
    """A time-sortable UUID v7."""
    if _stdlib_uuid7 is not None:
        return _stdlib_uuid7()
    return _fallback_uuid7()


def new_id() -> str:
    """A new primary key, as the canonical hyphenated string SQLite stores."""
    return str(uuid7())


def timestamp_ms(value: uuid.UUID) -> int:
    """Unix milliseconds embedded in a v7 UUID. Raises on any other version."""
    if value.version != 7:
        raise ValueError(f"not a UUID v7: {value}")
    return value.int >> 80
