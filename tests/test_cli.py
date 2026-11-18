"""R8.6a: the command-line interface skeleton — parsing, validation, production guard, configuration, exit codes, error boundary.

Everything runs in-process with injected handlers or with temporary configuration files; one subprocess test checks the real entry point.
No Google call, no credential, no ledger, no production path.
"""
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from seo_report import cli
from seo_report import contracts
from seo_report import ledger as lg
from seo_report import runner_config as rc
from seo_report.cli_exit import (BY_ABORT_REASON, BY_CATEGORY, Exit, describe, exit_code_for_batch, exit_code_for_ledger_error,
                                exit_code_for_response)
from test_pdf_renderer import FONT_FILES, LOGO

PERIOD = "2026-09-29_2026-10-26"
ALL_KEYS = [("system-info", None), ("doctor", None), ("clients", "list"), ("clients", "validate"), ("setup", "auth"), ("verify", "live"), ("report", "dry-run"), ("report", "preview"),
            ("report", "run"), ("report", "batch"), ("status", None), ("recovery", "inspect"), ("recovery", "acknowledge"), ("recovery", "cancel"),
            ("clients", "check"), ("clients", "add"), ("report", "open"), ("web", None), ("setup", "device")]
RUN_KEY = f"example-co/{PERIOD}"


def run(argv, *, environ=None, handlers=None):
    return cli.main(argv, environ=environ if environ is not None else {}, handlers=handlers)


class Spy:
    """A handler that records the parsed arguments and returns 0."""

    def __init__(self):
        self.calls = []

    def __call__(self, ctx):
        self.calls.append(ctx.args)
        return 0


def spied(*keys):
    spy = Spy()
    return spy, {key: spy for key in keys}


@pytest.fixture
def config(tmp_path):
    local = tmp_path / "local"
    data = {"paths": {"output_root": str(local / "output"), "history_db": str(local / "history" / "h.sqlite"),
                      "ledger_db": str(local / "ledger" / "l.sqlite"), "work_root": str(local / "work")},
            "render": {"logo": str(LOGO), "fonts": [{"label": "DejaVuSans", "path": str(FONT_FILES["DejaVuSans"]), "weight": "400"}]},
            "execution": {"lock_ttl_seconds": 600}}
    path = tmp_path / "runner.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path, {"LOCALAPPDATA": str(local)}


# --- help, version, structure --------------------------------------------------------------------------------------------------------

def test_help_lists_every_command_group_and_the_exit_codes(capsys):
    assert run(["--help"]) == 0
    out = capsys.readouterr().out
    for word in ("system-info", "doctor", "clients", "report", "status", "recovery", "setup", "--config", "--json", "exit codes:"):
        assert word in out
    for code in Exit:
        if code != Exit.NOT_AVAILABLE:  # 13 is described as reserved in the help (see the next test)
            assert f"{int(code):>3}  {describe(code)}" in out


def test_the_help_describes_the_current_state_not_the_sub_stage_plan(capsys):
    assert run(["--help"]) == 0
    top = capsys.readouterr().out
    assert "reserved: a command that is parsed but not built yet (none at present)" in top and " 13  " in top
    for stale in ("(R8.6", "not available until that sub-stage", "not available in this build yet"):
        assert stale not in top
    assert "readiness gate" in top
    for argv in (["clients", "--help"], ["setup", "--help"], ["report", "--help"], ["report", "dry-run", "--help"], ["recovery", "inspect", "--help"]):
        assert run(argv) == 0
        text = capsys.readouterr().out
        assert "(R8.6" not in text and "scratch" not in text.lower(), argv
    assert run(["report", "--help"]) == 0
    assert "plan only" in capsys.readouterr().out
    assert run(["report", "dry-run", "--help"]) == 0
    dry = capsys.readouterr().out
    assert "--scratch-dir" not in dry and "--client" in dry and "--period" in dry
    assert run(["recovery", "--help"]) == 0 and "interrupted" not in capsys.readouterr().out
    assert Exit.NOT_AVAILABLE == 13 and Exit.OK == 0 and Exit.INTERRUPTED == 130  # numeric values are untouched


