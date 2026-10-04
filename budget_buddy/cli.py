"""cli.py — sumo-budget-buddy entry point. See docs/dev/budget-buddy-plan.md
for the full command reference and design rationale.
"""
from __future__ import annotations

import argparse
import logging
import sys
from contextlib import ExitStack
from dataclasses import asdict

from budget_buddy import instance_config, lock, naming
from budget_buddy.config import ConfigError, DEFAULT_CONFIG_PATH, ScopeConfig, load_config, select_scopes
from budget_buddy.instance_config import (
    REGION_ENDPOINTS, endpoint_for_region, instance_status,
    list_instances_with_status, load_instances, remove_instance, save_instance,
)
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
    p.add_argument("--config", help=f"path to budget-buddy.yaml (default: {DEFAULT_CONFIG_PATH})")
    p.add_argument("--scope", action="append", dest="scopes", metavar="NAME",
                   help="named scope to target (repeatable)")
    p.add_argument("--all", action="store_true", help="target every scope in --config")


def _adhoc_scope_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--field", help="ad-hoc mode: metadata field, e.g. _sourceCategory; "
                                    "inferred from --scope-expr's left-hand side if omitted")
    p.add_argument("--scope-expr",
                    help='ad-hoc mode: e.g. "_sourceCategory=*cloudtrail*"; '
                         'defaults to "<field>=*" (every value) if omitted')
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
    p_eval.add_argument("--top", type=int, metavar="N",
                         help="keep only the N largest rows (by bytes, after sorting)")
    p_eval.add_argument("--format", choices=["table", "json", "csv"])
    p_eval.add_argument("--output")

    p_enf = sub.add_parser("enforce", parents=[common], help="sweep expired, then create budgets for exceptions")
    _config_scope_args(p_enf)
    p_enf.add_argument("--dry-run", action="store_true")
    p_enf.add_argument("--force", action="store_true", help="clear a stale concurrency lock")

    p_swp = sub.add_parser("sweep", parents=[common], help="delete expired budget-buddy-managed budgets")
    p_swp.add_argument("--scope", action="append", dest="scopes", metavar="NAME",
                        help="only sweep entries recorded under this scope name (repeatable)")
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

    p_inst = sub.add_parser("instances", parents=[common],
                             help=f"list or manage named Sumo instances ({instance_config.GLOBAL_CONFIG})")
    inst_sub = p_inst.add_subparsers(dest="instances_action", required=True)

    p_inst_list = inst_sub.add_parser("list", parents=[common], help="show every configured instance")
    p_inst_list.add_argument("--format", choices=["table", "json", "csv"])
    p_inst_list.add_argument("--output")

    p_inst_show = inst_sub.add_parser("show", parents=[common], help="detail for one instance (secrets masked)")
    p_inst_show.add_argument("name")

    p_inst_set = inst_sub.add_parser(
        "set", parents=[common], help="add or update a named instance (only given fields are touched)")
    p_inst_set.add_argument("name")
    p_inst_set.add_argument("--access-id", dest="access_id")
    p_inst_set.add_argument("--access-key", dest="access_key")
    p_inst_set.add_argument("--endpoint", help="API endpoint URL (if omitted, derived from --region)")
    p_inst_set.add_argument("--ui-base-url", dest="ui_base_url")
    p_inst_set.add_argument("--region", help=f"one of: {', '.join(sorted(REGION_ENDPOINTS))}")
    p_inst_set.add_argument("--description")

    p_inst_remove = inst_sub.add_parser(
        "remove", parents=[common], help="remove a named instance from the config file")
    p_inst_remove.add_argument("name")

    return parser


# ---------------------------------------------------------------------------
# command handlers
# ---------------------------------------------------------------------------

