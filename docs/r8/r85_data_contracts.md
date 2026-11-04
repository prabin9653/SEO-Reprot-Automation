# R8.5 — Data contracts

Design only. Contracts marked **Implemented and tested** exist in `src/seo_report/contracts.py` and their examples below are *machine-checked*
against that code by `tests/test_r85_documents.py`. Contracts marked **Designed but not implemented** are new, additive, and follow the same
conventions (closed objects, fixed codes, lower-case enums, UTC timestamps `YYYY-MM-DDTHH:MM:SS+00:00`, no free text, no client data).

**Reuse rule.** No state names, outcomes or error categories are invented. States: `requested, collecting, rendering, rendered, finalizing,
finalized, auditing, audited, publishing, ready_for_delivery, delivered, failed, blocked, cancelled`. Outcomes: `succeeded, in_progress, failed,
blocked, rejected, already_complete`. Error categories: the 19 in `contracts.ERROR_CATEGORIES`. New *codes* are fixed lower-case identifiers
(`[a-z0-9_]{1,64}`) and are listed per contract.

**Versioning strategy (all contracts).** `schema` names the contract; integer `version` starts at 1. Within a version only *additive optional*
fields may appear, and a consumer built for version N must ignore nothing silently: the runner-side contracts are *closed* (unknown fields are
rejected), so an additive field is introduced together with the code that accepts it. A breaking change increments `version`; producers and
consumers support N and N+1 together for one release (`schema_unsupported` otherwise). Make validates `schema` and `version` first.

**Never transmitted in any contract, log, sheet cell, notification or e-mail body:** OAuth access/refresh tokens, client secrets, API keys,
the webhook URL and signing secret, passwords, file-system paths, host or user names, spreadsheet IDs in payloads, exception messages or
tracebacks, ledger/database contents, client recipient e-mail addresses (only `recipient_set_hash16`), PDF bytes, client keyword or traffic data,
full Drive sharing links in logs. Idempotency details are per contract.

## 0. Transport envelope — `jasper.status-envelope` v1 (Designed but not implemented)

Wraps every runner → Make message. Adds ordering and dedupe information the payloads do not have (finding F4).