def test_the_help_has_no_machine_specific_text(capsys):
    for argv in (["--help"], ["report", "dry-run", "--help"], ["doctor", "--help"]):
        run(argv)
        text = capsys.readouterr().out
        assert os.path.expanduser("~") not in text and "OneDrive" not in text and "AppData" not in text


def test_version_and_help_of_subcommands(capsys):
    assert run(["--version"]) == 0 and cli.APP_VERSION in capsys.readouterr().out
    for argv in (["report", "--help"], ["report", "run", "--help"], ["recovery", "cancel", "--help"], ["clients", "validate", "--help"]):
        assert run(argv) == 0
        assert "usage:" in capsys.readouterr().out


@pytest.mark.parametrize("argv", [[], ["report"], ["recovery"], ["clients"], ["setup"], ["nonsense"], ["report", "frobnicate"]])
def test_missing_or_unknown_commands_are_usage_errors(argv, capsys):
    assert run(argv) == Exit.USAGE
    assert "usage:" in capsys.readouterr().err


def test_every_parsed_command_has_a_handler_and_a_known_delivery_stage():
    handlers = cli.default_handlers()
    assert ("system-info", None) in handlers
    assert set(handlers) == set(ALL_KEYS) and cli.DEFERRED == {}  # every command has its real handler since R8.6d
    assert cli.PRODUCTION_COMMANDS <= set(handlers)


# --- argument parsing and validation ---------------------------------------------------------------------------------------------------

GOOD = [
    (["report", "dry-run", "--client", "example-co", "--period", PERIOD], ("report", "dry-run")),
    (["report", "run", "--client", "example-co", "--period", PERIOD, "--production"], ("report", "run")),
    (["report", "batch", "--period", PERIOD, "--production"], ("report", "batch")),
    (["report", "batch", "--period", PERIOD, "--client", "a-co", "--client", "b-co", "--max-clients", "2", "--production"], ("report", "batch")),
    (["status"], ("status", None)), (["status", "--client", "example-co"], ("status", None)),
    (["status", "--run", RUN_KEY], ("status", None)), (["status", "--run", f"{RUN_KEY}~2"], ("status", None)),
    (["status", "--run", "0198c1a2-7b3c-7d4e-8f5a-000000001000"], ("status", None)),
    (["recovery", "inspect"], ("recovery", "inspect")),
    (["recovery", "acknowledge", "--run", RUN_KEY, "--operator", "op:jw", "--reason", "blocking_condition_resolved", "--production"],
     ("recovery", "acknowledge")),
    (["recovery", "cancel", "--run", f"{RUN_KEY}~2", "--operator", "jw", "--reason", "no_longer_needed", "--production"], ("recovery", "cancel")),
    (["clients", "list"], ("clients", "list")), (["clients", "validate", "--client", "example-co"], ("clients", "validate")),
    (["doctor"], ("doctor", None)), (["setup", "auth"], ("setup", "auth")),
]


@pytest.mark.parametrize("argv,key", GOOD, ids=[" ".join(a)[:60] for a, _ in GOOD])
def test_valid_commands_reach_their_handler_with_parsed_arguments(argv, key):
    spy, handlers = spied(key)
    assert run(argv, handlers=handlers) == 0
    assert len(spy.calls) == 1


def test_parsed_values_are_what_was_typed():
    spy, handlers = spied(("report", "batch"))
    run(["report", "batch", "--period", PERIOD, "--client", "a-co", "--client", "b-co", "--max-clients", "2", "--production"], handlers=handlers)
    args = spy.calls[0]
    assert (args.period, args.client, args.max_clients, args.production) == (PERIOD, ["a-co", "b-co"], 2, True)


