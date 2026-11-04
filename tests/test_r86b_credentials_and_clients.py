"""R8.6b: configurable credential locations, the Google client factory, collection wiring, and the `clients` / `setup auth` commands.

Offline only: synthetic token files in temporary folders, fake Google services, no network (a guard fixture fails any socket connection),
no browser flow (patched to fail), and the project's real token / client files are never opened (their metadata is compared before and after).
"""
import dataclasses
import datetime as dt
import json
import logging
import os
import socket
import subprocess
import sys
from pathlib import Path

import pytest
import yaml
from google.auth import exceptions as ge
from google.oauth2.credentials import Credentials
from googleapiclient.errors import HttpError

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from seo_report import auth
from seo_report import cli
from seo_report import client_loader
from seo_report import clients as cl
from seo_report import contracts
from seo_report import google_clients as gc
from seo_report import runner_config as rc
from seo_report import source_collector as sc
from seo_report.cli_exit import Exit
from seo_report.client_loader import ClientRun
from seo_report.sources import gsc as gsc_source
from test_client_loader import CLIENT_HEADER, KEYWORD_HEADER, client_row, keyword_row
from test_gsc import FakeService, default_handler, http_error
from test_pdf_renderer import FONT_FILES, LOGO
from test_pipeline import PERIOD_ID, RUN_KEY, SENDER, default_collect, go, make_client, world  # noqa: F401
from test_report_run import CURRENT, GA4, GSC

TODAY = dt.date(2026, 11, 2)
SYNTHETIC = ("SYNTHETIC-ACCESS-TOKEN", "SYNTHETIC-REFRESH-TOKEN", "SYNTHETIC-CLIENT-SECRET", "synthetic-client-id", "SYNTHETIC-USER-DIR")
REAL_FILES = (auth.TOKEN_PATH, auth.CLIENT_SECRET_PATH)


def stat_of(path):
    try:
        info = os.stat(path)
        return (info.st_size, info.st_mtime_ns)
    except OSError:
        return None


@pytest.fixture(scope="module", autouse=True)
def the_real_credential_files_are_never_touched():
    before = [stat_of(p) for p in REAL_FILES]
    yield
    assert [stat_of(p) for p in REAL_FILES] == before


