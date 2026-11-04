# R8.4.6 — Final R8 operational readiness assessment

Date of assessment: 2026-10-31. Basis: inspection of the code and the offline test suite. **No live system was exercised.**

> **Status update (R8.7b, 2026-11-01).** This assessment is the R8.4.6 view and is kept for history. Since then: blocking items 1 (entry point), 2 (production Google client factory) and 8 (in-runner preflight) are **implemented and tested with synthetic data** (R8.6a–d, corrected in R8.6f); the credentials directory is configurable (R8.6b); `pyproject.toml` and `README.md` were added (R8.7b). Still open: items 3 (request intake), 4 (credential relocation: **done in R8.7d**, originals not yet retired), 5 (live authentication: **not verified**), 6 (status sender), 7 (backups), 9 (clean-machine install and `pip install -e .`: **unverified**), 10 (Scheduled Task), 11 (Drive, approval, e-mail, Make.com: **not implemented**). Readiness is reported separately, never as one figure: core engine **ready** for controlled use (synthetic data only); local CLI and runner **ready** locally; live Google **unverified**; unattended production **blocked**; end-to-end automation **needs implementation**. The production gate deliberately blocks real reporting until its attestation and live-verification stages are complete. See [r86_final_verification.md](r86_final_verification.md) and `as_built_status.md`.

> **R8.7c addendum.** The production gate now has a mechanism that can clear `live_google_not_verified`: a DPAPI-protected, machine- and account-bound, seven-day verification attestation (`security_and_authentication.md` §12). **No attestation exists and no live verification has been performed**, so the gate still blocks all real reporting; live Google access is unverified, and Make, Drive, approval and e-mail are not implemented. *Update (R8.7d, 2026-11-02):* the Google credentials were relocated to `%LOCALAPPDATA%\JasperSEOReporting\credentials\` by the operator (hashes and ACL verified, external `runner.yaml` updated, originals retained), so `credentials_not_relocated` no longer applies. The system is not production-ready.

## 1. Verdict

* **The report engine and the local orchestration core are in good shape for controlled, attended local use** through the Python
  API: collection → render → finalization → audit for many clients, with a durable ledger, safe resume, operator recovery,
  report replacement, path/storage isolation, bounded rendering, non-interactive authentication and sanitized logging.
* **The system is NOT ready for unattended production execution.** Several pieces that make a run start by itself, report its
  status, and be operated by a person do not exist yet (section 4). None of these is a defect in the existing code; they are the
  unbuilt remainder of the plan.

## 2. What the R8.4 stages resolved (mapped to the audit findings)

| Finding | Stage | Resolution | Evidence |
|---|---|---|---|
| D2, D3 — SQLite error and transaction safety | R8.4.1 | classified `LedgerError`, rollback on every path, client- vs batch-level failures | offline tests incl. injected failures |
| D1 — blocked runs had no operator recovery | R8.4.2 | acknowledge → retry (condition re-checked, audit gate kept), cancel | offline tests |
| D4 — completed periods could not be regenerated | R8.4.2 | linked replacement run (`~N`), original untouched and re-verified | offline tests incl. real engine finalization |
| D5, P2 — path aliasing; network SQLite paths | R8.4.3 | canonical path comparison; UNC/mapped/unknown drives refused for databases | offline tests, real junctions; symlink tests skipped (privilege) |
| D6 — liveness classification and lease renewal | R8.4.4 | conservative liveness, reuse detection, background lease keeper | offline tests, real own-process probe |
| P3 — no render timeout | R8.4.4 | configurable hard limit, watchdog, phase-tagged failure | offline fakes + one real Chromium |
| D7 — interactive OAuth could start unattended | R8.4.5 | non-interactive `get_credentials`, explicit setup command, error classification | synthetic tokens only |
| Logging, dependency reproducibility, asset readiness, documentation | R8.4.6 | sanitized JSON logging; lock files; preflight; docs synchronised | offline tests |

The remaining items of the R8.4 audit's longer risk list (P1, P4–P15) are not re-derived here; this document only claims what the
stage reports and tests above show. Anything in that list that was not named in a stage objective should be treated as open until a
person re-checks it against the R8.4 audit report.

## 3. Strengths worth keeping

Deterministic, content-addressed render identity; append-only history; write-once dates; no automatic deletion anywhere;
every operator action recorded with who and why; idempotent requests; fail-closed path and drive checks; no secrets in outputs, events
or logs; an audit gate that cannot be skipped.

## 4. Blocking items for unattended production execution

1. **No entry point** — no CLI/`__main__` runs a batch, calls `configure_logging`, or lets an operator run `status`, `retry`,
   `acknowledge`, `cancel` or `supersede`. (Needs implementation.)
2. **No production Google client factory** — `Services.collect` is only ever a test fake; real Search Console/GA4/Sheets clients are
   not constructed by any code path. (Needs implementation.)
3. **No request intake** — `Report Requests` tab, `request_queue` and the new tabs are design only. (Needs implementation.)
4. **Credentials are inside OneDrive** and the code has no configurable credentials directory; relocation is documented but not
   authorised or performed. (Blocked on approval; needs a small code change first.)
5. **No live authentication check** — never run against Google; the OAuth consent-screen status (7-day refresh-token expiry when in
   "Testing") is unknown. (Unverified.)
6. **No status delivery** — the outbox is written but never sent; no alert on `auth_required`, `disk_full`, failures or blocked runs.
   (Needs implementation.)
7. **No backup job** for the ledger/history databases and output. (Needs implementation.)
8. **No in-runner asset preflight** — a missing font is found only at the render stage. (Needs implementation; the preflight script
   covers it manually.)
9. **No clean-machine installation** and no hash-pinned lock. (Unverified.)
10. **No Scheduled Task**, run-as account or permissions defined. (Blocked by items 1–2.)
11. **Delivery** (manifest, Drive, approval, e-mail) and **Make.com** are not started. (Not yet implemented; awaiting approval.)

## 5. Known limitations carried forward

PID-only process identity (mitigated by creation time); lease loss noticed at the next stage boundary; only the `rendering` stage
timeout is enforced; render watchdog ends processes on Windows only; lease times have one-second resolution; path checks are
point-in-time; an operator must move partial files by hand; identical-content replacement on the same day is refused by the engine;
`supersede` is idempotent by request key, not by target; a Chromium/Playwright upgrade between a render and its retry blocks the retry;
log redaction is pattern-based (a novel secret format could pass); foreign log handlers that raise can interrupt modules other than
the pipeline and runner; `scripts/inspect_sheet.py` now needs a cached token (it no longer signs in).

## 6. Recommended order of the remaining work (for approval, not started)

1. Configurable credentials directory + attended relocation (procedure ready) and a live read-only authentication check.
2. CLI/entry point with logging, operator commands and the asset/dependency preflight.
3. Production Google client factory and one attended end-to-end run on a single real client period, with the output reviewed by a person.
4. Backup job and disk/free-space and failure alerts.
5. Make.com architecture and integration design (R8.5) — **awaiting explicit approval; not started.**
6. Delivery automation after that.
