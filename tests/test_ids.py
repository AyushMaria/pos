"""UUID v7 — architecture §7.

Client-generated primary keys are what make the sync idempotent, so these
properties are load-bearing: version 7, correct variant, time-sortable, and
unique across a burst.
"""

from __future__ import annotations

import time
import uuid

import pytest
from hypothesis import given
from hypothesis import strategies as st

from app.domain.ids import _fallback_uuid7, new_id, timestamp_ms, uuid7


def test_version_and_variant() -> None:
    value = uuid7()
    assert value.version == 7
    assert value.variant == uuid.RFC_4122


def test_fallback_matches_the_stdlib_layout() -> None:
    value = _fallback_uuid7()
    assert value.version == 7
    assert value.variant == uuid.RFC_4122


def test_timestamp_is_now() -> None:
    before = time.time_ns() // 1_000_000
    value = uuid7()
    after = time.time_ns() // 1_000_000
    assert before <= timestamp_ms(value) <= after


def test_ids_sort_by_creation_time() -> None:
    first = uuid7()
    time.sleep(0.005)
    second = uuid7()
    assert str(first) < str(second)


def test_ids_are_unique_in_a_burst() -> None:
    generated = {new_id() for _ in range(20_000)}
    assert len(generated) == 20_000


def test_timestamp_rejects_other_versions() -> None:
    with pytest.raises(ValueError, match="not a UUID v7"):
        timestamp_ms(uuid.uuid4())


@given(st.integers(min_value=0, max_value=50))
def test_generation_never_produces_a_malformed_id(count: int) -> None:
    for _ in range(count):
        assert uuid.UUID(new_id()).version == 7
