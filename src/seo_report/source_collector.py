"""Collecting Search Console and GA4 data for one client and period (R8.3).

A thin service around the existing `sources.gsc.fetch_search_console` and `sources.ga4.fetch_ga4`, which keep their own
retry behaviour (429/5xx retried up to 4 times with backoff) [verified]. The Google clients are injected, so tests use
offline fakes and R8.3 makes no live call.

Rules
  * A fetch that raises a *known* source error is classified (fixed category and code) and the source is **wholly
    unavailable**: its data is None and a `SourceFailure` is recorded. A wholly unavailable source blocks finalization
    and delivery: the pipeline fails the run when `SourceResult.failures` is not empty.
  * A successful fetch is validated before report assembly: the right type, the requested property, the requested
    periods and non-negative integer metrics. A response that does not satisfy this is `source_invalid`.
  * A valid zero-result response is **not** unavailable: totals of zero come with the source's own `empty_period`
    warning and are reported as the warning `source_empty`. Incomplete or truncated data keeps its warnings
    (`source_incomplete`, `source_truncated`, ...) and the existing report notices represent it.
  * Authentication problems (`401`, a refresh failure) are `auth_required` and are never downgraded to "missing data".
  * Unknown exceptions are **not** caught here: a programming error stays visible.

Failure categories are those of `contracts.ERROR_CATEGORIES`; codes are fixed lower-case identifiers.
"""

import dataclasses
import errno
import json
import socket
import ssl
import time
from dataclasses import dataclass

import grpc
import httplib2
import requests.exceptions as requests_errors
from google.api_core import exceptions as api_errors
from google.auth import exceptions as auth_errors
from google.auth.exceptions import RefreshError
from googleapiclient.errors import HttpError

from seo_report.contracts import StageError
from seo_report.sources import ga4 as ga4_source
from seo_report.sources import gsc as gsc_source

_TEMPORARY = {408, 429, 500, 502, 503, 504}
# OSError numbers that mean "the network, not this machine": anything else is not guessed to be temporary
_NETWORK_ERRNOS = frozenset(getattr(errno, name) for name in (
    "ECONNRESET", "ECONNREFUSED", "ECONNABORTED", "ENETUNREACH", "EHOSTUNREACH", "ENETDOWN", "ETIMEDOUT", "EPIPE",
    "EHOSTDOWN", "ENETRESET", "ENOTCONN") if hasattr(errno, name))
_GRPC_TEMPORARY = {"UNAVAILABLE", "DEADLINE_EXCEEDED", "RESOURCE_EXHAUSTED", "ABORTED", "CANCELLED", "INTERNAL"}
_GRPC_DENIED = {"PERMISSION_DENIED", "NOT_FOUND"}
_GRPC_INVALID = {"INVALID_ARGUMENT", "FAILED_PRECONDITION", "OUT_OF_RANGE", "UNIMPLEMENTED"}
_RATE_LIMIT_REASONS = {"ratelimitexceeded", "userratelimitexceeded", "quotaexceeded", "rate_limit_exceeded"}


@dataclass(frozen=True)
class SourceFailure:
    source: str  # gsc | ga4
    category: str
    code: str


@dataclass(frozen=True)
class SourceWarning:
    source: str
    code: str


@dataclass(frozen=True)
class SourceResult:
    gsc: object | None
    ga4: object | None
    failures: tuple = ()
    warnings: tuple = ()

    @property
    def ok(self) -> bool:
        return not self.failures

    @property
    def unavailable_sources(self) -> tuple:
        return tuple(f.source for f in self.failures)


def _from_status(source: str, status) -> tuple[str, str]:
    if status == 401:
        return "auth_required", f"{source}_http_401"
    if status in (403, 404):
        return "source_denied", f"{source}_http_{status}"
    if status in _TEMPORARY:
        return "source_temporary", f"{source}_http_{status}"
    if status == 400:
        return "source_invalid", f"{source}_http_400"
    return "source_invalid", f"{source}_request_rejected"


def _http_status(error: HttpError):
    try:
        return int(error.resp.status)
    except (AttributeError, TypeError, ValueError):
        return None


