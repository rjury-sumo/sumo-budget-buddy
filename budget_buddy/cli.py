"""cli.py — sumo-budget-buddy entry point. See docs/dev/budget-buddy-plan.md
for the full command reference and design rationale.
"""
from __future__ import annotations

import argparse
import logging
import sys
from contextlib import ExitStack
from dataclasses import asdict

from budget_buddy import lock, naming
from budget_buddy.config import ConfigError, ScopeConfig, load_config, select_scopes
from budget_buddy.logging_setup import configure_logging
from budget_buddy.output import infer_format, render_detail, render_rows
from budget_buddy.reconcile import ClientCache, enforce_scope, evaluate_scope, sweep_registry
from budget_buddy.registry import Registry

logger = logging.getLogger("budget_buddy.cli")


def human_bytes(n: int) -> str:
    size = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def render_usage(usage_status: str, usage_bytes: int, capacity_bytes: int) -> str:
    if usage_status in ("Normal", "Approaching"):
        pct = round(usage_bytes / capacity_bytes * 100) if capacity_bytes else 0
        return f"{pct}% ({human_bytes(usage_bytes)} / {human_bytes(capacity_bytes)})"
    if usage_status == "Exceeded":
        return f"EXCEEDED ({human_bytes(usage_bytes)} @ trip)"
    return "? (unable to retrieve usage)"


# ---------------------------------------------------------------------------
# argument parsing
# ---------------------------------------------------------------------------

def _common_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(add_help=False)
    p.add_argument("--log-level", default="INFO",
                   choices=["TRACE", "DEBUG", "INFO", "WARNING", "ERROR"])
    return p


def _config_scope_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--config", help="path to budget-buddy.yaml")
    p.add_argument("--scope", action="append", dest="scopes", metavar="NAME",
                   help="named scope to target (repeatable)")
    p.add_argument("--all", action="store_true", help="target every scope in --config")


def _adhoc_scope_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--field", help="ad-hoc mode: metadata field, e.g. _sourceCategory")
    p.add_argument("--scope-expr", help='ad-hoc mode: e.g. "_sourceCategory=*cloudtrail*"')
    p.add_argument("--mode", choices=["per_value", "aggregate"], default="per_value")
    p.add_argument("--window", default="today")
    p.add_argument("--tz", default="America/Los_Angeles")
    p.add_argument("--threshold-bytes", type=int, default=5 * 1024 ** 3)
    p.add_argument("--instance", default="default")


def build_parser() -> argparse.ArgumentParser:
    common = _common_parser()
    parser = argparse.ArgumentParser(prog="sumo-budget-buddy", parents=[common])
    sub = parser.add_subparsers(dest="command", required=True)

    p_eval = sub.add_parser("evaluate", parents=[common], help="measure volume, no mutation")
    _config_scope_args(p_eval)
    _adhoc_scope_args(p_eval)
    p_eval.add_argument("--format", choices=["table", "json", "csv"])
    p_eval.add_argument("--output")

    p_enf = sub.add_parser("enforce", parents=[common], help="sweep expired, then create budgets for exceptions")
    _config_scope_args(p_enf)
    p_enf.add_argument("--dry-run", action="store_true")
    p_enf.add_argument("--force", action="store_true", help="clear a stale concurrency lock")

    p_swp = sub.add_parser("sweep", parents=[common], help="delete expired budget-buddy-managed budgets")
    p_swp.add_argument("--config", help="path to budget-buddy.yaml (only needed to resolve --scope names)")
    p_swp.add_argument("--scope", action="append", dest="scopes", metavar="NAME")
    p_swp.add_argument("--instance", default="default")
    p_swp.add_argument("--dry-run", action="store_true")
    p_swp.add_argument("--force", action="store_true", help="clear a stale concurrency lock")

    p_list = sub.add_parser("list", parents=[common], help="show registry-tracked (and optionally all) budgets")
    p_list.add_argument("--instance", default="default")
    p_list.add_argument("--all-budgets", action="store_true")
    p_list.add_argument("--format", choices=["table", "json", "csv"])
    p_list.add_argument("--output")

    p_status = sub.add_parser("status", parents=[common], help="one budget's detail + current usage")
    p_status.add_argument("target", help="budget ID, or scope_name:key to resolve via the registry")
    p_status.add_argument("--instance", default="default")

    return parser


# ---------------------------------------------------------------------------
# command handlers
# ---------------------------------------------------------------------------

def _resolve_scopes(args) -> list[ScopeConfig]:
    if args.config:
        cfg = load_config(args.config)
        for w in cfg.warnings:
            logger.warning(w)
        return select_scopes(cfg, args.scopes, args.all)

    if not args.field or not args.scope_expr:
        raise ConfigError("either --config (with --scope/--all) or --field + --scope-expr is required")
    return [ScopeConfig(
        name="adhoc", field=args.field, scope=args.scope_expr, mode=args.mode,
        window=args.window, tz=args.tz, threshold_bytes=args.threshold_bytes,
        instance=args.instance,
    )]


def cmd_evaluate(args) -> int:
    scopes = _resolve_scopes(args)
    clients = ClientCache()
    rows = []
    for scope in scopes:
        search_client, _ = clients.get(scope.instance)
        for r in evaluate_scope(scope, search_client):
            rows.append({
                "scope": r.scope_name, "key": r.key, "bytes": r.bytes, "gb": r.gb,
                "events": r.events, "threshold_bytes": r.threshold_bytes,
                "over_threshold": r.over_threshold, "window": r.window, "tz": r.tz,
            })
    fmt = infer_format(args.output, args.format)
    columns = ["scope", "key", "gb", "events", "threshold_bytes", "over_threshold", "window", "tz"]
    render_rows(rows, columns, fmt=fmt, output=args.output, title="evaluate")
    return 0


