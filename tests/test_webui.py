"""The local web page (`seo web`): its lock-down, its fixed set of actions, its jobs and its wiring.

No Google call and no report is made: the page's command runner is replaced by a recording fake, and the server listens on a free local port in a thread.
"""
import http.client
import json
import re
import sys
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from seo_report import cli, webui, webui_page  # noqa: E402
from seo_report.cli_exit import Exit  # noqa: E402
from test_r86d_report_status_recovery import home  # noqa: E402,F401  (a settings fixture; its network guard is deliberately not imported: this test talks to 127.0.0.1)

TOKEN = "test-token-123"
PERIOD = "2026-10-01_2026-10-28"


class FakeCli:
    """Records every command line the page runs and answers from a table (default: success)."""

    def __init__(self):
        self.calls, self.answers, self.gate = [], {}, None
        self.lock = threading.Lock()

    def __call__(self, argv, timeout):
        if self.gate is not None:
            self.gate.wait(10)
        with self.lock:
            self.calls.append(list(argv))
        for prefix, answer in self.answers.items():
            if argv[:len(prefix)] == list(prefix):
                return answer
        if argv[:2] == ["verify", "live"]:
            return 0, {"ok": True, "attestation_issued": True, "expires_at": "2026-11-24T00:00:00+00:00", "client_key": argv[3]}
        if argv[:2] == ["report", "run"]:
            return 0, {"ok": True, "client_key": argv[3], "period_id": argv[5], "state": "audited", "outcome": "succeeded", "error": None}
        if argv[:2] == ["clients", "check"]:
            return 0, {"ok": True, "counts": {}, "clients": [{"client_key": "a-co", "name": "A Co", "status": "ready", "period_id": PERIOD, "report": "not_run",
                                                               "google_verified": False, "verify_code": "attestation_missing", "code": None},
                                                              {"client_key": "b-co", "name": "B Co", "status": "needs_fix", "code": "keywords_missing"}]}
        return 0, {"ok": True}


@pytest.fixture
def server():
    fake = FakeCli()
    srv = webui.make_server(fake, port=0, token=TOKEN)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    srv.fake = fake
    yield srv
    srv.shutdown()
    srv.server_close()


def call(srv, method, path, body=None, headers=None, raw=None, token=True, host=None):
    conn = http.client.HTTPConnection("127.0.0.1", srv.port, timeout=10)
    hdrs = {"Host": host or f"127.0.0.1:{srv.port}"}
    if token:
        hdrs["X-Token"] = TOKEN
    if body is not None or raw is not None:
        hdrs["Content-Type"] = "application/json"
    hdrs.update(headers or {})
    payload = raw if raw is not None else (json.dumps(body) if body is not None else None)
    conn.request(method, path, body=payload, headers=hdrs)
    resp = conn.getresponse()
    data = resp.read()
    conn.close()
    try:
        parsed = json.loads(data)
    except ValueError:
        parsed = data.decode("utf-8", "replace")
    return resp.status, parsed, dict(resp.getheaders())


def wait_done(srv, job_id, seconds=10):
    end = time.time() + seconds
    while time.time() < end:
        status, body, _ = call(srv, "GET", f"/api/jobs/{job_id}")
        if body["job"]["state"] != "running":
            return body["job"]
        time.sleep(0.05)
    raise AssertionError("the job did not finish")


# === lock-down =================================================================================================================

def test_it_listens_on_the_loopback_address_only(server):
    assert server.server_address[0] == "127.0.0.1" and webui.HOST == "127.0.0.1"


def test_the_page_needs_the_token_and_is_served_with_strict_headers(server):
    assert call(server, "GET", "/", token=False)[0] == 403
    assert call(server, "GET", "/?t=wrong", token=False)[0] == 403
    status, html, headers = call(server, "GET", f"/?t={TOKEN}", token=False)
    assert status == 200 and headers["Cache-Control"] == "no-store" and headers["X-Content-Type-Options"] == "nosniff"
    policy = headers["Content-Security-Policy"]
    nonce = re.search(r"script-src 'nonce-([^']+)'", policy).group(1)
    assert "default-src 'none'" in policy and "frame-ancestors 'none'" in policy and f'<script nonce="{nonce}">' in html and f'<style nonce="{nonce}">' in html
    assert TOKEN in html and "__NONCE__" not in html and "__TOKEN__" not in html
    assert headers["Referrer-Policy"] == "no-referrer" and headers["X-Frame-Options"] == "DENY"


