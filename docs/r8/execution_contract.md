# R8.2 — Execution contract (request, response, states)

> **As-built status (updated R8.4.6).** This document was written at R8.2 as a **design**. The implemented system is described in [as_built_status.md](as_built_status.md); where this document and that one differ, **as_built_status.md is correct**. Labels used below: *Implemented and tested*, *Implemented, not live-tested*, *Proposed*, *Not yet implemented*, *Known limitation*. Sections not mentioned in the list are design only.
>
> * **Implemented and tested:** §2 request (transport is in-process objects only; the Make/HTTP transport of §1 is *Not yet implemented*), §3 response, §4 states and transitions (the pipeline reaches `audited` at most), §5 error categories (19), §7 versioning (`version` 1, additive changes only).
> * **Changed since R8.2:** actions now include `acknowledge`, `cancel`, `supersede` with mandatory `options.operator` and `options.reason` (closed list of eight reason codes); `options` may therefore hold three keys; responses may carry `generation`, `supersedes`, `operator_action`; run keys for later generations end in `~N`; the reject-code set grew (see as_built_status.md §2). §2.1 admission rules: a `blocked` run is retried only after an operator acknowledgement, and admission always looks at the *latest generation* of the period.
> * **Not yet implemented:** §6.2 successful execution **to `ready_for_delivery`** and §6.6 the delivery-ready message (no producer); `publish` is rejected with `publish_not_supported`.

Status: **design, not implemented.** All JSON in this document is illustrative; every value is synthetic
(`example-co`, fake hashes). No credential, token, secret or client personal data appears in any message.

Conventions: UTF-8 JSON, one object per message, `schema` + `version` members first, unknown members rejected by
the receiver (closed schemas, like the reviewer-record schema [V]), timestamps in UTC
`YYYY-MM-DDTHH:MM:SS+00:00` (the format the engine already validates [V]), dates `YYYY-MM-DD`, hashes lowercase hex.

## 1. Transport

| Direction | Mechanism | Notes |
|---|---|---|
| Make → runner (requests) | Make adds a row to the **Report Requests** tab; the runner polls it read-only ([google_sheets_contract](google_sheets_contract.md)) | no inbound port; the sheet row is the request, its columns map 1:1 to §2 |
| Runner → Make (status) | HTTPS POST of a §3 response to a Make custom webhook, from the ledger **outbox** with retry/backoff | the webhook URL and shared secret live in the runner's secret store, never in the sheet or the repo |
| Manual | CLI arguments map to the same §2 fields | `--request-id` is generated locally when absent |

The ledger, not the sheet and not Make, is authoritative for run state. A status POST that fails never changes run
state; it stays in the outbox until delivered.

## 2. Request

Schema `jasper.run-request` v1.

| Field | Type | Required | Rules |
|---|---|---|---|
| `schema`, `version` | `"jasper.run-request"`, `1` | yes | |
| `request_id` | UUID (lowercase) | yes | unique |
| `idempotency_key` | string 1–128, `[A-Za-z0-9:._/-]` | yes | unique per intended action; replay returns the stored response |
| `client_key` | string | yes | must exist in `config/clients.yaml` and be enabled in the workbook |
| `period_id` | `YYYY-MM-DD_YYYY-MM-DD` | yes | start ≤ end, exactly 28 days, equal to the workbook's Report Start/End Date for the client (pilot) |
| `action` | enum | yes | `validate`, `dry_run`, `run`, `retry`, `publish`, `status` |
| `requested_at` | timestamp | yes | |
| `requested_by` | string ≤ 64 | yes | `make:<scenario>` or `cli:<operator>`; no e-mail addresses |
| `options` | object | no | `replace_existing` bool (default false); `allow_partial_sources` bool (default false) |

Action semantics:

