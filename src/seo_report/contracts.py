"""The machine-readable contracts of the production runner (R8.3), as designed in docs/r8/execution_contract.md.

Four closed JSON schemas, all version 1:

  jasper.run-request     what Make.com (or an operator) asks the runner to do
  jasper.run-response    the runner's status for one request / run (also the status webhook payload)
  jasper.delivery-ready  the event that a finalized, audited PDF is ready for delivery (published in a later stage)
  jasper.batch-summary   the aggregate result of one runner batch

Validation is strict: every member is required unless stated, unknown members are refused, types are exact (a bool is
not an integer, text is never coerced), identifiers and codes have fixed shapes, and nothing is trimmed or repaired.
Outputs carry fixed codes and identifiers only: no credentials, tokens, client names, e-mail addresses, file system
paths, exception messages or report content. `ContractError(code, field)` has a fixed code and names the field, never
the offending value.

The module also holds the run-state machine (`TRANSITIONS`), the error categories with their retry classification
(`ERROR_CATEGORIES`) and the helpers that build and parse the stable identifiers (`period_id`, `run_key`).
"""

import dataclasses
import datetime as dt
import re
from dataclasses import dataclass

from seo_report.client_config import ClientConfigError, validate_client_key
from seo_report.periods import PERIOD_DAYS

REQUEST_SCHEMA = "jasper.run-request"
RESPONSE_SCHEMA = "jasper.run-response"
DELIVERY_READY_SCHEMA = "jasper.delivery-ready"
BATCH_SCHEMA = "jasper.batch-summary"
VERSION = 1

ACTIONS = ("validate", "dry_run", "run", "retry", "publish", "status", "acknowledge", "cancel", "supersede")
# Operator actions change a run on a person's decision. Each must carry the operator's label and a reason code, both
# recorded in the ledger's append-only event history. There is deliberately no generic "force" action.
OPERATOR_ACTIONS = ("acknowledge", "cancel", "supersede")
# Reasons are a closed set so that no free text (and no client data) reaches the ledger or a status message.
REASON_CODES = ("blocking_condition_resolved", "partial_file_reviewed", "audit_finding_reviewed", "data_corrected",
                "content_error", "no_longer_needed", "duplicate_request", "operator_decision")
MODES = ("production", "dry_run", "read_only")
OUTCOMES = ("succeeded", "in_progress", "failed", "blocked", "rejected", "already_complete")

# --- states ---------------------------------------------------------------------------------------------------------------

PROGRESS_STATES = ("requested", "collecting", "rendering", "rendered", "finalizing", "finalized", "auditing", "audited",
                   "publishing", "ready_for_delivery", "delivered")
IN_PROGRESS_STATES = ("collecting", "rendering", "finalizing", "auditing", "publishing")
STATES = PROGRESS_STATES + ("failed", "blocked", "cancelled")
TERMINAL_STATES = ("delivered", "cancelled")
# Where a run resumes after an interruption or a retry: the earliest state whose work is not durable yet.
RESUME_STATE = {"collecting": "requested", "rendering": "requested", "finalizing": "rendered",
                "auditing": "finalized", "publishing": "audited"}
RESUME_STATES = ("requested", "rendered", "finalized", "audited")
STAGE_OF_STATE = {"collecting": "collecting", "rendering": "rendering", "finalizing": "finalizing",
                  "auditing": "auditing", "publishing": "publishing"}
STAGES = tuple(STAGE_OF_STATE.values()) + ("intake",)

_FORWARD = {"requested": "collecting", "collecting": "rendering", "rendering": "rendered", "rendered": "finalizing",
            "finalizing": "finalized", "finalized": "auditing", "auditing": "audited", "audited": "publishing",
            "publishing": "ready_for_delivery", "ready_for_delivery": "delivered"}


def _transitions() -> dict:
    allowed = {state: set() for state in STATES}
    for source, target in _FORWARD.items():
        allowed[source].add(target)
    for state in IN_PROGRESS_STATES:
        allowed[state] |= {"failed", "blocked", RESUME_STATE[state]}  # failure, or recovery after an interruption
    for state in ("failed", "blocked"):
        allowed[state] |= set(RESUME_STATES)  # an admitted retry starts a new attempt at a resume state
    for state in STATES:
        if state not in TERMINAL_STATES and state != "cancelled":
            allowed[state].add("cancelled")
    return {state: frozenset(targets) for state, targets in allowed.items()}