def _run_enforce(scopes: list[ScopeConfig], *, dry_run: bool) -> int:
    clients = ClientCache()
    by_instance: dict[str, list[ScopeConfig]] = {}
    for s in scopes:
        by_instance.setdefault(s.instance, []).append(s)

    exit_code = 0
    for instance, inst_scopes in by_instance.items():
        search_client, budgets_client = clients.get(instance)
        registry = Registry(instance)

        swept = sweep_registry(registry, budgets_client, dry_run=dry_run)
        for r in swept:
            logger.info("sweep result: %s", asdict(r))
            if r.action == "error":
                exit_code = 1

        for scope in inst_scopes:
            results = enforce_scope(scope, search_client, budgets_client, registry, dry_run=dry_run)
            for r in results:
                if r.action in ("error", "capped_uncovered", "skipped_invalid_scope"):
                    exit_code = 1
                print(f"[{scope.name}] {r.action}: key={r.key!r} "
                      f"{'budget_id=' + r.budget_id if r.budget_id else ''} {r.detail}".strip())
    return exit_code


def cmd_enforce(args) -> int:
    scopes = _resolve_scopes(args)
    instances = sorted({s.instance for s in scopes})
    with _multi_lock(instances, "enforce", force=args.force):
        return _run_enforce(scopes, dry_run=args.dry_run)


def cmd_sweep(args) -> int:
    instance = args.instance
    with _multi_lock([instance], "sweep", force=args.force):
        budgets_client = ClientCache().get(instance)[1]
        registry = Registry(instance)
        results = sweep_registry(registry, budgets_client, scope_names=args.scopes, dry_run=args.dry_run)
        exit_code = 0
        for r in results:
            if r.action == "error":
                exit_code = 1
            print(f"{r.action}: scope={r.scope_name} key={r.key!r} budget_id={r.budget_id} {r.detail}".strip())
        return exit_code


class _multi_lock:
    """Acquire budget_buddy.lock for each instance in turn (nested), so
    `enforce --all` across several instances still gets exclusivity on each
    one's registry without serializing unrelated instances against each
    other's lock files."""

    def __init__(self, instances: list[str], command: str, *, force: bool):
        self._instances = instances
        self._command = command
        self._force = force

    def __enter__(self):
        self._exit_stack = ExitStack()
        for inst in self._instances:
            self._exit_stack.enter_context(lock.acquire(inst, self._command, force=self._force))
        return self

    def __exit__(self, *exc):
        return self._exit_stack.__exit__(*exc)


def cmd_list(args) -> int:
    registry = Registry(args.instance)
    _, budgets_client = ClientCache().get(args.instance)
    live = {b.id: b for b in budgets_client.list_budgets()}

    rows = []
    covered_ids = set()
    for entry in registry.all():
        covered_ids.add(entry.budget_id)
        b = live.get(entry.budget_id)
        if b is None:
            rows.append({"scope": entry.scope_name, "key": entry.key, "type": entry.budget_type,
                         "usage": "? (not found live)", "capacity": "", "action": "",
                         "expires": entry.expires_at, "managed": "yes"})
            continue
        rows.append({
            "scope": entry.scope_name, "key": entry.key, "type": b.budget_type,
            "usage": render_usage(b.usage_status, b.usage_bytes, b.capacity_bytes),
            "capacity": human_bytes(b.capacity_bytes), "action": b.action,
            "expires": entry.expires_at, "managed": "yes",
        })

    if args.all_budgets:
        for b in live.values():
            if b.id in covered_ids:
                continue
            marker = naming.parse_marker(b.description)
            rows.append({
                "scope": marker.scope_name if marker else "", "key": marker.key if marker else "",
                "type": b.budget_type,
                "usage": render_usage(b.usage_status, b.usage_bytes, b.capacity_bytes),
                "capacity": human_bytes(b.capacity_bytes), "action": b.action,
                "expires": marker.expires_at if marker else "", "managed": "yes" if marker else "no",
            })

    columns = (["managed"] if args.all_budgets else []) + \
              ["scope", "key", "type", "usage", "capacity", "action", "expires"]
    fmt = infer_format(args.output, args.format)
    render_rows(rows, columns, fmt=fmt, output=args.output, title="sumo-budget-buddy list")
    return 0


def cmd_status(args) -> int:
    _, budgets_client = ClientCache().get(args.instance)
    target = args.target
    budget_id = target
    if ":" in target:
        scope_name, _, key = target.partition(":")
        entry = Registry(args.instance).get(scope_name, key)
        if entry is None:
            print(f"no registry entry for {target!r}", file=sys.stderr)
            return 1
        budget_id = entry.budget_id

    b = budgets_client.get_budget(budget_id)
    marker = naming.parse_marker(b.description)
    detail = {
        "id": b.id, "name": b.name, "scope": b.scope, "budget_type": b.budget_type,
        "action": b.action, "capacity_bytes": b.capacity_bytes,
        "usage_bytes": b.usage_bytes, "usage_status": b.usage_status,
        "usage": render_usage(b.usage_status, b.usage_bytes, b.capacity_bytes),
        "description": b.description, "managed_by_budget_buddy": marker is not None,
        "created_at": b.created_at, "modified_at": b.modified_at,
    }
    render_detail(detail, title=f"budget {b.id}")
    return 0


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    configure_logging(args.log_level)

    handlers = {
        "evaluate": cmd_evaluate, "enforce": cmd_enforce, "sweep": cmd_sweep,
        "list": cmd_list, "status": cmd_status,
    }
    try:
        return handlers[args.command](args)
    except (ConfigError, lock.LockHeldError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
