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
`SUMO_ENDPOINT_<NAME>`, or an `[instances.<name>]` section in
`~/.sumo/instances.toml`:

```toml
[instances.prod]
access_id  = "..."
access_key = "..."
endpoint   = "https://api.au.sumologic.com"
region     = "AU"
```

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
all of them by name. See
[`docs/budget-buddy-plan.md`](docs/budget-buddy-plan.md#config-file)
for the full schema reference; short version:

```yaml
instance: default            # credential profile; per-scope override allowed

defaults:                    # fallback values; any scope can override any key
  mode: per_value            # per_value | aggregate
  window: today               # today | yesterday | last_Nh | YYYY-MM-DD/YYYY-MM-DD
  tz: America/Los_Angeles
  threshold_bytes: 5368709120  # 5 GiB
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
```

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
sumo-budget-buddy evaluate (--config PATH (--scope NAME | --all) | --field FIELD --scope-expr EXPR)
                           [--mode per_value|aggregate] [--window WINDOW] [--tz TZ]
                           [--threshold-bytes N] [--instance NAME]
                           [--format table|json|csv] [--output PATH]
                           [--log-level TRACE|DEBUG|INFO|WARNING|ERROR]
```

Two ways to run it: against named scopes in a config file, or ad hoc with
`--field`/`--scope-expr` (and optionally `--mode`/`--window`/`--tz`/
`--threshold-bytes`/`--instance`) for a one-off check with no config file at
all. Never touches `/v2/ingestBudgets` or the local registry — safe to run
anytime.

```bash
sumo-budget-buddy evaluate --field _sourceCategory \
    --scope-expr "_sourceCategory=*cloudtrail*" --window today
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
sumo-budget-buddy sweep [--config PATH] [--scope NAME] [--instance NAME]
                        [--dry-run] [--force]
```

`enforce` always sweeps first on its own, so this is mainly for manual
cleanup or inspection (`--dry-run`) between scheduled runs.

### `list` — what's currently tracked

```
sumo-budget-buddy list [--instance NAME] [--all-budgets]
                       [--format table|json|csv] [--output PATH]
```

Shows registry-tracked budgets with live usage. `--all-budgets` additionally
lists every budget on the account (a live API call), with a `managed`
column (yes/no, based on the `[managed-by=budget-buddy]` description marker)
so you can see at a glance what this tool owns versus what a human or another
tool created — budget-buddy only ever mutates the former. `--format` defaults
to a terminal table; `json`/`csv` for scripting, `--output FILE` to write
instead of stdout (format inferred from the extension if `--format` is
omitted).

### `status` — one budget's detail

```
sumo-budget-buddy status <budget-id-or-scope_name:key> [--instance NAME]
```

Accepts either a raw `/v2/ingestBudgets` ID or `scope_name:key` to resolve via
the local registry. Shows capacity, usage, and whether it carries the
budget-buddy marker.

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
