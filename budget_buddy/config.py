"""config.py — loads and validates budget-buddy's YAML scope configuration.

See docs/dev/budget-buddy-plan.md, "Config file" for the full schema and
rationale. One file defines reusable `defaults` plus a list of named
`scopes`; a run targets one or more scopes by name, or all of them.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass, field, fields
from pathlib import Path

import yaml

from budget_buddy.volume_query import SUPPORTED_FIELDS, GlobalScopeError, validate_scope_expr

DEFAULT_THRESHOLD_BYTES = 5 * 1024 ** 3  # 5 GiB
DEFAULT_MAX_BUDGETS = 50
DEFAULT_AUDIT_THRESHOLD = 85

_SCOPE_DEFAULTS = {
    "mode": "per_value",
    "window": "today",
    "tz": "America/Los_Angeles",
    "threshold_bytes": DEFAULT_THRESHOLD_BYTES,
    "budget_type": "dailyVolume",
    "action": "stopCollecting",
    "ttl": "end_of_day",
    "max_budgets": DEFAULT_MAX_BUDGETS,
    "audit_threshold": DEFAULT_AUDIT_THRESHOLD,
    "instance": "default",
}


class ConfigError(ValueError):
    """Raised for a malformed or invalid budget-buddy.yaml."""


@dataclass
class ScopeConfig:
    name: str
    field: str
    scope: str
    description: str = ""
    mode: str = _SCOPE_DEFAULTS["mode"]
    window: str = _SCOPE_DEFAULTS["window"]
    tz: str = _SCOPE_DEFAULTS["tz"]
    threshold_bytes: int = _SCOPE_DEFAULTS["threshold_bytes"]
    budget_type: str = _SCOPE_DEFAULTS["budget_type"]
    action: str = _SCOPE_DEFAULTS["action"]
    ttl: str = _SCOPE_DEFAULTS["ttl"]
    max_budgets: int = _SCOPE_DEFAULTS["max_budgets"]
    audit_threshold: int = _SCOPE_DEFAULTS["audit_threshold"]
    instance: str = _SCOPE_DEFAULTS["instance"]

    def __post_init__(self) -> None:
        if self.field not in SUPPORTED_FIELDS:
            raise ConfigError(
                f"scope {self.name!r}: unsupported field {self.field!r} — "
                f"supported: {', '.join(SUPPORTED_FIELDS)}"
            )
        if not self.scope.startswith(f"{self.field}="):
            raise ConfigError(
                f"scope {self.name!r}: `scope` must start with '{self.field}=', got {self.scope!r}"
            )
        try:
            validate_scope_expr(self.scope)
        except GlobalScopeError as exc:
            raise ConfigError(f"scope {self.name!r}: {exc}") from exc
        if self.mode not in ("per_value", "aggregate"):
            raise ConfigError(f"scope {self.name!r}: mode must be per_value|aggregate")
        if self.budget_type not in ("dailyVolume", "minuteVolume"):
            raise ConfigError(f"scope {self.name!r}: budget_type must be dailyVolume|minuteVolume")
        if self.action not in ("stopCollecting", "keepCollecting"):
            raise ConfigError(f"scope {self.name!r}: action must be stopCollecting|keepCollecting")
        if self.threshold_bytes <= 0:
            raise ConfigError(f"scope {self.name!r}: threshold_bytes must be > 0")
        if self.max_budgets <= 0:
            raise ConfigError(f"scope {self.name!r}: max_budgets must be > 0")
        if not (1 <= self.audit_threshold <= 99):
            raise ConfigError(f"scope {self.name!r}: audit_threshold must be 1-99")


@dataclass
class BudgetBuddyConfig:
    scopes: dict[str, ScopeConfig] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


_SCOPE_FIELD_NAMES = {f.name for f in fields(ScopeConfig)}


def load_config(path: str | Path) -> BudgetBuddyConfig:
    p = Path(path)
    if not p.exists():
        raise ConfigError(f"config file not found: {p}")
    try:
        raw = yaml.safe_load(p.read_text()) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"invalid YAML in {p}: {exc}") from exc

    if not isinstance(raw, dict):
        raise ConfigError(f"{p}: top level must be a mapping")

    top_instance = raw.get("instance")
    defaults = dict(_SCOPE_DEFAULTS)
    if top_instance:
        defaults["instance"] = top_instance
    defaults.update(raw.get("defaults") or {})

    raw_scopes = raw.get("scopes")
    if not raw_scopes or not isinstance(raw_scopes, list):
        raise ConfigError(f"{p}: `scopes` must be a non-empty list")

    scopes: dict[str, ScopeConfig] = {}
    warnings: list[str] = []
    for i, entry in enumerate(raw_scopes):
        if not isinstance(entry, dict):
            raise ConfigError(f"{p}: scopes[{i}] must be a mapping")
        if "name" not in entry or "field" not in entry or "scope" not in entry:
            raise ConfigError(f"{p}: scopes[{i}] requires name, field, and scope")

        merged = copy.deepcopy(defaults)
        merged.update(entry)
        unknown = set(merged) - _SCOPE_FIELD_NAMES
        if unknown:
            raise ConfigError(f"{p}: scope {entry.get('name')!r} has unknown key(s): {unknown}")

        scope_cfg = ScopeConfig(**merged)
        if scope_cfg.name in scopes:
            raise ConfigError(f"{p}: duplicate scope name {scope_cfg.name!r}")
        if scope_cfg.mode == "aggregate" and "max_budgets" in entry:
            warnings.append(
                f"scope {scope_cfg.name!r}: max_budgets is a no-op in aggregate mode "
                "(aggregate only ever produces 0 or 1 budget)"
            )
        scopes[scope_cfg.name] = scope_cfg

    return BudgetBuddyConfig(scopes=scopes, warnings=warnings)


def select_scopes(cfg: BudgetBuddyConfig, names: list[str] | None, all_scopes: bool) -> list[ScopeConfig]:
    if all_scopes:
        return list(cfg.scopes.values())
    if not names:
        raise ConfigError("specify --scope NAME [NAME ...] or --all")
    missing = [n for n in names if n not in cfg.scopes]
    if missing:
        raise ConfigError(f"unknown scope name(s): {missing} — known: {list(cfg.scopes)}")
    return [cfg.scopes[n] for n in names]