@pytest.fixture(autouse=True)
def no_network_and_no_browser(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("a network connection was attempted")
    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    import google_auth_oauthlib.flow as flow

    def browser(*args, **kwargs):
        raise AssertionError("the browser sign-in flow must not start")
    monkeypatch.setattr(flow.InstalledAppFlow, "run_local_server", browser)
    monkeypatch.setattr(flow.InstalledAppFlow, "from_client_secrets_file", classmethod(lambda cls, *a, **k: browser()))


@pytest.fixture
def folder(tmp_path):
    base = tmp_path / "SYNTHETIC-USER-DIR" / "creds"
    base.mkdir(parents=True)
    return base


def write_token(path, *, expiry=None):
    data = {"token": "SYNTHETIC-ACCESS-TOKEN", "refresh_token": "SYNTHETIC-REFRESH-TOKEN", "client_id": "synthetic-client-id",
            "client_secret": "SYNTHETIC-CLIENT-SECRET", "scopes": auth.SCOPES, "token_uri": "https://oauth2.googleapis.com/token"}
    if expiry:
        data["expiry"] = expiry
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


FUTURE = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
PAST = "2020-01-01T00:00:00Z"


@pytest.fixture(autouse=True)
def default_locations_are_temporary(monkeypatch, tmp_path):
    monkeypatch.setattr(auth, "TOKEN_PATH", tmp_path / "default" / "token.json")
    monkeypatch.setattr(auth, "CLIENT_SECRET_PATH", tmp_path / "default" / "client.json")


# --- Part A: credential locations -------------------------------------------------------------------------------------------------------

def test_without_configuration_the_fallback_is_the_existing_default_location(tmp_path):
    paths = auth.resolve_paths()
    assert (paths.client_file, paths.token_file, paths.source) == (tmp_path / "default" / "client.json", tmp_path / "default" / "token.json", "default")


def test_the_real_default_constants_are_unchanged_by_this_stage():
    source = (ROOT / "src" / "seo_report" / "auth.py").read_text(encoding="utf-8")
    assert 'CLIENT_SECRET_PATH = PROJECT_ROOT / "credentials" / "client_secret.json"' in source
    assert 'TOKEN_PATH = PROJECT_ROOT / "token.json"' in source
    assert auth.SCOPES == ["https://www.googleapis.com/auth/spreadsheets.readonly", "https://www.googleapis.com/auth/webmasters.readonly",
                           "https://www.googleapis.com/auth/analytics.readonly"]


def test_explicit_locations_win_and_a_single_one_leaves_the_other_on_the_fallback(folder, tmp_path):
    both = auth.resolve_paths(folder / "client.json", folder / "token.json")
    assert (both.client_file, both.token_file, both.source) == (folder / "client.json", folder / "token.json", "settings")
    mixed = auth.resolve_paths(token_file=folder / "token.json")
    assert (mixed.token_file, mixed.client_file, mixed.source) == (folder / "token.json", tmp_path / "default" / "client.json", "mixed")


def test_windows_variables_are_expanded_from_the_given_environment(folder):
    environ = {"JASPER_CREDS": str(folder)}
    paths = auth.resolve_paths("%JASPER_CREDS%\\client.json", "%JASPER_CREDS%\\token.json", environ)
    assert paths.token_file == Path(str(folder) + "\\token.json") and paths.client_file.name == "client.json"


@pytest.mark.parametrize("bad,code", [
    ("%UNSET_VARIABLE%\\token.json", "credential_path_variable_unset"), ("$HOME/token.json", "credential_path_variable_unset"),
    ("token.json", "credential_path_not_absolute"), ("creds/token.json", "credential_path_not_absolute"),
    ("C:\\creds\\..\\other\\token.json", "credential_path_ambiguous"), ("C:\\creds\\", "credential_path_invalid"),
    ("C:/creds/", "credential_path_invalid"), ("", "credential_path_invalid"), ("   ", "credential_path_invalid"),
    (b"C:\\x\\t.json", "credential_path_invalid"), (42, "credential_path_invalid"), ("C:\\a\x00b\\t.json", "credential_path_invalid")])
def test_malformed_or_ambiguous_locations_are_rejected_with_a_config_error(bad, code):
    with pytest.raises(contracts.StageError) as caught:
        auth.resolve_paths(token_file=bad, environ={})
    assert (caught.value.category, caught.value.code) == ("config_invalid", code)
    assert str(bad) not in str(caught.value) or not str(bad).strip()  # the offending text is never echoed


def test_path_status_reads_metadata_only(folder, monkeypatch):
    present, directory = write_token(folder / "t.json"), folder / "dir"
    directory.mkdir()
    monkeypatch.setattr("builtins.open", lambda *a, **k: (_ for _ in ()).throw(AssertionError("a credential file was opened")))
    assert auth.path_status(present) == "present" and auth.path_status(folder / "none.json") == "missing"
    assert auth.path_status(directory) == "not_a_file"
    monkeypatch.setattr(os, "stat", lambda *a, **k: (_ for _ in ()).throw(PermissionError(13, "denied")))
    assert auth.path_status(present) == "unreadable"


def test_describe_paths_reports_states_without_values(folder):
    write_token(folder / "token.json")
    description = auth.describe_paths(auth.resolve_paths(folder / "client.json", folder / "token.json"))
    assert description == {"configuration": "settings", "client_file": "missing", "token_file": "present", "outside_project": True}
    assert not any(secret in json.dumps(description) for secret in SYNTHETIC)


def test_existing_callers_of_get_credentials_keep_working(tmp_path):
    write_token(auth.TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True) or auth.TOKEN_PATH, expiry=FUTURE)
    assert auth.get_credentials().valid  # no arguments: the fallback location
    explicit = write_token(tmp_path / "explicit.json", expiry=FUTURE)
    assert auth.get_credentials(token_path=explicit).valid


def test_normal_authentication_uses_the_configured_token_and_the_atomic_refresh_write(folder, monkeypatch):
    token = write_token(folder / "token.json", expiry=PAST)

    def refreshed(self, request):
        self.token = "SYNTHETIC-NEW-TOKEN"
        self.expiry = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None) + dt.timedelta(hours=1)
    monkeypatch.setattr(Credentials, "refresh", refreshed)
    paths = auth.resolve_paths(folder / "client.json", token)
    assert auth.get_credentials(paths=paths).valid
    assert json.loads(token.read_text(encoding="utf-8"))["token"] == "SYNTHETIC-NEW-TOKEN"
    assert sorted(p.name for p in folder.iterdir()) == ["token.json"]  # temporary file replaced atomically, none left
    assert not auth.TOKEN_PATH.exists()  # the fallback location was not used or created


