# R8.5 — Security architecture and failure recovery

Design only. Labels: *Implemented and tested*, *Designed but not implemented*, *Proposed*, *Unverified*, *Blocked*. Facts about Make.com
or Google products are **Unverified** until checked against current documentation and a live account in R8.7+.

## Part 1 — Security architecture

### 1. Principles
1. The runner is **outbound-only**. It opens no listening socket and accepts no connection from Make, Google or the internet. (Today:
   *Implemented* — no network listener exists in `src/`; a source check test is proposed for R8.6.)
2. Each component gets its **own identity with the least rights it needs**; the runner's reporting identity stays read-only.
3. **Secrets live only in the runner's local secret store** (and Make's own secret storage). They never appear in sheets, payloads, logs, e-mails,
   source code or Git.
4. **Minimise what crosses a boundary**: ids, hashes (the 16-hex form where enough), fixed codes. No client content in notifications.
5. A failed security check **fails closed** (reject, alert, keep the data).

### 2. Authentication of the runner → Make channel
* Make's custom webhook URL contains an unguessable token (treated as a secret). On top of it every request carries
  `X-Jasper-Timestamp` and `X-Jasper-Signature: v1=<hex HMAC-SHA256(key, timestamp + "." + body)>` and `X-Jasper-Key-Id`.
* SCN-3 rejects (HTTP 401, counted, alert after N) when the signature is wrong, the key id is unknown, or the timestamp is more than 5 minutes
  off. A captured request cannot be replayed beyond that window, and replays inside it are absorbed by `dedupe_key`.
* If Make cannot compute an HMAC natively (**Unverified**), the fallback is a static secret in a header **plus** the secret URL, over HTTPS only;
  the weaker mode is documented as a known limitation and replay protection then relies on `dedupe_key` + `sequence`.
* HTTPS only, certificate verification on, no redirects followed, TLS 1.2 or later (the Python/OS default). A plain-HTTP URL is refused by
  configuration validation (future `runner_config` change).
