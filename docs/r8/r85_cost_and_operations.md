# R8.5 — Cost and operations design

Design only. **No Make.com plan, price, operation allowance or limit is stated here as fact.** Every number below is an *estimate derived from the
scenario designs* and is **Unverified** until checked against the current Make documentation and a measured pilot (R8.7). Counting rule
assumed: one operation per module execution (**Unverified**: whether idle polling triggers, routers, filters, data-store reads and webhook
responses count, and how). The point of this document is to show *where operations go* and *how to keep them low*.

## 1. Cost posture

| Resource | Choice | Cost note |
|---|---|---|
| Report generation | local Python on an existing Windows machine; local Chromium; no paid AI APIs | no cloud compute cost; electricity and the machine |
| Data | Google Sheets, Search Console, GA4 via the existing Google Workspace | no extra licence assumed; API quotas **Unverified** |
| Storage | Drive in the existing Workspace; small PDFs (the sample is ~120 KB) | negligible; retention policy pending |
| Orchestration | Make.com, **6 scenarios** | the only metered item; plan and limits **Unverified** |
| E-mail | existing Workspace mailbox via Make's Gmail connection | no extra licence assumed |
| Monitoring | Make itself plus the local logs; no paid monitoring service | |

## 2. Scenario count

Six physical scenarios (SCN-1, SCN-3, SCN-4A, SCN-4B, SCN-6, SCN-7); see [scenario specifications §0](r85_scenario_specifications.md).
Area 2 (queue) and area 5 (Drive upload) have no scenario of their own: the queue is a sheet, and the upload is done by the runner.

## 3. Operation drivers (estimates)

Estimated operations per execution, from the module lists in the scenario specifications:

| Scenario | Module work per execution | ≈ operations |
|---|---|---|
| SCN-1 scheduler, idle day | trigger + one search of flagged candidates | 2 |
| SCN-1, per client queued | read config row, validate, search duplicate, add row, update marker, notify | 6 |
| SCN-3, per received event | trigger, signature/version filter (free?), data-store check, data-store write, append history, find + update request row | 5–6 |
| SCN-4A, per delivery-ready | trigger, dedupe lookup, read settings, Drive metadata, search pending, add `Delivery`, notify (+1 to expire an older row) | 7–8 |
| SCN-4B, per approval decision | trigger, process row, verify, call SCN-6, update marker | 3–5 |
| SCN-4B, idle poll | trigger | 1 each (**Unverified** whether counted) |
| SCN-6, per delivery | trigger, read row, read settings, Drive metadata, claim, download, send, update row, append history, notify | 10 |
| SCN-7, per run | trigger, search requests, search delivery, a few conditional updates | 4 + actions |

### 3.1 Per client per month (one report, no problems)
Status events in **milestone mode**: ack, `collecting`, `rendered`, `audited`, `ready_for_delivery`, `delivery_ready`, `delivered` ≈ 7 events × ~6 =
**~42**; approval request ~8; approval decision ~4; e-mail ~10; scheduling ~6; ⇒ **≈ 70 operations per client per month**.
In **full mode** (every transition, ~12 events) the status part is ~72, so **≈ 100**. A failed-then-retried report adds roughly 6 per extra event.

### 3.2 Fixed monthly overhead (independent of the number of clients)
| Item | Frequency | ≈ operations / month |
|---|---|---|
| SCN-1 idle days | daily | 30 × 2 = 60 |
| SCN-4B idle polling | hourly 08:00–18:00 on weekdays (~220 runs) | ~220 (only if idle polls count) |
| SCN-7 watchdog | every 4 h (180 runs) × ~4 | ~720 |
| **Total fixed** | | **≈ 1,000** |

### 3.3 Example (illustrative only)
10 clients: 1,000 + 10 × 70 ≈ **1,700 / month**; 30 clients: 1,000 + 30 × 70 ≈ **3,100 / month**. Whether that fits a given Make plan is **not
asserted**; compare with the plan's allowance before R8.7.