| Field | Type | Req. | Rules |
|---|---|---|---|
| `schema`, `version` | string, int | yes | `jasper.status-envelope`, `1` |
| `kind` | enum | yes | `request_ack`, `run_status`, `delivery_ready`, `heartbeat` |
| `envelope_id` | string | yes | UUID, **stable across retries** of the same message |
| `dedupe_key` | string | yes | `[A-Za-z0-9:._/#-]{1,160}`; for state changes `<run_key>#<state_version>#make_status` (the ledger's existing outbox key), acks `ack:<idempotency_key or row hash>`, reconciliation `status:<request_id>`, heartbeat `heartbeat:<yyyymmdd>` |
| `sequence` | int ≥ 0 | yes | the run's `state_version` for `run_status`; 0 otherwise |
| `sent_at` | timestamp | yes | UTC, set at *first* send attempt |
| `delivery_attempt` | int ≥ 1 | yes | increments per retry |
| `runner_id` | string | yes | `[a-z0-9-]{1,32}` label, **not** the host name |
| `payload` | object | yes | validated against the schema implied by `kind` |

```json contract=jasper.status-envelope
{
  "schema": "jasper.status-envelope",
  "version": 1,
  "kind": "run_status",
  "envelope_id": "5b1f0c9e-3a52-4d0b-8a6e-2f7c1d9e4b03",
  "dedupe_key": "example-co/2026-09-29_2026-10-26#9#make_status",
  "sequence": 9,
  "sent_at": "2026-11-02T01:07:31+00:00",
  "delivery_attempt": 1,
  "runner_id": "runner-1",
  "payload": {
    "schema": "jasper.run-response",
    "version": 1,
    "request_id": "0b9a6a52-6c1e-4f0e-9a43-6d3c6e2b9a01",
    "idempotency_key": "monthly:2026-09-29_2026-10-26:example-co:run",
    "mode": "production",
    "run_key": "example-co/2026-09-29_2026-10-26",
    "client_key": "example-co",
    "period_id": "2026-09-29_2026-10-26",
    "state": "audited",
    "outcome": "succeeded",
    "attempt": 1,
    "timestamps": {"requested_at": "2026-11-02T01:00:00+00:00", "updated_at": "2026-11-02T01:07:30+00:00", "finished_at": null},
    "replayed": false,
    "run_id": "0198c1a2-7b3c-7d4e-8f5a-000000001000",
    "revision": 1,
    "identity": {
      "render_input_sha256": "4f2e8a1c9b7d60353a1c5e7f9b2d4f6a8c0e1b3d5f7a9c2e4b6d8f0a1c3e5b7d",
      "render_input_format": {"name": "jasper.render-input", "version": 2},
      "generation_date": "2026-11-02",
      "pdf_sha256": "9d8c7b6a5f4e3d2c1a0b9e8d7c6b5a4938271605f4e3d2c1b0a9988776665544",
      "pdf16": "9d8c7b6a5f4e3d2c",
      "pdf_bytes": 118765,
      "record_name": "reviewer.0198c1a2-7b3c-7d4e-8f5a-000000001000.9d8c7b6a5f4e3d2c.json"
    },
    "audit": {"outcome": "consistent", "finalized_valid": 1, "verification_complete": true},
    "delivery": null,
    "error": null,
    "retry": {"eligible": false, "kind": null, "after": null},
    "warnings": []
  }
}
```

Idempotency: receiver dedupes on `dedupe_key`; ignores `sequence` lower than the stored maximum for the run. Sensitive: none beyond the payload rules.

## 1. Reporting request — `jasper.run-request` v1 (Implemented and tested)

| Field | Type | Req. | Rules |
|---|---|---|---|
| `schema`, `version` | string, int | yes | `jasper.run-request`, `1` |
| `request_id` | string | yes | lower-case UUID |
| `idempotency_key` | string | yes | `[A-Za-z0-9:._/-]{1,128}` (no `~`) — grammar in the scenario specifications §3.1 |
| `client_key` | string | yes | stable key from `config/clients.yaml` |
| `period_id` | string | yes | `YYYY-MM-DD_YYYY-MM-DD`, exactly 28 days, equal to the workbook's explicit dates |
| `action` | enum | yes | `validate, dry_run*, run, retry, publish*, status, acknowledge, cancel, supersede` (* parsed but **rejected** today; Make writes only `run, retry, status, acknowledge, cancel, supersede`, and `publish` after R8.8) |
| `requested_at` | timestamp | yes | UTC |
| `requested_by` | string | yes | `[A-Za-z0-9:._-]{1,64}`, e.g. `make:monthly-scheduler`, `operator:jw` (no e-mail address) |
| `options` | object | no | closed: `replace_existing` (bool), `operator` (label), `reason` (enum) |
| `options.replace_existing` | bool | no | default false; must be true for `supersede` |
| `options.operator`, `options.reason` | string, enum | **required** for `acknowledge`, `cancel`, `supersede`; **refused** otherwise | reason ∈ `blocking_condition_resolved, partial_file_reviewed, audit_finding_reviewed, data_corrected, content_error, no_longer_needed, duplicate_request, operator_decision` |
| *(future, additive)* `options.target_run_key` | string | no | run key (may end `~N`) that the action targets; rejected with `target_not_latest` unless it is the latest generation |

```json contract=jasper.run-request
{
  "schema": "jasper.run-request",
  "version": 1,
  "request_id": "0b9a6a52-6c1e-4f0e-9a43-6d3c6e2b9a01",
  "idempotency_key": "monthly:2026-09-29_2026-10-26:example-co:run",
  "client_key": "example-co",
  "period_id": "2026-09-29_2026-10-26",
  "action": "run",
  "requested_at": "2026-11-02T01:00:00+00:00",
  "requested_by": "make:monthly-scheduler",
  "options": {"replace_existing": false}
}
```

An operator action (replacement of an audited report):

```json contract=jasper.run-request
{
  "schema": "jasper.run-request",
  "version": 1,
  "request_id": "7c2d4e6f-1a3b-4c5d-8e9f-0a1b2c3d4e5f",
  "idempotency_key": "supersede:example-co/2026-09-29_2026-10-26:g1",
  "client_key": "example-co",
  "period_id": "2026-09-29_2026-10-26",
  "action": "supersede",
  "requested_at": "2026-11-05T09:00:00+00:00",
  "requested_by": "operator:jw",
  "options": {"replace_existing": true, "operator": "op:jw", "reason": "content_error"}
}
```

Idempotency: the ledger registers `idempotency_key` once; a repeat returns the stored response (`replayed: true`); the same key with a different client,
period or action is refused (`idempotency_key_reused`). Sensitive: none; `requested_by` must not be an e-mail address. Row-to-request mapping:
[scenario specifications §3](r85_scenario_specifications.md).

## 2. Request acknowledgement — `jasper.request-ack` v1 (Designed but not implemented)

Sent as an envelope of `kind = request_ack`. Needed because a malformed row may have no valid `request_id`, so a `run-response` cannot
describe it. For an accepted/replayed request the matching `run-response` is attached.

| Field | Type | Req. | Rules |
|---|---|---|---|
| `schema`, `version` | string, int | yes | `jasper.request-ack`, `1` |
| `decision` | enum | yes | `accepted`, `replayed`, `rejected` |
| `received_at` | timestamp | yes | UTC runner time |
| `request_id`, `idempotency_key`, `client_key`, `period_id`, `action` | string \| null | yes | null only when the row was too malformed to read that field |
| `sheet_row` | int \| null | yes | 1-based row number in `Report Requests` (never cell content) |
| `reject_code` | string \| null | yes | required iff `decision = rejected`; existing reject codes plus the additive `row_invalid, period_invalid, request_expired, target_not_latest` |
| `run_key` | string \| null | yes | set when a run exists |
| `state` | state \| null | yes | latest state when known |
| `response` | `jasper.run-response` \| null | yes | present for `replayed` |

```json contract=jasper.request-ack
{
  "schema": "jasper.request-ack",
  "version": 1,
  "decision": "accepted",
  "received_at": "2026-11-02T01:00:40+00:00",
  "request_id": "0b9a6a52-6c1e-4f0e-9a43-6d3c6e2b9a01",
  "idempotency_key": "monthly:2026-09-29_2026-10-26:example-co:run",
  "client_key": "example-co",
  "period_id": "2026-09-29_2026-10-26",
  "action": "run",
  "sheet_row": 12,
  "reject_code": null,
  "run_key": "example-co/2026-09-29_2026-10-26",
  "state": "requested",
  "response": null
}
```

Idempotency: `dedupe_key = ack:<idempotency_key>` (or `ack:row:<row>:<hash16>` for unreadable rows); a re-ack is harmless. Sensitive: no cell contents, no client data.

## 3. Execution status — `jasper.run-response` v1 in a `run_status` envelope (Implemented and tested)

The existing response contract is used unchanged (see the envelope example in §0, which is machine-checked). Fields: identifiers, `state`,
`outcome`, `attempt`, `timestamps`, `identity`, `audit`, `delivery`, `error {category, code, stage}`, `retry {eligible, kind, after}`,
`warnings`, and the optional `generation`, `supersedes`, `operator_action`. `delivery` is non-null only in `ready_for_delivery`/`delivered`, and those
states are valid only with a passing audit (`delivery_without_audit` is refused by the validator). Idempotency: envelope `dedupe_key`. Sensitive: no
paths, no exception text (codes only).

A blocked report (never delivered):

```json contract=jasper.run-response
{
  "schema": "jasper.run-response",
  "version": 1,
  "request_id": "0b9a6a52-6c1e-4f0e-9a43-6d3c6e2b9a01",
  "idempotency_key": "monthly:2026-09-29_2026-10-26:example-co:run",
  "mode": "production",
  "run_key": "example-co/2026-09-29_2026-10-26",
  "client_key": "example-co",
  "period_id": "2026-09-29_2026-10-26",
  "state": "blocked",
  "outcome": "blocked",
  "attempt": 1,
  "timestamps": {"requested_at": "2026-11-02T01:00:00+00:00", "updated_at": "2026-11-02T01:07:30+00:00", "finished_at": "2026-11-02T01:07:30+00:00"},
  "replayed": false,
  "run_id": null,
  "revision": null,
  "identity": null,
  "audit": null,
  "delivery": null,
  "error": {"category": "audit_failed", "code": "finding_partial_file_present", "stage": "auditing"},
  "retry": {"eligible": false, "kind": null, "after": null},
  "warnings": []
}
```

## 4. Finalized report notification — the `run_status` for state `audited` (Implemented and tested)

Not a separate schema: the envelope in §0 *is* the finalized-report notification (state `audited`, `identity` with the render hash, generation date,
`pdf_sha256`, `pdf16`, `pdf_bytes`, `record_name`). It says "the report exists, is finalized and passed the audit"; it does **not** say "uploaded" or "approved".

## 5. Audit result (Implemented and tested)

Carried in `run-response.audit = {outcome, finalized_valid, verification_complete}` (passing: `consistent`, ≥ 1, `true`) and, on failure, as
`error {category: "audit_failed", code: <finding>}` with state `blocked` (example in §3). The `jasper.delivery-ready` contract can describe only a
passed audit. Sensitive: finding codes only.

## 6. Approval request — `jasper.approval-request` v1 (Designed but not implemented; Make-internal)

Created by SCN-4A; it is the data stored in/with the `Delivery` row and the notification, and the definition of what a reviewer sees.

| Field | Type | Req. | Rules |
|---|---|---|---|
| `schema`, `version` | string, int | yes | `jasper.approval-request`, `1` |
| `delivery_key` | string | yes | `<run_key>/<pdf16>/<recipient_set_hash16>` |
| `run_key`, `client_key`, `period_id` | string | yes | as elsewhere |
| `generation` | int ≥ 1 | yes | from the run key suffix |
| `pdf_sha256` | string | yes | 64 hex — the approval is bound to this |
| `pdf16`, `pdf_bytes` | string, int | yes | shown to the reviewer; `pdf16` is the confirmation code |
| `file_name` | string | yes | `<client_key>_<period_id>_seo-report_<pdf16>.pdf` |
| `audit` | object | yes | exactly the passing audit block |
| `supersedes_delivery_key` | string \| null | yes | the older delivery this replaces, when any |
| `requested_at`, `expires_at` | timestamp | yes | `expires_at = requested_at + 14 days` |
| `reviewer_group` | string | yes | label of the group, not addresses |

```json contract=jasper.approval-request
{
  "schema": "jasper.approval-request",
  "version": 1,
  "delivery_key": "example-co/2026-09-29_2026-10-26/9d8c7b6a5f4e3d2c/5a1b2c3d4e5f6a7b",
  "run_key": "example-co/2026-09-29_2026-10-26",
  "client_key": "example-co",
  "period_id": "2026-09-29_2026-10-26",
  "generation": 1,
  "pdf_sha256": "9d8c7b6a5f4e3d2c1a0b9e8d7c6b5a4938271605f4e3d2c1b0a9988776665544",
  "pdf16": "9d8c7b6a5f4e3d2c",
  "pdf_bytes": 118765,
  "file_name": "example-co_2026-09-29_2026-10-26_seo-report_9d8c7b6a5f4e3d2c.pdf",
  "audit": {"outcome": "consistent", "finalized_valid": 1, "verification_complete": true},
  "supersedes_delivery_key": null,
  "requested_at": "2026-11-02T01:08:00+00:00",
  "expires_at": "2026-11-16T01:08:00+00:00",
  "reviewer_group": "report-reviewers"
}
```

Idempotency: one `Delivery` row per `delivery_key`. Sensitive: no recipient addresses (only the reviewer-visible restricted Drive link, which lives in the notification and the row, never in logs).

## 7. Approval decision — `jasper.approval-decision` v1 (Designed but not implemented; recorded in the `Delivery` row)

| Field | Type | Req. | Rules |
|---|---|---|---|
| `schema`, `version` | string, int | yes | `jasper.approval-decision`, `1` |
| `delivery_key` | string | yes | must match an existing, non-expired `Delivery` row |
| `pdf_sha256` | string | yes | copied from the row by Make; must equal the row's hash and the Drive file property |
| `approved_pdf16` | string | iff `approved` | the reviewer's confirmation; must equal `pdf_sha256[:16]` |
| `decision` | enum | yes | `approved`, `rejected` |
| `reviewer` | string | yes | initials/label `[A-Za-z0-9:._-]{1,64}`, not an e-mail address |
| `decided_at` | timestamp | yes | UTC |
| `rejection_reason_code` | enum | iff `rejected` | `content_error, wrong_recipient, data_issue, other` |

```json contract=jasper.approval-decision
{
  "schema": "jasper.approval-decision",
  "version": 1,
  "delivery_key": "example-co/2026-09-29_2026-10-26/9d8c7b6a5f4e3d2c/5a1b2c3d4e5f6a7b",
  "pdf_sha256": "9d8c7b6a5f4e3d2c1a0b9e8d7c6b5a4938271605f4e3d2c1b0a9988776665544",
  "approved_pdf16": "9d8c7b6a5f4e3d2c",
  "decision": "approved",
  "reviewer": "JW",
  "decided_at": "2026-11-02T14:20:00+00:00",
  "rejection_reason_code": null
}
```

Rules: valid only if `decided_at < expires_at`; void if a newer pending/approved row exists for the period; a decision never carries over to a different
`pdf_sha256`. Idempotency: processed once per row (marker `decision_recorded_at`). Sensitive: no free-text comment is transmitted (free text may sit in the sheet's `notes`, never copied).

## 8. Drive upload result — `jasper.delivery-ready` v1 (Implemented and tested contract; the producer is Designed but not implemented)

Emitted by the runner's publisher after the upload is verified; it can only describe a passed audit.

| Field | Type | Req. | Rules |
|---|---|---|---|
| `schema`, `version` | string, int | yes | `jasper.delivery-ready`, `1` |
| `run_key`, `client_key`, `period_id` | string | yes | consistent with each other |
| `delivery_key` | string | yes | exactly 4 segments `<client_key>/<period_id>/<pdf16>/<recipient_set_hash16>`; `pdf16 = pdf_sha256[:16]` |
| `pdf_sha256`, `pdf_bytes` | string, int | yes | 64 hex; ≥ 1 |
| `drive_file_id`, `drive_folder_id` | string | yes | `[A-Za-z0-9_-]{10,80}` |
| `file_name` | string | yes | `[A-Za-z0-9._-]{1,200}` |
| `audit` | object | yes | `outcome = consistent`, `verification_complete = true`, `finalized_valid ≥ 1` |
| `uploaded_at` | timestamp | yes | UTC |
| `manifest_sha256` | string | yes | 64 hex; the manifest copy in Drive carries the file's MD5 for the e-mail stage |

```json contract=jasper.delivery-ready
{
  "schema": "jasper.delivery-ready",
  "version": 1,
  "run_key": "example-co/2026-09-29_2026-10-26",
  "client_key": "example-co",
  "period_id": "2026-09-29_2026-10-26",
  "delivery_key": "example-co/2026-09-29_2026-10-26/9d8c7b6a5f4e3d2c/5a1b2c3d4e5f6a7b",
  "pdf_sha256": "9d8c7b6a5f4e3d2c1a0b9e8d7c6b5a4938271605f4e3d2c1b0a9988776665544",
  "pdf_bytes": 118765,
  "drive_file_id": "1AbCdEfGhIjKlMnOpQrStUv",
  "drive_folder_id": "1ZyXwVuTsRqPoNmLkJiHgFe",
  "file_name": "example-co_2026-09-29_2026-10-26_seo-report_9d8c7b6a5f4e3d2c.pdf",
  "audit": {"outcome": "consistent", "finalized_valid": 1, "verification_complete": true},
  "uploaded_at": "2026-11-02T01:07:40+00:00",
  "manifest_sha256": "7a6b5c4d3e2f10918a7b6c5d4e3f2a1b0c9d8e7f6a5b4c3d2e1f0a9b8c7d6e5f"
}
```

*Future additive field:* `drive_md5` (32 hex) so Make can compare Drive's `md5Checksum` without downloading the manifest (the closed schema must be
extended in the same change). Idempotency: ledger `UNIQUE(run_key, pdf_sha256, recipient_set_hash16)` and the Drive lookup by `pdf_sha256`. Sensitive: Drive ids are identifiers, not credentials, but must not be logged with sharing links.

## 9. Email delivery result — `jasper.email-delivery-result` v1 (Designed but not implemented; written to the `Delivery` row by SCN-6)

| Field | Type | Req. | Rules |
|---|---|---|---|
| `schema`, `version` | string, int | yes | `jasper.email-delivery-result`, `1` |
| `delivery_key`, `pdf_sha256` | string | yes | the key and the exact hash sent |
| `outcome` | enum | yes | `sent`, `failed`, `ambiguous`, `cancelled` |
| `attempt` | int ≥ 1 | yes | ≤ 3 automatic |
| `recipient_set_hash16` | string | yes | 16 hex; **no addresses** |
| `recorded_at` | timestamp | yes | UTC |
| `sent_at` | timestamp \| null | yes | iff `sent` |
| `mail_message_ref` | string \| null | yes | opaque provider id, `[A-Za-z0-9._@<>-]{0,200}` stripped of addresses |
| `error_category` | string \| null | yes | `delivery_failed` (existing) iff `failed` |
| `error_code` | string \| null | yes | `mail_connection_failed, mail_rejected, drive_download_failed, attachment_too_large, recipient_invalid, recipient_changed, approval_missing, hash_mismatch` |

```json contract=jasper.email-delivery-result
{
  "schema": "jasper.email-delivery-result",
  "version": 1,
  "delivery_key": "example-co/2026-09-29_2026-10-26/9d8c7b6a5f4e3d2c/5a1b2c3d4e5f6a7b",
  "pdf_sha256": "9d8c7b6a5f4e3d2c1a0b9e8d7c6b5a4938271605f4e3d2c1b0a9988776665544",
  "outcome": "sent",
  "attempt": 1,
  "recipient_set_hash16": "5a1b2c3d4e5f6a7b",
  "recorded_at": "2026-11-02T14:25:03+00:00",
  "sent_at": "2026-11-02T14:25:01+00:00",
  "mail_message_ref": "msg-0001",
  "error_category": null,
  "error_code": null
}
```

Idempotency: one result lineage per `delivery_key`; claim-before-send; `ambiguous` blocks automatic resend. Sensitive: no addresses, no subject/body, no attachment.

## 10. Error notification — `jasper.error-notification` v1 (Designed but not implemented; Make → operator)

| Field | Type | Req. | Rules |
|---|---|---|---|
| `schema`, `version` | string, int | yes | `jasper.error-notification`, `1` |
| `notification_id` | string | yes | UUID |
| `dedupe_key` | string | yes | `<event>:<run_key or scope>:<yyyymmddHH>`; repeats within the window are suppressed |
| `severity` | enum | yes | `critical`, `high`, `medium`, `info` (SEV-1…SEV-4) |
| `event` | enum | yes | `credentials_missing, google_permission_error, google_quota_or_temporary, client_config_missing, ranking_data_missing, render_failed, finalization_failed, audit_failed, runner_offline, request_timeout, lock_conflict, drive_upload_failed, approval_pending, approval_rejected, email_delivery_failed, delivery_ambiguous, disk_full, ledger_unavailable` |
| `source` | enum | yes | `runner`, `make`, `google`, `approval`, `delivery` |
| `occurred_at` | timestamp | yes | UTC |
| `client_key`, `period_id`, `run_key` | string \| null | yes | internal ids only |
| `state` | state \| null | yes | existing state names |
| `error_category`, `error_code` | string \| null | yes | existing vocabulary |
| `action_hint` | enum | yes | `reauthenticate, review_blocked_run, fix_client_config, fix_rankings, retry_later, check_runner, approve_or_reject, check_mailbox, none` |

```json contract=jasper.error-notification
{
  "schema": "jasper.error-notification",
  "version": 1,
  "notification_id": "9e8d7c6b-5a49-4382-9716-05f4e3d2c1b0",
  "dedupe_key": "audit_failed:example-co/2026-09-29_2026-10-26:2026110201",
  "severity": "high",
  "event": "audit_failed",
  "source": "runner",
  "occurred_at": "2026-11-02T01:07:31+00:00",
  "client_key": "example-co",
  "period_id": "2026-09-29_2026-10-26",
  "run_key": "example-co/2026-09-29_2026-10-26",
  "state": "blocked",
  "error_category": "audit_failed",
  "error_code": "finding_partial_file_present",
  "action_hint": "review_blocked_run"
}
```

Rules: fixed enums only; notification text is produced from a fixed template by Make from these fields. Idempotency: `dedupe_key` window (1 h). Sensitive: nothing beyond ids and codes.

## 11. Supplementary — `jasper.runner-heartbeat` v1 (Proposed; optional)

Envelope `kind = heartbeat`, at most once a day and at start. Fields: `schema`, `version`, `runner_id`, `sent_at`, `ledger_ok` (bool),
`open_runs` (int ≥ 0), `outbox_backlog` (int ≥ 0), `oldest_backlog_age_minutes` (int ≥ 0), `last_poll_at` (timestamp). No host name, no paths.
Allows Make to distinguish "idle" from "offline" (finding F11).

```json contract=jasper.runner-heartbeat
{
  "schema": "jasper.runner-heartbeat",
  "version": 1,
  "runner_id": "runner-1",
  "sent_at": "2026-11-02T06:00:02+00:00",
  "ledger_ok": true,
  "open_runs": 0,
  "outbox_backlog": 0,
  "oldest_backlog_age_minutes": 0,
  "last_poll_at": "2026-11-02T05:45:01+00:00"
}
```

## 12. Compatibility with R8.3/R8.4

* No existing field changes meaning; `run-response` and `delivery-ready` are used verbatim.
* All additions are new schemas or additive optional fields that must be accepted by the closed-schema validators in the same change.
* Additive reject codes (`row_invalid, period_invalid, request_expired, target_not_latest`) and the optional `options.target_run_key` are the only changes to the request side.
