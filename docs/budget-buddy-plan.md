# Dev Plan: `sumo-budget-buddy` — Per-Category Ingest Budget Enforcement

## Problem

Sumo Logic's native ingest budget feature applies a budget to everything matching
a scope (e.g. `_sourceCategory=*prod*payment*`), but the number of active budgets
an org can run is capped (a **soft** limit — commonly ~100, raisable by Sumo
support, but never huge). Customers who want quota-style enforcement over a large
number of categories/environments (hundreds of `_sourceCategory` values, say)
can't just create one native budget per value — they'd blow the cap.

`sumo-budget-buddy` works around this by being a **polling, ephemeral-budget
scheduler**: each run it measures ingest volume for one or more named, configured
scopes over a time window (default: today so far, in a configurable timezone),
finds which individual metadata values have exceeded that scope's threshold, and
creates *short-lived* native ingest budgets only for those offenders — tracked
locally with a TTL so they get swept away at the start of the next calendar day.
This keeps the number of simultaneously-active native budgets low (bounded by how
many things are *currently* over threshold, further capped by a configurable
max-per-run) while still giving quota-like coverage over an arbitrarily large set
of categories.

## Scope of this plan

This is a **new, standalone tool** living in its own top-level folder
(`budget_buddy/`), not a `sumo <subcommand>` added to the existing `cli/`
package. Decisions made with the user:

- Targets **only** `/v2/ingestBudgets` (verified live — see Research below).
- **Follows** this repo's credential/instance conventions (`SUMO_ACCESS_ID[_NAME]`
  env vars, `~/.sumo/` state root, `~/.sumo/instances.toml`) for compatibility
  with the main `sumo` CLI, but has **no import dependency** on it — revised
  after the initial build to vendor its own copies of the handful of `cli.*`
  pieces it used (`http_client`, instance resolution, path conventions, the
  `sumologic_volume` dimension map) so `budget_buddy/` is a genuinely
  standalone, copy-elsewhere-able folder. See "Standalone portability" below.
- Is **single-shot and idempotent** — each invocation does
  evaluate → reconcile budgets → sweep expired, and relies on external
  scheduling (cron, Sumo scheduled search trigger, CI, etc.) to run
  periodically. No internal daemon/scheduler loop.
- **Config-file driven**: one or more named "scope" definitions (threshold, time
  window, TTL, grouping mode, budget type, etc.) live in a YAML config file; a
  run targets one, several, or all named scopes by name.
