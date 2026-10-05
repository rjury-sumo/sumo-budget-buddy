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

def test_enforce_scope_uses_budget_capacity_bytes_when_set(tmp_path, monkeypatch):
    # The budget's capacity can be decoupled from the evaluation threshold —
    # a scope flags exceptions at threshold_bytes but caps the created budget
    # at the smaller budget_capacity_bytes.
    monkeypatch.setattr(bb_paths, "OUTPUT_ROOT", tmp_path)
    registry = Registry("default")
    search = FakeSearchClient([_row_record("aws/x", 2000)])
    budgets = FakeBudgetsClient()

    reconcile.enforce_scope(_scope(budget_capacity_bytes=50), search, budgets, registry)

    [created] = budgets.budgets.values()
    assert created.capacity_bytes == 50


def test_enforce_scope_logs_budget_capacity_bytes_not_threshold_bytes(tmp_path, monkeypatch, caplog):
    # Regression: the "enforce created" log line once reported capacity as
    # scope.threshold_bytes even when budget_capacity_bytes diverged from it —
    # caught live when a 1,000,000-byte threshold scope created a budget
    # actually capped at 10,000 bytes, but the audit log still said 1,000,000.
    monkeypatch.setattr(bb_paths, "OUTPUT_ROOT", tmp_path)
    registry = Registry("default")
    search = FakeSearchClient([_row_record("aws/x", 2000)])
    budgets = FakeBudgetsClient()

    with caplog.at_level("INFO", logger="budget_buddy.reconcile"):
        reconcile.enforce_scope(_scope(threshold_bytes=1000, budget_capacity_bytes=50),
                                 search, budgets, registry)

    [created_log] = [r.message for r in caplog.records if "enforce created" in r.message]
    assert "capacity=50" in created_log
    assert "capacity=1000" not in created_log


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


# ---------------------------------------------------------------------------
# delete_budget — manual fix-up path, since sweep only ever touches
# TTL-expired entries and there is otherwise no way to remove a budget early
# ---------------------------------------------------------------------------

def _managed_budget(budget_id: str, scope_name: str, key: str) -> IngestBudget:
    desc = naming.build_description(scope_name, key, "_sourceCategory",
                                     datetime.now(timezone.utc), datetime.now(timezone.utc))
    return IngestBudget(
        id=budget_id, name=f"bb:{scope_name}:{key}", scope=f"_sourceCategory={key}",
        capacity_bytes=1000, action="stopCollecting", budget_type="dailyVolume",
        description=desc, timezone="UTC", reset_time="00:00", audit_threshold=85,
        usage_bytes=0, usage_status="Normal", created_at="", modified_at="",
    )


def test_delete_budget_by_raw_id_happy_path(tmp_path, monkeypatch):
    monkeypatch.setattr(bb_paths, "OUTPUT_ROOT", tmp_path)
    registry = Registry("default")
    registry.put(_registry_entry("s1", "k1", "B1", expires_in_hours=10.0))
    registry.save()
    budgets = FakeBudgetsClient()
    budgets.budgets["B1"] = _managed_budget("B1", "s1", "k1")

    result = reconcile.delete_budget("B1", budgets, registry)

    assert result.action == "deleted"
    assert "B1" not in budgets.budgets
    reloaded = Registry("default")
    assert reloaded.get("s1", "k1") is None  # registry entry forgotten too


def test_delete_budget_by_scope_name_key(tmp_path, monkeypatch):
    monkeypatch.setattr(bb_paths, "OUTPUT_ROOT", tmp_path)
    registry = Registry("default")
    registry.put(_registry_entry("s1", "k1", "B1", expires_in_hours=10.0))
    registry.save()
    budgets = FakeBudgetsClient()
    budgets.budgets["B1"] = _managed_budget("B1", "s1", "k1")

    result = reconcile.delete_budget("s1:k1", budgets, registry)

    assert result.action == "deleted"
    assert result.budget_id == "B1"
    assert "B1" not in budgets.budgets


def test_delete_budget_scope_name_key_with_no_registry_entry_errors(tmp_path, monkeypatch):
    monkeypatch.setattr(bb_paths, "OUTPUT_ROOT", tmp_path)
    registry = Registry("default")
    budgets = FakeBudgetsClient()

    result = reconcile.delete_budget("s1:nope", budgets, registry)

    assert result.action == "error"
    assert budgets.budgets == {}


def test_delete_budget_without_marker_is_refused(tmp_path, monkeypatch):
    # Default guardrail: refuse to delete a budget with no budget-buddy
    # marker, so this can't be pointed at an unrelated org budget by mistake.
    monkeypatch.setattr(bb_paths, "OUTPUT_ROOT", tmp_path)
    registry = Registry("default")
    budgets = FakeBudgetsClient()
    budgets.budgets["B1"] = IngestBudget(
        id="B1", name="hand-made", scope="_sourceCategory=k1", capacity_bytes=1000,
        action="stopCollecting", budget_type="dailyVolume", description="not ours",
        timezone="UTC", reset_time="00:00", audit_threshold=85, usage_bytes=0,
        usage_status="Normal", created_at="", modified_at="",
    )

    result = reconcile.delete_budget("B1", budgets, registry)

    assert result.action == "skipped_marker_mismatch"
    assert "B1" in budgets.budgets  # never deleted