TRANSITIONS = _transitions()

# --- errors ---------------------------------------------------------------------------------------------------------------

# category -> (retry eligible, retry kind, automatic retry allowed, state a failure of this category ends in)
ERROR_CATEGORIES = {
    "validation_failed": (False, None, False, "failed"),
    "config_invalid": (False, None, False, "failed"),
    "auth_required": (True, "collection", False, "failed"),  # eligible once the operator has re-authenticated
    "source_temporary": (True, "collection", True, "failed"),
    "source_denied": (False, None, False, "failed"),
    "source_incompatible": (False, None, False, "failed"),
    "source_invalid": (False, None, False, "failed"),
    "render_failed": (True, "render", True, "failed"),
    "layout_issues": (False, None, False, "blocked"),
    "stale_bundle": (True, "collection", True, "failed"),
    "finalization_failed": (True, "finalization", True, "failed"),
    "finalization_conflict": (False, None, False, "blocked"),
    "audit_failed": (False, None, False, "blocked"),
    "publish_failed": (True, "publish", True, "failed"),
    "delivery_failed": (True, "delivery", False, "failed"),
    "lock_unavailable": (False, None, False, "failed"),
    "run_in_progress": (False, None, False, "failed"),
    "timeout": (True, "collection", True, "failed"),
    "internal_error": (False, None, False, "failed"),
}
RETRY_KINDS = ("collection", "render", "finalization", "publish", "delivery")
REJECT_CODES = ("run_in_progress", "no_run", "not_failed", "not_audited", "use_retry", "operator_required", "cancelled",
                "publish_not_supported", "period_differs_from_workbook", "client_unknown", "client_invalid",
                "action_not_supported", "not_blocked", "condition_not_recoverable", "blocking_condition_unresolved",
                "replace_existing_required", "report_exists", "replacement_pending", "original_changed",
                "operator_details_required", "not_cancellable", "not_supersedable")

_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_IDEMPOTENCY = re.compile(r"[A-Za-z0-9:._/-]{1,128}")
_CODE = re.compile(r"[a-z0-9_]{1,64}")
_REQUESTER = re.compile(r"[A-Za-z0-9:._-]{1,64}")
_TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\+00:00", re.ASCII)
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}", re.ASCII)
_PERIOD_ID = re.compile(r"(\d{4}-\d{2}-\d{2})_(\d{4}-\d{2}-\d{2})", re.ASCII)
_SHA256 = re.compile(r"[0-9a-f]{64}")
_HASH16 = re.compile(r"[0-9a-f]{16}")
_DRIVE_ID = re.compile(r"[A-Za-z0-9_-]{10,80}")
_FILE_NAME = re.compile(r"[A-Za-z0-9._-]{1,200}")


class ContractError(ValueError):
    """A message does not satisfy its contract. `code` is fixed; `field` names the member; no values."""

    def __init__(self, code: str, field: str = ""):
        super().__init__(f"{code}: {field}" if field else code)
        self.code = code
        self.field = field


def _fail(code, field):
    raise ContractError(code, field)


class StageError(Exception):
    """A pipeline stage failed in an expected, classified way (`category` from `ERROR_CATEGORIES`, a fixed `code`).

    Raised by the services and turned into a `failed`/`blocked` run by the pipeline; never carries client data."""

    def __init__(self, category: str, code: str):
        if category not in ERROR_CATEGORIES:
            raise ValueError("unknown error category")
        super().__init__(f"{category}: {code}")
        self.category = category
        self.code = code


# --- identifiers ----------------------------------------------------------------------------------------------------------

def _date(text, field) -> dt.date:
    if not isinstance(text, str) or not _DATE.fullmatch(text):
        _fail("date_invalid", field)
    try:
        return dt.date.fromisoformat(text)
    except ValueError:
        _fail("date_invalid", field)


def period_id_of(start: dt.date, end: dt.date) -> str:
    for name, value in (("period_start", start), ("period_end", end)):
        if not isinstance(value, dt.date) or isinstance(value, dt.datetime):
            _fail("date_invalid", name)
    return f"{start.isoformat()}_{end.isoformat()}"