@pytest.mark.parametrize("bad_client", ["Example-Co", "example co", "-x", "", "a" * 64, "x/y", "e\u00e9"])
@pytest.mark.parametrize("command", [["report", "dry-run"], ["report", "run", "--production"], ["status"]])
def test_an_invalid_client_key_is_rejected_before_any_handler_runs(command, bad_client, capsys):
    spy, handlers = spied(("report", "dry-run"), ("report", "run"), ("status", None))
    argv = command[:2 if command[0] == "report" else 1] + ["--client", bad_client] + (["--period", PERIOD] if command[0] == "report" else [])
    argv += [a for a in command if a == "--production"]
    assert run(argv, handlers=handlers) == Exit.USAGE and not spy.calls
    err = capsys.readouterr().err  # a value that looks like an option ("-x") is refused by argparse itself, still with code 2
    assert "valid client key" in err or bad_client.startswith("-")


@pytest.mark.parametrize("bad_period", ["2026-09-29", "2026-09-29_2026-10-25", "2026-09-29_2026-10-27", "2026-10-26_2026-09-29", "20260929_20261026",
                                        "2026-13-01_2026-14-01", "", "latest"])
def test_an_invalid_period_is_rejected_before_any_handler_runs(bad_period, capsys):
    spy, handlers = spied(("report", "dry-run"), ("report", "batch"))
    assert run(["report", "dry-run", "--client", "example-co", "--period", bad_period], handlers=handlers) == Exit.USAGE
    assert run(["report", "batch", "--period", bad_period, "--production"], handlers=handlers) == Exit.USAGE
    assert not spy.calls and "reporting period" in capsys.readouterr().err


@pytest.mark.parametrize("argv", [
    ["report", "dry-run", "--period", PERIOD], ["report", "dry-run", "--client", "example-co"], ["report", "run", "--production"],
    ["report", "batch", "--production"], ["status", "--client", "a-co", "--run", RUN_KEY], ["status", "--run", "not a run"],
    ["recovery", "cancel", "--run", RUN_KEY, "--reason", "no_longer_needed", "--production"],
    ["recovery", "cancel", "--run", RUN_KEY, "--operator", "jw", "--production"],
    ["recovery", "cancel", "--run", RUN_KEY, "--operator", "jw", "--reason", "because I said so", "--production"],
    ["recovery", "acknowledge", "--run", RUN_KEY, "--operator", "jw@example.test", "--reason", "data_corrected", "--production"],
    ["recovery", "acknowledge", "--run", "0198c1a2-7b3c-7d4e-8f5a-000000001000", "--operator", "jw", "--reason", "data_corrected", "--production"],
    ["report", "batch", "--period", PERIOD, "--max-clients", "0", "--production"],
    ["report", "batch", "--period", PERIOD, "--max-clients", "many", "--production"],
])
def test_missing_or_malformed_options_are_usage_errors(argv):
    spy, handlers = spied(*ALL_KEYS)
    assert run(argv, handlers=handlers) == Exit.USAGE and not spy.calls


# --- the production guard ------------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("argv", [
    ["report", "run", "--client", "example-co", "--period", PERIOD],
    ["report", "batch", "--period", PERIOD],
    ["recovery", "acknowledge", "--run", RUN_KEY, "--operator", "jw", "--reason", "data_corrected"],
    ["recovery", "cancel", "--run", RUN_KEY, "--operator", "jw", "--reason", "no_longer_needed"],
])
def test_commands_that_change_production_state_require_the_explicit_flag(argv, capsys):
    spy, handlers = spied(*ALL_KEYS)
    assert run(argv, handlers=handlers) == Exit.USAGE and not spy.calls
    assert "--production" in capsys.readouterr().err
    assert run(argv + ["--production"], handlers=handlers) == 0 and len(spy.calls) == 1


def test_the_flag_cannot_be_misplaced_before_the_command_or_added_to_a_dry_run(capsys):
    spy, handlers = spied(*ALL_KEYS)
    assert run(["--production", "report", "run", "--client", "example-co", "--period", PERIOD], handlers=handlers) == Exit.USAGE
    assert run(["report", "dry-run", "--client", "example-co", "--period", PERIOD, "--production"], handlers=handlers) == Exit.USAGE
    assert not spy.calls


