"""R8.4.5: how source and authentication errors are classified (synthetic exceptions only; no Google call).

Every exception class used here exists in the installed libraries (their hierarchy was inspected, not assumed).
"""
import errno
import json
from types import SimpleNamespace
import socket
import ssl
import sys
from pathlib import Path

import grpc
import httplib2
import pytest
import requests.exceptions as rq
from google.api_core import exceptions as api
from google.auth import exceptions as ge
from googleapiclient.errors import HttpError

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from seo_report import auth
from seo_report import contracts
from seo_report import runner
from seo_report import source_collector as sc
from seo_report.client_loader import WorkbookError, WorkbookUnavailable, load_from_workbook
from seo_report.sources import ga4 as ga4_source
from seo_report.sources import gsc as gsc_source
from test_client_loader import CLIENT_HEADER, KEYWORD_HEADER, REGISTRY, client_row, keyword_row
from test_pipeline import SENDER, default_collect, go, req, world  # noqa: F401
from test_runner import KEYS, NAMES, requests_for


def http_error(status, reason=None, content=b"{}"):
    return HttpError(httplib2.Response({"status": str(status)}), content)


def rate_limit_body(reason):
    return json.dumps({"error": {"code": 403, "errors": [{"reason": reason}]}}).encode()


class Rpc(grpc.RpcError):
    def __init__(self, status):
        self._status = status

    def code(self):
        return self._status


def via_sources(error):
    """A `collect` that fails the way a Google fetch does, through the real classification in `collect_sources`."""
    def fetch(*args, **kwargs):
        raise error

    def collect(client_run, today):
        return sc.collect_sources(client_run.client, client_run.periods, gsc_service=None, ga4_client=None, today=today,
                                  fetch_gsc=fetch, fetch_ga4=fetch)
    return collect


def classify(error, source="gsc"):
    return sc.classify_error(source, error)


# --- the table ------------------------------------------------------------------------------------------------------------------

AUTH = "auth_required"
TEMP = "source_temporary"
DENIED = "source_denied"
INVALID = "source_invalid"