* **Secret management on the runner** (**Designed but not implemented; Blocked** on the credentials-location work): the URL and the signing key are
  stored in the Windows Credential Manager (DPAPI, per account; needs the `keyring` package, not installed) or in an owner-only file in
  `%LOCALAPPDATA%\JasperSEOReporting\credentials\`. They are **not** in `runner.yaml` (the loader already refuses secret-looking keys and values).
* **Rotation:** two keys valid at once (`key id` selects) for 24 h: create the new key in Make, add it, switch the runner, retire the old key.
  Rotate every 12 months, on staff change, or at once on suspicion; rotate the webhook URL by recreating the webhook. A rotation is a person's
  action (no automation).

### 3. Make's access to Google
| Make identity | Needs | Proposed rights | Status |
|---|---|---|---|
| Google connection used by Make | operational workbook: read/write specific tabs; reporting workbook: read the `Client Configuration` tab columns it needs; Drive: read the controlled folder; Gmail: send | A **dedicated Workspace user** (not a personal account) that is a member only of the files it needs; **Editor** on the operational workbook, **Viewer** on the reporting workbook and on `Jasper SEO Reports/`, Gmail send only | Proposed; the exact OAuth scopes Make requests are **Unverified** (Make's Google modules may request broad Drive/Sheets scopes; account-level isolation compensates) |
| Gmail sending | send as the Jasper reporting mailbox | the mailbox owner authorises the connection; no read access to mail | Proposed; **Unverified** whether a send-only scope is offered |

Make never holds the runner's reporting token, the runner's Drive token or the webhook signing key.

### 4. Runner's access to Google (current and later)
| Use | Scope today | Scope needed later | Notes |
|---|---|---|---|
| Reporting workbook, Search Console, Analytics | `spreadsheets.readonly`, `webmasters.readonly`, `analytics.readonly` — **Implemented, unchanged** | same | one cached token for one Windows account; stays inside OneDrive until relocation (**Blocked** on approval) |
| Read the operational workbook (queue, settings, delivery) | covered by `spreadsheets.readonly` if the runner's Google user can view it | same | the runner **never writes** a sheet |
| Upload to Drive (R8.8) | none | `drive.file` (create/read files the app created) — **a new consent, not approved** | Prefer a **separate OAuth token** so the reporting token remains read-only. Unverified: whether `drive.file` can share files/folders it created with Make's account, and whether the runner can find a root folder that a human created. Alternatives: the runner creates the root folder, or a Shared Drive with a narrow-scope account (decision D-3) |
| Sending e-mail | none; the runner never sends | none | |

Changing scopes or the Google Cloud project requires an explicit decision; none is made here.

### 5. Operational workbook protection
* The reporting workbook stays unchanged; the operational workbook is separate, shared by name with: Make's user (Editor), the runner's user
  (Viewer), reviewers (Editor on their cells only), operators (Editor).
* Protected ranges: request rows append-only after creation; `Execution History` append-only; `Delivery` approval cells editable only by the
  reviewer group; `pdf_sha256`, `delivery_key`, `drive_*` and `delivery_status` editable by Make only. Protected ranges prevent accidents; they
  are not a security boundary against a malicious editor, so every consumer **re-verifies** (the approval re-check in SCN-6 against the Drive
  property and the ledger-confirmed upload).
* Secrets are never written to any cell. Recipient addresses live only in `Client Delivery Settings`.

### 6. Approval authorization and audit
* Approvers are a named group maintained in the workbook sharing settings. `approved_by` is a label; the authoritative record of who edited
  is the sheet's revision history (kept; no purge). **Proposed** hardening: SCN-4B reads the revision author and checks it against an allow-list
  (**Unverified**).
* An approval authorises exactly `(delivery_key, pdf_sha256)`, requires the reviewer's `approved_pdf16` confirmation, is void after
  `expires_at`, and is void if a newer delivery exists for the period.
* **Audit trail** for one report: ledger events (every state change and operator action with operator and reason code), `deliveries` row,
  manifest (local and in Drive), `Execution History` rows, `Delivery` row history and revisions, Make execution logs (minimised), e-mail
  provider message reference. None can be deleted by automation.

### 7. Client data separation
One Drive folder per `client_key`; one client per message, per approval and per e-mail; no CC to other clients; Make data-store keys are
`delivery_key`s; notifications contain `client_key` (an internal slug) but never recipient addresses, names of contacts or report content.

### 8. Notification and log data minimisation
| Channel | May contain | Must not contain |
|---|---|---|
| Operator notification (e-mail/chat) | `client_key`, `period_id`, `pdf16`, state, severity, fixed codes, action hint, a restricted internal link to the review file/row | tokens, secrets, recipient addresses, contact names, full hashes, file paths, exception text, report content |
| `Execution History` | ids, states, codes, `pdf16` | anything above |
| Runner logs (R8.4.6) | sanitized JSON with identifiers and codes | secrets (redacted by pattern), paths (reduced to file names) |
| Client e-mail | fixed template text, the contact name (optional), period dates, the sender's contact block, the PDF | internal ids, other clients, hashes, error text |
| Make execution data | metadata; the PDF only transiently during SCN-6 | — (**Unverified**: Make's data-retention settings must be set to the shortest retention and to avoid storing attachments; check before R8.9) |

### 9. Secrets inventory (current vs later)
| Secret | Where it lives | Status |
|---|---|---|
| Google OAuth token + client secret (reporting, read-only) | project folder (OneDrive) | exists; **relocation Blocked** — see [credential_relocation_procedure](credential_relocation_procedure.md) |
| Drive write token (later) | local credentials folder, separate file | Designed but not implemented |
| Webhook URL + signing key | runner secret store; Make scenario settings | Designed but not implemented |
| Make Google/Gmail connections | Make's connection store | Designed but not implemented |
| Mailbox/app password (if SMTP were chosen instead) | not applicable; OAuth connection preferred | Proposed |

### 10. Security checklist for sign-off (to run in R8.7 and R8.9)
No listening port (`netstat -ano` shows none for the runner process); runner token files owner-only; signature failures alert; no secret in the
workbook (search); no secret in logs (sanitizer tests plus a grep on real logs); webhook secret rotation rehearsed once; approval re-check fails
when the PDF hash is altered in the sheet; delivery fails when the recipient set changes after approval; sending is impossible without the
`Delivery` approval; Make execution data retention verified.

## Part 2 — Failure recovery and reliability

**Invariants preserved in every case** (existing R8.3/R8.4 behaviour): the ledger is authoritative; no automation deletes or overwrites a
finalized report, record, sidecar, snapshot or Drive file; a partial file is never removed automatically; an audit failure blocks delivery and cannot
be bypassed by an acknowledgement; generation date and render identity survive retries; a replacement is a new linked run with its own hash.

| # | Failure | Detection | Automatic handling | Operator action | Effect on ledger / data |
|---|---|---|---|---|---|
| 1 | **Runner unavailable for hours** | SCN-7: `queued` > 12 h (SEV-2), > 24 h (SEV-1); optional heartbeat | rows wait; on return, oldest first; rows past `expires_at` are refused (`request_expired`) | restore the machine; re-issue expired rows | none; nothing was started |
| 2 | **Make creates a request twice** | same `idempotency_key` | ledger replay (second is `replayed`, no run); different key for the same period ⇒ answered `already_complete`/`run_in_progress` | none (SEV-4 note if repeated) | one run only |
| 3 | **Runner crashes during collection** | next tick finds run in `collecting` with a dead holder | `recover_interrupted` ⇒ `requested`; run restarts collection | none | attempt marked `interrupted`; generation date kept |
| 4 | **Crash during rendering** | same, state `rendering` | back to `requested`; the open sidecar (if any) is reused so the render identity and date survive; the render watchdog bounds hangs | none | partial draft files kept, never deleted |
| 5 | **Crash during finalization** | state `finalizing` | back to `rendered`; finalization is idempotent (stored `finalized_at`, render input); a crash right after the engine finalized is recovered without a second finalization | none | one record, one PDF |
| 6 | **Finalized but status notification fails** | outbox row stays pending | retry with backoff up to 7 days; later states supersede earlier rows (coalescing) | none; if `undeliverable` after 7 days, the daily terminal sweep or a Make `status` request heals it | ledger already correct |
| 7 | **Status webhook receives the same event twice** | `dedupe_key` seen | acknowledged `200`, ignored | none | none |
| 8 | **Make scenario stops midway** | missing `Execution History` row / row mismatch; Make error notification | redelivery re-runs the idempotent steps (dedupe key is written last; appends keyed by it); SCN-7 `status` reconciliation | re-run the failed Make execution if offered | ledger unaffected |
| 9 | **Approval delayed** | `pending` > 3 days ⇒ reminder; > 14 days ⇒ `expired` | reminders; expiry voids the approval | request a new delivery (a `publish` refresh request; the same Drive file is reused, no re-render) | PDF unchanged |
| 10 | **Approval rejected** | `approval_status = rejected` + code | `delivery_status = cancelled`; SEV-2 | fix the cause; if the report changes, a `supersede` request creates a new generation, hash and delivery | original run untouched; replacement is a new run |
| 11 | **PDF changes after approval** | a new `delivery-ready` with a different `pdf_sha256`; or SCN-6 re-check mismatch | SCN-4A expires the older pending/approved row; SCN-6 refuses to send when the hash differs from the approved one | review and approve the new PDF | old Drive file stays; the new PDF is a distinct file |
| 12 | **Drive upload succeeds but acknowledgement fails** | `publishing`/`publish_failed` run; Drive has a file with the `pdf_sha256` property | retry **looks up first** and reuses the file; confirmation completes; `delivery-ready` is sent | none | no second upload; mismatching file is reported, not deleted |
| 13 | **E-mail sent but delivery status not recorded** | `delivery_status = sending` older than 15 min | **no automatic resend**; SEV-2 | check the sent folder; set `sent` or `failed` by hand; resending is a deliberate operator act | at-least-once with a guard; exactly-once not guaranteed |
| 14 | **Client configuration becomes invalid** | `client_loader` rejection (fixed code), `config_invalid`, SCN-1 pre-checks | the client is rejected/skipped; other clients continue; a run in progress stops at its next validation and fails | correct the workbook; re-request | no state corruption; a rejected row is reported once |
| 15 | **Google credentials expire or are revoked** | `auth_required` (`auth_*` code) | the batch stops (existing rule), remaining requests stay unstarted, SEV-1 status event is still sent; no browser opens | an attended `auth setup` as the runner account | runs resume from their resume state |
| 16 | **Recipient set changes after readiness** | `recipient_set_hash16` ≠ `delivery_key` suffix | SCN-4A/6 cancel the delivery row (`recipient_changed`) | issue a `publish` refresh request | Drive file reused |
| 17 | **Ledger and workbook disagree** | reconciliation | ledger wins for report facts; `Delivery` wins for approval/delivery facts | investigate anomalies | see protocol §8 |
| 18 | **Disk full / ledger unavailable** | `abort_reason` | batch stops; SEV-1 | free space / repair; restart | no partial ledger writes (transactions) |

**What is deliberately not automated:** deleting or replacing a finalized report, re-sending an ambiguous e-mail, approving, rotating secrets,
changing recipients, creating a replacement report.

## Known risks (security and reliability)
1. The webhook is only as private as its URL and key; signing mitigates but Make's capabilities are **Unverified**.
2. Make processes the PDF during e-mail attachment and may retain execution data (decision D-5; verify retention).
3. Sheet-based approval can be edited by any editor; mitigated by protected ranges, re-verification, and revision history, not eliminated.
4. At-least-once e-mail delivery can duplicate a client e-mail after an ambiguous failure (guarded, resolved by a person).
5. A lost or compromised runner machine holds client data and credentials; relocation, owner-only ACLs and token revocation are the response.
6. Credentials remain inside OneDrive until the relocation stage is approved.
7. A single runner is a single point of failure; offline detection depends on SCN-7 and the optional heartbeat.
