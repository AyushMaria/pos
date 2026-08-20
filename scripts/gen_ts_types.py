"""Generate the TypeScript API contract from the FastAPI schema.

Execution plan §4: freeze the API shapes in phase 1 as Pydantic models and
generate TypeScript from the OpenAPI schema. Without this the Python and React
tracks diverge and a week goes on reconciling them — and the divergence shows
up as a runtime `undefined` in front of a customer, not as a build error.

Emits two files into ui-src/src/core/api/:

    openapi.json   the raw schema, for tooling that wants it
    contract.ts    hand-rolled TS for the shapes this API actually uses

    python scripts/gen_ts_types.py
    python scripts/gen_ts_types.py --check     # exit 1 if stale

The generator covers flat objects of primitives, arrays, nullable fields,
enums and references to other models. If a future endpoint needs a shape this
does not emit, teach this script rather than hand-editing the output.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from app.api.server import build_app  # noqa: E402
from app.config import Settings  # noqa: E402
from app.domain import permissions as perms  # noqa: E402

OUT_DIR = REPO_ROOT / "ui-src" / "src" / "core" / "api"
SCHEMA_PATH = OUT_DIR / "openapi.json"
CONTRACT_PATH = OUT_DIR / "contract.ts"

PRIMITIVES = {
    "string": "string",
    "integer": "number",
    "number": "number",
    "boolean": "boolean",
}


def _ref_name(ref: str) -> str:
    """"#/components/schemas/MoneyOut" -> "MoneyOut"."""
    return ref.rsplit("/", 1)[-1]


def ts_type(schema: dict[str, Any]) -> str:
    # A reference to another model. Pydantic emits these for nested objects,
    # and without this branch every nested field lands as `unknown` — which
    # type-checks and tells the UI nothing.
    if "$ref" in schema:
        return _ref_name(schema["$ref"])
    if "allOf" in schema and len(schema["allOf"]) == 1:
        return ts_type(schema["allOf"][0])
    if "anyOf" in schema:
        parts = [ts_type(option) for option in schema["anyOf"]]
        return " | ".join(dict.fromkeys(parts))
    if schema.get("type") == "null":
        return "null"
    if schema.get("type") == "array":
        return f"{ts_type(schema.get('items', {}))}[]"
    if "enum" in schema:
        return " | ".join(json.dumps(value) for value in schema["enum"])
    if schema.get("type") == "string" and schema.get("format") == "date-time":
        # ISO-8601 over the wire; the UI parses it, the API never sends a
        # Date object.
        return "string"
    return PRIMITIVES.get(schema.get("type", ""), "unknown")


# OpenAPI can only say "array of string" for these; the UI wants the real
# union so that a typo in a permission key is a compile error.
REFINEMENTS = {
    ("SessionResponse", "permissions"): "Permission[]",
    ("SessionResponse", "roles"): "Role[]",
}


def render_interface(name: str, schema: dict[str, Any]) -> str:
    required = set(schema.get("required", []))
    lines = []

    if description := schema.get("description"):
        lines.append(f"/** {description} */")
    lines.append(f"export interface {name} {{")

    for field, definition in schema.get("properties", {}).items():
        if field_doc := definition.get("description"):
            lines.append(f"  /** {field_doc} */")
        optional = "" if field in required else "?"
        rendered = REFINEMENTS.get((name, field), ts_type(definition))
        lines.append(f"  {field}{optional}: {rendered};")

    lines.append("}")
    return "\n".join(lines)


def render() -> tuple[str, str]:
    app = build_app(
        token="schema-generation",
        settings=Settings(supabase_url="", supabase_anon_key=""),
        db=_scratch_db(),
        run_migrations=True,
    )
    schema = app.openapi()

    interfaces = [
        render_interface(name, definition)
        for name, definition in sorted(
            schema.get("components", {}).get("schemas", {}).items()
        )
        # FastAPI's own validation-error models are not part of our contract.
        if not name.startswith(("HTTPValidationError", "ValidationError"))
    ]

    permission_union = "\n  | ".join(
        json.dumps(key) for key in sorted(perms.ALL_PERMISSIONS)
    )
    role_union = " | ".join(json.dumps(role) for role in perms.ROLES)

    contract = f"""// GENERATED FILE, DO NOT EDIT BY HAND.
//
// Sources: app/api/schemas.py (via OpenAPI) and app/domain/permissions.py.
// Regenerate with:  python scripts/gen_ts_types.py
// CI fails if this file and the Python contract disagree.

{chr(10).join(interfaces)}

/**
 * Every permission in the matrix (architecture §11.1).
 *
 * Client-side checks are UX only — Postgres RLS is the security boundary.
 * A <PermissionGate> hiding a button is a courtesy to the cashier, not a
 * control.
 */
export type Permission =
  | {permission_union};

export type Role = {role_union};

export const PERMISSIONS: readonly Permission[] = [
{chr(10).join(f'  {json.dumps(key)},' for key in sorted(perms.ALL_PERMISSIONS))}
] as const;
"""
    return json.dumps(schema, indent=2, sort_keys=True) + "\n", contract


def _scratch_db() -> Any:
    import tempfile

    from app.data.db import Database

    return Database(Path(tempfile.mkdtemp()) / "schema.sqlite3")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()

    schema_json, contract_ts = render()

    if args.check:
        stale = [
            path
            for path, expected in ((SCHEMA_PATH, schema_json), (CONTRACT_PATH, contract_ts))
            if not path.exists() or path.read_text(encoding="utf-8") != expected
        ]
        if stale:
            print(
                "TypeScript contract is out of date with the Pydantic models:\n"
                + "\n".join(f"  {p.relative_to(REPO_ROOT)}" for p in stale)
                + "\nRun: python scripts/gen_ts_types.py",
                file=sys.stderr,
            )
            return 1
        print("TypeScript contract is up to date")
        return 0

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    SCHEMA_PATH.write_text(schema_json, encoding="utf-8")
    CONTRACT_PATH.write_text(contract_ts, encoding="utf-8")
    print(f"wrote {SCHEMA_PATH.relative_to(REPO_ROOT)}")
    print(f"wrote {CONTRACT_PATH.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