def test_the_page_loads_nothing_from_outside_and_builds_dom_without_html_injection():
    page = webui_page.PAGE
    assert not re.search(r"(?:src|href|url\()\s*=?\s*[\"']?https?://", page) and "@import" not in page
    assert "innerHTML" not in page and "outerHTML" not in page and "insertAdjacentHTML" not in page and "document.write" not in page and "eval(" not in page


def test_the_nonce_changes_on_every_page_load(server):
    first = call(server, "GET", f"/?t={TOKEN}", token=False)[2]["Content-Security-Policy"]
    second = call(server, "GET", f"/?t={TOKEN}", token=False)[2]["Content-Security-Policy"]
    assert first != second


@pytest.mark.parametrize("token", [False])
def test_the_api_needs_the_token(server, token):
    assert call(server, "POST", "/api/check", {}, token=False)[0] == 403
    assert call(server, "POST", "/api/check", {}, headers={"X-Token": "nope"})[0] == 403
    assert call(server, "GET", "/api/jobs", token=False)[0] == 403
    assert server.fake.calls == []


def test_a_foreign_host_header_is_refused_even_with_the_token(server):
    for host in ("evil.example", f"evil.example:{server.port}", "127.0.0.1", "0.0.0.0:1"):
        assert call(server, "GET", f"/?t={TOKEN}", token=False, host=host)[0] == 421
    assert call(server, "POST", "/api/check", {}, host="localhost:1")[0] == 421
    assert call(server, "POST", "/api/check", {}, host=f"localhost:{server.port}")[0] == 200
    assert call(server, "POST", "/api/check", {}, host=f"LOCALHOST:{server.port}")[0] == 200


def test_a_cross_origin_post_is_refused(server):
    assert call(server, "POST", "/api/check", {}, headers={"Origin": "http://evil.example"})[0] == 403
    assert call(server, "POST", "/api/check", {}, headers={"Origin": "null"})[0] == 403
    assert call(server, "POST", "/api/check", {}, headers={"Origin": f"http://localhost:{server.port}"})[0] == 200
    assert call(server, "POST", "/api/check", {}, headers={"Origin": f"http://127.0.0.1:{server.port}"})[0] == 200


def test_the_request_must_be_json_and_a_sane_size(server):
    assert call(server, "POST", "/api/check", raw="{}", headers={"Content-Type": "text/plain"})[0] == 415
    assert call(server, "POST", "/api/check", raw="not json")[0] == 400
    assert call(server, "POST", "/api/check", raw="[1,2]")[0] == 400
    # the declared size is what is refused (before any of the body is read), so only the header is oversized here
    assert call(server, "POST", "/api/jobs", raw="{}", headers={"Content-Length": str(webui.MAX_BODY + 10)})[0] == 413


def test_only_get_and_post_exist_and_unknown_routes_are_404(server):
    for method in ("PUT", "DELETE", "PATCH"):
        assert call(server, method, "/api/check", {})[0] == 405
    assert call(server, "GET", "/etc/passwd", token=False)[0] == 404
    assert call(server, "GET", "/api/../x")[0] in (403, 404)
    assert call(server, "POST", "/api/shell", {"cmd": "calc"})[0] == 404
    assert call(server, "GET", "/api/jobs/nope")[0] == 404


# === the fixed set of actions ==================================================================================================

@pytest.mark.parametrize("client", ["../x", "a b", "A-Co", "x --production", "x;calc", "", None, 5, "-rf", "con", "a" * 64])
def test_a_bad_client_is_refused_before_any_command_is_built(server, client):
    for action in ("verify", "run"):
        status, _, _ = call(server, "POST", "/api/jobs", {"steps": [{"action": action, "client": client, "period": PERIOD}]})
        assert status == 400
    assert server.fake.calls == []


@pytest.mark.parametrize("period", ["2026-10-01_2026-10-27", "2026-10-01", "x", "2026-10-01_2026-10-28 --force", None, "../2026-10-01_2026-10-28"])
def test_a_bad_period_is_refused(server, period):
    status, _, _ = call(server, "POST", "/api/jobs", {"steps": [{"action": "run", "client": "a-co", "period": period}]})
    assert status == 400 and server.fake.calls == []