def parse_period_id(text, field: str = "period_id") -> tuple[dt.date, dt.date]:
    """`YYYY-MM-DD_YYYY-MM-DD` -> (start, end); the period must have exactly `PERIOD_DAYS` days."""
    match = _PERIOD_ID.fullmatch(text) if isinstance(text, str) else None
    if match is None:
        _fail("period_id_invalid", field)
    start, end = _date(match.group(1), field), _date(match.group(2), field)
    if end < start or (end - start).days + 1 != PERIOD_DAYS:
        _fail("period_length_invalid", field)
    return start, end


def run_key_of(client_key: str, period_id: str, generation: int = 1) -> str:
    """`<client_key>/<period_id>` for the first run of a period; later runs of the same period (a replacement made by
    an operator, or a restart after a cancellation) add `~<generation>`: `<client_key>/<period_id>~2`."""
    if isinstance(generation, bool) or not isinstance(generation, int) or generation < 1:
        _fail("generation_invalid", "generation")
    return f"{client_key}/{period_id}" + ("" if generation == 1 else f"~{generation}")


def parse_run_key_full(text, field: str = "run_key") -> tuple[str, str, int]:
    """(client_key, period_id, generation) of a run key; generation 1 has no suffix."""
    if not isinstance(text, str) or text.count("/") != 1:
        _fail("run_key_invalid", field)
    client_key, rest = text.split("/")
    period_id, _, suffix = rest.partition("~")
    generation = 1
    if "~" in rest:
        if not re.fullmatch(r"[2-9]|[1-9][0-9]{1,2}", suffix):
            _fail("run_key_invalid", field)
        generation = int(suffix)
    _client_key(client_key, field)
    parse_period_id(period_id, field)
    return client_key, period_id, generation


def parse_run_key(text, field: str = "run_key") -> tuple[str, str]:
    client_key, period_id, _ = parse_run_key_full(text, field)
    return client_key, period_id


def _client_key(value, field) -> str:
    try:
        return validate_client_key(value, where=field)
    except ClientConfigError:
        _fail("client_key_invalid", field)


def timestamp_of(moment: dt.datetime) -> str:
    """UTC `YYYY-MM-DDTHH:MM:SS+00:00` (the engine's own timestamp format)."""
    if not isinstance(moment, dt.datetime) or moment.tzinfo is None or moment.utcoffset() != dt.timedelta(0):
        _fail("timestamp_invalid", "timestamp")
    return moment.astimezone(dt.timezone.utc).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%S+00:00")


def _timestamp(value, field) -> str:
    if not isinstance(value, str) or not _TIMESTAMP.fullmatch(value):
        _fail("timestamp_invalid", field)
    try:
        dt.datetime.fromisoformat(value)
    except ValueError:
        _fail("timestamp_invalid", field)
    return value


def _closed(obj, fields, field, optional=()):
    if not isinstance(obj, dict):
        _fail("object_expected", field)
    missing = [name for name in fields if name not in obj and name not in optional]
    if missing:
        _fail("member_missing", f"{field}.{missing[0]}" if field else missing[0])
    extra = sorted(name for name in obj if name not in fields)
    if extra:
        _fail("member_unknown", f"{field}.{extra[0]}" if field else extra[0])
    return obj


def _pattern(value, regex, code, field):
    if not isinstance(value, str) or not regex.fullmatch(value):
        _fail(code, field)
    return value


def _choice(value, allowed, field):
    if not isinstance(value, str) or value not in allowed:
        _fail("value_not_allowed", field)
    return value


def _bool(value, field):
    if not isinstance(value, bool):
        _fail("boolean_expected", field)
    return value


def _int(value, field, low=0, high=2**53):
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        _fail("integer_expected", field)
    return value


# --- request --------------------------------------------------------------------------------------------------------------

REQUEST_FIELDS = ("schema", "version", "request_id", "idempotency_key", "client_key", "period_id", "action",
                  "requested_at", "requested_by", "options")
OPTION_FIELDS = ("replace_existing", "operator", "reason")