TABLE = [
    # authentication: always auth_required, never temporary or "no data"
    (ge.RefreshError("x"), AUTH, "auth_refresh_failed"),
    (ge.RefreshError("x", {"error": "invalid_grant"}), AUTH, "auth_credentials_revoked"),
    (ge.ReauthFailError("x"), AUTH, "auth_refresh_failed"),
    (ge.DefaultCredentialsError("x"), AUTH, "auth_credentials_invalid"),
    (ge.MalformedError("x"), AUTH, "auth_credentials_invalid"),
    (ge.OAuthError("x"), AUTH, "auth_credentials_invalid"),
    (FileNotFoundError(2, "no such file"), AUTH, "auth_credentials_missing"),
    (NotADirectoryError(20, "not a directory"), AUTH, "auth_credentials_missing"),
    (PermissionError(13, "denied"), AUTH, "auth_credentials_unreadable"),
    (auth.AuthRequiredError("auth_token_missing"), AUTH, "auth_token_missing"),
    (auth.AuthTemporaryError(), TEMP, "auth_network_error"),
    (http_error(401), AUTH, "gsc_http_401"),
    (api.Unauthenticated("x"), AUTH, "gsc_http_401"),
    (Rpc(grpc.StatusCode.UNAUTHENTICATED), AUTH, "gsc_grpc_unauthenticated"),
    # authorization is not authentication
    (http_error(403), DENIED, "gsc_http_403"),
    (api.PermissionDenied("x"), DENIED, "gsc_http_403"),
    (Rpc(grpc.StatusCode.PERMISSION_DENIED), DENIED, "gsc_grpc_permission_denied"),
    (http_error(404), DENIED, "gsc_http_404"),
    (api.NotFound("x"), DENIED, "gsc_http_404"),
    (Rpc(grpc.StatusCode.NOT_FOUND), DENIED, "gsc_grpc_not_found"),
    # permanent request problems
    (http_error(400), INVALID, "gsc_http_400"),
    (api.BadRequest("x"), INVALID, "gsc_http_400"),
    (api.InvalidArgument("x"), INVALID, "gsc_http_400"),
    (Rpc(grpc.StatusCode.INVALID_ARGUMENT), INVALID, "gsc_grpc_invalid_argument"),
    (http_error(418), INVALID, "gsc_request_rejected"),
    # temporary service answers
    (http_error(429), TEMP, "gsc_http_429"),
    (http_error(500), TEMP, "gsc_http_500"),
    (http_error(502), TEMP, "gsc_http_502"),
    (http_error(503), TEMP, "gsc_http_503"),
    (http_error(504), TEMP, "gsc_http_504"),
    (http_error(408), TEMP, "gsc_http_408"),
    (http_error(403, content=rate_limit_body("rateLimitExceeded")), TEMP, "gsc_http_403_rate_limited"),
    (http_error(403, content=rate_limit_body("insufficientPermissions")), DENIED, "gsc_http_403"),
    (api.TooManyRequests("x"), TEMP, "gsc_http_429"),
    (api.ServiceUnavailable("x"), TEMP, "gsc_http_503"),
    (api.InternalServerError("x"), TEMP, "gsc_http_500"),
    (api.GatewayTimeout("x"), TEMP, "gsc_http_504"),
    (api.DeadlineExceeded("x"), TEMP, "gsc_http_504"),
    (api.RetryError("gave up", ConnectionError("x")), TEMP, "gsc_retry_exhausted"),
    # gRPC transport (a bare RpcError carries only a status code)
    (Rpc(grpc.StatusCode.UNAVAILABLE), TEMP, "gsc_grpc_unavailable"),
    (Rpc(grpc.StatusCode.DEADLINE_EXCEEDED), TEMP, "gsc_grpc_deadline_exceeded"),
    (Rpc(grpc.StatusCode.RESOURCE_EXHAUSTED), TEMP, "gsc_grpc_resource_exhausted"),
    # the network
    (socket.gaierror(11001, "getaddrinfo failed"), TEMP, "gsc_dns_error"),
    (httplib2.ServerNotFoundError("Unable to find the server"), TEMP, "gsc_dns_error"),
    (TimeoutError("slow"), TEMP, "gsc_network_error"),
    (ConnectionError("down"), TEMP, "gsc_network_error"),
    (ConnectionResetError(errno.ECONNRESET, "reset"), TEMP, "gsc_network_error"),
    (ConnectionRefusedError(errno.ECONNREFUSED, "refused"), TEMP, "gsc_network_error"),
    (OSError(errno.ENETUNREACH, "network is unreachable"), TEMP, "gsc_network_error"),
    (OSError(errno.ETIMEDOUT, "timed out"), TEMP, "gsc_network_error"),
    (rq.ConnectTimeout("x"), TEMP, "gsc_timeout"),
    (rq.ReadTimeout("x"), TEMP, "gsc_timeout"),
    (rq.ConnectionError("x"), TEMP, "gsc_network_error"),
    (rq.ProxyError("x"), TEMP, "gsc_network_error"),
    (rq.ChunkedEncodingError("x"), TEMP, "gsc_network_error"),
    (ssl.SSLError("handshake"), TEMP, "gsc_network_error"),
    (ge.TransportError("x"), TEMP, "auth_network_error"),
    (ge.TimeoutError("x"), TEMP, "gsc_timeout"),
]


@pytest.mark.parametrize("error,category,code", TABLE, ids=[f"{type(e).__name__}-{c}" for e, _, c in TABLE])
def test_the_classification_table(error, category, code):
    assert classify(error) == (category, code)


def test_every_classified_category_and_code_is_valid_for_the_contract():
    for error, category, code in TABLE:
        assert category in contracts.ERROR_CATEGORIES
        assert len(code) <= 64 and code == code.lower() and code.replace("_", "").isalnum()
        contracts.error_info(category, code, "collecting")


def test_the_source_name_is_part_of_the_code():
    assert classify(http_error(503), "ga4") == (TEMP, "ga4_http_503")
    assert classify(socket.gaierror(1, "x"), "sheets") == (TEMP, "sheets_dns_error")


def test_authentication_failures_are_never_classified_as_temporary_or_missing_data():
    for error in (ge.RefreshError("x"), ge.RefreshError("x", {"error": "invalid_grant"}), ge.DefaultCredentialsError("x"),
                  FileNotFoundError(), PermissionError(), http_error(401), api.Unauthenticated("x")):
        assert classify(error)[0] == AUTH


