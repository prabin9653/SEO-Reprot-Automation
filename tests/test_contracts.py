"""R8.3: the request, response, delivery-ready and batch-summary contracts, the state machine and the error table."""
import copy
import datetime as dt
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from seo_report import contracts as c

PERIOD = "2026-10-01_2026-10-28"
SHA = "9d8c7b6a5f4e3d2c1b0a9f8e7d6c5b4a39281706f5e4d3c2b1a09f8e7d6c5b4a"
RENDER = "3f1c0d2e9a7b4c5d6e8f0a1b2c3d4e5f60718293a4b5c6d7e8f9012a3b4c5d6e"
RUN_ID = "0198c1a2-7b3c-7d4e-8f5a-000000001000"
STAMP = "2026-11-02T01:07:41+00:00"


def request_dict(**changes):
    value = {"schema": "jasper.run-request", "version": 1, "request_id": "0b9a6a52-6c1e-4f0e-9a43-6d3c6e2b9a01",
             "idempotency_key": f"monthly:{PERIOD}:example-co:run", "client_key": "example-co", "period_id": PERIOD,
             "action": "run", "requested_at": "2026-11-02T01:00:00+00:00", "requested_by": "make:monthly-orchestration",
             "options": {"replace_existing": False}}
    value.update(changes)
    return value


def response_dict(**changes):
    value = {
        "schema": "jasper.run-response", "version": 1, "request_id": "0b9a6a52-6c1e-4f0e-9a43-6d3c6e2b9a01",
        "idempotency_key": f"monthly:{PERIOD}:example-co:run", "replayed": False, "mode": "production",
        "run_key": f"example-co/{PERIOD}", "client_key": "example-co", "period_id": PERIOD, "state": "audited",
        "outcome": "succeeded", "attempt": 1, "run_id": RUN_ID, "revision": 1,
        "identity": {"render_input_sha256": RENDER, "render_input_format": {"name": "jasper.render-input", "version": 2},
                     "generation_date": "2026-11-02", "pdf_sha256": SHA, "pdf16": SHA[:16], "pdf_bytes": 118765,
                     "record_name": f"reviewer.{RUN_ID}.{SHA[:16]}.json"},
        "audit": {"outcome": "consistent", "finalized_valid": 1, "verification_complete": True},
        "delivery": None, "error": None, "retry": {"eligible": False, "kind": None, "after": None},
        "warnings": [{"code": "source_incomplete", "source": "ga4"}],
        "timestamps": {"requested_at": "2026-11-02T01:00:00+00:00", "updated_at": STAMP, "finished_at": None}}
    value.update(changes)
    return value


def delivery_ready(**changes):
    value = {"schema": "jasper.delivery-ready", "version": 1, "run_key": f"example-co/{PERIOD}", "client_key": "example-co",
             "period_id": PERIOD, "delivery_key": f"example-co/{PERIOD}/{SHA[:16]}/5a1b2c3d4e5f6a7b", "pdf_sha256": SHA,
             "pdf_bytes": 118765, "drive_file_id": "1AbCdEfGhIjKlMnOpQrStUvWxYz012345",
             "drive_folder_id": "1ZyXwVuTsRqPoNmLkJiHgFeDcBa987654", "file_name": f"example-co_{PERIOD}_seo-report_{SHA[:16]}.pdf",
             "audit": {"outcome": "consistent", "finalized_valid": 1, "verification_complete": True},
             "uploaded_at": STAMP, "manifest_sha256": RENDER}
    value.update(changes)
    return value


def code_of(function, *args):
    with pytest.raises(c.ContractError) as info:
        function(*args)
    return info.value.code


# --- identifiers ---------------------------------------------------------------------------------------------------------------