@dataclass(frozen=True)
class RunRequest:
    request_id: str
    idempotency_key: str
    client_key: str
    period_id: str
    action: str
    requested_at: str
    requested_by: str
    replace_existing: bool = False
    operator: str | None = None  # the person who took an operator action (acknowledge, cancel, supersede)
    reason: str | None = None  # one of REASON_CODES

    @property
    def run_key(self) -> str:
        """The key of the FIRST run of this period; later runs of the period add a generation suffix."""
        return run_key_of(self.client_key, self.period_id)

    @property
    def period(self) -> tuple[dt.date, dt.date]:
        return parse_period_id(self.period_id)

    def to_dict(self) -> dict:
        options = {"replace_existing": self.replace_existing}
        if self.operator is not None:
            options["operator"] = self.operator
        if self.reason is not None:
            options["reason"] = self.reason
        return {"schema": REQUEST_SCHEMA, "version": VERSION, "request_id": self.request_id,
                "idempotency_key": self.idempotency_key, "client_key": self.client_key, "period_id": self.period_id,
                "action": self.action, "requested_at": self.requested_at, "requested_by": self.requested_by,
                "options": options}


def parse_request(obj) -> RunRequest:
    """Validate a `jasper.run-request` object (a JSON object or a Report Requests row mapped to these names)."""
    _closed(obj, REQUEST_FIELDS, "", optional=("options",))
    if obj["schema"] != REQUEST_SCHEMA or isinstance(obj["version"], bool) or obj["version"] != VERSION:
        _fail("schema_unsupported", "schema")
    options = obj.get("options", {})
    _closed(options, OPTION_FIELDS, "options", optional=OPTION_FIELDS)
    replace = _bool(options.get("replace_existing", False), "options.replace_existing")
    action = _choice(obj["action"], ACTIONS, "action")
    operator, reason = options.get("operator"), options.get("reason")
    if action in OPERATOR_ACTIONS:  # a person decided: who and why are mandatory
        if operator is None or reason is None:
            _fail("operator_details_required", "options")
        _pattern(operator, _REQUESTER, "operator_invalid", "options.operator")
        _choice(reason, REASON_CODES, "options.reason")
    elif operator is not None or reason is not None:
        _fail("operator_details_not_allowed", "options")
    client_key = _client_key(obj["client_key"], "client_key")
    period_id = obj["period_id"]
    parse_period_id(period_id)
    return RunRequest(
        request_id=_pattern(obj["request_id"], _UUID, "uuid_invalid", "request_id"),
        idempotency_key=_pattern(obj["idempotency_key"], _IDEMPOTENCY, "idempotency_key_invalid", "idempotency_key"),
        client_key=client_key, period_id=period_id, action=action,
        requested_at=_timestamp(obj["requested_at"], "requested_at"),
        requested_by=_pattern(obj["requested_by"], _REQUESTER, "requester_invalid", "requested_by"),
        replace_existing=replace, operator=operator, reason=reason)


# --- response -------------------------------------------------------------------------------------------------------------

IDENTITY_FIELDS = ("render_input_sha256", "render_input_format", "generation_date", "pdf_sha256", "pdf16", "pdf_bytes",
                   "record_name")
AUDIT_FIELDS = ("outcome", "finalized_valid", "verification_complete")
DELIVERY_FIELDS = ("drive_file_id", "drive_folder_id", "file_name", "delivery_key")
ERROR_FIELDS = ("category", "code", "stage")
RETRY_FIELDS = ("eligible", "kind", "after")
WARNING_FIELDS = ("code", "source")
TIMESTAMP_FIELDS = ("requested_at", "updated_at", "finished_at")
RESPONSE_FIELDS = ("schema", "version", "request_id", "idempotency_key", "replayed", "mode", "run_key", "client_key",
                   "period_id", "state", "outcome", "attempt", "run_id", "revision", "identity", "audit", "delivery",
                   "error", "retry", "warnings", "timestamps")
# Present only when they carry information: the generation of a run that is not the first of its period, the run it
# replaces, and the operator action the response reports. Messages without them are exactly the original schema.
OPTIONAL_RESPONSE_FIELDS = ("generation", "supersedes", "operator_action")
OPERATOR_ACTION_FIELDS = ("action", "operator", "reason")


