"""R8.6c: logging activation at the CLI boundary, the local read-only preflight, and the `doctor` / `system-info` commands.

Synthetic files and temporary folders only. A guard fixture fails any network connection and any browser sign-in; the preflight is run with probes
for Playwright/Chromium except in the two tests that start the real local browser (skipped if Chromium is not installed).
"""
import dataclasses
import datetime as dt
import errno
import json
import logging
import os
import re
import shutil
import socket
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from google.oauth2.credentials import Credentials

from seo_report import asset_fingerprints as af
from seo_report import auth
from seo_report import cli
from seo_report import preflight as pf
from seo_report import runner
from seo_report.cli_exit import Exit
from test_pdf_renderer import FONT_FILES, LOGO

REAL_ACTIVATE = cli.activate_logging  # captured at import, before the autouse fixture in conftest.py replaces it
NOW = dt.datetime(2026, 11, 1, 12, 0, 0, tzinfo=dt.timezone.utc)
PERIOD = "2026-09-29_2026-10-26"


@pytest.fixture(autouse=True)
def no_network_no_browser_signin(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("a network connection was attempted")
    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    import google_auth_oauthlib.flow as flow
    monkeypatch.setattr(flow.InstalledAppFlow, "run_local_server", lambda *a, **k: (_ for _ in ()).throw(AssertionError("sign-in flow started")))


@pytest.fixture
def logged(monkeypatch):
    """Real logging activation for the test."""
    monkeypatch.setattr(cli, "activate_logging", REAL_ACTIVATE)


def tagged_handlers():
    return [h for h in logging.getLogger("seo_report").handlers if getattr(h, runner._HANDLER_TAG, False)]


# --- a complete, synthetic, healthy installation ------------------------------------------------------------------------------------

PROBES_OK = {"chromium_folders": lambda env: ["chromium-1234"], "launch_probe": lambda env: {"launched": True, "browser": "156.0.0.0"}}


@pytest.fixture
def home(tmp_path):
    """Everything `doctor` needs, in temporary folders: settings, registry, sender, assets, credential files, storage."""
    local = tmp_path / "local"
    for sub in ("output", "work", "history", "ledger"):
        (local / sub).mkdir(parents=True)
    creds = tmp_path / "creds"
    creds.mkdir()
    (creds / "client.json").write_text("{}", encoding="utf-8")
    (creds / "login.json").write_text("{}", encoding="utf-8")
    (tmp_path / "clients.yaml").write_text(yaml.safe_dump({"clients": [{"name": "Alpha Co", "client_key": "alpha-co"}]}), encoding="utf-8")
    (tmp_path / "jasper.yaml").write_text(yaml.safe_dump({"sender": {
        "name": "Sample", "title": "PM", "address_lines": ["1 Way"], "phone": "(000) 000-0000", "email": "sender@example.test",
        "website_label": "S", "website_url": "https://sender.example"}}), encoding="utf-8")
    data = {"paths": {"output_root": str(local / "output"), "history_db": str(local / "history" / "h.sqlite"),
                      "ledger_db": str(local / "ledger" / "l.sqlite"), "work_root": str(local / "work"),
                      "clients_config": str(tmp_path / "clients.yaml"), "sender_config": str(tmp_path / "jasper.yaml"),
                      "oauth_client_file": str(creds / "client.json"), "oauth_cache_file": str(creds / "login.json")},
            "render": {"logo": str(LOGO), "fonts": [{"label": "DejaVuSans", "path": str(FONT_FILES["DejaVuSans"]), "weight": "400"},
                                                     {"label": "DejaVuSans-Bold", "path": str(FONT_FILES["DejaVuSans-Bold"]), "weight": "700"}]},
            "workbooks": {"reporting_spreadsheet_id": "1AbCdEfGhIjKlMnOpQrStUvWxYz0123456789"}}
    path = tmp_path / "runner.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return SimpleNamespace(path=path, data=data, tmp=tmp_path, local=local, creds=creds, env={"LOCALAPPDATA": str(local)})


def save(home):
    home.path.write_text(yaml.safe_dump(home.data), encoding="utf-8")


def doctor(home, *extra, probes=None, json_mode=True, capsys=None):
    argv = (["--json"] if json_mode else []) + ["doctor", "--config", str(home.path), *extra]
    code = cli.main(argv, environ=home.env, preflight_probes={**PROBES_OK, **(probes or {})}, clock=lambda: NOW)
    out = capsys.readouterr().out if capsys else ""
    return code, (json.loads(out) if json_mode and out else out)


def finding(report, section, component):
    return next(f for f in report["findings"] if f["section"] == section and f["component"] == component)


# --- Part A: logging activation ----------------------------------------------------------------------------------------------------

def log_file(home):
    return home.local / "JasperSEOReporting" / "logs" / "seo_report.log"


def log_lines(home):
    return [json.loads(l) for l in log_file(home).read_text(encoding="utf-8").splitlines() if l.strip()]


def test_logging_starts_once_per_command_writes_json_lines_and_detaches(home, logged):
    assert cli.main(["system-info", "--config", str(home.path)], environ=home.env, preflight_probes=PROBES_OK) == 0
    assert tagged_handlers() == [] and logging.getLogger("seo_report").propagate is True
    records = log_lines(home)
    assert [r["event"] for r in records] == ["command started", "command finished"]
    assert records[0]["fields"] == {"command": "system-info"} and records[1]["fields"] == {"command": "system-info", "exit_code": 0}
    assert all(r["logger"] == "seo_report.cli" and r["ts"].endswith("+00:00") for r in records)


def test_two_commands_never_duplicate_handlers_or_lines(home, logged):
    for _ in range(2):
        cli.main(["system-info", "--config", str(home.path)], environ=home.env, preflight_probes=PROBES_OK)
        assert tagged_handlers() == []
    assert [r["event"] for r in log_lines(home)] == ["command started", "command finished"] * 2


def test_help_version_and_usage_errors_never_touch_the_log_folder(home, logged, capsys):
    for argv in (["--help"], ["--version"], ["report", "run"], ["bogus"], []):
        cli.main(argv, environ=home.env)
    assert not (home.local / "JasperSEOReporting").exists()


def test_run_and_period_identifiers_reach_the_log_records(home, logged):
    cli.main(["report", "dry-run", "--client", "alpha-co", "--period", PERIOD, "--config", str(home.path)], environ=home.env)
    started = log_lines(home)[0]
    assert (started["client_key"], started["period_id"]) == ("alpha-co", PERIOD) and started["fields"]["command"] == "report dry-run"


def test_secrets_logged_during_a_command_are_sanitized(home, logged):
    def noisy(ctx):
        logging.getLogger("seo_report.test").warning("refresh failed token=ya29.SYNTHETICSECRETVALUE0123456789 for C:\\Users\\someone\\Alpha Co\\x.json",
                                                     extra={"fields": {"client_secret": "GOCSPX-SYNTHETICSECRET0123"}})
        return 0
    cli.main(["system-info", "--config", str(home.path)], environ=home.env, handlers={("system-info", None): noisy})
    text = log_file(home).read_text(encoding="utf-8")
    assert "ya29" not in text and "GOCSPX" not in text and "someone" not in text and "[REDACTED]" in text


def test_log_records_never_contain_credential_paths(home, logged):
    cli.main(["clients", "validate", "--config", str(home.path)], environ=home.env, client_factory=object())
    text = log_file(home).read_text(encoding="utf-8")
    assert "creds" not in text and "client.json" not in text and "login.json" not in text


def test_a_failing_log_file_never_interrupts_the_command(home, logged, monkeypatch):
    def fail_open(self):
        raise OSError(errno.ENOSPC, "No space left on device")
    monkeypatch.setattr(logging.FileHandler, "_open", fail_open)
    monkeypatch.setattr(runner.SafeRotatingFileHandler, "failures", 0)
    assert cli.main(["system-info", "--config", str(home.path)], environ=home.env, preflight_probes=PROBES_OK) == 0
    assert runner.SafeRotatingFileHandler.failures >= 1 and tagged_handlers() == []


def test_an_unavailable_log_folder_warns_once_continues_and_is_reported(home, logged, capsys):
    env = {"LOCALAPPDATA": str(ROOT / "inside-the-project")}  # refused: logs may not live in the project
    code = cli.main(["--json", "doctor", "--config", str(home.path), "--skip-browser-launch"], environ=env, preflight_probes=PROBES_OK, clock=lambda: NOW)
    captured = capsys.readouterr()
    assert "logging is unavailable (log_dir_inside_project)" in captured.err and "inside-the-project" not in captured.err
    report = json.loads(captured.out)
    assert finding(report, "logging", "activation")["status"] == "blocked" and code == Exit.PREFLIGHT_FAILED
    assert not (ROOT / "inside-the-project").exists() and tagged_handlers() == []


@pytest.mark.parametrize("interrupt,expected", [(KeyboardInterrupt(), 130)])
def test_ctrl_c_during_a_command_ends_cleanly_with_logging_detached(home, logged, interrupt, expected):
    def boom(ctx):
        raise interrupt
    assert cli.main(["system-info", "--config", str(home.path)], environ=home.env, handlers={("system-info", None): boom}) == expected
    assert tagged_handlers() == [] and log_lines(home)[-1]["fields"]["exit_code"] == 130


def test_system_exit_propagates_and_logging_is_still_detached(home, logged):
    def stop(ctx):
        raise SystemExit(9)
    with pytest.raises(SystemExit):
        cli.main(["system-info", "--config", str(home.path)], environ=home.env, handlers={("system-info", None): stop})
    assert tagged_handlers() == []


# --- Part B: preflight checks (unit level) ------------------------------------------------------------------------------------------

def inputs(settings=None, **kw):
    return pf.Inputs(settings=settings, environ=kw.pop("environ", {}), app_version="8.6.0", **kw)


def settings_of(home):
    from seo_report import runner_config as rc
    return rc.load_settings(home.path, environ=home.env)


def status(findings, component):
    return next(f for f in findings if f.component == component)


def test_runtime_supported_and_unsupported_versions():
    ok = pf.check_runtime(inputs(probes={"python_version": (3, 14, 7), "machine": "AMD64", "system": "Windows", "release": "11"}))
    assert [f.status for f in ok] == ["ready"] * 4
    old = pf.check_runtime(inputs(probes={"python_version": (3, 13, 1)}))
    assert status(old, "python").status == "unverified" and status(old, "python").code == "python_version_untested"
    assert status(pf.check_runtime(inputs(probes={"machine": "ARM64"})), "architecture").status == "unverified"
    assert status(pf.check_runtime(inputs(probes={"system": "Windows", "release": "10"})), "operating_system").status == "unverified"
    other = status(pf.check_runtime(inputs(probes={"system": "Linux", "release": "6"})), "operating_system")
    assert (other.status, other.code) == ("blocked", "os_unsupported")


def test_the_real_environment_matches_the_pinned_dependencies():
    findings = pf.check_dependencies(inputs())
    assert [f.code for f in findings] == ["dependencies_match_pins"]


def test_dependency_problems_are_classified():
    pins = {"alpha": "1.0", "beta": "2.0", "gamma": "3.0"}
    versions = {"alpha": "1.0", "beta": "9.9"}
    findings = pf.check_dependencies(inputs(probes={"pins": pins, "installed_version": versions.get}))
    codes = {f.code: f for f in findings}
    assert codes["dependency_missing"].status == "blocked" and "gamma" in codes["dependency_missing"].message
    assert codes["dependency_version_differs"].status == "warning" and "beta" in codes["dependency_version_differs"].message
    assert pf.check_dependencies(inputs(probes={"requirements_path": Path("nowhere.txt")}))[0].status == "unverified"


def test_the_preflight_pin_reader_agrees_with_the_dependency_report_script():
    sys.path.insert(0, str(ROOT / "scripts"))
    import dependency_report as dr
    assert pf.read_pins(ROOT / "requirements.txt") == dr.read_pins(ROOT / "requirements.txt")


def test_browser_available_missing_and_failing():
    ok = pf.check_browser(inputs(probes={**PROBES_OK, "playwright_available": lambda: True, "playwright_version": lambda: "1.64.1"}))
    assert [f.code for f in ok] == ["playwright_installed", "chromium_present", "chromium_launches"]
    nopw = pf.check_browser(inputs(probes={"playwright_available": lambda: False}))
    assert (nopw[0].status, nopw[0].code) == ("blocked", "playwright_missing")
    nochromium = pf.check_browser(inputs(probes={"playwright_available": lambda: True, "playwright_version": lambda: "1", "chromium_folders": lambda e: []}))
    assert nochromium[-1].code == "chromium_missing" and "playwright install chromium" in nochromium[-1].action
    failed = pf.check_browser(inputs(probes={**PROBES_OK, "playwright_available": lambda: True, "playwright_version": lambda: "1",
                                              "launch_probe": lambda e: {"launched": False, "error": "launch_failed"}}))
    assert failed[-1].code == "chromium_launch_failed" and failed[-1].status == "blocked"
    slow = pf.check_browser(inputs(probes={**PROBES_OK, "playwright_available": lambda: True, "playwright_version": lambda: "1",
                                            "launch_probe": lambda e: {"launched": False, "error": "timeout"}}))
    assert slow[-1].code == "chromium_launch_timeout"
    skipped = pf.check_browser(inputs(launch_browser=False, probes={**PROBES_OK, "playwright_available": lambda: True, "playwright_version": lambda: "1"}))
    assert (skipped[-1].status, skipped[-1].code) == ("unverified", "chromium_launch_not_tested")


def test_chromium_detection_reads_only_the_playwright_folder(tmp_path):
    assert pf.chromium_folders({"LOCALAPPDATA": str(tmp_path)}) == []
    (tmp_path / "ms-playwright" / "chromium-1234").mkdir(parents=True)
    assert pf.chromium_folders({"LOCALAPPDATA": str(tmp_path)}) == ["chromium-1234"]


def test_the_real_browser_probe_starts_and_stops_chromium_and_cleans_its_temporary_folder(tmp_path, monkeypatch):
    if not pf.chromium_folders(os.environ):
        pytest.skip("Chromium is not installed")
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    result = pf.launch_probe(dict(os.environ))
    assert result.get("launched") is True and list(tmp_path.iterdir()) == []


def test_a_launch_probe_that_cannot_run_is_reported_not_raised(monkeypatch):
    import subprocess

    def timeout(*a, **k):
        raise subprocess.TimeoutExpired("x", 1)
    monkeypatch.setattr(subprocess, "run", timeout)
    assert pf.launch_probe({})["error"] == "timeout"
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(OSError("no python")))
    assert pf.launch_probe({})["error"] == "probe_not_started"


