#!/usr/bin/env python3
"""
test_config.py — Unit tests for budget_buddy/config.py.

No credentials, no network. Covers YAML loading, defaults merging, and
validation errors — see docs/dev/budget-buddy-plan.md, "Config file".

Run:
    uv run pytest budget_buddy/tests/test_config.py
"""
import pytest

from budget_buddy.config import ConfigError, ScopeConfig, load_config, select_scopes

MINIMAL = """
scopes:
  - name: cloudtrail-prod
    field: _sourceCategory
    scope: "_sourceCategory=*cloudtrail*"
"""

WITH_DEFAULTS_AND_OVERRIDE = """
instance: prod

defaults:
  threshold_bytes: 1000
  action: keepCollecting

scopes:
  - name: a
    field: _sourceCategory
    scope: "_sourceCategory=*a*"
  - name: b
    field: _collector
    scope: "_collector=*b*"
    threshold_bytes: 999
    mode: aggregate
"""


def _write(tmp_path, text):
    p = tmp_path / "budget-buddy.yaml"
    p.write_text(text)
    return p


def test_minimal_config_applies_builtin_defaults(tmp_path):
    cfg = load_config(_write(tmp_path, MINIMAL))
    s = cfg.scopes["cloudtrail-prod"]
    assert s.field == "_sourceCategory"
    assert s.mode == "per_value"
    assert s.budget_type == "dailyVolume"
    assert s.action == "stopCollecting"
    assert s.threshold_bytes == 5 * 1024 ** 3
    assert s.instance == "default"


def test_top_level_instance_and_defaults_merge_and_override(tmp_path):
    cfg = load_config(_write(tmp_path, WITH_DEFAULTS_AND_OVERRIDE))
    a, b = cfg.scopes["a"], cfg.scopes["b"]
    assert a.instance == "prod"
    assert a.threshold_bytes == 1000  # from `defaults`
    assert a.action == "keepCollecting"
    assert b.threshold_bytes == 999  # per-scope override wins over defaults
    assert b.mode == "aggregate"


def test_aggregate_mode_with_explicit_max_budgets_warns(tmp_path):
    text = WITH_DEFAULTS_AND_OVERRIDE.replace(
        "mode: aggregate", "mode: aggregate\n    max_budgets: 5"
    )
    cfg = load_config(_write(tmp_path, text))
    assert any("max_budgets is a no-op" in w for w in cfg.warnings)


def test_missing_required_key_raises(tmp_path):
    bad = "scopes:\n  - name: x\n    field: _sourceCategory\n"  # no `scope`
    with pytest.raises(ConfigError):
        load_config(_write(tmp_path, bad))


def test_unsupported_field_raises(tmp_path):
    bad = 'scopes:\n  - name: x\n    field: _notAField\n    scope: "_notAField=*"\n'
    with pytest.raises(ConfigError):
        load_config(_write(tmp_path, bad))


def test_scope_not_matching_field_prefix_raises(tmp_path):
    bad = 'scopes:\n  - name: x\n    field: _sourceCategory\n    scope: "_collector=*x*"\n'
    with pytest.raises(ConfigError):
        load_config(_write(tmp_path, bad))


def test_scope_with_bare_wildcard_value_raises(tmp_path):
    bad = 'scopes:\n  - name: x\n    field: _sourceCategory\n    scope: "_sourceCategory=*"\n'
    with pytest.raises(ConfigError):
        load_config(_write(tmp_path, bad))


def test_scope_with_blank_value_raises(tmp_path):
    bad = 'scopes:\n  - name: x\n    field: _sourceCategory\n    scope: "_sourceCategory="\n'
    with pytest.raises(ConfigError):
        load_config(_write(tmp_path, bad))


def test_duplicate_scope_name_raises(tmp_path):
    dup = MINIMAL + MINIMAL.replace("scopes:\n", "")
    with pytest.raises(ConfigError):
        load_config(_write(tmp_path, dup))


def test_unknown_key_in_scope_raises(tmp_path):
    bad = MINIMAL.replace("field: _sourceCategory", "field: _sourceCategory\n    bogus_key: 1")
    with pytest.raises(ConfigError):
        load_config(_write(tmp_path, bad))


def test_missing_file_raises():
    with pytest.raises(ConfigError):
        load_config("/does/not/exist.yaml")


def test_select_scopes_all(tmp_path):
    cfg = load_config(_write(tmp_path, WITH_DEFAULTS_AND_OVERRIDE))
    assert {s.name for s in select_scopes(cfg, None, True)} == {"a", "b"}


def test_select_scopes_by_name(tmp_path):
    cfg = load_config(_write(tmp_path, WITH_DEFAULTS_AND_OVERRIDE))
    assert [s.name for s in select_scopes(cfg, ["b"], False)] == ["b"]


def test_select_scopes_unknown_name_raises(tmp_path):
    cfg = load_config(_write(tmp_path, WITH_DEFAULTS_AND_OVERRIDE))
    with pytest.raises(ConfigError):
        select_scopes(cfg, ["nope"], False)


def test_select_scopes_requires_name_or_all(tmp_path):
    cfg = load_config(_write(tmp_path, WITH_DEFAULTS_AND_OVERRIDE))
    with pytest.raises(ConfigError):
        select_scopes(cfg, None, False)


@pytest.mark.parametrize("field,value", [
    ("mode", "bogus"),
    ("budget_type", "bogus"),
    ("action", "bogus"),
    ("threshold_bytes", 0),
    ("max_budgets", 0),
    ("audit_threshold", 0),
    ("audit_threshold", 100),
])
def test_scope_config_field_validation(field, value):
    kwargs = dict(name="x", field="_sourceCategory", scope="_sourceCategory=*x*")
    kwargs[field] = value
    with pytest.raises(ConfigError):
        ScopeConfig(**kwargs)