@dataclass(frozen=True)
class RunResponse:
    request_id: str
    idempotency_key: str
    mode: str
    run_key: str
    client_key: str
    period_id: str
    state: str
    outcome: str
    attempt: int
    timestamps: dict
    replayed: bool = False
    run_id: str | None = None
    revision: int | None = None
    identity: dict | None = None
    audit: dict | None = None
    delivery: dict | None = None
    error: dict | None = None
    retry: dict = dataclasses.field(default_factory=lambda: {"eligible": False, "kind": None, "after": None})
    warnings: tuple = ()
    generation: int = 1
    supersedes: str | None = None  # the run_key of the run this one replaces or restarts
    operator_action: dict | None = None

    def to_dict(self) -> dict:
        data = {"schema": RESPONSE_SCHEMA, "version": VERSION}
        for field in dataclasses.fields(self):
            value = getattr(self, field.name)
            if field.name in OPTIONAL_RESPONSE_FIELDS:
                if value in (None, 1):
                    continue
            data[field.name] = [dict(w) for w in value] if field.name == "warnings" else value
        return data

    @property
    def ok(self) -> bool:
        return self.outcome in ("succeeded", "already_complete")


def error_info(category: str, code: str, stage: str) -> dict:
    if category not in ERROR_CATEGORIES:
        _fail("value_not_allowed", "error.category")
    _pattern(code, _CODE, "code_invalid", "error.code")
    _choice(stage, STAGES, "error.stage")
    return {"category": category, "code": code, "stage": stage}


def retry_info(category: str | None, *, after: str | None = None) -> dict:
    if category is None:
        return {"eligible": False, "kind": None, "after": None}
    eligible, kind, _, _ = ERROR_CATEGORIES[category]
    return {"eligible": eligible, "kind": kind, "after": after if eligible else None}