def test_assets_present_with_agreeing_fingerprints(home):
    findings = pf.check_assets(inputs(settings_of(home)))
    assert {f.component: f.status for f in findings} == {"logo": "ready", "DejaVuSans": "ready", "DejaVuSans-Bold": "ready", "fingerprints": "ready"}
    assert status(findings, "fingerprints").code == "fingerprints_agree"


def test_a_missing_logo_is_blocked_with_a_clear_code(home):
    base = settings_of(home)
    findings = pf.check_assets(inputs(dataclasses.replace(base, logo_path=home.tmp / "nologo.png")))
    assert (status(findings, "logo").status, status(findings, "logo").code) == ("blocked", "asset_file_not_found")


def test_a_missing_bold_font_is_blocked(home):
    base = settings_of(home)
    findings = pf.check_assets(inputs(dataclasses.replace(base, fonts=base.fonts[:1])))
    assert any(f.code == "required_font_missing" and f.status == "blocked" and f.component == "DejaVuSans-Bold" for f in findings)


def test_a_font_that_is_not_a_file_or_not_readable_is_blocked(home, monkeypatch):
    base = settings_of(home)
    folder = home.tmp / "fontdir"
    folder.mkdir()
    bad = (dataclasses.replace(base.fonts[0], path=folder), base.fonts[1])
    assert status(pf.check_assets(inputs(dataclasses.replace(base, fonts=bad))), "DejaVuSans").code == "asset_not_a_regular_file"
    original = af.sha256_of_file
    monkeypatch.setattr(af, "sha256_of_file", lambda path, role="file": (_ for _ in ()).throw(af.AssetFingerprintError("file_unreadable", role))
                        if role == "DejaVuSans-Bold" else original(path, role=role))
    assert status(pf.check_assets(inputs(base)), "DejaVuSans-Bold").code == "asset_file_unreadable"


