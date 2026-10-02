"""
instance_config.py — named Sumo Logic instance (org) credential resolution
for budget_buddy.

Vendored and trimmed from cli/config.py (read-only resolution only — no
`sumo instances add/remove` equivalent here, budget_buddy never needs to
write this file) so budget_buddy has no import dependency on the parent
sumo-ai repo. Deliberately kept compatible with the same ~/.sumo/instances.toml
file and SUMO_ACCESS_ID[_NAME]-style env var convention, so a budget_buddy
installed alongside the main `sumo` CLI shares the same instance definitions
without any code coupling between the two.

Config file (~/.sumo/instances.toml)
-------------------------------------
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
    endpoint = (os.environ.get(f"SUMO_ENDPOINT{sfx}")
                or cfg.get("endpoint")
                or endpoint_for_region(cfg.get("region"))
                or DEFAULT_ENDPOINT).rstrip("/")
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
