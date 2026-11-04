# R8.2 — PDF storage and delivery architecture (design)

> **As-built status (updated R8.4.6).** This document was written at R8.2 as a **design**. The implemented system is described in [as_built_status.md](as_built_status.md); where this document and that one differ, **as_built_status.md is correct**. Labels used below: *Implemented and tested*, *Implemented, not live-tested*, *Proposed*, *Not yet implemented*, *Known limitation*. Sections not mentioned in the list are design only.
>
> **Design only (*Not yet implemented*).** The local storage layout of §2 is the one the engine already uses (reports, reviewer records, attempts, superseded PDFs); everything about Drive (§3), upload and confirmation (§4), the delivery manifest (§5), human approval (§6), delivery idempotency (§7), the eligibility gate in the delivery stage (§8) and cleanup (§9) has no code. The ledger has a `deliveries` table and a `record_delivery` method, with no caller. The pipeline stops at `audited`; `publish` is rejected. Replacement reports (R8.4.2) keep the previous PDF as `report.superseded.<hash16>.pdf`; a replacement is **not** delivered or published.

Status: **design only.** Nothing is uploaded, sent or created.

## 1. Rules that cannot be relaxed

1. A report becomes **eligible for delivery only after finalization and a passing audit**
   (`finalize_report` finalized/already_finalized **and** the audit gate in
   [production_architecture](production_architecture.md) §5.6). Eligibility is a ledger state (`audited`) and the
   publisher refuses any other state.
2. The delivered file is the **exact finalized PDF** (`reports/…/report.pdf`, bytes hashed again before upload).
3. A delivery **retry reuses the same Drive file**; it never invokes the runner or the renderer.
4. Nothing is overwritten or deleted by automation: not local files, not Drive files.
5. Human approval is required for every delivery in the pilot and is bound to the PDF hash.

## 2. Local storage

* Output root (engine-managed, unchanged) `%LOCALAPPDATA%\JasperSEOReporting\output` [V default]:
  `reports/<client_key>/<period_id>/` (`report.pdf`, superseded/duplicate copies, sidecars, draft),
  `reviewer/<client_key>/<period_id>/`, `attempts/<client_key>/<period_id>/`.
* New runner-managed sibling folder (not part of the engine's three sub-folders, ignored by the audit's name
  checks [V: unknown names are skipped]): `delivery/<client_key>/<period_id>/` holding
  `manifest.<pdf16>.json` (the delivery manifest, §5). Created by the runner only when the run reaches
  `publishing`.
