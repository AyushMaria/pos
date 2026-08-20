"""Find argon2id parameters worth about 100 ms on this machine.

Run it on the actual till, not on a developer laptop. A PIN check happens with
a customer waiting — at the login screen, and again every time a supervisor
authorises an override — so 500 ms is felt and 2 s is unusable. Too cheap and
a four-digit PIN falls to an offline attack on a stolen terminal database.

    python scripts/tune_argon2.py
    python scripts/tune_argon2.py --target-ms 100 --memory-mib 64

Put the printed values in the environment (POS_ARGON2_*). The same parameters
must be used by the authenticate-pin Edge Function, or a hash minted in the
cloud will verify slowly — though still correctly — on the terminal.
"""

from __future__ import annotations

import argparse
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from argon2 import PasswordHasher
from argon2.low_level import Type

SAMPLE_PIN = "849163"


def measure(time_cost: int, memory_kib: int, parallelism: int, rounds: int = 5) -> float:
    hasher = PasswordHasher(
        time_cost=time_cost,
        memory_cost=memory_kib,
        parallelism=parallelism,
        hash_len=32,
        salt_len=16,
        type=Type.ID,
    )
    hasher.hash(SAMPLE_PIN)  # warm caches; the first call is not representative
    timings = []
    for _ in range(rounds):
        start = time.perf_counter()
        hasher.hash(SAMPLE_PIN)
        timings.append((time.perf_counter() - start) * 1000)
    return statistics.median(timings)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target-ms", type=float, default=100.0)
    parser.add_argument("--memory-mib", type=int, default=64)
    parser.add_argument("--parallelism", type=int, default=4)
    parser.add_argument("--max-time-cost", type=int, default=12)
    args = parser.parse_args()

    memory_kib = args.memory_mib * 1024
    # ASCII only: this prints to a Windows console with a cp1252 code page.
    print(f"target {args.target_ms:.0f} ms | {args.memory_mib} MiB | p={args.parallelism}\n")

    best: tuple[int, float] | None = None
    for time_cost in range(1, args.max_time_cost + 1):
        elapsed = measure(time_cost, memory_kib, args.parallelism)
        marker = ""
        if best is None or abs(elapsed - args.target_ms) < abs(best[1] - args.target_ms):
            best = (time_cost, elapsed)
            marker = "  <-- closest so far"
        print(f"  t={time_cost:<3} {elapsed:7.1f} ms{marker}")
        if elapsed > args.target_ms * 2:
            break

    assert best is not None
    time_cost, elapsed = best
    print(f"\nClosest to target: t={time_cost} at {elapsed:.1f} ms\n")
    print("Set these in the environment:")
    print(f"  POS_ARGON2_TIME_COST={time_cost}")
    print(f"  POS_ARGON2_MEMORY_COST_KIB={memory_kib}")
    print(f"  POS_ARGON2_PARALLELISM={args.parallelism}")

    if elapsed < args.target_ms / 2:
        print(
            "\nWarning: even the slowest setting tried is well under target. "
            "Raise --memory-mib before raising the time cost — memory hardness "
            "is what actually resists a GPU."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
