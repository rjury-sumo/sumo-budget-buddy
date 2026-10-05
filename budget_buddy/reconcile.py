"""reconcile.py — the core evaluate/enforce/sweep logic, orchestrating
search.py (volume measurement), budgets.py (/v2/ingestBudgets), naming.py
(identification marker), registry.py (local TTL tracking), and timerange.py
(window resolution) per scope config. See docs/dev/budget-buddy-plan.md,
"Commands" for the walkthrough this implements.

Locking (lock.py) is deliberately NOT done here — it wraps whole commands in
cli.py, not individual scopes, so a single `enforce --all` run across many
scopes holds the lock for its entire duration.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

from budget_buddy import naming
from budget_buddy.budgets import BudgetAPIError, IngestBudgetsV2Client
from budget_buddy.config import ScopeConfig
from budget_buddy.instance_config import resolve_instance
from budget_buddy.registry import Registry, RegistryEntry
from budget_buddy.search import SearchClient
from budget_buddy.timerange import end_of_day, resolve_window
from budget_buddy.volume_query import GlobalScopeError, build_query, parse_rows, validate_scope_expr

logger = logging.getLogger("budget_buddy.reconcile")


@dataclass
class EvaluationRow:
    scope_name: str
    key: str
    field: str
    bytes: int
    gb: float
    events: int
    threshold_bytes: int
    over_threshold: bool
    window: str
    tz: str


@dataclass
class ActionResult:
    scope_name: str
    key: str
    action: str  # created | skipped_existing | skipped_invalid_scope | swept |
                 # swept_already_gone | skipped_marker_mismatch | capped_uncovered | error |
                 # deleted | already_gone
    budget_id: str | None = None
    bytes: int | None = None
    detail: str = ""


class ClientCache:
    """One (SearchClient, IngestBudgetsV2Client) pair per distinct instance
    encountered across the scopes targeted in a single run — scopes may name
    different instances."""

    def __init__(self) -> None:
        self._cache: dict[str, tuple[SearchClient, IngestBudgetsV2Client]] = {}

    def get(self, instance: str) -> tuple[SearchClient, IngestBudgetsV2Client]:
        if instance not in self._cache:
            creds = resolve_instance(instance)
            search = SearchClient(creds["access_id"], creds["access_key"], creds["endpoint"])
            budgets = IngestBudgetsV2Client(creds["access_id"], creds["access_key"], creds["endpoint"])
            self._cache[instance] = (search, budgets)
        return self._cache[instance]


def _glob_value(scope_expr: str) -> str:
    """`_sourceCategory=*cloudtrail*` -> `*cloudtrail*`."""
    _, _, value = scope_expr.partition("=")
    return value


def evaluate_scope(scope: ScopeConfig, search_client: SearchClient, *,
                    now: datetime | None = None) -> list[EvaluationRow]:
    window = resolve_window(scope.window, scope.tz, now=now)
    query = build_query(scope.field, _glob_value(scope.scope), scope.mode)
    logger.debug("scope %s: query=\n%s", scope.name, query)
    result = search_client.run_aggregate(query, window.from_ms, window.to_ms)
    rows = parse_rows(scope.field, result.records, scope.mode)

    out = []
    for r in rows:
        over = r.bytes > scope.threshold_bytes
        out.append(EvaluationRow(
            scope_name=scope.name, key=r.key, field=scope.field, bytes=r.bytes,
            gb=round(r.bytes / (1024 ** 3), 4), events=r.events,
            threshold_bytes=scope.threshold_bytes, over_threshold=over,
            window=scope.window, tz=scope.tz,
        ))
        logger.info(
            "evaluate scope=%s key=%s bytes=%d (%.3f GB) threshold=%d over_threshold=%s",
            scope.name, r.key, r.bytes, out[-1].gb, scope.threshold_bytes, over,
        )
    if not rows:
        logger.warning("scope %s: query matched zero values for window %s", scope.name, scope.window)
    return out


def sweep_registry(registry: Registry, budgets_client: IngestBudgetsV2Client, *,
                    scope_names: list[str] | None = None, dry_run: bool = False,
                    now: datetime | None = None) -> list[ActionResult]:
    """Delete every registry entry past its TTL, after re-verifying its live
    description marker still matches. `scope_names` restricts which scopes'
    entries are considered (None = every scope in the registry).

    Registry mutations are saved immediately after each one (not batched at
    the end) so a crash mid-sweep loses at most the single entry in flight,
    not everything already removed this run.
    """
    results: list[ActionResult] = []
    for entry in registry.expired(now=now):
        if scope_names is not None and entry.scope_name not in scope_names:
            continue
        if dry_run:
            results.append(ActionResult(entry.scope_name, entry.key, "swept",
                                         budget_id=entry.budget_id, detail="dry-run, not deleted"))
            continue

        # Live marker re-check before delete — the registry says what we
        # INTEND to delete, this confirms the live object is still really
        # ours before we actually do it.
        try:
            live = budgets_client.get_budget(entry.budget_id)
        except BudgetAPIError as exc:
            if exc.status_code == 404:
                # Genuinely gone (e.g. deleted manually) — safe to forget.
                logger.info("sweep: budget %s already gone (404) — removing from registry",
                            entry.budget_id)
                registry.remove(entry.scope_name, entry.key)
                registry.save()
                results.append(ActionResult(entry.scope_name, entry.key, "swept_already_gone",
                                             budget_id=entry.budget_id))
            else:
                # Anything else (401/403/500/...) is NOT confirmation the budget
                # is gone — leave it in the registry so the next cycle retries,
                # rather than silently abandoning tracking of a possibly-live budget.
                logger.error(
                    "sweep: GET budget %s failed (HTTP %s) — leaving in registry to retry next "
                    "cycle: %s", entry.budget_id, exc.status_code, exc,
                )
                results.append(ActionResult(entry.scope_name, entry.key, "error",
                                             budget_id=entry.budget_id, detail=str(exc)))
            continue
        except Exception as exc:  # noqa: BLE001 - transport/network failure, not an API response
            logger.error(
                "sweep: could not reach API to check budget %s — leaving in registry to retry "
                "next cycle: %s", entry.budget_id, exc,
            )
            results.append(ActionResult(entry.scope_name, entry.key, "error",
                                         budget_id=entry.budget_id, detail=str(exc)))
            continue

        if not naming.verify_marker(live.description, expected_scope=entry.scope_name,
                                     expected_key=entry.key):
            logger.error(
                "sweep: budget %s description no longer matches expected marker "
                "(scope=%s key=%s) — SKIPPING delete for safety", entry.budget_id,
                entry.scope_name, entry.key,
            )
            results.append(ActionResult(entry.scope_name, entry.key, "skipped_marker_mismatch",
                                         budget_id=entry.budget_id,
                                         detail="live description marker did not match registry"))
            continue

        deleted = budgets_client.delete_budget(entry.budget_id)
        registry.remove(entry.scope_name, entry.key)
        registry.save()
        action = "swept" if deleted else "swept_already_gone"
        logger.info("sweep %s: budget_id=%s scope=%s key=%s", action, entry.budget_id,
                   entry.scope_name, entry.key)
        results.append(ActionResult(entry.scope_name, entry.key, action, budget_id=entry.budget_id))

    return results


def enforce_scope(scope: ScopeConfig, search_client: SearchClient,
                   budgets_client: IngestBudgetsV2Client, registry: Registry, *,
                   dry_run: bool = False, now: datetime | None = None) -> list[ActionResult]:
    now = now or datetime.now().astimezone()
    rows = evaluate_scope(scope, search_client, now=now)
    offenders = sorted((r for r in rows if r.over_threshold), key=lambda r: r.bytes, reverse=True)

    results: list[ActionResult] = []
    expires_at = end_of_day(scope.tz, on=now)

    for row in offenders[: scope.max_budgets]:
        # get_active, not get: an expired-but-not-yet-swept entry (e.g. one
        # sweep deliberately left behind after a marker mismatch) must NOT
        # count as "already covered" — that would silently leave this key
        # unprotected forever. See registry.get_active's docstring.
        existing = registry.get_active(scope.name, row.key, now=now)
        if existing is not None:
            results.append(ActionResult(scope.name, row.key, "skipped_existing",
                                         budget_id=existing.budget_id, bytes=row.bytes,
                                         detail="already has a non-expired budget this cycle"))
            logger.info("enforce skipped_existing scope=%s key=%s budget_id=%s",
                       scope.name, row.key, existing.budget_id)
            continue

        if scope.mode == "aggregate":
            budget_scope_expr = scope.scope
        else:
            budget_scope_expr = f"{scope.field}={row.key}"

        # Last line of defense: never create a budget with a blank or bare-
        # wildcard scope value, no matter how a bad `row.key` could arise.
        try:
            validate_scope_expr(budget_scope_expr)
        except GlobalScopeError as exc:
            logger.error(
                "enforce: refusing to create a global-scope budget for scope=%s key=%r: %s",
                scope.name, row.key, exc,
            )
            results.append(ActionResult(scope.name, row.key, "skipped_invalid_scope",
                                         bytes=row.bytes, detail=str(exc)))
            continue

        name = naming.build_name(scope.name, row.key, expires_at)
        description = naming.build_description(scope.name, row.key, scope.field, now, expires_at)

        if dry_run:
            results.append(ActionResult(scope.name, row.key, "created", bytes=row.bytes,
                                         detail=f"dry-run: would create {name!r} scope={budget_scope_expr!r} "
                                                f"capacity={scope.budget_capacity_bytes}"))
            continue

        try:
            created = budgets_client.create_budget(
                name=name, scope=budget_scope_expr, capacity_bytes=scope.budget_capacity_bytes,
                action=scope.action, budget_type=scope.budget_type, description=description,
                timezone=scope.tz, reset_time="00:00", audit_threshold=scope.audit_threshold,
            )
        except Exception as exc:  # noqa: BLE001 - report and continue with remaining offenders
            logger.error("enforce: create_budget failed for scope=%s key=%s: %s",
                        scope.name, row.key, exc)
            results.append(ActionResult(scope.name, row.key, "error", bytes=row.bytes, detail=str(exc)))
            continue

        registry.put(RegistryEntry(
            budget_id=created.id, scope_name=scope.name, key=row.key, field=scope.field,
            budget_type=scope.budget_type, created_at=now.isoformat(), expires_at=expires_at.isoformat(),
        ))
        # Save immediately, not batched at the end of the loop — minimizes the
        # window in which a crash leaves a just-created budget untracked.
        registry.save()
        logger.info("enforce created scope=%s key=%s budget_id=%s bytes=%d capacity=%d expires=%s",
                   scope.name, row.key, created.id, row.bytes, scope.budget_capacity_bytes,
                   expires_at.isoformat())
        results.append(ActionResult(scope.name, row.key, "created", budget_id=created.id, bytes=row.bytes))

    uncovered = offenders[scope.max_budgets:]
    for row in uncovered:
        logger.error(
            "enforce: scope=%s max_budgets=%d reached — key=%s (bytes=%d) left uncovered this cycle",
            scope.name, scope.max_budgets, row.key, row.bytes,
        )
        results.append(ActionResult(scope.name, row.key, "capped_uncovered", bytes=row.bytes,
                                     detail=f"max_budgets={scope.max_budgets} reached"))

    return results


def _forget(registry: Registry, budget_id: str) -> None:
    """Remove whatever registry entry points at `budget_id`, if any, and
    persist immediately. Searches by budget_id rather than scope/key since a
    raw-ID `delete_budget` call may not know which scope/key it belongs to."""
    for entry in registry.all():
        if entry.budget_id == budget_id:
            registry.remove(entry.scope_name, entry.key)
            registry.save()
            return


def delete_budget(target: str, budgets_client: IngestBudgetsV2Client, registry: Registry, *,
                   force: bool = False, dry_run: bool = False) -> ActionResult:
    """Manually delete one budget by raw ID or `scope_name:key` (resolved via
    the registry) — the fix-up path for undoing a mistaken `enforce`, since
    `sweep` only ever touches entries already past their TTL and there is
    otherwise no way to remove a budget early. Refuses to delete anything
    that doesn't carry the budget-buddy description marker unless `force` is
    set, so this can't be pointed at an unrelated org budget by accident —
    the same defensive check `sweep_registry` applies before its own deletes.
    """
    scope_name, key = "", target
    budget_id = target
    if ":" in target:
        scope_name, _, key = target.partition(":")
        entry = registry.get(scope_name, key)
        if entry is None:
            return ActionResult(scope_name, key, "error", detail=f"no registry entry for {target!r}")
        budget_id = entry.budget_id

    try:
        live = budgets_client.get_budget(budget_id)
    except BudgetAPIError as exc:
        if exc.status_code == 404:
            _forget(registry, budget_id)
            return ActionResult(scope_name, key, "already_gone", budget_id=budget_id)
        return ActionResult(scope_name, key, "error", budget_id=budget_id, detail=str(exc))

    marker = naming.parse_marker(live.description)
    if marker is not None and not scope_name:
        scope_name, key = marker.scope_name, marker.key

    if marker is None and not force:
        return ActionResult(scope_name, key, "skipped_marker_mismatch", budget_id=budget_id,
                             detail="no budget-buddy marker on this budget's description — "
                                    "pass force=True to override")

    if dry_run:
        return ActionResult(scope_name, key, "deleted", budget_id=budget_id,
                             detail=f"dry-run: would delete (name={live.name!r})")

    budgets_client.delete_budget(budget_id)
    _forget(registry, budget_id)
    return ActionResult(scope_name, key, "deleted", budget_id=budget_id, detail=f"name={live.name!r}")
