# R8.6 — Command-line interface reference

**Current state (R8.7b).** Every command below is implemented. All of it is verified with synthetic data and temporary folders only; **no live Google access, real report or production run has happened**, and the production gate refuses `report run`, `report batch`, `recovery acknowledge` and `recovery cancel` (exit code 4) until its attestation and live-verification stages are done. R8.6f corrected the `--help` text and made `report dry-run --scratch-dir` a usage error (exit 2). The sub-stage notes below are kept as history; where they speak of commands that "arrive later" or answer "not available yet", that is no longer true. Launching: `python -m seo_report` needs the package to be importable (`PYTHONPATH=src` today); a `pyproject.toml` with a `seo-report` console script exists since R8.7b, but installing it has **not** been verified (see `README.md`).

**Status after sub-stage R8.6d** (R8.6d wired `report dry-run|run|batch`, `status` and `recovery inspect|acknowledge|cancel` to the existing pipeline, runner and ledger behind the production gate; see "R8.6d" at the end). Nothing is deferred any more. Where R8.6d says otherwise it overrides the older text below.

**Status after sub-stage R8.6c** (R8.6c activated structured logging at the start of every command and added the `doctor` preflight and the complete `system-info`; see
"R8.6c" at the end). R8.6a and R8.6b below remain true except where R8.6c says otherwise (`doctor` is no longer deferred; `system-info` is complete).

**Status after sub-stage R8.6b** (R8.6b added the Google client factory, configurable credential locations, and the `clients list`, `clients validate` and `setup auth`
commands; see "R8.6b" at the end). The paragraph below describes R8.6a and remains true.

**Status after sub-stage R8.6a.** R8.6 is delivered in reviewable sub-stages; this document grows with each. The command tree, argument validation, the production
guard, configuration resolution, output formats, exit codes and the error boundary are **Implemented and tested** (R8.6a). Handlers marked *(R8.6b/c/d)* below are
parsed and validated completely but answered "not available yet" (exit code 13, nothing read or written) until their sub-stage (historical: all were delivered by R8.6d). Nothing here contacts Google, Make.com, Drive or
e-mail, and no command was run against production *(true at R8.6a; since R8.6b `clients list` and `clients validate --online` read the reporting workbook through Google Sheets, read-only, and `setup auth` signs in; none of these has been run live)*.

Run it with `python -m seo_report ...` (entry point `src/seo_report/__main__.py`, logic in `cli.py` and `cli_report.py`, exit codes in `cli_exit.py`). `pyproject.toml` also declares the console script `seo-report = seo_report.cli:main`.

## Global rules
* **No command defaults to production.** A settings file must be named: `--config PATH` or the environment variable `JASPER_SEO_CONFIG` (the output says which). Options
  `--config` and `--json` are accepted before or after the command (`python -m seo_report --json status --config PATH` and `... status --config PATH --json` are equivalent). `--help` and `--version` need no settings file.
* **State-changing commands need `--production`** in addition: `report run`, `report batch`, `recovery acknowledge`, `recovery cancel`. Without it the command stops with exit code 2
  before anything is read. `report dry-run`, `status`, `recovery inspect`, `doctor`, `system-info`, `clients` and `setup auth` have no such flag. `report dry-run` takes only `--client` and `--period`: any other option, including the former `--scratch-dir`, is a usage error (exit code 2); it never writes production storage or the ledger.
* Client keys (`--client`) are validated with `client_config.validate_client_key`; periods (`--period`) with `contracts.parse_period_id` (`YYYY-MM-DD_YYYY-MM-DD`, exactly 28 days); run
  identifiers are a run key `<client>/<period>[~N]` or (for `status`) the engine run UUID; operators are labels (no e-mail addresses); reasons are the eight fixed reason codes.
* Output: text by default; `--json` prints one JSON object (`{"ok": true, ...}` or `{"ok": false, "exit_code": N, "error": {"code", "message"}}`; an error can add `details`, for example the production-gate `blockers` list). Dynamic text is sanitized; no secrets are ever
  printed. Diagnostic detail goes to the sanitized log.