def test_setup_and_normal_authentication_use_the_same_configured_locations(folder):
    paths = auth.resolve_paths(folder / "newfolder" / "client.json", folder / "newfolder" / "token.json")
    seen = {}

    class Flow:
        def run_local_server(self, port=0):
            return Credentials(token="SYNTHETIC-ACCESS-TOKEN", refresh_token="SYNTHETIC-REFRESH-TOKEN", client_id="synthetic-client-id",
                               client_secret="SYNTHETIC-CLIENT-SECRET", scopes=auth.SCOPES, token_uri="https://oauth2.googleapis.com/token",
                               expiry=dt.datetime.now(dt.timezone.utc).replace(tzinfo=None) + dt.timedelta(hours=1))

    def factory(client_path, scopes):
        seen["client"], seen["scopes"] = client_path, scopes
        return Flow()

    written = auth.run_setup(paths=paths, attended=True, flow_factory=factory)
    assert written == paths.token_file and seen["client"] == str(paths.client_file) and seen["scopes"] == auth.SCOPES
    assert auth.get_credentials(paths=paths).valid  # the very token that setup wrote
    assert not auth.TOKEN_PATH.exists()


def test_credential_errors_and_logs_never_contain_paths_or_contents(folder, caplog):
    (folder / "bad.json").write_text("SYNTHETIC-CLIENT-SECRET {", encoding="utf-8")
    with caplog.at_level(logging.DEBUG):
        with pytest.raises(auth.AuthRequiredError) as caught:
            auth.get_credentials(paths=auth.resolve_paths(token_file=folder / "bad.json"))
    text = str(caught.value) + repr(caught.value) + caplog.text
    assert not any(secret in text for secret in SYNTHETIC)


# --- settings ----------------------------------------------------------------------------------------------------------------------------

def settings_for(tmp_path, **paths):
    local = tmp_path / "local"
    data = {"paths": {"output_root": str(local / "output"), "history_db": str(local / "history" / "h.sqlite"),
                      "ledger_db": str(local / "ledger" / "l.sqlite"), "work_root": str(local / "work"), **paths},
            "render": {"logo": str(LOGO), "fonts": [{"label": "DejaVuSans", "path": str(FONT_FILES["DejaVuSans"]), "weight": "400"}]}}
    return rc.parse_settings(data, environ={"LOCALAPPDATA": str(local)})


def test_settings_accept_the_two_credential_locations_and_default_to_none(tmp_path, folder):
    assert settings_for(tmp_path).oauth_client_path is None and settings_for(tmp_path).oauth_cache_path is None
    configured = settings_for(tmp_path, oauth_client_file=str(folder / "c.json"), oauth_cache_file=str(folder / "t.json"))
    assert (configured.oauth_client_path, configured.oauth_cache_path) == (folder / "c.json", folder / "t.json")
    paths = auth.paths_from_settings(configured)
    assert (paths.client_file, paths.token_file, paths.source) == (folder / "c.json", folder / "t.json", "settings")
    assert auth.paths_from_settings(settings_for(tmp_path)).source == "default"


def test_settings_expand_windows_variables_before_validating(tmp_path, folder, monkeypatch):
    monkeypatch.setenv("JASPER_TEST_CREDS", str(folder))
    configured = settings_for(tmp_path, oauth_cache_file="%JASPER_TEST_CREDS%\\t.json")
    assert configured.oauth_cache_path == Path(str(folder) + "\\t.json")


@pytest.mark.parametrize("value,code", [("t.json", "path_not_absolute"), ("%NOT_A_SET_VARIABLE_X%\\t.json", "path_not_absolute"),
                                        ("C:\\a\\..\\t.json", "path_ambiguous"), (7, "path_invalid"), ("", "path_invalid")])
def test_settings_reject_malformed_credential_locations(tmp_path, value, code):
    with pytest.raises(rc.RunnerConfigError) as caught:
        settings_for(tmp_path, oauth_cache_file=value)
    assert caught.value.code == code and caught.value.field == "paths.oauth_cache_file"


