"""naming.py — the name/description convention that lets both a human (in the
Sumo UI) and this script itself positively identify a budget-buddy-managed
ingest budget. See docs/dev/budget-buddy-plan.md, "Identifying
budget-buddy-managed budgets" for the rationale.

Name:        bb:<scope_name>:<key>:exp<YYYYMMDD>        (<=128 chars, truncated
             with a content-hash suffix on the key if needed)
Description: [managed-by=budget-buddy] scope=<json-string> key=<json-string>
             field=<field> created=<iso8601> expires=<iso8601>

`scope`/`key` are embedded as JSON string literals (via `json.dumps`), not
raw text — a metadata value containing a literal `"`, whitespace, or other
special character would otherwise corrupt the marker and make it
unparseable, which would make `verify_marker` always reject that budget and
leave it permanently un-sweepable (see docs/dev/budget-buddy-plan.md
code-review notes). For a plain value with nothing to escape, `json.dumps`
produces the exact same `"value"` text a hand-rolled format would, so this is
a strict superset of the old behavior, not a visible format change for the
common case.

The name is for human legibility only and is NEVER parsed back to make a
decision — the registry is the source of truth for *which* budget IDs to act
on, and the description marker is re-checked live before any mutating call as
a second, independent confirmation. See `verify_marker`.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime

MAX_NAME_LEN = 128
MARKER_TAG = "managed-by=budget-buddy"

# A JSON string literal: "..." with any \" / \\ / etc. escape sequences inside.
_JSON_STRING = r'"(?:[^"\\]|\\.)*"'

_MARKER_RE = re.compile(
    r"\[managed-by=budget-buddy\]\s+scope=(?P<scope>" + _JSON_STRING + r")\s+"
    r"key=(?P<key>" + _JSON_STRING + r")\s+"
    r"field=(?P<field>\S+)\s+created=(?P<created>\S+)\s+expires=(?P<expires>\S+)"
)


@dataclass(frozen=True)
class Marker:
    scope_name: str
    key: str
    field: str
    created_at: str
    expires_at: str


def build_name(scope_name: str, key: str, expires_at: datetime) -> str:
    exp = expires_at.strftime("%Y%m%d")
    base = f"bb:{scope_name}:{key}:exp{exp}"
    if len(base) <= MAX_NAME_LEN:
        return base

    # Truncate only the key, append a short content hash of the FULL key so
    # two long keys that happen to share a prefix still produce distinct
    # names. The untruncated key always lives in the description and registry
    # regardless — this truncation is purely cosmetic for the Sumo UI.
    digest = hashlib.sha1(key.encode("utf-8")).hexdigest()[:8]
    suffix = f":exp{exp}"
    fixed = f"bb:{scope_name}:" + "~" + digest + suffix
    budget_for_key = MAX_NAME_LEN - len(fixed)
    if budget_for_key < 1:
        # scope_name itself is pathologically long; fall back to hash-only.
        return (f"bb:~{digest}{suffix}")[:MAX_NAME_LEN]
    truncated_key = key[:budget_for_key]
    return f"bb:{scope_name}:{truncated_key}~{digest}{suffix}"


def build_description(scope_name: str, key: str, field: str,
                       created_at: datetime, expires_at: datetime,
                       extra: str | None = None) -> str:
    desc = (
        f"[{MARKER_TAG}] scope={json.dumps(scope_name)} key={json.dumps(key)} field={field} "
        f"created={created_at.isoformat()} expires={expires_at.isoformat()}"
    )
    if extra:
        desc = f"{desc} {extra}"
    return desc[:1024]


def parse_marker(description: str | None) -> Marker | None:
    """Parse the structured marker out of a budget's `description`, or None
    if the description doesn't carry a recognizable budget-buddy marker."""
    if not description:
        return None
    m = _MARKER_RE.search(description)
    if not m:
        return None
    try:
        scope_name = json.loads(m.group("scope"))
        key = json.loads(m.group("key"))
    except json.JSONDecodeError:
        return None
    return Marker(
        scope_name=scope_name,
        key=key,
        field=m.group("field"),
        created_at=m.group("created"),
        expires_at=m.group("expires"),
    )


def verify_marker(description: str | None, *, expected_scope: str, expected_key: str) -> bool:
    """True iff `description` carries a budget-buddy marker matching the
    scope/key we expect for this budget ID, per the registry. Used as the
    mandatory live check immediately before any PUT/DELETE — see the plan's
    "Defensive verify-before-mutate" — never skip this to save an API call."""
    marker = parse_marker(description)
    if marker is None:
        return False
    return marker.scope_name == expected_scope and marker.key == expected_key