def validate_response(obj) -> dict:
    """Strict validation of a `jasper.run-response` object; returns it unchanged."""
    _closed(obj, RESPONSE_FIELDS + OPTIONAL_RESPONSE_FIELDS, "", optional=OPTIONAL_RESPONSE_FIELDS)
    if obj["schema"] != RESPONSE_SCHEMA or isinstance(obj["version"], bool) or obj["version"] != VERSION:
        _fail("schema_unsupported", "schema")
    _pattern(obj["request_id"], _UUID, "uuid_invalid", "request_id")
    _pattern(obj["idempotency_key"], _IDEMPOTENCY, "idempotency_key_invalid", "idempotency_key")
    _bool(obj["replayed"], "replayed")
    _choice(obj["mode"], MODES, "mode")
    client_key, period_id, generation = parse_run_key_full(obj["run_key"])
    if obj["client_key"] != client_key or obj["period_id"] != period_id:
        _fail("identity_inconsistent", "run_key")
    if obj.get("generation", 1) != generation or isinstance(obj.get("generation", 1), bool):
        _fail("identity_inconsistent", "generation")
    if obj.get("supersedes") is not None:
        other_client, other_period, other_generation = parse_run_key_full(obj["supersedes"], "supersedes")
        if (other_client, other_period) != (client_key, period_id) or other_generation >= generation:
            _fail("identity_inconsistent", "supersedes")
    elif generation > 1 and "supersedes" in obj:
        _fail("identity_inconsistent", "supersedes")
    if obj.get("operator_action") is not None:
        action = _closed(obj["operator_action"], OPERATOR_ACTION_FIELDS, "operator_action")
        _choice(action["action"], OPERATOR_ACTIONS, "operator_action.action")
        _pattern(action["operator"], _REQUESTER, "operator_invalid", "operator_action.operator")
        _choice(action["reason"], REASON_CODES, "operator_action.reason")
    _choice(obj["state"], STATES, "state")
    _choice(obj["outcome"], OUTCOMES, "outcome")
    _int(obj["attempt"], "attempt", 1)
    if obj["run_id"] is not None:
        _pattern(obj["run_id"], _UUID, "uuid_invalid", "run_id")
    if obj["revision"] is not None:
        _int(obj["revision"], "revision", 1)
    if obj["identity"] is not None:
        identity = _closed(obj["identity"], IDENTITY_FIELDS, "identity")
        _pattern(identity["render_input_sha256"], _SHA256, "hash_invalid", "identity.render_input_sha256")
        fmt = _closed(identity["render_input_format"], ("name", "version"), "identity.render_input_format")
        _pattern(fmt["name"], re.compile(r"[a-z0-9.-]{1,64}"), "format_invalid", "identity.render_input_format.name")
        _int(fmt["version"], "identity.render_input_format.version", 1, 1000)
        _date(identity["generation_date"], "identity.generation_date")
        if identity["pdf_sha256"] is not None:
            _pattern(identity["pdf_sha256"], _SHA256, "hash_invalid", "identity.pdf_sha256")
            if identity["pdf16"] != identity["pdf_sha256"][:16]:
                _fail("identity_inconsistent", "identity.pdf16")
            _int(identity["pdf_bytes"], "identity.pdf_bytes", 1)
        elif identity["pdf16"] is not None or identity["pdf_bytes"] is not None:
            _fail("identity_inconsistent", "identity.pdf_sha256")
        if identity["record_name"] is not None:
            _pattern(identity["record_name"], re.compile(r"reviewer\.[0-9a-f-]{36}\.[0-9a-f]{16}\.json"),
                     "record_name_invalid", "identity.record_name")
    if obj["audit"] is not None:
        audit = _closed(obj["audit"], AUDIT_FIELDS, "audit")
        _pattern(audit["outcome"], _CODE, "code_invalid", "audit.outcome")
        _int(audit["finalized_valid"], "audit.finalized_valid")
        _bool(audit["verification_complete"], "audit.verification_complete")
    if obj["delivery"] is not None:
        delivery = _closed(obj["delivery"], DELIVERY_FIELDS, "delivery")
        _pattern(delivery["drive_file_id"], _DRIVE_ID, "drive_id_invalid", "delivery.drive_file_id")
        _pattern(delivery["drive_folder_id"], _DRIVE_ID, "drive_id_invalid", "delivery.drive_folder_id")
        _pattern(delivery["file_name"], _FILE_NAME, "file_name_invalid", "delivery.file_name")
        _pattern(delivery["delivery_key"], _IDEMPOTENCY, "delivery_key_invalid", "delivery.delivery_key")
        if obj["state"] not in ("ready_for_delivery", "delivered"):
            _fail("delivery_not_ready", "delivery")  # a delivery block exists only once the report is ready
    if obj["error"] is not None:
        error = _closed(obj["error"], ERROR_FIELDS, "error")
        _choice(error["category"], tuple(ERROR_CATEGORIES), "error.category")
        _pattern(error["code"], _CODE, "code_invalid", "error.code")
        _choice(error["stage"], STAGES, "error.stage")
    retry = _closed(obj["retry"], RETRY_FIELDS, "retry")
    _bool(retry["eligible"], "retry.eligible")
    if retry["kind"] is not None:
        _choice(retry["kind"], RETRY_KINDS, "retry.kind")
    if retry["after"] is not None:
        _timestamp(retry["after"], "retry.after")
    if not isinstance(obj["warnings"], list):
        _fail("array_expected", "warnings")
    for position, warning in enumerate(obj["warnings"]):
        item = _closed(warning, WARNING_FIELDS, f"warnings[{position}]")
        _pattern(item["code"], _CODE, "code_invalid", f"warnings[{position}].code")
        _choice(item["source"], ("gsc", "ga4", "pipeline"), f"warnings[{position}].source")
    stamps = _closed(obj["timestamps"], TIMESTAMP_FIELDS, "timestamps")
    _timestamp(stamps["requested_at"], "timestamps.requested_at")
    _timestamp(stamps["updated_at"], "timestamps.updated_at")
    if stamps["finished_at"] is not None:
        _timestamp(stamps["finished_at"], "timestamps.finished_at")
    # delivery readiness is never claimed without a passing audit
    if obj["state"] in ("ready_for_delivery", "delivered") and not (
            obj["audit"] is not None and obj["audit"]["outcome"] == "consistent"
            and obj["audit"]["verification_complete"] is True and obj["audit"]["finalized_valid"] >= 1):
        _fail("delivery_without_audit", "state")
    return obj


# --- delivery-ready -------------------------------------------------------------------------------------------------------

DELIVERY_READY_FIELDS = ("schema", "version", "run_key", "client_key", "period_id", "delivery_key", "pdf_sha256",
                         "pdf_bytes", "drive_file_id", "drive_folder_id", "file_name", "audit", "uploaded_at",
                         "manifest_sha256")


