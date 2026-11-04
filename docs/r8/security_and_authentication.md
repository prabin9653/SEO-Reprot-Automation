# R8.2 — Authentication and credential security (design)

> **As-built status (updated R8.4.6).** This document was written at R8.2 as a **design**. The implemented system is described in [as_built_status.md](as_built_status.md); where this document and that one differ, **as_built_status.md is correct**. Labels used below: *Implemented and tested*, *Implemented, not live-tested*, *Proposed*, *Not yet implemented*, *Known limitation*. Sections not mentioned in the list are design only.
>
> * **Implemented and tested (offline):** §4 non-interactive authentication contract (`auth.get_credentials` never interactive; `auth.run_setup` only by explicit command at an attended console; structured `auth_*` codes; `auth_required` stops the batch), §5 expiry classification (`auth_credentials_revoked`, `auth_refresh_failed`), §9 logging (sanitized JSON lines, bounded rotation, identifiers; **not activated** until an entry point calls `configure_logging`).
> * **Proposed (not done):** §3 target credential layout and migration steps — see `credential_relocation_procedure.md`. *Update (R8.7d):* the code has had a configurable credentials location since R8.6b, and the files were relocated to `%LOCALAPPDATA%\JasperSEOReporting\credentials\` on 2026-11-02 (copied, hashes and ACL verified by the operator, external `runner.yaml` updated); the original copies in the project/OneDrive are retained and not yet retired, and the old token is not revoked. The text of this document otherwise describes the state before R8.7d.
> * **Not yet implemented:** §6 webhook authentication, §7 Make.com permissions, §8 PDF access controls for delivery, §10 backups.
> * **R8.6b (Implemented and tested offline):** the OAuth client file and the cached sign-in locations are configurable (`paths.oauth_client_file`, `paths.oauth_cache_file` in the runner settings; `auth.CredentialPaths`), with the in-project files as the unchanged fallback; the attended `setup auth` command and normal reporting use the same locations; the Google client factory (`google_clients.py`) builds the Sheets, Search Console and GA4 clients lazily from `auth.get_credentials()` and is injectable. Scopes, the Google Cloud project and the files' locations are unchanged; no credential was opened, moved or rotated. See `r86_cli_reference.md`.
> * **Unverified:** behaviour against real Google OAuth (refresh, revocation, consent-screen "Testing" 7-day expiry).

Status: **design only. No credential, token or secret was read, moved, created or changed in R8.2.** The files
`credentials/client_secret.json` and `token.json` were only observed to exist by name in R8.1.

## 1. Current state [V from code, not from the secret files]

* `auth.py`: `SCOPES = [spreadsheets.readonly, webmasters.readonly, analytics.readonly]`;
  `CLIENT_SECRET_PATH = <project>/credentials/client_secret.json`; `TOKEN_PATH = <project>/token.json`.
* `get_credentials()` loads the token, refreshes it when expired, and **falls back to `InstalledAppFlow.run_local_server`
  (opens a browser and waits) whenever there is no valid credential**, then rewrites `token.json`.
* Both files are git-ignored, but the project folder is under OneDrive (`C:\Users\…\OneDrive\Desktop\…`), so they
  are likely **synced to the cloud** [A: depends on the OneDrive Known Folder Backup / sync setting]. The `.venv`
  is also inside that folder.
* The sources call `get_credentials()` by default (`sheets.build_service`, `gsc.build_service`,
  `ga4.build_client` accept `credentials=` to inject) [V].

Consequences for an unattended runner: it could block forever waiting for a browser; secrets may leave the
machine through OneDrive; one token carries only read scopes so Drive upload needs new authorization.

## 2. Principles

1. Least privilege: separate credentials per purpose.
2. Secrets never in the repository, the project folder, OneDrive, the ledger, logs, status messages or sheets.
3. The unattended runner **never** starts an interactive flow; it fails with a fixed category and a notification.
4. Interactive sign-in is an explicit operator action on the runner machine.
5. Every secret has a documented owner, location, rotation/revocation procedure.

## 3. Target credential layout (migration plan, future tasks)

Directory (not synced, user-only): `%LOCALAPPDATA%\JasperSEOReporting\secrets\`, override
`JASPER_SEO_SECRETS_DIR`. The loader refuses a path inside the project tree or any OneDrive folder
(same helper the engine uses for the database [V: `snapshots.store.in_cloud_sync_folder`]).

| File | Purpose | Scopes |
|---|---|---|
| `google-oauth-client.json` | the OAuth *client* definition (client id/secret) | – |
| `google-reporting-token.json` | **reporting (read-only)** | `spreadsheets.readonly`, `webmasters.readonly`, `analytics.readonly` (existing) |
| `google-drive-token.json` | **Drive upload** | `drive.file` only (files the app creates) **[A: verify that `drive.file` can create files in the controlled folder; if not, create the root folder with the app and share it with Make's account]** |
| `google-sheets-write-token.json` | **future** Sheets write | `spreadsheets` — **not requested in this design**; Make writes the sheets |
| `make-status-webhook.json` | status webhook URL + shared secret | – (not Google) |

Three tokens are three separate consents, so a leaked read token cannot upload and a Drive token cannot read
Search Console or Analytics. Using one OAuth client for all three is acceptable; separate clients are optional.

Windows storage options, in order of preference:

1. **ACL-restricted files** in the directory above (owner full control only; permissions applied once with
   `icacls`; documented, no extra dependency) — recommended for the pilot.
2. Windows Credential Manager / DPAPI-protected storage through a library (`keyring` or `pywin32`) — stronger at
   rest, but adds a dependency and needs approval **[U]**.
3. Environment variables — rejected for tokens (visible to child processes).

### Migration steps (all future; none performed in R8.2)

1. Operator creates the secrets directory and sets its ACL.
2. Operator **moves** (not copies) `client_secret.json` and `token.json` there under the new names, then confirms
   the OneDrive web view no longer lists them and revokes/rotates if they were ever synced (rotating the OAuth
   client secret and re-consenting is the conservative choice if OneDrive had them).
3. A code change to `auth.py` (separate approval, R8.4): `get_credentials(purpose, interactive=False)`;
   default locations from `JASPER_SEO_SECRETS_DIR`; unchanged behaviour when `interactive=True`.
4. The repository keeps a `.gitignore` entry for the secrets patterns it already has.
5. `seo-report validate --online` checks presence, scopes and an access probe (`gsc.check_access`) for each purpose.

## 4. Non-interactive authentication contract

New exception `AuthRequiredError(purpose, reason)` with fixed `reason`:
`token_missing`, `token_unreadable`, `token_scope_mismatch`, `token_expired_no_refresh`, `refresh_failed`,
`client_secret_missing`. Behaviour:

* `interactive=False` (the default for the runner): never opens a browser; refreshes silently when possible and
  rewrites the token file atomically (temp file + rename in the same directory, user-only ACL).
* `RefreshError` (revoked/expired refresh token) → `AuthRequiredError(refresh_failed)`.
* The runner maps it to `error.category = auth_required` (see [execution_contract](execution_contract.md)),
  aborts the batch immediately (every remaining client would fail the same way), leaves all unstarted requests
  `queued`, sends one status message and exits with code 20.
* No secret value, token fragment or client id ever appears in a message, log line or exception text (the
  existing `auth.py` already promises this [V]).

## 5. Token expiry and re-authentication

Google refresh tokens can stop working (user revoked access, password/security change, unused for a long time, or
a consent screen left in "testing" with short-lived refresh tokens) **[A: verify current Google policy for the
account type and publishing status; if the OAuth consent screen is in testing mode, the pilot should either
publish it or accept a short re-authentication cycle]**. Procedure when `auth_required` is reported:

1. Operator receives the notification (Make, via the status webhook).
2. Operator runs the explicit interactive sign-in on the runner machine (proposed optional command
   `seo-report auth login --purpose reporting|drive`, separate approval; today: the existing `auth.py` flow).
3. Operator runs `seo-report validate --online`.
4. Operator issues `retry` requests; queued requests continue on the next poll.

## 6. Webhook authentication (runner → Make)

* The Make custom webhook URL is unguessable and treated as a secret (stored only in `make-status-webhook.json`).
* Every POST carries `X-Jasper-Token: <shared secret>`; Make's first filter rejects other values. A second,
  optional layer: `X-Jasper-Signature: sha256=<hex>` = HMAC-SHA256 of the raw body with a second secret, verified
  in Make if it offers a suitable function **[A]**.
* TLS only; the runner verifies certificates (no `verify=False`); a 4xx from Make is a permanent outbox failure
  (alert), a 5xx/timeout is retried with backoff.
* Rotation: change the secret in both places; the old one is rejected after the cut-over.

## 7. Make.com permissions

* Make's Google connection: edit rights to the four operational tabs only (preferably a separate spreadsheet),
  read access to the controlled Drive folder, no access to the client-data workbook beyond what 1a needs to read.
* Mail connection: only the sending mailbox. Separate Make users for operator vs administrator; two-factor on.
* The runner never receives a Make API key; Make never receives a Google token of the runner.

## 8. Client data separation and PDF access

* One folder per `client_key` locally and on Drive; no sharing of a folder between clients; Drive folder shared only
  with the operator group and Make's account (link sharing off).
* Status messages, ledger and logs contain no client names, no recipient addresses, no report text.
* PDFs contain client data: local output root and ledger live under the Windows profile, outside OneDrive
  [V enforced for the output root and the history database by the engine].

## 9. Logging

* JSON lines, one event per line: timestamp, level, `run_key`, `state`, fixed `code`, durations.
* A sanitizer rejects strings that look like tokens, e-mail addresses, absolute paths or URLs (the same families
  of checks `reviewer_record._unsafe_reason` applies [V]) before they reach a log.
* Log files in `%LOCALAPPDATA%\JasperSEOReporting\logs\`, rotated at 10 MB, kept 90 days.

## 10. Backups and retention (security view)

Ledger and history backups contain run metadata and client analytics snapshots: store them with the same ACLs, on a
second local drive or encrypted external storage. Secrets are **not** included in backups; they are re-created by
re-authorization. Retention periods are in [ledger_schema](ledger_schema.md) §8 and
[delivery_architecture](delivery_architecture.md) §9.

## 11. Open items

* **[U]** ACL files vs Credential Manager/DPAPI (new dependency).
* **[U]** Consent-screen publishing status and expected refresh-token lifetime.
* **[A]** `drive.file` semantics for the controlled folder.
* **[U]** Whether the OneDrive-synced copies of the current secrets require rotation.

## 12. Live-verification attestation (R8.7c) — Implemented and tested with synthetic data; **no attestation exists**

**Purpose.** The production gate's requirement `live_google_not_verified` could not be cleared by anything. R8.7c adds the *mechanism* that clears it, and nothing more: no live verification took place, no attestation was created, and no command can create one. `credentials_not_relocated` is independent and unchanged. The gate therefore still blocks all real reporting.

**Architecture** (`src/seo_report/attestation.py`; gate integration in `preflight.uncleared_requirements`, second stage in `cli_report.require_attestation_coverage`).
* An **attestation** is one DPAPI-protected file: `%LOCALAPPDATA%\JasperSEOReporting\attestation\live_verification.attest`. The gate evaluates it read-only on every `report run`, `report batch`, `recovery acknowledge` and `recovery cancel`, and `doctor` / `system-info` report its state. Evaluation never opens a browser, never touches the network, never writes and never extends the expiry.
* **Schema** `jasper.live-verification-attestation` v1 (protected JSON): `origin` (must be `live`), `issued_at`, `expires_at` (exactly seven days later), `binding`, credential-location fingerprints, workbook fingerprint, scopes fingerprint, the four checks (`oauth_refresh`, `sheets_read`, `search_console_read`, `ga4_read` with their outcomes and times), the verified clients (`client_key` plus fingerprints of their Search Console and GA4 properties), and a random nonce. Raw property ids, workbook id, machine id, SID and paths are never stored in the clear and never shown. The unprotected file header is only a magic string and a format byte.

**Evidence provenance.** An attestation can only be produced by `issue_attestation(VerificationEvidence.from_live_checks(...), settings)`. The evidence must contain a successful read-only check for the OAuth refresh (`ok` or `not_needed`), the reporting workbook, Search Console and GA4 for every listed client, all completed within 30 minutes of each other and of issuing, none in the future, for the configured workbook and the unchanged OAuth scopes. Synthetic evidence (`origin="synthetic"`) is refused at issue time and rejected at evaluation time. **Nothing in `src/` calls these functions today** (a test lists the permitted modules and currently allows only `attestation.py`); there is no command, option, environment variable or setting that supplies evidence or creates an attestation, and none was added.

**Machine and account binding.** `binding = SHA-256(label, MachineGuid, user SID)` (MachineGuid from `HKLM\SOFTWARE\Microsoft\Cryptography` via `winreg`; SID from the process token via `ctypes`). The binding is stored in the payload and also used as DPAPI entropy, so a different machine or Windows account fails twice (unprotect fails; the binding would not match). The credential-file *paths* (not contents) are fingerprinted too, so relocating credentials after verification invalidates it. If MachineGuid or the SID cannot be read the attestation is rejected (`attestation_identity_unavailable`).

**Tamper protection.** Windows DPAPI (`CryptProtectData` / `CryptUnprotectData` through `ctypes`, current-user key, UI forbidden, application entropy) — no new dependency. DPAPI authenticates the data, so any changed byte, another account or another machine fails (`attestation_unprotect_failed`). A plain hash would not protect against editing and is not used.

**Expiry and clock.** Valid for **7 days** (rationale: a monthly operator can verify, run and review within a working week; an OAuth client in "Testing" status loses refresh tokens after 7 days anyway; a revoked consent or changed property cannot hide behind an old record). The period is a constant in code, not a setting (the settings loader rejects unknown keys, so no configuration can lengthen it). Timestamps are UTC. Rejected: expired (`now >= expires_at`), issued more than five minutes in the future, a validity other than seven days, and a file whose modification time is more than five minutes ahead of "now" (`attestation_clock_suspect`, a conservative sign that the clock moved back). Re-verification writes a new record with a new period; checking never extends anything. *Limitation:* a clock set back by less than the record's age cannot be told apart from an honest clock when the file's time is also older; this cannot be fully solved offline.

**Storage and permissions.** Only under `%LOCALAPPDATA%` (no fallback when it is unset), canonicalised, never inside the project, OneDrive, a network share or via a link/alias, a regular file of at most 64 KB. The folder gets a protected DACL (current user and SYSTEM only) when the record is issued; on every evaluation the file's and the folder's owner/DACL are read (SDDL) and anything granting access to other principals (Everyone, Users, Authenticated Users, other accounts, no DACL) fails closed (`attestation_permissions_unsafe`). Writes go to a temporary file in the same folder, are flushed and `fsync`ed, then replaced atomically; a failure leaves the previous record intact and no temporary file.

**Gate behaviour.** `uncleared_requirements` now returns `live_google_not_verified` unless a valid attestation is present; for a present-but-invalid one it also returns the specific code. `credentials_not_relocated` is computed exactly as before; other blockers are untouched. After the workbook is read (the properties are only known then) every client about to run must be in the attestation with the same property fingerprints (`attestation_client_not_covered`, `attestation_property_changed`) before any client is built or the ledger is opened. An injected test gate replaces both stages; it is reachable from Python only.

**Failure codes (all fixed, none contains a value):** `attestation_missing`, `_location_unsafe`, `_inaccessible`, `_permissions_unsafe`, `_corrupt`, `_unsupported_version`, `_unprotect_failed`, `_protection_unavailable`, `_identity_unavailable`, `_not_live`, `_incomplete_evidence`, `_binding_mismatch`, `_expired`, `_future`, `_clock_suspect`, `_validity_invalid`, `_workbook_changed`, `_credentials_changed`, `_scopes_changed`, `_client_not_covered`, `_property_changed`, `_check_failed`.

**Recovery and renewal.** Expired, unreadable, tampered or mismatched → the gate blocks; a person repeats the live verification (R8.7e), which issues a fresh record. A moved machine or Windows account, new credential locations, a changed workbook, scope set or client property all require re-verification. A record is never edited or deleted by the application; an operator may delete the file to start over.

**What R8.7e must do.** After relocating the credentials (R8.7d): at an attended session, with the configured credential paths, actually (1) obtain/refresh a token non-interactively where one exists, (2) read the reporting workbook, (3) read Search Console and GA4 for the pilot client's configured properties, recording success only from real read-only responses; (4) build `VerificationEvidence.from_live_checks(...)` from those results and call `issue_attestation` once; (5) add its module to the tripwire allow-list in review. It must expose no flag that skips the checks and must never accept evidence from a file, argument or variable.

**Unresolved security limitations.** DPAPI protects against other accounts, other machines, copying and editing, **not against malicious code running as the same Windows user** (which could call `issue_attestation` itself); provenance is therefore a property of the code path and of review, not of cryptography. The attestation proves a past verification, not that Google access still works today. The record is not hardware- or TPM-bound. The clock-rollback check is heuristic. Real DPAPI/ACL behaviour was exercised only on this development machine with synthetic data.

**R8.7d note.** **Credential relocation (R8.7d, performed by the operator in a normal Windows PowerShell on 2026-11-02):** the OAuth client file and cached sign-in were copied to `%LOCALAPPDATA%\\JasperSEOReporting\\credentials\\`, the destination ACL (current user, SYSTEM, Administrators) and the source/destination SHA-256 hashes were verified at that time, and an external `runner.yaml` under `%LOCALAPPDATA%\\JasperSEOReporting\\config\\` points `paths.oauth_client_file` and `paths.oauth_cache_file` at the new files. The original OneDrive copies are **kept, not retired**, and the old token was not revoked. `doctor` checks the configured locations and file states only, not ACLs or contents. The relocation requirement checks **both** OAuth locations independently (R8.7d.3): the client file and the cached sign-in must each be configured outside the project and outside OneDrive or any recognized cloud-synchronised folder; this is a check of configured locations, not of ACLs or file contents. The R8.7c attestation binds exactly these configured paths; the future live verification (R8.7e) must run with them, from a normal (non-packaged) PowerShell session, because a packaged app such as Claude Desktop sees a redirected `%LOCALAPPDATA%`.


## 13. Live verification workflow (R8.7e.1b) — implemented and tested offline; **never run against Google**

`seo-report verify live --client CLIENT_KEY --live --config <runner.yaml>` (module `live_verification.py`) is the only code that may issue the verification attestation. **It contacts Google (read-only) when run, may rewrite `token.json`, and stores the attestation. It has not been executed: the live run is R8.7e.2 and needs separate approval. No attestation exists.**

**Required checks, in this fixed order; the first failure stops the run:** (1) configuration: registry, sender, workbook ID, the client, both credential locations relocated (§12 / R8.7d.3), a cached sign-in present; (2) `oauth_refresh`: credentials loaded; the outcome is `not_needed` (cached credentials were valid: no refresh, no write) or `ok` (a refresh was required and succeeded; `auth` then **rewrites the token file** atomically); a failed refresh stops everything and is never reported as an outcome (`auth.get_credentials_with_status`; `get_credentials` is unchanged); (3) `sheets_read`: the existing `load_workbook_clients` (read-only Sheets, structure validation) must load the requested client; its exact Search Console and GA4 identifiers come only from there; (4) `search_console_read`: `gsc.verify_access`: the exact property is in the account's accessible list and one totals query over a 7-day window ending 3 days ago is answered; (5) `ga4_read`: `ga4.verify_access`: the property's metadata exposes the report's fields and one one-metric report is answered. Neither verification writes anything or changes any property setting.

**Evidence and issuing.** The evidence (`VerificationEvidence.from_live_checks`) is built inside the run from its own results, with each check's timestamp taken from the clock when the step finished; there is no argument, option, environment variable or file through which evidence, a timestamp or a success flag can be supplied. `attestation.issue_attestation` is called exactly once, only after every check succeeded, and re-validates the evidence (four checks, 30-minute window, workbook, scopes, clients) before anything is written. A failed run neither creates nor replaces the attestation file. The tripwire test now permits only `live_verification.py` (and only for the issue and evidence interfaces) besides `attestation.py`.

**Operating requirements.** Run it from a normal Windows PowerShell session, not from Claude Desktop (its packaged environment redirects `%LOCALAPPDATA%`, which the attestation location check refuses). Use the same `--config` that will be used for reporting, because the attestation binds the configured credential paths. The Cloud project must have the Sheets, Search Console and Analytics Data APIs enabled and the signed-in account must have access to the workbook, the Search Console property and the GA4 property; the current error messages cannot always tell a disabled API from missing access. If the OAuth consent screen is in "Testing" status the refresh token expires after 7 days (the cached sign-in dates from 2026-10-29).


## 14. Report preview (R8.7h.2) — implemented and tested offline; never run against real data

`report preview` is a read-only data collection plus a local render. It has **not** been run against real data: the live-data preview is R8.7h.3 and needs the operator's authorisation.

**Production-state isolation (each point is enforced by the code it calls and by a test with tripwires and filesystem comparisons).** No ledger is opened (`dry_run_client` takes none), so there is no request, run, attempt, state event, outbox row or lock; the history database and the production output root are read only through the existing `mode=ro`, finalized-only reader, and no snapshot is stored; nothing is finalized, audited, promoted or recorded, no reviewer record is written and no production folder is created; the preview folder lies outside the project and OneDrive, may not overlap any production folder, and is created exclusively (an existing folder is never reused). The attestation is only read (the same read-only gate as a production run), never changed. There is no email, Drive, Make.com or webhook code at all.

**The label.** The production document is built unchanged; only when `render_report_pdf(..., preview_label=...)` is called (by the preview alone) a small bordered label `PREVIEW — NOT FINAL` is added in the empty top margin of every page (it starts above the header, which begins 10 mm down) and is then measured in the browser on every page: present exactly once, visible, inside the page, not intersecting the header row, rule, body or footer. A missing, hidden, outside or overlapping label is a layout issue and the preview is refused. A production render (the default) is byte-identical to before. Chromium's own per-page font report shows the label's glyphs on each page.

**Live side effects.** Read-only calls to Google Sheets, Search Console and GA4, and a possible refresh of the cached sign-in (which rewrites `token.json`), exactly as for `verify live` and `report run`. The first preview creates `%LOCALAPPDATA%\JasperSEOReporting\previews`. Run it from a normal PowerShell, not Claude Desktop.
