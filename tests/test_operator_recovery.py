"""R8.4.2: operator acknowledgement and cancellation of blocked or stuck runs (offline, temporary folders only).

Acknowledgement is a recorded precondition, never a state change: a blocked run stays blocked until a retry is admitted,
and the retry re-checks the blocking condition and still passes the mandatory audit gate.
"""
import dataclasses
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from seo_report import contracts
from seo_report import finalize_files as ff
from seo_report import ledger as ledger_module
from seo_report import pipeline
from seo_report.sources import gsc as gsc_source
from test_pipeline import (PERIOD_ID, RUN_KEY, default_collect, failing_collect, go, paths, req, tree,  # noqa: F401
                           world, NthOp)


def op(action, *, operator="ops:alice", reason="operator_decision", **kw):
    request = req(action=action, **kw)
    return dataclasses.replace(request, operator=operator, reason=reason)


def blocked_by_partial(world):
    """A finalized run that the audit gate blocked because an unfinished file sits in the reports folder."""
    w = world()
    reports, _, _ = paths(w)
    reports.mkdir(parents=True)
    stray = reports / ff.partial_name("report.pdf", "0123456789ab")
    stray.write_bytes(b"stray")
    assert go(w).state == "blocked"
    return w, stray


def resolve(w, stray):
    """What an operator does by hand: move the unfinished file out of the output tree (the system never deletes it)."""
    quarantine = w.tmp / "quarantine"
    quarantine.mkdir(exist_ok=True)
    stray.rename(quarantine / stray.name)
    return quarantine / stray.name


def synthetic_block(w, *, stage, category, code):
    """A blocked run made through the ledger alone (no files), for conditions that are hard to produce naturally."""
    lg = w.ledger
    run = lg.create_run("example-co", PERIOD_ID)
    run = lg.transition(run.run_key, "collecting", expected_state="requested", expected_version=run.state_version,
                        code="x", values={"generation_date": "2026-11-02"})
    return lg.transition(run.run_key, "blocked", expected_state="collecting", expected_version=run.state_version,
                         code="x", values={"failed_stage": stage, "error_category": category, "error_code": code})


# --- acknowledgement -------------------------------------------------------------------------------------------------------

def test_a_resolved_blocked_run_can_be_acknowledged_and_stays_blocked(world):
    w, stray = blocked_by_partial(world)
    resolve(w, stray)
    before = w.ledger.get_run(RUN_KEY)
    files = tree(w.settings.output_root)
    response = go(w, op("acknowledge", reason="partial_file_reviewed"))
    assert (response.outcome, response.state) == ("succeeded", "blocked")
    assert response.operator_action == {"action": "acknowledge", "operator": "ops:alice", "reason": "partial_file_reviewed"}
    run = w.ledger.get_run(RUN_KEY)
    assert (run.acknowledged, run.ack_operator, run.ack_reason) == (1, "ops:alice", "partial_file_reviewed")
    assert run.ack_at and run.state == "blocked" and run.state_version == before.state_version + 1
    assert run.attempt_count == before.attempt_count and run.error_category == "audit_failed"
    assert tree(w.settings.output_root) == files  # nothing was touched, moved or deleted
    event = [e for e in w.ledger.events_for(RUN_KEY) if e.code == "run_acknowledged"][0]
    assert event.kind == "operator" and "ops:alice" in event.detail_json


def test_acknowledgement_alone_never_makes_a_run_executable(world):
    w, stray = blocked_by_partial(world)
    resolve(w, stray)
    go(w, op("acknowledge"))
    for action in ("run", "retry"):
        if action == "run":
            response = go(w, req(action="run"))
            assert response.error["code"] == "operator_required"
    run = w.ledger.get_run(RUN_KEY)
    assert run.state == "blocked" and run.audit_outcome is None


def test_a_retry_without_acknowledgement_is_refused(world):
    w, stray = blocked_by_partial(world)
    resolve(w, stray)
    response = go(w, req(action="retry"))
    assert (response.outcome, response.error["code"]) == ("rejected", "operator_required")
    assert w.ledger.get_run(RUN_KEY).state == "blocked"


