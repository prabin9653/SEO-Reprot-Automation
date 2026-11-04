"""The sequential multi-client runner (R8.3).

`run_batch(requests, services=, ledger=)` processes requests one client at a time. There is no parallelism: the engine
has no cross-process lock, the snapshot database and the ledger are single-writer SQLite files, every render starts
its own Chromium and one OAuth identity shares one Google quota (docs/r8/production_architecture.md §8).

Isolation: `pipeline.run_client` never raises for ordinary operational problems, so one client's failure, rejection or
conflict never stops the others. The batch stops early only for conditions that would hit every remaining client:

  * `auth_required`      the reporting credentials need an operator (remaining requests stay unstarted)
  * `ledger_unavailable` the ledger cannot be used
  * `disk_full`          the disk is full
  * `batch_deadline`     the configured batch time has been used
  * `lock_unavailable`   another runner holds the runner-wide lock (nothing is started)

Requests that were not started are listed in the summary (`not_started`) and keep their `accepted` or unregistered state,
so the next poll picks them up. `KeyboardInterrupt` and `SystemExit` propagate after the lock is released.

`retry_due` builds the automatic retries (`retry` requests) for failed runs whose category allows it, whose retry time
has passed and whose attempt count is within `max_automatic_attempts`.

The summary (`BatchOutcome.summary()`) is a validated `jasper.batch-summary` object: counts and run responses only.
"""

import dataclasses
import datetime as dt
import json
import logging
import logging.handlers
import os
import re
from dataclasses import dataclass
from pathlib import Path

from seo_report import contracts
from seo_report import ledger as ledger_module
from seo_report import pipeline
from seo_report import runner_config as rc
from seo_report.contracts import ERROR_CATEGORIES, RunRequest

LOGGER = logging.getLogger("seo_report.runner")


# --- operational logging (R8.4.6) -------------------------------------------------------------------------------------------------
#
# One JSON object per line in a size-bounded local file. Every record is sanitized by a filter and a formatter before it is
# written: secrets are replaced, client folder paths are reduced to a file name, objects are never `repr()`ed, and tracebacks
# keep their frames and exception class but pass through the same text sanitizer. The run identifiers (client, period, run,
# request, stage) come from `pipeline.LOG_CONTEXT`, so records written by the ledger, authentication and rendering code carry
# them too. A failing log handler never raises into the caller (`SafeRotatingFileHandler`).

REDACTED = "[REDACTED]"
LOG_FILE_NAME = "seo_report.log"
LOG_MAX_BYTES = 5_000_000
LOG_BACKUPS = 5  # at most (backups + 1) x max_bytes = 30 MB of logs
MAX_TEXT = 4000
MAX_TRACEBACK = 8000
_HANDLER_TAG = "_seo_report_operational"
_KEY = r"(?:access_token|refresh_token|id_token|client_secret|client_id|password|passwd|pwd|api[_-]?key|private[_-]?key|secret|token|authorization|cookie|session)"
_SECRET_KEY = re.compile(r"secret|token|password|passwd|credential|authorization|api[_-]?key|private[_-]?key|cookie|bearer|session",
                         re.IGNORECASE)
_PATTERNS = (
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?:-----END [A-Z ]*PRIVATE KEY-----|$)", re.DOTALL), REDACTED),
    (re.compile(r"(?i)(?<![A-Za-z0-9_])((?:proxy-)?authorization)\s*[:=]\s*(?:(?:bearer|basic|digest|token)\s+)?[^\s,;]+"), r"\1: " + REDACTED),
    (re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+"), "Bearer " + REDACTED),
    (re.compile(r"(?i)(?<![A-Za-z0-9_])([\"']?" + _KEY + r"[\"']?\s*[:=]\s*)(?!\[REDACTED\])(\"[^\"]*\"|'[^']*'|[^\s,&;}\]\)]+)"), r"\1" + REDACTED),
    (re.compile(r"ya29\.[\w.-]+|1//[\w-]{10,}|GOCSPX-[\w-]+|AIza[0-9A-Za-z_-]{35}|gh[pousr]_[A-Za-z0-9]{20,}"), REDACTED),
    # any other long opaque string that mixes letters and digits (lowercase hex digests of 16/32/64 characters are diagnostics and stay)
    (re.compile(r"(?<![A-Za-z0-9_-])(?!(?:[0-9a-f]{16}|[0-9a-f]{32}|[0-9a-f]{64})(?![A-Za-z0-9_-]))(?=[A-Za-z0-9_-]*\d)(?=[A-Za-z0-9_-]*[A-Za-z])"
                r"[A-Za-z0-9_-]{40,}(?![A-Za-z0-9_-])"), REDACTED),
)
_PATH = re.compile(r"(?:[A-Za-z]:[\\/]+|\\\\)(?:[^\\/:*?\"<>|\r\n]+[\\/]+)*[^\s\\/:*?\"<>|]*")  # folder names may hold spaces
_CONTEXT_ORDER = ("run_key", "client_key", "period_id", "stage", "run_id", "request_id")