def test_period_ids_round_trip_and_must_span_exactly_28_days():
    start, end = c.parse_period_id(PERIOD)
    assert (start, end) == (dt.date(2026, 10, 1), dt.date(2026, 10, 28)) and c.period_id_of(start, end) == PERIOD
    assert c.run_key_of("example-co", PERIOD) == f"example-co/{PERIOD}"
    assert c.parse_run_key(f"example-co/{PERIOD}") == ("example-co", PERIOD)


@pytest.mark.parametrize("text", ["2026-10-01_2026-10-27", "2026-10-01_2026-10-29", "2026-10-28_2026-10-01",
                                  "2026-13-01_2026-13-28", "2026-10-1_2026-10-28", "2026-10-01-2026-10-28", "", None, 5,
                                  "2026-10-01_2026-10-28 ", "2026-02-30_2026-03-27"])
def test_invalid_period_ids_are_refused(text):
    with pytest.raises(c.ContractError):
        c.parse_period_id(text)


@pytest.mark.parametrize("text", ["example-co", "example-co/", "/2026-10-01_2026-10-28", "Bad Key/" + PERIOD,
                                  "a/b/" + PERIOD, None])
def test_invalid_run_keys_are_refused(text):
    with pytest.raises(c.ContractError):
        c.parse_run_key(text)


def test_timestamps_are_utc_with_seconds_precision():
    moment = dt.datetime(2026, 11, 2, 1, 2, 3, 999999, tzinfo=dt.timezone.utc)
    assert c.timestamp_of(moment) == "2026-11-02T01:02:03+00:00"
    for bad in (dt.datetime(2026, 11, 2), dt.datetime(2026, 11, 2, tzinfo=dt.timezone(dt.timedelta(hours=2))), "x"):
        with pytest.raises(c.ContractError):
            c.timestamp_of(bad)


# --- request ---------------------------------------------------------------------------------------------------------------------

def test_a_valid_request_is_parsed_and_round_trips():
    request = c.parse_request(request_dict())
    assert (request.client_key, request.action, request.replace_existing) == ("example-co", "run", False)
    assert request.run_key == f"example-co/{PERIOD}" and request.period[0] == dt.date(2026, 10, 1)
    assert c.parse_request(request.to_dict()) == request
    assert c.parse_request(request_dict(options={"replace_existing": True})).replace_existing is True


def test_options_are_optional():
    data = request_dict()
    del data["options"]
    assert c.parse_request(data).replace_existing is False


@pytest.mark.parametrize("member", ["schema", "version", "request_id", "idempotency_key", "client_key", "period_id",
                                    "action", "requested_at", "requested_by"])
def test_every_request_member_is_required(member):
    data = request_dict()
    del data[member]
    assert code_of(c.parse_request, data) == "member_missing"


@pytest.mark.parametrize("changes,code", [
    ({"extra": 1}, "member_unknown"), ({"schema": "other"}, "schema_unsupported"), ({"version": 2}, "schema_unsupported"),
    ({"version": True}, "schema_unsupported"), ({"request_id": "0B9A6A52-6C1E-4F0E-9A43-6D3C6E2B9A01"}, "uuid_invalid"),
    ({"request_id": "not-a-uuid"}, "uuid_invalid"), ({"idempotency_key": ""}, "idempotency_key_invalid"),
    ({"idempotency_key": "a b"}, "idempotency_key_invalid"), ({"idempotency_key": "x" * 129}, "idempotency_key_invalid"),
    ({"client_key": "Bad Key"}, "client_key_invalid"), ({"client_key": "x" * 64}, "client_key_invalid"),
    ({"client_key": 5}, "client_key_invalid"), ({"period_id": "2026-10-01_2026-10-27"}, "period_length_invalid"),
    ({"action": "delete"}, "value_not_allowed"), ({"action": None}, "value_not_allowed"),
    ({"requested_at": "2026-11-02T01:00:00Z"}, "timestamp_invalid"), ({"requested_at": "2026-11-02"}, "timestamp_invalid"),
    ({"requested_by": "person@example.test"}, "requester_invalid"), ({"requested_by": ""}, "requester_invalid"),
    ({"options": {"replace_existing": "yes"}}, "boolean_expected"), ({"options": {"extra": True}}, "member_unknown"),
    ({"options": []}, "object_expected")])