def test_unknown_actions_extra_options_and_oversized_jobs_are_refused(server):
    for steps in ([{"action": "doctor"}], [{"action": "recovery", "client": "a-co"}], [{"action": "run; calc", "client": "a-co"}], "run", [], None,
                  [{"action": "run", "client": "a-co", "period": PERIOD}] * (webui.MAX_STEPS + 1), ["verify"]):
        assert call(server, "POST", "/api/jobs", {"steps": steps})[0] == 400
    assert server.fake.calls == []


def test_extra_fields_never_become_command_line_options(server):
    status, body, _ = call(server, "POST", "/api/jobs", {"steps": [{"action": "run", "client": "a-co", "period": PERIOD, "production": False, "config": "x.yaml",
                                                                     "argv": ["--force"], "flags": "--no-gate"}]})
    assert status == 202
    wait_done(server, body["job"]["id"])
    assert server.fake.calls == [["report", "run", "--client", "a-co", "--period", PERIOD, "--production"]]


def test_the_commands_the_page_can_start_are_exactly_these():
    assert sorted(webui.ACTIONS) == ["run", "verify"] and sorted(webui.READ_ONLY) == ["check", "device", "history", "open", "register"]
    assert webui.ACTIONS["verify"]({"client": "k"}) == ["verify", "live", "--client", "k", "--live"]
    assert "--production" in webui.ACTIONS["run"]({"client": "k", "period": PERIOD})
    assert not any(token in " ".join(sum((b({"client": "k", "period": PERIOD, "name": "N"}) for b in webui.READ_ONLY.values()), []))
                   for token in ("--production", "--live", "recovery", "setup"))


# === jobs ======================================================================================================================

def test_a_job_runs_its_steps_in_order_and_reports_each_result(server):
    steps = [{"action": "verify", "client": "a-co"}, {"action": "run", "client": "a-co", "period": PERIOD},
             {"action": "verify", "client": "b-co"}, {"action": "run", "client": "b-co", "period": PERIOD}]
    status, body, _ = call(server, "POST", "/api/jobs", {"steps": steps})
    assert status == 202
    job = wait_done(server, body["job"]["id"])
    assert job["state"] == "done" and [s["state"] for s in job["steps"]] == ["done"] * 4
    assert [c[:2] for c in server.fake.calls] == [["verify", "live"], ["report", "run"], ["verify", "live"], ["report", "run"]]
    assert [c[3] for c in server.fake.calls] == ["a-co", "a-co", "b-co", "b-co"]
    assert "Google access verified" in job["steps"][0]["result"]["summary"] and "audited (succeeded)" in job["steps"][1]["result"]["summary"]


def test_only_one_job_runs_at_a_time(server):
    server.fake.gate = threading.Event()
    first = call(server, "POST", "/api/jobs", {"steps": [{"action": "verify", "client": "a-co"}]})
    assert first[0] == 202
    assert call(server, "POST", "/api/jobs", {"steps": [{"action": "verify", "client": "b-co"}]})[0] == 409
    assert call(server, "POST", "/api/register", {"name": "New Co"})[0] == 409
    active = call(server, "GET", "/api/jobs")[1]["active"]
    assert active is not None and active["id"] == first[1]["job"]["id"]
    server.fake.gate.set()
    wait_done(server, first[1]["job"]["id"])
    assert call(server, "GET", "/api/jobs")[1]["active"] is None
    assert call(server, "POST", "/api/jobs", {"steps": [{"action": "verify", "client": "b-co"}]})[0] == 202


def test_a_sign_in_problem_stops_the_later_steps_and_explains_what_to_do(server):
    server.fake.answers[("verify", "live")] = (5, {"ok": False, "exit_code": 5, "error": {"code": "auth_credentials_revoked", "message": "the sign-in is gone"}})
    steps = [{"action": "verify", "client": "a-co"}, {"action": "run", "client": "a-co", "period": PERIOD}, {"action": "verify", "client": "b-co"}]
    job = wait_done(server, call(server, "POST", "/api/jobs", {"steps": steps})[1]["job"]["id"])
    assert job["state"] == "failed" and [s["state"] for s in job["steps"]] == ["failed", "skipped", "skipped"]
    advice = job["steps"][0]["result"]["advice"]
    assert "setup auth" in advice["fix"] and len(server.fake.calls) == 1


