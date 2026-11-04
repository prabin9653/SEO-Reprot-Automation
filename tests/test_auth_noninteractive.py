"""R8.4.5: non-interactive Google authentication (synthetic tokens in temporary folders; no Google call, no real credential).

Every test passes its own temporary token path or replaces the module's default paths: the project's real `token.json` and
`credentials/client_secret.json` are never opened.
"""
import ast
import datetime as dt
import inspect
import json
import logging
import sys
import traceback
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from google.auth import exceptions as ge
from google.oauth2.credentials import Credentials

from seo_report import auth
from seo_report import contracts
from seo_report import source_collector as sc
from seo_report.sources import ga4 as ga4_source
from seo_report.sources import gsc as gsc_source
from seo_report.sources import sheets

SECRETS = ("SYNTHETIC-ACCESS-TOKEN", "SYNTHETIC-REFRESH-TOKEN", "SYNTHETIC-CLIENT-SECRET", "synthetic-client-id")


def write_token(path: Path, *, expiry=None, refresh=True, token="SYNTHETIC-ACCESS-TOKEN"):
    data = {"token": token, "client_id": "synthetic-client-id", "client_secret": "SYNTHETIC-CLIENT-SECRET",
            "scopes": auth.SCOPES, "token_uri": "https://oauth2.googleapis.com/token"}
    if refresh:
        data["refresh_token"] = "SYNTHETIC-REFRESH-TOKEN"
    if expiry is not None:
        data["expiry"] = expiry
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


PAST = "2020-01-01T00:00:00Z"
FUTURE = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%SZ")


@pytest.fixture
def token(tmp_path):
    return tmp_path / "token.json"


@pytest.fixture
def no_browser(monkeypatch):
    """Any attempt to start the interactive flow fails the test."""
    calls = []

    def forbidden(*args, **kwargs):
        calls.append(1)
        raise AssertionError("the interactive OAuth flow must not start")

    import google_auth_oauthlib.flow as flow
    monkeypatch.setattr(flow.InstalledAppFlow, "from_client_secrets_file", classmethod(lambda cls, *a, **k: forbidden()))
    monkeypatch.setattr(flow.InstalledAppFlow, "run_local_server", forbidden)
    return calls


@pytest.fixture(autouse=True)
def default_paths_are_never_used(monkeypatch, tmp_path):
    monkeypatch.setattr(auth, "TOKEN_PATH", tmp_path / "default-token-absent.json")
    monkeypatch.setattr(auth, "CLIENT_SECRET_PATH", tmp_path / "default-secret-absent.json")


def failure(token_path, **kw):
    with pytest.raises(contracts.StageError) as caught:
        auth.get_credentials(token_path=token_path, **kw)
    return caught.value


# --- failures a person must fix ----------------------------------------------------------------------------------------------

def test_a_missing_token_is_a_structured_auth_required_outcome(token, no_browser):
    error = failure(token)
    assert isinstance(error, auth.AuthRequiredError)
    assert (error.category, error.code) == ("auth_required", "auth_token_missing")
    assert isinstance(error.__cause__, FileNotFoundError)
    assert not no_browser


def test_the_default_token_location_is_used_when_no_path_is_given(no_browser):
    with pytest.raises(auth.AuthRequiredError) as caught:
        auth.get_credentials()  # TOKEN_PATH points at an absent temporary file in these tests
    assert caught.value.code == "auth_token_missing" and not no_browser


@pytest.mark.parametrize("content", ["", "not json", "[1, 2]", "{}", '{"refresh_token": "x"}'])
def test_a_malformed_token_is_invalid_credentials(token, no_browser, content):
    token.write_text(content, encoding="utf-8")
    assert failure(token).code == "auth_credentials_invalid" and not no_browser


@pytest.mark.parametrize("raised", [PermissionError(13, "denied"), OSError(5, "io")])
def test_an_unreadable_token_file_is_reported_as_unreadable(token, no_browser, monkeypatch, raised):
    token.write_text("{}", encoding="utf-8")

    def refuse(cls, *args, **kwargs):
        raise raised
    monkeypatch.setattr(Credentials, "from_authorized_user_file", classmethod(refuse))
    error = failure(token)
    assert (error.category, error.code) == ("auth_required", "auth_credentials_unreadable") and not no_browser