def test_a_different_font_file_is_a_visible_warning_because_it_changes_report_identity(home):
    base = settings_of(home)
    copy = home.tmp / "DejaVuSans.ttf"
    shutil.copyfile(FONT_FILES["DejaVuSans"], copy)
    with open(copy, "ab") as handle:
        handle.write(b"x")
    findings = pf.check_assets(inputs(dataclasses.replace(base, fonts=(dataclasses.replace(base.fonts[0], path=copy), base.fonts[1]))))
    assert (status(findings, "DejaVuSans").status, status(findings, "DejaVuSans").code) == ("warning", "asset_differs_from_tested")
    assert pf.overall_readiness(findings) == "warning"


def test_fingerprints_that_disagree_with_the_engine_block(home, monkeypatch):
    from seo_report import render_identity as idn
    real = idn.measure_render_fingerprints

    def wrong(**kw):
        measured = real(**kw)
        return dataclasses.replace(measured, logo_sha256="0" * 64)
    monkeypatch.setattr(idn, "measure_render_fingerprints", wrong)
    assert status(pf.check_assets(inputs(settings_of(home))), "fingerprints").code == "fingerprints_disagree"


def test_the_reference_hashes_describe_the_files_shipped_with_the_tests():
    assert af.sha256_of_file(LOGO) == pf.REFERENCE_HASHES["logo"]
    assert af.sha256_of_file(FONT_FILES["DejaVuSans"]) == pf.REFERENCE_HASHES["DejaVuSans"]
    assert af.sha256_of_file(FONT_FILES["DejaVuSans-Bold"]) == pf.REFERENCE_HASHES["DejaVuSans-Bold"]


