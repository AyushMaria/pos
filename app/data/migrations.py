"""Forward-only migration runner — architecture §7.

Numbered SQL files applied inside a transaction at startup, with SQLite's
``user_version`` as the marker. Alembic is overkill for an embedded,
single-writer database, and a terminal that has been offline for a fortnight
needs migrations that are trivially auditable.

Rules:
  * Files are ``NNN_name.sql``, numbered from 001 with no gaps.
  * A file that has been released is never edited. Fix forward.
  * The whole run is one transaction per file; a half-applied schema is worse
    than no upgrade at all.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path

from app.data.db import Database

log = logging.getLogger(__name__)

MIGRATIONS_DIR = Path(__file__).parent / "migrations"
_FILENAME = re.compile(r"^(\d{3})_([a-z0-9_]+)\.sql$")


class MigrationError(RuntimeError):
    pass


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    path: Path

    @property
    def sql(self) -> str:
        return self.path.read_text(encoding="utf-8")


def discover(directory: Path = MIGRATIONS_DIR) -> list[Migration]:
    """All migrations on disk, ordered, with numbering validated."""
    migrations: list[Migration] = []
    for path in sorted(directory.glob("*.sql")):
        match = _FILENAME.match(path.name)
        if match is None:
            raise MigrationError(
                f"{path.name}: migrations must be named NNN_lower_snake_case.sql"
            )
        migrations.append(
            Migration(version=int(match.group(1)), name=match.group(2), path=path)
        )

    for expected, migration in enumerate(migrations, start=1):
        if migration.version != expected:
            raise MigrationError(
                f"migration numbering has a gap or duplicate at {migration.path.name}: "
                f"expected {expected:03d}, found {migration.version:03d}"
            )
    return migrations


def latest_version(directory: Path = MIGRATIONS_DIR) -> int:
    migrations = discover(directory)
    return migrations[-1].version if migrations else 0


def migrate(db: Database, directory: Path = MIGRATIONS_DIR) -> int:
    """Apply every migration newer than ``user_version``. Returns the new version."""
    current = db.user_version
    target = latest_version(directory)

    if current > target:
        # The database was written by a newer build. Downgrading silently would
        # corrupt it; phase 9 turns this into a forced-update prompt.
        raise MigrationError(
            f"database schema is version {current} but this build only knows "
            f"{target}. Update the application."
        )

    for migration in discover(directory):
        if migration.version <= current:
            continue
        log.info("applying migration %03d_%s", migration.version, migration.name)
        # The version bump rides inside the same transaction as the DDL, so a
        # crash mid-migration leaves user_version pointing at the last schema
        # that actually landed. PRAGMA takes no bound parameter; the value is
        # an int parsed out of the filename, not user input.
        db.execute_script(f"{migration.sql}\nPRAGMA user_version = {migration.version:d};")
        current = migration.version

    return current
