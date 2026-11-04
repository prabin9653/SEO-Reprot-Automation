# R8.2 — Production architecture (design only)

> **As-built status (updated R8.4.6).** This document was written at R8.2 as a **design**. The implemented system is described in [as_built_status.md](as_built_status.md); where this document and that one differ, **as_built_status.md is correct**. Labels used below: *Implemented and tested*, *Implemented, not live-tested*, *Proposed*, *Not yet implemented*, *Known limitation*. Sections not mentioned in the list are design only.
>
> * **Implemented and tested:** §3 (reused modules unchanged), §5.1 `client_loader`, §5.2 `source_collector`, §5.3 `report_service`, §5.4 `render_service`, §5.5 `finalize_service`, §5.6 `audit_service`, §5.7 `ledger` (schema v2), §5.9 `pipeline.run_client` (adds operator actions, lease keeper, canonical path checks, structured logging), §5.10 `runner` (sequential multi-client batch, §8), dry run (mode `dry_run`, scratch only).
> * **Not yet implemented:** §5.8 `delivery_manifest`/`drive_publisher`, §5.11 `request_queue`, §5.13 `cli` and the §9 CLI contract, delivery and Make.com. §5.12 `status_outbox`: rows are written in the ledger transaction; **no sender exists**.
> * **Differences from the design:** the module numbering of the stage plan is not the one used in the roadmap (see `implementation_roadmap.md`); a request carries `operator`/`reason` options for three operator actions; run keys carry a generation suffix; `Services.collect` is injected and no production factory exists; the state machine stops at `audited`.
> * **Added after the design (no section here):** non-interactive authentication, canonical path validation, network-database refusal, lease keeper, render watchdog, structured logging — all in `as_built_status.md` §7–§10.

Status: **design, not implemented.** Nothing in this document exists as code. Companion documents:
[execution_contract](execution_contract.md), [ledger_schema](ledger_schema.md),
[google_sheets_contract](google_sheets_contract.md), [make_scenarios](make_scenarios.md),
[security_and_authentication](security_and_authentication.md), [delivery_architecture](delivery_architecture.md),
[implementation_roadmap](implementation_roadmap.md).

Conventions: **[V]** = verified in the existing code (R0–R7.5); **[P]** = proposed design; **[A]** = assumption to
verify; **[U]** = unresolved decision.

## 1. Approved baseline

| # | Decision |
|---|---|
| 1 | Backend: hybrid local Windows runner |
| 2 | Orchestration: Make.com |
| 3 | Reporting period: explicit start/end dates from Google Sheets (pilot) |
| 4 | Human approval before any client delivery |
| 5 | The local runner uploads the finalized PDF to a controlled Google Drive folder; Make.com handles approval and email |
| 6 | Execution ledger: local SQLite, separate from the snapshot database |
| 7 | Python owns data processing, insights, rendering, finalization and audit |
| 8 | Make.com owns scheduling, request coordination, status monitoring, approval and delivery |
| 9 | The reporting engine stays independent of Make.com |
| 10 | No pipeline or scenario is created in R8.2 |

## 2. System overview

```
                         (outbound only from the Windows machine; no inbound port)

  Make.com                                  Google                        Local Windows runner
  --------                                  ------                        --------------------
  S1 schedule  ──writes request rows──►  Sheet: Report Requests  ◄─polls (read-only)─  seo-report run --queue
                                                                                         │ claims request in ledger
                                                                                         │ per client: run_client(...)
                                                                                         │   collect → assemble → render →
                                                                                         │   finalize → audit → publish
  S1 status receiver ◄──POST status JSON (outbox, retried)────────────────────────────────┤
     └─ writes Execution History                                                           │
                                                      Drive: controlled folder ◄──upload──┘ (PDF + manifest)
  S2 delivery: ready event ─► fetch PDF from Drive ─► operator approval (Delivery tab)
     └─ send email after approval ─► writes Delivery tab ─► runner mirrors it (read-only) for delivery-status
```

Properties:

* The runner makes **only outbound** connections (Google APIs, the Make.com status webhook). Nothing listens.
* The runner never writes to Google Sheets (read-only scopes, decision in
  [security_and_authentication](security_and_authentication.md)). Make writes the sheet tabs.
* The ledger (local SQLite) is the runner's source of truth for run state; the sheet tabs are a human-facing
  mirror written by Make.
* The finalized PDF, its reviewer record and the history snapshot remain governed by the existing engine; the
  runner only calls it.

## 3. Existing modules reused unchanged [V]

| Concern | Module / function | Notes |
|---|---|---|
| Workbook read | `sources.sheets.load_clients(spreadsheet_id, service, default_sender)`; `clients.build_clients`, `select_clients` | `build_clients` and `select_clients` are **all-or-nothing** (one bad row raises for everyone); see §5.1 |
| Stable client key | `client_config.load_client_registry`, `ClientRegistry.key_for(name)`; `config/clients.yaml` | matched by exact sheet name |
| Periods | `periods.report_periods(start, end)` | exactly 28 days; previous 28 days derived |
| GSC / GA4 | `sources.gsc.build_service`, `fetch_search_console`, `check_access`; `sources.ga4.build_client`, `fetch_ga4` | in-process retry of 429/5xx (4 attempts, backoff) |
| Assembly | `report_run.assemble_report(client=, periods=, gsc=, ga4=, registry=, history_db_path=, output_root=, …)` → `ReportBundle` | in memory; reads history read-only |
| History / insights | `insights.history_rules.evaluate_history_rules`, `history_engine.compose_history_insights`, `history_recommendations.annotate_recommendations` | only composed in tests today (`test_reviewer_record.pipeline`) |
| Render input | `render_identity.measure_render_fingerprints`, `build_render_input(bundle, sender=, generation_date=, history_insights=, fingerprints=)` | |
| Draft + sidecar | `render_artifacts.produce_report_draft(reports_dir, candidate, workdir=, logo_path=, fonts=, finalized_render_hashes=)` → `DraftOutcome` | renderer `pdf_renderer.render_report_pdf` |
| Finalize | `report_finalize.finalize_report(bundle=, draft_path=, output_root=, history_db_path=, finalized_at=, replace_existing=, history_insights=, recommendation_annotations=, history_config=, render_input=)` → `FinalizationResult` | idempotent; no cross-process lock |
| Discover / audit | `render_state.finalized_render_hashes`, `open_sidecars`; `history_access.open_history` reader; `finalization_audit.audit_period(...)` → `AuditReport` | read-only |
| Paths | `output_layout.resolve_output_root`, `report_dir`, `reviewer_dir`, `attempt_dir`, `period_folder_name`; `snapshots.store.resolve_db_path` | defaults under `%LOCALAPPDATA%\JasperSEOReporting`; OneDrive paths refused |

Not to be modified in R8.3–R8.4: every R0–R7.5 module. The one anticipated exception is `auth.py`
(non-interactive mode, configurable credential location), which needs its own approval (see
[security_and_authentication](security_and_authentication.md) §3).

## 4. Identifiers

| Name | Format | Source |
|---|---|---|
| `client_key` | `[a-z0-9][a-z0-9_-]{0,62}` | `config/clients.yaml` [V]; mirrored into the sheet |
| `period_id` | `YYYY-MM-DD_YYYY-MM-DD` (inclusive start and end) | `output_layout.period_folder_name` [V] |
| `run_key` | `<client_key>/<period_id>` | proposed; one run per client and period |
| `request_id` | lowercase UUID | generated by Make (or the CLI for manual runs) |
| `idempotency_key` | ≤128 chars, `[A-Za-z0-9:._/-]` | generated by Make; unique per intended action |
| `run_id` | snapshot-store UUID | engine; assigned at persist |
| `pdf_sha256` / `pdf16` | 64 hex / first 16 | engine |
| `render_input_sha256` | 64 hex | engine |
| `delivery_key` | `<run_key>/<pdf16>/<recipient_set_hash16>` | proposed |