| Action | Effect |
|---|---|
| `validate` | validate request, client, period, workbook row; no state change except an event |
| `dry_run` | collect, assemble, render in scratch; never finalizes; recorded as an event only |
| `run` | create the run, or resume a run that is not `failed`/`blocked`; returns the stored result if already `audited` or later |
| `retry` | allowed only for a `failed` run whose `retry.eligible` is true; starts a new attempt at the failed stage's resume point |
| `publish` | (re)run only the upload/manifest step for a run that is `audited` or later |
| `status` | return the current stored state |

Idempotency key convention for Make-generated requests: `monthly:<period_id>:<client_key>:run`; an operator retry
uses `retry:<period_id>:<client_key>:<n>`.

### 2.1 Request admission rules

| Existing run for `run_key` | `run` | `retry` | `publish` |
|---|---|---|---|
| none | creates `requested` | rejected `no_run` | rejected `no_run` |
| `requested`…`auditing` (in progress or interrupted) | resumes (lock permitting), else `run_in_progress` | rejected `not_failed` | rejected `not_audited` |
| `failed`, retry eligible | rejected `use_retry` | new attempt | rejected `not_audited` |
| `failed`, not eligible / `blocked` | rejected `operator_required` | rejected `operator_required` | rejected |
| `audited`, `publishing` | returns stored result | rejected | resumes publishing |
| `ready_for_delivery`, `delivered` | returns stored result (`already_complete`) | rejected | returns stored result |
| `cancelled` | rejected `cancelled` | rejected | rejected |

A request with a previously seen `idempotency_key` returns the stored response without any other effect, even if
the run has since advanced (the response carries `replayed: true`).

## 3. Response / status message

Schema `jasper.run-response` v1. The same shape is used for the CLI JSON output and the status webhook.

| Field | Type | Notes |
|---|---|---|
| `schema`, `version` | | |
| `request_id`, `idempotency_key` | | echo of the request |
| `replayed` | bool | true when returned from the stored response |
| `mode` | `production` \| `dry_run` \| `read_only` | |
| `run_key`, `client_key`, `period_id` | | |
| `state` | enum (§4) | current run state |
| `outcome` | `succeeded` \| `in_progress` \| `failed` \| `blocked` \| `rejected` \| `already_complete` | |
| `attempt` | int ≥ 1 | |
| `run_id`, `revision` | UUID, int, or null | snapshot-store identity, null before persistence |
| `identity` | object or null | `render_input_sha256`, `render_input_format {name, version}`, `generation_date`, `pdf_sha256`, `pdf16`, `pdf_bytes`, `record_name` |
| `audit` | object or null | `outcome`, `finalized_valid`, `verification_complete` |
| `delivery` | object or null | `drive_file_id`, `drive_folder_id`, `file_name`, `delivery_key` (only from `ready_for_delivery`) |
| `error` | object or null | `category`, `code`, `stage` (fixed vocabularies only; no free text) |
| `retry` | object | `eligible` bool, `kind` (`collection`\|`render`\|`finalization`\|`publish`\|`delivery`\|null), `after` timestamp or null |
| `warnings` | array | `{code, source}`, e.g. incomplete GA4 data; fixed codes |
| `timestamps` | object | `requested_at`, `updated_at`, `finished_at` |

Never present: credentials, tokens, OAuth data, e-mail addresses, client names, URLs of the client, file system
paths, exception messages, tracebacks, report content.

## 4. States and transitions

Progress states: `requested` → `collecting` → `rendering` → `rendered` → `finalizing` → `finalized` → `auditing` →
`audited` → `publishing` → `ready_for_delivery` → `delivered`.

(`collecting` covers source collection, assembly and insights; `rendered` means the sidecar and the draft exist.)

Other states: `failed` (an error with a stage and category), `blocked` (operator action required; never retried
automatically), `cancelled` (operator cancelled), and, in the delivery mirror only, `delivery_failed`
(see [delivery_architecture](delivery_architecture.md); the run itself stays `ready_for_delivery`).