def test_read_only_commands_need_no_flag_but_still_need_an_explicit_configuration(config, capsys):
    path, env = config
    assert run(["system-info"], environ={}) == Exit.CONFIG_INVALID
    assert "no configuration file was given" in capsys.readouterr().err
    assert run(["system-info", "--config", str(path)], environ=env) == Exit.OK


def test_the_production_commands_are_exactly_the_ones_that_write(capsys):
    assert cli.PRODUCTION_COMMANDS == {("report", "run"), ("report", "batch"), ("recovery", "acknowledge"), ("recovery", "cancel")}


# --- configuration and system-info ---------------------------------------------------------------------------------------------------------

def tree(folder):
    return sorted(str(p.relative_to(folder)) for p in Path(folder).rglob("*"))


def test_system_info_shows_version_paths_and_creates_nothing(config, tmp_path, capsys):
    path, env = config
    before = tree(tmp_path)
    assert run(["system-info", "--config", str(path)], environ=env) == Exit.OK
    out = capsys.readouterr().out
    assert cli.APP_VERSION in out and "ledger_db" in out and "l.sqlite" in out and "from --config" in out
    assert tree(tmp_path) == before  # no folder, database or log was created


def test_system_info_json_is_one_valid_object(config, capsys):
    path, env = config
    assert run(["--json", "system-info", "--config", str(path)], environ=env) == Exit.OK
    data = json.loads(capsys.readouterr().out)
    assert data["ok"] is True and data["version"] == cli.APP_VERSION and data["config"]["source"] == "--config"
    assert set(data["storage"]) == {"output_root", "work_root", "history_db", "ledger_db", "log_dir"} and "pending_sections" not in data
    assert data["live_google_verified"] is False and data["runner_readiness"]["production_ready"] is False


def test_the_configuration_can_come_from_the_environment_and_says_so(config, capsys):
    path, env = config
    assert run(["system-info"], environ={**env, cli.ENV_CONFIG: str(path)}) == Exit.OK
    assert f"from {cli.ENV_CONFIG}" in capsys.readouterr().out


def test_an_explicit_option_wins_over_the_environment(config, tmp_path, capsys):
    path, env = config
    assert run(["system-info", "--config", str(path)], environ={**env, cli.ENV_CONFIG: str(tmp_path / "other.yaml")}) == Exit.OK


@pytest.mark.parametrize("make,code", [("missing_file", "config_not_found"), ("directory", "config_not_found")])
def test_a_missing_configuration_file_is_a_config_error(tmp_path, make, code, capsys):
    target = tmp_path / "nothing.yaml"
    if make == "directory":
        target.mkdir()
    assert run(["--json", "system-info", "--config", str(target)]) == Exit.CONFIG_INVALID
    assert json.loads(capsys.readouterr().out)["error"]["code"] == code


@pytest.mark.parametrize("content,code", [("paths:\n  ledger_db: relative/ledger.sqlite\n", "path_not_absolute"),
                                          ("paths: [1, 2]\n", "object_expected"), ("{", "file_invalid"),
                                          ("workbooks:\n  reporting_spreadsheet_id: not-a-sheet-id\n", "spreadsheet_id_invalid"),
                                          ("execution:\n  api_key: abc\n", "credential_in_config")])
def test_an_invalid_configuration_names_the_problem_but_never_the_value(tmp_path, content, code, capsys):
    path = tmp_path / "bad.yaml"
    path.write_text(content, encoding="utf-8")
    assert run(["system-info", "--config", str(path)]) == Exit.CONFIG_INVALID
    err = capsys.readouterr().err
    assert code in err and "relative/ledger.sqlite" not in err and "abc" not in err.replace("api_key", "")


def test_options_are_accepted_before_or_after_the_command(config, capsys):
    path, env = config
    for argv in (["--config", str(path), "--json", "system-info"], ["system-info", "--config", str(path), "--json"]):
        assert run(argv, environ=env) == Exit.OK
        assert json.loads(capsys.readouterr().out)["ok"] is True


# --- commands that arrive in later sub-stages ----------------------------------------------------------------------------------------------

