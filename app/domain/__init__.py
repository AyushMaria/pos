"""Pure business rules. No I/O, no imports from any other app package.

Enforced in CI by import-linter (see pyproject.toml) and type-checked under
``mypy --strict``. Phase 2 fills this package out with Money, cart, pricing,
tax and the barcode parser; phase 1 establishes only the permission model and
identifier generation, both of which the other layers need from day one.
"""