def test_a_retry_after_acknowledgement_keeps_the_identity_and_passes_the_audit_gate(world):
    w, stray = blocked_by_partial(world)
    resolve(w, stray)
    before = w.ledger.get_run(RUN_KEY)
    go(w, op("acknowledge"))
    w.clock.advance(days=2)
    retried = go(w, req(action="retry"))
    assert (retried.state, retried.outcome) == ("audited", "succeeded"), retried.error
    run = w.ledger.get_run(RUN_KEY)
    assert (run.generation_date, run.finalized_at, run.render_input_sha256, run.record_name, run.engine_run_id) == (
        before.generation_date, before.finalized_at, before.render_input_sha256, before.record_name, before.engine_run_id)
    assert run.attempt_count == before.attempt_count + 1 and run.acknowledged == 0
    assert run.audit_outcome == "consistent"  # the audit gate ran again
    codes = [e.code for e in w.ledger.events_for(RUN_KEY)]
    assert "run_acknowledged" in codes and "retry_after_acknowledgement" in codes


def test_the_blocking_condition_is_checked_again_when_the_retry_arrives(world):
    w, stray = blocked_by_partial(world)
    moved = resolve(w, stray)
    go(w, op("acknowledge"))
    moved.rename(stray)  # the unfinished file comes back after the acknowledgement
    response = go(w, req(action="retry"))
    assert (response.outcome, response.error["code"]) == ("rejected", "blocking_condition_unresolved")
    run = w.ledger.get_run(RUN_KEY)
    assert run.state == "blocked" and run.attempt_count == 1
    assert stray.exists()  # still not deleted


def test_an_unresolved_partial_file_blocks_acknowledgement(world):
    w, stray = blocked_by_partial(world)
    before = w.ledger.get_run(RUN_KEY)
    response = go(w, op("acknowledge"))
    assert (response.outcome, response.error["code"]) == ("rejected", "blocking_condition_unresolved")
    run = w.ledger.get_run(RUN_KEY)
    assert run.acknowledged == 0 and run.state_version == before.state_version
    assert stray.exists()
    refusal = [e for e in w.ledger.events_for(RUN_KEY) if e.code == "acknowledge_refused"]
    assert len(refusal) == 1 and "blocking_condition_unresolved" in refusal[0].detail_json


def test_an_unresolved_audit_finding_blocks_acknowledgement(world):
    w, stray = blocked_by_partial(world)
    resolve(w, stray)
    reports, _, _ = paths(w)
    (reports / "report.unarchived.pdf").write_bytes((reports / "report.pdf").read_bytes())  # an unarchived draft
    response = go(w, op("acknowledge"))
    assert (response.outcome, response.error["code"]) == ("rejected", "blocking_condition_unresolved")
    assert w.ledger.get_run(RUN_KEY).acknowledged == 0


@pytest.mark.parametrize("make_state", ["audited", "failed", "none"])
def test_only_blocked_runs_can_be_acknowledged(world, make_state):
    w = world(collect=failing_collect(gsc_source.SearchConsoleError("x", 403))) if make_state == "failed" else world()
    if make_state != "none":
        go(w)
    response = go(w, op("acknowledge"))
    assert response.outcome == "rejected"
    assert response.error["code"] == ("no_run" if make_state == "none" else "not_blocked")


@pytest.mark.parametrize("operator,reason", [(None, "operator_decision"), ("ops:alice", None), ("ops:alice", "because"),
                                              ("", "operator_decision")])
def test_operator_and_reason_are_mandatory(world, operator, reason):
    w, stray = blocked_by_partial(world)
    resolve(w, stray)
    response = go(w, op("acknowledge", operator=operator, reason=reason))
    assert (response.outcome, response.error["code"]) == ("rejected", "operator_details_required")
    assert w.ledger.get_run(RUN_KEY).acknowledged == 0


def test_a_repeated_acknowledgement_is_idempotent(world):
    w, stray = blocked_by_partial(world)
    resolve(w, stray)
    first = go(w, op("acknowledge", key="ack-1"))
    version = w.ledger.get_run(RUN_KEY).state_version
    replay = go(w, op("acknowledge", key="ack-1"))
    assert replay.replayed and replay.request_id == first.request_id
    again = go(w, op("acknowledge", key="ack-2", operator="ops:bob"))
    assert (again.outcome, again.state) == ("already_complete", "blocked")
    run = w.ledger.get_run(RUN_KEY)
    assert run.state_version == version and run.ack_operator == "ops:alice"  # the first acknowledgement stands
    assert [e.code for e in w.ledger.events_for(RUN_KEY)].count("run_acknowledged") == 1
    assert "acknowledge_repeated" in [e.code for e in w.ledger.events_for(RUN_KEY)]


