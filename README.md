# Jasper SEO Report Automation

A local, deterministic, ledger-backed runner that turns Google Search Console, Google Analytics 4 and a client workbook into an audited, finalized PDF SEO report for each client and 28-day reporting period.

> **Status: not production-ready.** The engine and the command-line runner are implemented and tested with synthetic data. **No live Google access has ever been verified, no real report has been produced, and the production gate deliberately blocks real reporting** (see [Limitations](#current-limitations-and-production-blockers)).

## What it does

```
reporting workbook (Sheets) ─┐
Search Console ──────────────┼─▶ collect ─▶ assemble ─▶ render (Chromium PDF) ─▶ finalize ─▶ audit ─▶ `audited`
GA4 ─────────────────────────┘        every step recorded in a local SQLite ledger (locks, retries, recovery)
```

* **Engine (R0–R7.5):** report assembly, insights, history comparison, PDF rendering, content-addressed finalization, reviewer records and a mandatory audit gate. Nothing is overwritten or deleted automatically.
* **Runner (R8.x):** a ledger (state machine, leases, idempotent requests, operator recovery), a pipeline per client, a sequential batch runner, structured sanitized logging, a deployment preflight and the `python -m seo_report` command line.
* A run **stops at `audited`**. Drive upload, approval, e-mail, Make.com and request intake are **not implemented** (design only: `docs/r8/`).

## Requirements

* Windows 11 (the only environment used so far) and **Python 3.14** (verified on 3.14.7 only).
* Chromium managed by Playwright, the DejaVu Sans font files and the logo (see below).
* Network access only for the Google APIs, and only when a real run is eventually authorised.

## Installation in a clean virtual environment

> **Not yet verified.** These steps are the intended procedure. No clean-machine install and no `pip install -e .` has been performed; the project's own environment is a development environment inside OneDrive. Treat the first execution of these steps as a verification task.

Use a folder **outside OneDrive** and outside the project (the runner refuses OneDrive-hosted storage for its databases and logs, and OneDrive can interfere with credentials and the virtual environment).

```powershell
py -3.14 -m venv C:\JasperSEO\venv
C:\JasperSEO\venv\Scripts\python -m pip install -r requirements-lock.txt   # exact tested versions (33 packages, no hashes)
C:\JasperSEO\venv\Scripts\python -m pip install -e . --no-deps             # run from the project folder
C:\JasperSEO\venv\Scripts\python -m playwright install chromium            # one-time download; tested build 156.0.8078.4 / Playwright 1.64.1
```

* `-e` (editable) is **required**: the code locates `templates/`, `config/`, `assets/` and `requirements.txt` relative to the checkout. A normal install into `site-packages` would not find them.
* The install builds with `setuptools` (declared in `pyproject.toml`), which pip downloads on first use.
* For the test tools add `python -m pip install -r requirements-dev-lock.txt` (pytest, matplotlib).
* After installing, either of these starts the CLI **without `PYTHONPATH`**: `seo-report --version` or `python -m seo_report --version`. (Until it is installed, `python -m seo_report` fails with *No module named seo_report* unless `PYTHONPATH=src` is set; that is how the test suite was run so far.)

## Fonts and logo

* **Logo:** `assets/jasper-logo.png` is in the project (override with `render.logo`).
* **Fonts:** two files are required, `DejaVuSans.ttf` and `DejaVuSans-Bold.ttf` (version 2.35). They are **not bundled**; copy them to a folder outside OneDrive (with the DejaVu licence file) and list them under `render.fonts` in the settings file. Expected SHA-256 values are in `docs/r8/dependency_and_asset_readiness.md`; `doctor` checks them.

## Configuration

1. Copy `config/runner.yaml.example` to a private location **outside the project and OneDrive** and edit it: storage folders (defaults are under `%LOCALAPPDATA%\JasperSEOReporting`), `render.fonts`, `workbooks.reporting_spreadsheet_id` and, later, `paths.oauth_client_file` / `paths.oauth_cache_file`. The loader refuses anything that looks like a credential.
2. `config/clients.yaml` (stable `client_key` per client) and `config/jasper.yaml` (default sender) are the client registry and sender settings.
3. **A settings file is never defaulted.** Name it on every command (`--config PATH`) or set `JASPER_SEO_CONFIG`.

## Safe command-line usage

Everything below is read-only or plan-only. All commands accept `--json` and `--config PATH`.

```powershell
seo-report --help
seo-report --version
seo-report --config C:\JasperSEO\runner.yaml system-info            # versions, redacted locations, readiness summary (no network)
seo-report --config C:\JasperSEO\runner.yaml doctor                 # local preflight: runtime, dependencies, Chromium, fonts/logo, storage, auth metadata, logging
seo-report --config C:\JasperSEO\runner.yaml doctor --skip-browser-launch
seo-report --config C:\JasperSEO\runner.yaml report dry-run --client example-co --period 2026-09-29_2026-10-26   # plan only: renders nothing, no Google, no ledger
seo-report --config C:\JasperSEO\runner.yaml status                 # read-only ledger view (does not create a ledger)
seo-report --config C:\JasperSEO\runner.yaml recovery inspect       # read-only: what blocked/failed runs need
```

* `doctor` and `system-info` never contact Google, never open credential files (file metadata only) and report `production_ready: false` while anything is outstanding.
* **`report preview` is not in the list above on purpose:** `seo-report report preview --client CLIENT_KEY --period START_END` contacts Google (read-only) and starts Chromium to render a real-data report marked `PREVIEW — NOT FINAL` into its own folder under `%LOCALAPPDATA%\JasperSEOReporting\previews\`. It creates no production report, ledger run or history entry. Implemented and tested offline; **not yet run on real data** (R8.7h.3).
* **`verify live` is not in the list above on purpose:** `seo-report verify live --client CLIENT_KEY --live` contacts Google (read-only OAuth refresh, workbook, Search Console, GA4), may rewrite the cached sign-in, and stores the verification attestation when every check succeeds. It is implemented and tested offline but **has not been run** (R8.7e.2 is pending approval); run it only from a normal PowerShell.
* **Not in the list above on purpose:** `clients list` and `clients validate --online` read the reporting workbook through Google Sheets (read-only), so they need a working sign-in and are **unverified**; `clients validate` without `--online` checks local configuration only.
* Commands that change production state need `--production`: `report run`, `report batch`, `recovery acknowledge`, `recovery cancel`. **They are currently refused by the production gate (exit code 4)**; there is no option or environment variable that bypasses it.
* Exit codes are stable (`--help` lists them): 0 ok · 2 usage · 3 configuration · 4 preflight/gate · 5 sign-in required · 6 source unavailable · 7 report failed · 8 finalization failed · 9 audit failed · 10 ledger · 11 lock · 12 operator action · 130 interrupted.

## Authentication (no credentials are stored in this repository)

The runner uses an OAuth client file and a cached sign-in file with **read-only** scopes (Sheets, Search Console, GA4). Normal runs are non-interactive and never open a browser.

* `seo-report --config <settings> setup auth` is the **only** command that starts the browser sign-in, and only at an attended console. **Not yet performed.**
* **Relocated (R8.7d):** the two files were copied to `%LOCALAPPDATA%\JasperSEOReporting\credentials\`, outside the project and OneDrive, with a restricted ACL (current user, SYSTEM, Administrators) and matching SHA-256 hashes checked by the operator, and an external `runner.yaml` points at them. The original copies in the project (OneDrive) are still there and not yet retired (`docs/r8/credential_relocation_procedure.md` §9). `doctor` reports the configured locations and file states, not the ACL. Never paste, e-mail or commit credentials; `credentials/`, `token*.json` and `client_secret*.json` are git-ignored.

## Verified and unverified

| Verified (synthetic data, local components) | Not verified (needs a future, authorised stage) |
|---|---|
| Full test suite (5,193 tests: 5,180 passed, 13 skipped at R8.6f) run with `PYTHONPATH=src` | `pip install -e .`, the `seo-report` command, a clean-machine install |
| CLI commands, exit codes, production gate, read-only commands (temporary folders, real local Chromium rendering of synthetic data) | Google sign-in, token refresh, any live Google read |
| Packaging metadata (consistency with `requirements.txt`, entry point resolves) | A real client report, a real ledger, Task Scheduler, backups |

## Current limitations and production blockers

* **Production gate closed by design:** `live_google_not_verified` can be cleared only by a valid, genuine, unexpired verification attestation (mechanism implemented in R8.7c; **no real attestation exists**, no command or setting can create one, and the live verification that will issue it is a future stage, R8.7e). `credentials_not_relocated` is an independent requirement that is **satisfied** since R8.7d (it is reported as outstanding only while the configured credential locations are in the project or a cloud-synchronised folder); Make.com, Drive and e-mail are not connected and there is no attestation, so real reporting stays blocked.
* Live Google access has **not** been verified, and the original credential copies are not yet retired; the OAuth consent-screen status is unknown (a "Testing" status expires refresh tokens after 7 days).
* **Not implemented:** request intake from an operational workbook, status sender, Make.com scenarios, Google Drive upload, approval, e-mail delivery, Windows scheduled task, backups, monitoring and alerts.
* No `supersede`, `poll` or `publish` CLI command (supersede exists in the library only).

More detail: `docs/r8/as_built_status.md`, `docs/r8/deployment_readiness.md`, `docs/r8/r86_cli_reference.md`, `docs/r8/r86_final_verification.md`.

## Tests

```powershell
$env:PYTHONPATH = "src"      # not needed after an editable install
python -m pytest tests -q
```

The tests use synthetic data, temporary folders and fakes; they make no Google, Make, Drive or e-mail calls.
