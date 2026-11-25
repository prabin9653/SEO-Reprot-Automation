# Jasper SEO Report Automation

A local, deterministic, ledger-backed tool that turns Google Search Console, Google Analytics 4 and a client workbook into an audited, finalized PDF SEO report for each client and 28-day reporting period. Each team member runs it **on their own Windows computer**, from a small local web page or from the command line.

> **Status: in use.** Reports for about twenty clients have been produced with live Google data. A run stops at `audited`: Drive upload, approval, e-mail and Make.com delivery are **not implemented** (design only: `docs/r8/`). Reports, run history and the Google sign-in stay on the computer that made them.

## What it does

```
reporting workbook (Sheets) ─┐
Search Console ──────────────┼─▶ collect ─▶ assemble ─▶ render (Chromium PDF) ─▶ finalize ─▶ audit ─▶ `audited`
GA4 ─────────────────────────┘        every step recorded in a local SQLite ledger (locks, retries, recovery)
```

* **Engine:** report assembly, insights, history comparison, PDF rendering, content-addressed finalization, reviewer records and a mandatory audit gate. Nothing is overwritten or deleted automatically; a finished report is never replaced.
* **Runner:** a ledger (state machine, leases, idempotent requests, operator recovery), a pipeline per client, a sequential batch runner, structured sanitized logging, a deployment preflight (`doctor`) and the `python -m seo_report` command line.
* **Operator experience:** a local web page (`seo web`), `clients check` / `clients add`, `report open`, plain-language "what it means / what to do" text on every error, per-device setup (`setup device`), who-ran-what attribution, and an optional shared team log that warns when a report already exists on another computer.

## For team members: start here

| I want to... | Read |
|---|---|
| set up my computer for the first time | [docs/team_setup.md](docs/team_setup.md) |
| create reports day to day | [docs/cheat_sheet.md](docs/cheat_sheet.md) and [docs/operator_quickstart.md](docs/operator_quickstart.md) |
| share finished reports with teammates (Google Drive folder and read-only dashboard) | [docs/shared_reports.md](docs/shared_reports.md) |
| look up a command, exit code or check | [docs/r8/r86_cli_reference.md](docs/r8/r86_cli_reference.md) |