def test_concurrent_acknowledgements_record_one_and_stale_actors_are_refused(world):
    w, stray = blocked_by_partial(world)
    resolve(w, stray)
    run = w.ledger.get_run(RUN_KEY)
    other = ledger_module.Ledger(w.settings.ledger_db_path, clock=w.clock)
    try:
        args = dict(expected_state="blocked", expected_version=run.state_version, reason="operator_decision")
        first, changed_first = w.ledger.acknowledge(RUN_KEY, operator="ops:alice", **args)
        second, changed_second = other.acknowledge(RUN_KEY, operator="ops:bob", **args)  # same stale version
        assert (changed_first, changed_second) == (True, False) and second.ack_operator == "ops:alice"
        # a different actor who saw an older state of a run that has since moved is refused
        w.ledger.transition(RUN_KEY, "finalized", expected_state="blocked", expected_version=first.state_version,
                            code="x", new_attempt=True)
        with pytest.raises(ledger_module.LedgerError) as caught:
            other.acknowledge(RUN_KEY, operator="ops:bob", **args)
        assert caught.value.code in ("not_blocked", "stale_state")
    finally:
        other.close()


def test_a_second_process_holding_the_client_lock_defers_the_acknowledgement(world):
    w, stray = blocked_by_partial(world)
    resolve(w, stray)
    other = ledger_module.Ledger(w.settings.ledger_db_path, clock=w.clock, pid_alive=lambda pid: True, owner_id="other")
    try:
        other.acquire_lock("client:example-co", ttl_seconds=600)
        response = go(w, op("acknowledge"))
        assert (response.outcome, response.error["category"]) == ("rejected", "run_in_progress")
        assert w.ledger.get_run(RUN_KEY).acknowledged == 0
    finally:
        other.close()


@pytest.mark.parametrize("code", ["render_input_already_finalized", "record_identity_differs"])
def test_conditions_a_retry_cannot_resolve_are_not_acknowledgeable(world, code):
    w = world()
    synthetic_block(w, stage="finalizing", category="finalization_conflict", code=code)
    response = go(w, op("acknowledge"))
    assert (response.outcome, response.error["code"]) == ("rejected", "condition_not_recoverable")


def test_a_blocked_layout_run_is_retried_from_the_start_after_acknowledgement(world):
    w = world()
    synthetic_block(w, stage="rendering", category="layout_issues", code="render_has_layout_issues")
    assert go(w, op("acknowledge", reason="data_corrected")).outcome == "succeeded"
    retried = go(w, req(action="retry"))
    assert (retried.state, retried.outcome) == ("audited", "succeeded"), retried.error
    run = w.ledger.get_run(RUN_KEY)
    assert run.generation_date == "2026-11-02" and run.attempt_count == 2


def test_replacing_an_existing_report_needs_an_explicit_flag_even_after_acknowledgement(world):
    w = world()
    synthetic_block(w, stage="finalizing", category="finalization_conflict", code="existing_report_differs")
    assert go(w, op("acknowledge")).outcome == "succeeded"
    refused = go(w, req(action="retry"))
    assert (refused.outcome, refused.error["code"]) == ("rejected", "replace_existing_required")
    assert w.ledger.get_run(RUN_KEY).state == "blocked" and w.ledger.get_run(RUN_KEY).replace_existing == 0
    go(w, req(action="retry", replace_existing=True))
    assert w.ledger.get_run(RUN_KEY).replace_existing == 1


def test_acknowledging_keeps_the_history_and_every_artifact(world):
    w, stray = blocked_by_partial(world)
    resolve(w, stray)
    events = [e.event_id for e in w.ledger.events_for(RUN_KEY)]
    files = tree(w.settings.output_root)
    history = w.settings.history_db_path.read_bytes()
    go(w, op("acknowledge"))
    after = [e.event_id for e in w.ledger.events_for(RUN_KEY)]
    assert after[:len(events)] == events and len(after) > len(events)
    assert tree(w.settings.output_root) == files and w.settings.history_db_path.read_bytes() == history