def test_a_token_file_without_a_refresh_token_is_invalid_credentials(token, no_browser):
    write_token(token, expiry=PAST, refresh=False)  # the library refuses such a file as malformed
    assert failure(token).code == "auth_credentials_invalid" and not no_browser


def test_an_expired_token_that_holds_no_refresh_token_needs_a_person(token, no_browser, monkeypatch):
    expired = Credentials(token="SYNTHETIC-ACCESS-TOKEN", expiry=dt.datetime(2020, 1, 1))
    monkeypatch.setattr(auth, "_load", lambda path: expired)
    assert failure(token).code == "auth_refresh_unavailable" and not no_browser


def test_a_refresh_refused_by_google_needs_a_person(token, no_browser, monkeypatch):
    write_token(token, expiry=PAST)
    monkeypatch.setattr(Credentials, "refresh", lambda self, request: (_ for _ in ()).throw(ge.RefreshError("refused")))
    error = failure(token)
    assert (error.category, error.code) == ("auth_required", "auth_refresh_failed")
    assert isinstance(error.__cause__, ge.RefreshError) and not no_browser


@pytest.mark.parametrize("args", [("invalid_grant: Token has been expired or revoked.", {"error": "invalid_grant"}),
                                  ("x", {"error": "invalid_grant", "error_description": "Bad Request"})])
def test_a_revoked_or_expired_grant_is_reported_as_revoked(token, no_browser, monkeypatch, args):
    write_token(token, expiry=PAST)
    monkeypatch.setattr(Credentials, "refresh", lambda self, request: (_ for _ in ()).throw(ge.RefreshError(*args)))
    assert failure(token).code == "auth_credentials_revoked" and not no_browser


def test_other_authentication_library_errors_are_invalid_credentials(token, no_browser, monkeypatch):
    write_token(token, expiry=PAST)
    monkeypatch.setattr(Credentials, "refresh", lambda self, request: (_ for _ in ()).throw(ge.OAuthError("bad")))
    assert failure(token).code == "auth_credentials_invalid" and not no_browser


def test_a_refresh_that_cannot_reach_google_is_temporary_not_an_authentication_failure(token, no_browser, monkeypatch):
    write_token(token, expiry=PAST)
    for raised in (ge.TransportError("dns"), ge.TimeoutError("slow")):
        monkeypatch.setattr(Credentials, "refresh", lambda self, request, r=raised: (_ for _ in ()).throw(r))
        error = failure(token)
        assert isinstance(error, auth.AuthTemporaryError)
        assert (error.category, error.code) == ("source_temporary", "auth_network_error")
    assert not no_browser


def test_a_refresh_that_leaves_the_credentials_invalid_is_refused(token, no_browser, monkeypatch):
    write_token(token, expiry=PAST)
    monkeypatch.setattr(Credentials, "refresh", lambda self, request: None)  # no new token, still expired
    assert failure(token).code == "auth_refresh_failed"


# --- the working paths -------------------------------------------------------------------------------------------------------

def test_a_valid_token_is_returned_without_a_refresh_or_a_write(token, no_browser, monkeypatch):
    write_token(token, expiry=FUTURE)
    before = token.read_bytes()
    monkeypatch.setattr(Credentials, "refresh", lambda *a: (_ for _ in ()).throw(AssertionError("no refresh expected")))
    creds = auth.get_credentials(token_path=token)
    assert creds.valid and token.read_bytes() == before and not no_browser


def refreshed(self, request):
    self.token = "SYNTHETIC-NEW-TOKEN"
    self.expiry = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None) + dt.timedelta(hours=1)