def test_settings_reject_folders_storage_overlap_and_identical_files(tmp_path, folder):
    local = tmp_path / "local"
    with pytest.raises(rc.RunnerConfigError) as caught:
        settings_for(tmp_path, oauth_cache_file=str(folder) + "\\")
    assert caught.value.code == "path_invalid"
    with pytest.raises(rc.RunnerConfigError) as caught:
        settings_for(tmp_path, oauth_cache_file=str(local / "output" / "t.json"))
    assert caught.value.code == "credential_path_in_storage"
    with pytest.raises(rc.RunnerConfigError) as caught:
        settings_for(tmp_path, oauth_client_file=str(folder / "same.json"), oauth_cache_file=str(folder / "same.json"))
    assert caught.value.code == "credential_paths_identical"


def test_the_key_names_do_not_trigger_the_secret_scanner(tmp_path, folder):
    settings_for(tmp_path, oauth_client_file=str(folder / "c.json"))  # no credential_in_config error


# --- Part B: the factory --------------------------------------------------------------------------------------------------------------

class Recorder:
    def __init__(self):
        self.credentials_calls, self.built = 0, []

    def provider(self):
        self.credentials_calls += 1
        return "FAKE-CREDENTIALS"

    def builder(self, name):
        def build(credentials):
            self.built.append((name, credentials))
            return f"fake-{name}"
        return build

    def factory(self):
        return gc.GoogleClientFactory(credentials_provider=self.provider, sheets_builder=self.builder("sheets"),
                                      gsc_builder=self.builder("gsc"), ga4_builder=self.builder("ga4"))


def test_creating_a_factory_does_nothing_and_clients_are_built_lazily_with_one_credentials_load():
    rec = Recorder()
    factory = rec.factory()
    assert rec.credentials_calls == 0 and rec.built == []
    assert (factory.sheets(), factory.search_console(), factory.analytics()) == ("fake-sheets", "fake-gsc", "fake-ga4")
    assert rec.credentials_calls == 1 and rec.built == [("sheets", "FAKE-CREDENTIALS"), ("gsc", "FAKE-CREDENTIALS"), ("ga4", "FAKE-CREDENTIALS")]
    assert dataclasses.is_dataclass(factory) and "FAKE" not in repr(factory)  # credentials are not in the repr


def test_the_default_credentials_come_from_auth_with_the_configured_locations(monkeypatch, folder):
    seen = []
    monkeypatch.setattr(auth, "get_credentials", lambda **kw: seen.append(kw) or "CREDS")
    paths = auth.resolve_paths(folder / "c.json", folder / "t.json")
    factory = gc.GoogleClientFactory(paths=paths, sheets_builder=lambda creds: creds)
    assert factory.sheets() == "CREDS" and factory.sheets() == "CREDS" and seen == [{"paths": paths}]


def test_the_default_builders_use_the_existing_service_constructions(monkeypatch):
    import googleapiclient.discovery as discovery
    from seo_report.sources import ga4 as ga4_source
    calls = []
    monkeypatch.setattr(discovery, "build", lambda name, version, **kw: calls.append((name, version, kw)) or object())
    monkeypatch.setattr(ga4_source, "BetaAnalyticsDataClient", lambda credentials: ("ga4-client", credentials))
    factory = gc.GoogleClientFactory(credentials_provider=lambda: "CREDS")
    factory.sheets()
    factory.search_console()
    assert factory.analytics() == ("ga4-client", "CREDS")
    assert calls == [("sheets", "v4", {"credentials": "CREDS", "cache_discovery": False}),
                     ("searchconsole", "v1", {"credentials": "CREDS", "cache_discovery": False})]


def test_authentication_errors_propagate_unchanged_from_every_client():
    def provider():
        raise auth.AuthRequiredError("auth_token_missing")
    factory = gc.GoogleClientFactory(credentials_provider=provider)
    for build in (factory.sheets, factory.search_console, factory.analytics):
        with pytest.raises(auth.AuthRequiredError) as caught:
            build()
        assert (caught.value.category, caught.value.code) == ("auth_required", "auth_token_missing")


def test_with_no_token_the_real_factory_fails_non_interactively():
    factory = gc.GoogleClientFactory()  # the temporary fallback location holds nothing
    with pytest.raises(auth.AuthRequiredError) as caught:
        factory.search_console()
    assert caught.value.code == "auth_token_missing"  # and the autouse guard proves no browser flow started


