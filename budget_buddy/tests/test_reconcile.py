#!/usr/bin/env python3
"""
test_reconcile.py — Unit tests for budget_buddy/reconcile.py.

No credentials, no network — SearchClient/IngestBudgetsV2Client are replaced
with small fakes implementing just the methods reconcile.py calls. Covers the
code-review regressions fixed here:

  - enforce_scope's idempotency check must ignore expired registry entries
    (registry.get_active, not get) — otherwise a key whose old budget expired
    but wasn't swept (e.g. a marker mismatch) never gets re-enforced.
  - enforce_scope must refuse to create a budget with a blank/bare-wildcard
    scope value, independent of whatever produced that key.
  - sweep_registry must only deregister a budget on a confirmed 404 — any
    other failure (auth, network, 5xx) must leave the registry entry in
    place for retry, not silently abandon tracking of a possibly-live budget.
  - sweep's scope_names filter actually filters to the given scopes.

Run:
    uv run pytest budget_buddy/tests/test_reconcile.py
"""
from datetime import datetime, timedelta, timezone

import budget_buddy.paths as bb_paths
from budget_buddy import naming, reconcile
from budget_buddy.budgets import BudgetAPIError, IngestBudget
from budget_buddy.config import ScopeConfig
from budget_buddy.registry import Registry, RegistryEntry
from budget_buddy.search import SearchResult


def _row_record(key: str, num_bytes: int, events: int = 1) -> dict:
    gbytes = num_bytes / (1024 ** 3)
    return {"map": {"sourcecategory": key, "gbytes": str(gbytes), "events": str(events)}}


class FakeSearchClient:
    def __init__(self, records: list[dict]):
        self.records = records

    def run_aggregate(self, query, from_ms, to_ms, **kw):
        return SearchResult(job_id="FAKE", total=len(self.records), records=self.records)


class FakeBudgetsClient:
    def __init__(self):
        self.budgets: dict[str, IngestBudget] = {}
        self._next = 1
        self.get_budget_error: Exception | None = None

    def create_budget(self, *, name, scope, capacity_bytes, action, budget_type,
                       description, timezone, reset_time, audit_threshold):
        bid = f"B{self._next}"
        self._next += 1
        b = IngestBudget(
            id=bid, name=name, scope=scope, capacity_bytes=capacity_bytes, action=action,
            budget_type=budget_type, description=description, timezone=timezone,
            reset_time=reset_time, audit_threshold=audit_threshold, usage_bytes=0,
            usage_status="Normal", created_at="2026-01-01T00:00:00Z",
            modified_at="2026-01-01T00:00:00Z",
        )
        self.budgets[bid] = b
        return b

    def get_budget(self, budget_id):
        if self.get_budget_error is not None:
            raise self.get_budget_error
        if budget_id not in self.budgets:
            raise BudgetAPIError(404, "not found", "get")
        return self.budgets[budget_id]

    def delete_budget(self, budget_id):
        existed = budget_id in self.budgets
        self.budgets.pop(budget_id, None)
        return existed


def _scope(**overrides) -> ScopeConfig:
    defaults = dict(
        name="s1", field="_sourceCategory", scope="_sourceCategory=*cloudtrail*",
        threshold_bytes=1000, mode="per_value", max_budgets=50,
    )
    defaults.update(overrides)
    return ScopeConfig(**defaults)


def _registry_entry(scope_name, key, budget_id, expires_in_hours):
    now = datetime.now(timezone.utc)
    return RegistryEntry(
        budget_id=budget_id, scope_name=scope_name, key=key, field="_sourceCategory",
        budget_type="dailyVolume", created_at=now.isoformat(),
        expires_at=(now + timedelta(hours=expires_in_hours)).isoformat(),
    )


# ---------------------------------------------------------------------------
# enforce_scope
# ---------------------------------------------------------------------------

def test_enforce_scope_creates_and_persists_to_disk(tmp_path, monkeypatch):
    monkeypatch.setattr(bb_paths, "OUTPUT_ROOT", tmp_path)
    registry = Registry("default")
    search = FakeSearchClient([_row_record("aws/x", 2000)])
    budgets = FakeBudgetsClient()

    results = reconcile.enforce_scope(_scope(), search, budgets, registry)

    assert [r.action for r in results] == ["created"]
    assert len(budgets.budgets) == 1
    # Persisted immediately (not just in the in-memory object) — a freshly
    # loaded Registry must see it too.
    reloaded = Registry("default")
    assert reloaded.get_active("s1", "aws/x") is not None


def test_enforce_scope_ignores_rows_under_threshold(tmp_path, monkeypatch):
    monkeypatch.setattr(bb_paths, "OUTPUT_ROOT", tmp_path)
    registry = Registry("default")
    search = FakeSearchClient([_row_record("aws/x", 10)])  # well under threshold_bytes=1000
    budgets = FakeBudgetsClient()

    results = reconcile.enforce_scope(_scope(), search, budgets, registry)

    assert results == []
    assert budgets.budgets == {}