def build_delivery_ready(*, run_key, delivery_key, pdf_sha256, pdf_bytes, drive_file_id, drive_folder_id, file_name,
                         audit: dict, uploaded_at, manifest_sha256) -> dict:
    client_key, period_id = parse_run_key(run_key)
    message = {"schema": DELIVERY_READY_SCHEMA, "version": VERSION, "run_key": run_key, "client_key": client_key,
               "period_id": period_id, "delivery_key": delivery_key, "pdf_sha256": pdf_sha256, "pdf_bytes": pdf_bytes,
               "drive_file_id": drive_file_id, "drive_folder_id": drive_folder_id, "file_name": file_name,
               "audit": dict(audit), "uploaded_at": uploaded_at, "manifest_sha256": manifest_sha256}
    return validate_delivery_ready(message)


def validate_delivery_ready(obj) -> dict:
    """Strict validation of a `jasper.delivery-ready` object. It can only describe a passed audit."""
    _closed(obj, DELIVERY_READY_FIELDS, "")
    if obj["schema"] != DELIVERY_READY_SCHEMA or isinstance(obj["version"], bool) or obj["version"] != VERSION:
        _fail("schema_unsupported", "schema")
    client_key, period_id = parse_run_key(obj["run_key"])
    if obj["client_key"] != client_key or obj["period_id"] != period_id:
        _fail("identity_inconsistent", "run_key")
    _pattern(obj["pdf_sha256"], _SHA256, "hash_invalid", "pdf_sha256")
    _int(obj["pdf_bytes"], "pdf_bytes", 1)
    parts = obj["delivery_key"].split("/") if isinstance(obj["delivery_key"], str) else []
    if len(parts) != 4 or "/".join(parts[:2]) != obj["run_key"] or parts[2] != obj["pdf_sha256"][:16] \
            or not _HASH16.fullmatch(parts[3]):
        _fail("delivery_key_invalid", "delivery_key")
    _pattern(obj["drive_file_id"], _DRIVE_ID, "drive_id_invalid", "drive_file_id")
    _pattern(obj["drive_folder_id"], _DRIVE_ID, "drive_id_invalid", "drive_folder_id")
    _pattern(obj["file_name"], _FILE_NAME, "file_name_invalid", "file_name")
    audit = _closed(obj["audit"], AUDIT_FIELDS, "audit")
    if audit["outcome"] != "consistent" or audit["verification_complete"] is not True \
            or isinstance(audit["finalized_valid"], bool) or not isinstance(audit["finalized_valid"], int) \
            or audit["finalized_valid"] < 1:
        _fail("audit_not_passed", "audit")
    _timestamp(obj["uploaded_at"], "uploaded_at")
    _pattern(obj["manifest_sha256"], _SHA256, "hash_invalid", "manifest_sha256")
    return obj


# --- batch summary --------------------------------------------------------------------------------------------------------

BATCH_FIELDS = ("schema", "version", "mode", "started_at", "finished_at", "aborted", "abort_reason", "counts",
                "results", "not_started")
COUNT_FIELDS = ("total", "succeeded", "failed", "blocked", "rejected", "already_complete", "replayed")
ABORT_REASONS = ("auth_required", "ledger_unavailable", "disk_full", "batch_deadline", "lock_unavailable")


def validate_batch_summary(obj) -> dict:
    _closed(obj, BATCH_FIELDS, "")
    if obj["schema"] != BATCH_SCHEMA or isinstance(obj["version"], bool) or obj["version"] != VERSION:
        _fail("schema_unsupported", "schema")
    _choice(obj["mode"], MODES, "mode")
    _timestamp(obj["started_at"], "started_at")
    _timestamp(obj["finished_at"], "finished_at")
    _bool(obj["aborted"], "aborted")
    if obj["abort_reason"] is not None:
        _choice(obj["abort_reason"], ABORT_REASONS, "abort_reason")
    if obj["aborted"] != (obj["abort_reason"] is not None):
        _fail("identity_inconsistent", "abort_reason")
    counts = _closed(obj["counts"], COUNT_FIELDS, "counts")
    for name in COUNT_FIELDS:
        _int(counts[name], f"counts.{name}")
    if not isinstance(obj["results"], list) or not isinstance(obj["not_started"], list):
        _fail("array_expected", "results")
    for response in obj["results"]:
        validate_response(response)
    for request_id in obj["not_started"]:
        _pattern(request_id, _UUID, "uuid_invalid", "not_started")
    return obj
