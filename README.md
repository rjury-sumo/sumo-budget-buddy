# sumo-budget-buddy

Ephemeral, per-category ingest budget enforcement for Sumo Logic — a
workaround for the fact that native ingest budgets are capped (a soft org
limit, commonly ~100 active at once) while real quota needs often span
hundreds of `_sourceCategory` values.

`budget_buddy` polls ingest volume for one or more configured scopes, finds
which individual metadata values (or an aggregate scope) have exceeded a
threshold, and creates *short-lived* native `/v2/ingestBudgets` entries only
for the offenders — tracked locally with a TTL and swept away at the next
calendar-day boundary. That keeps the number of simultaneously-active native
budgets low while giving quota-like coverage over an arbitrarily large set of
categories, as long as it's run on a schedule (cron, a scheduled CI job,
etc.).

Full design rationale, API research, and live-verification notes:
[`docs/budget-buddy-plan.md`](docs/budget-buddy-plan.md).

## Quickstart

A worked example, taken from a real run against a low-volume
`_sourceCategory` called `otel/mac` — see each linked section below for the
full flag reference.

This example's `scope` pattern (`*otel*`) happens to match only that one
value in the test org it ran against, which makes it easy to follow step by
step. The typical real-world use is broader: a wildcard like
`_sourceCategory=test/myapps/*` matching dozens or hundreds of distinct
category values at once, each getting its own independently tracked,
independently expiring budget under `mode: per_value` (the default) — one
scope config entry, many budgets. `otel/mac` below stands in for "whichever
one of those values happened to be over threshold this cycle."

```bash
# 0. Credentials (see Credentials below) — SUMO_ACCESS_ID / SUMO_ACCESS_KEY env vars,
#    or an ~/.sumo/instances.toml entry for a named instance.

# 1. Look before you budget anything — evaluate is always read-only.
sumo-budget-buddy evaluate --scope-expr "_sourceCategory=*otel*" --field _sourceCategory
#   otel/mac   0.0021 GB   9281 events   threshold=5 GiB   over_threshold=False
# Comfortably under the 5 GiB default — nothing to enforce yet at that threshold.
```

```yaml
# 2. Put a scope in ~/.sumo/budget-buddy.yaml. This one is deliberately strict for the
#    example: flag otel/mac as an exception past 1 MB, but cap the resulting budget at
#    just 10 KB so it blocks almost immediately — see "budget_capacity_bytes" below for
#    why those two numbers don't have to match.
scopes:
  - name: otel-mac-demo
    field: _sourceCategory
    scope: "_sourceCategory=*otel*"
    threshold_bytes: 1000000       # 1 MB — the evaluation threshold
    budget_capacity_bytes: 10000   # 10 KB — the actual enforced cap
```