def _resolve_scopes(args) -> list[ScopeConfig]:
    if args.config:
        config_path = args.config
    elif getattr(args, "field", None) or getattr(args, "scope_expr", None):
        # explicit ad-hoc flags take precedence over a stale default config file
        config_path = None
    else:
        config_path = DEFAULT_CONFIG_PATH if DEFAULT_CONFIG_PATH.exists() else None

    if config_path:
        cfg = load_config(config_path)
        for w in cfg.warnings:
            logger.warning(w)
        return select_scopes(cfg, args.scopes, args.all)

    field = args.field
    scope_expr = args.scope_expr
    if not field:
        # --field is redundant when --scope-expr already names it on its
        # left-hand side, e.g. "_sourceCategory=*cloudtrail*" — infer rather
        # than make the caller repeat it.
        if scope_expr and "=" in scope_expr:
            field, _, _ = scope_expr.partition("=")
        else:
            raise ConfigError(
                "either --config (with --scope/--all), a config file at "
                f"{DEFAULT_CONFIG_PATH}, --field, or a --scope-expr of the form "
                "'<field>=<value>' (field is inferred from it) is required"
            )
    # --scope-expr is optional in ad-hoc mode: this scope is read-only
    # (evaluate never reaches enforce), so "every value for this field" is a
    # legitimate ask here, unlike a config-file scope that could later be
    # enforced against — see config.py's load_config for that check.
    scope_expr = scope_expr or f"{field}=*"
    return [ScopeConfig(
        name="adhoc", field=field, scope=scope_expr, mode=args.mode,
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
    # build_query already sorts per_value server-side, but re-sort here too:
    # multiple scopes (--all) or aggregate rows interleave in scope order, not
    # volume order, once combined into one table.
    rows.sort(key=lambda r: r["bytes"], reverse=True)
    if args.top:
        rows = rows[: args.top]
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


def _preview(raw_value: str | None, source: str, *, reveal_prefix: bool) -> str:
    """`reveal_prefix` must be False for any true secret (access_key) —
    only access_id (an identifier, not a secret) gets a partial preview."""
    if source == "env":
        return "set"
    if source != "config":
        return "missing"
    if reveal_prefix and raw_value and len(raw_value) > 4:
        return raw_value[:4] + "****"
    return "****"


def cmd_instances_list(args) -> int:
    rows = [{
        "name": inst["name"],
        "credentials": "ok" if inst["has_credentials"] else "MISSING",
        "access_id": inst["access_id_source"],
        "access_key": inst["access_key_source"],
        "endpoint": inst["endpoint"],
        "region": inst.get("region") or "",
        "description": inst.get("description") or "",
    } for inst in list_instances_with_status()]
    fmt = infer_format(args.output, args.format)
    columns = ["name", "credentials", "access_id", "access_key", "endpoint", "region", "description"]
    render_rows(rows, columns, fmt=fmt, output=args.output, title="sumo-budget-buddy instances")
    return 0


def cmd_instances_show(args) -> int:
    name = args.name.lower()
    raw = load_instances().get(name, {})
    inst = instance_status(name, raw)
    detail = {
        "endpoint": inst["endpoint"],
        "ui_base_url": inst.get("ui_base_url") or "",
        "region": inst.get("region") or "",
        "description": inst.get("description") or "",
        "access_id": f"{_preview(raw.get('access_id'), inst['access_id_source'], reveal_prefix=True)} "
                     f"(source: {inst['access_id_source']})",
        "access_key": f"{_preview(raw.get('access_key'), inst['access_key_source'], reveal_prefix=False)} "
                      f"(source: {inst['access_key_source']})",
    }
    render_detail(detail, title=f"instance {name}")
    return 0


def cmd_instances_set(args) -> int:
    name = args.name.lower()
    fields = {
        "access_id": args.access_id, "access_key": args.access_key,
        "endpoint": args.endpoint, "ui_base_url": args.ui_base_url,
        "region": args.region, "description": args.description,
    }
    if args.region and args.endpoint is None:
        derived = endpoint_for_region(args.region)
        if derived:
            fields["endpoint"] = derived
        else:
            print(f"warning: unrecognized region {args.region!r} (known: "
                  f"{', '.join(sorted(REGION_ENDPOINTS))}) — endpoint not derived, "
                  "pass --endpoint explicitly if needed", file=sys.stderr)

    fields = {k: v for k, v in fields.items() if v is not None}
    if not fields:
        print("error: provide at least one field to set (e.g. --access-id, --endpoint)", file=sys.stderr)
        return 2

    save_instance(name, fields)
    print(f"instance {name!r} saved to {instance_config.GLOBAL_CONFIG} (fields: {', '.join(fields)})")
    return 0


def cmd_instances_remove(args) -> int:
    try:
        remove_instance(args.name)
    except SystemExit as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(f"instance {args.name!r} removed from {instance_config.GLOBAL_CONFIG}")
    return 0


def cmd_instances(args) -> int:
    handlers = {
        "list": cmd_instances_list, "show": cmd_instances_show,
        "set": cmd_instances_set, "remove": cmd_instances_remove,
    }
    return handlers[args.instances_action](args)


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    configure_logging(args.log_level)

    handlers = {
        "evaluate": cmd_evaluate, "enforce": cmd_enforce, "sweep": cmd_sweep,
        "list": cmd_list, "status": cmd_status, "instances": cmd_instances,
    }
    try:
        return handlers[args.command](args)
    except (ConfigError, lock.LockHeldError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