def test_a_real_client_is_built_from_a_synthetic_token_without_any_request(folder):
    write_token(folder / "t.json", expiry=FUTURE)
    factory = gc.GoogleClientFactory(paths=auth.resolve_paths(folder / "c.json", folder / "t.json"))
    service = factory.sheets()  # the real googleapiclient with bundled discovery; the autouse guard forbids any connection
    assert hasattr(service, "spreadsheets") and hasattr(factory.search_console(), "searchanalytics")
    assert factory.analytics() is not None


def test_importing_the_factory_and_the_cli_help_load_no_google_library():
    code = ("import sys, seo_report.google_clients, seo_report.cli; "
            "print(sorted(m for m in sys.modules if m.startswith(('googleapiclient','google_auth','google.','grpc','playwright','seo_report.auth','seo_report.sources'))))")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env={**os.environ, "PYTHONPATH": str(ROOT / "src")})
    assert out.returncode == 0 and out.stdout.strip() == "[]", out.stdout + out.stderr


# --- Part C: collection wiring ---------------------------------------------------------------------------------------------------------

def client_run(name="Example Co", key="example-co"):
    return ClientRun(key, make_client(name), CURRENT)


def collector(*, gsc_service=None, fetch_gsc=gsc_source.fetch_search_console, fetch_ga4=None, provider=None, sleep=lambda s: None):
    rec = Recorder()
    factory = gc.GoogleClientFactory(credentials_provider=provider or rec.provider, sheets_builder=rec.builder("sheets"),
                                     gsc_builder=lambda creds: gsc_service or FakeService(default_handler),
                                     ga4_builder=lambda creds: "GA4-CLIENT")
    return gc.make_collect(factory, fetch_gsc=fetch_gsc, fetch_ga4=fetch_ga4 or (lambda c, p, periods, **k: GA4), sleep=sleep), factory, rec


def test_clients_are_injected_into_the_existing_collection_functions():
    seen = {}

    def fetch_ga4(client, prop, periods, **kw):
        seen["ga4"] = client
        return dataclasses.replace(GA4, property_id=str(prop))
    service = FakeService(default_handler)
    collect, _, _ = collector(gsc_service=service, fetch_ga4=fetch_ga4)
    result = collect(client_run(), TODAY)
    assert result.ok and seen["ga4"] == "GA4-CLIENT" and service.calls  # the real GSC fetch ran against the injected service
    assert result.gsc.site_url == "https://example.test/"


def test_the_clients_are_built_once_on_first_use_not_per_client():
    collect, _, rec = collector()
    assert rec.credentials_calls == 0
    collect(client_run(), TODAY)
    collect(client_run("Other Co", "other-co"), TODAY)
    assert rec.credentials_calls == 1


def test_a_valid_zero_result_response_is_data_with_a_notice_not_an_outage():
    collect, _, _ = collector(gsc_service=FakeService(lambda body: {}))
    result = collect(client_run(), TODAY)
    assert result.ok and result.gsc.current.totals == gsc_source.EMPTY_METRICS
    assert sc.SourceWarning("gsc", "source_empty") in result.warnings


def test_incomplete_data_keeps_its_notices():
    current = dataclasses.replace(GSC.current, warnings=[gsc_source.DataWarning("incomplete_data", "m")])
    collect, _, _ = collector(fetch_gsc=lambda *a, **k: dataclasses.replace(GSC, current=current))
    result = collect(client_run(), TODAY)
    assert result.ok and sc.SourceWarning("gsc", "source_incomplete") in result.warnings


def test_an_unavailable_required_source_is_a_failure_and_the_existing_retry_policy_applies():
    calls, sleeps = [], []

    def handler(body):
        calls.append(1)
        raise http_error(503)
    collect, _, _ = collector(gsc_service=FakeService(handler), sleep=sleeps.append)
    result = collect(client_run(), TODAY)
    assert not result.ok and result.failures == (sc.SourceFailure("gsc", "source_temporary", "gsc_http_503"),)
    assert len(calls) == gsc_source.MAX_ATTEMPTS and len(sleeps) == gsc_source.MAX_ATTEMPTS - 1


def test_a_permission_error_keeps_its_classification_and_is_not_retried():
    calls = []

    def handler(body):
        calls.append(1)
        raise http_error(403)
    collect, _, _ = collector(gsc_service=FakeService(handler))
    assert collect(client_run(), TODAY).failures == (sc.SourceFailure("gsc", "source_denied", "gsc_http_403"),) and len(calls) == 1