## 4. Main sources of operation consumption (in order)
1. **SCN-7 watchdog** (fixed, 4-hourly) — the largest single item for small client counts.
2. **Status events** (SCN-3) — scale with clients × events.
3. **SCN-4B idle polling** — fixed, if empty polls count.
4. **E-mail delivery** (SCN-6) and **approval request** (SCN-4A) — per report.
5. SCN-1 — negligible once it searches only flagged candidates.

## 5. How to avoid unnecessary operations
* **The runner does the polling** (Sheets reads by the runner cost no Make operations) — the single most important saving. Make never polls the
  runner.
* **Coalesce status events** (milestone mode): about 40 % fewer events; the ledger remains complete.
* **SCN-1 searches only flagged candidates**: the operator sets `rankings_ready_period` for a client when the month's rankings are final; Make
  searches `Client Delivery Settings` for rows where `rankings_ready_period` is non-empty and differs from `last_requested_period` (two plain text
  columns, no formulas), instead of iterating every active client every day.
* **Longer watchdog interval**: 6 hours instead of 4 saves ~240 ops/month; 12 hours saves ~360. SEV-1 conditions do not depend on it (they are pushed
  by the runner's status events). Choose 4 h for the pilot, review after the first month.
* **Business-hours approval polling**, or an instant row trigger if the plan offers one.
* **Data store for dedupe** (one read + one write) rather than scanning `Execution History`.
* **Digest notifications** for SEV-3/4 (one message per day) instead of one per event; no "nothing to do" messages.
* **Filter before iterating**; keep scenarios free of loops that call Sheets per row; read tabs in one range call.
* **Avoid duplicate triggers**: the idempotency keys already prevent duplicate *work*; the same keys prevent duplicate *operations* by stopping at the first
  check.
* **Bounded retries**: e-mail 3 attempts; outbox backoff stretches to hourly; the circuit breaker stops sending when the URL is rejected.
* **Sequential client processing** at first: fewer concurrent status bursts, simpler to reason about.

## 5b. Other cost risks
Make execution storage and attachment handling may be plan-limited (**Unverified**); the Sheets/Drive API quotas are shared with the runner's own
usage; a large number of clients with the full-mode status would multiply operations — use milestone mode in production.

## 6. Operational responsibilities

| Role | Responsibilities | Frequency |
|---|---|---|
| **Operator** (project/report manager) | maintain `Client Delivery Settings` and the explicit period dates; set `rankings_ready_period` when rankings are final; read the daily digest; resolve blocked/failed runs (acknowledge, cancel, supersede) using the documented rules; confirm ambiguous e-mail sends | daily during the reporting window; weekly otherwise |
| **Reviewer** | open the review file, compare the PDF with the approval request (`pdf16`), approve or reject with the reason code | within 3 days of each request |
| **System administrator** | runner machine health, Windows updates timing (not mid-month), disk space, backups and a restore test, Task Scheduler, credentials custody and rotation, Make account and connection ownership, access reviews | weekly check; quarterly rotation/restore drill |
| **Developer** | contract changes (additive), Playwright/Chromium upgrades **outside** a reporting month, runner releases, post-incident fixes | as needed |

## 7. Suggested monitoring frequency
| What | How often | By |
|---|---|---|
| Daily digest (SEV-3/4) and any SEV-1/2 notifications | daily during the reporting window (first ~10 days of each cycle), otherwise weekly | operator |
| SCN-7 watchdog | every 4 h (automatic) | Make |
| Runner heartbeat (if adopted) | daily (automatic) | runner |
| Runner log review (`seo_report.log`) | on any alert; weekly skim | admin |
| Free disk space, ledger/history backup success | weekly | admin |
| Backup restore test | quarterly | admin |
| Make operation consumption vs plan | weekly in the first 3 months, then monthly | admin |
| Google OAuth token state / consent-screen status | monthly (and at once on any `auth_*` alert) | admin |
| Webhook key rotation | every 12 months or on staff change | admin |

## 8. Things to verify before relying on these numbers
Make's current plans, operation counting (idle triggers, routers, filters, data stores, webhook responses), scenario/webhook limits, execution-log
retention and attachment handling, instant versus polling triggers for Sheets, HMAC/hash functions available, Google Sheets and Drive API
quotas, Gmail sending limits for the chosen mailbox, and the actual operations measured in a one-client pilot.