def tree(folder):
    return sorted(str(p.relative_to(folder)) for p in Path(folder).rglob("*"))


def test_valid_storage_is_ready_and_nothing_is_created(home):
    before = tree(home.tmp)
    findings = pf.check_storage(inputs(settings_of(home)))
    assert {f.status for f in findings} == {"ready"} and tree(home.tmp) == before


def test_missing_directories_are_warnings_not_blockers_and_are_not_created(home):
    shutil.rmtree(home.local / "work")
    before = tree(home.tmp)
    findings = pf.check_storage(inputs(settings_of(home)))
    assert (status(findings, "work_root").status, status(findings, "work_root").code) == ("warning", "directory_will_be_created")
    assert tree(home.tmp) == before


def test_a_file_where_a_folder_is_expected_and_permission_problems_block(home):
    settings = settings_of(home)
    shutil.rmtree(home.local / "output")
    (home.local / "output").write_text("x", encoding="utf-8")
    assert status(pf.check_storage(inputs(settings)), "output_root").code == "not_a_directory"
    (home.local / "output").unlink()
    (home.local / "output").mkdir()
    denied = pf.check_storage(inputs(settings, probes={"access": lambda p, m: False}))
    assert status(denied, "work_root").code == "directory_not_writable"
    shutil.rmtree(home.local / "work")
    assert status(pf.check_storage(inputs(settings, probes={"access": lambda p, m: False})), "work_root").code == "parent_not_writable"


def test_low_disk_space_is_a_warning(home):
    low = pf.check_storage(inputs(settings_of(home), probes={"disk_usage": lambda p: SimpleNamespace(free=10 * 2**20)}))
    assert status(low, "disk_space").code == "low_disk_space" and status(low, "disk_space").status == "warning"


def test_overlapping_or_invalid_storage_paths_are_a_configuration_error(home, capsys):
    home.data["paths"]["ledger_db"] = str(home.local / "output" / "l.sqlite")
    save(home)
    code, report = doctor(home, "--skip-browser-launch", capsys=capsys)
    assert code == Exit.CONFIG_INVALID and finding(report, "configuration", "settings")["code"] == "ledger_overlaps_output"
    home.data["paths"]["ledger_db"] = "relative/l.sqlite"
    save(home)
    code, report = doctor(home, "--skip-browser-launch", capsys=capsys)
    assert code == Exit.CONFIG_INVALID and finding(report, "configuration", "settings")["code"] == "path_not_absolute"


def test_authentication_configuration_is_checked_by_metadata_only(home, monkeypatch):
    monkeypatch.setattr("builtins.open", lambda *a, **k: (_ for _ in ()).throw(AssertionError("a file was opened")))
    monkeypatch.setattr(Credentials, "from_authorized_user_file", classmethod(lambda cls, *a, **k: (_ for _ in ()).throw(AssertionError("read"))))
    findings = pf.check_authentication(inputs(settings_of(home)))
    by = {f.component: f for f in findings}
    assert by["oauth_client_file"].status == "ready" and by["cached_sign_in"].status == "ready"
    assert by["live_verification"].status == "unverified" and by["live_verification"].code == "live_google_not_verified"


def test_missing_client_file_is_a_warning_and_missing_sign_in_is_blocked_as_auth_required(home):
    (home.creds / "client.json").unlink()
    (home.creds / "login.json").unlink()
    by = {f.component: f for f in pf.check_authentication(inputs(settings_of(home)))}
    assert (by["oauth_client_file"].status, by["oauth_client_file"].code) == ("warning", "oauth_client_file_missing")
    assert (by["cached_sign_in"].status, by["cached_sign_in"].code) == ("blocked", "auth_token_missing")


def test_unusable_credential_locations_block(home):
    (home.creds / "login.json").unlink()
    (home.creds / "login.json").mkdir()
    by = {f.component: f for f in pf.check_authentication(inputs(settings_of(home)))}
    assert by["cached_sign_in"].code == "auth_credentials_unreadable" and by["cached_sign_in"].status == "blocked"


def test_credentials_inside_the_project_are_a_visible_warning(home):
    del home.data["paths"]["oauth_client_file"], home.data["paths"]["oauth_cache_file"]
    save(home)
    findings = pf.check_authentication(inputs(settings_of(home)))
    assert status(findings, "credential_location").code == "credentials_inside_project"


def test_the_cached_sign_in_not_being_writable_is_a_warning(home):
    findings = pf.check_authentication(inputs(settings_of(home), probes={"access": lambda p, m: m != os.W_OK}))
    assert status(findings, "cached_sign_in").code == "cached_sign_in_not_writable"


