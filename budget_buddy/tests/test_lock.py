#!/usr/bin/env python3
"""
test_lock.py — Unit tests for budget_buddy/lock.py.

No credentials, no network. Covers the concurrency guard for `enforce`/
`sweep` — see docs/dev/budget-buddy-plan.md, "Concurrency control": a second
acquire must be blocked, release must happen on normal exit and on
exception, and --force must clear a stale lock but never silently on its
own.

Run:
    uv run pytest budget_buddy/tests/test_lock.py
"""
import json

import pytest

import budget_buddy.paths as bb_paths
from budget_buddy import lock
from budget_buddy.paths import lock_file


def test_acquire_and_release_round_trip(tmp_path, monkeypatch):
    monkeypatch.setattr(bb_paths, "OUTPUT_ROOT", tmp_path)
    path = lock_file("default")
    with lock.acquire("default", "test"):
        assert path.exists()
    assert not path.exists()


def test_second_acquire_blocked_while_held(tmp_path, monkeypatch):
    monkeypatch.setattr(bb_paths, "OUTPUT_ROOT", tmp_path)
    with lock.acquire("default", "first"):
        with pytest.raises(lock.LockHeldError):
            with lock.acquire("default", "second"):
                pass


def test_lock_released_even_on_exception(tmp_path, monkeypatch):
    monkeypatch.setattr(bb_paths, "OUTPUT_ROOT", tmp_path)
    path = lock_file("default")
    with pytest.raises(RuntimeError):
        with lock.acquire("default", "first"):
            raise RuntimeError("boom")
    assert not path.exists()


def test_force_clears_stale_lock(tmp_path, monkeypatch):
    monkeypatch.setattr(bb_paths, "OUTPUT_ROOT", tmp_path)
    path = lock_file("default")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"pid": 999999999, "hostname": "nowhere",
                                 "command": "stale", "started_at": "2020-01-01T00:00:00+00:00"}))

    with pytest.raises(lock.LockHeldError):
        with lock.acquire("default", "no-force"):
            pass

    with lock.acquire("default", "with-force", force=True):
        assert path.exists()
    assert not path.exists()


def test_different_instances_do_not_contend(tmp_path, monkeypatch):
    monkeypatch.setattr(bb_paths, "OUTPUT_ROOT", tmp_path)
    with lock.acquire("alpha", "first"):
        with lock.acquire("beta", "second"):
            pass  # no LockHeldError — separate registries, separate locks


def test_lock_held_error_message_includes_diagnostics(tmp_path, monkeypatch):
    monkeypatch.setattr(bb_paths, "OUTPUT_ROOT", tmp_path)
    with lock.acquire("default", "enforce --all"):
        try:
            with lock.acquire("default", "enforce --all"):
                pass
        except lock.LockHeldError as exc:
            msg = str(exc)
            assert "pid=" in msg and "host=" in msg and "--force" in msg