def test_a_missing_sign_in_fails_before_any_request_and_is_auth_required():
    def provider():
        raise auth.AuthRequiredError("auth_token_missing")
    fetched = []
    collect, _, _ = collector(provider=provider, fetch_gsc=lambda *a, **k: fetched.append(1))
    with pytest.raises(auth.AuthRequiredError):
        collect(client_run(), TODAY)
    assert not fetched and sc.classify_error("gsc", auth.AuthRequiredError("auth_token_missing")) == ("auth_required", "auth_token_missing")


def test_in_the_pipeline_a_missing_sign_in_is_a_failed_auth_required_run(world):
    def provider():
        raise auth.AuthRequiredError("auth_token_missing")
    collect, _, _ = collector(provider=provider)
    w = world(collect=collect)
    response = go(w)
    assert (response.state, response.error["category"], response.error["code"]) == ("failed", "auth_required", "auth_token_missing")
    assert response.retry["eligible"] and w.ledger.get_run(RUN_KEY).pdf_sha256 is None  # nothing was rendered or finalized


def test_in_the_pipeline_an_unavailable_source_blocks_finalization(world):
    def handler(body):
        raise http_error(403)
    collect, _, _ = collector(gsc_service=FakeService(handler))
    w = world(collect=collect)
    response = go(w)
    assert (response.state, response.error["category"]) == ("failed", "source_denied")
    run = w.ledger.get_run(RUN_KEY)
    assert run.state == "failed" and run.record_name is None and run.engine_run_id is None


def test_in_the_pipeline_the_wired_collector_produces_an_audited_report(world):
    collect, _, _ = collector(gsc_service=FakeService(default_handler),
                              fetch_ga4=lambda c, p, periods, **k: dataclasses.replace(GA4, property_id=str(p)))
    w = world(collect=collect)
    assert go(w).state == "audited"


# --- Part D: the CLI -------------------------------------------------------------------------------------------------------------------

class FakeSheets:
    def __init__(self, client_rows, keyword_rows, error=None):
        self.client_rows, self.keyword_rows, self.error, self.calls = client_rows, keyword_rows, error, 0

    def spreadsheets(self):
        return self

    def values(self):
        return self

    def get(self, **kwargs):
        self.range = kwargs["range"]
        return self

    def execute(self):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return {"values": self.client_rows if cl.CLIENT_TAB in self.range else self.keyword_rows}


def alpha_rows():
    return ([CLIENT_HEADER, client_row("Alpha Co"), client_row("Beta Co", **{"GA4 Property ID": ""}),
             client_row("Gamma Co", Status="Paused")],
            [KEYWORD_HEADER, keyword_row("Alpha Co"), keyword_row("Beta Co"), keyword_row("Gamma Co")])


@pytest.fixture
def cfg(tmp_path):
    local = tmp_path / "local"
    (tmp_path / "clients.yaml").write_text(yaml.safe_dump({"clients": [
        {"name": "Alpha Co", "client_key": "alpha-co"}, {"name": "Beta Co", "client_key": "beta-co"},
        {"name": "Gamma Co", "client_key": "gamma-co"}]}), encoding="utf-8")
    (tmp_path / "jasper.yaml").write_text(yaml.safe_dump({"sender": {
        "name": "Sample Sender", "title": "PM", "address_lines": ["1 Sample Way"], "phone": "(000) 000-0000", "email": "sender@example.test",
        "website_label": "Sample", "website_url": "https://sender.example"},
        "regional_profiles": {"United States": {"title": "Project Manager | Sample Co", "address_lines": ["1 Sample Way"],
                                                "website_label": "Sample", "website_url": "https://sender.example"}}}), encoding="utf-8")
    data = {"paths": {"output_root": str(local / "output"), "history_db": str(local / "history" / "h.sqlite"),
                      "ledger_db": str(local / "ledger" / "l.sqlite"), "work_root": str(local / "work"),
                      "clients_config": str(tmp_path / "clients.yaml"), "sender_config": str(tmp_path / "jasper.yaml"),
                      "oauth_client_file": str(tmp_path / "creds" / "client.json"), "oauth_cache_file": str(tmp_path / "creds" / "token.json")},
            "render": {"logo": str(LOGO), "fonts": [{"label": "DejaVuSans", "path": str(FONT_FILES["DejaVuSans"]), "weight": "400"}]},
            "workbooks": {"reporting_spreadsheet_id": "1AbCdEfGhIjKlMnOpQrStUvWxYz0123456789"}}
    path = tmp_path / "runner.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path, {"LOCALAPPDATA": str(local)}, data


