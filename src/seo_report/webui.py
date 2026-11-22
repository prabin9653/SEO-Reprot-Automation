"""A local web page for the report runner (stage 3 of the operator-experience work):  `seo web`.

The page is a thin face on the command line. Every button runs one of a FIXED set of existing commands in a child process (`python -m seo_report --json ...`), so
the production gate, the attestation check, the 28-day rule, the ledger, locks and idempotency are exactly those of the command line. This module adds no report
logic and no way to run anything else.

Security model (the page runs on the operator's own machine and can start production runs, so it is locked down)
  * It listens on 127.0.0.1 only. Nothing on the network can reach it.
  * Every request must carry the Host of this server (a defence against DNS rebinding) and, for the API, the secret token printed at start (header `X-Token`,
    compared in constant time). A POST must also come from this origin and be `application/json`. The token is new for every start and is never written to disk.
  * The only actions are the ones in `ACTIONS`; their arguments are validated (client key, 28-day period id, client name) before any command is built. There is no
    parameter that becomes a command-line option, a path or a shell string, and no shell is used.
  * One mutating job at a time (the runner is sequential by design). A job cannot be cancelled from the page: stopping a run half-way is the console's Ctrl+C.
  * Strict response headers (`Content-Security-Policy` with a nonce, no external script, style, font or image; `no-store`; `nosniff`). All text that comes from the
    sheet (client names, codes) is inserted into the page as text, never as HTML.
  * Output of the commands is the already-sanitized JSON of `--json`; nothing else (no stderr, no path of a credential) is returned.

Pure standard library; nothing is installed.
"""

import hmac
import http.server
import json
import logging
import os
import re
import secrets
import socketserver
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

from seo_report import client_config, contracts, guidance
from seo_report import runner_config as rc

LOGGER = logging.getLogger("seo_report.web")
HOST = "127.0.0.1"
DEFAULT_PORT = 8765
MAX_BODY = 64 * 1024
MAX_STEPS = 60
READ_TIMEOUT = 300       # seconds for a read-only command (the workbook read, the device check)
STEP_TIMEOUT = 3600      # seconds for one mutating step (a report run has its own, shorter, limits)
STOP_CODES = (4, 5)      # exit codes that make later steps pointless: the production gate blocked them, or Google sign-in is needed
_NAME_MAX = client_config.MAX_NAME_CHARS

# action -> (needs --production, argv builder). The ONLY commands the page can start.
ACTIONS = {
    "verify": lambda p: ["verify", "live", "--client", p["client"], "--live"],
    "run": lambda p: ["report", "run", "--client", p["client"], "--period", p["period"], "--production"],
}
READ_ONLY = {
    "check": lambda p: ["clients", "check"] + (["--client", p["client"]] if p.get("client") else []),
    "device": lambda p: ["system-info"],
    "history": lambda p: ["status"] + (["--client", p["client"]] if p.get("client") else []),
    "open": lambda p: ["report", "open", "--client", p["client"]] + (["--period", p["period"]] if p.get("period") else []),
    "register": lambda p: ["clients", "add", "--name", p["name"]],
    # copies finished reports to the shared folder and rewrites its dashboard (never overwrites, contacts only Google Drive for the optional direct links)
    "export": lambda p: ["report", "export"],
}


