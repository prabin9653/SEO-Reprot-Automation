# R8.2 — Make.com scenarios (design)

> **As-built status (updated R8.4.6).** This document was written at R8.2 as a **design**. The implemented system is described in [as_built_status.md](as_built_status.md); where this document and that one differ, **as_built_status.md is correct**. Labels used below: *Implemented and tested*, *Implemented, not live-tested*, *Proposed*, *Not yet implemented*, *Known limitation*. Sections not mentioned in the list are design only.
>
> **Everything in this document is *Not yet implemented* (Proposed).** No Make.com scenario, webhook, scheduled trigger or connection exists, and none has been approved. What exists on the runner side: the request/response contracts (in-process), the idempotency key, the `make_status` **outbox rows** (written, never sent), and the batch summary object. The Make.com architecture stage (R8.5) has now produced the **superseding design**: [r85_make_architecture](r85_make_architecture.md), [r85_scenario_specifications](r85_scenario_specifications.md), [r85_communication_protocol](r85_communication_protocol.md), [r85_data_contracts](r85_data_contracts.md), [r85_security_and_reliability](r85_security_and_reliability.md), [r85_implementation_roadmap](r85_implementation_roadmap.md), [r85_cost_and_operations](r85_cost_and_operations.md). Where this older document differs (six physical scenarios instead of 1a/1b/2a/2b; a transport envelope for status messages; a flag-driven scheduler; no `dry_run` requests; Drive upload by the runner with Make only verifying; approval bound to the PDF hash with a confirmation code), **the R8.5 documents are correct**. Still *Designed but not implemented*: nothing in either design exists as a scenario, webhook or connection.

Status: **design only. No scenario, connection or webhook is created.** Make.com plan limits, module names,
operation counting, file-size limits, webhook behaviour and error-handler options below are **[A]** and must be
verified against current Make documentation in R8.5 before anything is built. Module names are descriptive, not
guaranteed UI labels.

## 1. Division of work

| Make.com | Local runner (Python) |
|---|---|
| schedules the month's requests, writes request rows | validates and executes requests, owns run state |
| receives status messages, writes `Execution History` | owns the ledger and the outbox |
| notifies the operator | never contacts the operator directly except through the status webhook |
| presents the PDF for approval, sends e-mail, records delivery | produces, finalizes, audits and uploads the PDF; never sends e-mail |

## 2. Scenario 1 — Monthly orchestration

Make triggers are one per scenario, so Scenario 1 is built as two linked scenarios (**1a** schedule/requests,
**1b** status receiver). Logically they are one workflow. **[A]**

### 1a — create requests (scheduled)