```bash
# 3. Preview before touching anything live.
sumo-budget-buddy enforce --scope otel-mac-demo --dry-run
#   [otel-mac-demo] created: key='otel/mac'  dry-run: would create 'bb:otel-mac-demo:otel/mac:exp...' ...capacity=10000

# 4. Run it for real.
sumo-budget-buddy enforce --scope otel-mac-demo
#   [otel-mac-demo] created: key='otel/mac' budget_id=00000000000096EC

# 5. Check it right away — a brand-new budget always starts at 0 usage, regardless of
#    how much that value had already ingested earlier today (see Config file below).
sumo-budget-buddy status 00000000000096EC
#   usage_bytes 0   usage_status Normal

# 6. Check again a few minutes later, once new ingest has had a chance to arrive.
sumo-budget-buddy status 00000000000096EC
#   usage_bytes 13815   usage_status Exceeded   usage "EXCEEDED (13.5 KB @ trip)"
# stopCollecting has now kicked in for otel/mac until this budget expires or is removed.

# 7. See everything this tool is tracking, with enough detail to act on a row directly.
sumo-budget-buddy list
#   id                scope           key       type         usage                      capacity  action          expires
#   00000000000096EC  otel-mac-demo   otel/mac  dailyVolume  EXCEEDED (13.5 KB @ trip)  9.8 KB    stopCollecting  2026-10-04T23:59:59-07:00

# 8a. Left alone, it's removed automatically at the next calendar-day boundary (`ttl:
#     end_of_day`, in the scope's tz) — enforce always sweeps expired entries first,
#     or run sweep directly: sumo-budget-buddy sweep
#
# 8b. Want it gone sooner? delete removes one budget immediately — guarded by the
#     budget-buddy marker check so it can't be pointed at an unrelated org budget
#     by accident (--force overrides that).
sumo-budget-buddy delete 00000000000096EC
#   deleted: scope=otel-mac-demo key='otel/mac' budget_id=00000000000096EC name='bb:otel-mac-demo:otel/mac:exp...'
```

This tool has no import dependency on any other project — `http_client.py`,
`instance_config.py`, `paths.py`, and the `VOLUME_DIMS` mapping in
`volume_query.py` are self-contained. The only piece shared *by convention,
not by import* with the sibling `sumo` CLI (if you also use it) is: the
`SUMO_ACCESS_ID[_NAME]` / `SUMO_ACCESS_KEY[_NAME]` / `SUMO_ENDPOINT[_NAME]`
environment variable pattern, and the `~/.sumo/` state directory layout — so
`budget_buddy` reads the same `~/.sumo/instances.toml` and env vars as a
co-installed `sumo` CLI, with zero code coupling between the two.

## Installation

```bash
uv sync
uv run sumo-budget-buddy --help
```

(or `pip install -e .` followed by `sumo-budget-buddy --help`).

Third-party dependencies (everything else used is Python 3.11+ stdlib):

| Package | Used for |
| --- | --- |
| `requests` | HTTP calls (Search Job API, `/v2/ingestBudgets`) |
| `pyyaml` | Parsing `budget-buddy.yaml` |
| `rich` | Table rendering (`list`/`status`/table-format `evaluate`) |
| `python-dotenv` | Auto-loads `~/.sumo/.env` / `./.env` if present — silently skipped if not installed |

## Credentials

```bash
export SUMO_ACCESS_ID=...
export SUMO_ACCESS_KEY=...
export SUMO_ENDPOINT=https://api.au.sumologic.com   # default; see region table below
```

Named instances: `SUMO_ACCESS_ID_<NAME>` / `SUMO_ACCESS_KEY_<NAME>` /
`SUMO_ENDPOINT_<NAME>`, **or** an `[instances.<name>]` section in
`~/.sumo/instances.toml`:

```toml
[instances.prod]
access_id  = "..."
access_key = "..."
endpoint   = "https://api.au.sumologic.com"
region     = "AU"
```

**The `instances.toml` section is entirely optional** — the two are
alternatives, not layers you both need. `--instance prod` with just
`SUMO_ACCESS_ID_PROD` / `SUMO_ACCESS_KEY_PROD` exported works with *no*
`[instances.prod]` section at all; nothing needs to be hardcoded in the
config file just because an instance has a name. The config file exists for
persisting values across sessions (so you don't re-export every time) or for
metadata env vars don't carry (`region`, `description`) — and even then any
single field can still be left to its env var instead of being written to
disk. Env vars always win over the config file for a given field.

Manage `instances.toml` without hand-editing it:

```bash
sumo-budget-buddy instances list                    # every configured instance + credential status
sumo-budget-buddy instances show prod                # one instance's detail (secrets masked)
sumo-budget-buddy instances set prod --access-id ... --access-key ... --region AU
sumo-budget-buddy instances remove prod
```

`list`/`show` report, per field, whether its value is coming from an env var
or the config file — `set` only writes the fields you pass, leaving the rest
of that instance's entry (and every other instance) untouched.

| Region | Endpoint |
| --- | --- |
| AU (default) | `https://api.au.sumologic.com` |
| US1 | `https://api.sumologic.com` |
| US2 | `https://api.us2.sumologic.com` |
| EU | `https://api.eu.sumologic.com` |

Auth is HTTP Basic (Access ID = username, Access Key = password). Env vars
always override the config file for the matching instance. Credentials need
write access to `/v2/ingestBudgets` for `enforce`/`sweep` — read-only
credentials are enough for `evaluate`/`list`/`status`.

## Config file

