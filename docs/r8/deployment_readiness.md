# Deployment readiness checklist (written at R8.4.6; updated through R8.7b)

> **Current summary (R8.7b).** Local CLI and runner: implemented, tested with synthetic data. **Done (R8.7d, operator, 2026-11-02):** credential relocation to `%LOCALAPPDATA%\JasperSEOReporting\credentials\` (hashes and ACL verified, external `runner.yaml` updated; the original copies are kept and not retired). **Not verified:** live Google access, package installation (`pip install -e .`), a clean-machine install. **Blocked by design:** real reporting (the production gate refuses `report run|batch` and state-changing recovery until its attestation and live-verification stages are done). **Not implemented:** request intake, status sender, Make.com, Drive, approval, e-mail, scheduled task, backups, monitoring. Nothing here is production-ready.

Nothing was deployed. Classification of each item, as of R8.4.6, for **controlled local deployment on a Windows 11 machine**:

* **Ready** — implemented, tested offline, and nothing further is needed for this item on the development machine.
* **Needs configuration** — the code supports it; a person must set it up on the deployment machine.
* **Needs implementation** — code or tooling is missing.
* **Unverified** — believed to work; not demonstrated.
* **Blocked** — cannot proceed until an explicit approval or a prerequisite item is done.

"Ready" never means "tested against Google, Make.com, Drive or a clean machine".

| # | Item | Status | Detail / evidence | Next step |
|---|---|---|---|---|
| 1 | Python runtime | **Ready** (dev) / **Unverified** (target) | 3.14.7 on Windows 11 build 26300; the suite passes here | Install the same Python on the target; run the suite there |
| 2 | Dependency installation | **Needs configuration**, **Unverified** | `requirements.txt` (13 pins), `requirements-lock.txt` (33), generated from installed metadata, verified by tests; no clean install done; no hashes | Create a venv **outside OneDrive**, `pip install -r requirements-lock.txt`, run `scripts/dependency_report.py` and the suite |
| 3 | Chromium installation | **Needs configuration** | `python -m playwright install chromium` (network, once); tested build 156.0.8078.4 with Playwright 1.64.1; a build change blocks retries (`render_not_reproducible`) | Install on the target; freeze the Playwright version during a reporting month |
| 4 | Font provisioning | **Needs configuration** | Two files required (`DejaVuSans.ttf`, `DejaVuSans-Bold.ttf`, v2.35) with SHA-256 values in `dependency_and_asset_readiness.md`; listed in `render.fonts`; not bundled in the project | Copy to a folder outside OneDrive with `LICENSE_DEJAVU`; set `render.fonts` |
| 5 | Logo provisioning | **Ready** | `assets/jasper-logo.png` is in the project; hash recorded | Keep unchanged; if the project moves, set `render.logo` |
| 6 | Asset preflight inside the runner | **Ready** (as `doctor` and as part of the production gate) | R8.6c: `python -m seo_report doctor` checks logo, both fonts, hashes and fingerprints before any run (blocked codes `asset_*`, `fonts_not_configured`, `required_font_missing`) and `preflight.production_gate()` (the local checks plus two requirements that cannot be cleared yet) is called first by `report run|batch` and state-changing recovery since R8.6d. The pipeline itself still finds a missing asset only at the render stage | Run `doctor` before each run |
| 7 | Storage directories (output, work, logs) | **Needs configuration** | Defaults under `%LOCALAPPDATA%\JasperSEOReporting`; created by the first run; canonical overlap/OneDrive/project checks enforced | Choose paths, put them in `runner.yaml`, run the preflight |
| 8 | Database directories (ledger, history) | **Needs configuration** | Must be on a local fixed drive; UNC/mapped/unknown drives refused (tested with fakes; real mapped drive not tested) | Same as 7; do not use a network or removable drive |
| 9 | Credential relocation | **Done (R8.7d)** — originals not yet retired | Since R8.6b the locations are configurable (`paths.oauth_client_file`, `paths.oauth_cache_file`). On 2026-11-02 the operator copied both files to `%LOCALAPPDATA%\JasperSEOReporting\credentials\` (outside the project and OneDrive), verified the restricted ACL and the source/destination hashes, and pointed an external `runner.yaml` at them; `doctor` shows no `credentials_inside_project` or `credentials_in_cloud_sync` warning and no longer lists `credentials_not_relocated` as outstanding (both OAuth locations are checked independently since R8.7d.3). `doctor` reports configured locations and file states, not ACLs. The OneDrive copies remain and the old token is not revoked (`credential_relocation_procedure.md` §9, pending) | Confirm a first successful use, then retire the old copies as in §9 (a separate, explicitly authorised step) |
| 10 | Non-interactive authentication | **Ready** (code) / **Unverified** (live) | `get_credentials()` never opens a browser; structured `auth_*` codes; tested with synthetic tokens only | One attended `auth setup` as the runner account; one live read-only check |
| 11 | OAuth consent-screen status | **Unverified** | If the consent screen is in "Testing", refresh tokens expire after 7 days (a Google Cloud setting, not changed) | A person checks the setting in the Google Cloud console |
| 12 | Production Google client factory | **Ready** (code, fakes) / **Unverified** (live) | R8.6b/d: `google_clients.GoogleClientFactory` and `make_collect` build the Sheets, Search Console and GA4 clients lazily from `auth.get_credentials`, and `report run|batch` use them; tested only with fakes; never run against Google | Live read-only verification at an attended session (a future, approved stage) |
| 13 | Windows account permissions | **Needs configuration** | Runner account needs: read/write on all storage folders and the credentials folder (create/rename for atomic token writes), read on fonts/logo/Playwright folder; ACL owner-only on credentials | Create/choose the account; apply `icacls` |
| 14 | Command-line entry point / Scheduled Task | CLI **Ready** (synthetic) / Scheduled Task **Blocked** | `python -m seo_report` exists (R8.6a–c: `system-info`, `doctor`, `clients`, `setup auth`); `report dry-run|run|batch`, `status` and `recovery` exist since R8.6d (synthetic integration tests only; the production gate still blocks real execution); request intake (`Report Requests` tab) is not implemented | Live verification and credential relocation (to clear the gate), then R8.7 intake, before any Task Scheduler setup |
| 15 | Scheduled Task prerequisites | **Blocked** by 14 | Run-as the credentials account; "Start in" outside OneDrive; battery/idle conditions; no desktop needed | Configure after 14 |
| 16 | Logging | **Ready** (local) / **Unverified** (long-running use) | R8.6c: activated once per CLI command (not for `--help`/`--version`); sanitized JSON lines, rotation (≤30 MB), run identifiers; `doctor` reports logging blocked if the folder is unusable. Not exercised over a full monthly cycle | Review the first real month's logs |
| 17 | Backups | **Needs implementation**, **Unverified** | No backup job exists. The ledger and history SQLite files (and the output folder) should be copied while no run is active (rollback-journal mode: copy when the lock is not held). Design in `ledger_schema.md` §8 | Define a nightly copy to a non-OneDrive disk; test a restore |
| 18 | Recovery | **Ready** (CLI, synthetic) / **Needs implementation** (`supersede` command) | Retry (by running `report run` again), acknowledge, cancel, stale-lease takeover and interrupted-run recovery are available through the CLI since R8.6d (`recovery inspect|acknowledge|cancel`); `supersede` exists in the library only; state-changing recovery is also behind the production gate | Expose `supersede` in a later stage |
| 19 | Disk-space requirements | **Unverified** | Logs ≤ 30 MB; each report keeps the PDF, superseded copies, sidecars, records and a history snapshot; sizes per month were not measured; a full disk stops the batch (`disk_full`) | Measure one month's output; set a free-space alert |
| 20 | Monitoring and alerts | **Needs implementation** | Statuses are written to the outbox but nothing sends them; the batch summary exists as an object; no alert on `auth_required`, `disk_full`, failed or blocked runs | Implement the outbox sender (R8.5/R8.6) or a simple e-mail/log alert |
| 21 | Client configuration | **Needs configuration** | Registry in `config/clients.yaml` (stable client keys) + `Clients`/`Keywords` tabs; rejected clients get fixed codes and never stop others | Review the registry against the workbook |
| 22 | Reporting-period configuration | **Ready** (code) / **Needs configuration** (monthly) | Explicit `Report Start/End Date`, exactly 28 days, must equal the request's `period_id` | Fill the dates in the workbook per run |
| 23 | Delivery (Drive, approval, e-mail) | **Needs implementation** | Not started; `publish` is refused; state machine stops at `audited` | Out of scope until R8.5+ |
| 24 | Make.com | **Needs implementation**, **Blocked** | Awaiting explicit approval to start the Make.com design stage | — |
| 25 | Clean-machine installation | **Unverified** | Never performed | Perform once in an isolated environment with explicit authorisation |

## R8.6e verification
The local CLI and runner were re-verified in R8.6e; the per-area classification (core engine, local CLI/runner, live Google, unattended production, end-to-end automation are reported separately) is in [r86_final_verification.md](r86_final_verification.md). Nothing here claims production readiness: the production gate is closed by design and live Google access is unverified (the credentials were relocated in R8.7d).

## R8.7b — launch and packaging
| Item | Status | Detail |
|---|---|---|
| Launch without `PYTHONPATH` | **Unverified** | `python -m seo_report` fails with *No module named seo_report* unless `PYTHONPATH=src` is set. `pyproject.toml` (src layout, the 13 existing pins, console script `seo-report`) and `README.md` were added in R8.7b; consistency with `requirements*.txt` is tested (`tests/test_packaging.py`), but no environment was created and nothing was installed |
| Install mode | **Needs configuration** | Editable install only (`pip install -e . --no-deps`): the code finds `templates/`, `config/`, `assets/` and `requirements.txt` relative to the checkout |
| Virtual environment location | **Needs configuration** | The development `.venv` is inside the OneDrive project folder; the intended one is outside OneDrive (README) |

## R8.7c — production-gate attestation
| Item | Status | Detail |
|---|---|---|
| Mechanism that clears `live_google_not_verified` | **Ready** (code, synthetic tests) | DPAPI-protected, machine- and account-bound, 7-day attestation evaluated by the gate; see `security_and_authentication.md` §12 |
| An actual attestation | **Does not exist — Blocked** | needs the future live verification (R8.7e), which needs the credential relocation (R8.7d) first. Nothing can create one today; the gate therefore still blocks all real reporting |
| `credentials_not_relocated` | **Satisfied since R8.7d** | independent of the attestation (an attestation never clears it); cleared by the relocated, configured credential paths and reported with the same condition as the gate |
| Live Google access | **Unverified** | never run |
| Re-verification cadence | **Needs configuration** | an attestation lasts 7 days; the operating routine for renewing it is not defined |


## R8.7e.1b — live verification workflow
| Item | Status | Detail |
|---|---|---|
| `seo-report verify live` (module `live_verification`) | **Implemented and tested offline; not executed** | four read-only checks (OAuth refresh, workbook, Search Console, GA4), then the attestation; `security_and_authentication.md` §13 |
| Live run (R8.7e.2) | **Pending — needs approval** | contacts Google, may rewrite `token.json`, stores the attestation; run from a normal PowerShell |
| Attestation | **Does not exist** | issued only by a successful live run |
| Google API enablement, granted scopes, account access | **Unverified** | cannot be checked without the live run |


## R8.7h.2 — report preview
| Item | Status | Detail |
|---|---|---|
| `report preview` (real-data preview with a visible label, in its own folder) | **Implemented and tested offline; not run on real data** | `r86_cli_reference.md`, `security_and_authentication.md` section 14 |
| A preview of Example Client with real Search Console / GA4 data | **Pending — R8.7h.3, needs the operator's authorisation** | contacts Google read-only, starts Chromium, writes only under `previews\` |
| Production isolation of the preview | **Verified by tests** (tripwires on ledger, history writes, finalization, locks; production folders unchanged) | |
| Replacing a finalized report from the CLI (`supersede`) | **Still not exposed** | review a preview before the production run |