def test_invalid_requests_are_refused_with_a_fixed_code(changes, code):
    assert code_of(c.parse_request, request_dict(**changes)) == code


def test_a_request_must_be_an_object():
    assert code_of(c.parse_request, [request_dict()]) == "object_expected"


def test_errors_name_the_field_and_never_the_value():
    with pytest.raises(c.ContractError) as info:
        c.parse_request(request_dict(requested_by="secret@example.test"))
    assert info.value.field == "requested_by" and "secret" not in str(info.value)


# --- response ---------------------------------------------------------------------------------------------------------------------

def test_the_documented_responses_validate():
    assert c.validate_response(response_dict())["state"] == "audited"
    failed = response_dict(state="failed", outcome="failed", run_id=None, revision=None, identity=None, audit=None,
                           error={"category": "source_temporary", "code": "gsc_http_503", "stage": "collecting"},
                           retry={"eligible": True, "kind": "collection", "after": STAMP})
    c.validate_response(failed)
    blocked = response_dict(state="blocked", outcome="blocked", audit=None,
                            error={"category": "finalization_conflict", "code": "existing_report_differs",
                                   "stage": "finalizing"})
    c.validate_response(blocked)
    rejected = response_dict(state="requested", outcome="rejected", run_id=None, revision=None, identity=None, audit=None,
                             error={"category": "validation_failed", "code": "period_differs_from_workbook", "stage": "intake"})
    c.validate_response(rejected)


def test_a_delivery_block_and_readiness_are_only_valid_with_a_passed_audit():
    delivery = {"drive_file_id": "1AbCdEfGhIjKlMnOpQrStUvWxYz012345", "drive_folder_id": "1ZyXwVuTsRqPoNmLkJiHgFeDcBa987654",
                "file_name": "a.pdf", "delivery_key": f"example-co/{PERIOD}/{SHA[:16]}/5a1b2c3d4e5f6a7b"}
    ready = response_dict(state="ready_for_delivery", delivery=delivery)
    assert c.validate_response(ready)["state"] == "ready_for_delivery"
    assert code_of(c.validate_response, response_dict(state="audited", delivery=delivery)) == "delivery_not_ready"
    for audit in (None, {"outcome": "attention_needed", "finalized_valid": 1, "verification_complete": True},
                  {"outcome": "consistent", "finalized_valid": 0, "verification_complete": True},
                  {"outcome": "consistent", "finalized_valid": 1, "verification_complete": False}):
        assert code_of(c.validate_response, response_dict(state="ready_for_delivery", audit=audit, delivery=delivery)) \
            == "delivery_without_audit"
        assert code_of(c.validate_response, response_dict(state="delivered", audit=audit, delivery=delivery)) \
            == "delivery_without_audit"


@pytest.mark.parametrize("changes,code", [
    ({"extra": 1}, "member_unknown"), ({"mode": "staging"}, "value_not_allowed"), ({"state": "done"}, "value_not_allowed"),
    ({"outcome": "ok"}, "value_not_allowed"), ({"attempt": 0}, "integer_expected"), ({"attempt": True}, "integer_expected"),
    ({"run_key": "other/" + PERIOD}, "identity_inconsistent"), ({"run_id": "x"}, "uuid_invalid"),
    ({"revision": 0}, "integer_expected"), ({"replayed": "no"}, "boolean_expected"),
    ({"warnings": [{"code": "x", "source": "bing"}]}, "value_not_allowed"), ({"warnings": {}}, "array_expected"),
    ({"warnings": [{"code": "Bad Code", "source": "gsc"}]}, "code_invalid"),
    ({"timestamps": {"requested_at": "x", "updated_at": STAMP, "finished_at": None}}, "timestamp_invalid"),
    ({"error": {"category": "made_up", "code": "x", "stage": "intake"}}, "value_not_allowed"),
    ({"error": {"category": "source_denied", "code": "has space", "stage": "intake"}}, "code_invalid"),
    ({"retry": {"eligible": True, "kind": "teleport", "after": None}}, "value_not_allowed")])