class BadRequest(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.message, self.status = message, status


# --- validation (nothing reaches a command line without passing here) ------------------------------------------------------------------------

def _clean_client(value) -> str:
    try:
        return client_config.validate_client_key(value, where="client")
    except client_config.ClientConfigError:
        raise BadRequest("not a valid client key") from None


def _clean_period(value) -> str:
    try:
        contracts.parse_period_id(value)
    except contracts.ContractError:
        raise BadRequest("not a valid 28-day period (YYYY-MM-DD_YYYY-MM-DD)") from None
    return value


def _clean_name(value) -> str:
    try:
        return client_config.validate_client_name(value, where="name")
    except client_config.ClientConfigError:
        raise BadRequest("not a usable client name") from None


def clean_params(action: str, raw) -> dict:
    if not isinstance(raw, dict):
        raise BadRequest("the request must be a JSON object")
    out = {}
    if action in ("verify", "run", "open"):
        out["client"] = _clean_client(raw.get("client"))
    elif action in ("check", "history") and raw.get("client") not in (None, ""):
        out["client"] = _clean_client(raw.get("client"))
    if action == "run":
        out["period"] = _clean_period(raw.get("period"))
    if action == "open" and raw.get("period") not in (None, ""):
        out["period"] = _clean_period(raw.get("period"))
    if action == "register":
        out["name"] = _clean_name(raw.get("name"))
    return out


# --- running the command line --------------------------------------------------------------------------------------------------------------

class CliRunner:
    """Runs `python -m seo_report --json <argv>` in a child process and returns (exit_code, parsed JSON or None)."""

    def __init__(self, config_path=None, python=None, environ=None):
        self.config_args = ["--config", str(config_path)] if config_path else []
        self.python = python or sys.executable
        self.environ = dict(os.environ if environ is None else environ)

    def __call__(self, argv, timeout):
        src = str(Path(__file__).resolve().parents[1])
        env = {**self.environ, "PYTHONPATH": src, "PYTHONIOENCODING": "utf-8", "JASPER_SEO_SOURCE": "web"}  # the run is recorded as started from the page
        flags = 0x08000000 if os.name == "nt" else 0  # CREATE_NO_WINDOW: no console window flashes for each command
        try:
            done = subprocess.run([self.python, "-m", "seo_report", "--json", *self.config_args, *argv], capture_output=True, text=True, encoding="utf-8",
                                  errors="replace", timeout=timeout, env=env, cwd=str(rc.PROJECT_ROOT), creationflags=flags, stdin=subprocess.DEVNULL)
        except subprocess.TimeoutExpired:
            return 1, {"ok": False, "error": {"code": "timeout", "message": "the command did not finish in time and was stopped"}}
        lines = [line for line in done.stdout.splitlines() if line.strip()]
        try:
            data = json.loads(lines[-1]) if lines else None
        except ValueError:
            data = None
        return done.returncode, data


def summarize(action: str, code: int, data) -> dict:
    """One step's result for the page: a short sentence, plain-language advice, and whether it worked. Built only from the sanitized JSON."""
    if not isinstance(data, dict):
        return {"ok": False, "exit_code": code, "summary": "The command gave no usable answer. Check the console with `seo doctor`.", "advice": None}
    error = data.get("error") if isinstance(data.get("error"), dict) else None
    ok = code == 0
    if action == "verify" and data.get("attestation_issued"):
        summary = f"Google access verified (valid until {data.get('expires_at')})."
    elif action == "run" and "state" in data:
        summary = f"{data.get('client_key')} {data.get('period_id')}: {data.get('state')} ({data.get('outcome')})"
        if error:
            summary += f" - {error.get('code')} at {error.get('stage')}"
    elif action == "export" and "copied" in data:
        summary = (f"Shared folder updated: {len(data['copied'])} copied, {data.get('already_there', 0)} already there, "
                   f"{data.get('direct_links', 0)} direct PDF link(s) in the dashboard." if ok else "The shared folder was not fully updated; see the details.")
        if data.get("link_note") == "drive_signin_missing":
            summary += " Links open a Drive search; run `setup drive` once for direct links."
        elif data.get("link_note") == "drive_lookup_unavailable":
            summary += " Some direct links could not be looked up yet (Drive may still be uploading); press the button again in a few minutes."
    elif error:
        summary = str(error.get("message") or error.get("code") or "failed")
    else:
        summary = "Done." if ok else "Failed."
    code_name = (error or {}).get("code") if error else None
    advice = guidance.explain(code_name) or (guidance.explain(data.get("outcome")) if data.get("outcome") == "already_complete" else None)
    advice_data = {"meaning": advice.meaning, "fix": advice.fix} if advice else (
        {"meaning": error.get("meaning"), "fix": error.get("fix")} if error and error.get("fix") else None)
    blockers = (data.get("details") or {}).get("blockers") if isinstance(data.get("details"), dict) else None
    fixes = [f"{b}: {guidance.explain(b).fix}" for b in (blockers or []) if guidance.explain(b)]
    return {"ok": ok, "exit_code": code, "summary": summary, "advice": advice_data, "blocker_fixes": fixes,
            "report_exists": data.get("outcome") == "already_complete"}


# --- jobs ---------------------------------------------------------------------------------------------------------------------------------

class Job:
    def __init__(self, steps):
        self.id = uuid.uuid4().hex[:12]
        self.steps = [{"action": a, "params": p, "state": "waiting", "result": None} for a, p in steps]
        self.state = "running"
        self.started, self.finished = time.time(), None
        self.lock = threading.Lock()

    def view(self) -> dict:
        with self.lock:
            return {"id": self.id, "state": self.state, "started": self.started, "finished": self.finished,
                    "steps": [{"action": s["action"], "client": s["params"].get("client"), "period": s["params"].get("period"), "state": s["state"],
                               "result": s["result"]} for s in self.steps]}


class Jobs:
    """At most one mutating job at a time; finished jobs stay readable until the process ends."""

    def __init__(self, run_cli):
        self._run_cli, self._jobs, self._active, self._lock = run_cli, {}, None, threading.Lock()

    def start(self, steps) -> Job:
        with self._lock:
            if self._active is not None and self._active.state == "running":
                raise BadRequest("another job is still running; wait for it to finish", 409)
            job = Job(steps)
            self._jobs[job.id], self._active = job, job
        threading.Thread(target=self._work, args=(job,), daemon=True, name=f"job-{job.id}").start()
        return job

    def get(self, job_id: str):
        return self._jobs.get(job_id)

    def active(self):
        with self._lock:
            return self._active if self._active is not None and self._active.state == "running" else None

    def _work(self, job: Job) -> None:
        stopped = False
        for step in job.steps:
            if stopped:
                with job.lock:
                    step["state"], step["result"] = "skipped", {"ok": False, "summary": "Not started: an earlier step needs attention first.", "advice": None}
                continue
            with job.lock:
                step["state"] = "running"
            try:
                code, data = self._run_cli(ACTIONS[step["action"]](step["params"]), STEP_TIMEOUT)
                result = summarize(step["action"], code, data)
            except Exception:  # never let a worker thread die silently; the details are in the log
                LOGGER.exception("web job step failed unexpectedly")
                code, result = 1, {"ok": False, "exit_code": 1, "summary": "Unexpected internal error; see the log.", "advice": None}
            with job.lock:
                step["state"], step["result"] = ("done" if result["ok"] else "failed"), result
            if code in STOP_CODES:
                stopped = True
        with job.lock:
            job.state = "failed" if any(s["state"] in ("failed", "skipped") for s in job.steps) else "done"
            job.finished = time.time()


def _add_advice(data: dict) -> None:
    """Attach the plain-language meaning and fix to each client row of a `clients check` answer (so the page can show what to do)."""
    for row in data.get("clients") or []:
        if not isinstance(row, dict):
            continue
        for field, target in (("code", "advice"), ("verify_code", "verify_advice")):
            item = guidance.explain(row.get(field))
            if item:
                row[target] = {"meaning": item.meaning, "fix": item.fix}


# --- the HTTP server -----------------------------------------------------------------------------------------------------------------------

class WebServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = False

    def __init__(self, address, run_cli, token: str):
        super().__init__(address, Handler)
        self.token, self.run_cli, self.jobs = token, run_cli, Jobs(run_cli)
        self.read_lock = threading.Lock()

    @property
    def port(self) -> int:
        return self.server_address[1]

    def url(self) -> str:
        return f"http://localhost:{self.port}/?t={self.token}"


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "SeoReportLocal"
    sys_version = ""
    protocol_version = "HTTP/1.1"

    # -- plumbing --
    def log_message(self, format, *args):  # noqa: A002 - the base class signature; requests are not logged (the URL carries a token)
        return

    def _headers(self, status, content_type, length, nonce=None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")  # a refused request may leave an unread body behind, so no connection is reused
        self.close_connection = True
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        policy = "default-src 'none'; connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
        if nonce:
            policy += f"; script-src 'nonce-{nonce}'; style-src 'nonce-{nonce}'"
        self.send_header("Content-Security-Policy", policy)
        self.end_headers()

    def _send_json(self, status, body):
        raw = json.dumps(body, sort_keys=True).encode("utf-8")
        self._headers(status, "application/json; charset=utf-8", len(raw))
        self.wfile.write(raw)

    def _fail(self, status, message):
        self._send_json(status, {"ok": False, "error": {"message": message}})

    def _host_ok(self) -> bool:
        host = (self.headers.get("Host") or "").lower()
        return host in (f"localhost:{self.server.port}", f"{HOST}:{self.server.port}")

    def _origin_ok(self) -> bool:
        origin = self.headers.get("Origin")
        return origin is None or origin.lower() in (f"http://localhost:{self.server.port}", f"http://{HOST}:{self.server.port}")

    def _token_ok(self, candidate) -> bool:
        return isinstance(candidate, str) and hmac.compare_digest(candidate.encode("utf-8"), self.server.token.encode("utf-8"))

    def _body(self) -> dict:
        if (self.headers.get("Content-Type") or "").split(";")[0].strip().lower() != "application/json":
            raise BadRequest("the request must be application/json", 415)
        try:
            length = int(self.headers.get("Content-Length") or "")
        except ValueError:
            raise BadRequest("a Content-Length is required", 411) from None
        if not 0 <= length <= MAX_BODY:
            raise BadRequest("the request is too large", 413)
        try:
            return json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            raise BadRequest("the request is not valid JSON") from None

    # -- routing --
    def do_GET(self):
        self._route("GET")

    def do_POST(self):
        self._route("POST")

    def do_PUT(self):
        self._fail(405, "method not allowed")

    do_DELETE = do_PATCH = do_PUT

    def _route(self, method):
        try:
            if not self._host_ok():
                return self._fail(421, "unexpected host")
            path, _, query = self.path.partition("?")
            if method == "GET" and path == "/":
                token = dict(part.partition("=")[::2] for part in query.split("&") if part).get("t")
                if not self._token_ok(token):
                    return self._fail(403, "open the address printed by `seo web` (it contains the access token)")
                return self._page()
            if not path.startswith("/api/"):
                return self._fail(404, "not found")
            if not self._token_ok(self.headers.get("X-Token")):
                return self._fail(403, "missing or wrong access token")
            if method == "POST" and not self._origin_ok():
                return self._fail(403, "cross-origin request refused")
            self._api(method, path[len("/api/"):])
        except BadRequest as error:
            self._fail(error.status, error.message)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:
            LOGGER.exception("unexpected error in the web interface")
            self._fail(500, "unexpected internal error; see the log")

    def _page(self):
        from seo_report.webui_page import render
        nonce = secrets.token_urlsafe(16)
        raw = render(nonce, self.server.token).encode("utf-8")
        self._headers(200, "text/html; charset=utf-8", len(raw), nonce=nonce)
        self.wfile.write(raw)

    def _api(self, method, name):
        run = self.server.run_cli
        if method == "GET" and name.startswith("jobs/"):
            job = self.server.jobs.get(name[len("jobs/"):])
            return self._send_json(200, {"ok": True, "job": job.view()}) if job else self._fail(404, "unknown job")
        if method == "GET" and name == "jobs":
            active = self.server.jobs.active()
            return self._send_json(200, {"ok": True, "active": active.view() if active else None})
        if method == "POST" and name == "jobs":
            raw = self._body()
            steps_raw = raw.get("steps") if isinstance(raw, dict) else None
            if not isinstance(steps_raw, list) or not 1 <= len(steps_raw) <= MAX_STEPS:
                raise BadRequest(f"give 1 to {MAX_STEPS} steps")
            steps = []
            for item in steps_raw:
                action = item.get("action") if isinstance(item, dict) else None
                if action not in ACTIONS:
                    raise BadRequest("unknown action")
                steps.append((action, clean_params(action, item)))
            job = self.server.jobs.start(steps)
            return self._send_json(202, {"ok": True, "job": job.view()})
        if method == "POST" and name in READ_ONLY:
            params = clean_params(name, self._body())
            if name in ("register", "export") and self.server.jobs.active():
                raise BadRequest("wait for the running job to finish before " + ("changing the client list" if name == "register" else "updating the shared folder"), 409)
            with self.server.read_lock:  # one read-only command at a time keeps the sign-in refresh and the workbook read orderly
                code, data = run(READ_ONLY[name](params), READ_TIMEOUT)
            if name in ("check", "device", "history") and isinstance(data, dict):
                if name == "check":
                    _add_advice(data)
                return self._send_json(200, {"ok": True, "exit_code": code, "data": data})
            return self._send_json(200, {"ok": code == 0, "exit_code": code, "result": summarize(name, code, data), "data": data if isinstance(data, dict) else None})
        raise BadRequest("not found", 404)


# --- starting it ---------------------------------------------------------------------------------------------------------------------------

def make_server(run_cli, *, port: int = DEFAULT_PORT, token: str | None = None) -> WebServer:
    """A server on 127.0.0.1. The default port is preferred; if it is taken (or `port` is 0) the system chooses a free one."""
    token = token or secrets.token_urlsafe(24)
    for candidate in ([port, 0] if port else [0]):
        try:
            return WebServer((HOST, candidate), run_cli, token)
        except OSError:
            continue
    raise OSError("no local port is available")
