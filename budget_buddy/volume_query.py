"""volume_query.py — builds and parses the free `sumologic_volume` audit-index
query used to measure ingest volume per metadata value. See
docs/dev/budget-buddy-plan.md, "Research: measuring volume (live-verified)"
for the live test that confirmed this query shape end to end against a real
org.

VOLUME_DIMS is vendored from cli/volume.py's `_DIMS` mapping (no import of
cli.volume, to keep budget_buddy free of any dependency on the parent sumo-ai
repo — see "Standalone portability" in the plan doc) — keep the two in sync
by hand if Sumo changes the sumologic_volume index's internal field names.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

logger = logging.getLogger("budget_buddy.volume_query")

# dim key -> { scope: the sumologic_volume rollup-dimension selector value
# (NOT the customer's actual metadata value — see note below), json_alias:
# the `| json` field-rename clause for that dimension's blob, dim_field: the
# parsed field name as written in the query, dim_key: the same field name as
# Sumo lowercases it in output (camelCase -> lowercase in results).
#
# Quirk: `_index=sumologic_volume _sourceCategory=<scope>` tags entries with
# a FIXED rollup-dimension name (e.g. "sourcecategory_and_tier_volume"), not
# the customer's real _sourceCategory value — the real value is embedded in
# a JSON blob that needs parsing (`| json field=data ...`), then filtered via
# `| where tolowercase(<dim_field>) matches ...` after parsing.
VOLUME_DIMS: dict[str, dict] = {
    "sourcecategory": {
        "scope": "sourcecategory_and_tier_volume",
        "json_alias": '"field","dataTier","sizeInBytes","count" as sourceCategory,dataTier,bytes,count',
        "dim_key": "sourcecategory",
        "dim_field": "sourceCategory",
    },
    "collector": {
        "scope": "collector_and_tier_volume",
        "json_alias": '"field","dataTier","sizeInBytes","count" as collector,dataTier,bytes,count',
        "dim_key": "collector",
        "dim_field": "collector",
    },
    "source": {
        "scope": "source_and_tier_volume",
        "json_alias": '"field","dataTier","sizeInBytes","count" as sourceName,dataTier,bytes,count',
        "dim_key": "sourcename",
        "dim_field": "sourceName",
    },
    "sourcehost": {
        "scope": "sourcehost_and_tier_volume",
        "json_alias": '"field","dataTier","sizeInBytes","count" as sourceHost,dataTier,bytes,count',
        "dim_key": "sourcehost",
        "dim_field": "sourceHost",
    },
    "sourcename": {
        "scope": "sourcename_and_tier_volume",
        "json_alias": '"field","dataTier","sizeInBytes","count" as sourceName,dataTier,bytes,count',
        "dim_key": "sourcename",
        "dim_field": "sourceName",
    },
    "sourceid": {
        "scope": "sourceid_and_tier_volume",
        "json_alias": '"field","dataTier","sizeInBytes","count" as sourceId,dataTier,bytes,count',
        "dim_key": "sourceid",
        "dim_field": "sourceId",
    },
    "view": {
        "scope": "view_and_tier_volume",
        "json_alias": '"field","dataTier","sizeInBytes","count" as index,dataTier,bytes,count',
        "dim_key": "index",
        "dim_field": "index",
    },
}

# Ingest-budget `scope` field name -> VOLUME_DIMS key. Deliberately a small,
# explicit allowlist rather than a case-fold/guess — an unsupported field
# should fail loudly at config-load time (see config.py), not silently
# degrade to a wrong or costly query path.
FIELD_TO_DIM = {
    "_sourceCategory": "sourcecategory",
    "_collector": "collector",
    "_source": "source",
    "_sourceHost": "sourcehost",
    "_sourceName": "sourcename",
    "_sourceId": "sourceid",
    "_view": "view",
}

SUPPORTED_FIELDS = tuple(FIELD_TO_DIM)

_LOWER_TO_FIELD = {f.lower(): f for f in FIELD_TO_DIM}


class UnsupportedFieldError(ValueError):
    """Raised when a scope's `field` has no free sumologic_volume dimension."""


def normalize_field(field: str) -> str:
    """Case-insensitive lookup of a metadata field name to its canonical
    (correctly cased) form, e.g. `_sourcecategory` -> `_sourceCategory` — Sumo
    itself treats field names case-insensitively, so an input's casing should
    never be a reason to reject it."""
    canonical = _LOWER_TO_FIELD.get(field.lower())
    if canonical is None:
        raise UnsupportedFieldError(
            f"field {field!r} has no free sumologic_volume dimension — supported: "
            f"{', '.join(SUPPORTED_FIELDS)}"
        )
    return canonical


