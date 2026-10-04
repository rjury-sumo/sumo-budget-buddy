# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

`sumo-budget-buddy` works around Sumo Logic's cap on simultaneously-active
native ingest budgets (~100, soft org limit) when real quota needs span
hundreds of `_sourceCategory`-style values. It polls ingest volume per
metadata value, and for whichever ones exceed a threshold, creates
*short-lived* native `/v2/ingestBudgets` entries — tracked locally with a
TTL and swept at the next calendar-day boundary. Run on a schedule
(`enforce`), this gives quota-like coverage over an arbitrarily large value
set while keeping the live budget count low.

Full design rationale and API research (including live-verification notes
against a real org): [`docs/budget-buddy-plan.md`](docs/budget-buddy-plan.md).
Read it before changing `volume_query.py`, `naming.py`, or `budgets.py` —
several non-obvious behaviors there (e.g. `budgetType` working despite being
undocumented in the OpenAPI schema) were discovered empirically.

## Commands

```bash
uv sync                         # install deps (dev group includes pytest/ruff)
uv run sumo-budget-buddy --help
uv run pytest                   # full suite — self-contained, no credentials/network
uv run pytest budget_buddy/tests/test_reconcile.py   # single file
uv run pytest budget_buddy/tests/test_reconcile.py::test_name  # single test
uv run ruff check .
```

There is no network-dependent test tier — all HTTP is mocked (`responses`
library) or avoided entirely by testing pure functions directly.

## Standalone portability (important when editing certain files)

This project was ported out of a parent `sumo-ai` repo and has **zero import
dependency** on it. Several files are deliberately *vendored* copies of
counterparts in that parent repo's `cli/` package, trimmed to just what
budget-buddy needs:

| File | Vendored from | Shared-by-convention-not-import with the sibling `sumo` CLI |
| --- | --- | --- |
| `http_client.py` | `cli/http_client.py` | retry/throttle policy |
| `instance_config.py` | `cli/config.py` | `~/.sumo/instances.toml`, `SUMO_ACCESS_ID[_NAME]` env vars |
| `paths.py` | — | `~/.sumo/output/<instance>/` layout, `SUMO_HOME` |
| `volume_query.py`'s `VOLUME_DIMS` | `cli/volume.py`'s `_DIMS` | — |

If Sumo changes the `sumologic_volume` index's internal field names, or the
parent repo's retry policy changes, these need to be updated by hand — there
is no shared module to patch once. Do not "fix" this duplication by adding
an import back to the parent repo; that coupling was deliberately removed.

## Architecture

Call graph for the two mutating commands:

```
cli.py (argparse, one subcommand per command)
  -> reconcile.py (evaluate_scope / enforce_scope / sweep_registry — the core logic)
       -> volume_query.py (build_query/parse_rows — the sumologic_volume query itself)
       -> timerange.py    (window string -> absolute epoch-ms bounds)
       -> search.py        (SearchClient: create -> poll -> fetch -> delete a search job)
       -> budgets.py        (IngestBudgetsV2Client: /v2/ingestBudgets CRUD)
       -> naming.py          (name/description marker a budget is tagged with)
       -> registry.py         (local JSON: which budget IDs this tool owns)
  -> lock.py (PID-file lock around enforce/sweep, held for the whole command)
  -> instance_config.py (credential/endpoint resolution per named instance)
```

Key design invariants, each with a reason that isn't obvious from the code
alone — preserve them when touching these areas:

- **The registry (`registry.py`) is the sole source of truth for which
  budget IDs budget-buddy owns** — ownership is never rediscovered by
  scanning the live API. Immediately before any `PUT`/`DELETE`, the live
  budget's `description` is re-fetched and checked against the expected
  marker (`naming.verify_marker`) as an independent safety check — a
  mismatch (e.g. hand-edited) causes that entry to be *skipped*, never
  force-deleted.
- **Registry writes happen immediately after each mutation**, not batched
  at the end of a loop — minimizes (but, per a `SIGTERM`/`SIGINT` landing
  between `create_budget()` and `registry.save()`, doesn't eliminate) the
  window in which a crash leaves a budget created-but-untracked. `list
  --all-budgets` surfaces such orphans by scanning for the marker with no
  matching registry entry.
- **A budget scope value must never be blank or a bare wildcard**
  (`_sourceCategory=*`). This is checked twice independently:
  config-load time (`config.py`, for scopes that could reach `enforce`) and
  immediately before every `create_budget` call (`reconcile.py`, the actual
  last line of defense regardless of how a bad value got there). Ad-hoc
  `evaluate`-only scopes built in `cli.py` skip the first check since they
  never reach `enforce`.
- **`registry.get_active`, not `get`, gates "is this key already covered"**
  during `enforce`. An expired-but-not-yet-swept entry (e.g. deliberately
  left behind by a marker mismatch during sweep) must not be treated as
  active coverage, or that key would silently never get re-enforced.
- **A `per_value` row with a missing/blank dimension key is dropped**, not
  given a fallback key like `"*"` — a fallback would silently become a
  global budget scope if later enforced.
- Each native ingest budget covers exactly **one** metadata dimension
  (hard API constraint) — `mode: per_value` on a wildcard `scope` is how one
  config entry fans out into many independently-tracked budgets; `mode:
  aggregate` collapses everything matching into a single combined budget.
- `http_client.send_with_retry` retries **only HTTP 429** (honoring
  `Retry-After`); 4xx never retries, 5xx retry is intentionally out of
  scope.
- `lock.py`'s PID-file lock wraps whole CLI commands (`cli.py`), not
  individual scopes within `reconcile.py` — `enforce --all` holds one lock
  per targeted instance for the run's entire duration. A crashed run's lock
  is only ever cleared by explicit `--force`, never an automatic stale-PID
  guess.
- Credential/endpoint resolution order (`instance_config.py`) is always env
  var → `~/.sumo/instances.toml` → built-in default (endpoint only); the
  `instances.toml` entry for a named instance is entirely optional.