def test_enforce_scope_respects_max_budgets_cap(tmp_path, monkeypatch):
    monkeypatch.setattr(bb_paths, "OUTPUT_ROOT", tmp_path)
    registry = Registry("default")
    search = FakeSearchClient([_row_record("a", 3000), _row_record("b", 2000)])
    budgets = FakeBudgetsClient()

    results = reconcile.enforce_scope(_scope(max_budgets=1), search, budgets, registry)

    by_action = {r.key: r.action for r in results}
    assert by_action == {"a": "created", "b": "capped_uncovered"}
    assert len(budgets.budgets) == 1


def test_enforce_scope_skips_when_active_entry_exists(tmp_path, monkeypatch):
    monkeypatch.setattr(bb_paths, "OUTPUT_ROOT", tmp_path)
    registry = Registry("default")
    registry.put(_registry_entry("s1", "aws/x", "B-OLD", expires_in_hours=1.0))
    registry.save()
    search = FakeSearchClient([_row_record("aws/x", 2000)])
    budgets = FakeBudgetsClient()

    results = reconcile.enforce_scope(_scope(), search, budgets, registry)

    assert [r.action for r in results] == ["skipped_existing"]
    assert budgets.budgets == {}  # no new budget created


def test_enforce_scope_recreates_when_existing_entry_expired(tmp_path, monkeypatch):
    # Regression: an expired-but-not-yet-swept registry entry (e.g. sweep
    # left it behind after a marker mismatch) must NOT block re-enforcement.
    monkeypatch.setattr(bb_paths, "OUTPUT_ROOT", tmp_path)
    registry = Registry("default")
    registry.put(_registry_entry("s1", "aws/x", "B-OLD", expires_in_hours=-1.0))
    registry.save()
    search = FakeSearchClient([_row_record("aws/x", 2000)])
    budgets = FakeBudgetsClient()

    results = reconcile.enforce_scope(_scope(), search, budgets, registry)

    assert [r.action for r in results] == ["created"]
    assert len(budgets.budgets) == 1


def test_enforce_scope_rejects_global_scope_key(tmp_path, monkeypatch):
    # Defense-in-depth: even if something upstream of enforce_scope ever
    # produced a bare "*" key (parse_rows itself now refuses to), enforce_scope
    # must still never call create_budget with a global scope expression.
    monkeypatch.setattr(bb_paths, "OUTPUT_ROOT", tmp_path)
    registry = Registry("default")
    budgets = FakeBudgetsClient()
    bad_row = reconcile.EvaluationRow(
        scope_name="s1", key="*", field="_sourceCategory", bytes=5000, gb=0.0, events=1,
        threshold_bytes=1000, over_threshold=True, window="today", tz="UTC",
    )
    monkeypatch.setattr(reconcile, "evaluate_scope", lambda scope, client, now=None: [bad_row])

    results = reconcile.enforce_scope(_scope(), FakeSearchClient([]), budgets, registry)

    assert [r.action for r in results] == ["skipped_invalid_scope"]
    assert budgets.budgets == {}


def test_enforce_scope_dry_run_does_not_mutate_registry_or_create(tmp_path, monkeypatch):
    monkeypatch.setattr(bb_paths, "OUTPUT_ROOT", tmp_path)
    registry = Registry("default")
    search = FakeSearchClient([_row_record("aws/x", 2000)])
    budgets = FakeBudgetsClient()

    results = reconcile.enforce_scope(_scope(), search, budgets, registry, dry_run=True)

    assert [r.action for r in results] == ["created"]
    assert budgets.budgets == {}
    assert registry.get_active("s1", "aws/x") is None


# ---------------------------------------------------------------------------
# sweep_registry
# ---------------------------------------------------------------------------

def test_sweep_registry_404_removes_entry_and_persists(tmp_path, monkeypatch):
    monkeypatch.setattr(bb_paths, "OUTPUT_ROOT", tmp_path)
    registry = Registry("default")
    registry.put(_registry_entry("s1", "k1", "B1", expires_in_hours=-1.0))
    registry.save()
    budgets = FakeBudgetsClient()  # B1 not in budgets.budgets -> get_budget raises 404

    results = reconcile.sweep_registry(registry, budgets)

    assert [r.action for r in results] == ["swept_already_gone"]
    reloaded = Registry("default")
    assert reloaded.get("s1", "k1") is None