| From | To | Condition |
|---|---|---|
| (none) | `requested` | request admitted |
| `requested` | `collecting` | lock acquired |
| `collecting` | `rendering` | bundle assembled |
| `rendering` | `rendered` | `produce_report_draft` ok (sidecar + draft) |
| `rendered` | `finalizing` | |
| `finalizing` | `finalized` | `finalize_report` returned `finalized`/`already_finalized` |
| `finalized` | `auditing` | |
| `auditing` | `audited` | audit passes the gate in [production_architecture](production_architecture.md) §5.6 |
| `audited` | `publishing` | publisher configured |
| `publishing` | `ready_for_delivery` | Drive upload confirmed and manifest written |
| `ready_for_delivery` | `delivered` | Make reports `delivery_status = sent` (mirrored from the Delivery tab) |
| any in-progress state | `failed` | classified error |
| any in-progress state | `blocked` | conflict needing an operator |
| `failed` | `requested` (new attempt) | `retry` admitted; resume point per [ledger_schema](ledger_schema.md) §6 |
| `blocked` | `requested` | operator resolves and issues `retry` with `operator_ack` recorded as an event |
| non-terminal | `cancelled` | operator |
| any in-progress state (`collecting`, `rendering`, `finalizing`, `auditing`, `publishing`) | its resume state | recovery after a crash, with the attempt closed as `interrupted` ([ledger_schema](ledger_schema.md) §6) |

Terminal for the runner: `delivered`, `cancelled`. `audited` is the last state when no publisher is configured
(R8.3 runs this way).

**Delivery gate:** a run may enter `publishing` only from `audited`; `ready_for_delivery` and the delivery manifest
cannot exist for a run whose audit did not pass.

## 5. Error categories

| `error.category` | Typical source | Retry `eligible` | `retry.kind` | Automatic? |
|---|---|---|---|---|
| `validation_failed` | request or period invalid | no | null | no (fix request) |
| `config_invalid` | client missing/misconfigured in workbook or registry | no | null | no |
| `auth_required` | token missing/expired/revoked, non-interactive | after operator | `collection` | no |
| `source_temporary` | 429/5xx after the module's own retries | yes | `collection` | yes, bounded |
| `source_denied` | 403/404 on a property | no | null | no |
| `source_incompatible` | GA4 schema incompatible | no | null | no |
| `render_failed` | browser/fonts/fingerprint | yes (once) | `render` | once |
| `layout_issues` | renderer reported layout issues | no | null | no |
| `stale_bundle` | history changed between assembly and finalization | yes | `collection` | yes |
| `finalization_failed` | operating-system failure after a write (`incomplete`) | yes | `finalization` | yes |
| `finalization_conflict` | existing report/record differs, name conflicts, record limit | no | null | no → `blocked` |
| `audit_failed` | audit not consistent | no | null | no → `blocked` |
| `publish_failed` | Drive upload/confirmation failed | yes | `publish` | yes |
| `delivery_failed` | e-mail failed (Make-owned; mirrored) | yes | `delivery` | Make |
| `lock_unavailable` | another process holds the client lock | no (poll again) | null | next poll |
| `timeout` | stage deadline exceeded | yes (once) | stage's kind | once |
| `internal_error` | unexpected exception | no | null | no |

Retry kinds are distinct and never cross: a `delivery` retry never calls the runner; a `publish` retry never
re-renders; a `finalization` retry reuses the stored `render_input`, `finalized_at` and options; a `render` retry
reuses the sidecar's identity; a `collection` retry re-fetches and re-assembles (a new bundle) and then continues
with the same stored `generation_date`.

## 6. Examples

### 6.1 Valid request

```json
{
  "schema": "jasper.run-request",
  "version": 1,
  "request_id": "0b9a6a52-6c1e-4f0e-9a43-6d3c6e2b9a01",
  "idempotency_key": "monthly:2026-10-01_2026-10-28:example-co:run",
  "client_key": "example-co",
  "period_id": "2026-10-01_2026-10-28",
  "action": "run",
  "requested_at": "2026-11-02T01:00:00+00:00",
  "requested_by": "make:monthly-orchestration",
  "options": {"replace_existing": false, "allow_partial_sources": false}
}
```

### 6.2 Successful execution (ready for delivery)

