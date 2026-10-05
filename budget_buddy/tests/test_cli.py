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

import budget_buddy.paths as bb_paths
from budget_buddy import cli
from budget_buddy.config import ConfigError, ScopeConfig

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


def test_enforce_with_no_config_raises_config_error_not_attribute_error(tmp_path, monkeypatch):
    # Regression: `enforce`'s real argparser never adds --field/--scope-expr
    # (only `evaluate`'s does — ad-hoc enforce isn't supported), so its
    # Namespace has neither attribute at all. _resolve_scopes used to access
    # args.field directly and crash with AttributeError instead of the clean
    # ConfigError `evaluate` gives in the same situation. Build the args via
    # the real parser (not the synthetic _args() helper above) so this
    # actually exercises enforce's parser shape.
    monkeypatch.setattr(cli, "DEFAULT_CONFIG_PATH", tmp_path / "missing.yaml")
    args = cli.build_parser().parse_args(["enforce"])
    assert not hasattr(args, "field")
    with pytest.raises(ConfigError):
        cli._resolve_scopes(args)


def test_run_enforce_sweep_is_scoped_to_targeted_scopes(tmp_path, monkeypatch):
    # Regression: _run_enforce's sweep pass used to omit scope_names, so
    # `enforce --scope foo` would also sweep (delete) any OTHER scope's
    # expired registry entries on the same instance as an unintended side
    # effect — unlike cmd_sweep, which correctly scopes to args.scopes.
    monkeypatch.setattr(bb_paths, "OUTPUT_ROOT", tmp_path)
    captured = {}

    def fake_sweep_registry(registry, budgets_client, *, scope_names=None, dry_run=False):
        captured["scope_names"] = scope_names
        return []

    monkeypatch.setattr(cli, "sweep_registry", fake_sweep_registry)
    monkeypatch.setattr(cli, "enforce_scope", lambda *a, **kw: [])

    class FakeClients:
        def get(self, instance):
            return (object(), object())

    monkeypatch.setattr(cli, "ClientCache", FakeClients)

    scope = ScopeConfig(name="s1", field="_sourceCategory", scope="_sourceCategory=*x*")
    cli._run_enforce([scope], dry_run=False)

    assert captured["scope_names"] == ["s1"]


def test_run_enforce_one_scope_failure_does_not_abort_the_rest(tmp_path, monkeypatch):
    # Regression: enforce_scope's call (via evaluate_scope) was unguarded,
    # so one scope's transient failure (e.g. a search job 500) propagated
    # out of _run_enforce and aborted every other scope's enforcement for
    # that cycle too.
    monkeypatch.setattr(bb_paths, "OUTPUT_ROOT", tmp_path)
    monkeypatch.setattr(cli, "sweep_registry", lambda *a, **kw: [])
    seen = []

    def fake_enforce_scope(scope, *a, **kw):
        seen.append(scope.name)
        if scope.name == "bad":
            raise RuntimeError("search job failed")
        return []

    monkeypatch.setattr(cli, "enforce_scope", fake_enforce_scope)

    class FakeClients:
        def get(self, instance):
            return (object(), object())

    monkeypatch.setattr(cli, "ClientCache", FakeClients)

    scopes = [
        ScopeConfig(name="bad", field="_sourceCategory", scope="_sourceCategory=*x*"),
        ScopeConfig(name="good", field="_sourceCategory", scope="_sourceCategory=*y*"),
    ]
    exit_code = cli._run_enforce(scopes, dry_run=False)

    assert seen == ["bad", "good"]  # "good" still ran despite "bad" failing
    assert exit_code == 1


def test_cmd_delete_takes_the_concurrency_lock(tmp_path, monkeypatch):
    # Regression: cmd_delete used to mutate the registry and delete a live
    # budget without acquiring lock.py's PID-file lock, unlike enforce/sweep
    # — risking a lost registry write if it raced one of them.
    monkeypatch.setattr(bb_paths, "OUTPUT_ROOT", tmp_path)
    from budget_buddy import lock

    class FakeClients:
        def get(self, instance):
            return (object(), object())

    monkeypatch.setattr(cli, "ClientCache", FakeClients)

    args = argparse.Namespace(target="B1", instance="default", force=False, dry_run=False)
    with lock.acquire("default", "enforce"):
        with pytest.raises(lock.LockHeldError):
            cli.cmd_delete(args)


def test_cmd_delete_returns_distinct_exit_code_for_marker_mismatch(tmp_path, monkeypatch):
    # Regression: skipped_marker_mismatch used to return the same exit code
    # (2) as a generic usage/config error, so a wrapper script couldn't tell
    # "refused as a safety measure" apart from "bad CLI usage".
    monkeypatch.setattr(bb_paths, "OUTPUT_ROOT", tmp_path)
    from budget_buddy.budgets import IngestBudget

    class FakeBudgetsClient:
        def get_budget(self, budget_id):
            return IngestBudget(
                id=budget_id, name="hand-made", scope="_sourceCategory=k1", capacity_bytes=1000,
                action="stopCollecting", budget_type="dailyVolume", description="not ours",
                timezone="UTC", reset_time="00:00", audit_threshold=85, usage_bytes=0,
                usage_status="Normal", created_at="", modified_at="",
            )

        def delete_budget(self, budget_id):
            return True

    class FakeClients:
        def get(self, instance):
            return (object(), FakeBudgetsClient())

    monkeypatch.setattr(cli, "ClientCache", FakeClients)

    args = argparse.Namespace(target="B1", instance="default", force=False, dry_run=False)
    assert cli.cmd_delete(args) == 3


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