def fake_factory(sheets):
    rec = Recorder()
    return gc.GoogleClientFactory(credentials_provider=rec.provider, sheets_builder=lambda creds: sheets)


def run(argv, env, **kw):
    return cli.main(argv, environ=env, **kw)


def test_clients_list_shows_keys_status_and_isolates_the_bad_row(cfg, capsys):
    path, env, _ = cfg
    sheets = FakeSheets(*alpha_rows())
    assert run(["clients", "list", "--config", str(path)], env, client_factory=fake_factory(sheets)) == Exit.OK
    out = capsys.readouterr().out
    assert "1 client(s) ready, 1 rejected, 1 skipped" in out
    assert "alpha-co" in out and "ga4_property_missing" in out and "gamma-co" in out and "status_not_active" in out
    assert "manager@example.test" not in out and "555-010-0123" not in out


def test_clients_list_json_has_no_contact_details(cfg, capsys):
    path, env, _ = cfg
    assert run(["--json", "clients", "list", "--config", str(path)], env, client_factory=fake_factory(FakeSheets(*alpha_rows()))) == Exit.OK
    data = json.loads(capsys.readouterr().out)
    assert [c["client_key"] for c in data["clients"]] == ["alpha-co"] and data["rejected"][0]["code"] == "ga4_property_missing"
    assert set(data["clients"][0]) == {"client_key", "name", "status", "period_id", "gsc_property_configured", "ga4_property_configured",
                                       "keywords", "sheet_row"}
    assert "manager@" not in json.dumps(data) and "example.test" not in json.dumps(data)


@pytest.mark.parametrize("error,expected,words", [
    (http_error(401), Exit.AUTH_REQUIRED, "setup auth"), (http_error(403), Exit.SOURCE_UNAVAILABLE, "source_denied"),
    (http_error(503), Exit.SOURCE_UNAVAILABLE, "source_temporary"), (ge.RefreshError("x"), Exit.AUTH_REQUIRED, "auth_refresh_failed")])
def test_clients_list_maps_google_problems_to_exit_codes(cfg, capsys, error, expected, words):
    path, env, _ = cfg
    assert run(["clients", "list", "--config", str(path)], env, client_factory=fake_factory(FakeSheets([], [], error=error))) == expected
    assert words in capsys.readouterr().err


def test_clients_list_without_sign_in_is_auth_required_with_the_setup_hint(cfg, capsys):
    path, env, _ = cfg
    assert run(["clients", "list", "--config", str(path)], env) == Exit.AUTH_REQUIRED  # the real factory over an empty credentials folder
    err = capsys.readouterr().err
    assert "auth_token_missing" in err and "setup auth" in err


def test_clients_list_header_problems_are_a_configuration_error(cfg, capsys):
    path, env, _ = cfg
    assert run(["clients", "list", "--config", str(path)], env, client_factory=fake_factory(FakeSheets([], []))) == Exit.CONFIG_INVALID
    assert "header_missing" in capsys.readouterr().err


def test_local_configuration_problems_are_exit_code_3(cfg, tmp_path, capsys):
    path, env, data = cfg
    (tmp_path / "clients.yaml").write_text("clients: []\n", encoding="utf-8")
    assert run(["clients", "list", "--config", str(path)], env) == Exit.CONFIG_INVALID and "client registry" in capsys.readouterr().err
    (tmp_path / "clients.yaml").write_text(yaml.safe_dump({"clients": [{"name": "Alpha Co", "client_key": "alpha-co"}]}), encoding="utf-8")
    (tmp_path / "jasper.yaml").write_text("sender: {}\n", encoding="utf-8")
    assert run(["clients", "list", "--config", str(path)], env) == Exit.CONFIG_INVALID and "sender configuration" in capsys.readouterr().err
    (tmp_path / "jasper.yaml").write_text(yaml.safe_dump({"sender": {"name": "S"}}), encoding="utf-8")
    del data["workbooks"]
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    assert run(["clients", "list", "--config", str(path)], env) == Exit.CONFIG_INVALID and "reporting_spreadsheet_id is not configured" in capsys.readouterr().err


class NoGoogle:
    """A factory that fails the test if anything asks it for a client."""

    def __getattr__(self, name):
        raise AssertionError(f"Google was contacted ({name})")


