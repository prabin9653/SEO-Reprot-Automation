"""R8.4.2: replacing an audited report through a new, linked run (offline, temporary folders only).

A replacement is a new run (`<client>/<period>~2`) with its own request, attempts and history, linked to the audited run it
follows. The audited run, its record, snapshot and PDF are never edited or deleted; the engine's own safe promotion keeps
the previous PDF as `report.superseded.<hash16>.pdf`. The replacement passes the full finalization and audit.
"""
import dataclasses
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from seo_report import audit_service
from seo_report import contracts
from seo_report import ledger as ledger_module
from seo_report import pipeline
from seo_report.sources import gsc as gsc_source
from test_pipeline import (PERIOD_ID, RUN_KEY, default_collect, failing_collect, flaky_store, go, paths, req, tree,  # noqa: F401
                           world)

SECOND_KEY = f"{RUN_KEY}~2"
THIRD_KEY = f"{RUN_KEY}~3"


def op(action, *, operator="ops:alice", reason="content_error", replace_existing=True, **kw):
    request = req(action=action, replace_existing=replace_existing, **kw)
    return dataclasses.replace(request, operator=operator, reason=reason)


def audited(world, **kw):
    w = world(**kw)
    assert go(w).state == "audited"
    w.clock.advance(days=1)  # a replacement made later has another generation date, hence another render identity
    return w


def audit_args(w, run_key):
    run = w.ledger.get_run(run_key)
    return dict(settings=w.settings, client_key="example-co", period_start=run.period_start, period_end=run.period_end,
                run_id=run.engine_run_id, pdf_sha256=run.pdf_sha256, pdf_bytes=run.pdf_bytes,
                record_name=run.record_name, render_input_sha256=run.render_input_sha256)


def runs(w):
    return w.ledger.runs_for_period("example-co", PERIOD_ID)


# --- success ---------------------------------------------------------------------------------------------------------------

def test_an_audited_report_is_replaced_by_a_new_linked_run_that_passes_finalization_and_audit(world):
    w = audited(world)
    original = w.ledger.get_run(RUN_KEY)
    response = go(w, op("supersede"))
    assert (response.state, response.outcome) == ("audited", "succeeded"), response.error
    assert (response.run_key, response.generation, response.supersedes) == (SECOND_KEY, 2, RUN_KEY)
    assert response.operator_action == {"action": "supersede", "operator": "ops:alice", "reason": "content_error"}
    assert contracts.validate_response(response.to_dict())
    new = w.ledger.get_run(SECOND_KEY)
    assert (new.generation, new.supersedes_run_key, new.link_kind, new.link_operator, new.link_reason) == (
        2, RUN_KEY, "supersede", "ops:alice", "content_error")
    assert new.replace_existing == 1 and new.audit_outcome == "consistent"
    assert new.generation_date == "2026-11-03" and original.generation_date == "2026-11-02"
    assert len(runs(w)) == 2


def test_the_replacement_has_its_own_identity_request_and_attempts(world):
    w = audited(world)
    request = op("supersede")
    go(w, request)
    original, new = w.ledger.get_run(RUN_KEY), w.ledger.get_run(SECOND_KEY)
    assert new.run_key != original.run_key
    assert new.render_input_sha256 != original.render_input_sha256 and new.pdf_sha256 != original.pdf_sha256
    assert new.record_name != original.record_name
    assert w.ledger.attempts_for(SECOND_KEY)[0].trigger_request == request.request_id
    assert w.ledger.attempts_for(RUN_KEY)[0].trigger_request != request.request_id
    stored = w.ledger.requests_by_key(request.idempotency_key)
    assert stored.run_key == SECOND_KEY and stored.action == "supersede"


