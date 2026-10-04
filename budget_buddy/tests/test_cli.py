#!/usr/bin/env python3
"""
test_cli.py — Unit tests for budget_buddy/cli.py's scope resolution.

No credentials, no network. Covers the --config / default-path / ad-hoc
fallback order in _resolve_scopes().

Run:
    uv run pytest budget_buddy/tests/test_cli.py
"""
import argparse

import pytest

from budget_buddy import cli
from budget_buddy.config import ConfigError

MINIMAL = """
scopes:
  - name: cloudtrail-prod
    field: _sourceCategory
    scope: "_sourceCategory=*cloudtrail*"
"""


def _args(**overrides):
    base = dict(config=None, scopes=None, all=False, field=None, scope_expr=None,
                mode="per_value", window="today", tz="America/Los_Angeles",
                threshold_bytes=5 * 1024 ** 3, instance="default")
    base.update(overrides)
    return argparse.Namespace(**base)


def test_explicit_config_path_is_used(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "DEFAULT_CONFIG_PATH", tmp_path / "unused.yaml")
    cfg_file = tmp_path / "budget-buddy.yaml"
    cfg_file.write_text(MINIMAL)
    scopes = cli._resolve_scopes(_args(config=str(cfg_file), all=True))
    assert [s.name for s in scopes] == ["cloudtrail-prod"]


def test_falls_back_to_default_config_path_when_present(tmp_path, monkeypatch):
    default_path = tmp_path / "budget-buddy.yaml"
    default_path.write_text(MINIMAL)
    monkeypatch.setattr(cli, "DEFAULT_CONFIG_PATH", default_path)
    scopes = cli._resolve_scopes(_args(all=True))
    assert [s.name for s in scopes] == ["cloudtrail-prod"]


def test_adhoc_args_used_when_no_config_present(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "DEFAULT_CONFIG_PATH", tmp_path / "missing.yaml")
    scopes = cli._resolve_scopes(_args(field="_sourceCategory", scope_expr="_sourceCategory=*x*"))
    assert [s.name for s in scopes] == ["adhoc"]


def test_adhoc_scope_expr_defaults_to_bare_wildcard_for_field(tmp_path, monkeypatch):
    """--field with no --scope-expr means "every value for this field" —
    the read-only ad-hoc evaluate path, unlike a config-file scope, is never
    reachable from enforce so this is safe to allow."""
    monkeypatch.setattr(cli, "DEFAULT_CONFIG_PATH", tmp_path / "missing.yaml")
    scopes = cli._resolve_scopes(_args(field="_sourceCategory"))
    assert [s.scope for s in scopes] == ["_sourceCategory=*"]


def test_adhoc_field_inferred_from_scope_expr_when_omitted(tmp_path, monkeypatch):
    """--scope-expr already names the field on its left-hand side, so
    --field is redundant and shouldn't be required."""
    monkeypatch.setattr(cli, "DEFAULT_CONFIG_PATH", tmp_path / "missing.yaml")
    scopes = cli._resolve_scopes(_args(scope_expr="_sourceCategory=*cloudtrail*"))
    assert [(s.field, s.scope) for s in scopes] == [("_sourceCategory", "_sourceCategory=*cloudtrail*")]


def test_error_message_names_default_path_when_nothing_given(tmp_path, monkeypatch):
    default_path = tmp_path / "missing.yaml"
    monkeypatch.setattr(cli, "DEFAULT_CONFIG_PATH", default_path)
    with pytest.raises(ConfigError, match=str(default_path)):
        cli._resolve_scopes(_args())


def test_instances_show_never_reveals_any_access_key_characters(tmp_path, monkeypatch, capsys):
    from budget_buddy import instance_config as ic
    monkeypatch.setattr(ic, "GLOBAL_CONFIG", tmp_path / "instances.toml")
    secret = "super-secret-access-key-value"
    ic.save_instance("prod", {"access_id": "id123456", "access_key": secret})

    rc = cli.cmd_instances_show(argparse.Namespace(name="prod"))
    out = capsys.readouterr().out

    assert rc == 0
    assert secret not in out
    assert secret[:4] not in out
    assert "id12" in out  # access_id (not a secret) still gets a short preview


def test_instances_set_requires_at_least_one_field(tmp_path, monkeypatch, capsys):
    from budget_buddy import instance_config as ic
    monkeypatch.setattr(ic, "GLOBAL_CONFIG", tmp_path / "instances.toml")

    rc = cli.cmd_instances_set(argparse.Namespace(
        name="prod", access_id=None, access_key=None, endpoint=None,
        ui_base_url=None, region=None, description=None,
    ))
    assert rc == 2
