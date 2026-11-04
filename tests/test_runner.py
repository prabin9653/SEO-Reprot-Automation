"""R8.3: the runner settings and the sequential multi-client runner (offline fakes, temporary folders)."""
import ast
import dataclasses
import errno
import json
import os
import sys
from pathlib import Path

import pytest
import yaml
from google.auth.exceptions import RefreshError

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from seo_report import contracts
from seo_report import ledger as ledger_module
from seo_report import pipeline, runner
from seo_report import runner_config as rc
from seo_report.sources import gsc as gsc_source
from test_pipeline import (Clock, FONT_FILES, LOGO, PERIOD_ID, default_collect, failing_collect, go, make_world, req,
                           world)  # noqa: F401

NAMES = ("Alpha Co", "Beta Co", "Gamma Co", "Delta Co", "Echo Co")
KEYS = ("alpha-co", "beta-co", "gamma-co", "delta-co", "echo-co")


def settings_data(tmp_path, **sections):
    local = tmp_path / "local"
    data = {"paths": {"output_root": str(local / "output"), "history_db": str(local / "history" / "h.sqlite"),
                      "ledger_db": str(local / "ledger" / "l.sqlite"), "work_root": str(local / "work")}}
    for name, value in sections.items():
        data.setdefault(name, {}).update(value)
    return data, {"LOCALAPPDATA": str(local)}


def requests_for(*keys, **kw):
    return [req(key, when=f"2026-11-02T00:5{i}:00+00:00", **kw) for i, key in enumerate(keys)]


def batch(w, *keys, **kw):
    return runner.run_batch(requests_for(*keys), services=w.services, ledger=w.ledger, **kw)


# --- settings ---------------------------------------------------------------------------------------------------------------------

def test_defaults_follow_the_existing_storage_layout(tmp_path):
    local = tmp_path / "local"
    settings = rc.parse_settings({}, environ={"LOCALAPPDATA": str(local)})
    base = local / "JasperSEOReporting"
    assert settings.output_root == base / "output"
    assert settings.ledger_db_path == base / "ledger" / "ledger.sqlite" and settings.work_root == base / "work"
    assert settings.history_db_path == Path(os.path.abspath(base / "history" / "seo_reports.sqlite"))
    assert settings.max_automatic_attempts == 3 and settings.retry_delays_seconds == (300, 900)
    assert settings.clients_config_path.name == "clients.yaml" and settings.logo_path.name == "jasper-logo.png"
    assert settings.fonts == () and settings.reporting_spreadsheet_id is None
    assert not (local / "JasperSEOReporting").exists()  # nothing is created


def test_an_explicit_configuration_is_validated_and_typed(tmp_path):
    data, environ = settings_data(tmp_path, render={"logo": str(LOGO), "fonts": [
        {"label": "DejaVuSans", "path": str(FONT_FILES["DejaVuSans"])},
        {"label": "DejaVuSans-Bold", "path": str(FONT_FILES["DejaVuSans-Bold"]), "weight": "700"}]},
        workbooks={"reporting_spreadsheet_id": "a" * 30, "operational_spreadsheet_id": "b" * 30},
        execution={"lock_ttl_seconds": 120, "stage_timeouts": {"rendering": 42}, "retry_delays_seconds": [60]})
    settings = rc.parse_settings(data, environ=environ)
    assert [(f.label, f.weight) for f in settings.fonts] == [("DejaVuSans", "400"), ("DejaVuSans-Bold", "700")]
    assert (settings.reporting_spreadsheet_id, settings.operational_spreadsheet_id) == ("a" * 30, "b" * 30)
    assert settings.lock_ttl_seconds == 120.0 and settings.stage_timeouts.rendering == 42.0
    assert settings.stage_timeouts.collecting == 600.0 and settings.retry_delays_seconds == (60,)
    with pytest.raises(dataclasses.FrozenInstanceError):
        settings.work_root = Path("x")


def error_of(data, environ):
    with pytest.raises(rc.RunnerConfigError) as info:
        rc.parse_settings(data, environ=environ)
    return info.value.code, info.value.field