## 5. Component specifications

Every new module below is **[P]**, a future file, and none is created in R8.2. Modules live in
`src/seo_report/` next to the existing flat layout. All of them are plain Python, take their collaborators
as arguments (so tests use fakes and never reach Google), read no clock themselves (a `clock` callable is
injected) and put no client text in results, logs or ledger events.

### 5.1 `client_loader` (client configuration loader)

* **Purpose:** produce per-client run inputs with **per-client failure isolation**.
* **Why new:** `clients.build_clients` raises `SheetDataError` for any malformed row anywhere, and
  `select_clients` raises for the first misconfigured active client, so one bad client would stop all clients [V].
* **Approach [P]:** read the two tabs once (`sources.sheets.read_tab`), split rows by client name, and call the
  **existing** `build_clients` once per client with `[header] + that client's rows` (both tabs), catching
  `SheetDataError` per client. No change to `clients.py`.
* **Inputs:** `spreadsheet_id`, a Sheets service, `ClientRegistry`, default sender, optional `client_key` filter.
* **Output:** `LoadResult(clients: dict[client_key, Client], rejected: tuple[Rejection(client_name|None, client_key|None, code, location)])`.
  Codes are fixed: `client_not_in_registry`, `client_row_invalid`, `required_field_missing`,
  `duplicate_client`, `keywords_missing`, `period_dates_missing`, `period_not_28_days`, `status_not_active`.
* **Errors:** `SheetsReadError(code)` when the workbook cannot be read (network, 401/403, tab missing).
* **Idempotency:** read-only.
* **Reuses:** `sources.sheets.read_tab`, `clients.build_clients`, `client_config.ClientRegistry`.

### 5.2 `source_collector`

* **Purpose:** fetch GSC and GA4 data for one client and period, classify failures.
* **Inputs:** `Client`, `ReportPeriods`, google clients (injected), `today` (injected).
* **Output:** `CollectResult(gsc: SearchConsoleData|None, ga4: Ga4Data|None, failures: tuple[SourceFailure], warnings: tuple[SourceWarning])`.
* **Rules:** a source that fails permanently leaves its data `None` **only if** the failure category allows a
  partial report; the engine already renders missing data as `unavailable` visuals with fixed messages [V].
  Authentication failure (`auth_required`) is never downgraded to "missing data".
