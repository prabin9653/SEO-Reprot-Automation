"""The single page served by `seo web` (see `webui`). Self-contained: no external script, style, font or image. All dynamic text is added with
`textContent`, never as HTML, so a client name from the sheet cannot inject markup."""

PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Jasper SEO Reports</title>
<style nonce="__NONCE__">
:root{--bg:#f5f6f8;--card:#fff;--ink:#16202b;--muted:#5d6b7a;--line:#dde2e8;--brand:#1f5fbf;--brand-ink:#fff;--ok:#157347;--ok-bg:#e3f4ea;
--warn:#8a5a00;--warn-bg:#fff1d6;--bad:#a6261d;--bad-bg:#fbe5e3;--idle:#4a5866;--idle-bg:#e9edf1;--shadow:0 1px 2px rgba(16,32,48,.08)}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){--bg:#10161d;--card:#18212b;--ink:#e6ebf0;--muted:#9aa8b6;--line:#2a3643;--brand:#5b9bff;--brand-ink:#08121f;
--ok:#6fd49a;--ok-bg:#16321f;--warn:#f0c067;--warn-bg:#3a2c0f;--bad:#ff8c82;--bad-bg:#3d1a17;--idle:#b4c0cc;--idle-bg:#243140;--shadow:none}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.45 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
header{background:var(--card);border-bottom:1px solid var(--line)}
.bar{max-width:1120px;margin:0 auto;padding:14px 20px;display:flex;flex-wrap:wrap;gap:12px;align-items:center;justify-content:space-between}
h1{font-size:18px;margin:0;letter-spacing:.2px}
.sub{color:var(--muted);font-size:13px}
main{max-width:1120px;margin:0 auto;padding:20px}
.chips{display:flex;flex-wrap:wrap;gap:8px}
.chip{padding:4px 10px;border-radius:999px;font-size:12.5px;font-weight:600;background:var(--idle-bg);color:var(--idle)}
.chip.ok{background:var(--ok-bg);color:var(--ok)}.chip.warn{background:var(--warn-bg);color:var(--warn)}.chip.bad{background:var(--bad-bg);color:var(--bad)}
.toolbar{display:flex;flex-wrap:wrap;gap:10px;align-items:center;margin:0 0 14px}
.toolbar .grow{flex:1}
.tabs{display:flex;gap:4px;margin:0 0 14px;border-bottom:1px solid var(--line)}
.tab{background:none;border:0;border-bottom:3px solid transparent;padding:8px 14px;font:inherit;color:var(--muted);cursor:pointer}
.tab[aria-selected="true"]{color:var(--ink);border-bottom-color:var(--brand);font-weight:600}
button.btn{font:inherit;font-weight:600;padding:7px 14px;border-radius:8px;border:1px solid var(--line);background:var(--card);color:var(--ink);cursor:pointer}
button.btn.primary{background:var(--brand);border-color:var(--brand);color:var(--brand-ink)}
button.btn:disabled{opacity:.5;cursor:not-allowed}
button.btn.small{padding:4px 10px;font-size:13px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;box-shadow:var(--shadow);overflow:hidden}
table{width:100%;border-collapse:collapse}
th,td{text-align:left;padding:10px 14px;border-bottom:1px solid var(--line);vertical-align:top}
th{font-size:12px;text-transform:uppercase;letter-spacing:.5px;color:var(--muted);background:var(--bg)}
tr:last-child td{border-bottom:0}
.key{color:var(--muted);font-size:12.5px;font-family:ui-monospace,Consolas,monospace}
.pill{display:inline-block;padding:2px 9px;border-radius:999px;font-size:12px;font-weight:600;background:var(--idle-bg);color:var(--idle);white-space:nowrap}
.pill.ok{background:var(--ok-bg);color:var(--ok)}.pill.warn{background:var(--warn-bg);color:var(--warn)}.pill.bad{background:var(--bad-bg);color:var(--bad)}
.fix{color:var(--muted);font-size:13px;margin-top:3px}
.fix{max-width:240px}
.warnline{color:var(--warn)}
.period{font-size:13px;color:var(--muted);white-space:nowrap}
.empty{padding:28px;text-align:center;color:var(--muted)}
#job{margin:0 0 16px;padding:14px 16px}
#job h2{font-size:15px;margin:0 0 8px}
.step{display:flex;gap:10px;padding:6px 0;border-top:1px solid var(--line)}
.step:first-of-type{border-top:0}
.icon{width:18px;text-align:center;font-weight:700}
.step .what{font-weight:600}
.msg{color:var(--muted);font-size:13px}
.advice{font-size:13px;margin-top:2px}
#note{min-height:22px;margin:0 0 10px;font-size:14px}
#note.err{color:var(--bad)}
footer{max-width:1120px;margin:0 auto;padding:6px 20px 30px;color:var(--muted);font-size:13px}
code{font-family:ui-monospace,Consolas,monospace;background:var(--idle-bg);padding:1px 5px;border-radius:5px}
@media (max-width:760px){th:nth-child(3),td:nth-child(3),th:nth-child(5),td:nth-child(5){display:none}}
</style>
</head>
<body data-token="__TOKEN__">
<header><div class="bar">
  <div><h1>Jasper SEO Reports</h1><div class="sub">Runs on this computer only. Nothing here is reachable from the network.</div></div>
  <div class="chips" id="chips" aria-label="This device"></div>
</div></header>
<main>
  <div class="tabs" role="tablist">
    <button class="tab" role="tab" id="tab-clients" aria-selected="true">Clients</button>
    <button class="tab" role="tab" id="tab-history" aria-selected="false">History on this device</button>
  </div>
  <div id="note" role="status" aria-live="polite"></div>
  <section id="job" class="card" hidden></section>
  <section id="view-clients">
    <div class="toolbar">
      <button class="btn" id="refresh">Refresh from the sheet</button>
      <button class="btn primary" id="run-all" disabled>Run all that are ready</button>
      <button class="btn" id="export-shared" title="Copy finished reports to the shared Drive folder and rebuild its dashboard">Refresh dashboard</button>
      <span class="sub grow" id="summary"></span>
    </div>
    <div class="card"><table><thead><tr><th>Client</th><th>Status</th><th>Period</th><th>Report</th><th>Google</th><th></th></tr></thead><tbody id="rows"></tbody></table>
    <div class="empty" id="clients-empty">Reading the sheet...</div></div>
  </section>
  <section id="view-history" hidden>
    <div class="toolbar"><button class="btn" id="refresh-history">Refresh</button><span class="sub grow">Reports and runs recorded on this computer.</span></div>
    <div class="card"><table><thead><tr><th>Run</th><th>State</th><th>Generated by</th><th>Attempts</th><th>Last update</th><th></th></tr></thead><tbody id="hist-rows"></tbody></table>
    <div class="empty" id="history-empty">Nothing recorded yet.</div></div>
  </section>
</main>
<footer>
  If Google sign-in has expired, run <code>seo setup auth</code> in a terminal once, then press Refresh. Finished reports are never overwritten.
  A running report cannot be cancelled from this page; to stop one, press Ctrl+C in the terminal that started <code>seo web</code>.
  <div id="tz"></div>
</footer>
<script nonce="__NONCE__">
(() => {
"use strict";
const token = document.body.dataset.token;
const $ = (id) => document.getElementById(id);
let clients = [], busy = false, poller = null;

function h(tag, props, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(props || {})) { if (k === "class") el.className = v; else if (k === "on") el.addEventListener(...v); else el.setAttribute(k, v); }
  for (const kid of kids) if (kid !== null && kid !== undefined && kid !== false) el.append(kid.nodeType ? kid : document.createTextNode(String(kid)));
  return el;
}
async function api(method, path, body) {
  const res = await fetch("/api/" + path, {method, headers: {"X-Token": token, "Content-Type": "application/json"}, body: body === undefined ? undefined : JSON.stringify(body)});
  let data = null; try { data = await res.json(); } catch (e) { /* not JSON */ }
  if (!res.ok) throw new Error((data && data.error && data.error.message) || ("The request failed (" + res.status + ")"));
  return data;
}
function note(text, isError) { const n = $("note"); n.textContent = text || ""; n.className = isError ? "err" : ""; }
function setBusy(value) { busy = value; for (const b of document.querySelectorAll("button.btn")) b.disabled = value; if (!value) updateRunAll(); }
// Times are stored in UTC and shown in the time zone of the device that views the page (with the zone's short name, so teammates in different zones can compare).
const TIME_FORMAT = {year: "numeric", month: "short", day: "numeric", hour: "numeric", minute: "2-digit", timeZoneName: "short"};
function when(ts) { if (!ts) return ""; const d = new Date(ts); return isNaN(d) ? String(ts) : d.toLocaleString(undefined, TIME_FORMAT); }

// ---- this device ----
async function loadDevice() {
  const chips = $("chips"); chips.replaceChildren();
  try {
    const r = await api("POST", "device", {}), d = r.data || {};
    const att = d.verification_attestation || {}, files = d.oauth_files || {};
    const signedIn = files.cached_sign_in === "present";
    chips.append(h("span", {class: "chip " + (signedIn ? "ok" : "bad")}, "Google sign-in: " + (signedIn ? "saved" : "missing")));
    chips.append(h("span", {class: "chip " + (att.status === "valid" ? "ok" : "warn")}, att.status === "valid" ? "Verified until " + when(att.expires_at) : "Google not verified here"));
    chips.append(h("span", {class: "chip " + ((d.runner_readiness || {}).local === "ready" ? "ok" : "warn")}, "Setup: " + ((d.runner_readiness || {}).local || "unknown")));
  } catch (e) { chips.append(h("span", {class: "chip bad"}, "Device check failed")); }
}

// ---- clients ----
const STATUS = {ready: ["Ready", "ok"], needs_fix: ["Needs fix", "bad"], not_registered: ["Not registered", "warn"], skipped: ["Skipped", ""], not_in_sheet: ["Not in sheet", ""]};
const REPORT = {not_run: ["Not run", ""], done: ["Done", "ok"], interrupted: ["Interrupted", "warn"], failed: ["Failed last time", "bad"], blocked: ["Blocked", "bad"]};
function pill(text, kind) { return h("span", {class: "pill " + (kind || "")}, text); }
function stepsFor(row) {
  const steps = [];
  if (!row.google_verified) steps.push({action: "verify", client: row.client_key});
  steps.push({action: "run", client: row.client_key, period: row.period_id});
  return steps;
}
function runnable(row) { return row.status === "ready" && row.report !== "done" && row.report !== "blocked"; }
function updateRunAll() { const n = clients.filter(runnable).length; const b = $("run-all"); b.disabled = busy || n === 0; b.textContent = n ? "Run all that are ready (" + n + ")" : "Run all that are ready"; }
function actionCell(row) {
  if (row.status === "ready") {
    if (row.report === "done") return h("button", {class: "btn small", on: ["click", () => openPdf(row.client_key, row.period_id)]}, "Open PDF");
    if (row.report === "blocked") return h("span", {class: "msg"}, "See History");
    const label = row.report === "failed" || row.report === "interrupted" ? "Retry" : (row.google_verified ? "Run report" : "Verify and run");
    return h("button", {class: "btn small primary", on: ["click", () => {
      const extra = row.elsewhere ? "\n\nNote: " + row.elsewhere.operator + " already generated this report on another computer (" + row.elsewhere.device + ") on " + when(row.elsewhere.at) + ". Creating it here makes a second copy." : "";
      if (confirm("Create the production report for " + row.name + " (" + row.period_id + ")?\nIt is written on this computer and a finished report can never be overwritten." + extra)) startJob(stepsFor(row));
    }]}, label);
  }
  if (row.status === "not_registered") return h("button", {class: "btn small", on: ["click", () => register(row.name)]}, "Register");
  return null;
}
function renderClients(data) {
  clients = data.clients || [];
  const body = $("rows"); body.replaceChildren();
  for (const row of clients) {
    const [label, kind] = STATUS[row.status] || [row.status, ""];
    const first = h("td", {}, h("div", {}, row.name || "(unnamed row)"), h("div", {class: "key"}, row.client_key || "no key yet"));
    // the page has its own buttons for registering and verifying, so those two rows get a short hint instead of the command-line advice
    const hint = row.status === "not_registered" ? "It is in the sheet but has no key yet. Register it to include it." :
      (row.status !== "ready" && row.advice ? row.advice.meaning + " " + row.advice.fix : null);
    const statusCell = h("td", {}, pill(label, kind), hint ? h("div", {class: "fix"}, hint) : null);
    const stamp = row.last_run_at ? (row.report === "done" ? "Generated " : "Last attempt ") + when(row.last_run_at) + (row.report === "done" && row.generated_by ? " by " + row.generated_by : "") : null;
    const other = row.elsewhere ? "Already generated by " + row.elsewhere.operator + " on another computer (" + row.elsewhere.device + ") " + when(row.elsewhere.at) + ". Running it here makes a second copy." : null;
    const reportCell = row.status === "ready" ? h("div", {}, pill(...(REPORT[row.report] || [row.report, ""])), stamp ? h("div", {class: "fix"}, stamp) : null, other ? h("div", {class: "fix warnline"}, other) : null) : null;
    const googleCell = row.status === "ready" ? (row.google_verified ? pill("Verified", "ok") : pill("Needs verify", "warn")) : null;
    const period = row.period_id ? h("span", {class: "period"}, row.period_id.replace("_", " to ")) : null;
    body.append(h("tr", {}, first, statusCell, h("td", {}, period), h("td", {}, reportCell), h("td", {}, googleCell), h("td", {}, actionCell(row))));
  }
  const c = data.counts || {};
  $("summary").textContent = (c.ready || 0) + " ready, " + ((c.needs_fix || 0) + (c.not_registered || 0)) + " need fixing, " + (c.skipped || 0) + " skipped";
  $("clients-empty").hidden = clients.length > 0;
  $("clients-empty").textContent = "No clients found in the sheet.";
  updateRunAll();
}
async function loadClients() {
  $("clients-empty").hidden = false; $("clients-empty").textContent = "Reading the sheet...";
  $("rows").replaceChildren(); $("summary").textContent = "";
  try {
    const r = await api("POST", "check", {});
    if (r.data && r.data.clients) renderClients(r.data);
    else { $("clients-empty").textContent = (r.data && r.data.error && r.data.error.message) || "The sheet could not be read."; showAdvice(r.data && r.data.error); }
  } catch (e) { $("clients-empty").textContent = e.message; }
}
function showAdvice(error) { if (error && error.fix) note(error.meaning + " " + error.fix, true); }

async function register(name) {
  if (!confirm("Register " + name + " in the client list on this computer?\nIts key is made from the name and can never be changed once a report exists.")) return;
  setBusy(true);
  try { const r = await api("POST", "register", {name}); note(r.result.summary + (r.data && r.data.added && r.data.added[0] ? " Key: " + r.data.added[0].client_key : ""), !r.ok); await loadClients(); }
  catch (e) { note(e.message, true); }
  setBusy(false);
}
async function exportShared() {
  setBusy(true); note("Updating the shared folder...");
  try { const r = await api("POST", "export", {}); note(r.result.summary, !r.ok); showAdvice(r.data && r.data.error); }
  catch (e) { note(e.message, true); }
  setBusy(false);
}
async function openPdf(client, period) {
  try { const r = await api("POST", "open", {client, period}); note(r.ok ? "Opened the report in your PDF viewer." : r.result.summary, !r.ok); } catch (e) { note(e.message, true); }
}

// ---- jobs ----
async function startJob(steps) {
  note(""); setBusy(true);
  try { const r = await api("POST", "jobs", {steps}); renderJob(r.job); poll(r.job.id); }
  catch (e) { note(e.message, true); setBusy(false); }
}
const ICON = {waiting: "o", running: "...", done: "✓", failed: "✗", skipped: "-"};
const VERB = {verify: "Verify Google access", run: "Create report"};
function renderJob(job) {
  const box = $("job"); box.hidden = false; box.replaceChildren();
  const title = job.state === "running" ? "Working..." : (job.state === "done" ? "Finished" : "Finished with problems");
  box.append(h("h2", {}, title));
  for (const s of job.steps) {
    const res = s.result;
    const advice = res && res.advice ? h("div", {class: "advice"}, [res.advice.meaning, res.advice.fix].filter(Boolean).join(" ")) : null;
    const fixes = res && res.blocker_fixes ? res.blocker_fixes.map((t) => h("div", {class: "advice"}, t)) : [];
    box.append(h("div", {class: "step"}, h("div", {class: "icon"}, ICON[s.state] || "?"),
      h("div", {}, h("div", {class: "what"}, (VERB[s.action] || s.action) + " - " + (s.client || "") + (s.period ? " (" + s.period + ")" : "")),
        res ? h("div", {class: "msg"}, res.summary) : h("div", {class: "msg"}, s.state), advice, ...fixes)));
  }
}
function poll(id) {
  clearInterval(poller);
  poller = setInterval(async () => {
    try {
      const r = await api("GET", "jobs/" + id); renderJob(r.job);
      if (r.job.state !== "running") { clearInterval(poller); setBusy(false); await loadClients(); await loadDevice(); }
    } catch (e) { clearInterval(poller); note(e.message, true); setBusy(false); }
  }, 2000);
}

// ---- history ----
async function loadHistory() {
  try {
    const r = await api("POST", "history", {}), runs = ((r.data || {}).runs || []).slice().sort((a, b) => String(b.updated_at).localeCompare(String(a.updated_at)));
    const body = $("hist-rows"); body.replaceChildren();
    for (const run of runs) {
      const kind = run.audited ? "ok" : (run.state === "failed" || run.state === "blocked" ? "bad" : "warn");
      const label = run.audited ? "Done (audited)" : run.state + (run.error_code ? " - " + run.error_code : "");
      body.append(h("tr", {}, h("td", {}, h("div", {class: "key"}, run.run_key)), h("td", {}, pill(label, kind)), h("td", {}, run.generated_by || ""), h("td", {}, run.attempt), h("td", {}, when(run.finalized_at || run.updated_at)),
        h("td", {}, run.audited ? h("button", {class: "btn small", on: ["click", () => openPdf(run.client_key, run.period_id)]}, "Open PDF") : null)));
    }
    $("history-empty").hidden = runs.length > 0;
  } catch (e) { $("history-empty").hidden = false; $("history-empty").textContent = e.message; }
}

// ---- wiring ----
function show(which) {
  for (const name of ["clients", "history"]) { $("view-" + name).hidden = name !== which; $("tab-" + name).setAttribute("aria-selected", String(name === which)); }
  if (which === "history") loadHistory();
}
$("tab-clients").addEventListener("click", () => show("clients"));
$("tab-history").addEventListener("click", () => show("history"));
$("refresh").addEventListener("click", async () => { note(""); setBusy(true); await loadClients(); await loadDevice(); setBusy(false); });
$("refresh-history").addEventListener("click", loadHistory);
$("export-shared").addEventListener("click", exportShared);
$("run-all").addEventListener("click", () => {
  const rows = clients.filter(runnable); if (!rows.length) return;
  const dupes = rows.filter((r) => r.elsewhere).map((r) => r.name + " (" + r.elsewhere.operator + ")");
  const note2 = dupes.length ? "\n\nAlready generated on another computer: " + dupes.join(", ") + ". Running them here makes second copies." : "";
  if (!confirm("Create " + rows.length + " production report(s) now?\nThey run one after another and each can take a few minutes. Finished reports are never overwritten." + note2)) return;
  startJob(rows.flatMap(stepsFor));
});
try { $("tz").textContent = "Times are shown in this device's time zone (" + Intl.DateTimeFormat().resolvedOptions().timeZone + ")."; } catch (e) { /* no Intl */ }
(async () => {
  await loadDevice();
  try { const r = await api("GET", "jobs"); if (r.active) { setBusy(true); renderJob(r.active); poll(r.active.id); } } catch (e) { /* no job */ }
  await loadClients();
})();
})();
</script>
</body>
</html>
"""


def render(nonce: str, token: str) -> str:
    """The page with this response's nonce and the access token filled in (both are URL-safe characters only)."""
    return PAGE.replace("__NONCE__", nonce).replace("__TOKEN__", token)