@pytest.mark.parametrize("section,extra,code", [
    ("paths", {"surprise": 1}, "setting_unknown"), ("render", {"colour": "red"}, "setting_unknown"),
    ("execution", {"threads": 8}, "setting_unknown"), ("workbooks", {"other": "x"}, "setting_unknown")])
def test_unknown_settings_are_refused(tmp_path, section, extra, code):
    data, environ = settings_data(tmp_path, **{section: extra})
    assert error_of(data, environ)[0] == code
    data, environ = settings_data(tmp_path)
    data["unknown_section"] = {}
    assert error_of(data, environ) == ("setting_unknown", "unknown_section")


@pytest.mark.parametrize("section,key", [("paths", "client_secret_path"), ("workbooks", "refresh_token"),
                                         ("execution", "api_key"), ("render", "password"),
                                         ("workbooks", "oauth_credentials"), ("paths", "private_key")])
def test_credential_like_keys_are_refused_by_name_only(tmp_path, section, key):
    data, environ = settings_data(tmp_path, **{section: {key: "ya29.should-never-be-echoed"}})
    code, field = error_of(data, environ)
    assert code == "credential_in_config" and field == f"{section}.{key}"


@pytest.mark.parametrize("value", ["ya29.a0AfH6SMBx", "1//0gAbCdEfGhIjKl", "GOCSPX-abcdef", "Bearer abcdefgh",
                                   "-----BEGIN PRIVATE KEY-----"])
def test_token_like_values_are_refused_and_never_echoed(tmp_path, value):
    data, environ = settings_data(tmp_path, workbooks={"reporting_spreadsheet_id": value})
    with pytest.raises(rc.RunnerConfigError) as info:
        rc.parse_settings(data, environ=environ)
    assert info.value.code == "credential_in_config" and value not in str(info.value)


def test_paths_must_be_absolute_and_fully_expanded(tmp_path):
    data, environ = settings_data(tmp_path)
    for bad in ("relative/folder", "%JASPER_UNSET%\\ledger.sqlite", "", "   ", 5):  # None means "not set": the default
        data["paths"]["ledger_db"] = bad
        code, field = error_of(data, environ)
        assert field == "paths.ledger_db" and code in ("path_not_absolute", "path_invalid"), bad


def test_the_ledger_work_and_output_locations_must_not_overlap_or_sit_in_the_project(tmp_path):
    data, environ = settings_data(tmp_path)
    local = tmp_path / "local"
    cases = [({"ledger_db": str(local / "history" / "h.sqlite")}, "ledger_overlaps_history"),
             ({"ledger_db": str(local / "output" / "ledger.sqlite")}, "ledger_overlaps_output"),
             ({"work_root": str(local / "output" / "work")}, "work_root_overlaps"),
             ({"work_root": str(local / "ledger")}, "work_root_overlaps"),
             ({"ledger_db": str(ROOT / "ledger.sqlite")}, "path_inside_project"),
             ({"work_root": str(ROOT / "work")}, "path_inside_project"),
             ({"output_root": str(ROOT / "out")}, "output_root_invalid")]
    for change, code in cases:
        mutated = json.loads(json.dumps(data))
        mutated["paths"].update(change)
        assert error_of(mutated, environ)[0] == code, change


def test_onedrive_locations_are_refused(tmp_path):
    data, environ = settings_data(tmp_path)
    cloud = tmp_path / "OneDrive"
    environ = {**environ, "OneDrive": str(cloud)}
    for key, code in (("ledger_db", "path_in_cloud_sync"), ("work_root", "path_in_cloud_sync"),
                      ("history_db", "path_in_cloud_sync"), ("output_root", "output_root_invalid")):
        mutated = json.loads(json.dumps(data))
        mutated["paths"][key] = str(cloud / "x" / "item")
        assert error_of(mutated, environ)[0] == code, key