def test_delete_budget_without_marker_force_overrides(tmp_path, monkeypatch):
    monkeypatch.setattr(bb_paths, "OUTPUT_ROOT", tmp_path)
    registry = Registry("default")
    budgets = FakeBudgetsClient()
    budgets.budgets["B1"] = IngestBudget(
        id="B1", name="hand-made", scope="_sourceCategory=k1", capacity_bytes=1000,
        action="stopCollecting", budget_type="dailyVolume", description="not ours",
        timezone="UTC", reset_time="00:00", audit_threshold=85, usage_bytes=0,
        usage_status="Normal", created_at="", modified_at="",
    )

    result = reconcile.delete_budget("B1", budgets, registry, force=True)

    assert result.action == "deleted"
    assert "B1" not in budgets.budgets


def test_delete_budget_already_gone_404_forgets_registry_entry(tmp_path, monkeypatch):
    monkeypatch.setattr(bb_paths, "OUTPUT_ROOT", tmp_path)
    registry = Registry("default")
    registry.put(_registry_entry("s1", "k1", "B1", expires_in_hours=10.0))
    registry.save()
    budgets = FakeBudgetsClient()  # B1 not present -> get_budget raises 404

    result = reconcile.delete_budget("s1:k1", budgets, registry)

    assert result.action == "already_gone"
    reloaded = Registry("default")
    assert reloaded.get("s1", "k1") is None


def test_delete_budget_by_scope_name_key_refuses_on_marker_mismatch(tmp_path, monkeypatch):
    # Regression: a scope_name:key target must verify the live marker
    # actually matches THAT scope/key, not just that some budget-buddy
    # marker is present — otherwise a stale/corrupted registry entry could
    # delete a live, unrelated budget that happens to carry a valid marker
    # for a different scope/key. Mirrors sweep_registry's verify_marker use.
    monkeypatch.setattr(bb_paths, "OUTPUT_ROOT", tmp_path)
    registry = Registry("default")
    registry.put(_registry_entry("s1", "k1", "B1", expires_in_hours=10.0))
    registry.save()
    budgets = FakeBudgetsClient()
    budgets.budgets["B1"] = _managed_budget("B1", "s2", "other-key")  # wrong scope/key

    result = reconcile.delete_budget("s1:k1", budgets, registry)

    assert result.action == "skipped_marker_mismatch"
    assert "B1" in budgets.budgets  # never deleted
    reloaded = Registry("default")
    assert reloaded.get("s1", "k1") is not None  # left in place, not forgotten


def test_delete_budget_by_scope_name_key_marker_mismatch_force_overrides(tmp_path, monkeypatch):
    monkeypatch.setattr(bb_paths, "OUTPUT_ROOT", tmp_path)
    registry = Registry("default")
    registry.put(_registry_entry("s1", "k1", "B1", expires_in_hours=10.0))
    registry.save()
    budgets = FakeBudgetsClient()
    budgets.budgets["B1"] = _managed_budget("B1", "s2", "other-key")

    result = reconcile.delete_budget("s1:k1", budgets, registry, force=True)

    assert result.action == "deleted"
    assert "B1" not in budgets.budgets


def test_sweep_registry_delete_failure_keeps_entry_for_retry(tmp_path, monkeypatch):
    # Regression: a failure from the DELETE call itself (not just the GET
    # check above it) must be caught and reported as "error", leaving the
    # entry in the registry for next cycle's retry — not propagate and
    # abort the whole sweep/enforce run over one flaky delete.
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

    def _boom(budget_id):
        raise ConnectionError("network is down")

    budgets.delete_budget = _boom

    results = reconcile.sweep_registry(registry, budgets)

    assert [r.action for r in results] == ["error"]
    assert "B1" in budgets.budgets  # delete never actually happened
    reloaded = Registry("default")
    assert reloaded.get("s1", "k1") is not None  # still tracked for next cycle


def test_delete_budget_dry_run_does_not_mutate(tmp_path, monkeypatch):
    monkeypatch.setattr(bb_paths, "OUTPUT_ROOT", tmp_path)
    registry = Registry("default")
    registry.put(_registry_entry("s1", "k1", "B1", expires_in_hours=10.0))
    registry.save()
    budgets = FakeBudgetsClient()
    budgets.budgets["B1"] = _managed_budget("B1", "s1", "k1")

    result = reconcile.delete_budget("s1:k1", budgets, registry, dry_run=True)

    assert result.action == "deleted"
    assert "B1" in budgets.budgets  # not actually deleted
    assert registry.get("s1", "k1") is not None  # not actually forgotten
