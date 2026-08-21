"""What goes on the wire — architecture §9.2, §17.

One envelope shape for every entity, carrying the schema version it was built
against. The version is the point:

> *Schema migration on offline terminals — forward-only local migrations;
> payloads carry a schema version, server rejects mismatch with a forced-update
> prompt.* (architecture §17)

A terminal that has been offline for a fortnight may be two migrations behind
the Postgres it is pushing into. Without a version it pushes a payload missing
a column the server now requires, the insert fails on a constraint, and the
sale quarantines with an error about a column name — which tells a shopkeeper
nothing. With one, the server can say "this terminal needs updating" and the
till can say that to the person standing at it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

#: Bumped when a migration changes what a payload contains. It tracks the
#: *local* schema version (SQLite `user_version`), because that is what
#: determines the shape of what this terminal can build.
SCHEMA_VERSION = 3


@dataclass(frozen=True, slots=True)
class Envelope:
    """One record, addressed and versioned.

    `id` is the client-generated UUID v7 the record already carries. The
    server inserts with `ON CONFLICT (id) DO NOTHING`, so re-sending after a
    dropped acknowledgement is a no-op rather than a second sale (§9.2).
    """

    entity: str
    op: str
    id: str
    client_seq: int
    data: dict[str, Any]
    schema_version: int = SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "entity": self.entity,
            "op": self.op,
            "id": self.id,
            "client_seq": self.client_seq,
            "data": self.data,
        }
