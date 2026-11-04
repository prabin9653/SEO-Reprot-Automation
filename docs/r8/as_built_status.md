# R8 — As-built status (written at R8.4.6; updated through R8.7b)

> **Current summary (R8.7b).** Implemented and tested with synthetic data only: the engine, ledger, pipeline, batch runner, CLI (`system-info`, `doctor`, `clients`, `setup auth`, `report dry-run|run|batch`, `status`, `recovery inspect|acknowledge|cancel`), the Google client factory and its wiring into `report run|batch`, logging activated by the CLI, and the local preflight. **Not done:** no live Google access has been verified; the Google credentials **were relocated in R8.7d** (2026-11-02, by the operator: copied to `%LOCALAPPDATA%\JasperSEOReporting\credentials\`, hashes and ACL verified, external `runner.yaml` updated; the OneDrive originals are retained and not retired); no real report has been produced; the production gate blocks real `report run|batch` and state-changing recovery (exit code 4) until a valid verification attestation exists (the mechanism exists since R8.7c; no attestation has been issued); Make.com, request intake, the status sender, Drive upload, approval, e-mail, a scheduled task, backups and monitoring are **not implemented**. `pyproject.toml` and `README.md` exist since R8.7b, but installing the package has not been verified. Sections below were written at R8.4.6; where one is marked *(superseded)* the newer statement here and in section 11a wins.

This document describes the system **as it exists in the code today**. The other documents in `docs/r8/` were written at R8.2 as
designs; each now starts with a short status block that points here and lists where the design and the implementation differ.
Nothing below is claimed beyond what the code and the tests show.

**Status legend**

| Label | Meaning |
|---|---|
| **Implemented and tested** | Code exists and is covered by automated tests that run offline with fakes/synthetic data |
| **Implemented, not live-tested** | Code exists; its contact with the real outside world (Google, Windows services, a real Chromium on a clean machine) has not been exercised, or only partly |
| **Proposed** | Written down as a recommendation or design; no code |
| **Not yet implemented** | Required by the design; no code |
| **Known limitation** | Behaviour that exists and is accepted or unresolved |

Test baseline when this was written: the full suite passes (see the R8.4.6 completion report for the exact count). "Tested" always
means offline tests; no stage so far has called a Google API, Make.com, Drive or e-mail.

---

## 1. Scope map: what exists

| Capability (design name) | Module | Status |
|---|---|---|
| Client/period loading from the reporting workbook | `client_loader` | Implemented and tested (raw rows); reading the live workbook is Implemented, not live-tested |
| Search Console / GA4 collection and classification | `source_collector` (+ `sources/gsc.py`, `sources/ga4.py`, R0–R7.5) | Implemented and tested; live Google calls not live-tested |
| Report assembly | `report_service` | Implemented and tested |
| Rendering | `render_service` → `pdf_renderer` (R6/R7) | Implemented and tested incl. one real-Chromium suite |
| Finalization, promotion, records, audit | `finalize_service`, `audit_service` → R7.x engine (unchanged) | Implemented and tested |
| Execution ledger (SQLite) | `ledger` | Implemented and tested |
| Pipeline for one client | `pipeline.run_client` | Implemented and tested |
| Multi-client runner | `runner.run_batch`, `retry_requests_due`, `retry_due` | Implemented and tested |
| Settings | `runner_config` | Implemented and tested |
| Non-interactive authentication | `auth` | Implemented and tested (synthetic tokens); never run against Google |
| Operational logging | `runner.configure_logging` and friends | Implemented and tested; **activated by the CLI** once per command since R8.6c |
| Command-line entry point (`cli`) | `cli`, `cli_exit`, `__main__` (`python -m seo_report`) | Implemented and tested for every command: `system-info`, `doctor`, `clients list|validate`, `setup auth` (R8.6a–c) and `report dry-run|run|batch`, `status`, `recovery inspect|acknowledge|cancel` (R8.6d, `cli_report.py`; synthetic integration tests only, **not executed against production**) |
| Local deployment preflight | `preflight` (`doctor`, `system-info`, `production_gate`) | Implemented and tested (R8.6c); local only — not a live verification |
| Google client factory, configurable credential locations | `google_clients`, `auth.CredentialPaths`, `paths.oauth_client_file`/`oauth_cache_file` | Implemented and tested offline (R8.6b); wired into `report run|batch` and `clients` (R8.6d); not live-tested |
| Request intake from the `Report Requests` tab (`request_queue`) | — | **Not yet implemented** |
| Production `collect` factory that builds the real Google clients | `google_clients.make_collect` | Implemented and tested with fakes (R8.6b/d); used by `report run|batch`; never run against Google |
| Status outbox **sender** (posting to Make) | — | **Not yet implemented** (rows are written; nothing reads and posts them) |
| Delivery manifest, Drive publisher, approval, e-mail | — | **Not yet implemented** |
| Make.com scenarios | — | **Not yet implemented** (R8.5+) |
| `Client Delivery Settings`, `Report Requests`, `Execution History`, `Delivery` tabs | — | **Not yet implemented** (the pipeline reads only `Clients` and `Keywords`) |

## 2. Request and response contracts (R8.3, extended in R8.4.2) — Implemented and tested

Module `contracts`. Closed JSON-schema-style messages, `version` 1, fixed codes, no client data or secrets.

* `jasper.run-request`: `request_id` (UUID), `idempotency_key`, `client_key`, `period_id` (`YYYY-MM-DD_YYYY-MM-DD`, exactly 28 days),
  `action`, `requested_at`, `requested_by`, `options` = `{replace_existing, operator, reason}`.
* Actions: `validate`, `dry_run`, `run`, `retry`, `publish`, `status`, `acknowledge`, `cancel`, `supersede`.
  * `validate` and `status` are read-only. `publish` is **rejected** (`publish_not_supported`): delivery does not exist yet.
  * `dry_run` is not accepted as a ledger request (`action_not_supported`); the dry run is `run_client(mode="dry_run")`.
  * `acknowledge`, `cancel`, `supersede` are *operator actions*: `options.operator` (a label) and `options.reason` (one of eight
    fixed codes: `blocking_condition_resolved`, `partial_file_reviewed`, `audit_finding_reviewed`, `data_corrected`,
    `content_error`, `no_longer_needed`, `duplicate_request`, `operator_decision`) are mandatory for them and refused for all other
    actions. There is deliberately no "force" action.
* `jasper.run-response`: identifiers, `state`, `outcome` (`succeeded | in_progress | failed | blocked | rejected | already_complete`),
  `attempt`, `identity` (render hash, generation date, PDF hash, record name), `audit`, `error {category, code, stage}`,
  `retry {eligible, kind, after}`, `warnings`, `timestamps`, `run_id`, `revision`. Optional, present only when they carry
  information (so first-generation messages are byte-identical to the original schema): `generation`, `supersedes`,
  `operator_action`.
* `jasper.batch-summary` (counts, results, `not_started`, `aborted`, `abort_reason`) and `jasper.delivery-ready` are validated by
  `contracts`; the delivery-ready message has a builder/validator but **nothing produces it** (no delivery stage).
* Run keys: `<client_key>/<period_id>` for generation 1, `<client_key>/<period_id>~N` for later generations.
* Reject codes (closed set): `run_in_progress`, `no_run`, `not_failed`, `not_audited`, `use_retry`, `operator_required`, `cancelled`,
  `publish_not_supported`, `period_differs_from_workbook`, `client_unknown`, `client_invalid`, `action_not_supported`, `not_blocked`,
  `condition_not_recoverable`, `blocking_condition_unresolved`, `replace_existing_required`, `report_exists`, `replacement_pending`,
  `original_changed`, `operator_details_required`, `not_cancellable`, `not_supersedable`.
* Transport: today requests are in-process `RunRequest` objects. HTTP/webhook transport to Make is **Not yet implemented**.

## 3. State machine (R8.3) — Implemented and tested

States: `requested → collecting → rendering → rendered → finalizing → finalized → auditing → audited → publishing →
ready_for_delivery → delivered`, plus `failed`, `blocked`, `cancelled`. **The pipeline stops at `audited`**: `publishing`,
`ready_for_delivery` and `delivered` exist in the machine but no code reaches them.

* In-progress states: `collecting, rendering, finalizing, auditing, publishing`. An in-progress state can end in `failed` or
  `blocked`, or fall back to its resume state after an interruption (`collecting/rendering → requested`, `finalizing → rendered`,
  `auditing → finalized`, `publishing → audited`).
* `failed` / `blocked` move only to a resume state, and only through an admitted retry that starts a new attempt.
* Any non-terminal state can become `cancelled`; `delivered` and `cancelled` are terminal.
* Error categories decide `failed` versus `blocked`: `layout_issues`, `finalization_conflict`, `audit_failed` block (an operator
  must act); the rest fail, with a retry classification (`collection | render | finalization | publish | delivery`).
* Admission for an existing run (`pipeline._admit`, always against the **latest generation** of the period): resumable states resume
  on `run`; `audited` and later return `already_complete`; `cancelled` rejects `run`; `blocked` rejects `run` and `retry` unless the
  run has been acknowledged; `failed` retries only when `retry_eligible` and only on `retry`.

## 4. Pipeline, runner, dry run — Implemented and tested

**`pipeline.run_client(client_key, period, services, ledger, request, mode)`**

1. Validates the request against the loaded client and the workbook's explicit period (no run is created for an invalid request).
2. Registers the request by idempotency key (a stored response is replayed verbatim).
3. Takes `runner:global` (unless the batch runner holds it) and `client:<key>` locks, after recovering runs a dead process left in an
   in-progress state; starts the lease keeper (section 8).
4. Admits (create / resume / retry / return / reject) or handles an operator action.
5. Runs the stages. Each stage first looks at what exists, so a resume never repeats finished work. Collection and assembly are
   read-only and repeated on resume; rendering reuses the open sidecar (original render hash and generation date survive);
   finalization reuses the stored `finalized_at`; the audit gate is read-only and mandatory.
6. Returns a validated response. Expected failures are `StageError`s (category + fixed code) and never raise out of `run_client`.
   Unexpected exceptions become `internal_error` with `exception_<class>` as the code; the traceback goes to the (sanitized) log only.
   `KeyboardInterrupt` and `SystemExit` are never caught; locks are released in a `finally`.

**`runner.run_batch(requests, services, ledger)`** processes requests one after another in a single process, ordered by
`(requested_at, client_key, request_id)`. It holds `runner:global` for the batch. It stops the batch (remaining requests stay
`not_started` and unregistered) on: `auth_required`, `disk_full`, `ledger_unavailable` (also after 3 consecutive busy-ledger clients),
`batch_deadline`, `lock_unavailable`. A client-level failure never stops the other clients. `retry_requests_due` builds automatic
`retry` requests for failed runs whose category allows it, whose retry time has passed and whose attempt count is within
`max_automatic_attempts`.

**Library dry run** *(superseded for the CLI: `report dry-run` is plan-only since R8.6d and renders nothing; `--scratch-dir` is rejected, exit code 2)* (`run_client(..., mode="dry_run", scratch_dir=...)`, not used by any command): collection, assembly and rendering only, into an isolated scratch
folder. It never touches the ledger, the history database or the output root, never finalizes, and ends in state `rendered`. The
scratch folder must be absolute and, after canonical resolution (section 7), must not overlap the output, work, history or ledger
locations or the project tree.

## 5. Ledger (R8.3, schema v2 since R8.4.2) — Implemented and tested

Local SQLite (`PRAGMA journal_mode=DELETE`, `synchronous=FULL`, `foreign_keys=ON`), `BEGIN IMMEDIATE` write transactions, optimistic
`state_version` on runs.

* Tables: `requests`, `runs`, `run_attempts`, `events` (append-only by trigger), `deliveries` (written by `record_delivery`; no
  caller exists), `locks`, `outbox`.
* **Schema version 2** (R8.4.2): `runs` gains `generation`, `supersedes_run_key`, `link_kind` (`supersede | restart`),
  `link_operator`, `link_reason`, `acknowledged`, `ack_operator`, `ack_reason`, `ack_at`; uniqueness is
  `(client_key, period_start, period_end, generation)`; `requests.action` accepts the three operator actions. A version-1 ledger is
  migrated in one transaction on first open (rows copied unchanged as generation 1); any failure rolls back and leaves version 1; a
  read-only open of version 1 is refused. Runs are never deleted (trigger). `generation_date` and `finalized_at` are write-once
  (trigger).
* **Outbox**: a `make_status` row is written in the same transaction as the state change it reports (deduplicated by
  `<run_key>#<state_version>#make_status`). **Nothing sends these rows** — Not yet implemented.
* Events record state changes, recoveries, locks, warnings, errors and operator actions (`kind = operator`) with fixed codes only.

### 5.1 SQLite error classification and transaction safety (R8.4.1) — Implemented and tested

`LedgerError(code, storage, transient)`. Storage codes: `ledger_unavailable`, `ledger_busy` (the only transient one),
`ledger_disk_full`, `ledger_io_error`, `ledger_corrupt`, `ledger_connection_unusable`, `ledger_error`. Classification uses SQLite's
result code, not message text. Every write goes through `_tx`: any exception in the body (including `KeyboardInterrupt`) rolls back
and is re-raised; a failing `COMMIT` or rollback is noted and, if the rollback itself fails, the connection is closed and refuses
further writes (`ledger_connection_unusable`). Nothing is retried automatically inside a transaction.

* **Client-level** (answer for this client; the batch continues): stale state, lock held/lost, a transient busy ledger →
  an unrecorded `rejected` response (`run_in_progress` or `lock_unavailable`); the request stays `accepted` and is tried again.
  Logical ledger errors become a `failed` response (`ledger_failure_response`).
* **Batch-level** (the batch stops): a storage failure that is not transient (`ledger_unavailable`, `ledger_disk_full`, ...), or three
  consecutive clients that found the ledger busy.

## 6. Operator recovery (R8.4.2) — Implemented and tested

* **Acknowledge** (blocked runs only): records operator + reason in an `operator` event and sets `acknowledged`; it is
  version-checked, append-only and idempotent. It **never moves the run**. It is refused while a partial file exists, while the audit
  gate still fails (for `audit_failed`), and for conditions a retry cannot fix (`render_input_already_finalized`,
  `record_identity_differs`, `existing_record_*`, `reader_record_limit`, `superseded_only_copy`, `existing_report_not_regular`).
* **Retry after acknowledgement**: re-validates the blocking condition at the moment of the retry, starts a new attempt at the resume
  state, keeps generation date and render identity, and still passes the audit gate. `existing_report_differs` additionally needs
  `replace_existing`. Any transition clears the acknowledgement flag (the record of who acknowledged stays in the events).
* **Cancel**: failed, blocked or recovered runs that have not produced a finalized report; refused (`not_cancellable`) for finalized or
  audited runs, for runs whose render hash is already finalized by the engine, and for a run under another process's live lock
  (`run_in_progress`). It deletes nothing; a repeat is idempotent.
* **Supersede**: on an `audited` run (and `ready_for_delivery`/`delivered`, unreachable today) creates a **new run**
  `<key>~N` linked to it (`link_kind = supersede`), with its own request, attempts and history, `replace_existing = true`
  (explicit consent required), and runs it through full finalization and audit. The original run, record and ledger row are untouched;
  the engine's safe promotion keeps the previous PDF as `report.superseded.<hash16>.pdf`. The original must pass the audit gate
  before, and is checked byte-for-byte (record hash, PDF hash) before the replacement is audited (`original_changed`). On a cancelled
  run `supersede` creates a `restart` generation. A repeat of the same request (same idempotency key, or the interrupted request
  re-sent) continues the same replacement.
* **Limitations**: a replacement with identical data on the same day is refused by the engine (`render_input_already_finalized`,
  not recoverable); a cancelled run's open sidecar blocks a restart with a different render identity until moved by hand;
  a second `supersede` with a *new* idempotency key after the replacement is audited creates the next generation (requests do not
  name a target run); partial files are never deleted — an operator moves them; the audit module is unchanged, so the relationship
  between original and replacement is visible in the ledger, events and responses, not in the engine's audit report.

## 7. Filesystem isolation (R8.4.3) — Implemented and tested

* `runner_config.canonical_path` resolves a path through its deepest **existing** ancestor with the operating system (case, 8.3 short
  names, junctions, symbolic links, aliased parents) and appends the not-yet-existing remainder. Nothing is created or written.
  It fails closed (`path_ambiguous`) for `..`, Windows alias spellings (trailing dot/space, `:` streams, device names), relative or
  malformed input, dangling links and any resolution error.
* Overlap checks use canonical keys: the ledger, history, work and output locations must not overlap each other or the project;
  the history database must not sit in the output folder; OneDrive protection is unchanged and also catches aliases.
* **Database locations** (ledger and history) must be on a local **fixed** drive: UNC paths (judged lexically, with no network
  access), mapped network drives (`GetDriveTypeW` = remote) and drives of unknown/removable/optical/RAM type are refused
  (`path_on_network`, `drive_type_unknown`); the canonical path is judged again so a link to a network location is caught.
  `Ledger()` applies the same rule itself. Output and work folders are not subject to the drive-type rule.
* Dry-run scratch folders use the same canonical comparison, before and after creation.
* **Limitation**: checks happen at configuration/scratch time; a link swapped in afterwards is not detected.

## 8. Locks, liveness, leases, render timeout (R8.4.4) — Implemented and tested

* **Liveness** (`ledger.probe_pid`): Windows `OpenProcess` with explicit ctypes types. "Not found" (error 87) is the only proof of
  death; access denied means alive; every other error, an unreadable exit code or an invalid PID is unknown, and unknown counts as
  alive. No `os.kill`. A live PID whose process was created after the lock was acquired (plus 5 s) is treated as a reused ID.
  Takeover still needs: lease expired, same host, holder proven gone. **PID-only identity is not proof**; creation time covers the
  common reuse case; a stricter identity would need the creation time stored in the lock row (a schema change not made).
* **Lease keeper** (`ledger.LeaseKeeper`, started by `pipeline` for every run): one daemon thread renews the client lease (and the
  runner's batch lease when this ledger owns it) every `execution.heartbeat_interval_seconds` (default `min(60, lock_ttl/4)`; at most
  `lock_ttl/3`). It uses its own connection, verifies owner **and** lease identity (`acquired_at`), never takes a lost lease back, and
  marks it lost after a conflict or after 3/4 of the lease without a successful renewal. The pipeline checks at every stage boundary
  and before every recorded step; a lost lease stops the run before further writes (`run_in_progress`). The thread is joined in a
  `finally` (KeyboardInterrupt/SystemExit included) before the locks are released. If the process dies the lease expires normally.
  **Limitation**: loss is noticed at the next boundary; a stage already running continues until it returns. Lease timestamps have
  one-second resolution.
* **Render watchdog** (`pdf_renderer`): Playwright timeouts for launch, navigation and context; a single watchdog thread per render
  ends the browser processes this render started (descendants of this process that did not exist before it) when the hard limit
  `execution.stage_timeouts.rendering` (default 300 s) passes, and the browser gets a separate 15 s to close. A breach raises
  `RenderTimeoutError(phase)` before any PDF is returned, mapped to `render_failed` / `render_timeout_<phase>` (retryable). Nothing is
  promoted or finalized; partial files are kept. **Limitations**: process ending works on Windows only (elsewhere the timeout is
  reported after the call returns); only the `rendering` entry of `stage_timeouts` is enforced — `collecting`, `finalizing`,
  `auditing` and `publishing` entries are accepted by the settings but **not enforced**; the cooperative `client_timeout_seconds`
  is checked only at stage boundaries.

## 9. Authentication (R8.4.5) — Implemented and tested (never against Google)

* `auth.get_credentials()` is **non-interactive**: it loads the cached token, refreshes it if expired, otherwise raises
  `AuthRequiredError` (a `StageError`, category `auth_required`). The only interactive code is `auth.run_setup()`, reached only by
  `python -m seo_report.auth setup` run by a person; it refuses without an attended console (`auth_interactive_blocked`).
  Scopes, token and client-secret locations and the Google Cloud project are unchanged.
* Codes: `auth_token_missing`, `auth_credentials_unreadable`, `auth_credentials_invalid`, `auth_refresh_unavailable`,
  `auth_credentials_revoked` (`invalid_grant`), `auth_refresh_failed`, `auth_interactive_blocked`; `auth_network_error` is
  `source_temporary`.
* Classification (`source_collector.classify_error`, also used by `client_loader.load_from_workbook` → `WorkbookUnavailable`):
  authentication → `auth_required` (never temporary); 403/404/`PERMISSION_DENIED`/`NOT_FOUND` → `source_denied`; 400 and similar →
  `source_invalid`; 408/429/5xx, gRPC `UNAVAILABLE`/`DEADLINE_EXCEEDED`/`RESOURCE_EXHAUSTED`..., DNS, timeouts, connection errors,
  rate-limit 403 → `source_temporary`; anything else is not classified and stays visible (`internal_error`). An `auth_required`
  failure stops the batch.
* Retries: the source modules retry 429/5xx up to 4 times themselves; a temporary failure then becomes a `failed` run that the
  runner retries automatically with backoff up to `max_automatic_attempts`; `auth_required` is retry-eligible only after a person
  fixed the credentials; permanent source errors are never retried automatically.
* Credentials still live inside the project (and OneDrive). See `credential_relocation_procedure.md`. **Proposed**, not done.

## 10. Operational logging and secret sanitization (R8.4.6) — Implemented and tested

* `runner.configure_logging(log_dir=None)` writes the `seo_report` loggers as **JSON lines** to
  `%LOCALAPPDATA%\JasperSEOReporting\logs\seo_report.log` (rotating, 5 MB × 5 backups = at most 30 MB). The folder must be absolute,
  outside the project and OneDrive. Calling it again replaces the handler (no duplicates); `shutdown_logging()` detaches it.
  *(superseded)* Since R8.6c the CLI calls it once per normal command (not for `--help`, `--version` or usage errors); see section 11a.
* Fields: `ts`, `level`, `logger`, `event`, then `run_key`, `client_key`, `period_id`, `stage`, `run_id`, `request_id` (from the
  pipeline's context, so ledger/auth/render records carry them), `error_category`, `error_code`, `exc_type`, `exc`, `fields`.
* Sanitization (filter + formatter): secrets are replaced by `[REDACTED]` — bearer/authorization values, `ya29.`/`1//`/`GOCSPX-` tokens,
  `access_token`/`refresh_token`/`client_secret`/`password`/`api_key`-style pairs, private-key blocks, and long opaque
  letter+digit strings; lowercase hex digests (16/32/64) are kept. Local paths are reduced to the file name. Values are never
  `repr()`ed (credential objects become `<Credentials>`); keys that look secret are redacted whatever the value. Exceptions keep their
  class, `category`/`code` and a sanitized traceback; messages are length-bounded.
* A failing log handler never raises into a run (`SafeRotatingFileHandler` counts failures; the pipeline's own log calls swallow
  `Exception`), and `KeyboardInterrupt`/`SystemExit` still propagate with locks released. **Limitation**: modules other than the
  pipeline and runner call their loggers directly, so a *foreign* handler that raises could still interrupt them; the file handler
  installed by `configure_logging` cannot.

## 11. Dependencies and assets (R8.4.6)

See `dependency_and_asset_readiness.md`. `requirements.txt` (13 direct pins), `requirements-dev.txt`, and the exact closures
`requirements-lock.txt` (33) and `requirements-dev-lock.txt` (49) were generated from the installed metadata; no clean-machine install
has been performed. `scripts/dependency_report.py` and `scripts/check_deployment.py` are read-only checks.

## 11a. CLI, logging activation and preflight (R8.6a–c) — Implemented and tested (local)

* `python -m seo_report` (`cli.py`, stable exit codes in `cli_exit.py`): no command defaults to production (a settings file must be named); `report run|batch` and
  `recovery acknowledge|cancel` need `--production`; `report dry-run` never touches production. Implemented now: `system-info`, `doctor`, `clients list|validate [--online]`,
  `setup auth`, and since R8.6d `report dry-run|run|batch`, `status`, `recovery inspect|acknowledge|cancel`. Real production execution is **blocked by the production gate** (`live_google_not_verified`, `credentials_not_relocated`).
* **Logging is active** for every command except `--help`/`--version`/usage errors: one JSON-lines log (`%LOCALAPPDATA%\JasperSEOReporting\logs\seo_report.log`), the sanitizer of R8.4.6, one handler per
  command, detached afterwards; an unusable folder gives a one-line safe warning and the command continues without a log file (`doctor` then reports it as blocked).
* **Preflight / `doctor`:** read-only checks of runtime, dependencies, Playwright and Chromium (including a local launch in a separate process), fonts and logo (hashes and rendering fingerprints),
  storage, authentication configuration (metadata only), client configuration and logging; findings are ready/warning/blocked/unverified; exit codes 0/1/3/4/5 by a documented priority.
  `production_ready` is always false; `preflight.local_gate()` is the local half of `preflight.production_gate()`, which `report run|batch` and `recovery acknowledge|cancel` call before any work (R8.6d). See `r86_cli_reference.md`.
* **Not live-verified:** Google sign-in and API access, `clients list`, `clients validate --online`, `setup auth`, a clean-machine install, Task Scheduler.

* **R8.6d (Implemented and tested with synthetic data):** the seven commands run the real pipeline/runner/ledger against temporary ledgers with a stub renderer and fake sources. `report dry-run` is plan-only. `status` and `recovery inspect` are read-only. The gate blocks all real production use today. No real report, PDF, production ledger write or Google call has occurred.

* **R8.6e verification (2026-11-01):** see [r86_final_verification.md](r86_final_verification.md). It found three items, all corrected in **R8.6f**: `report dry-run --scratch-dir` was accepted but ignored (now rejected, exit code 2); some `--help` labels were stale (corrected); and `test_a_lease_is_renewed_during_a_stage_longer_than_the_lease` had a one-second-resolution assertion race (a test defect, not a lock defect; the assertion was corrected, a regression test added, 25 of 25 isolated runs passed and the full suite passed: 5,180 passed, 13 skipped).
* **R8.7a audit and R8.7b (launch fixes):** `python -m seo_report` fails with *No module named seo_report* unless `PYTHONPATH=src` is set. R8.7b adds `pyproject.toml` (src layout, the 13 existing runtime pins, console script `seo-report`) and `README.md`; the code finds `templates/`, `config/`, `assets/` relative to the checkout, so only an **editable** install is supported. Installing was not authorised: `pip install -e .` and the `seo-report` command are **unverified**.

* **R8.7c (Implemented and tested with synthetic data):** `attestation.py` and its gate integration. `preflight.production_gate` clears `live_google_not_verified` only while a valid, DPAPI-protected, machine- and account-bound, 7-day attestation exists (second stage: every client about to run must be covered with unchanged property fingerprints); `credentials_not_relocated` and all other blockers are independent (the relocation requirement is satisfied since R8.7d). `doctor` and `system-info` report the state (`live_google_attested`, `verification_attestation`). No attestation was created, no command can create one, and nothing in `src/` calls `issue_attestation`; the live verification that will (R8.7e) has not been performed. `production_ready` stays false. See `security_and_authentication.md` §12.

* **R8.7d / R8.7d.2:** the credentials were relocated (see the summary above). `doctor` / `system-info` now drop `credentials_not_relocated` from `outstanding` through `preflight.credentials_relocated(settings, findings)`, the same function the production gate uses; before R8.7d.2 the entry was a static label. R8.7d.3 closed a gap in that condition: the cloud-synchronisation check covered only the cached sign-in's folder, so a client file in OneDrive passed; now both OAuth locations are checked independently. `production_ready` stays false; ACLs and file contents are not checked by `doctor`.

* **R8.7e.1b (Implemented and tested offline with fake Google services; never run against Google):** `live_verification.py` and the command `verify live --client K --live`. It runs, in order, the configuration check, the OAuth credential load (`auth.get_credentials_with_status` reports `not_needed`/`ok`; `get_credentials` is unchanged), the existing workbook loader, `gsc.verify_access` and `ga4.verify_access`, builds the evidence only from those results and calls `issue_attestation` exactly once on complete success; any failure leaves the attestation untouched. The tripwire allows only this module. **No attestation exists and the live run (R8.7e.2) is pending approval.** See `security_and_authentication.md` section 13.

* **R8.7h.2 (Implemented and tested offline; never run on real data):** `report preview` (`preview.py`, `cli_report.handle_preview`): `pipeline.dry_run_client` with a `PreviewRenderer`, a unique folder under `%LOCALAPPDATA%\JasperSEOReporting\previews\`, a manifest, and the label `PREVIEW — NOT FINAL` on every page (`pdf_renderer.render_report_pdf(..., preview_label=...)`, measured per page; production renders unchanged). No ledger, history write, finalization, lock or production folder is involved. The live-data preview is R8.7h.3. See `r86_cli_reference.md`.

* **R8.7h.4 (Implemented and tested offline with synthetic data; layout of a real-data report not yet reviewed):** two presentation changes in `report_sections.py`. (1) The Google Search Console section no longer has the "Daily performance" table of dates, clicks and impressions; the KPI tiles, the "Clicks per day" and "Impressions per day" charts (including the missing-days caption), the insight paragraphs, the comparison statements and the notices are unchanged. (2) The Geographical Performance table and its chart show only the top 5 countries, ranked by impressions (descending), then clicks (descending), then country name (`report_sections.ranked_countries`); the caption reads "Top 5 countries by impressions (of N)". Both are presentation only: the render input still carries every daily point and every country (so the render fingerprint inputs and all calculations and insight paragraphs are unchanged), nothing in collection, Sheets, GSC or GA4 changed. A section with five or fewer countries is shown in full. A real-data PDF has not been re-rendered since; the report is now one page shorter in the synthetic standard case (6 pages instead of 7).

* **R8.7h.11 (Implemented and tested offline with synthetic data; not yet seen on a real-data preview):** the contact block (cover and CONTACT page) is built from the client's own `Client Configuration` row: `Project Manager` (name), `Manager Email`, `Manager Phone`, read by header name (`clients.CLIENT_COLUMNS`) and trimmed. It prints the name, the fixed line `Project Manager`, `Email: …` and `Phone: …`; a blank email or phone is left out with its label. `clients.resolve_sender(name, email, phone)` inherits nothing from the default sender: **R8.7h.12** makes the Project Manager name mandatory for every `Active` row (a blank one is a `SheetDataError` naming the row and column; a non-active row without one is allowed but cannot be rendered), with no fallback to the name in `config/jasper.yaml`. A non-blank Manager Email that is not shaped like an address is a `SheetDataError` naming the row and column (never the value). `config/jasper.yaml` still holds the old default sender, but none of it can reach the report. See `report_templates.sender_lines`.

* **R8.7h.15 (supersedes the contact-block format of R8.7h.11; implemented and tested offline with synthetic data, not yet seen on a real-data preview):** the contact block (cover and CONTACT page) again has the reference layout: name, `Project Manager | Jasper IT Inc.`, the regional address line(s), `Cell: …`, `E-mail: …`, `Website: …` (a link). Name, phone and e-mail still come only from the client's own row (columns C, E, D; name mandatory, the others optional and left out with their label when blank). The title, address and website come from the **regional profile** of the row's Target Country, defined in `config/jasper.yaml` under `regional_profiles` (`United States`: 3402 E Pine Brook Way, Houston, TX 77059, `Jasper IT Inc` → https://jasperitinc.com/; `Australia`: Ingleburn NSW 2565, https://jasperitsolutions.com.au/). The country is matched by exact name ignoring case and spacing (`clients.profile_for`); an `Active` client whose Target Country has no profile is a `SheetDataError` naming the row and column and the configured countries; there is no default profile. A client that is not `Active` may lack a manager or profile (it cannot be rendered). `jasper.yaml` no longer holds any personal contact detail; its `sender.name` is an organisation label that is never printed. Adding a country means adding a block to `regional_profiles`.

## 12. What is not covered by any test

Live Google (Sheets, Search Console, GA4, OAuth refresh), a clean-machine installation, Windows Task Scheduler execution, a real
mapped network drive, long-running (hours) leases, disk-full on a real disk, and real e-mail/Drive/Make flows.