def test_invalid_responses_are_refused(changes, code):
    assert code_of(c.validate_response, response_dict(**changes)) == code


def test_identity_members_must_agree():
    data = response_dict()
    data["identity"]["pdf16"] = "0" * 16
    assert code_of(c.validate_response, data) == "identity_inconsistent"
    data = response_dict()
    data["identity"]["render_input_sha256"] = RENDER.upper()
    assert code_of(c.validate_response, data) == "hash_invalid"


def test_a_response_never_has_members_for_secrets_or_client_data():
    text = json.dumps(response_dict())
    for word in ("email", "token", "secret", "password", "path", "client_name", "website", "recipient"):
        assert word not in text.lower()


def test_run_response_objects_serialize_to_the_closed_schema():
    from seo_report import pipeline
    request = c.parse_request(request_dict())
    response = pipeline.build_response(request, None, outcome="rejected", updated_at=STAMP,
                                       error=c.error_info("validation_failed", "no_run", "intake"))
    assert set(response.to_dict()) == set(c.RESPONSE_FIELDS)
    c.validate_response(response.to_dict())
    assert pipeline.response_from_dict(response.to_dict(), replayed=True).replayed is True


# --- delivery-ready ---------------------------------------------------------------------------------------------------------------

def test_a_delivery_ready_message_validates_and_can_be_built():
    assert c.validate_delivery_ready(delivery_ready())["schema"] == "jasper.delivery-ready"
    message = c.build_delivery_ready(
        run_key=f"example-co/{PERIOD}", delivery_key=delivery_ready()["delivery_key"], pdf_sha256=SHA, pdf_bytes=1,
        drive_file_id="1AbCdEfGhIjKlMnOpQrStUvWxYz012345", drive_folder_id="1ZyXwVuTsRqPoNmLkJiHgFeDcBa987654",
        file_name="a.pdf", audit=delivery_ready()["audit"], uploaded_at=STAMP, manifest_sha256=RENDER)
    assert message["client_key"] == "example-co"


@pytest.mark.parametrize("changes,code", [
    ({"audit": {"outcome": "attention_needed", "finalized_valid": 1, "verification_complete": True}}, "audit_not_passed"),
    ({"audit": {"outcome": "consistent", "finalized_valid": 0, "verification_complete": True}}, "audit_not_passed"),
    ({"audit": {"outcome": "consistent", "finalized_valid": 1, "verification_complete": False}}, "audit_not_passed"),
    ({"audit": {"outcome": "consistent", "finalized_valid": True, "verification_complete": True}}, "audit_not_passed"),
    ({"delivery_key": f"example-co/{PERIOD}/{'0' * 16}/5a1b2c3d4e5f6a7b"}, "delivery_key_invalid"),
    ({"delivery_key": f"other/{PERIOD}/{SHA[:16]}/5a1b2c3d4e5f6a7b"}, "delivery_key_invalid"),
    ({"pdf_sha256": "x"}, "hash_invalid"), ({"pdf_bytes": 0}, "integer_expected"), ({"extra": 1}, "member_unknown"),
    ({"client_key": "other"}, "identity_inconsistent"), ({"file_name": "a b.pdf"}, "file_name_invalid"),
    ({"drive_file_id": "short"}, "drive_id_invalid")])
def test_invalid_delivery_ready_messages_are_refused(changes, code):
    assert code_of(c.validate_delivery_ready, delivery_ready(**changes)) == code


# --- batch summary ----------------------------------------------------------------------------------------------------------------