@pytest.mark.parametrize("section,key,value,code", [
    ("execution", "lock_ttl_seconds", 5, "number_out_of_range"), ("execution", "lock_ttl_seconds", True, "number_invalid"),
    ("execution", "lock_ttl_seconds", "600", "number_invalid"), ("execution", "max_automatic_attempts", 2.5, "number_invalid"),
    ("execution", "max_automatic_attempts", 0, "number_out_of_range"), ("execution", "client_timeout_seconds", 1e9, "number_out_of_range"),
    ("execution", "retry_delays_seconds", [0], "retry_delays_invalid"), ("execution", "retry_delays_seconds", "5", "retry_delays_invalid"),
    ("execution", "retry_delays_seconds", [True], "retry_delays_invalid"),
    ("execution", "stage_timeouts", {"unknown": 1}, "setting_unknown"),
    ("execution", "stage_timeouts", {"rendering": 0}, "number_out_of_range"),
    ("workbooks", "reporting_spreadsheet_id", "short", "spreadsheet_id_invalid"),
    ("workbooks", "reporting_spreadsheet_id", "REPLACE_WITH_REPORTING_SPREADSHEET_ID", "placeholder_value"),
    ("workbooks", "operational_spreadsheet_id", "has spaces " * 4, "spreadsheet_id_invalid"),
    ("render", "fonts", [], "fonts_invalid"), ("render", "fonts", "DejaVu", "fonts_invalid"),
    ("render", "fonts", [{"label": "bad label!", "path": "C:\\f.ttf"}], "font_label_invalid"),
    ("render", "fonts", [{"label": "A", "path": "C:\\f.ttf", "weight": "500"}], "font_weight_invalid"),
    ("render", "fonts", [{"label": "A", "path": "relative.ttf"}], "path_not_absolute"),
    ("render", "fonts", [{"label": "A", "path": "C:\\a.ttf"}, {"label": "A", "path": "C:\\b.ttf"}], "font_label_duplicate")])
def test_invalid_values_are_refused_with_a_fixed_code(tmp_path, section, key, value, code):
    data, environ = settings_data(tmp_path, **{section: {key: value}})
    assert error_of(data, environ)[0] == code


def test_files_are_read_with_the_safe_loader_and_size_limit(tmp_path):
    data, environ = settings_data(tmp_path)
    good = tmp_path / "runner.yaml"
    good.write_text(yaml.safe_dump(data), encoding="utf-8")
    assert rc.load_settings(good, environ=environ).ledger_db_path == tmp_path / "local" / "ledger" / "l.sqlite"
    for name, content, code in (("big.yaml", "#" + "x" * (rc.MAX_CONFIG_BYTES + 1), "file_too_large"),
                                ("bad.yaml", "a: [1, 2", "file_invalid"), ("tag.yaml", "a: !!python/object:os.system {}", "file_invalid")):
        (tmp_path / name).write_text(content, encoding="utf-8")
        with pytest.raises(rc.RunnerConfigError) as info:
            rc.load_settings(tmp_path / name, environ=environ)
        assert info.value.code == code
    with pytest.raises(rc.RunnerConfigError) as info:
        rc.load_settings(tmp_path / "missing.yaml", environ=environ)
    assert info.value.code == "file_unreadable"
    (tmp_path / "list.yaml").write_text("- 1\n- 2\n", encoding="utf-8")
    with pytest.raises(rc.RunnerConfigError):
        rc.load_settings(tmp_path / "list.yaml", environ=environ)


@pytest.mark.skipif(os.name != "nt", reason="the example uses Windows drive paths")
def test_the_example_file_is_safe_and_loads_once_its_placeholders_are_replaced(tmp_path):
    example = (ROOT / "config" / "runner.yaml.example").read_text(encoding="utf-8")
    environ = {"LOCALAPPDATA": str(tmp_path / "local")}
    with pytest.raises(rc.RunnerConfigError) as info:
        rc.parse_settings(yaml.safe_load(example), environ=environ)
    assert info.value.code == "placeholder_value"  # the placeholders can never be used by accident
    filled = example.replace("REPLACE_WITH_REPORTING_SPREADSHEET_ID", "r" * 30).replace(
        "REPLACE_WITH_OPERATIONAL_SPREADSHEET_ID", "o" * 30)
    settings = rc.parse_settings(yaml.safe_load(filled), environ=environ)
    assert [f.label for f in settings.fonts] == ["DejaVuSans", "DejaVuSans-Bold"] and settings.stage_timeouts.finalizing == 180.0
    for secret in ("ya29.", "client_secret", "refresh_token", "BEGIN PRIVATE"):
        assert secret not in example.replace("never put credentials, tokens or client secrets", "")


