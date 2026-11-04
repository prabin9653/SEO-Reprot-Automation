"""R8.4.6: structured operational logging and secret sanitization (synthetic secrets, temporary folders only)."""
import dataclasses
import errno
import json
import logging
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from google.oauth2.credentials import Credentials

from seo_report import auth
from seo_report import ledger as ledger_module
from seo_report import pipeline
from seo_report import runner
from seo_report.runner import REDACTED, sanitize_text, safe_value
from test_pipeline import PERIOD_ID, RUN_KEY, go, req, world  # noqa: F401

ACCESS = "ya29.SYNTHETICaccessTOKENvalue0123456789"
REFRESH = "1//SYNTHETICrefreshTOKENvalue0123456789"
SECRET = "GOCSPX-SYNTHETICclientSECRET0123"
OPAQUE = "AbCdEf0123456789AbCdEf0123456789AbCdEf0123"
SECRETS = (ACCESS, REFRESH, SECRET, OPAQUE, "SYNTHETICPASSWORD", "SYNTHETICKEY", "SYNTHETICBEARER")
LOG = logging.getLogger("seo_report.testlog")


@pytest.fixture
def log_file(tmp_path, monkeypatch):
    monkeypatch.setattr(runner.SafeRotatingFileHandler, "failures", 0)
    path = runner.configure_logging(tmp_path / "logs")
    yield path
    runner.shutdown_logging()


def lines(path):
    if not Path(path).exists():
        return []
    return [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines() if line.strip()]


def text_of(path):
    return Path(path).read_text(encoding="utf-8") if Path(path).exists() else ""


# --- the sanitizer ---------------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("raw", [
    f"token {ACCESS} expired", f"refresh {REFRESH}", f"secret {SECRET}", f"opaque {OPAQUE} value",
    "Authorization: Bearer SYNTHETICBEARER", "authorization=Basic SYNTHETICBEARER", "Bearer SYNTHETICBEARER",
    "access_token=SYNTHETICKEY&x=1", 'json {"refresh_token": "SYNTHETICKEY", "n": 1}', "client_secret: SYNTHETICKEY",
    "password=SYNTHETICPASSWORD", "api_key=SYNTHETICKEY", "PASSWORD: SYNTHETICPASSWORD",
    "-----BEGIN PRIVATE KEY-----\nSYNTHETICKEY\n-----END PRIVATE KEY-----"])
def test_secrets_are_removed_from_text(raw):
    cleaned = sanitize_text(raw)
    assert not any(secret in cleaned for secret in SECRETS + ("SYNTHETICKEY",)), cleaned
    assert REDACTED in cleaned


def test_diagnostics_that_are_not_secrets_survive():
    sha = "a" * 64
    text = f"run example-co/{PERIOD_ID} hash {sha} code auth_token_missing uuid 0198c1a2-7b3c-7d4e-8f5a-000000000001 http_429"
    assert sanitize_text(text) == text  # hashes, run keys, fixed codes and ids are the point of the log


def test_local_paths_are_reduced_to_a_file_name():
    for raw in (r"C:\Users\someone\OneDrive\Clients\Alpha Roofing Co\reports\report.pdf",
                "C:/Users/someone/Documents/Alpha Co/ledger.sqlite", r"\\server\share\Beta Co\x.pdf"):
        cleaned = sanitize_text(f"cannot open {raw} now")
        assert cleaned.endswith(" now") and "someone" not in cleaned and "Alpha" not in cleaned and "Beta" not in cleaned
        assert "<path>/" in cleaned


def test_text_is_bounded():
    assert len(sanitize_text("x " * 20000)) < 4100 and sanitize_text("x " * 20000).endswith("[truncated]")