1. **Trigger:** schedule (daily, early morning, in the operator's time zone). Daily rather than monthly because
   the pilot's due date is per-client (explicit dates), so "due" is evaluated each day.
2. **Read** `Client Configuration` (Google Sheets: search/get rows), `Client Delivery Settings`, and existing
   `Report Requests`.
3. **Filter / iterate** per client (iterator): due when all hold —
   `Status = active`; `settings_status = enabled`; `reporting_frequency <> manual`;
   `Report End Date + lag_days <= today` (lag default 3 days for GA4 processing [V: ga4.RECENT_DAYS = 2]);
   no existing request row with the same `idempotency_key`.
4. **Build** `period_id`, `idempotency_key = monthly:<period_id>:<client_key>:run`; set `rankings_ready` from the
   operator's flag in the request template (Make does not read ranking cells).
5. **Add row** to `Report Requests` (`request_status = queued`). The unique idempotency key makes a re-run of the
   scenario harmless (the pre-check plus the runner's own dedupe).
6. **Error handler:** a failure for one client logs to the operator notification and **continues** with the next
   iteration (per-bundle error handler: ignore/resume; **[A]** verify directive names).
7. **Notify** the operator with a summary of how many requests were queued.

### 1b — status receiver (webhook)

1. **Trigger:** custom webhook; the URL is secret and stored in the runner's secret store.
2. **Authenticate:** require header `X-Jasper-Token` equal to a stored secret (filter at the start of the
   scenario); optional `X-Jasper-Signature` HMAC (see [security_and_authentication](security_and_authentication.md) §6).
   A request failing the check is discarded and counted.
3. **Validate** `schema` = `jasper.run-response` or `jasper.delivery-ready`, `version = 1`; unknown → alert.
4. **Deduplicate** on (`outbox_id`, `run_key`): a Make data store or a lookup in `Execution History`
   (the runner may redeliver after a timeout). Duplicates are acknowledged with HTTP 200 and ignored.
5. **Append** a row to `Execution History`; **update** the request row (`request_status`, `last_state`, `last_status_at`).
6. **Route** by `state`/`outcome`:
   * `failed` with `retry.eligible` → notify the operator (the runner retries by itself within its policy);
   * `failed`/`blocked` not eligible → notify the operator with `error.category`/`error.code`;
   * `ready_for_delivery` → forward to Scenario 2 (§3) and notify;
   * `delivered` → close the request row.
7. **Respond** HTTP 200 quickly; heavy work after the response **[A]**.

### Data passed between 1a, 1b and the runner

`Report Requests` row (request) → runner; `jasper.run-response` (status) → 1b; `jasper.delivery-ready` → 2.
Contracts: [execution_contract](execution_contract.md). Only `client_key`, ids, hashes, fixed codes.

### Monitoring

* "Silence" alarm: a scheduled check (inside 1a) lists requests still `queued`/`submitted` after N hours (default
  12) and notifies the operator; the runner polls every 15 minutes (Windows Task Scheduler).
* Webhook failures: if Make is unreachable the runner's outbox retries; no data loss. A failed Make run is
  notified through Make's own error notification **[A]**.

## 3. Scenario 2 — Approval and delivery

Built as **2a** (ready event → approval request) and **2b** (approval → send), because approval is a human event
with an unbounded delay. **[A]**

### 2a — receive ready event and request approval

1. **Entry:** called from 1b (or its own webhook) with a `jasper.delivery-ready` message.
2. **Idempotency:** if a `Delivery` row with this `delivery_key` exists → stop (log).
3. **Validate recipients:** read `Client Delivery Settings` for `client_key`; require `settings_status = enabled`,
   `delivery_preference = approve_then_email`, non-empty `recipient_to`; compute `recipient_set_hash16`; verify
   the message's `delivery_key` ends with it. Failure → notify, set `delivery_status = cancelled`,
   `last_error_category = recipient_invalid`.
4. **Verify the file:** Google Drive: get file metadata for `drive_file_id`; require size = `pdf_bytes` and parent
   = `drive_folder_id` (and, if available, the stored property `pdf_sha256`) **[A]**. Mismatch → notify, do not
   proceed.
5. **Add row** to `Delivery` (`approval_status = pending`, `delivery_status = not_started`).
6. **Present for approval:** notify the operator (e-mail or chat) with the client key, period id, `pdf16`, a
   link to the Drive file for review, and a link to the `Delivery` row. No PDF content or client e-mail address in
   chat messages.

### 2b — send after approval

1. **Trigger:** watch the `Delivery` tab for `approval_status` changing to `approved` (polling or instant,
   **[A]**), or a Make "approval" mechanism if the plan offers one.
2. **Re-verify:** `approval_status = approved`, `approved_by` and `approved_at` present, `delivery_status =
   not_started` (or `failed` for a retry), and the row's `pdf_sha256` still equals the `Delivery-ready` hash
   (a superseding report creates a new `delivery_key`; an older approval never applies to it).
3. **Claim:** update the row to `delivery_status = sending`, `sending_started_at = now`, `delivery_attempts + 1`
   **before** sending. If another run already holds `sending` newer than 15 minutes → stop.
4. **Fetch:** download the PDF from Drive by `drive_file_id` (the same file, never regenerated).
5. **Send** the e-mail (provider connection, subject/body template, attachment or link per the file-size limit
   **[A]**) to `recipient_to`/`recipient_cc`, from the Jasper sender mailbox.
6. **Record:** `delivery_status = sent`, `sent_at`, `mail_message_ref`; append history; notify the operator.
7. **Error handler:** on any failure after claim: `delivery_status = failed`, `last_error_category`, notify.
8. **Retry:** the operator (or an automatic retry limited to 3 attempts at 15 minutes / 1 hour / 4 hours) re-runs
   2b for the same `delivery_key`; it re-fetches the same Drive file. A `failed` row is never regenerated by the
   runner. An ambiguous `sending` older than 15 minutes (crash between send and record) is **not** retried
   automatically: it requires the operator to confirm whether the mail left (exactly-once e-mail delivery cannot
   be guaranteed; the design is at-least-once with this guard).

## 4. Required modules and connections (descriptive)

| Need | Make module type | Connection |
|---|---|---|
| schedule | scheduler trigger | – |
| read/write rows | Google Sheets (search rows, get row, add row, update row, watch rows) | Make's Google connection (edit rights to the four new tabs) |
| status receiver | custom webhook + webhook response | – |
| iterate / branch / filter | iterator, router, filters, set variable | – |
| dedupe | data store (or Sheets lookup) | – |
| Drive metadata / download | Google Drive (get file, download file) | Google connection with read access to the controlled folder |
| e-mail | Gmail/Microsoft 365/SMTP | the sending mailbox connection |
| notifications | e-mail or chat module | operator channel |
| error handling | error-handler routes (ignore/resume/break) | – |

No Make.com module ever receives credentials, OAuth tokens or the runner's secrets.

## 5. Timeouts and retries (Make side) **[A]**

Webhook response within the platform's limit; Sheets/Drive calls use the platform's built-in retry for transient
errors; delivery retry as in §3.2b; no loop in Make calls the runner (the runner is never invoked by Make
directly, only through request rows).

## 6. Duplicate-execution prevention (Make side)

* 1a: pre-check of the idempotency key before adding a row; unique key in the runner.
* 1b: (`outbox_id`, `run_key`) dedupe.
* 2a: `delivery_key` existence check.
* 2b: claim-before-send state plus the ambiguity guard.