def test_the_original_run_report_record_and_ledger_row_are_unchanged(world):
    w = audited(world)
    before_row = w.ledger.get_run(RUN_KEY)
    before = tree(w.settings.output_root)
    original_pdf = before_row.pdf_sha256
    go(w, op("supersede"))
    assert w.ledger.get_run(RUN_KEY) == before_row
    after = tree(w.settings.output_root)
    reports, reviewer, _ = paths(w)
    old_pdf = next(data for name, data in before.items() if name.endswith("report.pdf"))
    for name, data in before.items():  # nothing existing was deleted or rewritten; only `report.pdf` now holds the new PDF
        assert name in after, name
        if not name.endswith("report.pdf"):
            assert after[name] == data, name
    superseded = [p for p in reports.iterdir() if p.name.startswith("report.superseded.")]
    assert len(superseded) == 1 and superseded[0].name.startswith(f"report.superseded.{original_pdf[:16]}")
    assert superseded[0].read_bytes() == old_pdf  # the previous PDF is kept byte for byte
    records = sorted(p.name for p in reviewer.iterdir() if p.name.startswith("reviewer."))
    assert len(records) == 2 and before_row.record_name in records


def test_both_runs_are_independently_auditable_and_the_ledger_tells_them_apart(world):
    w = audited(world)
    go(w, op("supersede"))
    replacement = audit_service.verify_finalization(**audit_args(w, SECOND_KEY))
    assert replacement.ok, replacement.reasons
    original = audit_service.verify_finalization(**audit_args(w, RUN_KEY))
    assert original.reader_lists_run and original.finalized_valid >= 1
    old, new = w.ledger.get_run(RUN_KEY), w.ledger.get_run(SECOND_KEY)
    assert (old.generation, old.supersedes_run_key, old.link_kind) == (1, None, None)
    assert (new.generation, new.supersedes_run_key, new.link_kind) == (2, RUN_KEY, "supersede")
    created = [e for e in w.ledger.events_for(SECOND_KEY) if e.code == "run_supersede_created"][0]
    noted = [e for e in w.ledger.events_for(RUN_KEY) if e.code == "run_supersede_requested"][0]
    assert RUN_KEY in created.detail_json and "baseline" in created.detail_json and SECOND_KEY in noted.detail_json
    assert w.ledger.latest_run("example-co", PERIOD_ID).run_key == SECOND_KEY


def test_the_replacement_is_not_delivered_or_published_by_this_stage(world):
    w = audited(world)
    response = go(w, op("supersede"))
    assert response.state == "audited" and response.delivery is None
    assert w.ledger.get_run(SECOND_KEY).state == "audited" and w.ledger.all_outbox() is not None
    assert not [d for d in w.ledger._all("SELECT * FROM deliveries")]


# --- failure and retry ---------------------------------------------------------------------------------------------------------

def test_a_failed_replacement_leaves_the_audited_original_valid(world):
    w = audited(world)
    before_row, before_files = w.ledger.get_run(RUN_KEY), tree(w.settings.output_root)
    failing = dataclasses.replace(w.services, collect=failing_collect(gsc_source.SearchConsoleError("x", 403)))
    response = go(w, op("supersede"), services=failing)
    assert (response.state, response.run_key) == ("failed", SECOND_KEY)
    assert w.ledger.get_run(RUN_KEY) == before_row and tree(w.settings.output_root) == before_files
    assert audit_service.verify_finalization(**audit_args(w, RUN_KEY)).ok
    assert w.ledger.get_run(SECOND_KEY).state == "failed" and len(runs(w)) == 2


def test_a_failed_replacement_is_retried_with_its_own_generation_date_and_render_identity(world):
    w = audited(world)
    broken = dataclasses.replace(w.services, store_factory=flaky_store("write"))
    first = go(w, op("supersede"), services=broken)
    assert (first.state, first.error["category"]) == ("failed", "finalization_failed"), first
    failed = w.ledger.get_run(SECOND_KEY)
    assert failed.generation_date == "2026-11-03" and failed.render_input_sha256
    w.clock.advance(days=4)
    retried = go(w, req(action="retry"), services=broken)
    assert (retried.state, retried.run_key) == ("audited", SECOND_KEY), retried.error
    done = w.ledger.get_run(SECOND_KEY)
    assert (done.generation_date, done.render_input_sha256, done.finalized_at) == (
        failed.generation_date, failed.render_input_sha256, failed.finalized_at)
    assert done.attempt_count == 2 and len(runs(w)) == 2
    assert audit_service.verify_finalization(**audit_args(w, SECOND_KEY)).ok