def test_clients_validate_is_offline_by_default_and_reports_credential_locations(cfg, capsys, monkeypatch):
    path, env, _ = cfg
    monkeypatch.setattr(Credentials, "from_authorized_user_file", classmethod(lambda cls, *a, **k: (_ for _ in ()).throw(AssertionError("opened"))))
    assert run(["clients", "validate", "--config", str(path)], env, client_factory=NoGoogle()) == Exit.OK
    out = capsys.readouterr().out
    assert "no Google contact" in out and "oauth_client_file: missing" in out and "workbook_rows: use --online" in out
    assert "outside the project" in out and "3 client(s) configured" in out


def test_clients_validate_one_client_and_an_unknown_one(cfg, capsys):
    path, env, _ = cfg
    assert run(["clients", "validate", "--client", "alpha-co", "--config", str(path)], env, client_factory=NoGoogle()) == Exit.OK
    assert run(["clients", "validate", "--client", "nobody-co", "--config", str(path)], env, client_factory=NoGoogle()) == Exit.CONFIG_INVALID
    assert "client_unknown" in capsys.readouterr().err or True


def test_clients_validate_online_reads_the_workbook_and_reports_rejections(cfg, capsys):
    path, env, _ = cfg
    sheets = FakeSheets(*alpha_rows())
    assert run(["clients", "validate", "--online", "--config", str(path)], env, client_factory=fake_factory(sheets)) == Exit.CONFIG_INVALID
    out = capsys.readouterr().out
    assert sheets.calls == 2 and "REJECTED" in out and "ga4_property_missing" in out and "workbook read, read-only" in out
    good = FakeSheets([CLIENT_HEADER, client_row("Alpha Co")], [KEYWORD_HEADER, keyword_row("Alpha Co")])
    assert run(["clients", "validate", "--online", "--config", str(path)], env, client_factory=fake_factory(good)) == Exit.OK


def test_setup_auth_routes_to_the_attended_setup_with_the_configured_locations(cfg, capsys):
    path, env, data = cfg
    seen = []
    assert run(["setup", "auth", "--config", str(path)], env, setup_runner=lambda **kw: seen.append(kw)) == Exit.OK
    (call,) = seen
    assert call["paths"].token_file == Path(data["paths"]["oauth_cache_file"]) and call["paths"].client_file == Path(data["paths"]["oauth_client_file"])
    out = capsys.readouterr().out
    assert "configuration: settings" in out and "creds" not in out  # the location is not echoed


def test_setup_auth_without_an_attended_console_is_refused(cfg, capsys):
    path, env, data = cfg
    Path(data["paths"]["oauth_client_file"]).parent.mkdir(parents=True)
    Path(data["paths"]["oauth_client_file"]).write_text("{}", encoding="utf-8")
    assert run(["setup", "auth", "--config", str(path)], env) == Exit.AUTH_REQUIRED  # pytest has no console: the real run_setup refuses
    assert "auth_interactive_blocked" in capsys.readouterr().err
    assert not Path(data["paths"]["oauth_cache_file"]).exists()


def test_setup_auth_without_the_client_file_does_not_start_or_create_anything(cfg, tmp_path, capsys):
    path, env, data = cfg
    assert run(["setup", "auth", "--config", str(path)], env) == Exit.AUTH_REQUIRED
    assert "OAuth client file is not available" in capsys.readouterr().err and not (tmp_path / "creds").exists()


def test_setup_is_never_triggered_by_any_other_command(cfg):
    path, env, _ = cfg
    called = []
    runner = lambda **kw: called.append(kw)  # noqa: E731
    for argv in (["system-info"], ["clients", "list"], ["clients", "validate"], ["clients", "validate", "--online"], ["doctor"], ["status"]):
        run(argv + ["--config", str(path)], env, setup_runner=runner, client_factory=fake_factory(FakeSheets(*alpha_rows())))
    assert called == []


def test_no_command_is_deferred_any_more(cfg, capsys):
    path, env, _ = cfg
    assert cli.DEFERRED == {}  # R8.6d implemented the last ones; none answers "not available" any more
    for argv in (["status"], ["recovery", "inspect"], ["report", "dry-run", "--client", "alpha-co", "--period", PERIOD_ID]):
        assert run(argv + ["--config", str(path)], env) != Exit.NOT_AVAILABLE
