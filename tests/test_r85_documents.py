"""R8.5: consistency of the Make.com design documents with each other and with the implemented contracts (documentation only).

The JSON examples in `docs/r8/r85_data_contracts.md` are marked ```json contract=<schema>. Those that reuse an existing contract are validated by the
real validators in `src/seo_report/contracts.py`; the new, not-yet-implemented contracts are checked against the field tables written in the same
document. Nothing here touches Make.com, Google, a ledger, or any credential.
"""
import json
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from seo_report import contracts as c

DOCS = ROOT / "docs" / "r8"
R85 = ("r85_make_architecture", "r85_scenario_specifications", "r85_communication_protocol", "r85_data_contracts",
       "r85_security_and_reliability", "r85_implementation_roadmap", "r85_cost_and_operations")
LABELS = ("Implemented and tested", "Designed but not implemented", "Proposed", "Unverified", "Blocked")
SENSITIVE = re.compile(r"ya29\.[\w-]{10,}|1//[\w-]{20,}|GOCSPX-[\w-]{10,}|AIza[0-9A-Za-z_-]{35}|-----BEGIN [A-Z ]*PRIVATE KEY")


def text(name):
    return (DOCS / f"{name}.md").read_text(encoding="utf-8")


def examples():
    found = []
    for match in re.finditer(r"```json contract=([\w.\-]+)\n(.*?)\n```", text("r85_data_contracts"), re.S):
        found.append((match.group(1), json.loads(match.group(2))))
    return found


EXAMPLES = examples()


def of(name):
    return [data for schema, data in EXAMPLES if schema == name]


# --- the documents ---------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("name", R85)
def test_each_document_exists_and_is_substantial(name):
    assert (DOCS / f"{name}.md").stat().st_size > 3000


def test_the_architecture_document_defines_the_capability_labels():
    body = text("r85_make_architecture")
    for label in LABELS:
        assert f"**{label}**" in body, label


@pytest.mark.parametrize("name", R85)
def test_every_document_declares_that_it_is_design_only(name):
    first = "\n".join(text(name).split("\n")[:12]).lower()
    assert "design" in first and ("only" in first or "nothing" in first)


@pytest.mark.parametrize("name", R85)
def test_local_links_resolve_and_mermaid_fences_are_closed(name):
    body = text(name)
    for target in re.findall(r"\]\(([\w./#-]+\.md)(?:#[\w-]+)?\)", body):
        assert (DOCS / target).exists(), (name, target)
    assert body.count("```") % 2 == 0, name


@pytest.mark.parametrize("name", R85)
def test_no_document_contains_a_secret_shaped_string(name):
    assert not SENSITIVE.search(text(name))


def test_the_design_never_opens_an_inbound_port_or_calls_the_runner_from_make():
    body = " ".join(text(n) for n in R85).lower()
    assert "no inbound port" in body and "outbound-only" in body
    assert "make never calls the runner" in body or "make never calls" in body
    for forbidden in ("port forwarding is used", "expose a local port", "ngrok", "public endpoint on the runner"):
        assert forbidden not in body


def test_nothing_is_described_as_already_connected():
    body = text("r85_make_architecture")
    assert "Any Make.com scenario, webhook, connection | none" in body
    assert "CLI / entry point | none" in body


def test_the_roadmap_has_every_stage_and_the_decisions_and_conflicts():
    body = text("r85_implementation_roadmap")
    for heading in ("R8.6", "R8.7", "R8.8", "R8.9", "R8.10"):
        assert heading in body
    assert len(re.findall(r"\| D-\d+ \|", body)) == 15 and len(re.findall(r"\| C\d+ \|", body)) == 11
    assert "ten required checks" in body.lower() or "10 required checks" in body.lower()


# --- the data contracts -----------------------------------------------------------------------------------------------------

def test_the_expected_examples_are_present():
    schemas = {s for s, _ in EXAMPLES}
    assert schemas >= {"jasper.run-request", "jasper.run-response", "jasper.delivery-ready", "jasper.status-envelope", "jasper.request-ack",
                       "jasper.approval-request", "jasper.approval-decision", "jasper.email-delivery-result",
                       "jasper.error-notification", "jasper.runner-heartbeat"}


@pytest.mark.parametrize("data", of("jasper.run-request"))
def test_request_examples_satisfy_the_implemented_request_contract(data):
    request = c.parse_request(data)
    assert request.to_dict() == data  # round-trips exactly
    assert re.fullmatch(r"[A-Za-z0-9:._/-]{1,128}", data["idempotency_key"]) and "~" not in data["idempotency_key"]


def test_operator_actions_carry_operator_and_reason_and_others_do_not():
    for data in of("jasper.run-request"):
        has = "operator" in data["options"]
        assert has == (data["action"] in c.OPERATOR_ACTIONS)


@pytest.mark.parametrize("data", of("jasper.run-response"))
def test_response_examples_satisfy_the_implemented_response_contract(data):
    assert c.validate_response(data) is data


@pytest.mark.parametrize("data", of("jasper.delivery-ready"))
def test_delivery_ready_example_satisfies_the_implemented_contract(data):
    assert c.validate_delivery_ready(data) is data