def test_client_configuration_checks(home):
    by = {f.component: f for f in pf.check_configuration(inputs(settings_of(home)))}
    assert by["client_registry"].status == "ready" and by["sender"].status == "ready" and by["reporting_workbook"].status == "ready"
    assert by["recipients"].code == "recipients_separate" and by["workbook_rows"].status == "unverified"
    (home.tmp / "clients.yaml").write_text("clients: []\n", encoding="utf-8")
    (home.tmp / "jasper.yaml").write_text("sender: {}\n", encoding="utf-8")
    by = {f.component: f for f in pf.check_configuration(inputs(settings_of(home)))}
    assert by["client_registry"].code == "client_registry_invalid" and by["sender"].code == "sender_config_invalid"
    missing = pf.check_configuration(inputs(None, config_code="config_not_given"))
    assert (missing[0].status, missing[0].code) == ("blocked", "config_not_given")


# --- readiness, exit-code priority, report shape -----------------------------------------------------------------------------------

def fx(section, component, status_, code):
    return pf.Finding(section, component, status_, code, "m", "")


def test_overall_readiness_is_derived_and_unverified_never_hides_or_lowers_it():
    assert pf.overall_readiness([fx("runtime", "a", "ready", "x"), fx("runtime", "b", "unverified", "y")]) == "ready"
    assert pf.overall_readiness([fx("runtime", "a", "ready", "x"), fx("runtime", "b", "warning", "y")]) == "warning"
    assert pf.overall_readiness([fx("runtime", "a", "warning", "x"), fx("runtime", "b", "blocked", "y")]) == "blocked"


@pytest.mark.parametrize("findings,expected", [
    ([fx("runtime", "a", "ready", "x")], Exit.OK),
    ([fx("runtime", "a", "warning", "x"), fx("runtime", "b", "unverified", "y")], Exit.OK),
    ([fx("authentication", "t", "blocked", "auth_token_missing")], Exit.AUTH_REQUIRED),
    ([fx("authentication", "t", "blocked", "auth_token_missing"), fx("assets", "logo", "blocked", "asset_file_not_found")], Exit.PREFLIGHT_FAILED),
    ([fx("browser", "c", "blocked", "chromium_missing")], Exit.PREFLIGHT_FAILED),
    ([fx("configuration", "settings", "blocked", "path_not_absolute"), fx("browser", "c", "blocked", "chromium_missing")], Exit.CONFIG_INVALID),
    ([fx("configuration", "sender", "blocked", "sender_config_invalid"), fx("authentication", "t", "blocked", "auth_token_missing")], Exit.CONFIG_INVALID),
    ([fx("runtime", "check", "blocked", "preflight_internal_error"), fx("configuration", "settings", "blocked", "config_not_given")], Exit.INTERNAL_ERROR)])
def test_exit_code_priority(findings, expected):
    assert pf.exit_code(findings) == expected


def test_a_check_that_raises_becomes_an_internal_error_finding(monkeypatch):
    def boom(inp):
        raise RuntimeError("secret detail")
    monkeypatch.setattr(pf, "CHECKS", (("runtime", boom),))
    findings = pf.run_preflight(inputs())
    assert findings[0].code == "preflight_internal_error" and "secret" not in findings[0].message and pf.exit_code(findings) == Exit.INTERNAL_ERROR


# --- Part C/D: the commands ------------------------------------------------------------------------------------------------------------

CODE = re.compile(r"[a-z0-9_]{1,64}")
STAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\+00:00")


def check_schema(report):
    assert set(report) == {"schema", "version", "generated_at", "application", "overall_readiness", "production_ready", "outstanding", "counts",
                           "exit_code", "findings"}
    assert report["schema"] == "jasper.preflight-report" and report["version"] == 1 and STAMP.fullmatch(report["generated_at"])
    assert report["application"] == {"name": "seo-report", "version": cli.APP_VERSION}
    assert report["overall_readiness"] in ("ready", "warning", "blocked") and report["production_ready"] is False
    assert report["outstanding"] and set(report["counts"]) == set(pf.STATUSES) and sum(report["counts"].values()) == len(report["findings"])
    for f in report["findings"]:
        assert set(f) == {"section", "component", "status", "code", "message", "action"}
        assert f["section"] in pf.SECTIONS and f["status"] in pf.STATUSES and CODE.fullmatch(f["code"])
        assert isinstance(f["message"], str) and isinstance(f["action"], str)


def test_doctor_json_is_one_valid_object_with_the_documented_schema(home, capsys):
    code, report = doctor(home, capsys=capsys)
    check_schema(report)
    assert report["exit_code"] == int(code) and report["generated_at"] == "2026-11-01T12:00:00+00:00"


def test_a_healthy_installation_is_locally_ready_with_warnings_only_and_never_production_ready(home, capsys):
    code, report = doctor(home, capsys=capsys)
    assert code == Exit.AUTH_REQUIRED or code == Exit.OK  # decided by the sign-in state below
    assert report["production_ready"] is False and "live_google_not_verified" in report["outstanding"]
    assert not [f for f in report["findings"] if f["status"] == "blocked"], [f for f in report["findings"] if f["status"] == "blocked"]
    assert code == Exit.OK and report["overall_readiness"] in ("ready", "warning")
    assert finding(report, "authentication", "live_verification")["status"] == "unverified"


def test_doctor_exit_codes_for_the_main_failure_classes(home, capsys):
    (home.creds / "login.json").unlink()
    code, report = doctor(home, capsys=capsys)
    assert code == Exit.AUTH_REQUIRED and report["overall_readiness"] == "blocked"
    code, report = doctor(home, probes={"chromium_folders": lambda e: []}, capsys=capsys)
    assert code == Exit.PREFLIGHT_FAILED and finding(report, "browser", "chromium")["code"] == "chromium_missing"
    home.data["workbooks"] = {}
    save(home)
    code, report = doctor(home, capsys=capsys)
    assert code == Exit.CONFIG_INVALID and finding(report, "configuration", "reporting_workbook")["code"] == "reporting_workbook_not_configured"


