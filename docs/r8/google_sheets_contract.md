# R8.2 — Google Sheets contract

> **As-built status (updated R8.4.6).** This document was written at R8.2 as a **design**. The implemented system is described in [as_built_status.md](as_built_status.md); where this document and that one differ, **as_built_status.md is correct**. Labels used below: *Implemented and tested*, *Implemented, not live-tested*, *Proposed*, *Not yet implemented*, *Known limitation*. Sections not mentioned in the list are design only.
>
> * **Implemented and tested (offline):** §1 the existing `Clients` and `Keywords` tabs are read by `client_loader` (per-client isolation, fixed rejection codes, explicit 28-day period). Reading the live workbook is *Implemented, not live-tested*. Authentication is non-interactive; read failures raise `WorkbookUnavailable(category, code)`.
> * **Not yet implemented:** §3 `Client Delivery Settings`, §4 `Report Requests`, §5 `Execution History`, §6 `Delivery`, §8 validation of those tabs, §9 access arrangements. The execution history lives in the local ledger today; nothing is written back to any workbook.

Status: **design, not implemented. The workbook is not modified by this stage.** All example rows are synthetic.

## 1. Existing tabs (unchanged) [V]

| Tab | Parsed by | Required columns | Other columns read |
|---|---|---|---|
| `Client Configuration` | `clients.CLIENT_COLUMNS` | Client Name, Website, GSC Property, GA4 Property ID, Status | Project Manager (also accepts the misspelling "Project Maanger"), Manager Email, Manager Phone, Target Country, Target City, Report Start Date, Report End Date |
| `Keyword Ranking Sheet` | `clients.KEYWORD_COLUMNS` | Client Name, Keyword, Previous Ranking, Current Ranking | Target Location, Target URL, Notes |

Verified behaviour that the design relies on:

* Headers are matched case- and space-insensitively; **unknown extra columns are ignored**; a known header that
  appears twice is an error. New columns would therefore not break the parser, but this design adds **no column to
  the existing tabs**.
* A client is identified in the workbook only by its **exact name**; the stable `client_key` lives in
  `config/clients.yaml`, not in the workbook.
* `Status` equal to `active` (case-insensitive) selects a client. Report Start/End Date are optional in the parser;
  for the pilot they become **required** (checked by the runner's validation, not by the parser).
* **`Manager Email` and `Manager Phone` are the *sender's* (project manager's) details.** They feed the report's
  sender block (`clients.resolve_sender`). They are never a recipient and are never repurposed. Recipients live
  only in the new tab `Client Delivery Settings`.
* Rankings are typed by hand in `Keyword Ranking Sheet` (no rank tracker). A `Ranking Ready` gate is therefore part
  of the request (see §3).

## 2. New tabs overview

| Tab | Written by | Read by | Purpose |
|---|---|---|---|
| `Client Delivery Settings` | operator | Make (and runner for validation) | client_key mirror, recipients, frequency, delivery preference |
| `Report Requests` | Make (and operator) | runner (read-only polling) | the work queue |
| `Execution History` | Make (from runner status webhooks) | operator | append-only status log |
| `Delivery` | Make (and operator for approval) | Make; runner (read-only mirror) | Drive file reference, approval, delivery state |

The task named three new tabs; a fourth (`Client Delivery Settings`) is proposed because recipients and
`client_key` must not live in the existing tabs, and the existing parser would not hold them. **[U]** confirm.

Data types used below: *text*; *date* `YYYY-MM-DD` (date cell or ISO text; the existing parser accepts both [V]);
*timestamp* UTC `YYYY-MM-DDTHH:MM:SS+00:00` as text; *bool* `TRUE`/`FALSE`; *enum* the listed lower-case values.
Row 1 is the header; header matching is case-insensitive. No formulas in data columns that Make or the runner
reads. Protected ranges: Make-written columns are protected from manual edits except where stated.

## 3. `Client Delivery Settings`

One row per client. Key: `client_key` (unique).