def test_an_expired_token_is_refreshed_and_cached_atomically(token, no_browser, monkeypatch):
    write_token(token, expiry=PAST)
    monkeypatch.setattr(Credentials, "refresh", refreshed)
    creds = auth.get_credentials(token_path=token)
    assert creds.valid and json.loads(token.read_text(encoding="utf-8"))["token"] == "SYNTHETIC-NEW-TOKEN"
    assert [p.name for p in token.parent.iterdir()] == ["token.json"]  # no temporary file left behind
    assert not no_browser


def test_a_token_that_cannot_be_saved_still_serves_this_run(token, no_browser, monkeypatch, caplog):
    write_token(token, expiry=PAST)
    before = token.read_bytes()
    monkeypatch.setattr(Credentials, "refresh", refreshed)
    monkeypatch.setattr(auth.os, "replace", lambda *a: (_ for _ in ()).throw(PermissionError(13, "locked")))
    with caplog.at_level(logging.DEBUG, logger="seo_report.auth"):
        creds = auth.get_credentials(token_path=token)
    assert creds.valid and token.read_bytes() == before  # the old file is intact
    assert [p.name for p in token.parent.iterdir()] == ["token.json"]
    assert "could not be saved" in caplog.text and "PermissionError" in caplog.text


# --- the production path can never be interactive ---------------------------------------------------------------------------

def test_get_credentials_has_no_interactive_option():
    parameters = inspect.signature(auth.get_credentials).parameters
    assert set(parameters) == {"token_path", "refresh_request", "paths"}  # `paths` (R8.6b) only says WHERE the token is
    assert not any("interactive" in name or "browser" in name for name in parameters)


@pytest.mark.parametrize("scenario", ["missing", "no_refresh", "refused", "revoked", "malformed"])
def test_no_failure_of_get_credentials_ever_starts_the_browser_flow(token, no_browser, monkeypatch, scenario):
    if scenario == "no_refresh":
        write_token(token, expiry=PAST, refresh=False)
    elif scenario in ("refused", "revoked"):
        write_token(token, expiry=PAST)
        args = ("x", {"error": "invalid_grant"}) if scenario == "revoked" else ("x",)
        monkeypatch.setattr(Credentials, "refresh", lambda self, request: (_ for _ in ()).throw(ge.RefreshError(*args)))
    elif scenario == "malformed":
        token.write_text("{", encoding="utf-8")
    with pytest.raises(auth.AuthRequiredError):
        auth.get_credentials(token_path=token)
    assert not no_browser


def test_the_source_clients_use_the_non_interactive_credentials(no_browser):
    for build in (sheets.build_service, gsc_source.build_service, ga4_source.build_client):
        with pytest.raises(auth.AuthRequiredError) as caught:
            build()  # default paths: an absent temporary token
        assert caught.value.code == "auth_token_missing"
    assert not no_browser


def test_the_interactive_flow_is_referenced_only_inside_the_setup_function():
    tree = ast.parse((ROOT / "src" / "seo_report" / "auth.py").read_text(encoding="utf-8"))
    for node in tree.body:  # nothing at module level imports or uses the OAuth flow library
        for sub in ast.walk(node) if not isinstance(node, (ast.FunctionDef, ast.ClassDef)) else ():
            assert not (isinstance(sub, ast.ImportFrom) and (sub.module or "").startswith("google_auth_oauthlib"))
    holders = set()
    for function in (n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)):
        text = ast.get_source_segment((ROOT / "src" / "seo_report" / "auth.py").read_text(encoding="utf-8"), function)
        if "run_local_server" in text or "google_auth_oauthlib" in text:
            holders.add(function.name)
    assert holders == {"run_setup"}


def test_no_other_source_module_can_start_an_interactive_flow():
    offenders = []
    for path in (ROOT / "src").rglob("*.py"):
        if path.name == "auth.py":
            continue
        text = path.read_text(encoding="utf-8")
        if any(word in text for word in ("InstalledAppFlow", "run_local_server", "google_auth_oauthlib")):
            offenders.append(path.name)
    assert offenders == []


# --- explicit setup stays available and isolated -----------------------------------------------------------------------------

