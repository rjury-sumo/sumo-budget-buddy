"""
instance_config.py — named Sumo Logic instance (org) credential resolution
for budget_buddy.

Vendored and trimmed from cli/config.py (incl. a trimmed `save_instance`/
`remove_instance` write path, backing `sumo-budget-buddy instances set` /
`remove` — see cli.py) so budget_buddy has no import dependency on the
parent sumo-ai repo. Deliberately kept compatible with the same
~/.sumo/instances.toml file and SUMO_ACCESS_ID[_NAME]-style env var
convention, so a budget_buddy installed alongside the main `sumo` CLI
shares the same instance definitions without any code coupling between
the two.

Config file (~/.sumo/instances.toml) — entirely optional, see below
---------------------------------------------------------------------
  [instances.default]
  access_id  = "..."
  access_key = "..."
  endpoint   = "https://api.au.sumologic.com"
  region     = "AU"

Environment variables
----------------------
  'default' instance:   SUMO_ACCESS_ID, SUMO_ACCESS_KEY, SUMO_ENDPOINT
  named instance <NAME>: SUMO_ACCESS_ID_<NAME>, SUMO_ACCESS_KEY_<NAME>, SUMO_ENDPOINT_<NAME>
  Env vars always override the config file for the matching instance.

  A named instance needs NO instances.toml entry at all: `--instance prod`
  with just SUMO_ACCESS_ID_PROD / SUMO_ACCESS_KEY_PROD set resolves fine on
  its own (`load_instances()` falls back to `{}` for an unknown name). The
  config file is only for persisting values you don't want to re-export
  every session (or metadata like region/description) — and even then, any
  individual field can still be left to its env var instead.
"""
from __future__ import annotations

import os
import sys
import tomllib
from pathlib import Path

GLOBAL_CONFIG = Path.home() / ".sumo" / "instances.toml"
PROJECT_CONFIG = Path.cwd() / ".sumo.toml"

DEFAULT_ENDPOINT = "https://api.au.sumologic.com"

REGION_ENDPOINTS = {
    "US1": "https://api.sumologic.com",
    "US2": "https://api.us2.sumologic.com",
    "EU":  "https://api.eu.sumologic.com",
    "AU":  "https://api.au.sumologic.com",
    "JP":  "https://api.jp.sumologic.com",
    "CA":  "https://api.ca.sumologic.com",
    "IN":  "https://api.in.sumologic.com",
    "DE":  "https://api.de.sumologic.com",
    "KR":  "https://api.kr.sumologic.com",
    "CH":  "https://api.ch.sumologic.com",
    "ESC": "https://api.esc.sumologic.com",
    "FED": "https://api.fed.sumologic.com",
}


def endpoint_for_region(region: str | None) -> str | None:
    if not region:
        return None
    return REGION_ENDPOINTS.get(region.strip().upper())


def _resolve_endpoint(cfg: dict, sfx: str) -> str:
    """env var -> config file -> region -> built-in default, then
    normalized. Shared by instance_status and resolve_instance so the two
    can't drift on how an endpoint is derived (resolve_instance additionally
    validates the scheme — see there)."""
    return (os.environ.get(f"SUMO_ENDPOINT{sfx}")
            or cfg.get("endpoint")
            or endpoint_for_region(cfg.get("region"))
            or DEFAULT_ENDPOINT).rstrip("/")


# Load .env once at import time — optional dependency, harmless if absent.
try:
    from dotenv import load_dotenv
    _sumo_home = Path(os.environ.get("SUMO_HOME", Path.home() / ".sumo"))
    for _env_path in (_sumo_home / ".env", Path(".env")):
        if _env_path.exists():
            load_dotenv(_env_path)
            break
except ImportError:
    pass


