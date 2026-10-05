#!/usr/bin/env python3
"""
test_naming.py — Unit tests for budget_buddy/naming.py.

Covers the name/description convention and the defensive marker
verification used immediately before any mutating API call — see
docs/dev/budget-buddy-plan.md, "Identifying budget-buddy-managed budgets".

Run:
    uv run pytest budget_buddy/tests/test_naming.py
"""
from datetime import datetime, timezone

from budget_buddy.naming import (
    MAX_NAME_LEN,
    build_description,
    build_name,
    parse_marker,
    verify_marker,
)

EXP = datetime(2026, 10, 2, 23, 59, 59, tzinfo=timezone.utc)
CREATED = datetime(2026, 10, 2, 0, 0, 0, tzinfo=timezone.utc)


def test_build_name_basic_shape():
    name = build_name("cloudtrail-prod", "aws/observability/cloudtrail/logs", EXP)
    assert name == "bb:cloudtrail-prod:aws/observability/cloudtrail/logs:exp20261002"
    assert len(name) <= MAX_NAME_LEN


def test_build_name_truncates_long_key_with_hash_suffix():
    long_key = "x" * 200
    name = build_name("scope1", long_key, EXP)
    assert len(name) <= MAX_NAME_LEN
    assert name.startswith("bb:scope1:")
    assert "~" in name
    assert name.endswith(":exp20261002")


def test_build_name_truncation_is_deterministic_and_distinguishes_keys():
    name_a = build_name("scope1", "x" * 200 + "A", EXP)
    name_b = build_name("scope1", "x" * 200 + "B", EXP)
    assert name_a != name_b


def test_build_description_contains_marker_and_fields():
    desc = build_description("cloudtrail-prod", "aws/.../logs", "_sourceCategory", CREATED, EXP)
    assert "[managed-by=budget-buddy]" in desc
    assert 'scope="cloudtrail-prod"' in desc
    assert 'key="aws/.../logs"' in desc
    assert "field=_sourceCategory" in desc
    assert len(desc) <= 1024


def test_build_description_escapes_quotes_in_key():
    # A metadata value containing a literal double-quote must not corrupt
    # the marker format or silently truncate the key on round-trip.
    tricky_key = 'foo"bar'
    desc = build_description("scope1", tricky_key, "_sourceCategory", CREATED, EXP)
    marker = parse_marker(desc)
    assert marker is not None
    assert marker.key == tricky_key
    assert verify_marker(desc, expected_scope="scope1", expected_key=tricky_key) is True


def test_build_description_escapes_whitespace_in_scope_name():
    desc = build_description("scope with spaces", "key1", "_sourceCategory", CREATED, EXP)
    marker = parse_marker(desc)
    assert marker is not None
    assert marker.scope_name == "scope with spaces"


def test_build_description_truncates_long_key_without_breaking_the_marker():
    # Regression: a blind desc[:1024] slice could land mid-field (e.g. cut
    # "expires=..." short), making verify_marker reject the budget forever.
    # Truncating the key instead must keep the marker fully parseable.
    long_key = "aws/observability/cloudtrail/" + "x" * 2000
    desc = build_description("scope1", long_key, "_sourceCategory", CREATED, EXP)
    assert len(desc) <= 1024
    marker = parse_marker(desc)
    assert marker is not None
    assert marker.field == "_sourceCategory"
    assert marker.created_at == CREATED.isoformat()
    assert marker.expires_at == EXP.isoformat()
    assert marker.key != long_key  # truncated
    assert marker.key.startswith("aws/observability/cloudtrail/")


def test_build_description_truncation_is_deterministic_and_distinguishes_keys():
    desc_a = build_description("scope1", "x" * 2000 + "A", "_sourceCategory", CREATED, EXP)
    desc_b = build_description("scope1", "x" * 2000 + "B", "_sourceCategory", CREATED, EXP)
    assert desc_a != desc_b
    assert parse_marker(desc_a).key != parse_marker(desc_b).key


def test_parse_marker_round_trips_build_description():
    desc = build_description("cloudtrail-prod", "aws/observability/cloudtrail/logs",
                              "_sourceCategory", CREATED, EXP)
    marker = parse_marker(desc)
    assert marker is not None
    assert marker.scope_name == "cloudtrail-prod"
    assert marker.key == "aws/observability/cloudtrail/logs"
    assert marker.field == "_sourceCategory"


def test_parse_marker_returns_none_for_foreign_description():
    assert parse_marker("created by Jane for the payments team, do not touch") is None
    assert parse_marker(None) is None
    assert parse_marker("") is None


def test_verify_marker_matches_expected_scope_and_key():
    desc = build_description("scope1", "key1", "_sourceCategory", CREATED, EXP)
    assert verify_marker(desc, expected_scope="scope1", expected_key="key1") is True


def test_verify_marker_rejects_mismatched_scope_or_key():
    desc = build_description("scope1", "key1", "_sourceCategory", CREATED, EXP)
    assert verify_marker(desc, expected_scope="scope1", expected_key="key2") is False
    assert verify_marker(desc, expected_scope="scope2", expected_key="key1") is False


def test_verify_marker_rejects_description_without_marker():
    assert verify_marker("hand-edited by a human", expected_scope="scope1", expected_key="key1") is False
