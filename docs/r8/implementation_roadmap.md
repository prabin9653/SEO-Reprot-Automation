# R8.2 — Implementation roadmap, dependencies and open decisions

> **Update (R8.7h.4).** Report layout refinements implemented and tested with synthetic data: the Search Console daily table is removed (both daily charts stay) and the geography table/chart show the top 5 countries by impressions. A new real-data preview is needed to review them. Earlier: **Update (R8.7h.2).** `report preview` (a real-data render with a visible `PREVIEW — NOT FINAL` label, in its own folder, touching no production state) is implemented and tested offline; it has **not** been run on real data (R8.7h.3, pending the operator). Earlier (R8.7e.1b): The live-verification workflow (`seo-report verify live`, module `live_verification.py`) is implemented and tested offline with fake Google services; it has **not** been run against Google and no attestation exists. The live run is **R8.7e.2, pending approval**. Earlier (R8.7d, 2026-11-02): The Google credentials were relocated by the operator to `%LOCALAPPDATA%\JasperSEOReporting\credentials\` (hashes and ACL verified, external `runner.yaml` updated, originals retained); `credentials_not_relocated` is satisfied and `doctor`/`system-info` report it with the gate's own condition (R8.7d.2). Pending: the live Google verification and an attestation (R8.7e.2), Make.com, Drive, e-mail and the first real pilot. Earlier (R8.7c): The production-gate attestation mechanism is implemented and tested with synthetic data (`attestation.py`; design in `security_and_authentication.md` §12). It is the only thing that can clear `live_google_not_verified`, and nothing can issue an attestation yet: that is the job of the live verification (R8.7e), which needs the credential relocation (R8.7d) first. R8.7e is **not started or approved**. Earlier (R8.7b): R8.6a–d (CLI and local runner: `report`, `status`, `recovery`, `doctor`, Google client factory, configurable credential paths, logging activation) are **done** and verified with synthetic data; R8.6e verified them and R8.6f corrected the three issues it found; R8.7a audited readiness; R8.7b added `pyproject.toml`, `README.md` and the documentation corrections. The planned CLI of R8.4 was built in R8.6. **Not done:** live Google verification, any real report, the live verification and an actual attestation, the operational workbook, request intake, the status sender, Make.com, Drive, approval and e-mail. See [r86_final_verification.md](r86_final_verification.md) and [as_built_status.md](as_built_status.md).
>
> **As-built status (updated R8.4.6).** This document was written at R8.2 as a **design**. The implemented system is described in [as_built_status.md](as_built_status.md); where this document and that one differ, **as_built_status.md is correct**. Labels used below: *Implemented and tested*, *Implemented, not live-tested*, *Proposed*, *Not yet implemented*, *Known limitation*. Sections not mentioned in the list are design only.
>
> **Stage numbers differ from what happened.** This roadmap planned R8.3 (pipeline library), R8.4 (CLI and controlled execution), R8.5 (Make.com design), R8.6 (Make.com implementation), R8.7 (multi-client testing), R8.8 (delivery), R8.9 (readiness and pilot). As executed: **R8.3** pipeline, ledger, runner (done); **R8.4** became a read-only operational audit followed by six reliability stages — **R8.4.1** SQLite/transaction safety, **R8.4.2** operator recovery and superseding, **R8.4.3** filesystem isolation, **R8.4.4** locks and render timeout, **R8.4.5** non-interactive authentication and error classification, **R8.4.6** logging, dependencies, assets, documentation, credential-relocation procedure, readiness assessment (all done). *(superseded: the CLI and the production Google client factory were built in R8.6; see the update above.)* Not started at R8.4.6: request intake, R8.5 Make.com design (awaiting explicit approval), delivery, multi-client live testing, pilot. See `final_readiness_assessment.md` §4 and §6 for the blocking items and the recommended order. **R8.5 (Make.com architecture and design) is now complete as a design**; the stages after it are renumbered in [r85_implementation_roadmap.md](r85_implementation_roadmap.md): R8.6 CLI and local runner, R8.7 Make.com integration, R8.8 Drive, R8.9 approval and e-mail, R8.10 production deployment. None is started or approved.

Status: **design only.** Every stage below needs its own explicit approval before work starts. R0–R7.5 modules are
protected throughout; the single anticipated exception is `auth.py` (R8.4, separate approval).

## 1. Unresolved decisions (to settle before or during R8.3)

| ID | Decision | Proposed default | Needed by |
|---|---|---|---|
| U1 | Fourth sheet tab `Client Delivery Settings` (beyond the three named tabs) | yes | R8.3 contracts, R8.5 |
| U2 | Operational tabs in a **separate spreadsheet** from the client-data workbook | yes (keeps the workbook read-only for automation) | R8.5 |
| U3 | Partial-source policy (report allowed when GSC or GA4 is wholly unavailable) | no in production unless `allow_partial_sources` | R8.3 |
| U4 | Secret storage: ACL-restricted files vs Credential Manager/DPAPI (new dependency) | ACL files for the pilot | R8.4 |
| U5 | OAuth consent-screen publishing status / refresh-token lifetime; rotation of possibly OneDrive-synced secrets | verify with Google docs; rotate | R8.4 |
| U6 | `drive.file` can create files in the controlled folder? else app-created root folder shared with Make | verify | R8.8 |
| U7 | Drive/Delivery retention (24 months proposed) and client privacy requirements | 24 months | R8.8 |
| U8 | Locale: `en-AU` wording/date formats/time zones (not implemented) | out of pilot scope | after R8.9 |
| U9 | Dependency manifest (`requirements.txt` or `pyproject.toml`; none exists today [V]) | `requirements.txt` with pinned versions | R8.3 (needs approval to add) |
| U10 | Backup target for the ledger, history DB and output root | second drive or encrypted external storage | R8.9 |
| U11 | Runner machine availability (always on, Task Scheduler "run whether user is logged on or not") | decide in R8.9 | R8.9 |
| U12 | Make.com plan, operation budget, file-size and webhook limits | verify in R8.5 | R8.5 |

## 2. Risks

| Risk | Mitigation |
|---|---|
| Engine has no cross-process lock; two runners could corrupt state | `runner:global` ledger lock; Task Scheduler single instance |
| `build_clients`/`select_clients` abort on any bad row | per-client split loader (no change to `clients.py`) |
| Manual rankings late or wrong | `rankings_ready` gate; approval step catches content errors |
| Auth expiry mid-batch | non-interactive failure, batch abort, one notification |
| Make unreachable | outbox retry; ledger authoritative; silence alarm |
| E-mail sent twice or lost on crash | claim-before-send, ambiguity guard, operator resolution |
| Drive and local copies diverge | hash + size + MD5 confirmation, appProperties, never overwrite |
| Secrets exposed through OneDrive | relocation and rotation before any scheduled run (R8.4) |
| Re-render after promotion (G1) creates a duplicate draft | accepted, documented; audit tolerates `duplicate_pdf_present` |
| Names of clients change in the sheet | `client_key` mirror and exact-name cross-check |

## 3. Stage plan

### R8.3 — Pipeline library (no CLI, no network in tests)

* **Objective:** implement `run_client`, the runner, the ledger and the services of
  [production_architecture](production_architecture.md) through `audited`, with injected fakes for Google.
* **New files (proposed):**

| File | Content |
|---|---|
| `src/seo_report/contracts.py` | request/response/manifest/delivery-ready models, closed-schema validation, error categories and retry table, state machine dictionary |
| `src/seo_report/runner_config.py` | non-secret settings (paths, timeouts, sheet ids, thresholds), validation, overlap checks |
| `src/seo_report/ledger.py` | schema creation/migration check, requests, runs, attempts, events, locks, outbox, deliveries mirror, recovery |
| `src/seo_report/client_loader.py` | per-client isolated loader (§5.1 of the architecture) |
| `src/seo_report/source_collector.py` | GSC/GA4 collection with failure classification |
| `src/seo_report/report_service.py` | assembly + history/insights/annotations |
| `src/seo_report/render_service.py` | fingerprints, render input, `produce_report_draft`, sidecar-first resume |
| `src/seo_report/finalize_service.py` | `finalize_report` mapping |
| `src/seo_report/audit_service.py` | audit gate |
| `src/seo_report/pipeline.py` | `run_client` and the dry-run mode |
| `src/seo_report/runner.py` | sequential multi-client runner, batch abort rules |
| `config/runner.yaml.example` | non-secret example settings (no real ids) |
| `requirements.txt` (if U9 approved) | pinned versions of the installed packages |
| tests | `tests/test_contracts.py`, `test_ledger.py`, `test_client_loader.py`, `test_source_collector.py`, `test_report_service.py`, `test_render_service.py`, `test_finalize_service.py`, `test_audit_service.py`, `test_pipeline.py`, `test_runner.py` |

* **Dependencies:** this design; decisions U3, U9. No new packages (stdlib `sqlite3`, existing libraries).
* **Acceptance criteria:**
  1. a synthetic client runs `requested → … → audited` with stub renderer and fake Google clients, with the
     ledger, events and outbox rows exactly as specified;
  2. replaying a request, a second request for the same `run_key`, and a re-run after `audited` change nothing;
  3. interruption injected at every stage boundary resumes with the **same** render hash and generation date
     (reusing the R7.5 patterns);
  4. one failing client in a batch of five does not stop the others; an `auth_required` failure aborts the batch;
  5. the ledger refuses overlap with the history DB/output root; dry-run writes nothing outside scratch;
  6. real-Chromium test limited to one client; all earlier tests still pass.
* **Safety controls:** no network, no credentials, no real client data, temporary folders only; ledger never
  touches the snapshot DB; source check that no module deletes or overwrites files.
* **Approval boundary:** stops after R8.3; no CLI, no Drive, no Make.

### R8.4 — CLI and controlled execution

* **Files:** `src/seo_report/cli.py`, `src/seo_report/__main__.py`, tests; **`auth.py` change (separate approval):**
  `get_credentials(purpose, interactive=False)`, `AuthRequiredError`, configurable secrets directory.
* **Acceptance:** commands, arguments, JSON output and exit codes of
  [production_architecture](production_architecture.md) §9; `run` impossible without `--production`; `dry-run`
  cannot write outside scratch; non-interactive auth never opens a browser (test with a fake flow that fails if
  called); the credential relocation checklist completed by the operator and verified by `validate --online`
  (read-only checks).
* **Approval boundary:** the operator performs the credential move; Claude does not touch secrets.

### R8.5 — Make.com integration design

* **Objective:** verify current Make documentation (plan limits, modules, webhooks, error handlers, file limits);
  finalize [make_scenarios](make_scenarios.md) and the sheet layout; decide U1, U2, U12.
* **Files:** documentation only (`docs/r8/…` updates). **Acceptance:** every [A] item in the scenario document is
  resolved or replaced. **Boundary:** no scenario is created.

### R8.6 — Make.com scenario implementation

* **Objective:** build Scenarios 1a/1b and 2a/2b against a **test spreadsheet** and a test mailbox.
* **External components:** Make.com, test spreadsheet, test Drive folder. **Acceptance:** synthetic requests flow
  end to end; duplicate rows and duplicate webhooks are ignored; per-client error isolation; no real client data.
* **Boundary:** explicit approval to create scenarios and connections; no client e-mail.

### R8.7 — Multi-client testing

* **Objective:** 5, then 20 synthetic clients (and one internal test client with real Google access if approved)
  through the runner; measure per-stage times, quota behaviour, Chromium overhead, memory.
* **Acceptance:** timeouts and backoff tuned from measurements; failure injection (auth, 429, render, conflict);
  no duplicate finalizations or uploads. **Boundary:** no external recipients.

### R8.8 — Delivery automation

* **Files:** `src/seo_report/drive_publisher.py`, `src/seo_report/delivery_manifest.py`,
  `src/seo_report/status_outbox.py`, `src/seo_report/request_queue.py`, tests; Make scenario 2a/2b completion.
* **Acceptance:** upload confirmation, lookup-before-upload, never overwrite/delete, manifest exclusive write,
  approval bound to the hash, delivery retry reuses the Drive file, ambiguity guard, delivery mirror in the ledger.
* **Safety:** test Drive folder and test mailbox only; source check that no delete/overwrite call exists.
* **Boundary:** a real delivery to an internal address only, with approval.

### R8.9 — Operational readiness and production pilot

* **Objective:** runbook (daily/monthly checks, re-authentication, recovery, restore), Task Scheduler setup,
  backups, monitoring, one real pilot client with the approval gate.
* **Acceptance:** restore drill for ledger and history DB; alarm drills (silence, auth, Make down); pilot client
  report delivered after human approval. **Boundary:** explicit go decision by the owner.

## 4. Cross-stage constraints

* No parallel execution before R8.7 evidence and a database-safety review.
* Every new module has injected clocks/collaborators and offline tests; real Chromium only in one lifecycle test.
* No stage may weaken a protected R0–R7.5 behaviour; the full suite (3,957 passed / 10 skipped at R7.5) must stay green.
* Any change to an existing module is listed in the stage's approval request before editing.