* Work folders for rendering: `%LOCALAPPDATA%\JasperSEOReporting\work\<run_key with '/' → '__'>\<attempt>\`,
  must be empty and outside the project (renderer rule [V]); never deleted automatically (operator cleanup after
  the retention window, §9).

## 3. Controlled Google Drive structure

```
Jasper SEO Reports/                       (root, created once; shared with the operator group and Make's account)
  <client_key>/                           one folder per client, never shared across clients
    <period_id>/
      <client_key>_<period_id>_seo-report_<pdf16>.pdf
      <client_key>_<period_id>_manifest_<pdf16>.json   (copy of the delivery manifest, for audit)
```

* Folder names are the stable identifiers, never client names.
* Link sharing off; no public links; access is by account.
* File name = `<client_key>_<period_id>_seo-report_<pdf16>.pdf`; the 16-hex suffix makes superseding reports
  distinct files in the same folder (the old file stays).
* The uploader sets Drive `appProperties` (or `description` if properties are unavailable) **[A]**:
  `run_key`, `pdf_sha256`, `pdf_bytes`, `manifest_sha256`. They make the upload findable and verifiable without
  a local lookup.

## 4. Upload procedure and confirmation

1. Preconditions (re-checked inside the publisher, not trusted from the caller): run state is `audited`;
   `report.pdf` exists, is a regular file, and its SHA-256 and size equal the ledger's `pdf_sha256`/`pdf_bytes`
   and the reviewer record's; the record is version 2 with the ledger's `render_input_sha256`; the sidecar passes
   `render_state.read_sidecar_for`.
2. Ensure the client and period folders exist (lookup by name under the root, create if absent; never rename or
   delete).
3. **Lookup before upload:** list files in the period folder with the same `pdf_sha256` property. If one exists
   and its size matches → reuse it (no second upload). If a file with the same name exists but different content
   → `blocked` (`publish_conflict`).
4. Upload with a resumable upload; request `id, size, md5Checksum, parents, appProperties` back.
5. **Confirmation:** returned `size` equals `pdf_bytes`; `md5Checksum` equals the MD5 of the local file (Drive does
   not return SHA-256, so MD5 plus the stored SHA-256 property is the check) **[A]**; the parent is the period
   folder. Any mismatch → `publish_failed`, the uploaded file is **not** deleted (it is reported; operator
   decides), and a retry uploads nothing new if a matching file now exists.
6. Upload and confirm the manifest copy the same way.
7. In one ledger transaction: insert the `deliveries` row (`upload_confirmed = 1`), move `publishing →
   ready_for_delivery`, enqueue the `jasper.delivery-ready` outbox message.

## 5. Delivery manifest (schema `jasper.delivery-manifest` v1)

```json
{
  "schema": "jasper.delivery-manifest",
  "version": 1,
  "run_key": "example-co/2026-10-01_2026-10-28",
  "client_key": "example-co",
  "period_id": "2026-10-01_2026-10-28",
  "pdf": {"name": "report.pdf", "sha256": "…64 hex…", "bytes": 118765, "pdf16": "…16 hex…"},
  "identity": {
    "render_input_sha256": "…64 hex…",
    "render_input_format": {"name": "jasper.render-input", "version": 2},
    "generation_date": "2026-11-02",
    "run_id": "0198c1a2-7b3c-7d4e-8f5a-000000001000",
    "revision": 1,
    "record_name": "reviewer.0198c1a2-7b3c-7d4e-8f5a-000000001000.9d8c7b6a5f4e3d2c.json"
  },
  "audit": {"outcome": "consistent", "finalized_valid": 1, "verification_complete": true, "checked_at": "2026-11-02T01:07:30+00:00"},
  "drive": {"folder_id": "…", "file_id": "…", "file_name": "…", "uploaded_at": "2026-11-02T01:07:40+00:00", "size": 118765, "md5": "…"},
  "delivery_key_prefix": "example-co/2026-10-01_2026-10-28/9d8c7b6a5f4e3d2c",
  "created_at": "2026-11-02T01:07:41+00:00"
}
```

Canonical JSON (sorted keys, compact, trailing newline) like the sidecar [V]; `manifest_sha256` is the hash of
those bytes. The recipient set is not in the manifest (the full `delivery_key` adds `recipient_set_hash16` in
Make). The manifest is written exclusively (no overwrite); an existing identical manifest is reused, a different
one → `blocked`.

## 6. Human approval record

* Stored in the `Delivery` tab ([google_sheets_contract](google_sheets_contract.md) §6): `approval_status`,
  `approved_by` (initials), `approved_at`, `rejection_reason_code`; the sheet's version history is the audit trail,
  and Make appends an `Execution History` row for each change.
* The approval is bound to `pdf_sha256` and `delivery_key`. A superseding report (new `pdf16`) creates a new
  Delivery row that needs its own approval.
* `expired`: pending approvals older than a configurable window (default 14 days) are set `expired` by Make and
  require a fresh request to deliver.
* The runner never approves, never sends and never changes approval fields.

## 7. Delivery idempotency and retry

| Layer | Guard |
|---|---|
| runner | `deliveries.delivery_key` unique, `UNIQUE(run_key, pdf_sha256, recipient_set_hash16)`; the same file is published once |
| Make 2a | existing `Delivery` row for the key → stop |
| Make 2b | claim by setting `sending` before the mail; ambiguity guard for `sending` older than 15 minutes |
| retry | 2b re-run for the same key: downloads the same `drive_file_id`, re-sends; max 3 automatic attempts |

E-mail delivery is **at-least-once with a guard**; exactly-once cannot be guaranteed across a crash between "mail
accepted" and "row updated". The operator resolves ambiguous `sending` rows. A delivery failure never changes the
run's finalized state and never triggers a render.

## 8. Eligibility gate (checked three times)

1. `pipeline` moves to `publishing` only from `audited`.
2. `drive_publisher` re-verifies hashes and the audit verdict stored in the ledger.
3. Make 2a re-verifies the message and the Drive metadata before creating the Delivery row, and 2b re-verifies the
   approval and the hash before sending.

## 9. Retention and cleanup

| Item | Policy |
|---|---|
| local `report.pdf`, superseded copies, records, sidecars | kept (the engine never deletes) |
| work folders | operator-initiated cleanup after 90 days; no automatic deletion |
| Drive files | kept at least 24 months **[U: client contract / privacy policy]**; cleanup is manual and logged |
| manifests | kept with the PDF |
| `Delivery` rows | kept; archived to a new tab yearly, not deleted |
| attempt records / ledger events | per [ledger_schema](ledger_schema.md) §8 |

The runner has no delete capability for Drive or local reports; its Drive scope (`drive.file`) is used for create
and read only, and the code path contains no delete or overwrite call (enforced by a source check in tests).