def test_doctor_without_any_settings_file_reports_it_and_still_checks_the_runtime(capsys, tmp_path):
    code = cli.main(["--json", "doctor", "--skip-browser-launch"], environ={"LOCALAPPDATA": str(tmp_path)}, preflight_probes=PROBES_OK, clock=lambda: NOW)
    report = json.loads(capsys.readouterr().out)
    check_schema(report)
    assert code == Exit.CONFIG_INVALID and finding(report, "configuration", "settings")["code"] == "config_not_given"
    assert finding(report, "runtime", "python")["status"] == "ready" and finding(report, "assets", "logo_and_fonts")["status"] == "unverified"


def test_a_missing_settings_file_gets_its_own_code(capsys, tmp_path):
    cli.main(["--json", "doctor", "--config", str(tmp_path / "nope.yaml"), "--skip-browser-launch"], environ={"LOCALAPPDATA": str(tmp_path)},
             preflight_probes=PROBES_OK)
    assert finding(json.loads(capsys.readouterr().out), "configuration", "settings")["code"] == "config_not_found"


def test_doctor_text_has_the_nine_sections_and_each_findings_fields(home, capsys):
    code, text = doctor(home, json_mode=False, capsys=capsys)
    for number, title in enumerate(list(pf.SECTION_TITLES.values()) + ["Overall readiness"], start=1):
        assert f"{number}. {title}" in text
    assert re.search(r"\[(ready|warning|blocked|unverified)\s*\] \w+: .+ \([a-z0-9_]+\)", text)
    assert "Production-ready: NO" in text and f"Exit code: {int(code)}" in text


def test_doctor_output_has_no_credential_values_tokens_or_unnecessary_paths(home, capsys):
    (home.creds / "login.json").write_text(json.dumps({"token": "SYNTHETIC-ACCESS-TOKEN", "client_secret": "SYNTHETIC-CLIENT-SECRET"}), encoding="utf-8")
    code, report = doctor(home, capsys=capsys)
    text = json.dumps(report)
    assert "SYNTHETIC" not in text and str(home.tmp) not in text and "creds" not in text and "Users" not in text


def test_doctor_is_read_only_no_google_no_sign_in_no_production_writes(home, capsys, monkeypatch):
    monkeypatch.setattr(Credentials, "refresh", lambda *a, **k: (_ for _ in ()).throw(AssertionError("refresh")))
    monkeypatch.setattr(auth, "get_credentials", lambda **k: (_ for _ in ()).throw(AssertionError("credentials requested")))
    monkeypatch.setattr(auth, "run_setup", lambda **k: (_ for _ in ()).throw(AssertionError("setup")))
    before = tree(home.tmp)
    doctor(home, capsys=capsys)
    assert tree(home.tmp) == before  # no folder, database, report or log was created


def test_skip_browser_launch_makes_the_launch_check_unverified(home, capsys):
    code, report = doctor(home, "--skip-browser-launch", capsys=capsys)
    assert finding(report, "browser", "chromium_launch")["status"] == "unverified"


def test_system_info_shows_the_complete_picture_redacted(home, capsys):
    code = cli.main(["--json", "system-info", "--config", str(home.path)], environ=home.env, preflight_probes=PROBES_OK)
    data = json.loads(capsys.readouterr().out)
    assert code == 0 and data["ok"] is True
    for key in ("application", "version", "python", "operating_system", "architecture", "config", "storage", "ledger", "dependencies", "playwright",
                "assets", "oauth_files", "logging", "runner_readiness", "live_google_verified"):
        assert key in data, key
    assert data["live_google_verified"] is False and data["runner_readiness"]["production_ready"] is False
    assert data["oauth_files"] == {"configuration": "settings", "client_file": "present", "cached_sign_in": "present", "outside_project": True}
    assert data["storage"]["output_root"].startswith("%LOCALAPPDATA%\\") and str(home.tmp) not in json.dumps(data)
    assert data["ledger"] == {"database": "not_created_yet"} and data["logging"]["format"] == "json-lines"
    assert data["assets"]["logo"]["status"] == "ready" and data["playwright"]["chromium"] == "chromium_present"


def test_system_info_text_and_its_guarantees(home, capsys, monkeypatch):
    monkeypatch.setattr(pf, "launch_probe", lambda env: (_ for _ in ()).throw(AssertionError("a browser was started")))
    before = tree(home.tmp)
    assert cli.main(["system-info", "--config", str(home.path)], environ=home.env, preflight_probes={"chromium_folders": lambda e: ["chromium-1"]}) == 0
    out = capsys.readouterr().out
    for phrase in ("Python 3.", "Storage:", "Dependencies:", "Playwright:", "Assets:", "Credentials:", "Logging:", "Runner readiness", "production-ready: NO",
                   "Live Google integration verified: NO"):
        assert phrase in out, phrase
    assert tree(home.tmp) == before


def test_system_info_still_needs_a_valid_configuration(capsys):
    assert cli.main(["system-info"], environ={}) == Exit.CONFIG_INVALID


def test_help_and_version_stay_lightweight():
    code = ("import sys, seo_report.cli as c; c.main(['--version']); "
            "print(sorted(m for m in sys.modules if m in ('seo_report.preflight','seo_report.runner','seo_report.pipeline') or m.startswith(('playwright','googleapiclient'))))")
    import subprocess
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env={**os.environ, "PYTHONPATH": str(ROOT / "src"),
                                                                                         "LOCALAPPDATA": tempfile.mkdtemp(prefix="seo-help-")})
    assert out.returncode == 0 and out.stdout.strip().endswith("[]"), out.stdout + out.stderr