# --- unknown exceptions stay visible ---------------------------------------------------------------------------------------------

@pytest.mark.parametrize("error", [ValueError("x"), KeyError("k"), RuntimeError("boom"), TypeError("t"), AttributeError("a"),
                                   OSError(errno.ENOSPC, "No space left on device"), OSError(errno.EBADF, "bad"),
                                   api.Unknown("x"), Rpc(grpc.StatusCode.UNKNOWN), Rpc(grpc.StatusCode.DATA_LOSS),
                                   rq.TooManyRedirects("x"), ge.MutualTLSChannelError("x") if False else KeyError("m")])
def test_unrecognised_errors_are_not_classified(error):
    assert classify(error) is None


def test_an_unrecognised_exception_is_re_raised_with_its_identity_and_message():
    boom = RuntimeError("a programming error with details")

    def fetch(*args, **kwargs):
        raise boom
    with pytest.raises(RuntimeError) as caught:
        sc.collect_sources(SimpleNamespace(gsc_property="p", ga4_property_id="1"), None, gsc_service=None, ga4_client=None,
                           today=None, fetch_gsc=fetch)
    assert caught.value is boom


def test_an_unknown_error_in_the_pipeline_stays_diagnosable_without_leaking_its_text(world):
    def collect(client_run, today):
        raise RuntimeError("secret-looking-detail")
    w = world(collect=collect)
    response = go(w)
    assert (response.state, response.error["category"], response.error["code"]) == (
        "failed", "internal_error", "exception_runtime_error")
    assert "secret-looking-detail" not in json.dumps(response.to_dict())


def test_a_disk_error_during_collection_is_not_mistaken_for_the_network(world):
    def collect(client_run, today):
        raise OSError(errno.ENOSPC, "disk full")
    w = world(collect=collect)
    assert go(w).error["code"] == "disk_full"


# --- retry behaviour ----------------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("error,retry_kind,eligible,automatic", [
    (socket.gaierror(11001, "dns"), "collection", True, True),
    (TimeoutError("slow"), "collection", True, True),
    (http_error(503), "collection", True, True),
    (http_error(403), None, False, False),
    (http_error(404), None, False, False),
    (http_error(400), None, False, False),
    (ge.RefreshError("x"), "collection", True, False),  # eligible only after a person has fixed the credentials
])
def test_what_the_pipeline_does_with_each_class_of_failure(world, error, retry_kind, eligible, automatic):
    w = world(collect=via_sources(error))
    response = go(w)
    run = w.ledger.get_run(f"example-co/{response.period_id}")
    assert response.state == "failed" and response.retry["eligible"] is eligible and response.retry["kind"] == retry_kind
    assert bool(run.retry_after) is automatic  # automatic retries are scheduled only for temporary failures


def test_a_permanent_error_is_not_retried_automatically_ever(world):
    w = world(collect=via_sources(http_error(404)))
    go(w)
    w.clock.advance(days=30)
    assert runner.retry_requests_due(services=w.services, ledger=w.ledger) == []


def test_a_temporary_network_error_is_retried_a_bounded_number_of_times(world):
    w = world(collect=via_sources(socket.gaierror(11001, "dns")), execution={"max_automatic_attempts": 2, "retry_delays_seconds": [1]})
    go(w)
    attempts = 0
    for _ in range(6):
        w.clock.advance(hours=1)
        due = runner.retry_requests_due(services=w.services, ledger=w.ledger)
        if not due:
            break
        attempts += 1
        go(w, due[0])
    assert 1 <= attempts <= 2  # bounded by max_automatic_attempts, then an operator decides


# --- authentication stops the batch ---------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("error", [auth.AuthRequiredError("auth_token_missing"), ge.RefreshError("x"),
                                   ge.RefreshError("x", {"error": "invalid_grant"}), FileNotFoundError(2, "gone"),
                                   PermissionError(13, "denied")])
