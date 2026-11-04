# R8.5 — Make.com scenario specifications

Design only; **no scenario exists**. Labels (defined in [r85_make_architecture.md](r85_make_architecture.md) §0): *Implemented and tested*,
*Designed but not implemented*, *Proposed*, *Unverified*, *Blocked*. Make.com module names, operation counting, trigger behaviour and limits
below are **Unverified** and must be checked against current Make documentation before R8.7. Module names are descriptive.

## 0. Scenario inventory

The seven functional areas of the brief map onto **six physical Make scenarios** (a trigger per scenario, and fewer scenarios cost fewer
operations):

| ID | Scenario | Trigger | Functional area |
|---|---|---|---|
| SCN-1 | Monthly reporting scheduler | schedule (daily) | 1 scheduler (also writes the queue of 2) |
| SCN-3 | Status receiver | custom webhook | 3 status; routes to 4 and 7 |
| SCN-4A | Approval request | internal webhook from SCN-3 | 4 approval (request) |
| SCN-4B | Approval decision | row-change trigger on `Delivery` (or instant, Unverified) | 4 approval (decision) → starts 6 |
| SCN-6 | Client e-mail delivery | called by SCN-4B (internal webhook) | 6 delivery |
| SCN-7 | Monitoring and queue watchdog | schedule (every 4 h) | 7 monitoring, and the queue maintenance of 2 |

Area **2 (request queue)** is a *data structure and protocol* (the `Report Requests` tab, [protocol](r85_communication_protocol.md)), not a
scenario; SCN-1 creates rows, SCN-7 expires/escalates them, the runner consumes them. Area **5 (Drive)** is performed by the **local
runner's publish stage** (it is the only component that can read the PDF; Make cannot reach the Windows machine); Make's part is
verification in SCN-4A/SCN-6. Report generation and delivery are separate: delivery scenarios start only from a `delivery-ready` message.

Shared rules for every scenario: (1) validate `schema`/`version` first and discard unknown versions with an alert; (2) a per-item error
handler that records the error and continues with the next item; (3) notifications carry only ids and fixed codes
([security doc §8](r85_security_and_reliability.md)); (4) no module ever receives a runner credential or Google token; (5) every write is
idempotent or guarded by a claim.

## 1. Operational workbook (additions to the R8.2 contract)

A **separate spreadsheet** (approved in R8.2; the reporting workbook stays unchanged and is never written by automation). Tabs as in
[google_sheets_contract](google_sheets_contract.md) §3–§6 with these **proposed additions** (all *Designed but not implemented*):

`Report Requests` — add: `operator` (text, for operator actions), `reason` (the eight fixed reason codes, for operator actions),
`expires_at` (timestamp), `target_run_key` (text, for `supersede`/`cancel`/`acknowledge`/`retry`), `ack_at`, `ack_outcome`.
`rankings_ready` stays a **Make-side gate only** (finding F2). Allowed `action` values written by Make: `run`, `retry`, `status`,
`acknowledge`, `cancel`, `supersede`, `publish` (once R8.8 exists). **Never** `dry_run` (finding F1) and never `validate` for production.

`Client Delivery Settings` — add two plain text columns (no formulas): `rankings_ready_period` (set by the operator to the `period_id` whose rankings are
final; this is the Make-side "rankings ready" gate, finding F2) and `last_requested_period` (written by SCN-1 after it queues that period).

`Delivery` — add: `approved_pdf16` (the 16-hex confirmation the reviewer types or selects), `supersedes_delivery_key`, `expires_at`,
`approval_requested_at`, `email_attempt_ref` (opaque). Existing columns unchanged.

Protected ranges: request rows are append-only for Make; reviewers may edit only `approval_status`, `approved_by`, `approved_at`,
`approved_pdf16`, `rejection_reason_code` on `Delivery`; `Execution History` is append-only. The runner's Google identity has **read-only**
access to the operational workbook.

## 2. SCN-1 — Monthly reporting scheduler

**Purpose.** Create the month's `run` requests, once per client per period, without a human.

**Trigger.** Schedule: **daily at 06:00 local time** (rationale: due dates are per client — explicit `Report End Date` — so "due" is
evaluated daily; one execution per day costs one trigger operation plus lookups, far less than hourly; a request created a few hours
after a client becomes due is acceptable for a monthly deliverable).