class GlobalScopeError(ValueError):
    """Raised when a scope expression's value portion is blank or a bare
    wildcard (e.g. `_sourceCategory=*`, `_sourceCategory=`) — never allowed,
    since it would match every value for that field rather than a specific
    category/pattern. Checked both at config-load time (config.py) and again
    immediately before every budget creation (reconcile.py) as a last line
    of defense against a bad value reaching this point by any other path."""


def parse_scope_expr(expr: str) -> tuple[str, str]:
    """Split a `field=value` scope expression into its two halves, e.g.
    `_sourceCategory=*cloudtrail*` -> (`_sourceCategory`, `*cloudtrail*`).
    The one place this split happens — shared by validate_scope_expr here,
    cli.py's _resolve_scopes, and reconcile.py's _glob_value."""
    field, _, value = expr.partition("=")
    return field, value


def validate_scope_expr(expr: str) -> None:
    """Raise GlobalScopeError if `expr` (a `field=value` string) has a blank
    or effectively-unconstrained value (only `*` characters, e.g. `*`, `**`)."""
    _, value = parse_scope_expr(expr)
    value = value.strip()
    if not value or value.strip("*") == "":
        raise GlobalScopeError(
            f"scope expression {expr!r} has a blank or global (bare wildcard) value — "
            "a budget scope must be non-global, e.g. '_sourceCategory=*cloudtrail*' or "
            "an exact value, never '_sourceCategory=*' or '_sourceCategory='."
        )


@dataclass(frozen=True)
class VolumeRow:
    key: str
    gbytes: float
    events: int

    @property
    def bytes(self) -> int:
        return int(round(self.gbytes * (1024 ** 3)))


def build_query(field: str, filter_glob: str, mode: str) -> str:
    """Build the sumologic_volume query for `field` (e.g. `_sourceCategory`),
    filtered to `filter_glob` (the scope's own wildcard match value, e.g.
    `*cloudtrail*`), grouped per-value (`mode="per_value"`) or summed into a
    single row (`mode="aggregate"`)."""
    field = normalize_field(field)
    if mode not in ("per_value", "aggregate"):
        raise ValueError(f"mode must be 'per_value' or 'aggregate', got {mode!r}")

    d = VOLUME_DIMS[FIELD_TO_DIM[field]]
    df = d["dim_field"]
    group_clause = f" by {df}" if mode == "per_value" else ""
    # Only per_value has multiple output rows to order — aggregate always
    # collapses to 0 or 1 row, so a sort clause there would be a no-op.
    sort_clause = "\n| sort by gbytes desc" if mode == "per_value" else ""
    # filter_glob is the scope's own (operator-controlled) value, but it's
    # still spliced into a double-quoted query literal — escape backslash
    # and embedded quotes so a value containing a literal `"` can't break
    # out of the string or produce a malformed query.
    escaped_glob = filter_glob.replace("\\", "\\\\").replace('"', '\\"')

    return (
        f'_index=sumologic_volume {field}={d["scope"]}\n'
        f'| parse regex "(?<data>\\{{[^\\{{]+\\}})" multi\n'
        f'| json field=data {d["json_alias"]} nodrop\n'
        f'| where tolowercase({df}) matches tolowercase("{escaped_glob}")\n'
        f'| bytes/1Gi as gbytes\n'
        f'| sum(gbytes) as gbytes, sum(count) as events{group_clause}{sort_clause}\n'
    ).rstrip()


def parse_rows(field: str, records: list[dict], mode: str) -> list[VolumeRow]:
    """Parse search-job record rows (each `{"map": {...}}`, string-valued)
    into VolumeRow entries. `mode="aggregate"` always yields exactly 0 or 1
    row (no grouping key in the query), keyed `"*"` for display purposes.

    A `per_value` row whose dimension field is missing/blank (e.g. the
    sumologic_volume JSON blob failed to parse for that row) is SKIPPED
    rather than given a fallback key — a fallback like `"*"` would silently
    become a global, unconstrained budget scope if that row's volume later
    gets enforced. See docs/dev/budget-buddy-plan.md code-review notes."""
    field = normalize_field(field)
    d = VOLUME_DIMS[FIELD_TO_DIM[field]]
    key_field = d["dim_key"]  # Sumo lowercases the camelCase alias in output

    rows: list[VolumeRow] = []
    for rec in records:
        m = rec.get("map", rec)
        gbytes = float(m.get("gbytes", 0) or 0)
        events = int(float(m.get("events", 0) or 0))
        if mode == "per_value":
            key = m.get(key_field)
            if not key:
                logger.warning(
                    "skipping row with missing/blank %r dimension value (row=%r) — "
                    "refusing to fall back to a global scope", key_field, m,
                )
                continue
        else:
            key = "*"
        rows.append(VolumeRow(key=key, gbytes=gbytes, events=events))
    return rows
