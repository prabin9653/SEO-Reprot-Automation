# R8.6e — Final verification of the CLI and local runner (R8.6a–d)

Date: 2026-11-01. Basis: inspection of the current source and tests, test execution, and isolated runs of the real `python -m seo_report` against a **temporary** configuration (temporary
ledger/output/work folders under the system temp folder, placeholder OAuth files containing `{}`, placeholder spreadsheet id). **Nothing live was exercised**: no Google call, no sign-in,
no credential file opened, no production report, PDF or ledger write, no Make.com/Drive/e-mail. No application source file and no test was changed in this stage (documentation only).

## 1. Test commands and results

Environment: Windows 11, Python 3.14.7 (`.venv`). All commands: `$env:PYTHONPATH="src"; .\.venv\Scripts\python.exe -m pytest <targets> -q -p no:cacheprovider`.

| Group | Targets | Result |
|---|---|---|
| R8.6 targeted | `test_cli`, `test_r86b_credentials_and_clients`, `test_r86c_logging_and_preflight`, `test_r86d_report_status_recovery` | **313 passed** |
| CLI / runner integration | `test_runner`, `test_pipeline`, `test_client_loader`, `test_source_collector`, `test_auth_noninteractive`, `test_source_error_classification`, `test_operational_logging`, `test_deployment_readiness` | **390 passed** |
| Ledger and recovery | `test_ledger`, `test_ledger_v2`, `test_ledger_errors`, `test_operator_recovery`, `test_operator_contracts`, `test_supersede`, `test_contracts` | **281 passed** |
| R8.4 reliability | `test_lock_reliability`, `test_render_timeouts`, `test_path_safety`, `test_ledger_errors` | **192 passed, 3 skipped** |
| Full suite (`tests`), R8.6d run 1 | | 5174 passed, 13 skipped, **1 failed** (timing test below) |
| Full suite, R8.6d run 2 | | **5175 passed, 13 skipped, 0 failed** |
| Full suite, R8.6e | | 5174 passed, 13 skipped, **1 failed** (same timing test) |

So the suite is **not reliably green**: of three complete runs, two failed on the same test and one passed. The 13 skips are the unprivileged-symlink/platform tests documented earlier.

### Timing-sensitive test — **unresolved**
`tests/test_lock_reliability.py::test_a_lease_is_renewed_during_a_stage_longer_than_the_lease` (line 266).

* Failing assertion: `held.expires_at > timestamp_of(real_clock())`, e.g. `'2026-11-01T15:39:02+00:00' > '2026-11-01T15:39:02+00:00'` (equal, so false).
* Evidence it is **not caused by load alone**: run in isolation 15 times on an idle machine it failed **3 times**; with 16 busy-loop processes on 8 cores it failed 1 of 10 times; it passed inside the
  four-group runs above. Failures appear in every condition, at a rate of roughly 10–20 %.