def test_structured_values_are_sanitized_without_calling_repr():
    class Boom:
        def __repr__(self):
            raise AssertionError("repr must not be used")

        __str__ = __repr__

    value = {"Authorization": "Bearer SYNTHETICBEARER", "nested": {"client_secret": SECRET, "ok": 1, "list": [ACCESS, "fine"]},
             "creds": Boom(), "path": Path(r"C:\Users\x\Clients\Alpha Co\r.pdf"), "number": 3, "nan": float("nan"),
             "error": RuntimeError(f"failed with {ACCESS}"), "refresh_token": REFRESH, "set": {1}}
    safe = safe_value(value)
    dumped = json.dumps(safe)
    assert not any(secret in dumped for secret in SECRETS) and "Alpha" not in dumped
    assert safe["Authorization"] == REDACTED and safe["refresh_token"] == REDACTED and safe["nested"]["client_secret"] == REDACTED
    assert safe["creds"] == "<Boom>" and safe["number"] == 3 and safe["nested"]["ok"] == 1
    assert safe["error"].startswith("RuntimeError: ") and safe["nan"] == "nan"


def test_a_credentials_object_is_never_serialized():
    creds = Credentials(token=ACCESS, refresh_token=REFRESH, client_id="id", client_secret=SECRET)
    assert safe_value(creds) == "<Credentials>" and safe_value([creds, {"c": creds}]) == ["<Credentials>", {"c": "<Credentials>"}]
    assert not any(secret in repr(creds) for secret in SECRETS)


# --- the log file ---------------------------------------------------------------------------------------------------------------

def test_a_record_has_predictable_fields(log_file):
    with pipeline.log_context(client_key="example-co", period_id=PERIOD_ID, run_key=RUN_KEY, request_id="r-1"):
        pipeline.update_log_context(stage="rendering", run_id="0198c1a2-7b3c-7d4e-8f5a-000000000001")
        LOG.warning("render problem %s", "code_x", extra={"fields": {"error_category": "render_failed",
                                                                      "error_code": "render_timeout_pdf", "n": 2}})
    (record,) = lines(log_file)
    assert list(record)[:4] == ["ts", "level", "logger", "event"]
    assert record["level"] == "WARNING" and record["logger"] == "seo_report.testlog" and record["event"] == "render problem code_x"
    assert (record["client_key"], record["period_id"], record["run_key"], record["stage"], record["request_id"]) == (
        "example-co", PERIOD_ID, RUN_KEY, "rendering", "r-1")
    assert record["run_id"].startswith("0198c1a2") and record["error_category"] == "render_failed"
    assert record["error_code"] == "render_timeout_pdf" and record["fields"] == {"n": 2}
    assert record["ts"].endswith("+00:00")


def test_the_context_ends_with_the_block(log_file):
    with pipeline.log_context(client_key="a"):
        pass
    assert pipeline.LOG_CONTEXT.get() is None
    LOG.info("outside")
    assert "client_key" not in lines(log_file)[-1]


def test_secrets_in_messages_arguments_and_fields_never_reach_the_file(log_file):
    LOG.error("token %s secret=%s", ACCESS, SECRET, extra={"fields": {"refresh_token": REFRESH, "note": f"x {OPAQUE}",
                                                                     "headers": {"Authorization": "Bearer SYNTHETICBEARER"}}})
    LOG.info("dict args %(a)s", {"a": f"Bearer SYNTHETICBEARER {REFRESH}"})
    LOG.info("credentials %s", Credentials(token=ACCESS, refresh_token=REFRESH, client_id="i", client_secret=SECRET))
    content = text_of(log_file)
    assert content and not any(secret in content for secret in SECRETS)
    assert all(json.loads(line) for line in content.splitlines())


def test_exception_messages_and_tracebacks_are_sanitized_but_stay_useful(log_file):
    def fail():
        raise auth.AuthRequiredError("auth_token_missing") from RuntimeError(f"refresh failed {REFRESH} {ACCESS} password=SYNTHETICPASSWORD")

    try:
        fail()
    except auth.AuthRequiredError:
        LOG.exception("authentication failed C:\\Users\\someone\\Clients\\Alpha Co\\token.json")
    (record,) = lines(log_file)
    content = text_of(log_file)
    assert not any(secret in content for secret in SECRETS) and "someone" not in content and "Alpha" not in content
    assert record["exc_type"] == "AuthRequiredError" and (record["error_category"], record["error_code"]) == (
        "auth_required", "auth_token_missing")
    assert "Traceback" in record["exc"] and "in fail" in record["exc"] and "RuntimeError" in record["exc"]
    assert "<path>/" in record["exc"]  # source file locations are reduced to a file name