| Column | Type | Req. | Rules / allowed values | Example |
|---|---|---|---|---|
| `client_key` | text | yes | `[a-z0-9][a-z0-9_-]{0,62}`; must exist in `config/clients.yaml`; unique | `example-co` |
| `client_name` | text | yes | must equal the `Client Configuration` name **exactly** (cross-check; mismatch = `config_invalid`) | `Example Co` |
| `settings_status` | enum | yes | `enabled`, `paused` | `enabled` |
| `reporting_frequency` | enum | yes | `every_28_days`, `monthly_calendar`, `manual` (pilot supports `every_28_days` and `manual`; the period still comes from the explicit dates) | `every_28_days` |
| `delivery_preference` | enum | yes | `approve_then_email` (pilot default), `hold_for_manual` | `approve_then_email` |
| `recipient_contact_name` | text | no | client-side contact person, used in the e-mail greeting only | `Pat Sample` |
| `recipient_to` | text | yes if delivery enabled | one or more e-mail addresses separated by `;`; **never** the Manager Email | `pat@example.test` |
| `recipient_cc` | text | no | same rules | |
| `report_locale` | enum | no | `en-US` (only supported value today [V]); `en-AU` reserved, **not implemented** | `en-US` |
| `notes` | text | no | free text for operators; never read by automation | |

Derived by Make, not stored: `recipient_set_hash16` = first 16 hex of SHA-256 over the sorted, lower-cased,
trimmed recipient addresses of `recipient_to` + `recipient_cc`, so the ledger and the `Delivery` tab can key a
delivery without holding addresses.

## 4. `Report Requests`

One row per request. The runner reads rows; it never writes them. Columns:

| Column | Type | Req. | Rules / allowed values | Example |
|---|---|---|---|---|
| `request_id` | text | yes | lowercase UUID, unique | `0b9a6a52-6c1e-4f0e-9a43-6d3c6e2b9a01` |
| `idempotency_key` | text | yes | 1–128 chars `[A-Za-z0-9:._/-]`, unique | `monthly:2026-10-01_2026-10-28:example-co:run` |
| `client_key` | text | yes | as above | `example-co` |
| `period_id` | text | yes | `YYYY-MM-DD_YYYY-MM-DD`, exactly 28 days; equals the client's Report Start/End Date | `2026-10-01_2026-10-28` |
| `period_start` | date | yes | equals `period_id` start | `2026-10-01` |
| `period_end` | date | yes | equals `period_id` end | `2026-10-28` |
| `action` | enum | yes | `validate`, `dry_run`, `run`, `retry`, `publish` | `run` |
| `replace_existing` | bool | no (default FALSE) | only honoured for `run`/`retry`; TRUE is an explicit operator choice | `FALSE` |
| `rankings_ready` | bool | yes for `run` | set TRUE by the operator when the month's Previous/Current Ranking cells are final | `TRUE` |
| `requested_at` | timestamp | yes | | `2026-11-02T01:00:00+00:00` |
| `requested_by` | text | yes | `make:<scenario>` or `operator:<initials>`; no e-mail addresses | `make:monthly-orchestration` |
| `request_status` | enum | Make | `queued`, `submitted`, `running`, `completed`, `failed`, `blocked`, `rejected`, `cancelled` (maintained by Make from status webhooks) | `queued` |
| `last_state` | text | Make | mirror of the latest run state | `requested` |
| `last_status_at` | timestamp | Make | | |

The runner treats a row as new when its `idempotency_key` is not in the ledger. A row that fails schema validation
is recorded as a rejected request with a fixed code (`row_invalid`, `period_invalid`, `client_unknown`,
`rankings_not_ready`, …) and reported through the status webhook; Make then sets `request_status = rejected`.
`rankings_ready = FALSE` rejects a `run` with `rankings_not_ready` (retryable once the flag is set and a new
request is added).

## 5. `Execution History` (append-only)

Make appends one row per status message received from the runner. Rows are never edited.

| Column | Type | Req. | Rules | Example |
|---|---|---|---|---|
| `received_at` | timestamp | yes | Make receipt time | `2026-11-02T01:07:42+00:00` |
| `outbox_id` | integer | yes | from the runner; dedupe key with `run_key` + `state_version` | `17` |
| `request_id` | text | yes | | |
| `run_key` | text | yes | | `example-co/2026-10-01_2026-10-28` |
| `client_key` | text | yes | | `example-co` |
| `period_id` | text | yes | | |
| `attempt` | integer | yes | | `1` |
| `state` | enum | yes | run states ([execution_contract](execution_contract.md) §4) | `ready_for_delivery` |
| `outcome` | enum | yes | `succeeded`, `in_progress`, `failed`, `blocked`, `rejected`, `already_complete` | `succeeded` |
| `error_category` | text | no | fixed vocabulary | `` |
| `error_code` | text | no | fixed vocabulary | `` |
| `retry_eligible` | bool | yes | | `FALSE` |
| `retry_kind` | text | no | | `` |
| `run_id` | text | no | engine UUID | `0198c1a2-…-000000001000` |
| `render_input_sha256` | text | no | 64 hex | |
| `pdf16` | text | no | 16 hex | `9d8c7b6a5f4e3d2c` |
| `audit_outcome` | text | no | `consistent` etc. | `consistent` |
| `message_code` | text | no | fixed code | |

