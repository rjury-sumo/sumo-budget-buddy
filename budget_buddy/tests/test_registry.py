#!/usr/bin/env python3
"""
test_registry.py — Unit tests for budget_buddy/registry.py.

No credentials, no network. Isolates state under tmp_path by monkeypatching
budget_buddy.paths.OUTPUT_ROOT (which instance_root() resolves against at
call time).

Run:
    uv run pytest budget_buddy/tests/test_registry.py
"""
from datetime import datetime, timedelta, timezone

import budget_buddy.paths as bb_paths
from budget_buddy.registry import Registry, RegistryEntry


def _entry(scope="s1", key="k1", expires_in_hours=1.0):
    now = datetime.now(timezone.utc)
    return RegistryEntry(
        budget_id="BID1", scope_name=scope, key=key, field="_sourceCategory",
        budget_type="dailyVolume", created_at=now.isoformat(),
        expires_at=(now + timedelta(hours=expires_in_hours)).isoformat(),
    )


def test_put_get_remove_round_trip(tmp_path, monkeypatch):
    monkeypatch.setattr(bb_paths, "OUTPUT_ROOT", tmp_path)
    reg = Registry("default")
    reg.put(_entry())
    assert reg.get("s1", "k1") is not None
    reg.remove("s1", "k1")
    assert reg.get("s1", "k1") is None


def test_save_and_reload_persists_entries(tmp_path, monkeypatch):
    monkeypatch.setattr(bb_paths, "OUTPUT_ROOT", tmp_path)
    reg = Registry("default")
    reg.put(_entry(scope="s1", key="k1"))
    reg.save()

    reloaded = Registry("default")
    e = reloaded.get("s1", "k1")
    assert e is not None
    assert e.budget_id == "BID1"


def test_all_filters_by_scope_name(tmp_path, monkeypatch):
    monkeypatch.setattr(bb_paths, "OUTPUT_ROOT", tmp_path)
    reg = Registry("default")
    reg.put(_entry(scope="s1", key="k1"))
    reg.put(_entry(scope="s2", key="k1"))
    assert len(reg.all()) == 2
    assert len(reg.all(scope_name="s1")) == 1


def test_expired_returns_only_past_entries(tmp_path, monkeypatch):
    monkeypatch.setattr(bb_paths, "OUTPUT_ROOT", tmp_path)
    reg = Registry("default")
    reg.put(_entry(scope="s1", key="future", expires_in_hours=1.0))
    reg.put(_entry(scope="s1", key="past", expires_in_hours=-1.0))

    expired = reg.expired(now=datetime.now(timezone.utc))
    assert {e.key for e in expired} == {"past"}


def test_missing_registry_file_yields_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(bb_paths, "OUTPUT_ROOT", tmp_path)
    reg = Registry("default")
    assert reg.all() == []
    assert reg.expired() == []


def test_corrupted_registry_file_yields_empty_not_crash(tmp_path, monkeypatch):
    monkeypatch.setattr(bb_paths, "OUTPUT_ROOT", tmp_path)
    reg = Registry("default")
    reg.path.parent.mkdir(parents=True, exist_ok=True)
    reg.path.write_text("{not valid json")
    reloaded = Registry("default")
    assert reloaded.all() == []


def test_get_active_returns_entry_when_not_expired(tmp_path, monkeypatch):
    monkeypatch.setattr(bb_paths, "OUTPUT_ROOT", tmp_path)
    reg = Registry("default")
    reg.put(_entry(scope="s1", key="k1", expires_in_hours=1.0))
    assert reg.get_active("s1", "k1") is not None


def test_get_active_returns_none_when_expired(tmp_path, monkeypatch):
    # Regression: a stale registry entry (e.g. one sweep deliberately left in
    # place after a marker mismatch) must not count as "still protecting
    # this key" just because it's present in the registry file.
    monkeypatch.setattr(bb_paths, "OUTPUT_ROOT", tmp_path)
    reg = Registry("default")
    reg.put(_entry(scope="s1", key="k1", expires_in_hours=-1.0))
    assert reg.get("s1", "k1") is not None  # raw get still finds it
    assert reg.get_active("s1", "k1") is None  # but it's not "active"


def test_get_active_returns_none_when_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(bb_paths, "OUTPUT_ROOT", tmp_path)
    reg = Registry("default")
    assert reg.get_active("s1", "nope") is None


def test_malformed_entry_is_skipped_not_crashed_on(tmp_path, monkeypatch):
    # Regression: RegistryEntry(**v) for a shape-mismatched entry (missing/
    # extra field from a half-applied schema change or manual edit) used to
    # raise an uncaught TypeError out of Registry(), crashing every command
    # that constructs one. Only the malformed entry should be skipped.
    monkeypatch.setattr(bb_paths, "OUTPUT_ROOT", tmp_path)
    reg = Registry("default")
    reg.put(_entry(scope="s1", key="k1"))
    reg.save()
    import json
    raw = json.loads(reg.path.read_text())
    raw["entries"]["s2:k2"] = {"budget_id": "BID2", "scope_name": "s2"}  # missing required fields
    reg.path.write_text(json.dumps(raw))

    reloaded = Registry("default")
    assert reloaded.get("s1", "k1") is not None
    assert reloaded.get("s2", "k2") is None


def test_instances_are_isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(bb_paths, "OUTPUT_ROOT", tmp_path)
    a = Registry("alpha")
    a.put(_entry(scope="s1", key="k1"))
    a.save()
    b = Registry("beta")
    assert b.all() == []
