#!/usr/bin/env python3
"""
test_instance_config.py — Unit tests for budget_buddy/instance_config.py's
write path (save_instance/remove_instance) and status reporting
(instance_status/list_instances_with_status).

No credentials, no network. Isolates ~/.sumo/instances.toml under tmp_path
by monkeypatching GLOBAL_CONFIG, same pattern as test_lock.py/test_registry.py.

Run:
    uv run pytest budget_buddy/tests/test_instance_config.py
"""
from budget_buddy import instance_config as ic


def test_save_instance_writes_new_section(tmp_path, monkeypatch):
    monkeypatch.setattr(ic, "GLOBAL_CONFIG", tmp_path / "instances.toml")
    ic.save_instance("prod", {"access_id": "id123", "access_key": "key456", "region": "US1"})

    data = ic._load_toml(ic.GLOBAL_CONFIG)["instances"]["prod"]
    assert data == {"access_id": "id123", "access_key": "key456", "region": "US1"}


def test_save_instance_merges_partial_update(tmp_path, monkeypatch):
    monkeypatch.setattr(ic, "GLOBAL_CONFIG", tmp_path / "instances.toml")
    ic.save_instance("prod", {"access_id": "id123", "access_key": "key456"})
    ic.save_instance("prod", {"endpoint": "https://api.eu.sumologic.com"})

    data = ic._load_toml(ic.GLOBAL_CONFIG)["instances"]["prod"]
    assert data == {"access_id": "id123", "access_key": "key456",
                     "endpoint": "https://api.eu.sumologic.com"}


def test_save_instance_leaves_other_instances_untouched(tmp_path, monkeypatch):
    monkeypatch.setattr(ic, "GLOBAL_CONFIG", tmp_path / "instances.toml")
    ic.save_instance("prod", {"access_id": "id123", "access_key": "key456"})
    ic.save_instance("dev", {"access_id": "id789", "access_key": "key000"})

    data = ic._load_toml(ic.GLOBAL_CONFIG)["instances"]
    assert set(data) == {"prod", "dev"}
    assert data["prod"]["access_id"] == "id123"


def test_save_instance_escapes_quotes_and_backslashes(tmp_path, monkeypatch):
    monkeypatch.setattr(ic, "GLOBAL_CONFIG", tmp_path / "instances.toml")
    ic.save_instance("prod", {"description": 'has "quotes" and \\backslash'})

    data = ic._load_toml(ic.GLOBAL_CONFIG)["instances"]["prod"]
    assert data["description"] == 'has "quotes" and \\backslash'


def test_remove_instance_deletes_section(tmp_path, monkeypatch):
    monkeypatch.setattr(ic, "GLOBAL_CONFIG", tmp_path / "instances.toml")
    ic.save_instance("prod", {"access_id": "id123", "access_key": "key456"})
    ic.remove_instance("prod")

    assert "prod" not in ic._load_toml(ic.GLOBAL_CONFIG).get("instances", {})


def test_remove_instance_missing_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(ic, "GLOBAL_CONFIG", tmp_path / "instances.toml")
    try:
        ic.remove_instance("ghost")
        assert False, "expected SystemExit"
    except SystemExit as exc:
        assert "ghost" in str(exc)


def test_instance_status_prefers_env_over_config(tmp_path, monkeypatch):
    monkeypatch.setattr(ic, "GLOBAL_CONFIG", tmp_path / "instances.toml")
    ic.save_instance("prod", {"access_id": "cfg_id", "access_key": "cfg_key"})
    monkeypatch.setenv("SUMO_ACCESS_ID_PROD", "env_id")

    status = ic.instance_status("prod")
    assert status["access_id_source"] == "env"
    assert status["access_key_source"] == "config"
    assert status["has_credentials"] is True


def test_instance_status_works_without_any_toml_entry(tmp_path, monkeypatch):
    monkeypatch.setattr(ic, "GLOBAL_CONFIG", tmp_path / "instances.toml")
    monkeypatch.setenv("SUMO_ACCESS_ID_PROD", "env_id")
    monkeypatch.setenv("SUMO_ACCESS_KEY_PROD", "env_key")

    status = ic.instance_status("prod")
    assert status["has_credentials"] is True
    assert status["access_id_source"] == status["access_key_source"] == "env"


def test_list_instances_with_status_always_includes_default(tmp_path, monkeypatch):
    monkeypatch.setattr(ic, "GLOBAL_CONFIG", tmp_path / "instances.toml")
    monkeypatch.setattr(ic, "PROJECT_CONFIG", tmp_path / "missing.sumo.toml")

    names = {i["name"] for i in ic.list_instances_with_status()}
    assert names == {"default"}