* Cause (from the numbers): the test uses a one-second lease renewed every 0.1 s, and the ledger stores timestamps truncated to whole seconds (documented limitation: "lease times have one-second
  resolution"). `expires_at = trunc(last_renewal + 1 s)`; if the last renewal happened shortly before a second boundary and the check happens just after it, the truncated expiry equals the truncated
  "now" and the strict `>` fails although the lease was renewed correctly (`heartbeat_at` advanced; the lock was held; the second owner was still refused in a passing sibling assertion).
  This is a **flaw in the test's assertion** (strict comparison on a one-second grid), not evidence of a production lock defect: production leases are 600 s.
* Not changed: neither the test nor any lock behaviour was modified (not authorised in this stage). Suggested follow-up for approval: compare `>=`, or use a lease of 3 s, or compare against `now - 1 s`.
  Until then a full-suite failure of only this test should be re-run in isolation and judged by this analysis, not ignored.

## 2. CLI completeness (real `python -m seo_report`, isolated configuration)

| Command | Check | Result |
|---|---|---|
| `--help`, `--version`, every sub-command `--help` | exit | 0 |
| `--help` / `--version` / `report --help` | Google, Playwright and HTTP libraries imported | **none** (verified in a clean interpreter) |
| `system-info`, `system-info --json` | exit, redaction | 0; no absolute user path, spreadsheet id or e-mail in output |
| `doctor`, `doctor --json` (both with `--skip-browser-launch`; the Chromium launch probe is covered by tests) | exit, findings | 0; only `unverified` findings (`live_google_not_verified`, `workbook_rows_not_checked`, `chromium_launch_not_tested`); `production_ready: false` |
| `clients list` | placeholder sign-in file `{}` | 5 (auth required) with no network |
| `report dry-run` (text, `--json`) | exit, side effects | 0; temporary tree byte-identical before/after |
| `status`, `status --json`, `--client`, `--run` | exit, side effects | 0, 0, 0, 3 (unknown run); no ledger created; tree identical |
| `recovery inspect` (text, `--json`) | exit, side effects | 0; no ledger created; tree identical |
| `report run`, `report batch`, `recovery acknowledge`, `recovery cancel` **with** `--production` | exit | **4** (gate blocks); no ledger, folder or file created |
| the same four **without** `--production` | exit | **2** (usage) |
| `report run --force`, unknown command, no configuration at all | exit | 2, 2, 3 (`config_not_given`; no default location) |

Argument validation, handler routing and exit-code mapping are additionally covered by 313 R8.6 tests (including every failure class: auth 5, source 6, render 7, finalization 8, audit 9, ledger 10,
lock 11, operator 12, interrupt 130). The only change to the temporary tree in all of these runs was the log folder created by logging activation on the first command (see §6).

## 3. Production gate findings

* `--production` is enforced by the parser (`validate_args`, `PRODUCTION_COMMANDS`) for `report run`, `report batch`, `recovery acknowledge`, `recovery cancel`; there is no such flag on `dry-run`, `status` or `recovery inspect`.
* `cli_report.require_gate` is the single call site of `preflight.production_gate`. In `handle_run`, `handle_batch` and `_operator_action` it is called **before** the workbook read, Google client construction,
  any Google request, ledger open, report generation or recovery state change (verified by reading the source and by a test that checks the order in the source text).
* **No bypass:** the gate has no option, environment variable or file. The environment variables the code reads are `JASPER_SEO_CONFIG` (names the settings file only), `LOCALAPPDATA`, and
  `PLAYWRIGHT_BROWSERS_PATH` (browser location for the preflight); the injected `gate`/`services_options`/`handlers`/`client_factory` exist only as Python arguments of `cli.main`, and `__main__` calls `main()`
  with no injected arguments. Tests set bypass-looking variables (`JASPER_SKIP_GATE`, …) and show no effect. No default-to-production: a missing configuration is exit 3.
* **The gate blocks all real production execution today** by design: `live_google_not_verified` and `credentials_not_relocated` have no clearing mechanism in code. `production_ready: false` is unchanged.

## 4. Read-only verification
Demonstrated by filesystem snapshots (path + SHA-256 of every file) of an isolated tree around each real command (§2) and by in-process tests: `dry-run`, `status` and `recovery inspect` write nothing, open the
ledger read-only (or not at all), do not create a missing ledger, change no lock, delete no file (a stray partial file survives inspection), and never contact Google. `status` shows delivery only as `not_tracked`.
`recovery acknowledge|cancel` never delete anything, including finalized reports (tests assert unchanged output trees); `cli.py`/`cli_report.py`/`google_clients.py` contain no delete/move calls.
The only deletion in the CLI-side code is `preflight` removing the scratch folder it created itself for the Chromium launch probe.

## 5. Google client and authentication safety
* Clients are built lazily (`GoogleClientFactory`; credentials are loaded on first use; a test fails if credentials are loaded at build time). Help and version import no Google libraries.
* The only interactive code is `auth.run_setup` (imports `InstalledAppFlow` itself); only `setup auth` reaches it, and it refuses without an attended console. The test suite forbids sockets and the sign-in flow during all report/status/recovery tests.
* `SCOPES` are unchanged: `spreadsheets.readonly`, `webmasters.readonly`, `analytics.readonly`. Credential paths come from the `oauth_client_file`/`oauth_cache_file` settings with the compatible in-project fallback.
* Source errors keep their classifications (auth → 5, source denied/unavailable → 6). Fake-client injection works. Logs and output never contain credential contents (sanitizer tests; leak scan of every real run above).
* Credential files were only `stat`ed (size and modification time) before and after; they are unchanged.

## 6. Logging and preflight
Logging activates once per normal command (not for help, version or usage errors); handler failures are guarded and do not interrupt a command; output is sanitized JSON lines; client/run context propagates
(tested); rotation is 5 MB × 5. **Note:** the first command creates the log folder `%LOCALAPPDATA%\JasperSEOReporting\logs` (that is the purpose of logging activation); `doctor`'s other checks create nothing.
`doctor` covers runtime, dependencies, Playwright/Chromium, logo and fonts with fingerprints, storage, authentication metadata, client configuration and logging; no Google call, no credential contents;
production readiness stays false with outstanding codes `live_google_not_verified`, `credentials_not_relocated`, `make_not_connected`, `drive_not_connected`, `email_not_connected`.

## 7. Pipeline, ledger and recovery integration
`report run` → `pipeline.run_client` through `Services` (the `make_collect(factory)` collector); `report batch` → `runner.run_batch` (sequential; client failures isolated; auth and ledger failures abort and leave
the rest "not started"; a held client lock is reported as a lock conflict). Idempotency (replayed requests), lease/lock release (also on Ctrl+C), generation superseding, the finalization and audit gates, optimistic
version checks and append-only events are the existing implementations, exercised through the CLI in synthetic integration tests (stub renderer, fake sources, temporary ledgers). The protected R0–R7.5 modules
are unchanged since R8.6c. **These are synthetic integration tests, not production execution.**

## 8. Defects and inaccuracies found (reported, not fixed — application source is out of scope for R8.6e)
1. **`report dry-run --scratch-dir PATH` is accepted and silently ignored**, and the parser help for `dry-run` still says "collect, assemble and render into a scratch folder". The command is plan-only. (cosmetic/misleading; fix: remove the option and correct the help.)
2. **Stale placeholder text in `cli.py`:** sub-command help labels "(R8.6b)" on `clients` and `setup`; the epilog "Commands marked (R8.6b/c/d) are parsed and validated but not available until that sub-stage"; exit code 13
   described as "not available in this build yet" (no command uses it any more); the module docstring still speaks of deferred commands. (cosmetic.)
3. **Timing test race** described in §1 (test defect; unresolved).
`docs/r8/r86_cli_reference.md` already describes the actual behaviour (plan-only dry-run, no `--scratch-dir`), so the documents and the `--help` output currently disagree on these points.

## 9. Deployment readiness (five separate questions — there is deliberately no combined percentage)

| Area | Status |
|---|---|
| 1. Core reporting engine (R0–R7.5: assembly, rendering, finalization, audit) | **Ready** for controlled use; verified offline with real Chromium rendering of synthetic data; not verified with real client data |
| 2. Local CLI and runner (R8.6) | **Ready** locally (implemented and tested with synthetic data); three cosmetic/test defects above |
| 3. Live Google integration | **Unverified** — never run; blocked by credential relocation and a first attended verification |
| 4. Unattended production | **Blocked** — gate closed; no scheduler, backups, monitoring, status delivery |
| 5. End-to-end automated reporting | **Needs implementation** — no request intake, polling, Make.com, Drive, e-mail |

| Item | Classification |
|---|---|
| Local CLI | Ready |
| Python environment (3.14.7, lock files) | Ready (clean-machine install **Unverified**) |
| Dependency reproducibility | Needs configuration (version-pinned locks exist; no hash pinning, no clean-machine rebuild) |
| Chromium | Ready on this machine (local launch verified); other machines Unverified |
| Fonts and logo | Ready (fingerprints checked) |
| Storage paths | Needs configuration (a real settings file outside the project/OneDrive must be created and chosen) |
| Logging | Ready |
| Credential path configuration | Ready (code); credentials themselves still in the project folder |
| Credential relocation | **Blocked** (needs the operator's attended action) |
| Google authentication | Unverified |
| Google API verification | Unverified |
| Client configuration | Needs configuration (registry validated locally; workbook rows `not_checked` online) |
| Reporting execution | Ready with synthetic data; **Unverified** with real data |
| Production gate | Ready (and deliberately closed) |
| Ledger and recovery | Ready (synthetic); backups **Needs implementation** |
| Backups | Needs implementation |
| Monitoring / alerts | Needs implementation |
| Windows Task Scheduler | Blocked (gate; not created) |
| Make.com integration | Needs implementation (design only) |
| Drive integration | Needs implementation |
| E-mail delivery | Needs implementation |

## 10. Outstanding deployment blockers
Live Google verification; credential relocation by the operator; the OAuth consent-screen status (7-day refresh-token expiry in "Testing") unknown; no request intake or workbook polling; no status
webhook sender; no Make.com scenarios; no Drive upload; no e-mail delivery; no Scheduled Task; no backups or monitoring; no clean-machine install.

## 11. R8.7 prerequisites (Make.com integration, per `r85_implementation_roadmap.md`)
Approval to start R8.7; decision on the three defects in §8 (a one-line test change and small help-text corrections); a stable full-suite result; for any later live use: relocated credentials and a successful
attended `setup auth` + `clients validate --online`; a test spreadsheet, test Drive folder and test mailbox; the owner's decision on U1/U2/U12 as recorded in the R8.5 documents. Nothing in R8.7 requires the gate to be opened.

## 12. Explicit limitations
No live Google behaviour is verified. Synthetic tests use a stub renderer in the integration path (real Chromium rendering is covered by the engine's own tests). Delivery is not tracked. `report dry-run` does not
render. The gate cannot be opened by any code path today. Lease times have one-second resolution. Only the rendering stage timeout is enforced. Log redaction is pattern-based. Windows-only behaviours were tested on one machine.

## 13. Resolution (R8.6f)
Items 1–3 of §8 were corrected in R8.6f: the lease-renewal test now compares on the one-second grid (`expires_at >= now`, and renewal proved by `expires_at`/`heartbeat_at` strictly beyond the original lease) with a
deterministic same-second regression test; `--scratch-dir` is refused with an explanation; the stale help labels and the exit-code 13 description in the help were corrected. The shared exit-code description in `cli_exit.py` was left unchanged.