**Inputs (read-only).** Operational workbook: `Client Delivery Settings` (candidates), `Report Requests` (duplicate check). Reporting workbook
`Client Configuration`, read for *candidates only*: `Client Name`, `Status`, `Report Start Date`, `Report End Date` (only these columns are used;
Make needs view access to that workbook — decision D-7). *The existing workbook is read, never written;
`Manager Email`/`Manager Phone` are not read at all and are never a recipient.*

**Steps.**
1. Search `Client Delivery Settings` for rows with `settings_status = enabled`, `rankings_ready_period` non-empty **and** different from
   `last_requested_period` — i.e. only clients the operator has flagged (one search per day instead of one lookup per active client).
2. Iterate the candidates. For each, read its `Client Configuration` row by exact name (`client_name`); a missing row, or `Status` not `active`
   (case-insensitive), ⇒ notification `client_config_missing` (SEV-3) and skip.
3. **Due test (all must hold):** `settings_status = enabled`; `reporting_frequency <> manual`; both dates present and
   `end − start = 27 days` (28 inclusive days — pre-check mirroring `periods`); `Report End Date + 3 days <= today` (GA4 processing lag;
   the code's `RECENT_DAYS = 2`, plus one day of margin); `rankings_ready_period` equals this `period_id` (the operator flag).
4. Compute `period_id = <start>_<end>` and `idempotency_key = monthly:<period_id>:<client_key>:run`.
5. **Duplicate prevention (two layers):** (a) search `Report Requests` for that key; if found, skip; (b) the runner's ledger rejects a
   repeat by the same key (replay) and by the same `(client, period)` with a different key (admission by the latest run).
6. Add the row (then write `last_requested_period = period_id` on the settings row so the candidate search no longer returns it): `request_id` (new UUID), `action = run`, `requested_at` (UTC), `requested_by = make:monthly-scheduler`,
   `expires_at = requested_at + 7 days`, `request_status = queued`.
7. Summarise: one notification per execution **only if** something was queued or an error occurred (no daily "nothing to do" message).

**Error handling.** Per-client errors are caught by an error handler that continues the iteration; Sheets quota/temporary errors use the
platform retry then notify SEV-3; a failed whole execution notifies SEV-2 (a missed month is the real risk, caught also by SCN-7's
"period due but no request" check).

**State.** Rows only; no data store needed.

**Out of scope for SCN-1:** corrections. A replacement report is an **operator-created** request row (`supersede`), never automatic.

## 3. Area 2 — Reporting request queue (data structure + protocol)

The queue is the `Report Requests` tab. Each row maps 1:1 to a `jasper.run-request` ([data contracts](r85_data_contracts.md) §1):

| Sheet column | `RunRequest` field | Notes |
|---|---|---|
| `request_id` | `request_id` | lower-case UUID, unique |
| `idempotency_key` | `idempotency_key` | key grammar §3.1; unique |
| `client_key` | `client_key` | stable key from `config/clients.yaml` |
| `period_id` | `period_id` | must equal the workbook's explicit dates, exactly 28 days |
| `action` | `action` | see §1 |
| `requested_at`, `requested_by` | same | `make:<scenario>` or `operator:<initials>`; no e-mail addresses |
| `replace_existing` | `options.replace_existing` | TRUE only by an explicit operator choice; mandatory TRUE for `supersede` |
| `operator`, `reason` | `options.operator`, `options.reason` | mandatory for `acknowledge`/`cancel`/`supersede`, refused otherwise (existing rule) |
| `target_run_key` | *(future)* `options.target_run_key` | additive optional; the runner rejects `target_not_latest` if it is not the latest generation |
| `expires_at` | *(future)* not part of the request | checked by the runner against its clock; reject code `request_expired` (additive) |
| `request_status`, `last_state`, `last_status_at`, `ack_*` | — | Make-maintained mirrors; the runner never reads them as truth |

### 3.1 Idempotency-key grammar (allowed characters `[A-Za-z0-9:._/-]`, max 128; **`~` is not allowed**, so generations are written `g<N>`)

| Intent | Key | Why it is safe to repeat |
|---|---|---|
| monthly run | `monthly:<period_id>:<client_key>:run` | one per client per period |
| manual run | `manual:<period_id>:<client_key>:<yyyymmddThhmmZ>` | operator-chosen instant; separate intent |
| operator retry | `retry:<client_key>/<period_id>:g<N>:a<attempt>` | one retry per attempt number of generation N |
| acknowledge | `acknowledge:<client_key>/<period_id>:g<N>:v<state_version>` | one per blocked state (the envelope's `sequence` is the version) |
| cancel | `cancel:<client_key>/<period_id>:g<N>` | one per generation |
| supersede | `supersede:<client_key>/<period_id>:g<N>` | N = generation being replaced; a repeat replays, a *second* supersede of the replacement uses `g<N+1>` |
| publish (refresh) | `publish:<client_key>/<period_id>:g<N>:<recipient_set_hash16>` | one per recipient set |
| status (reconcile) | `status:<client_key>/<period_id>:<yyyymmddThhmmZ>` | read-only; bucketed by the minute |

### 3.2 Request states (Make-maintained `request_status`, mapped from the runner's state; no new names)

| `request_status` | Set when | Source |
|---|---|---|
| `queued` | row created | SCN-1 / operator |
| `submitted` | the runner's **ack** (`accepted`) arrives | ack envelope |
| `running` | `run-response.state` in `collecting … auditing, publishing` | status envelope |
| `completed` | state `audited` and no delivery pending, or `delivered` | status envelope |
| `failed` / `blocked` | `outcome = failed|blocked` | status envelope |
| `rejected` | ack with `decision = rejected` | ack envelope |
| `cancelled` | state `cancelled` or expiry by SCN-7 (`error_code = request_expired`) | envelope / SCN-7 |

### 3.3 Duplicate polling and processing

Polling is **read-only**, so repeated or overlapping polls cannot change anything. A row is processed at most once because the ledger
registers each `idempotency_key` once (`register_request`): a second sight is a *replay* answered with the stored response, never a second
run. Two runner processes cannot work at the same time (`runner:global` lock). A row edited after creation (same key, different client/
period/action) is refused by the ledger (`idempotency_key_reused`) and reported.

## 4. SCN-3 — Local runner status receiver

**Purpose.** Let Make observe and record execution; route events.

**Trigger.** Custom webhook (secret URL). **Response** `200` immediately with `{"received": true}`; processing after the response where
the platform allows (**Unverified**).

**Steps.**
1. **Authenticate** (security doc §2): header `X-Jasper-Signature` = `v1=<hex HMAC-SHA256(secret, timestamp + "." + raw body)>` and header
   `X-Jasper-Timestamp`; reject if the timestamp is more than 5 minutes off or the signature differs. Reject = HTTP `401`, counted, never
   processed. Fallback if HMAC cannot be computed in Make (**Unverified**): static token header over HTTPS plus the secret URL.
2. **Validate** the envelope: `schema = jasper.status-envelope`, `version = 1`, `kind` known, payload `schema/version` match the kind.
   Unknown → `400` + SEV-3 notification (contract drift).
3. **Deduplicate** on `dedupe_key` (a Make data store or a lookup in `Execution History`; data store is cheaper per check — **Unverified**).
   Duplicate → `200`, no further action. **Out-of-order guard:** ignore a `run_status` whose `sequence` is lower than the stored maximum for
   that `run_key` (still `200`).
4. **Record:** append to `Execution History`; update the request row mirrors (`request_status`, `last_state`, `last_status_at`).
5. **Route** by kind/outcome:
   * `request_ack` accepted → `submitted`; rejected → SEV-3 notification with the reject code;
   * `run_status` `failed` + `retry.eligible` → no alert unless attempts exhausted (the runner retries on its own);
   * `failed` not eligible → SEV-2; `blocked` → SEV-2 (operator action needed: acknowledge/cancel/supersede); `auth_required` → **SEV-1**;
   * `delivery_ready` → call SCN-4A (internal webhook);
   * `delivered` mirror → close the request row.
6. Return.

**Reconciliation and offline behaviour.** See the protocol document §7–§9. Make unavailable ⇒ the runner's outbox retries; nothing is lost
(the ledger is authoritative). Late duplicates are harmless (dedupe).

**Status vocabulary** — exactly the existing run states and outcomes. The product milestones map as follows:

| Product milestone | Envelope | State / outcome in the payload |
|---|---|---|
| Request accepted | `request_ack` | outcome `in_progress`, state `requested` (accepted) |
| Execution started | `run_status` | state `collecting` |
| Data collection completed | `run_status` | state `rendering` (entering rendering means collection ended) — *milestone, optional* |
| Rendering completed | `run_status` | state `rendered` |
| Finalization completed | `run_status` | state `finalized` — *optional* |
| Audit completed | `run_status` | state `audited`, `audit.outcome = consistent`, identity block with the PDF hash |
| Blocked / failed | `run_status` | `blocked` / `failed` with `error` and `retry` |
| Ready for approval | `delivery_ready` (+ `run_status` `ready_for_delivery`) | after the Drive upload is verified |

Default **milestone mode** sends: ack, `collecting`, `rendered`, `audited`, `ready_for_delivery` (+ `delivery_ready`), `failed`, `blocked`,
`cancelled`, `delivered`. Full mode (every transition) is available for a pilot (cost: [cost doc](r85_cost_and_operations.md)).

## 5. SCN-4A / SCN-4B — Human approval

### SCN-4A — approval request
1. **Entry:** internal webhook from SCN-3 with a validated `jasper.delivery-ready` (its `audit` already proves a passed audit — the contract
   refuses to describe anything else).
2. **Idempotency:** a `Delivery` row with this `delivery_key` exists → stop.
3. **Supersession rule:** if another `Delivery` row for the same `(client_key, period_id)` has `approval_status = pending`, set it to
   `expired` with reason `superseded` **before** creating the new row, so an obsolete PDF can never be approved after a replacement exists.
4. **Recipient validation:** read `Client Delivery Settings` for the client: `settings_status = enabled`, `delivery_preference =
   approve_then_email`, non-empty `recipient_to`, every address syntactically valid (basic pattern; no live check), count ≤ 5; recompute
   `recipient_set_hash16` and require it to equal the `delivery_key` suffix. Mismatch ⇒ `Delivery` row `cancelled` with
   `last_error_category = delivery_failed`, `error_code = recipient_changed`; SEV-2; the fix is a `publish` refresh request.
5. **Verify the file:** Drive "get file" for `drive_file_id`: size = `pdf_bytes`; parent = `drive_folder_id`; `appProperties.pdf_sha256` =
   `pdf_sha256`; `md5Checksum` present (kept for SCN-6). Any mismatch ⇒ `Delivery` row `cancelled` (`drive_verification_failed`), SEV-2.
6. **Create the `Delivery` row** (`approval_status = pending`, `delivery_status = not_started`, `expires_at = now + 14 days`).
7. **Notify the reviewer group** with: client key, period id, `pdf16`, audit status, "approve in the sheet row", a restricted Drive review
   link. No attachment, no recipient addresses, no full hash.

### SCN-4B — approval decision
1. **Trigger:** change on `Delivery` rows (polling "watch rows" on a schedule of **every 15 minutes during working hours** — approvals are
   human-speed; or an instant trigger if available — **Unverified**).
2. **Process only rows whose `approval_status` just became `approved` or `rejected`** and that have not been processed (a `decision_recorded_at`
   marker avoids reprocessing).
3. **Approve path — all must hold or the decision is voided and flagged:**
   `approved_by` and `approved_at` present; `approved_pdf16` equals `pdf_sha256[:16]` of **this row** (the reviewer confirms the version they
   looked at); the row is not `expired`; no newer pending/approved row exists for the same period (the newer one wins).
   Then call SCN-6 (internal webhook) with the `delivery_key`.
4. **Reject path:** `rejection_reason_code` mandatory (`content_error`, `wrong_recipient`, `data_issue`, `other`); set `delivery_status =
   cancelled`; notify SEV-2 with the code; the operator decides: fix data and request a `supersede`, or stop. A rejection never triggers
   regeneration by itself.
5. **Expiry:** SCN-7 sets `pending` rows older than 14 days to `expired` and notifies; an expired approval is void and needs a new request
   (rationale: data freshness and reviewer attention; the period is fixed, so lateness does not change content, but a stale pending
   approval is a safety risk when a later replacement exists).

**Binding rules.** The approval authorises exactly one `(delivery_key, pdf_sha256)` pair. A replacement PDF has a new key and a new
`Delivery` row; an old `approved` status is never copied. Editing `pdf_sha256` on the row is blocked by protected ranges and detected
by SCN-6's re-check against the Drive file property.

## 6. Area 5 — Google Drive upload (runner publish stage; Make verifies)

**Owner.** The local runner's `publishing` stage ("publisher", **Designed but not implemented**, R8.8). It runs only for a run in state
`audited`, never invokes rendering or finalization, and is separate from report generation. **Blocked** on approval of a Drive write scope.

**Folder hierarchy** (R8.2 delivery architecture §3, unchanged):
`Jasper SEO Reports/<client_key>/<period_id>/` containing `<client_key>_<period_id>_seo-report_<pdf16>.pdf` and
`<client_key>_<period_id>_manifest_<pdf16>.json`. Folder names are stable keys, never client names; no public links; sharing by account.

**Procedure** (R8.2 §4): re-check preconditions inside the publisher (state `audited`; file regular; SHA-256 and size equal the ledger and the
reviewer record); ensure folders; **look up** by `appProperties.pdf_sha256` first (reuse an identical file, never upload twice); resumable
upload; read back `id, size, md5Checksum, parents, appProperties`; compare size, MD5 of the local file, parent; write the manifest;
one ledger transaction records the delivery, moves `publishing → ready_for_delivery` and enqueues `delivery-ready`.

**Duplicates.** Same bytes ⇒ reuse. Same name, different bytes ⇒ `blocked` (`publish_conflict`). A replacement has a different `pdf16`, so
it is a distinct file in the same folder; the old file stays.
**Retry/recovery.** `publish_failed` is retryable (`retry.kind = publish`); a retry looks the file up first, so an upload that succeeded
but was not acknowledged is *found*, not repeated. A mismatching uploaded file is **not deleted**; it is reported.
**Permissions.** The runner's Drive identity: create/read in the controlled root only; no delete/overwrite call exists in the code path
(source check planned). Make: read-only on the root. Reviewers: view. Clients: no Drive access.
**Retention.** Kept ≥ 24 months (**decision pending**); manual, logged cleanup only. **Audit trail:** ledger events, `deliveries` row,
manifest (also in Drive), `Execution History`.

## 7. SCN-6 — Client e-mail delivery

**Recipient source.** Only `Client Delivery Settings.recipient_to/cc` in the operational workbook. **Never** `Manager Email`.
**Entry:** internal webhook from SCN-4B with a `delivery_key`; SCN-6 re-reads the row and re-verifies everything itself (it trusts nothing
passed in).

**Steps.**
1. **Re-verify (G4–G6):** `approval_status = approved` with `approved_by/at`; `approved_pdf16` = `pdf_sha256[:16]`; row not `expired`;
   `delivery_status = not_started` (or `failed` with attempts < 3); recipients still hash to the key's suffix; Drive file still has
   `appProperties.pdf_sha256` = row hash, size = `pdf_bytes`, `md5Checksum` = the value recorded at SCN-4A.
2. **Claim before sending:** update the row to `delivery_status = sending`, `sending_started_at = now`, `delivery_attempts + 1`. If a row is
   already `sending` with `sending_started_at` newer than 15 minutes → stop. If older than 15 minutes → **do not retry automatically**
   (ambiguous: the mail may have left); raise SEV-2 and require the operator to decide.
3. **Download** the PDF from Drive by `drive_file_id` (the same file; the runner is never invoked).
4. **Send** through the sending mailbox connection: fixed template, subject and body containing only the client contact name (optional
   `recipient_contact_name`), the period dates and the sender's contact block; **attachment** of the PDF (size is far below mailbox limits;
   decision D-5). Variant "corrected report" when `supersedes_delivery_key` is set.
5. **Record:** `delivery_status = sent`, `sent_at`, `mail_message_ref` (opaque provider id), append `Execution History`; notify the operator
   (SEV-4 digest).
6. **Failure after the claim:** `delivery_status = failed`, `last_error_category = delivery_failed`, error code (fixed list:
   `mail_connection_failed`, `mail_rejected`, `drive_download_failed`, `attachment_too_large`, `recipient_invalid`); automatic retries at
   15 min, 1 h and 4 h (rationale: transient provider errors clear within minutes to hours; three attempts bound duplicates and noise);
   then SEV-2.

**Idempotency and duplicate-send prevention.** `delivery_key` is unique in `Delivery`; claim-before-send; the ambiguity guard; SCN-4B
passes only an approved key once (marker). **At-least-once with a guard**: exactly-once cannot be guaranteed if the scenario dies between
"mail accepted" and "row updated" — that case ends in `sending` older than 15 minutes and is resolved by a person (check the sent folder).
**Delivery history:** `Delivery` rows plus `Execution History`; the runner mirrors `sent`/`failed`/`cancelled` read-only into
`ledger.deliveries` and records `delivered` for the run.
**Human approval enforcement:** SCN-6 has no other entry point than SCN-4B's validated call and re-checks approval itself.

## 8. SCN-7 — Monitoring and queue watchdog

**Trigger:** schedule **every 4 hours** (rationale: the slowest acceptable detection of a stuck month is hours, not minutes; hourly would
multiply fixed operations ~4×; a SEV-1 condition is pushed immediately by SCN-3, not discovered by polling).

**Checks (read-only on the workbook):**
1. `Report Requests` rows `queued` older than **12 h** ⇒ runner offline or not polling (SEV-2; SEV-1 after 24 h).
2. Rows `submitted/running` with no status for more than **6 h** ⇒ stuck; enqueue a `status` request row for reconciliation (once per row per 6 h).
3. Rows with `expires_at` passed and still `queued` ⇒ mark `cancelled` (`request_expired`), SEV-3.
4. `Delivery` rows `pending` older than 3 days ⇒ reminder (SEV-3), older than 14 days ⇒ `expired` (SEV-3).
5. `Delivery` rows `sending` older than 15 minutes ⇒ ambiguity alert (SEV-2).
6. Active clients whose period is due (SCN-1 rule) but have no request ⇒ SEV-3 (missed month).
7. Optional: last `runner-heartbeat` older than 26 h ⇒ SEV-1/2 (**Proposed**; heartbeat is optional).

**Notification catalogue** (event → default severity → trigger → recipient group). Content rules in the security doc §8.

| Event | Sev | Source | Notes |
|---|---|---|---|
| `credentials_missing` / `auth_required` (any `auth_*`) | **1** | runner status (`error.category = auth_required`) | batch stops; person must run `auth setup` |
| `google_permission_error` (`source_denied`, 403/404) | 2 | runner | per client; not retried automatically |
| `google_quota_or_temporary` (`source_temporary`) | 3 (2 after attempts are exhausted) | runner | runner retries by itself |
| `client_config_missing` / invalid | 3 | SCN-1 / runner `config_invalid` | |
| `ranking_data_missing` | 3 | runner reject codes from `client_loader` (`keywords_missing`, `ranking_invalid`) | |
| `render_failed` (incl. `render_timeout_*`) | 3 | runner | retryable |
| `finalization_failed` / `finalization_conflict` | 3 / 2 (blocked) | runner | |
| `audit_failed` | 2 | runner (`blocked`) | never delivered; operator reviews |
| `runner_offline` / `request_timeout` | 2 → 1 | SCN-7 | |
| `lock_conflict` (`lock_unavailable`, `run_in_progress`) | 4 (2 if repeated 3×) | runner | normally self-resolving |
| `drive_upload_failed` (`publish_failed`) | 2 | runner | retryable |
| `approval_pending` | 4 → 3 after 3 days | SCN-4A/7 | |
| `approval_rejected` | 2 | SCN-4B | |
| `email_delivery_failed` | 2 | SCN-6 | after 3 attempts |
| `delivery_ambiguous` | 2 | SCN-7 | person must check |
| `disk_full` / `ledger_unavailable` (batch aborts) | **1** | runner (`abort_reason`) | |

**Severity and escalation.** SEV-1 critical: act now — first message immediately, repeat every 4 h until acknowledged, second channel
(SMS/chat) if configured. SEV-2 high: same business day; repeat daily. SEV-3 medium: included in a daily digest. SEV-4 info: weekly/daily
digest. Escalation: unacknowledged SEV-2 for 24 h becomes SEV-1.