def test_an_unformattable_message_is_logged_safely():
    # built by hand: pytest's own capture handler would raise on a record like this before ours is asked
    bad = logging.LogRecord("seo_report.testlog", logging.INFO, __file__, 1, "needs two %s %s", ("only one",), None)
    assert json.loads(runner.JsonFormatter().format(bad))["event"] == "<log message could not be formatted>"
    odd = logging.LogRecord("seo_report.testlog", logging.INFO, __file__, 1, object(), None, None)
    runner.SanitizingFilter().filter(odd)
    assert json.loads(runner.JsonFormatter().format(odd))["event"] == "<object>"


def test_a_pipeline_run_logs_identifiers_and_hides_secrets_from_exceptions(world, log_file):
    def collect(client_run, today):
        raise ValueError(f"upstream said token={ACCESS} client_secret={SECRET} for C:\\Users\\someone\\Alpha Co\\x.json")
    w = world(collect=collect)
    response = go(w)
    assert response.error["code"] == "exception_value_error"
    records = lines(log_file)
    failure = next(r for r in records if r["event"].startswith("unexpected error in a pipeline stage"))
    assert failure["client_key"] == "example-co" and failure["period_id"] == PERIOD_ID
    assert failure["run_key"] == RUN_KEY and failure["stage"] == "collecting"
    assert (failure["error_category"], failure["error_code"]) == ("internal_error", "exception_value_error")
    assert failure["exc_type"] == "ValueError" and "ValueError" in failure["exc"]
    content = text_of(log_file)
    assert not any(secret in content for secret in SECRETS) and "someone" not in content and "Alpha" not in content
    assert any(r["event"].startswith("run failed") and r["error_code"] == "exception_value_error" for r in records)


# --- handlers, location and growth ---------------------------------------------------------------------------------------------------

def test_configuring_twice_never_duplicates_handlers_or_lines(tmp_path):
    first = runner.configure_logging(tmp_path / "logs")
    second = runner.configure_logging(tmp_path / "logs")
    try:
        tagged = [h for h in logging.getLogger("seo_report").handlers if getattr(h, runner._HANDLER_TAG, False)]
        assert len(tagged) == 1 and first == second
        LOG.info("once")
        assert [r["event"] for r in lines(second)] == ["once"]
    finally:
        runner.shutdown_logging()
    assert not [h for h in logging.getLogger("seo_report").handlers if getattr(h, runner._HANDLER_TAG, False)]
    assert logging.getLogger("seo_report").propagate is True
    runner.shutdown_logging()  # idempotent


def test_the_log_folder_must_be_local_absolute_and_outside_the_project_and_onedrive(tmp_path):
    for bad, code in ((Path("relative/logs"), "log_dir_not_absolute"), (runner.rc.PROJECT_ROOT / "logs", "log_dir_inside_project"),
                      (tmp_path / "OneDrive - Test" / "logs", "log_dir_in_cloud_sync"),
                      (tmp_path / "a" / ".." / "logs", "log_dir_ambiguous")):
        with pytest.raises(runner.LoggingConfigError) as caught:
            runner.configure_logging(bad)
        assert caught.value.code == code
    assert not (runner.rc.PROJECT_ROOT / "logs").exists()


def test_the_default_location_follows_the_local_storage_layout(tmp_path):
    path = runner.configure_logging(environ={"LOCALAPPDATA": str(tmp_path)})
    try:
        assert path == tmp_path / "JasperSEOReporting" / "logs" / "seo_report.log"
    finally:
        runner.shutdown_logging()