One YAML file defines named, reusable scopes — a run targets one, several, or
all of them by name. `--config PATH` points at it explicitly; if omitted,
`~/.sumo/budget-buddy.yaml` is used when present (same `~/.sumo/` convention
as [`instances.toml`](#credentials)). See
[`budget-buddy.example.yaml`](budget-buddy.example.yaml) for a ready-to-copy
starting point, and
[`docs/budget-buddy-plan.md`](docs/budget-buddy-plan.md#config-file)
for the full schema reference; short version:

```yaml
instance: default            # credential profile; per-scope override allowed

defaults:                    # fallback values; any scope can override any key
  mode: per_value            # per_value | aggregate
  window: today               # today | yesterday | last_Nh | YYYY-MM-DD/YYYY-MM-DD
  tz: America/Los_Angeles
  threshold_bytes: 5368709120  # 5 GiB — crossing this flags a value as an "exception"
  budget_type: dailyVolume     # dailyVolume | minuteVolume
  action: stopCollecting       # stopCollecting | keepCollecting
  max_budgets: 50
  audit_threshold: 85

scopes:
  - name: cloudtrail-prod
    description: "AWS CloudTrail logs, prod org"
    field: _sourceCategory
    scope: "_sourceCategory=*cloudtrail*"
    # all other keys inherited from `defaults` unless overridden here

  - name: test-foo-rollup
    field: _sourceCategory
    scope: "_sourceCategory=test/foo/*"
    mode: per_value              # each matching value tracked independently
    threshold_bytes: 1073741824  # 1 GiB override for this scope
    budget_capacity_bytes: 10485760  # but cap the actual budget at 10 MiB once created
```

`budget_capacity_bytes` (optional, any scope) decouples "what counts as an
exception" from "how small a cap to actually enforce" — it defaults to
`threshold_bytes` if omitted, matching the original all-in-one behavior. Set
it lower to force near-immediate `stopCollecting` once a value is flagged,
regardless of how much it had already ingested before the budget existed —
a native budget's `usageBytes` always starts at 0 at creation, it is never
backfilled with same-day ingest from before the budget existed.

Supported `field` values (the only ones with a free `sumologic_volume` index
dimension to measure against — see `volume_query.py`): `_sourceCategory`,
`_collector`, `_source`, `_sourceHost`, `_sourceName`, `_sourceId`, `_view`.
Each native ingest budget covers exactly one metadata dimension (a hard
constraint of the underlying API) — `mode: per_value` on a wildcard `scope`
pattern is how one scope config can still fan out into many independently
tracked budgets (e.g. `_sourceCategory=test/foo/*` matching 100 distinct
category values).

`scope` must be non-global: a blank or bare-wildcard value
(`_sourceCategory=*`, `_sourceCategory=`) is rejected at config-load time,
and the same check runs again immediately before every budget creation —
a scope this broad would budget *all* data for that field, never the intent
of this tool.

## Commands

### `evaluate` — read-only volume measurement

```
sumo-budget-buddy evaluate (--config PATH (--scope NAME | --all) | [--field FIELD] [--scope-expr EXPR])
                           [--mode per_value|aggregate] [--window WINDOW] [--tz TZ]
                           [--threshold-bytes N] [--instance NAME] [--top N]
                           [--format table|json|csv] [--output PATH]
                           [--log-level TRACE|DEBUG|INFO|WARNING|ERROR]
```

Two ways to run it: against named scopes in a config file, or ad hoc with
`--field`/`--scope-expr` (and optionally `--mode`/`--window`/`--tz`/
`--threshold-bytes`/`--instance`) for a one-off check with no config file at
all. Never touches `/v2/ingestBudgets` or the local registry — safe to run
anytime.

```bash
sumo-budget-buddy evaluate --scope-expr "_sourceCategory=*cloudtrail*" --window today
```

`--field` is only needed when it can't be inferred — `--scope-expr` already
names it on its left-hand side, so `--field _sourceCategory --scope-expr
"_sourceCategory=*cloudtrail*"` and just `--scope-expr
"_sourceCategory=*cloudtrail*"` are equivalent. Field names are matched
case-insensitively either way (`_sourcecategory` and `_sourceCategory` are the
same field to Sumo).

Results are always sorted by volume, largest first — `--top N` caps the
output at the N biggest values, useful for reviewing a wide-open scope before
deciding what to budget. `--scope-expr` can be omitted too, as long as
`--field` is given: it then defaults to `<field>=*`, i.e. every value for
that field. (A bare wildcard is intentionally *not* allowed for a named scope
in a config file, since those can be targeted by `enforce` — a scope that
broad is never a valid budget target. The ad-hoc path here is read-only and
never reaches `enforce`.)

```bash
# top 20 _sourceCategory values by volume, last 24h, across everything
sumo-budget-buddy evaluate --field _sourceCategory --window last_24h --top 20

# same, but only values matching a pattern
sumo-budget-buddy evaluate --field _sourceCategory \
    --scope-expr "_sourceCategory=*prod*" --window last_24h --top 20 \
    --format csv --output usage.csv
```

### `enforce` — the one to put on a schedule

```
sumo-budget-buddy enforce --config PATH (--scope NAME | --all)
                          [--dry-run] [--force]
                          [--log-level TRACE|DEBUG|INFO|WARNING|ERROR]
```

Sweeps anything past its TTL first, then evaluates the targeted scopes and
creates budgets for whatever's over threshold, up to each scope's
`max_budgets` (worst offenders first). Idempotent — safe to run repeatedly
within the same day; an offender that already has an active budget is left
alone. Exits non-zero if a scope's `max_budgets` cap was reached (some
offenders left uncovered this cycle) or a create call failed.

```bash
sumo-budget-buddy enforce --config budget-buddy.yaml --all
```

`--force` clears a stale concurrency lock left behind by a run that crashed
without releasing it — see "Concurrency" below. It does **not** bypass any
budget logic.

### `sweep` — delete expired budget-buddy-managed budgets

```
sumo-budget-buddy sweep [--scope NAME] [--instance NAME] [--dry-run] [--force]
```

`enforce` always sweeps first on its own, so this is mainly for manual
cleanup or inspection (`--dry-run`) between scheduled runs.

### `list` — what's currently tracked

```
sumo-budget-buddy list [--instance NAME] [--all-budgets]
                       [--format table|json|csv] [--output PATH]
```

Shows registry-tracked budgets with live usage, including each row's `id` so
it can be passed straight to `status`/`delete` without a separate lookup.
`--all-budgets` additionally lists every budget on the account (a live API
call), with a `managed` column (yes/no, based on the `[managed-by=budget-buddy]`
description marker) so you can see at a glance what this tool owns versus what
a human or another tool created — budget-buddy only ever mutates the former.
`--format` defaults to a terminal table; `json`/`csv` for scripting, `--output
FILE` to write instead of stdout (format inferred from the extension if
`--format` is omitted).

### `status` — one budget's detail

```
sumo-budget-buddy status <budget-id-or-scope_name:key> [--instance NAME]
```

Accepts either a raw `/v2/ingestBudgets` ID or `scope_name:key` to resolve via
the local registry. Shows capacity, usage, and whether it carries the
budget-buddy marker.

### `delete` — manual fix-up for one budget

```
sumo-budget-buddy delete <budget-id-or-scope_name:key> [--instance NAME]
                         [--force] [--dry-run]
```

`sweep` only ever removes registry entries past their TTL — there is
otherwise no way to undo a mistaken `enforce` before end-of-day. `delete`
fills that gap: it deletes one budget immediately (by raw ID or
`scope_name:key`, same resolution as `status`) and forgets its registry
entry if there is one. It refuses to touch a budget whose description
doesn't carry the `[managed-by=budget-buddy]` marker, or — when given a
`scope_name:key` target — whose marker doesn't match that exact scope/key
(the same check `sweep` applies before its own deletes). `--force`
overrides that guardrail (use with care: it bypasses the "is this actually
ours" check) and, like `enforce`/`sweep`'s `--force`, also clears a stale
concurrency lock. Exits `3` (not the generic usage-error `2`) when refused
for a marker mismatch, so a wrapper script can tell the two apart.

### `instances` — manage `~/.sumo/instances.toml`

```
sumo-budget-buddy instances list [--format table|json|csv] [--output PATH]
sumo-budget-buddy instances show <name>
sumo-budget-buddy instances set <name> [--access-id ID] [--access-key KEY]
                                        [--endpoint URL] [--ui-base-url URL]
                                        [--region LABEL] [--description TEXT]
sumo-budget-buddy instances remove <name>
```

See [Credentials](#credentials) above — this is just a CLI wrapper around
that file so you don't have to hand-edit TOML. `show` always masks
`access_key` completely and only ever shows a short prefix of `access_id`
(never the full value); `set` only touches the fields you pass, merging with
whatever's already stored for that instance.

## How a budget is identified as "ours"

Every budget `enforce` creates gets a human-legible name
(`bb:<scope_name>:<key>:exp<YYYYMMDD>`, truncated with a content hash if too
long) and a machine-parseable `description` marker
(`[managed-by=budget-buddy] scope=... key="..." field=... created=... expires=...`).
Before any `PUT`/`DELETE`, the live budget is re-fetched and its description
marker re-verified against what the local registry expects — a mismatch (e.g.
someone hand-edited it) causes that entry to be skipped, never force-deleted.
The registry is the source of truth for *which* budget IDs to act on; the
marker check is an independent confirmation immediately before anything
destructive happens.

## Concurrency

`enforce`/`sweep` take a PID-file lock
(`~/.sumo/output/<instance>/budget-buddy/registry.lock`) so two runs never
race against the same registry. A second run while one is active fails fast
with a clear error naming the holding PID/host/age. If a prior run crashed
without releasing the lock, re-run with `--force` to clear it — this is the
only way a lock is cleared on behalf of a run that didn't create it.

## Logging

`--log-level TRACE|DEBUG|INFO|WARNING|ERROR` (default `INFO`). Since `enforce`
typically runs unattended on a schedule, `INFO` is written to read as a full
audit trail on its own: every scope evaluated, every value's measured bytes
vs. threshold, and every create/skip/sweep decision with its budget ID and
expiry. `DEBUG` adds full query text and API payloads; `TRACE` adds
per-row parse detail and HTTP retry internals.

## State files

```
~/.sumo/output/<instance>/budget-buddy/
  registry.json   # budgets this tool created: id, scope, key, type, created/expires
  registry.lock   # held only while enforce/sweep is running
```

Deleting `registry.json` does not touch any live Sumo Logic budgets — it just
makes budget-buddy forget what it created, so a subsequent `enforce` may
recreate budgets for still-offending values (the previous ones, if any,
become unmanaged until deleted by hand or found via `list --all-budgets`).

## Tests

Self-contained — no credentials, no network:

```bash
uv run pytest
```
