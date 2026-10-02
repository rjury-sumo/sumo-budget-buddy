#!/usr/bin/env python3
"""
test_output.py — Unit tests for budget_buddy/output.py.

No credentials, no network. Covers --format/--output inference and the
json/csv renderers (table rendering is exercised manually/visually — it's
`rich` terminal output, not worth snapshot-testing here).

Run:
    uv run pytest budget_buddy/tests/test_output.py
"""
import csv
import json
from io import StringIO

import pytest

from budget_buddy.output import infer_format, render_rows


def test_infer_format_explicit_wins():
    assert infer_format("out.csv", "json") == "json"


def test_infer_format_from_extension():
    assert infer_format("out.json", None) == "json"
    assert infer_format("out.csv", None) == "csv"
    assert infer_format("out.txt", None) == "table"


def test_infer_format_defaults_to_table():
    assert infer_format(None, None) == "table"


def test_infer_format_rejects_unknown_format():
    with pytest.raises(ValueError):
        infer_format(None, "yaml")


def test_render_rows_json_to_file(tmp_path):
    out = tmp_path / "out.json"
    rows = [{"a": 1, "b": "x"}, {"a": 2, "b": "y"}]
    render_rows(rows, ["a", "b"], fmt="json", output=str(out))
    assert json.loads(out.read_text()) == rows


def test_render_rows_csv_to_file(tmp_path):
    out = tmp_path / "out.csv"
    rows = [{"a": 1, "b": "x"}, {"a": 2, "b": "y"}]
    render_rows(rows, ["a", "b"], fmt="csv", output=str(out))
    reader = csv.DictReader(StringIO(out.read_text()))
    got = list(reader)
    assert got == [{"a": "1", "b": "x"}, {"a": "2", "b": "y"}]


def test_render_rows_csv_ignores_extra_fields_not_in_columns(tmp_path):
    out = tmp_path / "out.csv"
    rows = [{"a": 1, "b": "x", "c": "ignored"}]
    render_rows(rows, ["a", "b"], fmt="csv", output=str(out))
    reader = csv.DictReader(StringIO(out.read_text()))
    assert list(reader.fieldnames) == ["a", "b"]


def test_render_rows_unknown_format_raises():
    with pytest.raises(ValueError):
        render_rows([], ["a"], fmt="xml")