# --- cancellation ----------------------------------------------------------------------------------------------------------

def failed_world(world):
    w = world(collect=failing_collect(gsc_source.SearchConsoleError("x", 403)))
    assert go(w).state == "failed"
    return w


def test_a_failed_run_can_be_cancelled_with_an_operator_and_a_reason(world):
    w = failed_world(world)
    response = go(w, op("cancel", reason="no_longer_needed"))
    assert (response.outcome, response.state) == ("succeeded", "cancelled")
    assert response.operator_action["reason"] == "no_longer_needed"
    run = w.ledger.get_run(RUN_KEY)
    assert run.state == "cancelled" and run.finished_at and run.acknowledged == 0
    event = [e for e in w.ledger.events_for(RUN_KEY) if e.code == "run_cancelled"][0]
    assert event.kind == "operator" and "ops:alice" in event.detail_json and (event.from_state, event.to_state) == (
        "failed", "cancelled")
    assert go(w, req(action="run")).error["code"] == "cancelled"  # a cancelled run is never resumed by accident


def test_a_blocked_render_run_can_be_cancelled(world):
    w = world()
    synthetic_block(w, stage="rendering", category="layout_issues", code="render_has_layout_issues")
    assert go(w, op("cancel")).state == "cancelled"


def test_a_repeated_cancellation_is_idempotent(world):
    w = failed_world(world)
    first = go(w, op("cancel", key="cancel-1"))
    version = w.ledger.get_run(RUN_KEY).state_version
    assert go(w, op("cancel", key="cancel-1")).replayed and first.state == "cancelled"
    again = go(w, op("cancel", key="cancel-2", operator="ops:bob"))
    assert (again.outcome, again.state) == ("already_complete", "cancelled")
    assert w.ledger.get_run(RUN_KEY).state_version == version
    codes = [e.code for e in w.ledger.events_for(RUN_KEY)]
    assert codes.count("run_cancelled") == 1 and "cancel_repeated" in codes


def test_a_completed_audited_run_cannot_be_cancelled(world):
    w = world()
    assert go(w).state == "audited"
    files, before = tree(w.settings.output_root), w.ledger.get_run(RUN_KEY)
    response = go(w, op("cancel"))
    assert (response.outcome, response.error["code"]) == ("rejected", "not_cancellable")
    assert w.ledger.get_run(RUN_KEY) == before and tree(w.settings.output_root) == files


def test_a_blocked_run_with_a_finalized_report_cannot_be_cancelled(world):
    w, stray = blocked_by_partial(world)
    response = go(w, op("cancel"))
    assert (response.outcome, response.error["code"]) == ("rejected", "not_cancellable")
    assert w.ledger.get_run(RUN_KEY).state == "blocked" and stray.exists()


def test_a_run_whose_report_was_finalized_behind_the_ledger_is_not_cancelled(world, monkeypatch):
    from seo_report import finalize_service
    w = world()
    real = finalize_service.finalize

    def finalize_then_die(*args, **kwargs):
        real(*args, **kwargs)
        raise KeyboardInterrupt()

    monkeypatch.setattr(finalize_service, "finalize", finalize_then_die)
    monkeypatch.setattr(ledger_module.Ledger, "release_lock", lambda self, name: None)
    with pytest.raises(KeyboardInterrupt):
        go(w)
    monkeypatch.undo()
    w.clock.advance(hours=2)
    survivor = ledger_module.Ledger(w.settings.ledger_db_path, clock=w.clock, pid_alive=lambda pid: False)
    try:
        response = pipeline.run_client("example-co", PERIOD_ID, services=w.services, ledger=survivor, request=op("cancel"))
        assert (response.outcome, response.error["code"]) == ("rejected", "not_cancellable")
        assert survivor.get_run(RUN_KEY).state == "rendered"  # recovered, never cancelled
    finally:
        survivor.close()