class LoggingConfigError(ValueError):
    """The log folder is not acceptable. `code` is fixed; the path is never included."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _short_path(match) -> str:
    parts = [p for p in re.split(r"[\\/]+", match.group(0)) if p]
    return "<path>/" + parts[-1] if parts else "<path>"


def sanitize_text(value, limit: int = MAX_TEXT) -> str:
    """Remove secrets and reduce file-system paths to their last component; bounded in length."""
    text = value if isinstance(value, str) else str(value)
    for pattern, replacement in _PATTERNS:
        text = pattern.sub(replacement, text)
    text = _PATH.sub(_short_path, text)
    return text if len(text) <= limit else text[:limit] + "...[truncated]"


def safe_value(value, depth: int = 0):
    """A JSON-safe, sanitized stand-in for any value. Never `repr()`s an arbitrary object: credential objects and other
    unknown types are reduced to their class name."""
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        return value if value == value and abs(value) != float("inf") else str(value)
    if isinstance(value, str):
        return sanitize_text(value)
    if isinstance(value, os.PathLike):
        return sanitize_text(os.fspath(value))
    if isinstance(value, BaseException):
        return f"{type(value).__name__}: {sanitize_text(value)}"
    if depth >= 4:
        return f"<{type(value).__name__}>"
    if isinstance(value, dict):
        return {str(k)[:64]: REDACTED if _SECRET_KEY.search(str(k)) else safe_value(v, depth + 1)
                for k, v in list(value.items())[:50]}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [safe_value(v, depth + 1) for v in list(value)[:50]]
    return f"<{type(value).__name__}>"


class SanitizingFilter(logging.Filter):
    """Makes the record safe in place; never raises and never drops a record."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            if isinstance(record.args, dict):
                record.args = safe_value(record.args)
            elif record.args:
                record.args = tuple(safe_value(a) for a in record.args)
            if not isinstance(record.msg, str):
                record.msg = f"<{type(record.msg).__name__}>"
            fields = getattr(record, "fields", None)
            record.fields = safe_value(fields) if isinstance(fields, dict) else {}
        except Exception:
            record.msg, record.args, record.fields = "<log record could not be sanitized>", None, {}
        return True