def _rate_limited(error: HttpError) -> bool:
    """A 403 that Google sends for quota/rate limits is temporary, not a permission problem."""
    try:
        body = json.loads(error.content.decode("utf-8", "replace"))
        reasons = {str(item.get("reason", "")).lower() for item in body["error"].get("errors", ()) if isinstance(item, dict)}
    except (AttributeError, KeyError, TypeError, ValueError):
        return False
    return bool(reasons & _RATE_LIMIT_REASONS)


def _from_grpc(source: str, name: str) -> tuple[str, str] | None:
    name = name.upper()
    if name == "UNAUTHENTICATED":
        return "auth_required", f"{source}_grpc_unauthenticated"
    if name in _GRPC_TEMPORARY:
        return "source_temporary", f"{source}_grpc_{name.lower()}"
    if name in _GRPC_DENIED:
        return "source_denied", f"{source}_grpc_{name.lower()}"
    if name in _GRPC_INVALID:
        return "source_invalid", f"{source}_grpc_{name.lower()}"
    return None  # UNKNOWN, DATA_LOSS, ...: not guessed


def _revoked(error: RefreshError) -> bool:
    """Google answers `invalid_grant` when the token was revoked, expired for good or the password changed."""
    return any(isinstance(part, dict) and part.get("error") == "invalid_grant" for part in error.args[1:])


def classify_error(source: str, error: BaseException) -> tuple[str, str] | None:
    """(category, code) for a known source error, or None for anything else (which the caller must not swallow).

    Order matters: errors that have already been classified by the authentication module, then every authentication
    problem (never downgraded to a temporary one), then network and service errors by their exact class, and only then
    the sources' own errors. Anything not recognised returns None and is re-raised by the caller."""
    if isinstance(error, StageError):  # raised by `auth.get_credentials` (already category + code)
        return error.category, error.code
    # -- the network, as seen by the authentication library ----------------------------------------------------------------
    if isinstance(error, auth_errors.TransportError):
        return "source_temporary", "auth_network_error"
    if isinstance(error, auth_errors.TimeoutError):
        return "source_temporary", f"{source}_timeout"
    # -- authentication ----------------------------------------------------------------------------------------------------
    if isinstance(error, RefreshError):
        if _revoked(error):
            return "auth_required", "auth_credentials_revoked"
        return "auth_required", "auth_refresh_failed"
    if isinstance(error, auth_errors.GoogleAuthError):  # missing/malformed/unusable credentials, OAuth errors
        return "auth_required", "auth_credentials_invalid"
    if isinstance(error, (FileNotFoundError, NotADirectoryError)):
        return "auth_required", "auth_credentials_missing"
    if isinstance(error, PermissionError):
        return "auth_required", "auth_credentials_unreadable"
    # -- HTTP, gRPC and the network, by exact class ----------------------------------------------------------------------------
    if isinstance(error, HttpError):
        status = _http_status(error)
        if status == 403 and _rate_limited(error):
            return "source_temporary", f"{source}_http_403_rate_limited"
        return _from_status(source, status)
    if isinstance(error, api_errors.RetryError):
        return "source_temporary", f"{source}_retry_exhausted"
    if isinstance(error, api_errors.GoogleAPICallError):
        status = error.code if isinstance(error.code, int) and not isinstance(error.code, bool) else None
        if status is not None:
            return _from_status(source, int(status))
        name = getattr(getattr(error, "grpc_status_code", None), "name", None)
        return _from_grpc(source, name) if name else None
    if isinstance(error, grpc.RpcError):
        code = error.code() if callable(getattr(error, "code", None)) else None
        return _from_grpc(source, getattr(code, "name", "")) if code is not None else None
    if isinstance(error, requests_errors.HTTPError) and getattr(error, "response", None) is not None:
        return _from_status(source, getattr(error.response, "status_code", None))
    if isinstance(error, (socket.gaierror, httplib2.ServerNotFoundError)):
        return "source_temporary", f"{source}_dns_error"
    if isinstance(error, requests_errors.Timeout):
        return "source_temporary", f"{source}_timeout"
    if isinstance(error, (requests_errors.ConnectionError, requests_errors.ChunkedEncodingError, ssl.SSLError)):
        return "source_temporary", f"{source}_network_error"
    if isinstance(error, gsc_source.SearchConsoleError):
        if error.status is None:
            return "source_denied", "gsc_property_not_accessible"
        return _from_status(source, error.status)
    if isinstance(error, ga4_source.Ga4Error):
        if error.incompatible:
            return "source_incompatible", "ga4_schema_incompatible"
        if error.status is None:
            return "source_invalid", "ga4_request_invalid"
        return _from_status(source, error.status)
    if isinstance(error, (TimeoutError, ConnectionError)):
        return "source_temporary", f"{source}_network_error"
    if isinstance(error, OSError) and error.errno in _NETWORK_ERRNOS:
        return "source_temporary", f"{source}_network_error"
    return None  # any other OSError (a full disk, a bad handle) or exception is not guessed to be a network problem