* **Failure categories:** `auth_required`, `source_temporary` (429/5xx after the module's own 4 attempts),
  `source_denied` (401/403/404 that is not an auth refresh problem), `source_incompatible` (`Ga4Error.incompatible`),
  `source_invalid` (bad property id/URL).
* **Policy for partial data [U]:** whether a report may be produced when one of GSC/GA4 is wholly unavailable.
  Proposed default: **no** in production (state `failed`, category `source_denied`/`source_temporary`) unless the
  request sets `options.allow_partial_sources = true`; incomplete data *inside* an available source is always allowed
  and is reported as warnings.
* **Reuses:** `sources.gsc`, `sources.ga4`. **Idempotency:** read-only; safe to repeat.

### 5.3 `report_service` (assembly + history/insights)

* **Purpose:** from a `Client` and collected data build everything finalization needs.
* **Inputs:** `Client`, `ReportPeriods`, `CollectResult`, `ClientRegistry`, paths (`history_db_path`, `output_root`).
* **Output:** `AssembledReport(bundle: ReportBundle, history_insights: HistoryInsights, annotations)`.
* **Steps [V in tests, P as service]:** `assemble_report` → `evaluate_history_rules` →
  `compose_history_insights` → `annotate_recommendations`, exactly as `tests/test_reviewer_record.pipeline` does
  (without the test-only note filter; the R2 probe showed none is needed).
* **Errors:** `UnknownClientError` → `config_invalid`; any engine exception → `internal_error` with exception class only.
* **Idempotency:** deterministic for identical inputs; stores nothing.

### 5.4 `render_service`

* **Purpose:** build the immutable render input and produce the sidecar and draft.
* **Inputs:** `AssembledReport`, sender, `generation_date`, fingerprint sources (logo `assets/jasper-logo.png`,
  DejaVu fonts), `reports_dir`, `reviewer_dir`, work-folder root.
* **Output:** `RenderResult(render_input, draft: DraftOutcome)`.
* **Order [P]:** (1) read finalized hashes with `render_state.finalized_render_hashes(reviewer_dir, require_complete=True)`;
  (2) if an **open sidecar** exists for the period use its input (`render_state.open_sidecars`) so the original
  render hash and generation date survive retries; otherwise build a candidate with `generation_date` = the
  **run's recorded generation date** (set once, stored in the ledger at first render, never recomputed);
  (3) `produce_report_draft` with a fresh empty work folder outside the project and outside OneDrive.
* **Errors:** `render_failed` (browser, fonts, fingerprint mismatch), `layout_issues`
  (`DraftOutcome.code == "render_has_layout_issues"`), `finalization_conflict` for sidecar/draft conflicts.
* **Idempotency:** the sidecar makes retries resume with the same identity [V].
* **Known benign behaviour:** G1 (a re-render after promotion hands off a duplicate draft) is kept as is.

### 5.5 `finalize_service`

* **Purpose:** call `finalize_report(..., render_input=<DraftOutcome.render_input>)` and map the result.
* **Inputs:** bundle, history insights, annotations, render input, `finalized_at` (UTC `YYYY-MM-DDTHH:MM:SS+00:00`,
  from the injected clock, **recorded in the ledger before the call** so retries reuse it), `replace_existing`.
* **Output:** `FinalizeVerdict(state: finalized|already_finalized|blocked|retryable, category, result)`.
* **Mapping [P]:** `OUTCOME_FINALIZED/ALREADY` → ok; `OUTCOME_STALE` → retry from collecting;
  `OUTCOME_CONFLICT` → `finalization_conflict` (blocked, operator); `OUTCOME_REJECTED` with `render_*` or
  `draft_invalid` → `finalization_conflict`/`render_failed`; `OUTCOME_FAILED` with `incomplete=True` →
  `finalization_failed` (retry the same call).
* **Never** passes `render_input=None` in production [U-resolved: v1 path is for historical records only].

### 5.6 `audit_service`

* **Purpose:** the verification that gates delivery.
* **Output:** `AuditVerdict(ok: bool, outcome, finalized_valid, verification_complete, reader_lists_run, findings_summary)`.
* **Pass condition (all required):** `audit_period(...).outcome == "consistent"`, `verification_complete`,
  `finalized_valid >= 1`, the finalized-only reader lists the run, and the record for the PDF is version 2 with the
  ledger's `render_input_sha256` and `pdf_sha256`. Only information-level findings are tolerated
  (`finalized_valid`, `superseded_pdf_present`, `duplicate_pdf_present`, `attempt_record_valid`,
  `unarchived_pdf_present` is **not** tolerated after finalization).
* **Reuses:** `finalization_audit.audit_period`, `history_access.open_history`.

### 5.7 `ledger`

See [ledger_schema](ledger_schema.md). Single module owning all SQLite access to the ledger: request intake,
run creation, state transitions (with expected-state checks), events, outbox, locks, delivery mirror, recovery.

### 5.8 `delivery_manifest` and `drive_publisher`

See [delivery_architecture](delivery_architecture.md). The manifest builder is pure; the publisher is the only
component that talks to Google Drive and is injected (so R8.3 can run with a `NullPublisher`).

### 5.9 `pipeline.run_client(client_key, period, *, services, ledger, mode)`

* **Purpose:** drive one client and period through the state machine, resumably.
* **Inputs:** `client_key`, `PeriodSpec(start, end)`, a `Services` bundle (clock, loader result, collector,
  report/render/finalize/audit services, publisher, paths, settings), the ledger, `mode` (`production` | `dry_run`).
* **Output:** `RunOutcome` (the response contract in [execution_contract](execution_contract.md)).
* **Algorithm [P]:**
  1. Acquire the client lock (`ledger.acquire_lock`); on failure return `run_in_progress` without side effects.
  2. Load or create the run row (`run_key` unique). If `delivered`/`ready_for_delivery`/`audited` already hold the
     requested result, return it (`already_complete`) without regenerating.
  3. Determine the resume point from the state (table in [ledger_schema](ledger_schema.md) §6).
  4. For each stage: set the in-progress state (transaction), execute (no DB transaction open), record the
     completed state (transaction), append the event, enqueue the status outbox row.
  5. On exception: classify to an error category, store `failed`/`blocked`, never re-raise to the caller of
     `run_client` (the caller sees a `RunOutcome`).
  6. Release the lock in `finally`.
* **Idempotency:** every stage is either read-only, deterministic, or already idempotent in the engine [V].
* **`dry_run` mode:** performs steps 1–4 for collection → assembly → render **only**, into a scratch directory,
  with an in-memory ledger; never finalizes, never writes the history DB, the production output root, the real
  ledger or Drive.

### 5.10 `runner` (multi-client)

* **Purpose:** process an ordered list of requests/clients sequentially with failure isolation.
* **Rules:** sequential processing, per-client isolation, batch-abort conditions, timeouts and backoff: §8 below.
* **Output:** `BatchOutcome(results: tuple[RunOutcome], counts, aborted: bool, abort_reason)`.

### 5.11 `request_queue` (intake)

* **Purpose:** read the `Report Requests` tab (read-only), validate rows against the request schema, and register
  new requests in the ledger by `idempotency_key`. Rows already registered are ignored; invalid rows are recorded
  as `rejected` with a fixed code and reported by the status path (the sheet is never written by the runner).

### 5.12 `status_outbox`

* **Purpose:** post status JSON ([execution_contract](execution_contract.md)) to the Make.com status webhook from
  the ledger outbox, with retry/backoff; failures never affect run state.

### 5.13 `cli`

See §9 and [execution_contract](execution_contract.md) / the CLI contract below.

## 6. Error conditions summary

Fixed error categories with retry eligibility are defined once, in [execution_contract](execution_contract.md) §5.

## 7. Idempotency rules (summary)

1. One run per `run_key`; a second request for a run that is running → rejected `run_in_progress`.
2. A run that already reached `audited` or later is never regenerated by `run`; the stored result is returned.
3. Rendering after a first render uses the sidecar's identity (hash and generation date) [V].
4. Finalization is called with the same `render_input`, `finalized_at` and `replace_existing` on retry.
5. Publishing is keyed by `pdf_sha256` + `run_key`; an existing matching Drive file is reused, never overwritten.
6. Delivery is keyed by `delivery_key`; a delivery retry reuses the same Drive file and never invokes the runner.
7. Replaying a request with the same `idempotency_key` returns the stored response.

## 7a. Concurrency and the existing databases

* The snapshot database is a single-writer SQLite file (rollback journal, busy timeout); there is **no
  cross-process lock** in the engine [V]. The ledger's `runner:global` lock therefore protects the engine too:
  at most one runner process may call `finalize_report` at a time. Task Scheduler must be configured with
  "do not start a new instance".
* The ledger file is a different file from the snapshot database and from the output root; the loader refuses a
  ledger path that overlaps either (same rule as `output_layout.resolve_output_root`).

## 8. Multi-client execution (Design H)

| Aspect | Rule |
|---|---|
| Order | requests ordered by `requested_at`, then `client_key`; clients are processed **sequentially** |
| Isolation | each client runs inside `run_client`, which never raises; one failure never stops the batch |
| Batch aborts | only for errors that will hit every client: `auth_required` (reporting credentials), `ledger_unavailable`, `disk_full`; remaining requests stay `queued` |
| Concurrency | none initially. Parallel execution is **not** justified until R8.7 measures it: the engine has no cross-process lock, SQLite allows one writer, Chromium runs per render, and one OAuth identity shares one quota |
| Timeouts [P] | per stage: collection 10 min, render 5 min, finalize 3 min, audit 2 min, publish 5 min; per client 25 min; per batch configurable (default 6 h). Values are starting points to tune in R8.7 |
| Retry/backoff | source calls: engine's own 4 attempts (1 s doubling, capped 30 s) [V]; run-level automatic retry of retryable categories: at most 2 further attempts per run per batch with 5 and 15 minute delays, then wait for the next poll; `source_temporary` after those → remains `failed` retryable |
| Quotas | 429 handled inside source modules; the runner adds a 1-second pause between clients (configurable) and records `quota_pressure` events [A] |
| 5 clients | one poll, minutes |
| 20 clients | one poll, well under the batch deadline |
| 50 clients | one poll; render/fetch time dominates; measure |
| 100 clients | may need two polls or a longer deadline; the queue continues on the next poll |

Per-client wall-clock times are unknown until measured; R8.7 records them and tunes the timeouts. Estimates in
earlier documents were illustrative [A].

## 9. CLI contract

Entry point `seo-report` (future `python -m seo_report`). Output is machine-readable JSON by default
(`--format json`), human text with `--format text`; logs are JSON lines on stderr; no output ever contains
credentials, tokens, client names, e-mail addresses or file paths outside the output root.

Common arguments: `--config PATH` (non-secret runner settings), `--format json|text`, `--log-file PATH`,
`--no-color`. Exit codes (stable):

| Code | Meaning |
|---|---|
| 0 | success (including `already_complete`) |
| 2 | usage error |
| 10 | validation failed (request/client/period) |
| 11 | configuration invalid |
| 20 | authentication required (non-interactive) |
| 21 | temporary source failure |
| 22 | source access denied or incompatible |
| 30 | render failed or layout issues |
| 31 | finalization conflict (operator needed) |
| 32 | finalization failed (retryable) |
| 33 | audit failed |
| 40 | publish failed |
| 50 | run in progress / lock held |
| 70 | batch finished with at least one failure |
| 99 | internal error |

| Command | Purpose | Key arguments | Writes? |
|---|---|---|---|
| `validate` | check settings, workbook structure, per-client config and period; with `--online` also credential presence and GSC/GA4/Drive access checks | `--client KEY`/`--all`, `--period-id ID`, `--online` | nothing |
| `dry-run` | collect, assemble and render into a scratch folder; no finalize, no production writes | `--client KEY`, `--period-id ID`, `--scratch-dir PATH` | scratch folder only |
| `run` | production execution; explicitly requires `--production`. `--request-id ID` runs one registered request, `--client/--period-id` runs one pair, `--queue` polls the Report Requests tab once and processes all pending | `--production` (required), `--replace-existing`, `--max-clients N` | ledger, history DB, output root, Drive, status webhook |
| `status` | read the ledger | `--run-key`, `--request-id`, `--open`, `--all` | nothing |
| `audit` | read-only audit of one period | `--client`, `--period-id`, `--deep` | nothing |
| `delivery-status` | read delivery records and the mirrored Delivery tab | `--run-key`, `--delivery-key`, `--pending` | nothing (the mirror refresh is part of `run --queue`) |

Safety restrictions: `run` without `--production` exits 2; `dry-run` refuses a scratch directory inside the
output root, the history database folder, the ledger folder or the project tree; `dry-run` and the read-only
commands open the ledger read-only and never create it; every command prints `"mode": "production" | "dry_run" | "read_only"`.
An optional operator-only `auth` command for interactive sign-in is described in
[security_and_authentication](security_and_authentication.md) and needs separate approval.

## 10. New modules potentially required (summary)

`contracts.py`, `runner_config.py`, `ledger.py`, `client_loader.py`, `source_collector.py`, `report_service.py`,
`render_service.py`, `finalize_service.py`, `audit_service.py`, `pipeline.py`, `runner.py`, `request_queue.py`,
`status_outbox.py`, `delivery_manifest.py`, `drive_publisher.py`, `cli.py`, `__main__.py`, plus a non-secret
settings file `config/runner.yaml`. The staging of these files is in [implementation_roadmap](implementation_roadmap.md).