def test_a_batch_summary_validates():
    summary = {"schema": "jasper.batch-summary", "version": 1, "mode": "production", "started_at": STAMP,
               "finished_at": STAMP, "aborted": True, "abort_reason": "auth_required",
               "counts": {name: 0 for name in c.COUNT_FIELDS}, "results": [response_dict()],
               "not_started": ["0b9a6a52-6c1e-4f0e-9a43-6d3c6e2b9a02"]}
    assert c.validate_batch_summary(summary)["aborted"] is True
    bad = copy.deepcopy(summary)
    bad["abort_reason"] = None
    assert code_of(c.validate_batch_summary, bad) == "identity_inconsistent"
    bad = copy.deepcopy(summary)
    bad["abort_reason"] = "boredom"
    assert code_of(c.validate_batch_summary, bad) == "value_not_allowed"


# --- state machine and error table ----------------------------------------------------------------------------------------------------

def test_the_forward_chain_is_allowed_and_nothing_can_be_skipped():
    chain = c.PROGRESS_STATES
    for here, there in zip(chain, chain[1:]):
        assert there in c.TRANSITIONS[here]
    assert "rendered" not in c.TRANSITIONS["requested"] and "audited" not in c.TRANSITIONS["finalized"]
    assert "ready_for_delivery" not in c.TRANSITIONS["finalized"] and "ready_for_delivery" not in c.TRANSITIONS["auditing"]
    assert c.TRANSITIONS["audited"] == {"publishing", "cancelled"}


def test_failure_recovery_and_retry_transitions():
    for state in c.IN_PROGRESS_STATES:
        assert {"failed", "blocked", c.RESUME_STATE[state]} <= c.TRANSITIONS[state]
        assert c.RESUME_STATE[state] in c.STATES
    for state in ("failed", "blocked"):
        assert set(c.RESUME_STATES) <= c.TRANSITIONS[state]
    assert c.TRANSITIONS["delivered"] == frozenset() and c.TRANSITIONS["cancelled"] == frozenset()
    assert all(t in c.STATES for targets in c.TRANSITIONS.values() for t in targets)


def test_every_error_category_has_a_consistent_retry_classification():
    for category, (eligible, kind, automatic, final_state) in c.ERROR_CATEGORIES.items():
        assert final_state in ("failed", "blocked")
        assert (kind is None) == (not eligible) and (not automatic or eligible)
        assert kind is None or kind in c.RETRY_KINDS
    assert c.ERROR_CATEGORIES["finalization_conflict"][3] == "blocked" and c.ERROR_CATEGORIES["audit_failed"][3] == "blocked"
    assert c.ERROR_CATEGORIES["auth_required"][2] is False  # never retried automatically
    assert c.ERROR_CATEGORIES["source_temporary"][:3] == (True, "collection", True)
    assert c.ERROR_CATEGORIES["delivery_failed"][1] == "delivery" and c.ERROR_CATEGORIES["publish_failed"][1] == "publish"


def test_stage_errors_need_a_known_category():
    error = c.StageError("source_denied", "gsc_http_403")
    assert (error.category, error.code) == ("source_denied", "gsc_http_403")
    with pytest.raises(ValueError):
        c.StageError("made_up", "x")


def test_error_and_retry_info_builders():
    assert c.error_info("audit_failed", "x", "auditing") == {"category": "audit_failed", "code": "x", "stage": "auditing"}
    assert c.retry_info("source_temporary", after=STAMP) == {"eligible": True, "kind": "collection", "after": STAMP}
    assert c.retry_info("source_denied", after=STAMP) == {"eligible": False, "kind": None, "after": None}
    assert c.retry_info(None) == {"eligible": False, "kind": None, "after": None}
    for bad in (("made_up", "x", "intake"), ("audit_failed", "Bad Code", "intake"), ("audit_failed", "x", "nowhere")):
        with pytest.raises(c.ContractError):
            c.error_info(*bad)
