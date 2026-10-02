"""paths.py — budget-buddy's state directory.

Vendored (no import of cli.paths) so budget_buddy has no dependency on the
parent sumo-ai repo — see docs/dev/budget-buddy-plan.md, "Standalone
portability". Deliberately kept pointed at the same ~/.sumo/output/<instance>/
layout the main `sumo` CLI uses (same SUMO_HOME env var), purely for
directory-convention compatibility if installed alongside it — not an import
dependency.
"""
from __future__ import annotations

import os
from pathlib import Path

SUMO_HOME = Path(os.environ.get("SUMO_HOME", Path.home() / ".sumo"))
OUTPUT_ROOT = SUMO_HOME / "output"


def _norm(instance: str) -> str:
    return (instance or "default").lower().strip()


def instance_root(instance: str = "default") -> Path:
    return OUTPUT_ROOT / _norm(instance)


def budget_buddy_dir(instance: str = "default") -> Path:
    d = instance_root(instance) / "budget-buddy"
    d.mkdir(parents=True, exist_ok=True)
    return d


def registry_file(instance: str = "default") -> Path:
    return budget_buddy_dir(instance) / "registry.json"


def lock_file(instance: str = "default") -> Path:
    return budget_buddy_dir(instance) / "registry.lock"