```json
{
  "schema": "jasper.run-response",
  "version": 1,
  "request_id": "0b9a6a52-6c1e-4f0e-9a43-6d3c6e2b9a01",
  "idempotency_key": "monthly:2026-10-01_2026-10-28:example-co:run",
  "replayed": false,
  "mode": "production",
  "run_key": "example-co/2026-10-01_2026-10-28",
  "client_key": "example-co",
  "period_id": "2026-10-01_2026-10-28",
  "state": "ready_for_delivery",
  "outcome": "succeeded",
  "attempt": 1,
  "run_id": "0198c1a2-7b3c-7d4e-8f5a-000000001000",
  "revision": 1,
  "identity": {
    "render_input_sha256": "3f1c0d2e9a7b4c5d6e8f0a1b2c3d4e5f60718293a4b5c6d7e8f9012a3b4c5d6",
    "render_input_format": {"name": "jasper.render-input", "version": 2},
    "generation_date": "2026-11-02",
    "pdf_sha256": "9d8c7b6a5f4e3d2c1b0a9f8e7d6c5b4a39281706f5e4d3c2b1a09f8e7d6c5b4a",
    "pdf16": "9d8c7b6a5f4e3d2c",
    "pdf_bytes": 118765,
    "record_name": "reviewer.0198c1a2-7b3c-7d4e-8f5a-000000001000.9d8c7b6a5f4e3d2c.json"
  },
  "audit": {"outcome": "consistent", "finalized_valid": 1, "verification_complete": true},
  "delivery": {
    "drive_file_id": "1AbCdEfGhIjKlMnOpQrStUvWxYz012345",
    "drive_folder_id": "1ZyXwVuTsRqPoNmLkJiHgFeDcBa987654",
    "file_name": "example-co_2026-10-01_2026-10-28_seo-report_9d8c7b6a5f4e3d2c.pdf",
    "delivery_key": "example-co/2026-10-01_2026-10-28/9d8c7b6a5f4e3d2c/5a1b2c3d4e5f6a7b"
  },
  "error": null,
  "retry": {"eligible": false, "kind": null, "after": null},
  "warnings": [{"code": "source_incomplete", "source": "ga4"}],
  "timestamps": {"requested_at": "2026-11-02T01:00:00+00:00", "updated_at": "2026-11-02T01:07:41+00:00",
                 "finished_at": "2026-11-02T01:07:41+00:00"}
}
```

### 6.3 Validation failure (period does not match the workbook)

```json
{
  "schema": "jasper.run-response", "version": 1,
  "request_id": "5c7e1d44-2a0b-4c5e-8d11-9f3a6b7c8d02",
  "idempotency_key": "monthly:2026-10-02_2026-10-29:example-co:run",
  "replayed": false, "mode": "production",
  "run_key": "example-co/2026-10-02_2026-10-29", "client_key": "example-co", "period_id": "2026-10-02_2026-10-29",
  "state": "requested", "outcome": "rejected", "attempt": 1,
  "run_id": null, "revision": null, "identity": null, "audit": null, "delivery": null,
  "error": {"category": "validation_failed", "code": "period_differs_from_workbook", "stage": "intake"},
  "retry": {"eligible": false, "kind": null, "after": null},
  "warnings": [],
  "timestamps": {"requested_at": "2026-11-02T01:00:00+00:00", "updated_at": "2026-11-02T01:00:02+00:00", "finished_at": "2026-11-02T01:00:02+00:00"}
}
```

A rejected request creates no run row (only a `requests` row with `status = rejected`); `state` is shown as
`requested` for the response only.

### 6.4 Temporary source failure