def test_the_envelope_carries_a_valid_payload_and_the_documented_fields():
    (envelope,) = of("jasper.status-envelope")
    assert set(envelope) == {"schema", "version", "kind", "envelope_id", "dedupe_key", "sequence", "sent_at", "delivery_attempt",
                             "runner_id", "payload"}
    assert envelope["kind"] in ("request_ack", "run_status", "delivery_ready", "heartbeat") and envelope["version"] == 1
    assert re.fullmatch(r"[A-Za-z0-9:._/#-]{1,160}", envelope["dedupe_key"]) and envelope["delivery_attempt"] >= 1
    assert re.fullmatch(r"[a-z0-9-]{1,32}", envelope["runner_id"])
    c.validate_response(envelope["payload"])
    assert envelope["dedupe_key"].startswith(envelope["payload"]["run_key"] + "#" + str(envelope["sequence"]))


def test_the_audited_status_example_is_a_finalized_audited_report_and_nothing_more():
    (envelope,) = of("jasper.status-envelope")
    payload = envelope["payload"]
    assert payload["state"] == "audited" and payload["audit"]["outcome"] == "consistent" and payload["delivery"] is None
    assert payload["identity"]["pdf16"] == payload["identity"]["pdf_sha256"][:16]


def test_the_blocked_example_has_no_pdf_identity_and_is_not_deliverable():
    blocked = next(d for d in of("jasper.run-response") if d["state"] == "blocked")
    assert blocked["identity"] is None and blocked["audit"] is None and blocked["error"]["category"] == "audit_failed"
    assert blocked["error"]["category"] in c.ERROR_CATEGORIES and c.ERROR_CATEGORIES["audit_failed"][3] == "blocked"


def test_all_examples_use_one_consistent_pdf_hash_and_delivery_key():
    hashes, keys = set(), set()
    (envelope,) = of("jasper.status-envelope")
    hashes.add(envelope["payload"]["identity"]["pdf_sha256"])
    for schema in ("jasper.delivery-ready", "jasper.approval-request", "jasper.approval-decision", "jasper.email-delivery-result"):
        for data in of(schema):
            hashes.add(data["pdf_sha256"])
            keys.add(data["delivery_key"])
    assert len(hashes) == 1 and len(keys) == 1
    (key,) = keys
    (digest,) = hashes
    run_key, pdf16, recipients = key.rsplit("/", 2)
    assert pdf16 == digest[:16] and re.fullmatch(r"[0-9a-f]{16}", recipients)


def test_approval_is_bound_to_the_exact_pdf_hash():
    (request,) = of("jasper.approval-request")
    (decision,) = of("jasper.approval-decision")
    assert request["pdf16"] == request["pdf_sha256"][:16] and request["file_name"].endswith(request["pdf16"] + ".pdf")
    assert decision["pdf_sha256"] == request["pdf_sha256"] and decision["delivery_key"] == request["delivery_key"]
    assert decision["decision"] == "approved" and decision["approved_pdf16"] == decision["pdf_sha256"][:16]
    assert decision["rejection_reason_code"] is None and decision["decided_at"] < request["expires_at"]
    assert request["audit"] == {"outcome": "consistent", "finalized_valid": 1, "verification_complete": True}


def test_new_contract_enumerations_reuse_existing_vocabularies_and_stay_closed():
    (ack,) = of("jasper.request-ack")
    assert ack["decision"] in ("accepted", "replayed", "rejected") and ack["state"] in c.STATES and ack["action"] in c.ACTIONS
    (notice,) = of("jasper.error-notification")
    assert notice["state"] in c.STATES and notice["error_category"] in c.ERROR_CATEGORIES
    assert notice["severity"] in ("critical", "high", "medium", "info")
    body = text("r85_data_contracts")
    for event in re.search(r"`event` \| enum \| yes \| `([^|]+)` \|", body).group(1).replace("`", "").split(","):
        assert re.fullmatch(r"[a-z_]+", event.strip())
    (result,) = of("jasper.email-delivery-result")
    assert result["outcome"] in ("sent", "failed", "ambiguous", "cancelled") and result["error_category"] in (None, "delivery_failed")


def test_no_example_carries_an_address_a_path_a_token_or_a_secret():
    forbidden = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[a-z]{2,}|[A-Za-z]:\\|/Users/|token|secret|password|Bearer", re.I)
    for schema, data in EXAMPLES:
        assert not forbidden.search(json.dumps(data)), schema


def test_every_state_and_reason_named_in_the_examples_exists_in_the_contracts():
    for schema, data in EXAMPLES:
        for state in (data.get("state"), (data.get("payload") or {}).get("state")):
            assert state is None or state in c.STATES, (schema, state)
    for data in of("jasper.run-request"):
        assert data["options"].get("reason") in (None, *c.REASON_CODES)


def test_every_enumerated_error_category_in_the_design_documents_is_an_existing_one():
    known = set(c.ERROR_CATEGORIES)
    body = text("r85_data_contracts") + text("r85_scenario_specifications") + text("r85_communication_protocol")
    for category in re.findall(r"error_category[`\" :=|]+\s*`?([a-z_]+)`?", body):
        if category in ("null", "string", "none", "optional", "code", "category", "and", "the"):
            continue
        assert category in known or category in ("delivery_failed",), category