def test_a_completed_replacement_is_not_regenerated_by_a_later_run_request(world):
    w = audited(world)
    go(w, op("supersede"))
    before = tree(w.settings.output_root)
    again = go(w, req(action="run"))
    assert (again.outcome, again.run_key) == ("already_complete", SECOND_KEY)
    assert len(runs(w)) == 2 and tree(w.settings.output_root) == before


# --- idempotency and guards ----------------------------------------------------------------------------------------------------

def test_a_repeated_supersede_request_creates_no_second_replacement(world):
    w = audited(world)
    request = op("supersede", key="supersede-1")
    first = go(w, request)
    replay = go(w, dataclasses.replace(request, request_id=req().request_id))  # same idempotency key
    assert replay.replayed and replay.run_key == first.run_key == SECOND_KEY
    assert len(runs(w)) == 2


def test_an_interrupted_supersede_is_continued_by_the_same_request_never_duplicated(world, monkeypatch):
    w = audited(world)
    calls = []

    def collect(client_run, today):
        calls.append(1)
        raise KeyboardInterrupt()

    monkeypatch.setattr(ledger_module.Ledger, "release_lock", lambda self, name: None)
    request = op("supersede", key="supersede-2")
    with pytest.raises(KeyboardInterrupt):
        go(w, request, services=dataclasses.replace(w.services, collect=collect))
    assert w.ledger.get_run(SECOND_KEY).state == "collecting"
    monkeypatch.undo()
    w.clock.advance(hours=2)
    survivor = ledger_module.Ledger(w.settings.ledger_db_path, clock=w.clock, pid_alive=lambda pid: False)
    try:
        response = pipeline.run_client("example-co", PERIOD_ID, services=w.services, ledger=survivor, request=request)
        assert (response.state, response.run_key) == ("audited", SECOND_KEY), response.error
        assert len(survivor.runs_for_period("example-co", PERIOD_ID)) == 2
    finally:
        survivor.close()


def test_a_second_supersede_while_a_replacement_is_pending_is_refused(world):
    w = audited(world)
    failing = dataclasses.replace(w.services, collect=failing_collect(gsc_source.SearchConsoleError("x", 403)))
    go(w, op("supersede"), services=failing)
    response = go(w, op("supersede"))
    assert (response.outcome, response.error["code"]) == ("rejected", "replacement_pending")
    assert len(runs(w)) == 2


def test_replacing_needs_the_explicit_replace_flag(world):
    w = audited(world)
    before = tree(w.settings.output_root)
    response = go(w, op("supersede", replace_existing=False))
    assert (response.outcome, response.error["code"]) == ("rejected", "replace_existing_required")
    assert len(runs(w)) == 1 and tree(w.settings.output_root) == before


@pytest.mark.parametrize("operator,reason", [(None, "content_error"), ("ops:alice", None), ("ops:alice", "whatever")])
def test_a_replacement_needs_an_operator_and_a_reason(world, operator, reason):
    w = audited(world)
    response = go(w, op("supersede", operator=operator, reason=reason))
    assert (response.outcome, response.error["code"]) == ("rejected", "operator_details_required")
    assert len(runs(w)) == 1


def test_only_an_audited_run_can_be_superseded(world):
    w = world(collect=failing_collect(gsc_source.SearchConsoleError("x", 403)))
    assert go(w).state == "failed"
    response = go(w, op("supersede"))
    assert (response.outcome, response.error["code"]) == ("rejected", "not_audited")
    assert len(runs(w)) == 1


def test_an_original_that_no_longer_passes_the_audit_is_not_replaced(world):
    w = audited(world)
    reports, _, _ = paths(w)
    pdf = reports / "report.pdf"
    pdf.write_bytes(pdf.read_bytes()[:-1] + b" ")
    response = go(w, op("supersede"))
    assert (response.outcome, response.error["code"]) == ("rejected", "original_changed")
    assert len(runs(w)) == 1