## Commands
| Command | Purpose | Arguments | Stage |
|---|---|---|---|
| `system-info` | informational summary: versions, OS, architecture, redacted configuration and storage paths, dependency, Playwright/Chromium, asset, credential-file and logging status, local readiness, "live Google verified: no" | — | **R8.6c** (complete) |
| `doctor` | the local, read-only deployment preflight (8 sections + overall readiness); text or `--json` | `[--skip-browser-launch]` | **R8.6c** |
| `clients list` | stable client keys, status, rejection codes (one bad row never hides the valid clients); **reads the reporting workbook through read-only Google Sheets access (it has no offline mode and no `--online` option)** | — | **R8.6b** (offline-tested; live read not yet verified) |
| `clients validate` | offline checks of the registry, sender configuration, workbook id and credential locations (nothing contacts Google); with `--online` it also reads the workbook (read-only Google Sheets access) and checks every row | `[--client KEY] [--online]` | **R8.6b** |
| `report dry-run` | plan only: what a real run would read, do and write, and the production blockers (**no** Google call, collection, rendering, ledger access or writes); exit code = the local preflight's | `--client KEY --period P` (no `--production`; `--scratch-dir` is rejected, exit 2) | R8.6d (done) |
| `report preview` | **LIVE (read-only Google) + Chromium**: a real-data report in its own preview folder, labelled `PREVIEW — NOT FINAL` on every page; touches no ledger, history, lock or production folder; **not yet run on real data** (R8.7h.3) | `--client KEY --period P` (the workbook's exact period) | **R8.7h.2** |
| `report run` | run one client through the ledger (today refused by the production gate, exit 4) | `--client KEY --period P --production` | R8.6d (done) |
| `report batch` | run clients sequentially (today refused by the production gate, exit 4) | `--period P --production [--client KEY]... [--max-clients N]` (`--client` repeatable; N is 1–1000) | R8.6d (done) |
| `status` | run states from the ledger (read-only); `--client` and `--run` are mutually exclusive | `[--client KEY \| --run RUN_ID]` | R8.6d (done) |
| `recovery inspect` | blocked and failed runs and what each needs (read-only) | `[--client KEY]` | R8.6d (done) |
| `recovery acknowledge` | record an operator review of a blocked run (never moves it); today refused by the production gate | `--run RUN_KEY --operator LABEL --reason CODE --production` (`CODE` is one of the eight reason codes) | R8.6d (done) |
| `recovery cancel` | cancel a run with no finalized report; today refused by the production gate | `--run RUN_KEY --operator LABEL --reason CODE --production` | R8.6d (done) |
| `verify live` | **LIVE**: contacts Google (read-only OAuth refresh if needed, the reporting workbook, Search Console, GA4) and, only if every check succeeds, stores the verification attestation that clears `live_google_not_verified`. May rewrite the cached sign-in. Never run by any other command; **not yet executed** (R8.7e.2 pending) | `--client KEY --live` (`--live` is mandatory; no evidence, timestamp or success flag can be supplied) | **R8.7e.1b** |
| `setup auth` | the only command that starts the browser sign-in (reuses `auth.run_setup`), with the configured credential locations; it needs an attended console (stdin and stdout are terminals) and refuses otherwise with exit code 5; never part of normal reporting, never started by another command | — | **R8.6b** (never run live) |

`recovery acknowledge|cancel` map to the existing operator actions (R8.4.2): they keep the version checks, the audit gate and the "never delete" rules because they go through the same
pipeline code. There is no `retry` command: `report run` sends the pipeline's `retry` action itself when the latest run is failed or blocked (admission rules still decide). `supersede` exists in the contracts and the pipeline but is **not exposed as a command**; neither are `poll` or `publish`.

## Exit codes (stable; never renumbered)
| Code | Name | Meaning |
|---|---|---|
| 0 | OK | completed (including "already complete") |
| 1 | INTERNAL_ERROR | unexpected exception; see the log |
| 2 | USAGE | invalid arguments, an unsupported option (for example `report dry-run --scratch-dir`) or missing `--production` |
| 3 | CONFIG_INVALID | configuration missing/invalid, or an invalid request (unknown client, period differs from the workbook) |
| 4 | PREFLIGHT_FAILED | the preflight found a mandatory requirement missing, or the production gate refused a production command |
| 5 | AUTH_REQUIRED | Google credentials missing/expired/revoked: run `setup auth` |
| 6 | SOURCE_UNAVAILABLE | a required Google source could not be read (temporary, denied, invalid, incompatible) |
| 7 | REPORT_FAILED | rendering/report generation failed or timed out |
| 8 | FINALIZATION_FAILED | finalization failed |
| 9 | AUDIT_FAILED | the mandatory audit did not pass; never deliverable |
| 10 | LEDGER_UNAVAILABLE | ledger/storage unusable, including a full disk |
| 11 | LOCK_CONFLICT | another process holds the lock, or the ledger was busy |
| 12 | OPERATOR_ACTION_REQUIRED | blocked run or a request that needs a decision |
| 13 | NOT_AVAILABLE | reserved: a command that is parsed but not built yet; no current command returns it |
| 130 | INTERRUPTED | Ctrl+C; locks were released |

Mapping rules reuse `contracts.ERROR_CATEGORIES` and the batch abort reasons (`cli_exit.BY_CATEGORY`, `cli_exit.BY_ABORT_REASON`); a batch exits with its abort reason if it aborted,
otherwise with the lowest-numbered failure among its clients.

## R8.6b — credential locations, Google client factory, injection

**Credential locations.** `paths.oauth_client_file` (the OAuth client definition, "client secret") and `paths.oauth_cache_file` (the cached sign-in, "token") in the runner
settings; both optional and independent. Omitted ⇒ the existing fallback inside the project (`auth.CLIENT_SECRET_PATH`, `auth.TOKEN_PATH`) — **nothing was moved and the current
location is not changed silently**. `%VARIABLES%` are expanded; paths must be absolute, name a file (not a folder), contain no `..`, and stay out of the output/work/history/ledger
folders; the two must differ. Settings errors name the key and a fixed code, never the value. `auth.CredentialPaths`/`auth.paths_from_settings` carry the result; **`setup auth` and
normal reporting read the same `CredentialPaths`**. Files are never opened to validate them (`auth.path_status` uses file-system metadata only), token refresh keeps the atomic write,
and the first `setup auth` creates a newly configured credentials folder if needed. The key names intentionally avoid the words "secret" and "token" (the settings loader refuses
credential-looking keys).

**Factory** (`src/seo_report/google_clients.py`, lazy, no Google import until a client is requested):

| Function | Builds | Via |
|---|---|---|
| `GoogleClientFactory.credentials()` | credentials (once per factory instance) | `auth.get_credentials(paths=...)` — non-interactive |
| `.sheets()` | Sheets API v4 service | `sources.sheets.build_service(credentials)` (now accepts credentials, like the other two) |
| `.search_console()` | Search Console API v1 service | `sources.gsc.build_service(credentials)` |
| `.analytics()` | Analytics Data client | `sources.ga4.build_client(credentials)` |

**Injection.** `make_collect(factory)` returns the `Services.collect` callable `(client_run, today) → SourceResult`: it builds the GSC and GA4 clients on the first call and delegates to the
existing `source_collector.collect_sources` (property/period validation, 429/5xx retries, error classification, valid-zero-result handling, notices — all unchanged).
`load_workbook_clients(factory, ...)` gives `client_loader.load_from_workbook` a lazily-built Sheets client so credential errors are classified inside the loader's guard
(`WorkbookUnavailable`, `auth_required` → exit 5). Every builder and the credentials provider can be replaced by a fake. `make_collect` is now wired into `report run|batch` (R8.6d).

**Exit codes for these commands.** Missing/expired sign-in or no console for setup → 5; permission, quota, network problems reading the workbook → 6; registry, sender, workbook id,
client key or headers invalid → 3.

**Offline verification only.** These commands and the factory were verified with synthetic token files, fake Google services and a guard that forbids any network connection. No live
Google call or sign-in was made; the live behaviour of `clients list`, `clients validate --online` and `setup auth` is **unverified** (see the deployment-readiness items).

Nothing is deferred after R8.6d.

## R8.6c — logging activation, preflight (`doctor`), `system-info`

### Logging activation
`cli.main` starts the existing structured log **once per command**, right after the arguments were accepted (`cli.activate_logging` → `runner.configure_logging`): JSON lines, the
existing sanitizer, 5 MB × 5 rotation, one handler (a second command replaces it; it is detached in a `finally`, also on Ctrl+C and `SystemExit`). Folder: `%LOCALAPPDATA%\JasperSEOReporting\logs\`
(absolute, outside the project and OneDrive, validated by `runner.check_log_dir` without creating anything). `--help`, `--version` and usage errors return **before** this point and never touch the log
folder. Each command writes `command started` / `command finished` (with the command name and exit code) and carries `client_key` / `period_id` when the command has them; records written by deeper
code carry the run identifiers through the existing context. If the folder is unusable the console gets one safe line (`warning: logging is unavailable (<code>); …`, no path), the command **continues
without a log file**, and `doctor` reports logging as **blocked** (a production run must not lose its diagnostics). A failing log file (full disk, locked file) is counted, never raised
(`SafeRotatingFileHandler.emit` now guards the lazy file open), so it cannot interrupt a command or corrupt the ledger.

### `doctor` — checks (all local and read-only; nothing is created in production storage, nothing contacts Google)
| # | Section | Checks | Possible statuses |
|---|---|---|---|
| 1 | Application and Python runtime | application version; Python = tested 3.14.x; architecture AMD64; Windows 11 | ready · unverified (untested Python/architecture/Windows) · blocked (not Windows) |
| 2 | Dependencies | every `requirements.txt` pin is installed (`importlib.metadata`) at the pinned version | ready · warning (version differs) · blocked (missing) · unverified (no requirements file) |
| 3 | Playwright and Chromium | Playwright installed; a `chromium-*` build folder exists; headless Chromium starts and stops (separate process, its own temporary folder created and removed; no network request) | ready · blocked (`playwright_missing`, `chromium_missing`, `chromium_launch_failed`, `chromium_launch_timeout`) · unverified (`--skip-browser-launch`) |
| 4 | Fonts and logo | logo and both DejaVu Sans faces configured, regular, readable; SHA-256 against the tested files; the rendering fingerprints (`render_identity.measure_render_fingerprints`) agree with the measured hashes | ready · warning (not the tested file: report identity would change) · blocked (`asset_file_not_found`, `asset_not_a_regular_file`, `asset_file_unreadable`, `fonts_not_configured`, `required_font_missing`, `fingerprints_disagree`) |
| 5 | Storage and filesystem | canonical/overlap/OneDrive/project/database-drive validation (done when the settings load); output, work, history and ledger folders exist and are writable (`os.access`; missing folders only warn and are **not created**); free disk space | ready · warning (`directory_will_be_created`, `low_disk_space`) · blocked (`not_a_directory`, `directory_not_writable`, `parent_not_writable`) |
| 6 | Authentication configuration | credential locations valid; client file and cached sign-in state (metadata only, **never opened**); writability; inside-project/OneDrive location; "live Google not verified" | ready · warning (`oauth_client_file_missing`, `credentials_inside_project`, …) · blocked (`auth_token_missing` = auth required, `auth_credentials_unreadable`) · unverified (`live_google_not_verified`) |
| 7 | Client configuration | settings file valid; client registry; sender configuration; reporting workbook id; no recipient setting exists (Manager Email stays the sender's contact); workbook rows (properties, periods, keywords) are **not read** | ready · blocked (`config_not_given`, `config_not_found`, settings codes, `client_registry_invalid`, `sender_config_invalid`, `reporting_workbook_not_configured`) · unverified (`workbook_rows_not_checked`) |
| 8 | Logging | log folder acceptable; logging active for this command | ready · warning (folder not created yet) · blocked (`log_dir_*`, `logging_unavailable`) |
| 9 | Overall readiness | derived from the findings | see below |

### Readiness model
Per finding: `ready` · `warning` (non-blocking) · `blocked` (required component missing/invalid) · `unverified` (needs a live environment or an action this check may not perform).
`overall_readiness` = `blocked` if any finding is blocked, else `warning` if any warning, else `ready`; **`unverified` findings never lower it but are always listed**.
`production_ready` is **always `false`** (as in R8.6c; still true) and `outstanding` lists why. The list is `live_google_not_verified`, `credentials_not_relocated`, `make_not_connected`,
`drive_not_connected`, `email_not_connected`. A passing local preflight therefore never means "ready for production".

### Exit code of `doctor` (first matching rule)
| Rule | Exit code |
|---|---|
| a check itself failed unexpectedly (`preflight_internal_error`) | 1 |
| a blocked finding in Client configuration → settings file/registry/sender/workbook id | 3 |
| any other blocked finding (runtime, dependencies, browser, assets, storage, logging, unusable credential location) | 4 |
| the only blocked finding is a missing/unusable sign-in (`auth_token_missing`, `auth_credentials_*`) | 5 |
| otherwise (ready, warnings, unverified) | 0 |
Warnings alone never produce a failing exit code. `preflight.local_gate()` applies the same rules; `preflight.production_gate()` adds the two production requirements `live_google_not_verified` (cleared only by a valid, genuine, unexpired verification attestation; none exists today) and `credentials_not_relocated` (see R8.6d and `security_and_authentication.md` §12).

### JSON schema — `jasper.preflight-report`, version 1 (`doctor --json`; one object, keys sorted)
```
schema, version          "jasper.preflight-report", 1
generated_at             UTC "YYYY-MM-DDTHH:MM:SS+00:00"
application              {name: "seo-report", version}
overall_readiness        "ready" | "warning" | "blocked"
production_ready         false
outstanding              [fixed codes]
counts                   {ready, warning, blocked, unverified}
exit_code                the process exit code
findings                 [{section, component, status, code, message, action}]   section ∈ runtime|dependencies|browser|assets|storage|authentication|configuration|logging
```
Codes are fixed lower-case identifiers; messages and actions never contain credential values, tokens, e-mail addresses or absolute user paths. `doctor` works even when the settings file is
missing or invalid (that is then a finding and exit code 3). Add `--skip-browser-launch` to avoid starting Chromium.

### `system-info` fields
`application`, `version`, `python`, `operating_system`, `architecture`, `config {path (redacted), source}`, `storage {output_root, work_root, history_db, ledger_db, log_dir}` (redacted:
`%LOCALAPPDATA%\…`, `<project>\…`), `ledger {database: present|not_created_yet}` (existence only), `dependencies`, `playwright {version, chromium}`, `assets`, `oauth_files {configuration, client_file,
cached_sign_in, outside_project}` (states only), `logging {active, code, format, rotation}`, `runner_readiness {local, production_ready: false, blocked, outstanding}`,
`live_google_verified: false`, `lock_ttl_seconds`. It needs a valid settings file (exit 3 otherwise), never starts the browser, signs in, contacts Google or writes anything.

### Local versus live verification
Everything above is **local**: it was verified with synthetic files, probes and a real local Chromium start. **Not verified live:** Google sign-in and API access, `clients list`/`validate --online`,
`setup auth`, a clean-machine installation, Task Scheduler execution, a real network drive, and report execution against production.

## Sub-stage plan
R8.6a CLI architecture and parsing (**done**) · R8.6b Google client factory, configurable credential paths, `clients`, `setup auth` (**done**) · R8.6c logging activation, preflight, `doctor`, full (**done**)
`system-info` · R8.6d runner integration, `report`, `status`, `recovery` (**done**) · R8.6e full regression and deployment-readiness verification (**done**) · R8.6f test and help-text corrections (**done**) · R8.7b `pyproject.toml`, `README.md` and documentation corrections (**done**). Each ends with a review stop.

## R8.6d — Report, status and recovery commands

All seven commands share the argument forms in the command table. Output is sanitized (no absolute user paths, credentials, tokens, e-mail addresses, spreadsheet ids or client contact data); `--json` prints one object.
Exit codes are the existing ones: 0 success · 1 internal · 2 usage · 3 invalid configuration · 4 preflight failure · 5 authentication required · 6 source unavailable · 7 report failed · 8 finalization failed ·
9 audit failed · 10 ledger unavailable · 11 lock conflict · 12 operator action required · 13 not implemented (no longer used by any command) · 130 interrupted. No new state or error category exists.

### Production gate
`report run`, `report batch`, `recovery acknowledge` and `recovery cancel` call `preflight.production_gate()` **first** — before the workbook is read, any Google client is built, the ledger is opened or any folder is created.
The gate is the local preflight (no blocked finding) **plus** two independent requirements: `live_google_not_verified`, which only a valid, genuine, unexpired live-verification attestation can clear (since R8.7c; **no real attestation exists yet**, nothing can create one from the command line, and an attestation never clears anything else), and `credentials_not_relocated`
(satisfied since R8.7d: the OAuth client and cached sign-in are configured at a location outside the project and cloud-synced folders and were relocated there; `doctor` checks the configured locations and file states, not ACLs or contents; the original copies are kept). A blocked gate prints the fixed blocker codes (`details.blockers`), exits with 4 (5 when the only blockers are
sign-in problems, 3 for configuration) and does nothing else. **Today the gate blocks every real production execution** (there is no attestation; Make, Drive and e-mail are not connected). There is no option, environment variable or file that bypasses it; the only way to pass a different
gate is the Python-only `gate=` argument of `cli.main`, used by tests with synthetic data. `production_ready: false` keeps its meaning.

### `report dry-run`
Planning only. Validates the client key and period, loads the settings and the client registry (an unregistered client → exit 3), runs the local preflight (the Chromium launch is not started: `browser_launch: not_tested`), and prints: the sources a real run would read (Sheets, Search Console, GA4 — read-only), the eight steps
(check the request against the workbook's client and period, take the locks, collect, assemble, render, finalize, run the mandatory audit gate, stop — delivery is not implemented), where it would write (redacted), the production blockers, and what was and was not performed. Its exit code is the local preflight's (0, 3, 4 or 5), not the production gate's. It makes **no** Google call and no sign-in, reads no workbook (the
workbook row is reported as `not_checked`), opens or creates no ledger and writes no file. It does not render a sample report.

### `report run`
Resolves the client and period (the workbook is read with the lazily built Google clients), builds `Services` with the Google collector, and calls the existing `pipeline.run_client`. Locking, idempotency, retry,
recovery, finalization and the mandatory audit gate are those of the pipeline. A re-run of an audited period reports `already_complete`; a `failed` run is retried by running the command again (the CLI sends the pipeline's `retry`
action; admission rules still decide); a `blocked` run needs `recovery acknowledge` first. Exit code from the response: audited → 0, auth → 5, source → 6, render → 7, finalization → 8, audit → 9, ledger → 10, lock → 11, operator needed → 12; Ctrl+C → 130 with locks released.

### `report batch`
Sequential, in client-key order, through `runner.run_batch`; the gate runs before any client. One client's failure never stops the others; authentication and ledger failures abort the batch (the rest are reported as not started).
Output: per-client key, state, outcome, audit and exit code; `rejected` (invalid workbook rows), `skipped`, `not_due_for_this_period`, `not_eligible` (`--client` keys not runnable), `processed`, `not_started`, `aborted`.
Aggregate exit code: the abort reason if aborted, otherwise the lowest-numbered client failure, upgraded to 3 when rejected or unknown clients exist; 0 only when everything ran cleanly.

### `status`
Read-only (the ledger is opened read-only; a missing ledger is reported as `not_created_yet` and is not created). Without options: the latest runs; `--client KEY`; `--run RUN_KEY|UUID`. Each run shows state, stage, error code,
attempt, generation, supersedes/superseded-by, `finalized`, `audited`, a 16-character PDF fingerprint, and `delivery: "not_tracked"` — delivery is never inferred. Unknown run → exit 3.

### `recovery inspect`
Read-only. For every blocked or failed run (optionally one client): what it needs (`operator_acknowledgement`, `condition_not_recoverable_by_retry`, retry by `report run`), available actions, why cancel would be refused,
whether partial files are present, reconciliation conflicts, and the client lock as safe facts only (`held`, `lease_expired`, `holder` alive/dead/unknown — never the owner id).

### `recovery acknowledge` / `recovery cancel`
Need `--production` (so the gate), an explicit `--run RUN_KEY`, `--operator LABEL` and a fixed `--reason` code. They address the **latest generation** only and use the pipeline's operator actions unchanged: optimistic
version checks, append-only events, idempotent replays (same request → `replayed`), `acknowledge` refused while the blocking condition (partial file, failed audit) remains or when the condition cannot be recovered by a retry,
`cancel` refused for runs with a finalized report. Nothing is ever deleted; acknowledging does not move the run (use `report run`). Exit 0 means the *operator action* was recorded, not that the run is resolved.

### Verification status
Everything in R8.6d was verified with **synthetic integration tests** (temporary ledgers, stub renderer, fake data, injected permissive gate, sockets forbidden). **No real report, no live Google call and no production
execution has happened.**

### R8.6f — corrections
`report dry-run --scratch-dir PATH` is **refused** with a usage error (exit code 2: "`--scratch-dir` is not supported: `report dry-run` only plans and renders nothing…"); the option is hidden from the help because dry-run has no scratch folder.
The `--help` text no longer carries sub-stage labels; the epilog describes exit code 13 as reserved (no current command returns it; numeric values are unchanged) and mentions the readiness gate.

### R8.7d.2 — `outstanding` and the relocation requirement
The `outstanding` list of `doctor --json` and `system-info` drops `credentials_not_relocated` when `preflight.credentials_relocated(settings, findings)` holds, which is the same function the production gate (`uncleared_requirements`) applies: the settings are available and no `credentials_inside_project`, `credentials_in_cloud_sync` or `credential_path_invalid` finding exists. **Both** OAuth locations (`paths.oauth_client_file` and `paths.oauth_cache_file`) are checked independently (R8.7d.3): either one inside the project, in a OneDrive or other cloud-synchronised folder, unconfigured (the in-project fallback applies) or invalid means "not relocated". The check looks at configured locations only: it does not inspect file contents or ACLs. Without settings relocation is never assumed. `live_google_not_verified` leaves the list only for a valid attestation; `make_not_connected`, `drive_not_connected` and `email_not_connected` always stay. `production_ready` remains `false`.

## R8.7e.1b — `verify live`
`seo-report verify live --client CLIENT_KEY --live --config PATH [--json]`. Fixed order, stopping at the first failure: configuration (both OAuth locations relocated, sign-in present) -> OAuth credential load with its outcome (`not_needed` or `ok`; a refresh rewrites the token file) -> reporting workbook read through the existing loader -> Search Console access (`sites.list` plus one totals query) -> GA4 access (property metadata plus one one-metric report) -> evidence built from those results -> attestation issued once. Exit codes are the existing ones (3 configuration or client problem, 4 credentials not relocated or attestation refused, 5 sign-in required, 6 a Google source unavailable or denied, 1 unexpected); JSON failures carry `details.stage` and `details.attestation_issued: false`. Output never contains tokens, property identifiers or the workbook ID. Run it from a normal PowerShell (not Claude Desktop). It has not been executed against Google; the live run is R8.7e.2.

## R8.7h.2 — `report preview`

`seo-report report preview --client CLIENT_KEY --period YYYY-MM-DD_YYYY-MM-DD --config <runner.yaml> [--json]`. It has **not** been run against real data: the live-data preview is R8.7h.3 and needs the operator's authorisation.

**What it does.** The same collection (Search Console, GA4, the workbook rows and keyword rankings), assembly and Chromium rendering as a production run, through the existing `pipeline.dry_run_client`, into a preview folder, with the label `PREVIEW — NOT FINAL` on every page. **It contacts Google (read-only) and starts Chromium when run.** It passes the same gate as `report run` (the read-only local preflight with the Chromium launch, and a valid verification attestation covering the client and its properties) and needs the exact period of the workbook (`clients list` shows it); a different period is exit code 3 (`period_differs_from_workbook`). It takes no `--production` or `--live` flag because it changes no production state.

**Where.** `%LOCALAPPDATA%\JasperSEOReporting\previews\<client_key>\<period>\preview-<UTC time>-<id>\`, containing `preview-manifest.json` (status `incomplete` while it runs, `preview_only` on success, `failed` or `interrupted` otherwise; client key, period, creation time, PDF hash and size, pages, validation result, data warnings; no property identifiers, tokens or contact data), `preview.pdf` (only after every check passed) and `render\` (the scratch folder of the dry run: its sidecar, the draft PDF and the Chromium work folder). A preview folder is never reused, overwritten or deleted.

**Output.** The notice `PREVIEW ONLY — NO PRODUCTION REPORT CREATED`, client, period, preview ID, PDF path, page count and the number of pages on which the label was measured, `Validation: passed`, the SHA-256, and any data warnings. JSON adds `status: preview_only`. Exit codes are the existing ones: 3 configuration, workbook or period problem; 4 gate (attestation, Chromium); 5 sign-in required; 6 a Google source failed; 7 rendering or layout validation failed; 10 preview storage failed; 130 interrupted. JSON failures carry `details.stage` and `details.production_report_created: false`.

**Review.** Open `preview.pdf`; every page shows the red label in its top margin; the manifest next to it says `preview_only`. A finalized report is `reports\<client>\<period>\report.pdf` under the production output root, with a reviewer record and a ledger run; a preview has none of these and cannot be promoted or finalized.

## R8.7h.4 — report layout (affects `report preview` and `report run`)

* **Google Search Console:** no daily table. The daily series is still collected and drawn in the "Clicks per day" and "Impressions per day" charts.
* **Geographical Performance:** the chart and the table show the same top 5 countries, ranked by impressions (descending), then clicks (descending), then country name. The caption states how many countries exist ("Top 5 countries by impressions (of N)"). The report data keeps every country.