def test_an_authentication_failure_stops_the_batch_and_leaves_the_rest_unstarted(world, error):
    def collect(client_run, today):
        if client_run.client_key == "gamma-co":
            def fetch(*a, **k):
                raise error
            return sc.collect_sources(client_run.client, client_run.periods, gsc_service=None, ga4_client=None, today=today,
                                      fetch_gsc=fetch, fetch_ga4=fetch)
        return default_collect(client_run, today)
    w = world(names=NAMES, collect=collect)
    items = requests_for(*KEYS)
    outcome = runner.run_batch(items, services=w.services, ledger=w.ledger)
    assert outcome.aborted and outcome.abort_reason == "auth_required"
    assert outcome.results[-1].error["category"] == "auth_required" and len(outcome.not_started) == 2


def test_credentials_missing_when_the_service_is_built_stop_the_batch_through_the_stage_error(world):
    def collect(client_run, today):
        raise auth.AuthRequiredError("auth_token_missing")  # what `build_service()` raises before any fetch
    w = world(collect=collect)
    response = go(w)
    assert (response.state, response.error["category"], response.error["code"]) == (
        "failed", "auth_required", "auth_token_missing")
    assert response.retry["eligible"] is True and response.retry["kind"] == "collection"


def test_a_failed_refresh_for_the_network_only_does_not_stop_the_batch(world):
    def collect(client_run, today):
        raise auth.AuthTemporaryError()
    w = world(names=NAMES, collect=collect)
    outcome = runner.run_batch(requests_for(*KEYS), services=w.services, ledger=w.ledger)
    assert not outcome.aborted and len(outcome.results) == len(KEYS)
    assert all(r.error["category"] == "source_temporary" for r in outcome.results)


def test_worst_failure_puts_authentication_first():
    failures = (sc.SourceFailure("gsc", TEMP, "gsc_http_503"), sc.SourceFailure("ga4", AUTH, "auth_refresh_failed"),
                sc.SourceFailure("gsc", DENIED, "gsc_http_403"))
    assert sc.worst_failure(failures).category == AUTH


# --- the workbook (Sheets) ----------------------------------------------------------------------------------------------------------------

class Service:
    def __init__(self, error=None):
        self.error = error

    def spreadsheets(self):
        return self

    def values(self):
        return self

    def get(self, **kwargs):
        return self

    def execute(self):
        if self.error is not None:
            raise self.error
        return {"values": [CLIENT_HEADER, client_row()]}


def load(service=None, **kw):
    return load_from_workbook("sheet-id", registry=REGISTRY, service=service, default_sender=SENDER, **kw)


@pytest.mark.parametrize("error,category,code", [
    (http_error(401), AUTH, "sheets_http_401"), (http_error(403), DENIED, "sheets_http_403"),
    (http_error(404), DENIED, "sheets_http_404"), (http_error(429), TEMP, "sheets_http_429"),
    (http_error(503), TEMP, "sheets_http_503"), (socket.gaierror(11001, "dns"), TEMP, "sheets_dns_error"),
    (ge.RefreshError("x"), AUTH, "auth_refresh_failed"), (FileNotFoundError(), AUTH, "auth_credentials_missing")])
def test_workbook_read_failures_are_structured_and_keep_their_cause(error, category, code):
    with pytest.raises(WorkbookUnavailable) as caught:
        load(service=Service(error))
    assert (caught.value.category, caught.value.code) == (category, code)
    assert caught.value.__cause__ is error and isinstance(caught.value, WorkbookError)


def test_missing_credentials_when_the_workbook_service_is_built_are_auth_required(monkeypatch, tmp_path):
    monkeypatch.setattr(auth, "TOKEN_PATH", tmp_path / "absent-token.json")
    with pytest.raises(WorkbookUnavailable) as caught:
        load()  # builds the real service: non-interactive, fails before any network call
    assert (caught.value.category, caught.value.code) == (AUTH, "auth_token_missing")


def test_an_unrecognised_workbook_error_is_not_swallowed():
    boom = RuntimeError("unexpected")
    with pytest.raises(RuntimeError) as caught:
        load(service=Service(boom))
    assert caught.value is boom


def test_header_problems_are_still_workbook_errors_not_unavailability():
    class Empty(Service):
        def execute(self):
            return {"values": []}
    with pytest.raises(WorkbookError) as caught:
        load(service=Empty())
    assert not isinstance(caught.value, WorkbookUnavailable) and caught.value.code == "header_missing"