def test_the_preflight_module_has_no_network_credential_or_deletion_code():
    source = (ROOT / "src" / "seo_report" / "preflight.py").read_text(encoding="utf-8")
    code = source.split('"""', 2)[2]
    for word in ("requests", "urllib", "googleapiclient", "run_local_server", "get_credentials", "from_authorized_user_file", ".refresh(", "unlink",
                 "os.remove", "makedirs", "os.mkdir", ".mkdir("):
        assert word not in code, word
    assert code.count("rmtree") == 1  # only the temporary folder that launch_probe itself created

def test_the_local_gate_allows_only_a_preflight_without_blockers(home):  # the local half of the R8.6d production gate
    allowed, code, blocking = pf.local_gate(inputs(settings_of(home), probes=PROBES_OK, logging_status={"active": True, "code": None}))
    assert allowed and code == Exit.OK and blocking == []
    (home.creds / "login.json").unlink()
    allowed, code, blocking = pf.local_gate(inputs(settings_of(home), probes=PROBES_OK, logging_status={"active": True, "code": None}))
    assert not allowed and code == Exit.AUTH_REQUIRED and [f.code for f in blocking] == ["auth_token_missing"]
    allowed, code, blocking = pf.local_gate(inputs(None, config_code="config_not_given", probes=PROBES_OK))
    assert not allowed and code == Exit.CONFIG_INVALID


def test_every_section_title_is_declared_and_the_nine_section_names_are_stable():
    assert list(pf.SECTION_TITLES) == list(pf.SECTIONS) and len(pf.SECTIONS) == 8
    assert list(pf.SECTION_TITLES.values()) == ["Application and Python runtime", "Dependencies", "Playwright and Chromium", "Fonts and logo",
                                               "Storage and filesystem", "Authentication configuration", "Client configuration", "Logging"]


# --- R8.7d.2: the "outstanding" list and the production gate use ONE definition of "credentials relocated" ---------------------------------

STILL_OUTSTANDING = ["make_not_connected", "drive_not_connected", "email_not_connected"]


def relocation_scenarios(home):
    """(label, settings or None, expected relocated) for each way the credential locations can be configured."""
    outside = settings_of(home)
    scenarios = [("relocated outside the project", outside, True)]
    fallback = json.loads(json.dumps(home.data))
    fallback["paths"] = {k: v for k, v in fallback["paths"].items() if not k.startswith("oauth_")}
    home.data = fallback
    save(home)
    scenarios.append(("fallback inside the project", settings_of(home), False))
    onedrive = json.loads(json.dumps(home.data))
    onedrive["paths"]["oauth_client_file"] = str(home.creds / "client.json")
    onedrive["paths"]["oauth_cache_file"] = str(home.tmp / "OneDrive" / "creds" / "login.json")
    home.data = onedrive
    save(home)
    scenarios.append(("cached token inside OneDrive", settings_of(home), False))
    scenarios.append(("invalid credential path", dataclasses.replace(outside, oauth_client_path="   "), False))
    scenarios.append(("missing settings", None, False))
    return scenarios


def evaluate(settings, home):
    inp = inputs(settings, environ=home.env, probes=PROBES_OK, logging_status={"active": True, "code": None}, launch_browser=False)
    findings = pf.run_preflight(inp)
    gate_requirements = pf.uncleared_requirements(inp, findings)
    outstanding = pf.build_report(findings, app_version="x", settings=settings)["outstanding"]
    return findings, gate_requirements, outstanding


def test_relocated_credentials_leave_the_outstanding_list_and_every_other_case_keeps_them(home):
    for label, settings, relocated in relocation_scenarios(home):
        findings, gate_requirements, outstanding = evaluate(settings, home)
        assert pf.credentials_relocated(settings, findings) is relocated, label
        assert ("credentials_not_relocated" not in outstanding) is relocated, label
        assert ("credentials_not_relocated" not in gate_requirements) is relocated, label
        assert outstanding == ["live_google_not_verified"] + ([] if relocated else ["credentials_not_relocated"]) + STILL_OUTSTANDING, label


def test_the_status_report_and_the_gate_agree_on_relocation_in_every_scenario(home):
    for label, settings, _ in relocation_scenarios(home):
        findings, gate_requirements, outstanding = evaluate(settings, home)
        assert ("credentials_not_relocated" in gate_requirements) == ("credentials_not_relocated" in outstanding), label
        assert ("credentials_not_relocated" in gate_requirements) == (not pf.credentials_relocated(settings, findings)), label


def test_without_a_valid_attestation_the_live_verification_stays_and_unrelated_entries_are_untouched(home):
    findings, gate_requirements, outstanding = evaluate(settings_of(home), home)
    assert outstanding == ["live_google_not_verified"] + STILL_OUTSTANDING and gate_requirements == ["live_google_not_verified"]
    assert pf.OUTSTANDING == ("live_google_not_verified", "credentials_not_relocated", "make_not_connected", "drive_not_connected", "email_not_connected")
    assert pf.GATE_REQUIREMENTS == ("live_google_not_verified", "credentials_not_relocated")


def test_missing_settings_never_assume_relocation(home):
    findings = pf.run_preflight(inputs(None, config_code="config_not_given", probes=PROBES_OK))
    assert pf.credentials_relocated(None, findings) is False
    assert pf.credentials_relocated(None, []) is False
    assert "credentials_not_relocated" in pf.outstanding_for(findings, None) and "credentials_not_relocated" in pf.outstanding_for(findings)
    assert pf.build_report(findings, app_version="x")["outstanding"] == list(pf.OUTSTANDING)  # the old call form keeps the old, conservative result