class FakeFlow:
    def __init__(self, secret_path, scopes):
        self.secret_path, self.scopes = secret_path, scopes

    def run_local_server(self, port=0):
        creds = Credentials(token="SYNTHETIC-SETUP-TOKEN", refresh_token="SYNTHETIC-REFRESH-TOKEN",
                            client_id="synthetic-client-id", client_secret="SYNTHETIC-CLIENT-SECRET", scopes=auth.SCOPES,
                            token_uri="https://oauth2.googleapis.com/token")
        assert port == 0
        return creds


def test_setup_refuses_to_run_without_an_attended_console(token, no_browser):
    for attended in (False, None):  # None: detect; pytest has no console
        with pytest.raises(auth.AuthRequiredError) as caught:
            auth.run_setup(token_path=token, attended=attended, flow_factory=FakeFlow)
        assert caught.value.code == "auth_interactive_blocked"
    assert not token.exists() and not no_browser


def test_an_attended_setup_caches_a_token_with_the_unchanged_scopes(tmp_path, token):
    seen = {}

    def factory(path, scopes):
        seen["scopes"] = scopes
        return FakeFlow(path, scopes)

    result = auth.run_setup(client_secret_path=tmp_path / "secret.json", token_path=token, attended=True,
                            flow_factory=factory)
    assert result == token and json.loads(token.read_text(encoding="utf-8"))["token"] == "SYNTHETIC-SETUP-TOKEN"
    assert seen["scopes"] == auth.SCOPES == [
        "https://www.googleapis.com/auth/spreadsheets.readonly", "https://www.googleapis.com/auth/webmasters.readonly",
        "https://www.googleapis.com/auth/analytics.readonly"]


@pytest.mark.parametrize("raised,code", [(FileNotFoundError(), "auth_token_missing"),
                                         (PermissionError(), "auth_credentials_unreadable"),
                                         (ValueError("bad"), "auth_credentials_invalid")])
def test_setup_failures_are_structured_too(token, raised, code):
    def factory(path, scopes):
        raise raised
    with pytest.raises(auth.AuthRequiredError) as caught:
        auth.run_setup(token_path=token, attended=True, flow_factory=factory)
    assert caught.value.code == code and not token.exists()


def test_the_setup_command_line(capsys):
    assert auth.main([]) == 2 and auth.main(["login"]) == 2
    assert auth.main(["setup"]) == 1  # pytest has no console: blocked, nothing started
    output = capsys.readouterr().out
    assert "auth_interactive_blocked" in output and not any(s in output for s in SECRETS)


# --- no secret values anywhere ----------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("scenario", ["missing", "no_refresh", "refused", "revoked", "malformed", "transport", "unsaved"])
def test_no_secret_reaches_an_exception_message_or_a_log(token, no_browser, monkeypatch, caplog, scenario):
    leak = "SYNTHETIC-CLIENT-SECRET SYNTHETIC-REFRESH-TOKEN SYNTHETIC-ACCESS-TOKEN"
    if scenario == "no_refresh":
        write_token(token, expiry=PAST, refresh=False)
    elif scenario in ("refused", "revoked", "transport", "unsaved"):
        write_token(token, expiry=PAST)
        raised = {"refused": ge.RefreshError(leak), "revoked": ge.RefreshError(leak, {"error": "invalid_grant"}),
                  "transport": ge.TransportError(leak)}.get(scenario)
        if raised is not None:
            monkeypatch.setattr(Credentials, "refresh", lambda self, request: (_ for _ in ()).throw(raised))
        else:
            monkeypatch.setattr(Credentials, "refresh", refreshed)
            monkeypatch.setattr(auth.os, "replace", lambda *a: (_ for _ in ()).throw(OSError(leak)))
    elif scenario == "malformed":
        token.write_text(leak + " {", encoding="utf-8")
    with caplog.at_level(logging.DEBUG):
        try:
            auth.get_credentials(token_path=token)
        except contracts.StageError as error:
            for text in (str(error), repr(error), error.code):
                assert not any(secret in text for secret in SECRETS), scenario
    assert not any(secret in caplog.text for secret in SECRETS), scenario
    assert not any(secret in record.getMessage() for secret in SECRETS for record in caplog.records)