```json
{
  "schema": "jasper.run-response", "version": 1,
  "request_id": "0b9a6a52-6c1e-4f0e-9a43-6d3c6e2b9a01",
  "idempotency_key": "monthly:2026-10-01_2026-10-28:example-co:run",
  "replayed": false, "mode": "production",
  "run_key": "example-co/2026-10-01_2026-10-28", "client_key": "example-co", "period_id": "2026-10-01_2026-10-28",
  "state": "failed", "outcome": "failed", "attempt": 1,
  "run_id": null, "revision": null, "identity": null, "audit": null, "delivery": null,
  "error": {"category": "source_temporary", "code": "gsc_http_503", "stage": "collecting"},
  "retry": {"eligible": true, "kind": "collection", "after": "2026-11-02T01:20:00+00:00"},
  "warnings": [],
  "timestamps": {"requested_at": "2026-11-02T01:00:00+00:00", "updated_at": "2026-11-02T01:05:00+00:00", "finished_at": "2026-11-02T01:05:00+00:00"}
}
```

### 6.5 Finalization failure (conflict, operator needed)

```json
{
  "schema": "jasper.run-response", "version": 1,
  "request_id": "0b9a6a52-6c1e-4f0e-9a43-6d3c6e2b9a01",
  "idempotency_key": "monthly:2026-10-01_2026-10-28:example-co:run",
  "replayed": false, "mode": "production",
  "run_key": "example-co/2026-10-01_2026-10-28", "client_key": "example-co", "period_id": "2026-10-01_2026-10-28",
  "state": "blocked", "outcome": "blocked", "attempt": 1,
  "run_id": null, "revision": null,
  "identity": {"render_input_sha256": "3f1c0d2e9a7b4c5d6e8f0a1b2c3d4e5f60718293a4b5c6d7e8f9012a3b4c5d6",
               "render_input_format": {"name": "jasper.render-input", "version": 2}, "generation_date": "2026-11-02",
               "pdf_sha256": "9d8c7b6a5f4e3d2c1b0a9f8e7d6c5b4a39281706f5e4d3c2b1a09f8e7d6c5b4a",
               "pdf16": "9d8c7b6a5f4e3d2c", "pdf_bytes": 118765, "record_name": null},
  "audit": null, "delivery": null,
  "error": {"category": "finalization_conflict", "code": "existing_report_differs", "stage": "finalizing"},
  "retry": {"eligible": false, "kind": null, "after": null},
  "warnings": [],
  "timestamps": {"requested_at": "2026-11-02T01:00:00+00:00", "updated_at": "2026-11-02T01:06:10+00:00", "finished_at": "2026-11-02T01:06:10+00:00"}
}
```

`error.code` is the engine's own fixed category (`FinalizationResult.category`) when one exists.

### 6.6 Delivery-ready message (the event that starts Scenario 2)

```json
{
  "schema": "jasper.delivery-ready",
  "version": 1,
  "run_key": "example-co/2026-10-01_2026-10-28",
  "client_key": "example-co",
  "period_id": "2026-10-01_2026-10-28",
  "delivery_key": "example-co/2026-10-01_2026-10-28/9d8c7b6a5f4e3d2c/5a1b2c3d4e5f6a7b",
  "pdf_sha256": "9d8c7b6a5f4e3d2c1b0a9f8e7d6c5b4a39281706f5e4d3c2b1a09f8e7d6c5b4a",
  "pdf_bytes": 118765,
  "drive_file_id": "1AbCdEfGhIjKlMnOpQrStUvWxYz012345",
  "drive_folder_id": "1ZyXwVuTsRqPoNmLkJiHgFeDcBa987654",
  "file_name": "example-co_2026-10-01_2026-10-28_seo-report_9d8c7b6a5f4e3d2c.pdf",
  "audit": {"outcome": "consistent", "finalized_valid": 1, "verification_complete": true},
  "uploaded_at": "2026-11-02T01:07:40+00:00",
  "manifest_sha256": "0a1b2c3d4e5f60718293a4b5c6d7e8f90a1b2c3d4e5f60718293a4b5c6d7e8f9"
}
```

It is sent only after upload confirmation and manifest creation. It carries no recipient and no e-mail address;
recipients come from the `Client Delivery Settings` tab inside Make.

## 7. Versioning

`version` increments only for incompatible changes. A receiver rejects an unknown `schema` or `version`; new
optional members require a version bump because the schemas are closed.
