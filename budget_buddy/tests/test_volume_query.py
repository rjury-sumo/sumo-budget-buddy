#!/usr/bin/env python3
"""
test_volume_query.py — Unit tests for budget_buddy/volume_query.py.

No credentials, no network. Covers query construction and result-row parsing
for the free `sumologic_volume` index technique — see
docs/dev/budget-buddy-plan.md, "Research: measuring volume (live-verified)".

Run:
    uv run pytest budget_buddy/tests/test_volume_query.py
"""
import pytest

from budget_buddy.volume_query import (
    FIELD_TO_DIM,
    GlobalScopeError,
    UnsupportedFieldError,
    VolumeRow,
    build_query,
    parse_rows,
    validate_scope_expr,
)


def test_build_query_per_value_includes_group_by_and_filter():
    q = build_query("_sourceCategory", "*cloudtrail*", "per_value")
    assert "_index=sumologic_volume" in q
    assert '_sourceCategory=sourcecategory_and_tier_volume' in q
    assert 'tolowercase("*cloudtrail*")' in q
    assert "by sourceCategory" in q


def test_build_query_aggregate_has_no_group_by():
    q = build_query("_sourceCategory", "*cloudtrail*", "aggregate")
    assert "by sourceCategory" not in q
    assert "sum(gbytes) as gbytes, sum(count) as events" in q


def test_build_query_per_value_sorts_by_volume_desc():
    q = build_query("_sourceCategory", "*cloudtrail*", "per_value")
    assert "| sort by gbytes desc" in q


def test_build_query_aggregate_has_no_sort_clause():
    q = build_query("_sourceCategory", "*cloudtrail*", "aggregate")
    assert "sort" not in q


def test_build_query_unsupported_field_raises():
    with pytest.raises(UnsupportedFieldError):
        build_query("_notAField", "*x*", "per_value")


def test_build_query_bad_mode_raises():
    with pytest.raises(ValueError):
        build_query("_sourceCategory", "*x*", "bogus")


def test_every_supported_field_builds_without_error():
    for field in FIELD_TO_DIM:
        q = build_query(field, "*x*", "per_value")
        assert field in q


def test_parse_rows_per_value_extracts_key_and_bytes():
    records = [{"map": {"sourcecategory": "aws/cloudtrail/logs", "gbytes": "0.5", "events": "100"}}]
    rows = parse_rows("_sourceCategory", records, "per_value")
    assert rows == [VolumeRow(key="aws/cloudtrail/logs", gbytes=0.5, events=100)]
    assert rows[0].bytes == int(0.5 * 1024 ** 3)


def test_parse_rows_aggregate_ignores_grouping_key():
    records = [{"map": {"gbytes": "1.0", "events": "200"}}]
    rows = parse_rows("_sourceCategory", records, "aggregate")
    assert len(rows) == 1
    assert rows[0].key == "*"
    assert rows[0].gbytes == 1.0


def test_parse_rows_empty_records_yields_empty_list():
    assert parse_rows("_sourceCategory", [], "per_value") == []


def test_parse_rows_handles_unflattened_map_dict():
    # Rows may or may not already be unwrapped from {"map": ...} depending on
    # caller — parse_rows accepts either.
    records = [{"sourcecategory": "x", "gbytes": "0.1", "events": "1"}]
    rows = parse_rows("_sourceCategory", records, "per_value")
    assert rows[0].key == "x"


def test_parse_rows_skips_rows_missing_dimension_value_instead_of_defaulting_to_star():
    # Regression: a row whose dimension field failed to parse must be
    # dropped, never silently given key="*" (which would later be used
    # verbatim as a global, unconstrained budget scope).
    records = [
        {"map": {"gbytes": "0.5", "events": "100"}},  # sourcecategory missing entirely
        {"map": {"sourcecategory": "", "gbytes": "0.2", "events": "10"}},  # blank
        {"map": {"sourcecategory": "aws/cloudtrail/logs", "gbytes": "0.1", "events": "5"}},
    ]
    rows = parse_rows("_sourceCategory", records, "per_value")
    assert [r.key for r in rows] == ["aws/cloudtrail/logs"]
    assert all(r.key != "*" for r in rows)


def test_build_query_accepts_lowercase_field_name():
    # Sumo treats field names case-insensitively — lowercase/mixed-case input
    # must resolve to the same canonical dimension as the exact-cased name.
    q_lower = build_query("_sourcecategory", "*cloudtrail*", "per_value")
    q_canonical = build_query("_sourceCategory", "*cloudtrail*", "per_value")
    assert q_lower == q_canonical


def test_parse_rows_accepts_lowercase_field_name():
    records = [{"map": {"sourcecategory": "aws/cloudtrail/logs", "gbytes": "0.1", "events": "5"}}]
    rows = parse_rows("_SOURCECATEGORY", records, "per_value")
    assert [r.key for r in rows] == ["aws/cloudtrail/logs"]


def test_validate_scope_expr_rejects_bare_wildcard():
    with pytest.raises(GlobalScopeError):
        validate_scope_expr("_sourceCategory=*")


def test_validate_scope_expr_rejects_multiple_wildcards_only():
    with pytest.raises(GlobalScopeError):
        validate_scope_expr("_sourceCategory=**")


def test_validate_scope_expr_rejects_blank_value():
    with pytest.raises(GlobalScopeError):
        validate_scope_expr("_sourceCategory=")


def test_validate_scope_expr_rejects_whitespace_only_value():
    with pytest.raises(GlobalScopeError):
        validate_scope_expr("_sourceCategory=   ")


def test_validate_scope_expr_accepts_narrowing_patterns():
    validate_scope_expr("_sourceCategory=*cloudtrail*")
    validate_scope_expr("_sourceCategory=aws/cloudtrail/logs")
    validate_scope_expr("_sourceCategory=test/foo/*")


def test_build_query_escapes_embedded_quote_in_filter_glob():
    # Regression: filter_glob was spliced into a double-quoted query literal
    # unescaped — a value containing a literal `"` could break out of the
    # string or alter the query's meaning.
    q = build_query("_sourceCategory", 'foo"bar', "per_value")
    assert 'tolowercase("foo\\"bar")' in q
    # The quote must not appear unescaped (which would terminate the string
    # literal early).
    assert 'tolowercase("foo"bar")' not in q