## 6. `Delivery`

One row per `delivery_key`. Created by Make when it receives a `jasper.delivery-ready` message; thereafter Make
(and the operator for the approval columns) update it. The runner only reads it (mirror).

| Column | Type | Req. | Rules / allowed values | Example |
|---|---|---|---|---|
| `delivery_key` | text | yes | `<run_key>/<pdf16>/<recipient_set_hash16>`, unique | `example-co/2026-10-01_2026-10-28/9d8c7b6a5f4e3d2c/5a1b2c3d4e5f6a7b` |
| `run_key` | text | yes | | |
| `client_key` | text | yes | | |
| `period_id` | text | yes | | |
| `pdf_sha256` | text | yes | 64 hex; the approval and the send are bound to this hash | |
| `pdf16` | text | yes | | |
| `pdf_bytes` | integer | yes | | `118765` |
| `drive_folder_id` | text | yes | | |
| `drive_file_id` | text | yes | | |
| `file_name` | text | yes | `<client_key>_<period_id>_seo-report_<pdf16>.pdf` | |
| `upload_confirmed_at` | timestamp | yes | from the delivery-ready message | |
| `recipient_set_hash16` | text | yes | see §3 | `5a1b2c3d4e5f6a7b` |
| `approval_status` | enum | yes | `pending`, `approved`, `rejected`, `expired` | `pending` |
| `approved_by` | text | on approve/reject | operator initials/label (not an e-mail) | `JW` |
| `approved_at` | timestamp | on approve/reject | | |
| `rejection_reason_code` | enum | on reject | `content_error`, `wrong_recipient`, `data_issue`, `other` | |
| `delivery_status` | enum | yes | `not_started`, `sending`, `sent`, `failed`, `cancelled` | `not_started` |
| `delivery_attempts` | integer | yes | | `0` |
| `sending_started_at` | timestamp | no | set before the e-mail is sent | |
| `sent_at` | timestamp | on sent | | |
| `mail_message_ref` | text | no | provider message id, if available | |
| `last_error_category` | text | no | fixed vocabulary | |
| `updated_at` | timestamp | yes | | |

Allowed edits: `approval_status`, `approved_by`, `approved_at`, `rejection_reason_code` may be edited by the
operator (protected range limited to named editors); everything else is written by Make only. Sheet version
history is the audit trail for the human approval; Make additionally appends an `Execution History`-style row for
each approval change **[A]** (see [make_scenarios](make_scenarios.md)).

## 7. Identifiers

* **Client identifier:** the existing `client_key`, mirrored in `Client Delivery Settings`, `Report Requests`,
  `Execution History` and `Delivery`. Name matching is only a cross-check.
* **Period identifier:** the existing period-folder convention `YYYY-MM-DD_YYYY-MM-DD`.
* **Pilot period source:** the client's `Report Start Date` / `Report End Date` in `Client Configuration`. Make
  copies them into the request row; the runner re-reads the workbook and rejects any mismatch
  (`period_differs_from_workbook`).

## 8. Validation the runner performs on these tabs (read-only)

Header presence and uniqueness; type and enum checks; `client_key` in the registry and in `Client Delivery
Settings`; name cross-check; period is 28 days and equals the workbook dates; `idempotency_key` uniqueness; no
duplicate `request_id`; `delivery_key` shape in `Delivery`. Failures are fixed codes; the offending row number is
reported, never cell content.

## 9. Access

The runner's reporting credential has **read-only** Sheets scope (as today [V]). Make uses its own Google
connection with edit rights to the four new tabs only (a separate spreadsheet for the operational tabs is an
option **[U]**: it would keep the client-data workbook strictly read-only for automation; recommended if the
workbook is shared widely).
