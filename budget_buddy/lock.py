"""lock.py — PID-file concurrency lock for `enforce`/`sweep` (the two commands
that mutate the registry and create/delete real budgets). See
docs/dev/budget-buddy-plan.md, "Concurrency control" for the rationale:
a `enforce`/`sweep` run must never overlap another one against the same
registry, but a crashed prior run must be recoverable via an explicit
`--force`, never an automatic stale-PID guess.
"""
from __future__ import annotations

import json
import logging
import os
import signal
import socket
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from budget_buddy.paths import lock_file

logger = logging.getLogger("budget_buddy.lock")


class LockHeldError(Exception):
    """Raised when another run already holds the lock. Carries the stale/
    active lock's recorded contents for the caller's error message."""

    def __init__(self, info: dict):
        self.info = info
        age_s = _age_seconds(info)
        super().__init__(
            f"budget-buddy is already running (pid={info.get('pid')} "
            f"host={info.get('hostname')} command={info.get('command')!r} "
            f"started {age_s:.0f}s ago). If you're certain this is a stale "
            f"lock from a crashed run, re-run with --force."
        )


def _age_seconds(info: dict) -> float:
    try:
        started = datetime.fromisoformat(info["started_at"])
        return (datetime.now(timezone.utc) - started).total_seconds()
    except (KeyError, ValueError):
        return float("nan")


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, owned by someone else — still "alive"
    return True


def _read_lock(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None


@contextmanager
def acquire(instance: str, command: str, *, force: bool = False):
    """Acquire the registry lock for `instance`, yielding control, and always
    releasing on the way out (including on exception). Raises LockHeldError
    if another live-looking run holds it and `force` is False."""
    path = lock_file(instance)
    path.parent.mkdir(parents=True, exist_ok=True)

    if force and path.exists():
        stale = _read_lock(path)
        logger.warning("--force: clearing existing lock %s (was: %s)", path, stale)
        path.unlink(missing_ok=True)

    info = {
        "pid": os.getpid(),
        "hostname": socket.gethostname(),
        "command": command,
        "started_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        existing = _read_lock(path) or {}
        # Best-effort liveness hint only — never used to auto-clear the lock.
        # A false "it's dead" is worse than requiring a human to pass --force.
        same_host = existing.get("hostname") == socket.gethostname()
        alive = same_host and _pid_alive(existing.get("pid", -1))
        logger.debug("lock held, same_host=%s alive=%s existing=%s", same_host, alive, existing)
        raise LockHeldError(existing)

    with os.fdopen(fd, "w") as f:
        json.dump(info, f)

    def _cleanup(*_a):
        path.unlink(missing_ok=True)

    # os._exit() here skips any `finally` further up the stack (e.g. inside
    # enforce_scope/sweep_registry) — a signal delivered between a successful
    # create_budget() call and the registry.save() that records it can still
    # leave that budget untracked. reconcile.py mitigates this by saving the
    # registry immediately after every single mutation rather than batching
    # at the end of a loop, which minimizes but doesn't eliminate the window.
    # list --all-budgets surfaces such an orphan (its description marker is
    # set before the create call) even without a registry entry.
    prev_term = signal.signal(signal.SIGTERM, lambda *a: (_cleanup(), os._exit(143)))
    prev_int = signal.signal(signal.SIGINT, lambda *a: (_cleanup(), os._exit(130)))
    try:
        yield
    finally:
        signal.signal(signal.SIGTERM, prev_term)
        signal.signal(signal.SIGINT, prev_int)
        path.unlink(missing_ok=True)