def test_a_live_run_cannot_be_cancelled_by_another_process(world):
    w = failed_world(world)
    other = ledger_module.Ledger(w.settings.ledger_db_path, clock=w.clock, pid_alive=lambda pid: True, owner_id="other")
    try:
        other.acquire_lock("client:example-co", ttl_seconds=600)
        response = go(w, op("cancel"))
        assert (response.outcome, response.error["category"]) == ("rejected", "run_in_progress")
        assert w.ledger.get_run(RUN_KEY).state == "failed"
    finally:
        other.close()


def test_a_run_left_in_progress_by_a_dead_process_is_recovered_then_cancelled(world, monkeypatch):
    def boom(client_run, today):
        raise KeyboardInterrupt()
    w = world(collect=boom)
    monkeypatch.setattr(ledger_module.Ledger, "release_lock", lambda self, name: None)
    with pytest.raises(KeyboardInterrupt):
        go(w)
    assert w.ledger.get_run(RUN_KEY).state == "collecting"
    monkeypatch.undo()
    w.clock.advance(hours=2)
    survivor = ledger_module.Ledger(w.settings.ledger_db_path, clock=w.clock, pid_alive=lambda pid: False)
    try:
        response = pipeline.run_client("example-co", PERIOD_ID, services=w.services, ledger=survivor, request=op("cancel"))
        assert response.state == "cancelled"
        assert [a.outcome for a in survivor.attempts_for(RUN_KEY)] == ["interrupted", "interrupted"]
        assert survivor.get_lock("client:example-co") is None
    finally:
        survivor.close()


def test_concurrent_cancellations_apply_once(world):
    w = failed_world(world)
    run = w.ledger.get_run(RUN_KEY)
    other = ledger_module.Ledger(w.settings.ledger_db_path, clock=w.clock)
    try:
        args = dict(expected_state="failed", expected_version=run.state_version, code="run_cancelled")
        w.ledger.transition(RUN_KEY, "cancelled", **args)
        with pytest.raises(ledger_module.StaleStateError):
            other.transition(RUN_KEY, "cancelled", **args)
    finally:
        other.close()


@pytest.mark.parametrize("interrupt", [KeyboardInterrupt, SystemExit])
def test_an_interrupted_cancellation_leaves_a_consistent_recoverable_state(world, monkeypatch, interrupt):
    w = failed_world(world)
    before = w.ledger.get_run(RUN_KEY)

    def boom(self, *args, **kwargs):
        raise interrupt()

    monkeypatch.setattr(ledger_module.Ledger, "transition", boom)
    with pytest.raises(interrupt):
        go(w, op("cancel"))
    monkeypatch.undo()
    assert w.ledger.get_run(RUN_KEY) == before
    assert w.ledger.get_lock("client:example-co") is None and w.ledger.get_lock("runner:global") is None
    assert go(w, op("cancel")).state == "cancelled"  # the same action succeeds afterwards


def test_a_failure_inside_the_cancelling_transaction_rolls_back_everything(world, monkeypatch):
    w = failed_world(world)
    before, events = w.ledger.get_run(RUN_KEY), len(w.ledger.events_for(RUN_KEY))
    real = w.ledger._event

    def failing_event(**kwargs):
        real(**kwargs)
        raise KeyboardInterrupt()

    monkeypatch.setattr(w.ledger, "_event", failing_event)
    with pytest.raises(KeyboardInterrupt):
        go(w, op("cancel"))
    monkeypatch.undo()
    assert w.ledger.get_run(RUN_KEY) == before and len(w.ledger.events_for(RUN_KEY)) == events


def test_cancellation_deletes_no_file_snapshot_or_history(world):
    w = world(services_options={"file_ops": NthOp("rename_noreplace", 3)})
    assert go(w).state == "failed"
    files, history = tree(w.settings.output_root), w.settings.history_db_path.read_bytes()
    events = len(w.ledger.events_for(RUN_KEY))
    assert go(w, op("cancel")).state == "cancelled"
    assert tree(w.settings.output_root) == files and w.settings.history_db_path.read_bytes() == history
    assert len(w.ledger.events_for(RUN_KEY)) > events and w.ledger.get_run(RUN_KEY) is not None


def test_cancel_requires_operator_details(world):
    w = failed_world(world)
    response = go(w, op("cancel", operator=None))
    assert (response.outcome, response.error["code"]) == ("rejected", "operator_details_required")
    assert w.ledger.get_run(RUN_KEY).state == "failed"