def test_sweep_registry_non_404_api_error_keeps_entry(tmp_path, monkeypatch):
    # Regression: only a confirmed 404 may deregister — anything else must
    # leave the entry for retry, since the budget may well still be live.
    monkeypatch.setattr(bb_paths, "OUTPUT_ROOT", tmp_path)
    registry = Registry("default")
    registry.put(_registry_entry("s1", "k1", "B1", expires_in_hours=-1.0))
    registry.save()
    budgets = FakeBudgetsClient()
    budgets.get_budget_error = BudgetAPIError(500, "internal error", "get")

    results = reconcile.sweep_registry(registry, budgets)

    assert [r.action for r in results] == ["error"]
    reloaded = Registry("default")
    assert reloaded.get("s1", "k1") is not None  # still tracked for next cycle


def test_sweep_registry_generic_exception_keeps_entry(tmp_path, monkeypatch):
    # Same as above but for a non-BudgetAPIError failure (network/transport).
    monkeypatch.setattr(bb_paths, "OUTPUT_ROOT", tmp_path)
    registry = Registry("default")
    registry.put(_registry_entry("s1", "k1", "B1", expires_in_hours=-1.0))
    registry.save()
    budgets = FakeBudgetsClient()
    budgets.get_budget_error = ConnectionError("network is down")

    results = reconcile.sweep_registry(registry, budgets)

    assert [r.action for r in results] == ["error"]
    reloaded = Registry("default")
    assert reloaded.get("s1", "k1") is not None


def test_sweep_registry_marker_mismatch_skips_and_keeps_entry(tmp_path, monkeypatch):
    monkeypatch.setattr(bb_paths, "OUTPUT_ROOT", tmp_path)
    registry = Registry("default")
    registry.put(_registry_entry("s1", "k1", "B1", expires_in_hours=-1.0))
    registry.save()
    budgets = FakeBudgetsClient()
    budgets.budgets["B1"] = IngestBudget(
        id="B1", name="hand-edited", scope="_sourceCategory=k1", capacity_bytes=1000,
        action="stopCollecting", budget_type="dailyVolume", description="repurposed by a human",
        timezone="UTC", reset_time="00:00", audit_threshold=85, usage_bytes=0,
        usage_status="Normal", created_at="", modified_at="",
    )

    results = reconcile.sweep_registry(registry, budgets)

    assert [r.action for r in results] == ["skipped_marker_mismatch"]
    assert "B1" in budgets.budgets  # never deleted
    reloaded = Registry("default")
    assert reloaded.get("s1", "k1") is not None  # left in place, not forgotten


def test_sweep_registry_happy_path_deletes_and_verifies_marker(tmp_path, monkeypatch):
    monkeypatch.setattr(bb_paths, "OUTPUT_ROOT", tmp_path)
    registry = Registry("default")
    registry.put(_registry_entry("s1", "k1", "B1", expires_in_hours=-1.0))
    registry.save()
    budgets = FakeBudgetsClient()
    desc = naming.build_description("s1", "k1", "_sourceCategory",
                                     datetime.now(timezone.utc), datetime.now(timezone.utc))
    budgets.budgets["B1"] = IngestBudget(
        id="B1", name="bb:s1:k1", scope="_sourceCategory=k1", capacity_bytes=1000,
        action="stopCollecting", budget_type="dailyVolume", description=desc,
        timezone="UTC", reset_time="00:00", audit_threshold=85, usage_bytes=0,
        usage_status="Normal", created_at="", modified_at="",
    )

    results = reconcile.sweep_registry(registry, budgets)

    assert [r.action for r in results] == ["swept"]
    assert "B1" not in budgets.budgets
    reloaded = Registry("default")
    assert reloaded.get("s1", "k1") is None


def test_sweep_registry_scope_names_filters(tmp_path, monkeypatch):
    monkeypatch.setattr(bb_paths, "OUTPUT_ROOT", tmp_path)
    registry = Registry("default")
    registry.put(_registry_entry("s1", "k1", "B1", expires_in_hours=-1.0))
    registry.put(_registry_entry("s2", "k2", "B2", expires_in_hours=-1.0))
    registry.save()
    budgets = FakeBudgetsClient()  # neither budget exists -> both would 404 if swept

    results = reconcile.sweep_registry(registry, budgets, scope_names=["s1"])

    assert {r.scope_name for r in results} == {"s1"}
    reloaded = Registry("default")
    assert reloaded.get("s1", "k1") is None  # swept
    assert reloaded.get("s2", "k2") is not None  # untouched by this call


def test_sweep_registry_dry_run_does_not_mutate(tmp_path, monkeypatch):
    monkeypatch.setattr(bb_paths, "OUTPUT_ROOT", tmp_path)
    registry = Registry("default")
    registry.put(_registry_entry("s1", "k1", "B1", expires_in_hours=-1.0))
    registry.save()
    budgets = FakeBudgetsClient()

    results = reconcile.sweep_registry(registry, budgets, dry_run=True)

    assert [r.action for r in results] == ["swept"]
    assert registry.get("s1", "k1") is not None  # not actually removed