def _load_toml(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        with open(path, "rb") as fh:
            return tomllib.load(fh)
    except Exception as exc:  # noqa: BLE001 - a broken config file shouldn't crash the tool
        print(f"Warning: could not parse {path}: {exc}", file=sys.stderr)
        return {}


def load_instances() -> dict[str, dict]:
    """Merged instance_name -> config dict; project (.sumo.toml) overrides
    global (~/.sumo/instances.toml). 'default' is always present."""
    global_data = {k.lower(): v for k, v in _load_toml(GLOBAL_CONFIG).get("instances", {}).items()}
    project_data = {k.lower(): v for k, v in _load_toml(PROJECT_CONFIG).get("instances", {}).items()}
    merged = dict(global_data)
    for name, cfg in project_data.items():
        merged[name] = {**merged.get(name, {}), **cfg}
    merged.setdefault("default", {})
    return merged


def instance_status(name: str, cfg: dict | None = None) -> dict:
    """Credential availability (and env-var vs config-file source) for one
    instance `name` — works even if `name` has no instances.toml entry at
    all, since a purely env-defined instance (SUMO_ACCESS_ID_<NAME> etc.)
    is fully valid and never needs one."""
    name = name.lower()
    if cfg is None:
        cfg = load_instances().get(name, {})
    sfx = _env_suffix(name)
    id_env = os.environ.get(f"SUMO_ACCESS_ID{sfx}")
    key_env = os.environ.get(f"SUMO_ACCESS_KEY{sfx}")
    access_id = id_env or cfg.get("access_id")
    access_key = key_env or cfg.get("access_key")
    endpoint = _resolve_endpoint(cfg, sfx)

    def _src(env_val, cfg_val):
        if env_val:
            return "env"
        if cfg_val:
            return "config"
        return "missing"

    return {
        "name": name,
        "endpoint": endpoint,
        "ui_base_url": os.environ.get(f"SUMO_UI_BASE_URL{sfx}") or cfg.get("ui_base_url"),
        "region": cfg.get("region"),
        "description": cfg.get("description"),
        "has_credentials": bool(access_id and access_key),
        "access_id_source": _src(id_env, cfg.get("access_id")),
        "access_key_source": _src(key_env, cfg.get("access_key")),
    }


def list_instances_with_status() -> list[dict]:
    """Every instance with an instances.toml entry (plus 'default', always
    present) and its credential status — backs `sumo-budget-buddy instances
    list`. A purely env-defined instance with no toml entry won't appear
    here (nothing to enumerate it from) but still resolves fine via
    `instance_status(name)` / `resolve_instance(name)` directly."""
    return [instance_status(name, cfg) for name, cfg in load_instances().items()]


_WRITABLE_FIELDS = ("access_id", "access_key", "endpoint", "ui_base_url", "region", "description")

_TOML_ESCAPES = {"\\": "\\\\", '"': '\\"', "\n": "\\n", "\r": "\\r", "\t": "\\t"}


def _toml_escape(value: str) -> str:
    return "".join(_TOML_ESCAPES.get(c, c) for c in str(value))


def _load_toml_strict(path: Path) -> dict:
    """Like `_load_toml`, but lets a parse failure raise instead of
    silently returning {} — used on the write path so a malformed file
    can't look like an empty one and get overwritten, wiping every other
    saved instance."""
    if not path.exists():
        return {}
    with open(path, "rb") as fh:
        return tomllib.load(fh)


def _load_instances_for_write(path: Path) -> dict[str, dict]:
    try:
        data = _load_toml_strict(path)
    except Exception as exc:
        raise SystemExit(
            f"refusing to update {path}: it is not valid TOML ({exc}). "
            "Fix or remove it by hand, then retry."
        )
    return {k.lower(): v for k, v in data.get("instances", {}).items()}


def _write_instances_toml(instances: dict[str, dict]) -> None:
    """Serialise `instances` back to ~/.sumo/instances.toml. Hand-rolled
    rather than a TOML-writer dependency: the shape here is always a flat
    `[instances.<name>]` table of string fields, nothing a generic writer
    is needed for."""
    GLOBAL_CONFIG.parent.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []
    for name, cfg in instances.items():
        lines.append(f'[instances."{_toml_escape(name)}"]')
        ordered_keys = list(_WRITABLE_FIELDS) + [k for k in cfg if k not in _WRITABLE_FIELDS]
        for key in ordered_keys:
            val = cfg.get(key)
            if val is not None:
                lines.append(f'{key} = "{_toml_escape(val)}"')
        lines.append("")
    GLOBAL_CONFIG.write_text("\n".join(lines))


def save_instance(name: str, fields: dict) -> None:
    """Add or update a named instance in ~/.sumo/instances.toml, merging
    with whatever is already there — only the given fields are touched."""
    name = name.lower()
    global_data = _load_instances_for_write(GLOBAL_CONFIG)
    existing = global_data.get(name, {})
    global_data[name] = {**existing, **{k: v for k, v in fields.items() if v is not None}}
    _write_instances_toml(global_data)


def remove_instance(name: str) -> None:
    """Remove a named instance from ~/.sumo/instances.toml."""
    name = name.lower()
    global_data = _load_instances_for_write(GLOBAL_CONFIG)
    if name not in global_data:
        if name in load_instances():
            raise SystemExit(
                f"instance {name!r} is not in {GLOBAL_CONFIG} (it comes from {PROJECT_CONFIG} "
                "or an environment variable) — nothing to remove here"
            )
        raise SystemExit(f"instance {name!r} not found in {GLOBAL_CONFIG}")
    del global_data[name]
    _write_instances_toml(global_data)


def _env_suffix(name: str) -> str:
    return "" if name == "default" else f"_{name.upper()}"


def resolve_instance(name: str) -> dict:
    """Resolve credentials and config for a named instance.

    Resolution order per field: env var -> config file -> built-in default
    (endpoint only). Raises SystemExit with a helpful message if access_id or
    access_key are missing.
    """
    name = name.lower()
    instances = load_instances()
    cfg = instances.get(name, {})
    sfx = _env_suffix(name)

    access_id = os.environ.get(f"SUMO_ACCESS_ID{sfx}") or cfg.get("access_id")
    access_key = os.environ.get(f"SUMO_ACCESS_KEY{sfx}") or cfg.get("access_key")
    endpoint = _resolve_endpoint(cfg, sfx)
    if not endpoint.startswith("https://"):
        raise SystemExit(
            f"SUMO_ENDPOINT must use https://. Got: {endpoint!r}\n"
            "Update the endpoint in your config or environment variable."
        )

    if not access_id or not access_key:
        id_var, key_var = f"SUMO_ACCESS_ID{sfx}", f"SUMO_ACCESS_KEY{sfx}"
        raise SystemExit(
            f"Missing credentials for instance '{name}'.\n"
            f"  export {id_var}=...\n"
            f"  export {key_var}=...\n"
            f"  (or add an [instances.{name}] section to {GLOBAL_CONFIG})"
        )

    return {
        "name": name,
        "access_id": access_id,
        "access_key": access_key,
        "endpoint": endpoint,
        "ui_base_url": os.environ.get(f"SUMO_UI_BASE_URL{sfx}") or cfg.get("ui_base_url"),
        "region": cfg.get("region"),
        "description": cfg.get("description"),
    }
