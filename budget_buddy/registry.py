"""registry.py — local JSON state: the sole source of truth for which
/v2/ingestBudgets IDs budget-buddy created and is responsible for sweeping.
Never used for discovery by scanning the live API (see
docs/dev/budget-buddy-plan.md, "TTL / sweep semantics") — only for "which IDs
do I own," with a live marker re-check (naming.verify_marker) immediately
before any mutating call.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

from budget_buddy.paths import registry_file


@dataclass
class RegistryEntry:
    budget_id: str
    scope_name: str
    key: str
    field: str
    budget_type: str
    created_at: str  # ISO 8601
    expires_at: str  # ISO 8601


def _entry_key(scope_name: str, key: str) -> str:
    return f"{scope_name}:{key}"


class Registry:
    """In-memory view of the registry file. Callers are responsible for
    serializing access across processes — see lock.py; this class does not
    lock on its own."""

    def __init__(self, instance: str = "default"):
        self.instance = instance
        self.path: Path = registry_file(instance)
        self._entries: dict[str, RegistryEntry] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text())
        except (json.JSONDecodeError, OSError):
            self._entries = {}
            return
        self._entries = {k: RegistryEntry(**v) for k, v in raw.get("entries", {}).items()}

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"entries": {k: asdict(v) for k, v in self._entries.items()}}
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, indent=2, sort_keys=True))
        tmp.replace(self.path)  # atomic on POSIX

    def get(self, scope_name: str, key: str) -> RegistryEntry | None:
        """Raw lookup, regardless of expiry. Prefer `get_active` for any
        idempotency/"does this already have a budget" decision — an expired
        entry here does not mean a budget is still protecting this key."""
        return self._entries.get(_entry_key(scope_name, key))

    def get_active(self, scope_name: str, key: str, *,
                    now: datetime | None = None) -> RegistryEntry | None:
        """Like `get`, but returns None for an entry whose TTL has already
        passed. Use this for "does this key already have a budget I should
        leave alone" checks — relying on `get` alone is only correct if a
        sweep has *just* removed every expired entry first, which isn't
        guaranteed (e.g. sweep deliberately leaves a marker-mismatched entry
        in place — see sweep_registry in reconcile.py) and would otherwise
        make that key permanently skipped rather than re-enforced."""
        entry = self.get(scope_name, key)
        if entry is None:
            return None
        now = now or datetime.now().astimezone()
        try:
            exp = datetime.fromisoformat(entry.expires_at)
        except ValueError:
            return entry  # malformed expiry: fail safe, treat as still active
        return entry if exp > now else None

    def put(self, entry: RegistryEntry) -> None:
        self._entries[_entry_key(entry.scope_name, entry.key)] = entry

    def remove(self, scope_name: str, key: str) -> None:
        self._entries.pop(_entry_key(scope_name, key), None)

    def all(self, *, scope_name: str | None = None) -> list[RegistryEntry]:
        vals = list(self._entries.values())
        if scope_name is not None:
            vals = [e for e in vals if e.scope_name == scope_name]
        return vals

    def expired(self, *, now: datetime | None = None) -> list[RegistryEntry]:
        now = now or datetime.now().astimezone()
        out = []
        for e in self._entries.values():
            try:
                exp = datetime.fromisoformat(e.expires_at)
            except ValueError:
                continue
            if exp <= now:
                out.append(e)
        return out