@pytest.mark.parametrize("argv,key", [(["status"], ("status", None)), (["recovery", "inspect"], ("recovery", "inspect"))])
def test_the_deferral_mechanism_says_when_a_command_arrives_and_does_nothing(argv, key, tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)  # since R8.6d no command is deferred; the mechanism is kept and still behaves as documented
    assert run(argv, handlers={key: cli.deferred_handler("R9.9")}) == Exit.NOT_AVAILABLE
    err = capsys.readouterr().err
    assert "R9.9" in err and "Nothing was read or changed" in err and "exit code 13" in err
    assert tree(tmp_path) == []


def test_deferred_commands_in_json_mode_report_the_same_code(capsys):
    assert run(["--json", "status"], handlers={("status", None): cli.deferred_handler("R9.9")}) == Exit.NOT_AVAILABLE
    data = json.loads(capsys.readouterr().out)
    assert data == {"error": {"code": "not_available", "message": data["error"]["message"]}, "exit_code": 13, "ok": False}


# --- exit codes -----------------------------------------------------------------------------------------------------------------------

def test_exit_codes_are_stable_unique_and_described():
    assert {c.name: int(c) for c in Exit} == {
        "OK": 0, "INTERNAL_ERROR": 1, "USAGE": 2, "CONFIG_INVALID": 3, "PREFLIGHT_FAILED": 4, "AUTH_REQUIRED": 5, "SOURCE_UNAVAILABLE": 6,
        "REPORT_FAILED": 7, "FINALIZATION_FAILED": 8, "AUDIT_FAILED": 9, "LEDGER_UNAVAILABLE": 10, "LOCK_CONFLICT": 11,
        "OPERATOR_ACTION_REQUIRED": 12, "NOT_AVAILABLE": 13, "INTERRUPTED": 130}
    assert len({int(c) for c in Exit}) == len(Exit) and all(describe(c) for c in Exit)


def test_every_error_category_has_an_exit_code():
    assert set(contracts.ERROR_CATEGORIES) <= set(BY_CATEGORY)
    assert set(contracts.ABORT_REASONS) == set(BY_ABORT_REASON)


def response(outcome, state, error=None):
    ts = {"requested_at": "2026-11-02T01:00:00+00:00", "updated_at": "2026-11-02T01:00:00+00:00", "finished_at": None}
    return contracts.RunResponse(
        request_id="0b9a6a52-6c1e-4f0e-9a43-6d3c6e2b9a01", idempotency_key="k", mode="production", run_key=RUN_KEY, client_key="example-co",
        period_id=PERIOD, state=state, outcome=outcome, attempt=1, timestamps=ts, error=error)


@pytest.mark.parametrize("outcome,state,error,expected", [
    ("succeeded", "audited", None, Exit.OK), ("already_complete", "audited", None, Exit.OK), ("in_progress", "requested", None, Exit.OK),
    ("blocked", "blocked", None, Exit.OPERATOR_ACTION_REQUIRED),
    ("failed", "failed", ("auth_required", "auth_token_missing", "collecting"), Exit.AUTH_REQUIRED),
    ("failed", "failed", ("source_temporary", "gsc_http_503", "collecting"), Exit.SOURCE_UNAVAILABLE),
    ("failed", "failed", ("source_denied", "gsc_http_403", "collecting"), Exit.SOURCE_UNAVAILABLE),
    ("failed", "failed", ("render_failed", "render_timeout_pdf", "rendering"), Exit.REPORT_FAILED),
    ("failed", "failed", ("finalization_failed", "promote_failed", "finalizing"), Exit.FINALIZATION_FAILED),
    ("blocked", "blocked", ("finalization_conflict", "existing_report_differs", "finalizing"), Exit.OPERATOR_ACTION_REQUIRED),
    ("blocked", "blocked", ("layout_issues", "render_has_layout_issues", "rendering"), Exit.OPERATOR_ACTION_REQUIRED),
    ("blocked", "blocked", ("audit_failed", "finding_partial_file_present", "auditing"), Exit.AUDIT_FAILED),
    ("failed", "failed", ("internal_error", "disk_full", "intake"), Exit.LEDGER_UNAVAILABLE),
    ("failed", "failed", ("internal_error", "exception_value_error", "collecting"), Exit.INTERNAL_ERROR),
    ("rejected", "requested", ("run_in_progress", "stale_state", "intake"), Exit.LOCK_CONFLICT),
    ("rejected", "requested", ("lock_unavailable", "ledger_busy", "intake"), Exit.LOCK_CONFLICT),
    ("rejected", "requested", ("validation_failed", "client_unknown", "intake"), Exit.CONFIG_INVALID),
    ("rejected", "requested", ("validation_failed", "period_differs_from_workbook", "intake"), Exit.CONFIG_INVALID),
    ("rejected", "blocked", ("validation_failed", "operator_required", "intake"), Exit.OPERATOR_ACTION_REQUIRED),
    ("rejected", "cancelled", ("validation_failed", "cancelled", "intake"), Exit.OPERATOR_ACTION_REQUIRED),
    ("rejected", "audited", ("validation_failed", "not_failed", "intake"), Exit.OPERATOR_ACTION_REQUIRED),
])
def test_response_to_exit_code_mapping(outcome, state, error, expected):
    info = contracts.error_info(*error) if error else None
    resp = response(outcome, state, info)
    assert exit_code_for_response(resp) == expected == exit_code_for_response(resp.to_dict())


