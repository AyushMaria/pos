"""Re-mint every committed argon2 hash at the current parameters.

An argon2 hash carries the cost it was made with, so changing
`POS_ARGON2_*` changes nothing about an existing hash — it will keep
verifying, at the old price, forever. Anything committed to this repository
has to be re-minted deliberately, and this is the script that does it.

    python scripts/remint_pin_hashes.py            # rewrite in place
    python scripts/remint_pin_hashes.py --check    # exit 1 if stale

Two files, for two different reasons:

**`supabase/seed/seed.sql`** carries the three development PINs. Left alone, a
freshly seeded project starts every account at the old cost and pays it until
someone signs in.

**`supabase/functions/authenticate-pin/index.ts`** carries a decoy hash, used
when the employee code is unknown so that a missing code and a wrong PIN cost
the same wall-clock time. It only works if it carries the *same parameters* as
a real hash. It did not: the decoy said `t=2` while real hashes said `t=12`, so
an unknown code answered roughly six times faster and employee codes could be
enumerated with a stopwatch. `tests/test_pins.py` fails if they drift again.

Real staff PINs are not here and cannot be. Those migrate themselves:
`authenticate-pin` re-mints any hash it verifies that was made at other
parameters, which is the only moment anyone holds the plaintext.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from app.config import get_settings  # noqa: E402
from app.security.pins import hash_pin  # noqa: E402

SEED = REPO_ROOT / "supabase" / "seed" / "seed.sql"
EDGE = REPO_ROOT / "supabase" / "functions" / "authenticate-pin" / "index.ts"

#: The development credentials, published in the README and in the seed's own
#: comment. Not secrets; they exist so the till can be demonstrated.
DEV_PINS = {"C001": "4913", "S001": "7261", "M001": "5820"}

#: Verified against, never matched. It only has to *cost* the same as a real
#: PIN, so the digits are arbitrary — but `hash_pin` enforces the PIN policy on
#: whatever it is given, so arbitrary still means twelve non-repeating digits.
#:
#: A caller cannot get in by guessing it: the decoy is only ever reached when
#: the employee code is unknown, and that branch rejects on `!employee` before
#: it looks at whether the hash matched.
DECOY_SUBJECT = "849163520741"


def params_of(settings) -> str:  # type: ignore[no-untyped-def]
    return (
        f"m={settings.argon2_memory_cost_kib},"
        f"t={settings.argon2_time_cost},"
        f"p={settings.argon2_parallelism}"
    )


def remint_seed(text: str, settings, minted: dict[str, str]) -> str:  # type: ignore[no-untyped-def]
    """Replace each employee's pin_hash, keyed on the code beside it."""
    for code in DEV_PINS:
        fresh = minted[code]
        # The seed lists ('<uuid>', 'C001', 'Anita Rao', '<hash>', 'active').
        # Anchor on the employee code so the three cannot be swapped.
        pattern = re.compile(
            r"('" + re.escape(code) + r"',\s*'[^']*',\s*\n?\s*')\$argon2id\$[^']*(')",
            re.MULTILINE,
        )
        text, count = pattern.subn(lambda m: m.group(1) + fresh + m.group(2), text)
        if count != 1:
            raise SystemExit(
                f"expected exactly one pin_hash for {code} in {SEED.name}, found {count}"
            )
    return text


def remint_edge(text: str, settings, decoy: str) -> str:  # type: ignore[no-untyped-def]
    """Replace the decoy hash and the parameter constants beside it."""
    pattern = re.compile(
        r'const DECOY_HASH = "[^"]*"(?:\s*\+\s*\n?\s*"[^"]*")*;',
        re.MULTILINE,
    )
    text, count = pattern.subn(f'const DECOY_HASH = "{decoy}";', text)
    if count != 1:
        raise SystemExit(f"expected exactly one DECOY_HASH in {EDGE.name}, found {count}")

    constants = (
        f"const ARGON2 = {{ memoryKiB: {settings.argon2_memory_cost_kib}, "
        f"timeCost: {settings.argon2_time_cost}, "
        f"parallelism: {settings.argon2_parallelism} }} as const;"
    )
    text, count = re.subn(r"const ARGON2 = \{[^}]*\} as const;", constants, text)
    if count != 1:
        raise SystemExit(f"expected exactly one ARGON2 constant in {EDGE.name}")

    return text


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="verify without writing")
    args = parser.parse_args()

    settings = get_settings()
    wanted = params_of(settings)

    if args.check:
        # A hash is salted, so it cannot be compared to a freshly minted one.
        # What can be checked is the cost baked into what is committed.
        stale = []
        for path in (SEED, EDGE):
            text = path.read_text(encoding="utf-8")
            for found in re.findall(r"\$argon2id\$v=19\$(m=\d+,t=\d+,p=\d+)\$", text):
                if found != wanted:
                    stale.append(f"{path.name}: {found}, want {wanted}")
        if stale:
            print("committed hashes were minted at other parameters:", file=sys.stderr)
            for line in stale:
                print(f"  {line}", file=sys.stderr)
            print("\n  python scripts/remint_pin_hashes.py", file=sys.stderr)
            return 1
        print(f"committed hashes are at {wanted}")
        return 0

    print(f"minting at {wanted} — this costs one hash each, so it is not instant")

    # Mint everything, and compute both replacements, before writing anything.
    # A crash halfway used to leave the seed re-minted and the Edge Function
    # untouched — the two files that most need to agree, disagreeing, with the
    # error message pointing at neither.
    minted = {code: hash_pin(pin, settings) for code, pin in DEV_PINS.items()}
    decoy = hash_pin(DECOY_SUBJECT, settings)

    seed_text = remint_seed(SEED.read_text(encoding="utf-8"), settings, minted)
    edge_text = remint_edge(EDGE.read_text(encoding="utf-8"), settings, decoy)

    SEED.write_text(seed_text, encoding="utf-8", newline="")
    print(f"  {SEED.relative_to(REPO_ROOT)}  ({len(DEV_PINS)} PINs)")

    EDGE.write_text(edge_text, encoding="utf-8", newline="")
    print(f"  {EDGE.relative_to(REPO_ROOT)}  (decoy + constants)")

    print(
        "\nNow: apply the seed changes to any live project, and redeploy the\n"
        "function — `supabase functions deploy authenticate-pin`. Real staff\n"
        "migrate themselves on next sign-in."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