def test_an_ordinary_failure_does_not_stop_the_other_clients(server):
    server.fake.answers[("report", "run", "--client", "a-co")] = (6, {"ok": False, "error": {"code": "gsc_property_not_accessible", "message": "denied"}, "state": "failed",
                                                                     "outcome": "failed", "client_key": "a-co", "period_id": PERIOD})
    steps = [{"action": "run", "client": "a-co", "period": PERIOD}, {"action": "run", "client": "b-co", "period": PERIOD}]
    job = wait_done(server, call(server, "POST", "/api/jobs", {"steps": steps})[1]["job"]["id"])
    assert [s["state"] for s in job["steps"]] == ["failed", "done"] and job["state"] == "failed"
    assert "Search Console" in job["steps"][0]["result"]["advice"]["meaning"]


def test_a_gate_block_lists_a_fix_for_each_blocker_and_stops(server):
    server.fake.answers[("report", "run")] = (4, {"ok": False, "error": {"code": "production_gate_blocked", "message": "blocked"},
                                                  "details": {"blockers": ["live_google_not_verified", "unknown_blocker"]}})
    job = wait_done(server, call(server, "POST", "/api/jobs", {"steps": [{"action": "run", "client": "a-co", "period": PERIOD},
                                                                          {"action": "run", "client": "b-co", "period": PERIOD}]})[1]["job"]["id"])
    first = job["steps"][0]["result"]
    assert job["steps"][1]["state"] == "skipped" and len(first["blocker_fixes"]) == 1 and "verify live" in first["blocker_fixes"][0]


def test_an_already_complete_report_is_explained_not_called_a_failure():
    result = webui.summarize("run", 0, {"ok": True, "client_key": "a-co", "period_id": PERIOD, "state": "audited", "outcome": "already_complete", "error": None})
    assert result["ok"] is True and result["report_exists"] is True and "never overwritten" in result["advice"]["meaning"]


def test_a_command_that_gives_no_answer_is_reported_safely():
    for data in (None, "text", 5, []):
        result = webui.summarize("run", 1, data)
        assert result["ok"] is False and result["advice"] is None and "doctor" in result["summary"]


def test_an_unexpected_error_in_a_worker_is_contained(server):
    def boom(argv, timeout):
        raise RuntimeError("secret path C:\\Users\\x")
    server.jobs._run_cli = boom
    job = wait_done(server, call(server, "POST", "/api/jobs", {"steps": [{"action": "verify", "client": "a-co"}]})[1]["job"]["id"])
    assert job["state"] == "failed" and "secret" not in json.dumps(job) and "Unexpected internal error" in job["steps"][0]["result"]["summary"]


# === read-only commands ========================================================================================================

def test_check_returns_the_table_with_plain_language_advice(server):
    status, body, _ = call(server, "POST", "/api/check", {})
    assert status == 200 and server.fake.calls == [["clients", "check"]]
    rows = {r["client_key"]: r for r in body["data"]["clients"]}
    assert "verify live" in rows["a-co"]["verify_advice"]["fix"] and "keyword" in rows["b-co"]["advice"]["meaning"]


def test_device_history_open_and_register_run_only_their_own_commands(server):
    call(server, "POST", "/api/device", {})
    call(server, "POST", "/api/history", {"client": "a-co"})
    call(server, "POST", "/api/open", {"client": "a-co", "period": PERIOD})
    status, body, _ = call(server, "POST", "/api/register", {"name": "New Co"})
    assert status == 200 and body["ok"] is True
    assert server.fake.calls == [["system-info"], ["status", "--client", "a-co"], ["report", "open", "--client", "a-co", "--period", PERIOD],
                                 ["clients", "add", "--name", "New Co"]]


@pytest.mark.parametrize("path,body", [("open", {"client": "../x"}), ("open", {}), ("register", {"name": " padded "}), ("register", {"name": ""}),
                                       ("register", {"name": 5}), ("check", {"client": "BAD KEY"}), ("history", {"client": "x y"})])
def test_read_only_commands_validate_their_arguments_too(server, path, body):
    assert call(server, "POST", f"/api/{path}", body)[0] == 400 and server.fake.calls == []


