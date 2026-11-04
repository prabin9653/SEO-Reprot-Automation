"""R8.4.2: the additive contract changes for operator actions and run generations."""
import copy
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from seo_report import contracts as c

PERIOD = "2026-09-29_2026-10-26"
BASE = {"schema": c.REQUEST_SCHEMA, "version": 1, "request_id": "00000000-0000-0000-0000-000000000001",
        "idempotency_key": "k1", "client_key": "example-co", "period_id": PERIOD, "action": "run",
        "requested_at": "2026-11-02T00:59:00+00:00", "requested_by": "make:test"}


def request(action, **options):
    return {**BASE, "action": action, "options": options}


@pytest.mark.parametrize("action", ["acknowledge", "cancel", "supersede"])
def test_operator_actions_need_an_operator_and_a_closed_reason(action):
    parsed = c.parse_request(request(action, operator="ops:alice", reason="operator_decision"))
    assert (parsed.operator, parsed.reason) == ("ops:alice", "operator_decision")
    assert parsed.to_dict()["options"] == {"replace_existing": False, "operator": "ops:alice", "reason": "operator_decision"}
    for options in ({}, {"operator": "ops:alice"}, {"reason": "operator_decision"}):
        with pytest.raises(c.ContractError) as caught:
            c.parse_request(request(action, **options))
        assert caught.value.code == "operator_details_required"
    with pytest.raises(c.ContractError):
        c.parse_request(request(action, operator="ops:alice", reason="free text with client data"))
    with pytest.raises(c.ContractError):
        c.parse_request(request(action, operator="has space", reason="operator_decision"))


def test_other_actions_refuse_operator_details():
    with pytest.raises(c.ContractError) as caught:
        c.parse_request(request("run", operator="ops:alice", reason="operator_decision"))
    assert caught.value.code == "operator_details_not_allowed"
    assert c.parse_request(request("run")).operator is None


def test_there_is_no_force_action():
    assert not [a for a in c.ACTIONS if "force" in a or "unblock" in a]


def test_run_keys_carry_the_generation_and_stay_backward_compatible():
    assert c.run_key_of("example-co", PERIOD) == f"example-co/{PERIOD}"
    assert c.run_key_of("example-co", PERIOD, 2) == f"example-co/{PERIOD}~2"
    assert c.parse_run_key_full(f"example-co/{PERIOD}") == ("example-co", PERIOD, 1)
    assert c.parse_run_key_full(f"example-co/{PERIOD}~12") == ("example-co", PERIOD, 12)
    assert c.parse_run_key(f"example-co/{PERIOD}~3") == ("example-co", PERIOD)
    for bad in (f"example-co/{PERIOD}~1", f"example-co/{PERIOD}~0", f"example-co/{PERIOD}~x", f"example-co/{PERIOD}~"):
        with pytest.raises(c.ContractError):
            c.parse_run_key_full(bad)
    with pytest.raises(c.ContractError):
        c.run_key_of("example-co", PERIOD, 0)


def response(**kw):
    base = dict(request_id=BASE["request_id"], idempotency_key="k1", mode="production", run_key=f"example-co/{PERIOD}",
                client_key="example-co", period_id=PERIOD, state="audited", outcome="succeeded", attempt=1,
                timestamps={"requested_at": BASE["requested_at"], "updated_at": BASE["requested_at"], "finished_at": None})
    base.update(kw)
    return c.RunResponse(**base)


def test_a_first_generation_response_is_exactly_the_original_schema():
    data = response().to_dict()
    assert not {"generation", "supersedes", "operator_action"} & set(data)
    assert c.validate_response(data)


def test_a_replacement_response_carries_its_generation_link_and_operator_action():
    data = response(run_key=f"example-co/{PERIOD}~2", generation=2, supersedes=f"example-co/{PERIOD}",
                    operator_action={"action": "supersede", "operator": "ops:alice", "reason": "content_error"}).to_dict()
    assert c.validate_response(data) and data["generation"] == 2


@pytest.mark.parametrize("change", [
    {"generation": 3}, {"supersedes": "example-co/2026-01-01_2026-01-28"}, {"supersedes": f"example-co/{PERIOD}~2"},
    {"operator_action": {"action": "delete", "operator": "ops:alice", "reason": "content_error"}},
    {"operator_action": {"action": "cancel", "operator": "ops:alice", "reason": "my own words"}},
    {"operator_action": {"action": "cancel", "operator": "ops:alice"}},
])
def test_inconsistent_generation_fields_are_rejected(change):
    data = response(run_key=f"example-co/{PERIOD}~2", generation=2, supersedes=f"example-co/{PERIOD}").to_dict()
    data = copy.deepcopy(data)
    data.update(change)
    with pytest.raises(c.ContractError):
        c.validate_response(data)


def test_the_new_reject_codes_exist():
    for code in ("not_blocked", "condition_not_recoverable", "blocking_condition_unresolved", "replace_existing_required",
                 "replacement_pending", "original_changed", "operator_details_required", "not_cancellable"):
        assert code in c.REJECT_CODES