def test_configuration_code_never_reads_credentials():
    tree = ast.parse((ROOT / "src" / "seo_report" / "runner_config.py").read_text(encoding="utf-8"))
    imported = {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names} | {
        n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
    assert not any(name.startswith("seo_report.auth") or name.startswith("google") for name in imported)
    assert "token.json" not in (ROOT / "src" / "seo_report" / "runner_config.py").read_text(encoding="utf-8")


# --- the runner: isolation ----------------------------------------------------------------------------------------------------------

def test_five_clients_are_processed_one_after_another_and_summarized(world):
    slept = []
    w = world(names=NAMES, execution={"inter_client_pause_seconds": 2}, services_options={"sleep": slept.append})
    outcome = batch(w, *KEYS)
    assert [r.client_key for r in outcome.results] == list(KEYS) and not outcome.aborted and outcome.not_started == ()
    assert all(r.state == "audited" and r.outcome == "succeeded" for r in outcome.results)
    assert outcome.counts["total"] == 5 and outcome.counts["succeeded"] == 5 and outcome.counts["failed"] == 0
    assert slept == [2, 2, 2, 2]  # a pause between clients, none after the last
    summary = outcome.summary()
    assert summary["schema"] == "jasper.batch-summary" and len(summary["results"]) == 5
    assert w.ledger.get_lock("runner:global") is None
    assert [r.state for r in w.ledger.list_runs()] == ["audited"] * 5


def test_requests_are_processed_in_request_time_then_client_order(world):
    w = world(names=NAMES)
    shuffled = [req("echo-co", when="2026-11-02T00:50:00+00:00"), req("beta-co", when="2026-11-02T00:40:00+00:00"),
                req("alpha-co", when="2026-11-02T00:50:00+00:00")]
    outcome = runner.run_batch(shuffled, services=w.services, ledger=w.ledger)
    assert [r.client_key for r in outcome.results] == ["beta-co", "alpha-co", "echo-co"]


def test_one_clients_failures_never_stop_the_others(world):
    def collect(client_run, today):
        if client_run.client_key == "beta-co":
            return failing_collect(gsc_source.SearchConsoleError("x", 403))(client_run, today)
        if client_run.client_key == "gamma-co":
            raise ValueError("a bug in one client's path")
        return default_collect(client_run, today)
    w = world(names=NAMES, collect=collect)
    outcome = batch(w, *KEYS)
    by_client = {r.client_key: r for r in outcome.results}
    assert by_client["beta-co"].error["category"] == "source_denied" and by_client["gamma-co"].error["category"] == "internal_error"
    assert [k for k, r in by_client.items() if r.state == "audited"] == ["alpha-co", "delta-co", "echo-co"]
    assert (outcome.counts["succeeded"], outcome.counts["failed"]) == (3, 2) and not outcome.aborted
    assert {r.run_key: r.state for r in w.ledger.list_runs()} == {f"{k}/{PERIOD_ID}": by_client[k].state for k in KEYS}
    for key in ("alpha-co", "delta-co", "echo-co"):  # the good clients are complete and independent
        assert w.ledger.get_run(f"{key}/{PERIOD_ID}").record_name


def test_rejected_and_unknown_clients_do_not_stop_the_batch(world):
    w = world(names=("Alpha Co", "Beta Co"))
    items = [req("alpha-co", when="2026-11-02T00:50:00+00:00"), req("nobody", when="2026-11-02T00:51:00+00:00"),
             req("beta-co", when="2026-11-02T00:52:00+00:00")]
    outcome = runner.run_batch(items, services=w.services, ledger=w.ledger)
    assert [r.outcome for r in outcome.results] == ["succeeded", "rejected", "succeeded"] and not outcome.aborted
    assert outcome.counts["rejected"] == 1


def test_a_client_that_is_being_processed_elsewhere_is_skipped_and_the_rest_continue(world):
    w = world(names=("Alpha Co", "Beta Co"))
    other = ledger_module.Ledger(w.settings.ledger_db_path, clock=w.clock, pid_alive=lambda pid: True)
    try:
        other.acquire_lock("client:alpha-co", ttl_seconds=600)
        outcome = batch(w, "alpha-co", "beta-co")
        assert [(r.client_key, r.outcome) for r in outcome.results] == [("alpha-co", "rejected"), ("beta-co", "succeeded")]
        assert outcome.results[0].error["category"] == "run_in_progress" and not outcome.aborted
    finally:
        other.close()


def test_the_same_request_twice_in_one_batch_is_replayed(world):
    w = world()
    request = req()
    outcome = runner.run_batch([request, request], services=w.services, ledger=w.ledger)
    assert [r.replayed for r in outcome.results] == [False, True] and outcome.counts["replayed"] == 1
    assert len(w.ledger.list_runs()) == 1


# --- the runner: batch-level stops ---------------------------------------------------------------------------------------------------

def test_expired_credentials_stop_the_batch_and_leave_the_rest_unstarted(world):
    def collect(client_run, today):
        if client_run.client_key == "gamma-co":
            return failing_collect(RefreshError("revoked"))(client_run, today)
        return default_collect(client_run, today)
    w = world(names=NAMES, collect=collect)
    items = requests_for(*KEYS)
    outcome = runner.run_batch(items, services=w.services, ledger=w.ledger)
    assert outcome.aborted and outcome.abort_reason == "auth_required"
    assert [r.client_key for r in outcome.results] == ["alpha-co", "beta-co", "gamma-co"]
    assert outcome.results[2].error["category"] == "auth_required"
    assert outcome.not_started == (items[3].request_id, items[4].request_id)
    assert {r.client_key for r in w.ledger.list_runs()} == {"alpha-co", "beta-co", "gamma-co"}  # nothing created for the rest
    assert w.ledger.get_request(items[3].request_id) is None
    assert outcome.summary()["abort_reason"] == "auth_required" and w.ledger.get_lock("runner:global") is None


def test_a_full_disk_stops_the_batch(world):
    def renderer(render_input, *, workdir, logo_path, fonts):
        raise OSError(errno.ENOSPC, "No space left on device")
    w = world(names=NAMES, renderer=renderer)
    outcome = batch(w, *KEYS)
    assert outcome.aborted and outcome.abort_reason == "disk_full" and len(outcome.results) == 1
    assert outcome.results[0].error["code"] == "disk_full" and len(outcome.not_started) == 4


def test_an_unusable_ledger_stops_the_batch(world, monkeypatch):
    w = world(names=NAMES)

    def broken(self, request):
        raise ledger_module.LedgerError("ledger_unavailable")
    monkeypatch.setattr(ledger_module.Ledger, "register_request", broken)
    outcome = batch(w, *KEYS)
    assert outcome.aborted and outcome.abort_reason == "ledger_unavailable" and outcome.results == ()
    assert len(outcome.not_started) == 5 and w.ledger.get_lock("runner:global") is None


def test_the_batch_deadline_stops_the_batch_between_clients(world):
    clock = Clock()

    def collect(client_run, today):
        clock.advance(hours=2)  # every client takes two hours
        return default_collect(client_run, today)
    w = world(names=NAMES, collect=collect, clock=clock, execution={"batch_deadline_seconds": 3600})
    outcome = batch(w, *KEYS)
    assert outcome.aborted and outcome.abort_reason == "batch_deadline"
    assert [r.client_key for r in outcome.results] == ["alpha-co"] and len(outcome.not_started) == 4


def test_a_second_runner_cannot_start_while_one_holds_the_runner_lock(world):
    w = world(names=NAMES)
    other = ledger_module.Ledger(w.settings.ledger_db_path, clock=w.clock, pid_alive=lambda pid: True)
    try:
        other.acquire_lock("runner:global", ttl_seconds=600)
        items = requests_for(*KEYS)
        outcome = runner.run_batch(items, services=w.services, ledger=w.ledger)
        assert outcome.aborted and outcome.abort_reason == "lock_unavailable" and outcome.results == ()
        assert outcome.not_started == tuple(i.request_id for i in items)
        assert w.ledger.list_runs() == [] and not w.settings.output_root.exists()
        assert other.get_lock("runner:global") is not None  # not released by the loser
    finally:
        other.close()


def test_max_clients_limits_one_batch(world):
    w = world(names=NAMES)
    items = requests_for(*KEYS)
    outcome = runner.run_batch(items, services=w.services, ledger=w.ledger, max_clients=2)
    assert len(outcome.results) == 2 and not outcome.aborted
    assert outcome.not_started == tuple(i.request_id for i in items[2:])
    assert runner.run_batch(items[2:], services=w.services, ledger=w.ledger).counts["succeeded"] == 3


def test_a_keyboard_interrupt_propagates_and_releases_the_runner_lock(world):
    def collect(client_run, today):
        raise KeyboardInterrupt()
    w = world(collect=collect)
    with pytest.raises(KeyboardInterrupt):
        batch(w, "example-co")
    assert w.ledger.get_lock("runner:global") is None and w.ledger.get_lock("client:example-co") is None


def test_only_request_objects_are_accepted(world):
    w = world()
    with pytest.raises(TypeError):
        runner.run_batch([{"client_key": "example-co"}], services=w.services, ledger=w.ledger)


# --- automatic retries ---------------------------------------------------------------------------------------------------------------

def flaky(fail_times, error):
    state = {"left": fail_times}

    def collect(client_run, today):
        if client_run.client_key == "beta-co" and state["left"] > 0:
            state["left"] -= 1
            return failing_collect(error)(client_run, today)
        return default_collect(client_run, today)
    return collect


def test_a_temporary_failure_is_retried_automatically_once_its_time_has_come(world):
    w = world(names=("Alpha Co", "Beta Co"), collect=flaky(1, gsc_source.SearchConsoleError("x", 503)))
    first = batch(w, "alpha-co", "beta-co")
    beta = w.ledger.get_run(f"beta-co/{PERIOD_ID}")
    assert beta.state == "failed" and beta.retry_after and first.counts["failed"] == 1
    assert runner.retry_requests_due(services=w.services, ledger=w.ledger) == []  # not yet
    w.clock.advance(minutes=10)
    due = runner.retry_requests_due(services=w.services, ledger=w.ledger)
    assert [(r.client_key, r.action, r.requested_by) for r in due] == [("beta-co", "retry", "runner:auto-retry")]
    outcome = runner.retry_due(services=w.services, ledger=w.ledger)
    assert [(r.client_key, r.state) for r in outcome.results] == [("beta-co", "audited")]
    assert w.ledger.get_run(f"beta-co/{PERIOD_ID}").attempt_count == 2
    assert runner.retry_requests_due(services=w.services, ledger=w.ledger) == []


def test_non_automatic_and_exhausted_failures_are_not_retried(world):
    w = world(names=("Alpha Co", "Beta Co"), collect=flaky(5, gsc_source.SearchConsoleError("x", 403)))
    batch(w, "beta-co")
    w.clock.advance(days=1)
    assert runner.retry_requests_due(services=w.services, ledger=w.ledger) == []  # a denial needs a person
    w2 = world(names=("Alpha Co", "Beta Co"), collect=flaky(5, RefreshError("x")))  # shares tmp: use a new batch
    assert runner.retry_requests_due(services=w2.services, ledger=w2.ledger) == []


def test_automatic_retries_stop_after_the_configured_attempts(world):
    w = world(names=("Alpha Co", "Beta Co"), collect=flaky(5, gsc_source.SearchConsoleError("x", 503)),
              execution={"max_automatic_attempts": 1})
    batch(w, "beta-co")
    w.clock.advance(hours=1)
    assert len(runner.retry_requests_due(services=w.services, ledger=w.ledger)) == 1
    runner.retry_due(services=w.services, ledger=w.ledger)
    run = w.ledger.get_run(f"beta-co/{PERIOD_ID}")
    assert run.state == "failed" and run.attempt_count == 2 and run.retry_after is None
    w.clock.advance(days=1)
    assert runner.retry_requests_due(services=w.services, ledger=w.ledger) == []


# --- summaries ---------------------------------------------------------------------------------------------------------------------------

def test_summaries_hold_no_client_names_paths_or_secrets(world):
    w = world(names=NAMES[:2], collect=flaky(1, gsc_source.SearchConsoleError("x", 403)))
    text = json.dumps(batch(w, "alpha-co", "beta-co").summary())
    for secret in ("Alpha Co", "Beta Co", str(w.tmp), "sender@example.test", "123456789", "example.test"):
        assert secret not in text
    contracts.validate_batch_summary(json.loads(text))