- Default per-value threshold **5 GiB/day**, configurable per scope. The
  threshold value *is* the budget's `capacityBytes` — no separate "alert vs cap"
  split (confirmed with user: same number drives both the exceeded-check and the
  created budget's capacity).
- Fan-out in per-value mode is bounded by a **configurable max-budgets-per-run**
  (default 50 — distinct from Sumo's own ~100 org-wide soft cap, which may
  actually be raised higher for some accounts), sorted by volume descending
  (worst offenders first). Hitting the cap is a **reported error**, not a silent
  drop — remaining over-threshold values are listed in the output so the
  operator knows coverage is incomplete that cycle.
- Each native ingest budget covers exactly **one metadata dimension**, by
  design of the underlying API (`scope` is a single `field=value` string) — this
  isn't a choice this tool makes, it's a hard constraint of `/v2/ingestBudgets`.
  A scope's `scope` pattern (e.g. `_sourceCategory=test/foo/*`) can still expand
  into *many* per-value budgets when `mode: per_value` — e.g. a pattern matching
  100 distinct `_sourceCategory` values over threshold produces 100 tracked
  budgets (subject to `max_budgets`), each with its own TTL.
- Structured, leveled logging throughout (TRACE/DEBUG/INFO/WARNING/ERROR) —
  this tool runs unattended on a schedule, so every create/update/skip/sweep
  decision must be auditable after the fact without re-running anything.

---

## Research: the budget APIs (live-verified)

Investigated `docs/dev/apis/sumologic-api.yaml`, then **verified behavior live**
against the sandbox org (`default` instance in this repo's `~/.sumo/`
config — AU region, `https://api.au.sumologic.com`, default
`SUMO_ACCESS_ID`/`SUMO_ACCESS_KEY` env vars) using scope `_sourceCategory=*cloudtrail*`
per the user's instruction. Three budgets were created with a 1024-byte
capacity and `action: keepCollecting` (non-disruptive — never actually stops
collection), inspected via `GET`, then deleted via `DELETE`; final `GET
/v2/ingestBudgets` confirmed an empty list, i.e. full cleanup.

### `/v2/ingestBudgets` (`ingestBudgetManagementV2`) — the one we use

```
GET    /v2/ingestBudgets                  listIngestBudgetsV2
POST   /v2/ingestBudgets                  createIngestBudgetV2
GET    /v2/ingestBudgets/{id}             getIngestBudgetV2
PUT    /v2/ingestBudgets/{id}             updateIngestBudgetV2
DELETE /v2/ingestBudgets/{id}             deleteIngestBudgetV2
POST   /v2/ingestBudgets/{id}/usage/reset resetIngestBudgetUsage (dailyVolume only)
```

`IngestBudgetDefinitionV2` (request body for create/update):

| Field | Required | Notes |
| --- | --- | --- |
| `name` | yes | display name, ≤128 chars |
| `scope` | yes | `field=value` string, wildcards supported — e.g. `_sourceCategory=*prod*nginx*` matches `prod/nginx`, `dev/nginx`, `dev/nginx/error`, etc. Field must be enabled in the Fields table. |
| `capacityBytes` | yes | unit depends on `budgetType`: bytes/**day** for `dailyVolume`, bytes/**minute** for `minuteVolume` |
| `action` | yes | `stopCollecting` \| `keepCollecting` |
| `budgetType` | no | **live-verified**, see below — `dailyVolume` (default) or `minuteVolume` |
| `timezone` | no | IANA tz, default `Etc/UTC` |
| `resetTime` | no | `HH:MM`, default `00:00` |
| `description` | no | free text |
| `auditThreshold` | no | 1–99, default **85** (confirmed live) — % usage logged to Audit Index |

**`budgetType` live-verification result:** the field is genuinely accepted on
`POST` even though it's missing from the *published* `IngestBudgetDefinitionV2`
schema (a real documentation gap, not evidence of non-support). Confirmed:

- `budgetType: "minuteVolume"` → accepted, echoed back unchanged on `GET`.
  `timezone`/`resetTime` still default (`Etc/UTC`/`00:00`) and are harmlessly
  present in the response even though they're semantically inert for this type
  (no daily reset applies).
- `budgetType: "dailyVolume"` → accepted, `timezone`/`resetTime` behave as
  documented (we set `America/Los_Angeles` / `00:00` and got them back as-is).
- **Omitting `budgetType` entirely → defaults to `dailyVolume`**, with
  `auditThreshold` defaulting to 85 when also omitted. This confirms
  `dailyVolume` is the "legacy"-equivalent default behavior the user was
  describing, and `minuteVolume` is the newer opt-in rate type — both live on
  the same `/v2/ingestBudgets` endpoint, not two separate APIs.
- `DELETE` returns `204` with an empty body; a follow-up `GET
  /v2/ingestBudgets` confirmed removal.

**Conclusion:** no separate "legacy" ingest-budget write endpoint exists or is
needed. `budget_buddy/budgets.py` sends `budgetType` explicitly on every create
(never relies on the default) so scope config is self-documenting, and treats
it as a confirmed-working field.

### `/v1/budgets` (`budgetManagement`, `ScanBudget`) — explicitly NOT used

Found while searching for "budget" — this is a **different feature**: a
per-user/role **search scan-cost** budget (`ScanBudgetDefinition`: `capacity`,
`unit` GB/MB/TB/KB, `window` Query/Daily/Weekly/Monthly, `scope` =
included/excluded users/roles). It caps how much a *user* can scan in search,
not how much a *category* can ingest. Not a fit for this tool — noted here only
so a future reader doesn't rediscover it and get confused about which API to
use.

---

## Research: measuring volume (live-verified)

Rather than scanning each target category's own raw logs with `sum(_size)`
(which incurs real scan cost on Flex/Infrequent tiers), **reuse the same
technique `cli/volume.py` already uses**: query the free, pre-aggregated
`sumologic_volume` audit index. This index tags its own entries with a fixed
rollup-dimension scope (e.g. `_sourceCategory=sourcecategory_and_tier_volume`
for the sourceCategory dimension — **not** the customer's actual category
value), with the real per-category breakdown embedded as a JSON blob that needs
parsing. See `cli/volume.py` `_DIMS` dict (L71+) for the field mapping per
dimension (`sourcecategory`, `collector`, `source`, `sourcehost`, `sourcename`,
`view`).

Live-verified against the sandbox org with scope `_sourceCategory=*cloudtrail*`,
`--hours 24`, both grouping modes, via a real search job (`sumo search`, same
API path budget-buddy will use):

**Per-value** (`mode: per_value`):

```sumoql
_index=sumologic_volume _sourceCategory=sourcecategory_and_tier_volume
| parse regex "(?<data>\{[^\{]+\})" multi
| json field=data "field","dataTier","sizeInBytes","count" as sourceCategory,dataTier,bytes,count
| where tolowercase(sourceCategory) matches tolowercase("*cloudtrail*")
| bytes/1Gi as gbytes
| sum(gbytes) as gbytes, sum(count) as events by sourceCategory
```

→ returned exactly one row: `aws/observability/cloudtrail/logs`, 0.0013 GB,
745 events — matching `sumo volume list --dim sourcecategory --filter
"*cloudtrail*"` output exactly.

**Aggregate/global** (`mode: aggregate`) — same query, final line without
`by sourceCategory`:

```sumoql
| sum(gbytes) as gbytes, sum(count) as events
```

→ returned a single summed row, same total.

**Design implication:** `budget_buddy/volume_query.py` builds exactly this
pattern per dimension (reusing `cli/volume.py`'s `_DIMS` mapping rather than
re-deriving it), varying only the `where ... matches` filter value (the
config's `scope` pattern) and whether the final `sum(...)` has a `by <field>`
clause (`per_value` vs `aggregate`). The **`aggregate` mode mirrors what a
single native budget already does natively** — included for completeness/parity
checks, but the user confirmed `per_value` is the primary use case this tool
exists for.

Time-range handling (`budget_buddy/timerange.py`) is **independent of which
index is queried** — the search job always takes absolute UTC `from`/`to`
bounds, so "today in `America/Los_Angeles`" is resolved to UTC entirely in our
own code before the query ever reaches Sumo, using stdlib `zoneinfo`.

---

## Identifying budget-buddy-managed budgets

Two separate audiences need to tell a budget-buddy-created budget apart from
one created by a human or another tool: the **user**, skimming the Sumo UI or
`sumo-budget-buddy list` output, and **the script itself**, before it ever
mutates (`PUT`/`DELETE`) a budget it thinks it owns. Both are solved with the
same two fields, set on every create and never relied on to be parsed back as
the *only* source of truth (the registry remains primary — see below).

**Name** (`name`, ≤128 chars) — human-legible, TTL visible at a glance in the
Sumo UI's budget list without opening the budget:

```text
bb:<scope_name>:<key>:exp<YYYYMMDD>
```

e.g. `bb:cloudtrail-prod:aws/observability/cloudtrail/logs:exp20261002`. If the
full name would exceed 128 chars (long category paths, long scope names), the
`<key>` portion is truncated with a short content hash suffix
(`...~a1b2c3d4`) to stay unique and traceable — the *full, untruncated* key is
always in the registry and in `description` regardless, so truncation here is
purely cosmetic and never affects matching logic.

**Description** (`description`, ≤1024 chars) — a single-line, machine-parseable
marker plus full identifying detail, *independent* of the registry file:

```text
[managed-by=budget-buddy] scope=cloudtrail-prod key="aws/observability/cloudtrail/logs" field=_sourceCategory created=2026-10-02T00:00:00-07:00 expires=2026-10-02T23:59:59-07:00
```

**Defensive verify-before-mutate:** before any `PUT` or `DELETE` against a
budget ID pulled from the local registry, `budget_buddy/budgets.py` first
re-`GET`s that budget and confirms its `description` still carries
`[managed-by=budget-buddy]` with matching `scope=`/`key=` values. A mismatch
(someone hand-edited the description, repurposed the budget, or — extremely
unlikely — an ID collision) is logged at `ERROR` and that entry is **skipped**,
never force-deleted/updated, and surfaced in the run's exit summary. This means
the registry says *what we intend to manage*, but the live marker is checked
every time before anything destructive actually happens — the script can never
silently touch a budget it didn't create, even if its own local state file
were corrupted, stale, or copied from a different machine.

`sumo-budget-buddy list` also distinguishes the two audiences at the UI level
(see Commands below): by default it shows only registry-tracked budgets, with
an `--all-budgets` flag to additionally list every `/v2/ingestBudgets` entry on
the account with a `MANAGED` column (yes/no, based on the same description
marker) — useful both as a sanity check and for spotting pre-existing
human-managed budgets that must never be touched.

---

## Concurrency control

`enforce` and `sweep` mutate the registry file and create/delete real budgets
— they must never run concurrently against the same registry (`evaluate`,
`list`, and `status` are read-only and need no lock). A PID-file-style lock
guards this, stored next to the registry:
`~/.sumo/output/<instance>/budget-buddy/registry.lock`.

- **Acquire**: atomic create (`O_CREAT | O_EXCL`), writing `{pid, hostname,
  command, started_at}` as JSON into the file.
- **Already held**: if the create fails because the file exists, read its
  contents and report a clear `ERROR`-level exit: which PID/host/command
  started it and how long ago, then stop — no implicit waiting or retrying.
  This check is a best-effort liveness hint (same-host PID still running via
  `os.kill(pid, 0)`), **not** a reason to auto-clear the lock even when the
  PID looks dead — a false "it's dead, proceed" is worse than requiring a
  human to confirm.
- **`--force`**: explicit override for the crash-recovery case the user
  described (a prior run died without releasing its lock) — deletes the
  existing lock file unconditionally and logs a `WARNING` including the stale
  lock's recorded PID/host/age, then proceeds normally. This is the *only*
  way a lock is ever cleared on behalf of a run that isn't the one that
  created it.
- **Release**: the lock file is removed in a `finally` block on normal exit
  (success or handled error) and on `SIGTERM`/`SIGINT`, so the common case
  (even a `Ctrl-C`) cleans up properly — `--force` exists specifically for the
  unclean cases that can't (a hard crash, `SIGKILL`, OOM-kill).

---

## Usage states (live-verified)

`IngestBudgetV2` carries `usageBytes` and `usageStatus` directly (confirmed in
every create/get response during live testing) — `usageStatus` is one of
`Normal | Approaching | Exceeded | Unknown`. The user's key clarification: for
a **blocking** budget (`action: stopCollecting`, the default this tool creates
and the only action it's designed around), once usage crosses capacity,
collection actually stops — so `usageBytes` **freezes** at whatever value
triggered the trip and won't increase further until the next native reset
(`dailyVolume`) or indefinitely (`minuteVolume`, which resets every minute
regardless). A naive `usageBytes / capacityBytes` percentage would therefore be
misleading once tripped (it reads as "~100%" forever, not "still growing").
Rendering (in `list`/`status`, both table and `--json`) follows `usageStatus`
directly rather than re-deriving a percentage past the trip point:

| `usageStatus` | Rendering |
| --- | --- |
| `Normal` / `Approaching` | `"{pct}% ({human_bytes(usageBytes)} / {human_bytes(capacityBytes)})"` |
| `Exceeded` | `"EXCEEDED ({human_bytes(usageBytes)} @ trip — collection stopped)"` — the frozen value, explicitly labeled so it's never misread as a live/growing number |
| `Unknown` | `"? (unable to retrieve usage)"` |

This also resolves what the first draft of this plan flagged as an open
"update-on-growth" question: for a `stopCollecting` budget, once tripped,
*staying* tripped for the rest of the TTL window is the entire point (the tool
exists to enforce a cap, not just alert), so there's no mid-day capacity bump
to design — `usageBytes` genuinely can't tell us the category "wants" more
than the cap once collection has stopped. (A `keepCollecting`/warn-style budget
would keep counting past 100% and could meaningfully support a growth-based
capacity bump later, but that's explicitly out of scope — the user confirmed
this tool only creates blocking budgets.)

---

## Config file

A single YAML file (`budget-buddy.yaml` by default, `--config PATH` to
override) defines named scopes. A run targets one or more scopes by name, or
`--all`.

```yaml
instance: default            # instance_config.py credential profile; per-scope override allowed

defaults:                    # fallback values; any scope can override any key
  mode: per_value            # per_value | aggregate
  window: today               # today | yesterday | last_Nh (N substituted) | explicit from/to
  tz: America/Los_Angeles
  threshold_bytes: 5368709120  # 5 GiB
  budget_type: dailyVolume     # dailyVolume | minuteVolume
  action: stopCollecting       # stopCollecting | keepCollecting
  ttl: end_of_day              # end_of_day (in `tz`) | explicit duration e.g. "6h"
  max_budgets: 50
  audit_threshold: 85

scopes:
  - name: cloudtrail-prod
    description: "AWS CloudTrail logs, prod org"
    field: _sourceCategory
    scope: "_sourceCategory=*cloudtrail*"
    # all other keys inherited from `defaults` unless overridden here

  - name: test-foo-rollup
    description: "Sandbox/test app category rollup — many individual values expected"
    field: _sourceCategory
    scope: "_sourceCategory=test/foo/*"
    mode: per_value             # each of the (potentially ~100) matching values
                                 # gets its own threshold + budget + TTL tracked
                                 # independently
    threshold_bytes: 1073741824  # 1 GiB override for this scope

  - name: collector-watch
    field: _collector
    scope: "_collector=*ingest-gw*"
    mode: aggregate              # single combined budget, mirrors native behavior
    budget_type: minuteVolume
    threshold_bytes: 10485760    # 10 MiB/min
```

Validation at load time: `field` must be one of the dimensions
`cli/volume.py`'s `_DIMS` supports (or a custom field — flagged as
unvalidated/best-effort since custom fields aren't in that fixed mapping);
`scope` must start with `field=`; `mode: aggregate` + per-scope `max_budgets`
is a no-op warning (aggregate only ever produces 0 or 1 budget).

---

## Architecture

```
budget_buddy/
  __init__.py
  cli.py                 # argparse entry point, subcommands below
  http_client.py          # vendored retry/throttle policy (Throttle,
                          #   send_with_retry) — no cli.* import.
  instance_config.py      # vendored, read-only instance/credential
                          #   resolution (SUMO_ACCESS_ID[_NAME] env vars,
                          #   ~/.sumo/instances.toml) — no cli.* import.
  search.py              # thin search-job wrapper (create/poll/fetch/delete),
                          #   narrow to this tool's one need: aggregate volume
                          #   query against sumologic_volume. Built on
                          #   http_client.send_with_retry + Throttle (NOT a
                          #   copy of cli/sumo_search.py — no caching/PII/
                          #   webview needed for one aggregate query/run).
  timerange.py            # "today"/"yesterday"/"last_Nh"/explicit -> (from_ms,
                          #   to_ms), timezone-aware via stdlib zoneinfo.
  volume_query.py         # builds the sumologic_volume query per dimension +
                          #   mode (vendored copy of cli/volume.py's _DIMS
                          #   mapping, trimmed to the keys this tool uses),
                          #   parses results into {value: bytes}.
  budgets.py              # IngestBudgetsV2Client: list/create/update/delete/
                          #   reset-usage against /v2/ingestBudgets, using
                          #   instance_config.resolve_instance() for
                          #   credentials and http_client for retry/throttle.
  config.py               # loads/validates budget-buddy.yaml -> ScopeConfig
                          #   objects (dataclasses), applying `defaults` merge.
  registry.py             # local JSON state: budgets we created, keyed by
                          #   (scope_name, value) — budget id, budget_type,
                          #   created_at, expires_at, last_seen_bytes. Lives at
                          #   ~/.sumo/output/<instance>/budget-buddy/registry.json
  paths.py                # vendored state-directory conventions (SUMO_HOME,
                          #   instance_root) — no cli.* import.
  lock.py                 # PID-file concurrency lock alongside the registry
                          #   (registry.lock) for `enforce`/`sweep`; --force
                          #   override for crash recovery. See Concurrency
                          #   control below.
  naming.py               # builds the `bb:<scope>:<key>:exp<date>` name and
                          #   `[managed-by=budget-buddy] ...` description
                          #   marker; also parses/verifies a marker on a live
                          #   budget before any mutate call. See Identifying
                          #   budget-buddy-managed budgets below.
  reconcile.py            # the core loop: acquire lock -> sweep expired ->
                          #   evaluate -> diff vs registry -> create/update/
                          #   skip -> release lock, with INFO-level decision
                          #   logging at every step.
  logging_setup.py        # configures leveled logging (see Logging below),
                          #   including the custom TRACE level.
  README.md               # user-facing docs — install, config, commands.
  tests/                  # self-contained unit test suite (also wired into
                          #   the parent repo's pytest testpaths).
docs/dev/budget-buddy-plan.md   # this file (dev history/rationale, stays in
                                 #   the parent repo — not needed at runtime)
```

### Why not copy `docs/search-job-api-reference/sumo_search_client.py` verbatim?

That reference client is explicitly designed to be copied into *external*
projects with zero repo coupling, but carries features budget-buddy never
needs (raw-message pagination, PII redaction, discovery endpoints). Instead
`budget_buddy/search.py` is modeled on its lifecycle logic but trimmed to the
one aggregate-query path this tool uses, built on budget_buddy's own vendored
`http_client` (see "Standalone portability" below) rather than either copy.

### Standalone portability

The initial build deliberately shared this repo's `cli.config`/`cli.http_client`/
`cli.paths`/`cli.volume` as import dependencies (a conscious tradeoff at the
time — less code to duplicate, automatic bugfix propagation). Revisited after
the fact: the user wanted `budget_buddy/` to be copy-elsewhere-able, so each of
those four touchpoints was vendored instead:

| Was | Now | Notes |
| --- | --- | --- |
| `from cli import http_client` | `budget_buddy/http_client.py` | Verbatim copy — already had zero `cli.*` deps of its own. |
| `from cli.config import resolve_instance` | `budget_buddy/instance_config.py` | Trimmed to read-only resolution (no `sumo instances add/remove` equivalent — budget-buddy never writes this file). Same `~/.sumo/instances.toml` format and `SUMO_ACCESS_ID[_NAME]` env convention, so it reads the same config a co-installed `sumo` CLI uses, with zero import coupling. |
| `from cli.paths import instance_root` | `budget_buddy/paths.py` | Inlined the `SUMO_HOME`/`OUTPUT_ROOT`/`instance_root` logic directly — same `~/.sumo/output/<instance>/` layout convention, no import. |
| `from cli.volume import _DIMS` | `budget_buddy/volume_query.py` (`VOLUME_DIMS`) | Inlined, trimmed to the four keys this tool actually reads (`scope`, `json_alias`, `dim_key`, `dim_field` — dropped `group_by`/`display`, which budget-buddy never used). |

Tradeoff accepted: these are now two copies of the same logic (one in `cli/`,
one in `budget_buddy/`) that won't automatically stay in sync — a bugfix or
Sumo-side schema change to the `sumologic_volume` index layout, for instance,
would need to be applied in both places by hand. Acceptable because none of
the four vendored pieces are expected to change often, and the portability
win (this folder can be `cp -r`'d into a brand-new project and still work)
was judged worth that risk. `budget_buddy/` still has no pyproject.toml of its
own — it's built and installed as a subpackage of this repo's pyproject.toml
(`budget_buddy*` in `[tool.setuptools.packages.find]`, `sumo-budget-buddy`
console script) — so extracting it to a genuinely separate project still
requires writing a small standalone `pyproject.toml` declaring its three real
third-party dependencies (`requests`, `pyyaml`, `rich`; `python-dotenv` is
optional). See `budget_buddy/README.md` for that dependency list and a
suggested minimal `pyproject.toml` for exactly that extraction.

---

## Commands

```
sumo-budget-buddy evaluate   --config PATH (--scope NAME [NAME...] | --all)
                              [--instance NAME] [--json] [--log-level LEVEL]
                              # also supports pure ad-hoc mode without a config
                              # file, for a quick one-off measurement:
                              #   --field _sourceCategory --scope-expr "*" \
                              #   --mode per_value|aggregate --window today \
                              #   --tz America/Los_Angeles --threshold-bytes N

sumo-budget-buddy enforce    --config PATH (--scope NAME [NAME...] | --all)
                              [--instance NAME] [--dry-run] [--force]
                              [--log-level LEVEL]
                              # --force: clear a stale concurrency lock left
                              # by a prior run that crashed (see Concurrency
                              # control below) — NOT a bypass of any budget
                              # logic, purely the lock.

sumo-budget-buddy sweep      --config PATH [--scope NAME ...] [--force]
                              [--dry-run] [--log-level LEVEL]
                              # deletes any registry entries past expires_at;
                              # --force here means the same lock override as
                              # `enforce`

sumo-budget-buddy list       [--instance NAME] [--all-budgets]
                              [--format table|json|csv] [--output PATH]
                              # table of registry-tracked (budget-buddy-
                              # managed) budgets: scope, key, type, usage %
                              # or EXCEEDED, capacity, action, expiry.
                              # --all-budgets additionally lists every budget
                              # on the account (a live /v2/ingestBudgets GET),
                              # flagging a MANAGED yes/no column via the
                              # description marker — see table format below.
                              # --format defaults to table on a terminal;
                              # json/csv for scripting. --output writes to a
                              # file instead of stdout (format inferred from
                              # the extension if --format is omitted: .json,
                              # .csv, else table/text). evaluate shares the
                              # same --format/--output flags.

sumo-budget-buddy status     <id-or-scope-name:value>
                              # one budget's detail + current usage via GET
```

### `list` — terminal-friendly output

Default (registry-tracked only):

```text
SCOPE            KEY                                    TYPE         USAGE                          CAPACITY   ACTION          EXPIRES (tz)
cloudtrail-prod  aws/observability/cloudtrail/logs       dailyVolume  42% (2.1 GB / 5.0 GB)           5.0 GB     stopCollecting  2026-10-02 23:59:59 PT
test-foo-rollup  test/foo/bar                            dailyVolume  EXCEEDED (1.0 GB @ trip)        1.0 GB     stopCollecting  2026-10-02 23:59:59 PT
test-foo-rollup  test/foo/baz                             dailyVolume  12% (122 MB / 1.0 GB)           1.0 GB     stopCollecting  2026-10-02 23:59:59 PT
```

With `--all-budgets`, an extra `MANAGED` column (`yes`/`no`) is prepended, and
rows for budgets lacking the `[managed-by=budget-buddy]` marker are included
with `KEY`/`SCOPE` blank (we don't know their origin) — purely informational,
never touched. `--json` emits the same rows as structured data (including the
raw `usageBytes`/`usageStatus`/`capacityBytes`, not just the rendered string)
for scripting.

`enforce` = sweep expired → evaluate configured scope(s) → create/update
budgets for exceptions. This is the one command meant to be put on a schedule
(e.g. hourly cron); run multiple times a day safely due to idempotency.

### `evaluate` — the "opinionated endpoint" from requirement #1

For each targeted scope config: resolve the time window (default **today so
far** in the scope's `tz`), run the `sumologic_volume` query (per-value or
aggregate per `mode`), and report `{key, bytes, gb, threshold_bytes,
over_threshold: bool}` per row. Purely read-only — never touches
`/v2/ingestBudgets` or the registry. Safe to run ad hoc, on-demand, or as a
`--dry-run` preview of what `enforce` would do.

### `enforce` — requirements #2–#4

1. **Sweep first** — delete any registry entries whose `expires_at` has
   passed (via the real `DELETE` call, not just local bookkeeping), so a new
   calendar day always starts clean before anything new is created.
2. Run the equivalent of `evaluate` for each targeted scope.
3. Within each scope, sort offenders by volume descending.
4. For each offender, up to that scope's `max_budgets`:
   - If the registry already has a non-expired entry for `(scope_name, key)`,
     leave it alone — idempotent re-run within the same day/cycle, never
     duplicates or recreates. (No growth-based capacity bump is needed here —
     see Usage states above: once a blocking budget trips, staying tripped for
     the rest of the TTL window is the intended behavior, not a gap.)
   - Otherwise `POST /v2/ingestBudgets`: `scope = "<field>=<key>"` (exact
     value) or the scope's own wildcard `scope` string (`aggregate` mode),
     `capacityBytes = threshold_bytes`, `budgetType`, `action`,
     `auditThreshold`, `timezone`/`resetTime` aligned to the scope's `tz` so
     the native `dailyVolume` reset (if used) lines up with the same
     calendar-day boundary as our own TTL, and `name`/`description` built per
     the naming/marker scheme above — matching for idempotency is always by
     registry lookup, never by re-parsing the name back.
   - Record `{budget_id, scope_name, key, budget_type, created_at,
     expires_at}` in the local registry.
5. If offender count exceeds `max_budgets` for a scope: still create up to the
   cap (worst offenders first), then **exit non-zero** and log+print the list
   of uncovered offenders — never fail silently, since under-coverage is
   exactly the failure mode this tool exists to prevent.

### TTL / sweep semantics

- `expires_at` = end of the calendar day (23:59:59) in the scope's `tz`, on
  the day the budget was created — computed once at creation time and stored
  verbatim (not recomputed relative to "now" later), matching "enforce only
  for today."
- `sweep` deletes via `DELETE /v2/ingestBudgets/{id}` and removes the registry
  entry. A `404` on delete (already gone — e.g. removed manually in the UI) is
  treated as success, with a WARNING-level log line, and the registry entry is
  still cleaned up.
- The registry is the sole source of truth for *which IDs* budget-buddy
  attempts to act on — it never discovers what to sweep/update by scanning
  `/v2/ingestBudgets` and matching on name. (The live description-marker check
  from "Identifying budget-buddy-managed budgets" above runs *after* that, as
  a second confirmation before the mutate actually happens — discovery and
  verification are deliberately two separate steps, both required.)

---

## Logging

Runs unattended on a schedule, so every decision has to be reconstructable from
logs alone after the fact — no re-running needed to find out "why was this
budget created." Standard library `logging`, with a custom `TRACE` level (5,
below `DEBUG`'s 10) registered via `logging.addLevelName`, consistent with the
`--log-level LEVEL` flag convention already used elsewhere in this repo
(`sumo api --log-level`, `sumo volume list --log-level`).

| Level | Used for |
| --- | --- |
| `ERROR` | API call failures (non-2xx from create/delete/list), `max_budgets` cap exceeded for a scope, config validation failures |
| `WARNING` | Sweep found a registry entry whose budget was already gone (404 on delete), a scope matched zero values, `budgetType` mismatch between config and a pre-existing budget being left alone |
| `INFO` | One line per decision: scope evaluated (name, window, row count), each value's measured bytes vs threshold, each create/skip/sweep action with the budget id and expiry — this is the primary audit trail and should read as a story of "what happened this run" on its own |
| `DEBUG` | Full query text sent to Sumo, raw API request/response payloads (minus credentials), registry diff before/after |
| `TRACE` | Per-row intermediate parsing detail (e.g. every line of the `sumologic_volume` JSON-blob parse), HTTP retry/backoff internals |

Default level `INFO`. `--json` mode (for `evaluate`/`list`) keeps structured
output on stdout separate from log lines on stderr, matching the rest of this
repo's `--json` convention.

---

## Remaining implementation details for Phase 1

Resolved via sandbox testing and user decisions above; nothing blocking is left
open, but these are worth nailing down as the first lines of code are written
rather than assumed. (Registry-write concurrency and mid-day capacity growth
are now covered above — Concurrency control and Usage states — and are not
listed here again.)

1. **Custom (non-`_DIMS`) metadata fields.** `cli/volume.py`'s `sumologic_volume`
   rollup dimensions only cover the built-in fields (`sourceCategory`,
   `collector`, `source`, `sourceHost`, `sourceName`, `view`). A scope
   targeting a genuinely custom field (not one of these) can't use the free
   index at all and would need to fall back to a real `sum(_size)` scan over
   that field's own data — which does incur scan cost. v1 should validate
   `field` against the known dimension set at config-load time and give a
   clear error (not a silent wrong-cost-path) if an unsupported field is used.

---

## Post-implementation code review (manual — `/code-review` is user-only)

A manual review after the initial build (and the standalone-portability
vendoring pass) found 8 issues, all fixed:

1. **Global-scope leak**: `parse_rows` defaulted a missing dimension value to
   `"*"`, which `enforce_scope` would use verbatim as a budget's scope — a
   malformed row could silently create a budget matching *all* data for that
   field. Fixed by skipping such rows (logged) instead of defaulting, plus a
   `validate_scope_expr`/`GlobalScopeError` guard enforced both at
   config-load time and again immediately before every `create_budget` call —
   a scope's value may never be blank or a bare wildcard (`*`, `**`).
2. **Sweep over-trusted failures**: the pre-delete verify-GET caught bare
   `Exception` and treated *any* failure (401/403/500/timeout, not just 404)
   as "already gone," permanently deregistering a possibly-live budget. Fixed
   to only deregister on a confirmed `BudgetAPIError(status_code=404)`;
   every other failure leaves the registry entry in place and reports
   `action="error"` (which now also fails the command's exit code).
3. **Idempotency ignored expiry**: `enforce_scope`'s "does this key already
   have a budget" check (`registry.get`) didn't verify the entry hadn't
   expired — it relied entirely on `sweep_registry` already having removed
   it, but sweep deliberately leaves a marker-mismatched entry in place,
   which then permanently blocked re-enforcement for that key. Fixed with a
   new `Registry.get_active()` that treats an expired entry as absent.
4. **Marker format wasn't escape-safe**: a `key`/`scope_name` containing a
   literal `"` broke the hand-rolled `key="..."` marker's regex round-trip,
   making `verify_marker` permanently reject that budget. Fixed by embedding
   both via `json.dumps`/`json.loads` instead of raw interpolation (a
   no-op change in output for values with nothing to escape).
5. **Dead code**: `IngestBudgetsV2Client.update_budget` had no caller and no
   test — removed.
6. **`sweep --scope` with 2+ values silently swept everything** instead of
   honoring the list. `sweep_registry` now takes `scope_names: list[str]`
   and filters properly.
7. **Dead no-op branch** in `ScopeConfig.__post_init__` (the real
   aggregate+`max_budgets` warning already lives in `config.load_config`) —
   removed.
8. **Crash window between create and registry commit**: `registry.save()`
   was batched once at the end of `enforce_scope`'s/`sweep_registry`'s loop,
   so a crash partway through a multi-offender run lost every mutation made
   so far this call, not just the one in flight. Fixed by saving immediately
   after each `put()`/`remove()`. A residual window still exists between a
   successful API call and the very next local write (documented in
   `lock.py` next to the signal handlers) — not eliminable without a
   two-phase commit, but now as small as it can reasonably be.

27 new regression tests added (`budget_buddy/tests/test_reconcile.py` is new;
`test_config.py`, `test_registry.py`, `test_naming.py`, `test_volume_query.py`
extended) — 96 total, all passing, plus a repeat live round-trip
(create → idempotent skip → force-expire → sweep) against the sandbox org
confirming the fixed code path end to end.
