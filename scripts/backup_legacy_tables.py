"""Back up the legacy billing tables before 0000 moves them out of reach.

`supabase db dump` runs `pg_dump` inside a container, so it needs Docker
Desktop and fails at `LegacyDockerRunError` without it. This does the same job
over PostgREST: no Docker, no database password, just the key already in
`.env`.

It exists for one moment in the project's life — the minutes before
`0000_legacy_rehome` moves `public.products` into the `legacy` schema, after
which PostgREST cannot see it at all. That table is the only copy of 32,165
real products and their barcodes.

    python scripts/backup_legacy_tables.py

Writes newline-delimited JSON so that a half-finished file is still a readable
file, and so a diff between two backups is a diff between rows.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import httpx

REPO_ROOT = Path(__file__).resolve().parent.parent

#: Everything the retired system owned. Named rather than discovered, because
#: after 0001 there is a `public.products` again and it is not this one.
TABLES = ("products", "orders")

PAGE = 1000


def env() -> tuple[str, str]:
    """The legacy project's URL and key, from the environment or `.env`.

    These are the *unprefixed* names — the retired system's own credentials,
    which the import scripts already use. The till reads the `POS_`-prefixed
    pair instead and must never be handed this key: it is the `service_role`
    key, and architecture §1.7 forbids bundling it.
    """
    values: dict[str, str] = {}
    dotenv = REPO_ROOT / ".env"
    if dotenv.exists():
        for line in dotenv.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                values[key.strip()] = value.strip()

    url = os.environ.get("SUPABASE_URL") or values.get("SUPABASE_URL", "")
    key = os.environ.get("SUPABASE_KEY") or values.get("SUPABASE_KEY", "")
    if not url or not key:
        sys.exit("SUPABASE_URL and SUPABASE_KEY must be set (see .env.example)")
    return url.rstrip("/"), key


def dump(client: httpx.Client, url: str, headers: dict[str, str], table: str) -> Path:
    out = REPO_ROOT / f"backup-legacy-{table}.jsonl"
    written = 0
    with out.open("w", encoding="utf-8") as handle:
        offset = 0
        while True:
            response = client.get(
                f"{url}/rest/v1/{table}",
                headers={**headers, "Range-Unit": "items",
                         "Range": f"{offset}-{offset + PAGE - 1}"},
                # Ordered by primary key so two backups of an unchanged table
                # are byte-identical, and a diff means something.
                params={"select": "*", "order": "id.asc"},
            )
            response.raise_for_status()
            rows = response.json()
            for row in rows:
                handle.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
            written += len(rows)
            if len(rows) < PAGE:
                break
            offset += PAGE

    print(f"{table}: {written:,} rows -> {out.name} ({out.stat().st_size:,} bytes)")
    return out


def main() -> int:
    url, key = env()
    headers = {"apikey": key, "Authorization": f"Bearer {key}"}
    with httpx.Client(timeout=60.0) as client:
        for table in TABLES:
            dump(client, url, headers, table)
    print("\nCheck the counts against the project before running `supabase db push`.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