def _count(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _valid_gsc(data, client, periods) -> bool:
    if not isinstance(data, gsc_source.SearchConsoleData) or data.site_url != client.gsc_property:
        return False
    for got, wanted in ((data.current, periods.current), (data.previous, periods.previous)):
        totals = getattr(got, "totals", None)
        if getattr(got, "period", None) != wanted or totals is None or not (_count(totals.clicks) and _count(totals.impressions)):
            return False
    return True


def _valid_ga4(data, client, periods) -> bool:
    if not isinstance(data, ga4_source.Ga4Data) or data.property_id != str(client.ga4_property_id).strip():
        return False
    for got, wanted in ((data.current, periods.current), (data.previous, periods.previous)):
        whole = getattr(got, "whole_site", None)
        if getattr(got, "period", None) != wanted or whole is None or not (_count(whole.sessions) and _count(whole.total_users)):
            return False
    return True


_WARNING_CODES = {"empty_period": "source_empty", "incomplete_data": "source_incomplete", "truncated": "source_truncated"}


def _warnings(source: str, data) -> list[SourceWarning]:
    found = {_WARNING_CODES.get(w.code, "source_" + "".join(ch if ch.isalnum() else "_" for ch in w.code.lower()))
             for w in data.warnings}
    return [SourceWarning(source, code) for code in sorted(found)]


def collect_sources(client, periods, *, gsc_service, ga4_client, today, sleep=time.sleep,
                    fetch_gsc=gsc_source.fetch_search_console, fetch_ga4=ga4_source.fetch_ga4) -> SourceResult:
    """Fetch and validate both sources. `gsc_service` / `ga4_client` are the injected Google clients; `fetch_gsc` and
    `fetch_ga4` default to the existing functions and are replaceable in tests."""
    failures, warnings, gsc_data, ga4_data = [], [], None, None

    try:
        gsc_data = fetch_gsc(gsc_service, client.gsc_property, periods, today=today, sleep=sleep)
    except Exception as error:
        known = classify_error("gsc", error)
        if known is None:
            raise
        failures.append(SourceFailure("gsc", *known))
    else:
        if _valid_gsc(gsc_data, client, periods):
            warnings += _warnings("gsc", gsc_data)
        else:
            gsc_data = None
            failures.append(SourceFailure("gsc", "source_invalid", "gsc_response_invalid"))

    try:
        ga4_data = fetch_ga4(ga4_client, client.ga4_property_id, periods, today=today, sleep=sleep)
    except Exception as error:
        known = classify_error("ga4", error)
        if known is None:
            raise
        failures.append(SourceFailure("ga4", *known))
    else:
        if _valid_ga4(ga4_data, client, periods):
            warnings += _warnings("ga4", ga4_data)
        else:
            ga4_data = None
            failures.append(SourceFailure("ga4", "source_invalid", "ga4_response_invalid"))

    return SourceResult(gsc_data, ga4_data, tuple(failures), tuple(warnings))


def worst_failure(failures) -> SourceFailure:
    """The failure that decides the run's category: authentication first, then temporary, then the rest."""
    order = {"auth_required": 0, "source_temporary": 1, "source_denied": 2, "source_incompatible": 3, "source_invalid": 4}
    return min(failures, key=lambda f: order.get(f.category, 9))
