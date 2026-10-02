"""output.py — shared rendering for `list`/`evaluate`/`status`: table (rich,
matching the rest of this repo's `cli/resources.py` convention), JSON, or
CSV, to stdout or to a file. See docs/dev/budget-buddy-plan.md, "Commands",
`list`'s `--format`/`--output` flags.
"""
from __future__ import annotations

import csv
import json
import sys
from io import StringIO
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.table import Table

VALID_FORMATS = ("table", "json", "csv")

_EXT_TO_FORMAT = {".json": "json", ".csv": "csv"}


def infer_format(output: str | None, requested: str | None) -> str:
    """`--format` wins if given; otherwise infer from `--output`'s extension;
    otherwise default to table."""
    if requested:
        if requested not in VALID_FORMATS:
            raise ValueError(f"format must be one of {VALID_FORMATS}, got {requested!r}")
        return requested
    if output:
        return _EXT_TO_FORMAT.get(Path(output).suffix.lower(), "table")
    return "table"


def render_rows(rows: list[dict[str, Any]], columns: list[str], *,
                 fmt: str, output: str | None = None, title: str | None = None) -> None:
    """Render `rows` (each a dict of column_name -> value) as `fmt` to
    `output` (a file path) or stdout if `output` is None."""
    if fmt == "json":
        text = json.dumps(rows, indent=2, default=str)
    elif fmt == "csv":
        buf = StringIO()
        writer = csv.DictWriter(buf, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({c: row.get(c, "") for c in columns})
        text = buf.getvalue()
    elif fmt == "table":
        _render_table(rows, columns, output=output, title=title)
        return
    else:
        raise ValueError(f"unknown format {fmt!r}")

    if output:
        Path(output).write_text(text)
    else:
        sys.stdout.write(text if text.endswith("\n") else text + "\n")


def render_detail(fields: dict[str, Any], *, output: str | None = None, title: str | None = None) -> None:
    """Vertical field/value table for a single record — e.g. `status` — rather
    than one very wide row, matching cli/resources.py's `_print_detail`."""
    file_handle = open(output, "w") if output else None
    try:
        console = Console(file=file_handle, width=200 if file_handle else None)
        t = Table(title=title, show_header=False, box=None)
        t.add_column("field", style="bold cyan", min_width=20)
        t.add_column("value", overflow="fold")
        for k, v in fields.items():
            t.add_row(k, str(v))
        console.print(t)
    finally:
        if file_handle:
            file_handle.close()


def _render_table(rows: list[dict[str, Any]], columns: list[str], *,
                   output: str | None, title: str | None) -> None:
    file_handle = open(output, "w") if output else None
    try:
        console = Console(file=file_handle, width=200 if file_handle else None)
        t = Table(title=title, show_lines=False)
        for col in columns:
            t.add_column(col, overflow="fold")
        for row in rows:
            t.add_row(*(str(row.get(c, "")) for c in columns))
        console.print(t)
    finally:
        if file_handle:
            file_handle.close()