def test_batch_exit_codes_follow_the_abort_reason_then_the_most_serious_result():
    ok, fail_src, blocked = (response("succeeded", "audited"),
                             response("failed", "failed", contracts.error_info("source_temporary", "gsc_http_503", "collecting")),
                             response("blocked", "blocked", contracts.error_info("audit_failed", "x", "auditing")))
    batch = lambda results, reason=None: SimpleNamespace(aborted=reason is not None, abort_reason=reason, results=results)  # noqa: E731
    assert exit_code_for_batch(batch([ok, ok])) == Exit.OK
    assert exit_code_for_batch(batch([ok, fail_src])) == Exit.SOURCE_UNAVAILABLE
    assert exit_code_for_batch(batch([fail_src, blocked])) == Exit.SOURCE_UNAVAILABLE  # the lowest-numbered failure leads
    assert exit_code_for_batch(batch([ok], "auth_required")) == Exit.AUTH_REQUIRED
    assert exit_code_for_batch(batch([ok], "ledger_unavailable")) == Exit.LEDGER_UNAVAILABLE
    assert exit_code_for_batch(batch([ok], "disk_full")) == Exit.LEDGER_UNAVAILABLE
    assert exit_code_for_batch(batch([ok], "lock_unavailable")) == Exit.LOCK_CONFLICT


@pytest.mark.parametrize("code,expected", [("lock_held", Exit.LOCK_CONFLICT), ("stale_state", Exit.LOCK_CONFLICT),
                                           ("ledger_busy", Exit.LOCK_CONFLICT), ("ledger_unavailable", Exit.LEDGER_UNAVAILABLE),
                                           ("ledger_disk_full", Exit.LEDGER_UNAVAILABLE), ("ledger_corrupt", Exit.LEDGER_UNAVAILABLE)])
def test_ledger_errors_map_to_lock_conflict_or_unavailable(code, expected):
    assert exit_code_for_ledger_error(lg.LedgerError(code)) == expected


# --- the error boundary ----------------------------------------------------------------------------------------------------------------

def raising(error):
    def handler(ctx):
        raise error
    return {("system-info", None): handler}


@pytest.mark.parametrize("error,expected", [
    (KeyboardInterrupt(), Exit.INTERRUPTED), (rc.RunnerConfigError("path_not_absolute", "paths.ledger_db"), Exit.CONFIG_INVALID),
    (contracts.StageError("auth_required", "auth_token_missing"), Exit.AUTH_REQUIRED),
    (contracts.StageError("source_temporary", "auth_network_error"), Exit.SOURCE_UNAVAILABLE),
    (lg.LockHeldError("lock_held"), Exit.LOCK_CONFLICT), (lg.LedgerError("ledger_unavailable"), Exit.LEDGER_UNAVAILABLE),
    (cli.CliError(Exit.PREFLIGHT_FAILED, "mandatory asset missing", "asset_missing"), Exit.PREFLIGHT_FAILED),
    (RuntimeError("boom"), Exit.INTERNAL_ERROR)])