def test_doctor_and_system_info_expose_the_same_outstanding_list_and_production_stays_not_ready(home, capsys):
    code, report = doctor(home, "--skip-browser-launch", capsys=capsys)
    assert code == 0 and report["production_ready"] is False
    assert report["outstanding"] == ["live_google_not_verified"] + STILL_OUTSTANDING
    cli.main(["--json", "system-info", "--config", str(home.path)], environ=home.env, preflight_probes=PROBES_OK, clock=lambda: NOW)
    info = json.loads(capsys.readouterr().out)
    assert info["runner_readiness"]["outstanding"] == report["outstanding"] and info["runner_readiness"]["production_ready"] is False
    home.data["paths"].pop("oauth_client_file")
    home.data["paths"].pop("oauth_cache_file")
    save(home)
    code, report = doctor(home, "--skip-browser-launch", capsys=capsys)
    assert report["outstanding"] == list(pf.OUTSTANDING)
    cli.main(["--json", "system-info", "--config", str(home.path)], environ=home.env, preflight_probes=PROBES_OK, clock=lambda: NOW)
    assert json.loads(capsys.readouterr().out)["runner_readiness"]["outstanding"] == list(pf.OUTSTANDING)
    assert "credentials_inside_project" in {f["code"] for f in report["findings"]}


# --- R8.7d.3: BOTH OAuth locations are checked independently -------------------------------------------------------------------------------

PROJECT_DIR = Path(__file__).resolve().parents[1] / "credentials_matrix_unused"  # inside the project; nothing is ever created there
KINDS = ("safe", "project", "onedrive", "missing", "invalid")


def matrix_settings(home, client_kind, token_kind):
    """Synthetic settings whose two OAuth locations are of the given kinds. Nothing is created; the credential files are never opened."""
    from seo_report import runner_config as rc

    def location(kind, name):
        return {"safe": home.creds / name, "project": PROJECT_DIR / name, "onedrive": home.tmp / "OneDrive" / "creds" / name}.get(kind)
    data = json.loads(json.dumps(home.data))
    for key, kind, name in (("oauth_client_file", client_kind, "client.json"), ("oauth_cache_file", token_kind, "login.json")):
        place = location(kind, name)
        if place is None:
            data["paths"].pop(key, None)  # "missing" and "invalid" start from an unconfigured location
        else:
            data["paths"][key] = str(place)
    home.path.write_text(yaml.safe_dump(data), encoding="utf-8")
    settings = rc.load_settings(home.path, environ=home.env)
    changes = {}
    if client_kind == "invalid":
        changes.update(oauth_client_path="   ")
    if token_kind == "invalid":
        changes.update(oauth_cache_path="   ")
    return dataclasses.replace(settings, **changes) if changes else settings


MATRIX = [  # (client, token, relocated): the twelve scenarios of the R8.7d.3 brief, "missing" = the location is not configured
    ("safe", "safe", True), ("project", "safe", False), ("safe", "project", False), ("onedrive", "safe", False), ("safe", "onedrive", False),
    ("project", "onedrive", False), ("onedrive", "project", False), ("missing", "safe", False), ("safe", "missing", False),
    ("invalid", "safe", False), ("safe", "invalid", False)]


@pytest.mark.parametrize("client_kind,token_kind,relocated", MATRIX)
def test_each_oauth_location_is_validated_independently(home, client_kind, token_kind, relocated):
    settings = matrix_settings(home, client_kind, token_kind)
    findings, gate_requirements, outstanding = evaluate(settings, home)
    assert pf.credentials_relocated(settings, findings) is relocated, (client_kind, token_kind)
    assert ("credentials_not_relocated" in gate_requirements) is (not relocated)  # the gate agrees with the helper
    assert ("credentials_not_relocated" in outstanding) is (not relocated)  # and so does the status report
    others = [code for code in outstanding if code != "credentials_not_relocated"]
    assert others == ["live_google_not_verified"] + STILL_OUTSTANDING  # nothing else changes
    assert pf.production_gate(inputs(settings, environ=home.env, probes=PROBES_OK, logging_status={"active": True, "code": None}))[0] is False


def test_missing_settings_are_not_relocated_for_the_helper_the_gate_and_the_report(home):
    findings, gate_requirements, outstanding = evaluate(None, home)
    assert pf.credentials_relocated(None, findings) is False
    assert "credentials_not_relocated" in gate_requirements and "credentials_not_relocated" in outstanding


def test_a_cloud_synchronised_client_file_is_reported_with_a_specific_finding(home):
    findings, _, _ = evaluate(matrix_settings(home, "onedrive", "safe"), home)
    assert status(findings, "credential_location").code == "credentials_in_cloud_sync"
    findings, _, _ = evaluate(matrix_settings(home, "safe", "onedrive"), home)
    assert status(findings, "credential_location").code == "credentials_in_cloud_sync"
    findings, _, _ = evaluate(matrix_settings(home, "safe", "safe"), home)
    assert not [f for f in findings if f.code in ("credentials_in_cloud_sync", "credentials_inside_project")]


def test_doctor_and_system_info_agree_for_each_independent_failure(home, capsys):
    for client_kind, token_kind, relocated in (("safe", "safe", True), ("onedrive", "safe", False), ("safe", "onedrive", False), ("project", "safe", False)):
        matrix_settings(home, client_kind, token_kind)
        code, report = doctor(home, "--skip-browser-launch", capsys=capsys)
        cli.main(["--json", "system-info", "--config", str(home.path)], environ=home.env, preflight_probes=PROBES_OK, clock=lambda: NOW)
        info = json.loads(capsys.readouterr().out)
        assert info["runner_readiness"]["outstanding"] == report["outstanding"], (client_kind, token_kind)
        assert ("credentials_not_relocated" not in report["outstanding"]) is relocated, (client_kind, token_kind)
        assert report["production_ready"] is False and info["runner_readiness"]["production_ready"] is False