def test_log_growth_is_bounded(tmp_path):
    path = runner.configure_logging(tmp_path / "logs", max_bytes=2000, backups=2)
    try:
        for n in range(400):
            LOG.info("filler line number %s with some padding text to take space", n)
    finally:
        runner.shutdown_logging()
    files = sorted(p for p in path.parent.iterdir())
    assert 2 <= len(files) <= 3 and all(p.stat().st_size < 2000 + 600 for p in files)


# --- a failing logger never harms a run ---------------------------------------------------------------------------------------------------

def test_a_disk_error_while_logging_is_counted_not_raised(log_file, monkeypatch):
    handler = next(h for h in logging.getLogger("seo_report").handlers if getattr(h, runner._HANDLER_TAG, False))
    LOG.info("first")
    stream = handler.stream

    class Full:
        def write(self, data):
            raise OSError(errno.ENOSPC, "No space left on device")

        def flush(self):
            pass

        def close(self):
            pass

    handler.stream = Full()
    LOG.error("this record cannot be written")
    LOG.error("nor this one")
    assert runner.SafeRotatingFileHandler.failures == 2
    handler.stream = stream
    LOG.info("recovered")
    assert [r["event"] for r in lines(log_file)][-1] == "recovered"


class RaisingHandler(logging.Handler):
    def __init__(self, error):
        super().__init__()
        self.error = error

    def emit(self, record):
        raise self.error


def test_a_logging_failure_does_not_change_the_ledger_or_skip_cleanup(world):
    w = world(collect=lambda client_run, today: (_ for _ in ()).throw(ValueError("boom")))
    handler = RaisingHandler(OSError("log sink broke"))
    logger = logging.getLogger("seo_report.pipeline")
    logger.addHandler(handler)
    try:
        response = go(w)
    finally:
        logger.removeHandler(handler)
    assert (response.state, response.error["code"]) == ("failed", "exception_value_error")
    run = w.ledger.get_run(RUN_KEY)
    assert run.state == "failed" and run.error_code == "exception_value_error"
    assert w.ledger.get_lock("client:example-co") is None and w.ledger.get_lock("runner:global") is None


@pytest.mark.parametrize("interrupt", [KeyboardInterrupt, SystemExit])
def test_an_interrupt_in_a_stage_leaves_valid_logs_released_locks_and_a_clean_context(world, log_file, interrupt):
    def collect(client_run, today):
        raise interrupt()
    w = world(collect=collect)
    with pytest.raises(interrupt):
        go(w)
    assert pipeline.LOG_CONTEXT.get() is None
    assert w.ledger.get_lock("client:example-co") is None and w.ledger.get_lock("runner:global") is None
    assert all(isinstance(r, dict) for r in lines(log_file))  # every line is complete JSON


@pytest.mark.parametrize("interrupt", [KeyboardInterrupt, SystemExit])
def test_an_interrupt_raised_by_a_log_handler_still_releases_the_locks(world, interrupt):
    w = world(collect=lambda client_run, today: (_ for _ in ()).throw(ValueError("boom")))
    handler = RaisingHandler(interrupt())
    logger = logging.getLogger("seo_report.pipeline")
    logger.addHandler(handler)
    try:
        with pytest.raises(interrupt):
            go(w)
    finally:
        logger.removeHandler(handler)
    assert w.ledger.get_lock("client:example-co") is None and w.ledger.get_lock("runner:global") is None
    assert pipeline.LOG_CONTEXT.get() is None


def test_repr_of_the_main_objects_carries_no_secret(world):
    w = world()
    request = req()
    response = go(w, request)
    for obj in (w.settings, request, response, w.services.registry, ledger_module.LeaseKeeper):
        assert not any(secret in repr(obj) for secret in SECRETS)
    assert "token" not in repr(request).lower() or "token" in "auth_token"  # no credential field exists on a request
    assert not [f.name for f in dataclasses.fields(type(request)) if "secret" in f.name or "password" in f.name]