def test_exceptions_from_a_handler_become_stable_exit_codes(error, expected, capsys):
    assert run(["system-info"], handlers=raising(error)) == expected
    err = capsys.readouterr().err
    assert f"exit code {int(expected)}" in err and "Traceback" not in err


def test_an_unexpected_exception_never_prints_its_text(capsys):
    leak = RuntimeError("token=ya29.SYNTHETICSECRETVALUE0123456789 in C:\\Users\\someone\\Clients\\Alpha Co")
    assert run(["system-info"], handlers=raising(leak)) == Exit.INTERNAL_ERROR
    out = capsys.readouterr()
    assert "ya29" not in out.err + out.out and "someone" not in out.err + out.out and "RuntimeError" in out.err
    assert "details are in the log" in out.err


def test_system_exit_from_a_handler_is_never_swallowed():
    with pytest.raises(SystemExit) as caught:
        run(["system-info"], handlers=raising(SystemExit(7)))
    assert caught.value.code == 7


def test_unexpected_errors_are_logged_with_a_traceback_for_the_sanitized_log(caplog):
    with caplog.at_level("ERROR", logger="seo_report.cli"):
        run(["system-info"], handlers=raising(RuntimeError("boom")))
    assert any(r.exc_info and r.exc_info[0] is RuntimeError for r in caplog.records)


# --- the real entry point and import hygiene ----------------------------------------------------------------------------------------------

def entry(*args):
    import tempfile
    env = {**os.environ, "PYTHONPATH": str(ROOT / "src"), "LOCALAPPDATA": tempfile.mkdtemp(prefix="seo-cli-test-")}  # real logging runs here
    env.pop(cli.ENV_CONFIG, None)
    return subprocess.run([sys.executable, "-m", "seo_report", *args], capture_output=True, text=True, env=env, cwd=str(ROOT.parent), timeout=120)


def test_python_dash_m_seo_report_works():
    result = entry("--version")
    assert result.returncode == 0 and f"seo-report {cli.APP_VERSION}" in result.stdout
    assert entry("report", "run", "--client", "example-co", "--period", PERIOD).returncode == Exit.USAGE
    assert entry("system-info").returncode == Exit.CONFIG_INVALID
    assert entry("status").returncode == Exit.CONFIG_INVALID  # a real handler now: it needs a settings file
    assert entry("doctor", "--skip-browser-launch").returncode == Exit.CONFIG_INVALID  # no settings file was named


def test_importing_the_cli_loads_no_google_library_and_no_credential_module():
    code = "import sys, seo_report.cli; print(sorted(m for m in sys.modules if m.startswith(('googleapiclient','google_auth','seo_report.auth','playwright'))))"
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env={**os.environ, "PYTHONPATH": str(ROOT / "src")})
    assert result.returncode == 0 and result.stdout.strip() == "[]", result.stdout + result.stderr


def test_the_cli_source_has_no_network_credential_or_deletion_code():
    source = (ROOT / "src" / "seo_report" / "cli.py").read_text(encoding="utf-8")
    code = source.split('"""', 2)[2]
    for word in ("requests", "urllib", "socket", "smtplib", "token.json", "client_secret", "unlink", "rmtree", "os.remove", "run_local_server"):
        assert word not in code, word

def test_the_cli_reference_documents_every_command_flag_and_exit_code():
    doc = (ROOT / "docs" / "r8" / "r86_cli_reference.md").read_text(encoding="utf-8")
    for command in ("system-info", "doctor", "clients list", "clients validate", "report dry-run", "report run", "report batch", "status",
                    "recovery inspect", "recovery acknowledge", "recovery cancel", "setup auth", "--production", "JASPER_SEO_CONFIG"):
        assert command in doc, command
    for code in Exit:
        assert f"| {int(code)} | {code.name} |" in doc, code.name