The short version, from the project folder in a normal PowerShell window:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\install_device.ps1   # once: private Python environment, tested packages, PDF browser
.\seo.cmd setup device --spreadsheet-id "<workbook address>"  # once: folders, fonts, settings; lists what is still missing
.\seo.cmd setup auth                                          # once: sign in with the team's Google account
.\seo.cmd web                                                 # every day: the local page (this computer only)
```

`seo.cmd` finds Python and the source by itself and reads the settings from `%LOCALAPPDATA%\JasperSEOReporting\config\runner.yaml`. A "new" report means a new 28-day period in the sheet: a finished report for the same client and period can never be recreated.

## Requirements

* Windows 11 (the only environment used) and **Python 3.14** (verified on 3.14.7 only).
* Chromium managed by Playwright, the DejaVu Sans font files and the logo. The two fonts and their licence are bundled in `assets/fonts` and installed by `setup device`; the logo is `assets/jasper-logo.png`.
* Network access only for the Google APIs.

## Advanced: manual installation

`scripts/install_device.ps1` does this for you. By hand, in a folder **outside OneDrive** (the runner refuses OneDrive-hosted storage for its databases and logs):

```powershell
py -3.14 -m venv C:\JasperSEO\venv
C:\JasperSEO\venv\Scripts\python -m pip install -r requirements-lock.txt   # exact tested versions (no hashes)
C:\JasperSEO\venv\Scripts\python -m pip install -e . --no-deps             # optional: makes `seo-report` available
C:\JasperSEO\venv\Scripts\python -m playwright install chromium            # one-time download; tested build 156.0.8078.4 / Playwright 1.64.1
```

* Run from the project checkout. The code locates `templates/`, `config/`, `assets/` and `requirements.txt` relative to it, so a normal (non-editable) install into `site-packages` would not find them: use either an editable install (`-e` (editable), as above) or `PYTHONPATH=src` (what `seo.cmd` does).
* For the test tools add `python -m pip install -r requirements-dev-lock.txt` (pytest, matplotlib).
* `seo-report --version` or `python -m seo_report --version` starts the CLI (the second needs `PYTHONPATH=src` until the editable install is done).

## Configuration

1. **Runner settings:** `seo setup device` writes `%LOCALAPPDATA%\JasperSEOReporting\config\runner.yaml` (never overwritten once it exists). See `config/runner.yaml.example` for every option: storage folders (defaults under `%LOCALAPPDATA%\JasperSEOReporting`), `render.fonts`, `workbooks.reporting_spreadsheet_id`, `paths.oauth_client_file` / `paths.oauth_cache_file` and the optional `paths.shared_activity_dir`. The loader refuses anything that looks like a credential. A different file can be named with `--config PATH` or `JASPER_SEO_CONFIG`.
2. **Client registry:** `config/clients.yaml` maps each exact sheet Client Name to a permanent `client_key` (it is local and git-ignored; copy the team lead's so keys match across computers). `seo clients add` maintains it. `config/clients.yaml.example` shows the format.
3. **Contact block and logo:** `config/jasper.yaml` holds the regional Jasper details (United States, Australia). The contact person (name, e-mail, phone) comes only from the client's own row in the workbook. A regional profile may also name its own report logo (`logo: jasper-us-logo.png`, a `.png` inside `assets/`); a region without one uses the standard `assets/jasper-logo.png`. A finished report keeps the logo it was made with, so a changed logo shows on new reports (a new 28-day period) or in a `report preview`.

## Safety rules

* **The production gate:** `report run` and `report batch` need `--production` and pass a built-in production gate: a valid verification attestation covering that client and its properties (`verify live`, valid 7 days, bound to the Windows account and computer) and credentials kept outside the project and OneDrive (`live_google_not_verified`, `credentials_not_relocated`). There is no option or environment variable that bypasses it. The web page runs the same commands, so the same gate applies.
* A report's period must be exactly 28 days and equal the client's dates in the workbook.
* Commands that only read (`clients check`, `status`, `doctor`, `report open`, `report dry-run`) never write reports, the ledger or history.
* The web page listens on `127.0.0.1` only, needs a one-time access token, can start only `verify live` and `report run`, and runs one job at a time.
* No credentials are stored in this repository. `credentials/`, `token*.json`, `client_secret*.json`, the databases, reports and the private settings files are git-ignored. Never paste, e-mail or commit credentials.

## Authentication

The runner uses an OAuth client file and a cached sign-in with **read-only** scopes (Sheets, Search Console, GA4). `seo setup auth` is the only command that opens a browser sign-in. If Google reports the sign-in as revoked (`auth_credentials_revoked`), run it again; this happens about weekly while the Google consent screen is in "Testing" status, and publishing the consent screen (a Google Cloud setting, not a code change) removes that expiry.

## Commands at a glance

All commands accept `--json` and `--config PATH`. Everything with `--production` or `--live` writes data or contacts Google.

```powershell
seo web                                       # the local page
seo clients check                             # every client: ready, or exactly what to fix
seo clients add --name "<exact sheet name>"   # register a client (--all-missing [--yes] for every missing one)
seo verify live --client KEY --live           # contacts Google (read-only); stores the 7-day verification
seo report run --client KEY --period START_END --production
seo report open --client KEY                  # open the latest finished PDF
seo status                                    # run states from the ledger (read-only)
seo doctor                                    # local preflight: runtime, packages, Chromium, fonts, storage, auth metadata
seo report dry-run --client KEY --period START_END   # plan only; renders nothing, contacts nothing
seo setup device | setup auth                 # one-time setup
```

`report preview` renders a real-data report labelled `PREVIEW — NOT FINAL` into its own folder without creating a production report. `system-info`, `recovery inspect|acknowledge|cancel` and `report batch` are described in `docs/r8/r86_cli_reference.md`. Exit codes are stable (`--help` lists them).

## Limitations

* **Delivery is not implemented:** request intake, status sender, Make.com scenarios, Google Drive upload, approval, e-mail delivery, Windows scheduled task, backups, monitoring and alerts. A run ends at `audited`.
* **Per-device storage:** each computer has its own ledger and reports. The optional shared activity folder only *warns* that a report exists elsewhere; it is not a lock.
* A running report cannot be cancelled from the web page (Ctrl+C in the terminal that started it).
* No `supersede`, `poll` or `publish` CLI command (supersede exists in the library only).
* What has **not** been verified: a clean-machine install from scratch on a second computer, and the shared activity folder across real computers (both are covered by tests in temporary folders only).

More detail: `docs/r8/as_built_status.md`, `docs/r8/deployment_readiness.md`, `docs/r8/security_and_authentication.md`.

## Tests

```powershell
$env:PYTHONPATH = "src"      # not needed after an editable install
python -m pytest tests -q
```

More than 5,600 tests; they use synthetic data, temporary folders and fakes and make no Google, Make, Drive or e-mail calls.