def test_a_replacement_is_not_audited_when_the_original_changed_meanwhile(world, monkeypatch):
    w = audited(world)
    real, state = audit_service.verify_finalization, {"calls": 0}

    def tamper_after_the_audit(**kwargs):
        verdict = real(**kwargs)
        reports, _, _ = paths(w)
        for path in reports.iterdir():
            if path.name.startswith("report.superseded."):
                path.write_bytes(path.read_bytes() + b" ")  # the original's PDF changes behind the replacement
        return verdict

    monkeypatch.setattr(audit_service, "verify_finalization", tamper_after_the_audit)
    response = go(w, op("supersede"))
    assert (response.state, response.error["category"], response.error["code"]) == (
        "blocked", "audit_failed", "original_changed")
    assert w.ledger.get_run(SECOND_KEY).audit_outcome is None
    assert w.ledger.get_run(RUN_KEY).state == "audited"
    assert "original_changed" in [e.code for e in w.ledger.events_for(SECOND_KEY)]


def test_a_second_process_holding_the_client_lock_defers_the_supersede(world):
    w = audited(world)
    other = ledger_module.Ledger(w.settings.ledger_db_path, clock=w.clock, pid_alive=lambda pid: True, owner_id="other")
    try:
        other.acquire_lock("client:example-co", ttl_seconds=600)
        response = go(w, op("supersede"))
        assert (response.outcome, response.error["category"]) == ("rejected", "run_in_progress")
        assert len(runs(w)) == 1
    finally:
        other.close()


def test_two_replacements_of_the_same_run_cannot_both_be_created(world):
    w = audited(world)
    other = ledger_module.Ledger(w.settings.ledger_db_path, clock=w.clock)
    try:
        link = dict(supersedes=RUN_KEY, link_kind="supersede", operator="ops:alice", reason="content_error")
        w.ledger.create_run("example-co", PERIOD_ID, **link)
        with pytest.raises(ledger_module.LedgerError) as caught:
            other.create_run("example-co", PERIOD_ID, **link)
        assert caught.value.code == "run_not_latest"
        assert len(runs(w)) == 2
    finally:
        other.close()


# --- restart after a cancellation ----------------------------------------------------------------------------------------------

def test_a_cancelled_run_is_restarted_as_a_new_linked_generation(world):
    w = world(collect=failing_collect(gsc_source.SearchConsoleError("x", 403)))
    assert go(w).state == "failed"
    cancel = dataclasses.replace(req(action="cancel"), operator="ops:alice", reason="data_corrected")
    assert go(w, cancel).state == "cancelled"
    working = dataclasses.replace(w.services, collect=default_collect)
    response = go(w, op("supersede", replace_existing=False), services=working)
    assert (response.state, response.run_key, response.supersedes) == ("audited", SECOND_KEY, RUN_KEY), response.error
    new = w.ledger.get_run(SECOND_KEY)
    assert (new.link_kind, new.replace_existing) == ("restart", 0)
    assert w.ledger.get_run(RUN_KEY).state == "cancelled"  # the cancelled run is kept as it was


def test_a_replacement_of_a_replacement_is_a_new_generation_with_its_own_key(world):
    w = audited(world)
    go(w, op("supersede"))
    w.clock.advance(days=1)
    third = go(w, op("supersede"))
    assert (third.state, third.run_key, third.supersedes) == ("audited", THIRD_KEY, SECOND_KEY), third.error
    assert [r.generation for r in runs(w)] == [1, 2, 3]
    assert contracts.parse_run_key_full(THIRD_KEY) == ("example-co", PERIOD_ID, 3)


def test_status_reports_the_latest_generation(world):
    w = audited(world)
    go(w, op("supersede"))
    status = go(w, req(action="status"))
    assert (status.run_key, status.generation, status.supersedes, status.state) == (SECOND_KEY, 2, RUN_KEY, "audited")


def test_a_period_without_a_run_cannot_be_superseded(world):
    w = world()
    response = go(w, op("supersede"))
    assert (response.outcome, response.error["code"]) == ("rejected", "no_run")