class JsonFormatter(logging.Formatter):
    """One sanitized JSON object per record with a fixed set of leading fields."""

    def format(self, record: logging.LogRecord) -> str:
        try:
            message = record.getMessage()
        except Exception:
            message = "<log message could not be formatted>"
        data = {"ts": dt.datetime.fromtimestamp(record.created, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f+00:00"),
                "level": record.levelname, "logger": record.name, "event": sanitize_text(message)}
        context = pipeline.LOG_CONTEXT.get() or {}
        for key in _CONTEXT_ORDER:
            if context.get(key) is not None:
                data[key] = safe_value(context[key])
        fields = dict(getattr(record, "fields", None) or {})  # a copy: the record may be formatted by several handlers
        for key in ("error_category", "error_code"):
            if key in fields:
                data[key] = fields.pop(key)
        if record.exc_info and record.exc_info[1] is not None:
            error = record.exc_info[1]
            data["exc_type"] = type(error).__name__
            for attribute, key in (("category", "error_category"), ("code", "error_code")):
                value = getattr(error, attribute, None)
                if isinstance(value, str) and key not in data:
                    data[key] = sanitize_text(value, 64)
            try:
                data["exc"] = sanitize_text(self.formatException(record.exc_info), MAX_TRACEBACK)
            except Exception:
                data["exc"] = "<traceback could not be formatted>"
        if fields:
            data["fields"] = fields
        return json.dumps(data, ensure_ascii=True, allow_nan=False, default=lambda o: f"<{type(o).__name__}>")


class SafeRotatingFileHandler(logging.handlers.RotatingFileHandler):
    """A size-bounded log file whose failures (disk full, locked file, rotation error) are counted, never raised."""

    failures = 0

    def emit(self, record) -> None:
        # `FileHandler.emit` opens the file outside the base class's error handling (delay=True): a folder that became unwritable after start-up
        # would otherwise raise into the running command. Any Exception is a counted logging failure; KeyboardInterrupt/SystemExit still propagate.
        try:
            super().emit(record)
        except Exception:
            self.handleError(record)

    def handleError(self, record) -> None:
        type(self).failures += 1  # silent on purpose: logging must not interrupt a run or its cleanup


def check_log_dir(log_dir=None, *, environ=None) -> Path:
    """The log folder that `configure_logging` would use, validated WITHOUT creating anything (raises `LoggingConfigError`)."""
    folder = Path(log_dir) if log_dir is not None else rc.default_local_base(environ) / "logs"
    if not folder.is_absolute():
        raise LoggingConfigError("log_dir_not_absolute")
    try:
        key = rc.canonical_key(folder)
    except rc.RunnerConfigError:
        raise LoggingConfigError("log_dir_ambiguous") from None
    if rc.keys_overlap(key, rc.canonical_key(rc.PROJECT_ROOT)):
        raise LoggingConfigError("log_dir_inside_project")
    if rc.in_cloud_sync_folder(folder, environ):
        raise LoggingConfigError("log_dir_in_cloud_sync")
    return folder


def configure_logging(log_dir=None, *, environ=None, level: int = logging.INFO, max_bytes: int = LOG_MAX_BYTES,
                      backups: int = LOG_BACKUPS) -> Path:
    """Send the `seo_report` loggers to a rotating, sanitized JSON-lines file and return its path. Safe to call again:
    the previous handler is replaced, never duplicated. The folder must be absolute, outside the project and outside
    OneDrive; the default is `%LOCALAPPDATA%\\JasperSEOReporting\\logs`."""
    folder = check_log_dir(log_dir, environ=environ)
    folder.mkdir(parents=True, exist_ok=True)
    shutdown_logging()
    handler = SafeRotatingFileHandler(folder / LOG_FILE_NAME, maxBytes=max_bytes, backupCount=backups, encoding="utf-8",
                                      delay=True)
    handler.addFilter(SanitizingFilter())
    handler.setFormatter(JsonFormatter())
    setattr(handler, _HANDLER_TAG, True)
    root = logging.getLogger("seo_report")
    root.addHandler(handler)
    root.setLevel(level)
    root.propagate = False  # one destination: no duplicate lines through the root logger
    return folder / LOG_FILE_NAME


def shutdown_logging() -> None:
    """Detach and close the handlers `configure_logging` added (idempotent)."""
    root = logging.getLogger("seo_report")
    for handler in list(root.handlers):
        if getattr(handler, _HANDLER_TAG, False):
            root.removeHandler(handler)
            handler.close()
            root.propagate = True


@dataclass(frozen=True)
class BatchOutcome:
    results: tuple
    aborted: bool
    abort_reason: str | None
    not_started: tuple  # request IDs
    started_at: str
    finished_at: str
    mode: str = "production"

    @property
    def counts(self) -> dict:
        counts = {name: 0 for name in contracts.COUNT_FIELDS}
        counts["total"] = len(self.results)
        for response in self.results:
            if response.replayed:
                counts["replayed"] += 1
            if response.outcome in counts:
                counts[response.outcome] += 1
        return counts

    def summary(self) -> dict:
        data = {"schema": contracts.BATCH_SCHEMA, "version": contracts.VERSION, "mode": self.mode,
                "started_at": self.started_at, "finished_at": self.finished_at, "aborted": self.aborted,
                "abort_reason": self.abort_reason, "counts": self.counts,
                "results": [r.to_dict() for r in self.results], "not_started": list(self.not_started)}
        return contracts.validate_batch_summary(data)


MAX_CONSECUTIVE_LEDGER_BUSY = 3  # clients in a row that found the ledger busy: it is not just a moment, stop the batch


def _storage_reason(error) -> str:
    """The batch abort reason for a database that cannot be used (a closed set: see `contracts.ABORT_REASONS`)."""
    return "disk_full" if error.code == "ledger_disk_full" else "ledger_unavailable"


def _abort_reason(response) -> str | None:
    error = response.error
    if error is None:
        return None
    if error["category"] == "auth_required":
        return "auth_required"
    if error["code"] == "disk_full":
        return "disk_full"
    return None


def run_batch(requests, *, services: pipeline.Services, ledger, max_clients: int | None = None) -> BatchOutcome:
    """Process `requests` (RunRequest objects) one after another. See the module docstring."""
    requests = list(requests)
    for item in requests:
        if not isinstance(item, RunRequest):
            raise TypeError("requests must be RunRequest objects")
    ordered = sorted(requests, key=lambda r: (r.requested_at, r.client_key, r.request_id))
    settings = services.settings
    started = services.clock()
    stamp = contracts.timestamp_of(started)
    results, aborted, reason, pending = [], False, None, list(ordered)

    def finish():
        return BatchOutcome(tuple(results), aborted, reason, tuple(r.request_id for r in pending), stamp,
                            contracts.timestamp_of(services.clock()))

    try:
        ledger.acquire_lock("runner:global", ttl_seconds=settings.lock_ttl_seconds)
    except ledger_module.LockHeldError:
        return BatchOutcome((), True, "lock_unavailable", tuple(r.request_id for r in ordered), stamp, stamp)
    except ledger_module.LedgerError as error:
        return BatchOutcome((), True, _storage_reason(error), tuple(r.request_id for r in ordered), stamp, stamp)
    batch_services = dataclasses.replace(services, global_lock_held=True)
    try:
        try:
            ledger.recover_interrupted()
        except ledger_module.LedgerError as error:
            aborted, reason = True, _storage_reason(error)
            return finish()
        limit = len(ordered) if max_clients is None else max(0, int(max_clients))
        processed, busy_streak = 0, 0
        while pending and processed < limit:
            if (services.clock() - started).total_seconds() > settings.batch_deadline_seconds:
                aborted, reason = True, "batch_deadline"
                break
            item = pending[0]
            try:
                response = pipeline.run_client(item.client_key, item.period_id, services=batch_services,
                                               ledger=ledger, request=item)
            except ledger_module.LedgerError as error:
                if error.storage and not error.transient:
                    pipeline._log(logging.ERROR, "the ledger cannot be used, the batch stops: %s", error.code,
                      extra={"fields": {"error_category": "internal_error", "error_code": error.code}})
                    aborted, reason = True, _storage_reason(error)
                    break
                # a logical ledger error that concerns this client only: it is reported, the others continue
                pipeline._log(logging.ERROR, "a ledger error ended one client's request: %s", error.code,
                          extra={"fields": {"error_category": "internal_error", "error_code": error.code}})
                response = pipeline.ledger_failure_response(item, services, error)
            pending.pop(0)
            processed += 1
            results.append(response)
            busy_streak = busy_streak + 1 if (response.error or {}).get("code") == "ledger_busy" else 0
            if busy_streak >= MAX_CONSECUTIVE_LEDGER_BUSY:
                aborted, reason = True, "ledger_unavailable"
                break
            stop = _abort_reason(response)
            if stop is not None:
                aborted, reason = True, stop
                break
            if pending and processed < limit and settings.inter_client_pause_seconds:
                services.sleep(settings.inter_client_pause_seconds)
        return finish()
    finally:
        try:
            ledger.release_lock("runner:global")
        except ledger_module.LedgerError:
            pipeline._log(logging.WARNING, "the runner lock could not be released")


def retry_requests_due(*, services: pipeline.Services, ledger) -> list[RunRequest]:
    """`retry` requests for failed runs that may be retried automatically now (see the module docstring)."""
    now = contracts.timestamp_of(services.clock())
    due = []
    for run in ledger.list_runs(("failed",)):
        eligible, _, automatic, _ = ERROR_CATEGORIES.get(run.error_category, (False, None, False, "failed"))
        if not (run.retry_eligible and eligible and automatic and run.retry_after and run.retry_after <= now
                and run.attempt_count <= services.settings.max_automatic_attempts):
            continue
        period_id = f"{run.period_start}_{run.period_end}"
        due.append(RunRequest(
            request_id=services.new_id(), idempotency_key=f"auto-retry:{run.run_key}:{run.attempt_count}",
            client_key=run.client_key, period_id=period_id, action="retry", requested_at=now,
            requested_by="runner:auto-retry"))
    return due


def retry_due(*, services: pipeline.Services, ledger) -> BatchOutcome:
    return run_batch(retry_requests_due(services=services, ledger=ledger), services=services, ledger=ledger)
