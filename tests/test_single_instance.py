"""Single-instance lock — architecture §14.

Two tills on one register would duplicate receipt sequences and double-count
the drawer, so the second launch has to refuse rather than race.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.security.single_instance import AlreadyRunning, SingleInstanceLock


def test_first_launch_acquires(tmp_path: Path) -> None:
    lock = SingleInstanceLock(tmp_path / "pos.lock")
    lock.acquire()
    assert (tmp_path / "pos.lock").exists()
    lock.release()


def test_second_launch_is_refused(tmp_path: Path) -> None:
    first = SingleInstanceLock(tmp_path / "pos.lock")
    first.acquire()

    second = SingleInstanceLock(tmp_path / "pos.lock")
    with pytest.raises(AlreadyRunning, match="already open"):
        second.acquire()

    first.release()


def test_the_lock_is_reusable_after_release(tmp_path: Path) -> None:
    """A clean shutdown must not leave the till unable to reopen."""
    first = SingleInstanceLock(tmp_path / "pos.lock")
    first.acquire()
    first.release()

    second = SingleInstanceLock(tmp_path / "pos.lock")
    second.acquire()
    second.release()


def test_context_manager_releases(tmp_path: Path) -> None:
    with SingleInstanceLock(tmp_path / "pos.lock"):
        pass

    SingleInstanceLock(tmp_path / "pos.lock").acquire()


def test_the_lock_records_the_pid(tmp_path: Path) -> None:
    """So an operator can see which process is holding the register."""
    import os

    lock = SingleInstanceLock(tmp_path / "pos.lock")
    lock.acquire()
    assert (tmp_path / "pos.lock").read_bytes().strip() == str(os.getpid()).encode()
    lock.release()


def test_release_is_safe_without_acquire(tmp_path: Path) -> None:
    SingleInstanceLock(tmp_path / "pos.lock").release()


def test_directory_is_created(tmp_path: Path) -> None:
    lock = SingleInstanceLock(tmp_path / "nested" / "deeper" / "pos.lock")
    lock.acquire()
    lock.release()