# === the real command runner ===================================================================================================

def test_the_command_runner_builds_a_plain_argument_list_without_a_shell(monkeypatch):
    seen = {}

    def fake_run(args, **kwargs):
        seen.update(args=args, kwargs=kwargs)
        return type("R", (), {"stdout": "warning line\n" + json.dumps({"ok": True}) + "\n", "returncode": 0})()
    monkeypatch.setattr(webui.subprocess, "run", fake_run)
    code, data = webui.CliRunner(config_path="C:/x/runner.yaml", python="py.exe", environ={"A": "1"})(["clients", "check"], 30)
    assert (code, data) == (0, {"ok": True})
    assert seen["args"] == ["py.exe", "-m", "seo_report", "--json", "--config", "C:/x/runner.yaml", "clients", "check"]
    assert seen["kwargs"].get("shell") in (None, False) and seen["kwargs"]["timeout"] == 30 and seen["kwargs"]["env"]["A"] == "1"
    assert seen["kwargs"]["env"]["PYTHONPATH"].endswith("src") and seen["kwargs"]["stdin"] is webui.subprocess.DEVNULL


def test_the_command_runner_survives_garbage_and_timeouts(monkeypatch):
    monkeypatch.setattr(webui.subprocess, "run", lambda *a, **k: type("R", (), {"stdout": "not json", "returncode": 3})())
    assert webui.CliRunner()(["x"], 5) == (3, None)

    def slow(*a, **k):
        raise webui.subprocess.TimeoutExpired("x", 1)
    monkeypatch.setattr(webui.subprocess, "run", slow)
    code, data = webui.CliRunner()(["x"], 5)
    assert code == 1 and data["error"]["code"] == "timeout"


# === the `web` command =========================================================================================================

def test_the_web_command_prints_the_address_opens_the_browser_and_stops_on_ctrl_c(home, monkeypatch, capsys):
    import webbrowser
    closed, opened = [], []

    class FakeServer:
        port = 8765

        def url(self):
            return "http://localhost:8765/?t=abc"

        def serve_forever(self):
            raise KeyboardInterrupt

        def server_close(self):
            closed.append(True)
    seen = {}
    monkeypatch.setattr(webui, "make_server", lambda runner, port: seen.update(runner=runner, port=port) or FakeServer())
    monkeypatch.setattr(webbrowser, "open", lambda url: opened.append(url))
    assert cli.main(["web", "--config", str(home.path)], environ=home.env) == Exit.OK
    out = capsys.readouterr().out
    assert "http://localhost:8765/?t=abc" in out and "127.0.0.1" in out and "Stopped." in out
    assert opened == ["http://localhost:8765/?t=abc"] and closed == [True] and seen["port"] == 8765 and seen["runner"].config_args == ["--config", str(home.path.resolve())]
    opened.clear()
    assert cli.main(["web", "--no-browser", "--config", str(home.path)], environ=home.env) == Exit.OK and opened == []


def test_the_web_command_reports_a_broken_configuration_instead_of_starting(home, monkeypatch, capsys):
    started = []
    monkeypatch.setattr(webui, "make_server", lambda *a, **k: started.append(1))
    assert cli.main(["web", "--config", str(home.tmp / "missing.yaml")], environ=home.env) == Exit.CONFIG_INVALID and started == []

def test_the_web_command_is_wired_with_a_safe_default_port():
    parser = cli.build_parser()
    args = parser.parse_args(["web"])
    assert args.port == 8765 and args.no_browser is False
    for bad in ("80", "99999", "abc"):
        with pytest.raises(SystemExit):
            parser.parse_args(["web", "--port", bad])
    assert cli.build_parser().parse_args(["web", "--no-browser", "--port", "9000"]).port == 9000
    assert ("web", None) in cli.default_handlers() and ("web", None) not in cli.PRODUCTION_COMMANDS


def test_a_busy_preferred_port_falls_back_to_a_free_one():
    first = webui.make_server(FakeCli(), port=0)
    try:
        second = webui.make_server(FakeCli(), port=first.port)
        try:
            assert second.port != first.port and second.server_address[0] == "127.0.0.1"
        finally:
            second.server_close()
    finally:
        first.server_close()
