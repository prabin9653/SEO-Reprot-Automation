"""R8.3: the execution ledger, in temporary folders only. The snapshot/history database is never involved."""
import json
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from seo_report import contracts as c
from seo_report import ledger as lg
from test_pipeline import Clock, PERIOD_ID, req

KEY = f"example-co/{PERIOD_ID}"


@pytest.fixture
def ledger(tmp_path):
    made = []

    def build(name="ledger.sqlite", **kwargs):
        kwargs.setdefault("clock", Clock())
        kwargs.setdefault("pid_alive", lambda pid: True)
        item = lg.Ledger(tmp_path / "ledger" / name, **kwargs)
        made.append(item)
        return item
    yield build
    for item in made:
        item.close()


def move(ledger, to, **kw):
    run = ledger.get_run(KEY)
    return ledger.transition(KEY, to, expected_state=run.state, expected_version=run.state_version,
                             code=kw.pop("code", "test_move"), **kw)


def walk(ledger, *states):
    for state in states:
        move(ledger, state)


# --- creation and schema ---------------------------------------------------------------------------------------------------------

def test_the_database_is_created_with_the_documented_tables_and_settings(ledger, tmp_path):
    item = ledger()
    tables = {r[0] for r in item._con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"requests", "runs", "run_attempts", "events", "deliveries", "locks", "outbox"} <= tables
    assert item._con.execute("PRAGMA user_version").fetchone()[0] == lg.SCHEMA_VERSION
    assert item._con.execute("PRAGMA journal_mode").fetchone()[0].lower() == "delete"
    assert item._con.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert item.integrity_check()


def test_it_is_a_separate_file_and_never_creates_the_history_database(ledger, tmp_path):
    item = ledger()
    assert item.path.parent == tmp_path / "ledger" and not (tmp_path / "history").exists()
    assert not any(p.suffix == ".sqlite" and p != item.path for p in tmp_path.rglob("*.sqlite"))


def test_reopening_keeps_the_data_and_an_unknown_schema_is_refused(ledger, tmp_path):
    first = ledger()
    first.create_run("example-co", PERIOD_ID)
    first.close()
    second = lg.Ledger(first.path, clock=Clock())
    assert second.get_run(KEY).state == "requested"
    second.close()
    raw = sqlite3.connect(first.path)
    raw.execute("PRAGMA user_version=99")
    raw.commit()
    raw.close()
    with pytest.raises(lg.LedgerError, match="ledger_schema_unsupported"):
        lg.Ledger(first.path, clock=Clock())


def test_a_read_only_open_never_creates_the_file_and_cannot_write(ledger, tmp_path):
    with pytest.raises(lg.LedgerError, match="ledger_missing"):
        lg.Ledger(tmp_path / "nowhere" / "l.sqlite", clock=Clock(), readonly=True)
    assert not (tmp_path / "nowhere").exists()
    writer = ledger()
    writer.create_run("example-co", PERIOD_ID)
    reader = lg.Ledger(writer.path, clock=Clock(), readonly=True)
    assert reader.get_run(KEY).state == "requested"
    with pytest.raises(lg.LedgerError, match="ledger_read_only"):
        reader.create_run("other-co", PERIOD_ID)
    reader.close()


def test_a_missing_database_can_be_refused_instead_of_created(tmp_path):
    with pytest.raises(lg.LedgerError, match="ledger_missing"):
        lg.Ledger(tmp_path / "ledger" / "x.sqlite", clock=Clock(), create=False)
    assert not (tmp_path / "ledger").exists()


def test_a_database_in_a_onedrive_folder_is_refused(tmp_path):
    cloud = tmp_path / "OneDrive"
    with pytest.raises(lg.LedgerError, match="ledger_in_cloud_sync"):
        lg.Ledger(cloud / "ledger.sqlite", clock=Clock(), environ={"OneDrive": str(cloud)})
    with pytest.raises(lg.LedgerError, match="ledger_path_not_absolute"):
        lg.Ledger("relative.sqlite", clock=Clock())


def test_the_schema_has_no_place_for_client_names_contacts_or_secrets(ledger):
    columns = {row[1] for table in ("requests", "runs", "run_attempts", "events", "deliveries", "locks", "outbox")
               for row in ledger()._con.execute(f"PRAGMA table_info({table})")}
    for word in ("name", "email", "recipient", "token", "secret", "password", "website", "text", "message", "path"):
        assert not [column for column in columns if word in column.replace("file_name", "").replace("lock_name", "")
                    .replace("record_name", "").replace("render_format_name", "")
                        .replace("recipient_set_hash16", "")], word  # a hash of the recipient set, never an address


# --- requests ----------------------------------------------------------------------------------------------------------------------

def test_a_request_is_registered_once_and_replayed_afterwards(ledger):
    item = ledger()
    request = req()
    first = item.register_request(request)
    assert (first.kind, first.status, first.response) == ("new", "accepted", None)
    item.finish_request(request.request_id, status="completed", response={"state": "audited"})
    again = item.register_request(request)
    assert (again.kind, again.status, again.response) == ("replay", "completed", {"state": "audited"})
    assert item._con.execute("SELECT COUNT(*) FROM requests").fetchone()[0] == 1


def test_an_idempotency_key_identifies_one_intended_action(ledger):
    item = ledger()
    item.register_request(req(key="k1"))
    with pytest.raises(lg.LedgerError, match="idempotency_key_reused"):
        item.register_request(req(key="k1", action="retry"))
    with pytest.raises(lg.LedgerError, match="idempotency_key_reused"):
        item.register_request(req("other-co", key="k1"))


def test_a_request_id_cannot_be_reused_under_a_new_key(ledger):
    item = ledger()
    request = req()
    item.register_request(request)
    clash = c.RunRequest(**{**request.__dict__, "idempotency_key": "another-key"})
    with pytest.raises(lg.LedgerError, match="request_id_conflict"):
        item.register_request(clash)


def test_finishing_an_unknown_request_or_with_a_bad_status_is_refused(ledger):
    item = ledger()
    with pytest.raises(lg.LedgerError, match="request_missing"):
        item.finish_request("0b9a6a52-6c1e-4f0e-9a43-6d3c6e2b9a99", status="completed", response={})
    with pytest.raises(lg.LedgerError, match="request_status_invalid"):
        item.finish_request("x", status="done", response={})


# --- runs and transitions ------------------------------------------------------------------------------------------------------------

def test_one_run_per_client_and_period(ledger):
    item = ledger()
    run = item.create_run("example-co", PERIOD_ID)
    assert (run.run_key, run.state, run.state_version, run.attempt_count) == (KEY, "requested", 0, 1)
    with pytest.raises(lg.LedgerError, match="run_exists"):
        item.create_run("example-co", PERIOD_ID)
    other = item.create_run("other-co", PERIOD_ID)
    assert other.run_key == f"other-co/{PERIOD_ID}"
    with pytest.raises(c.ContractError):
        item.create_run("example-co", "2026-10-01_2026-10-02")


def test_the_run_key_is_tied_to_its_parts_by_the_database(ledger):
    item = ledger()
    with pytest.raises(sqlite3.IntegrityError):
        item._con.execute("INSERT INTO runs(run_key, client_key, period_start, period_end, state, created_at, updated_at) "
                          "VALUES ('a/b','example-co','2026-10-01','2026-10-28','requested','t','t')")


def test_every_documented_transition_is_recorded_with_an_event(ledger):
    item = ledger()
    item.create_run("example-co", PERIOD_ID)
    walk(item, *c.PROGRESS_STATES[1:])
    run = item.get_run(KEY)
    assert run.state == "delivered" and run.state_version == 10 and run.finished_at
    events = [(e.from_state, e.to_state) for e in item.events_for(KEY) if e.kind == "state_change"]
    assert events == [(None, "requested")] + list(zip(c.PROGRESS_STATES, c.PROGRESS_STATES[1:]))
    assert [a.outcome for a in item.attempts_for(KEY)] == ["succeeded"]  # closed when the audit passed (audited)


def test_a_transition_not_in_the_state_machine_is_refused_and_changes_nothing(ledger):
    item = ledger()
    item.create_run("example-co", PERIOD_ID)
    before = item.get_run(KEY)
    for target in ("rendered", "audited", "ready_for_delivery", "delivered", "failed"):
        with pytest.raises(lg.InvalidTransitionError):
            move(item, target)
    with pytest.raises(lg.InvalidTransitionError, match="state_unknown"):
        move(item, "somewhere")
    assert item.get_run(KEY) == before and len(item.events_for(KEY)) == 1


def test_the_optimistic_check_refuses_a_stale_writer(ledger):
    item = ledger()
    item.create_run("example-co", PERIOD_ID)
    with pytest.raises(lg.StaleStateError):
        item.transition(KEY, "collecting", expected_state="requested", expected_version=5, code="x")
    with pytest.raises(lg.StaleStateError):
        item.transition(KEY, "collecting", expected_state="rendered", expected_version=0, code="x")
    move(item, "collecting")
    with pytest.raises(lg.StaleStateError):  # the same transition attempted twice from the old version
        item.transition(KEY, "collecting", expected_state="requested", expected_version=0, code="x")
    with pytest.raises(lg.LedgerError, match="run_missing"):
        item.transition("nobody/" + PERIOD_ID, "collecting", expected_state="requested", expected_version=0, code="x")


def test_two_connections_cannot_both_win_the_same_transition(ledger):
    first, second = ledger(), ledger()
    first.create_run("example-co", PERIOD_ID)
    run = first.get_run(KEY)
    first.transition(KEY, "collecting", expected_state="requested", expected_version=run.state_version, code="x")
    with pytest.raises(lg.StaleStateError):
        second.transition(KEY, "collecting", expected_state="requested", expected_version=run.state_version, code="x")


def test_only_listed_columns_can_be_set_and_failed_values_are_checked(ledger):
    item = ledger()
    item.create_run("example-co", PERIOD_ID)
    with pytest.raises(lg.LedgerError, match="column_not_settable"):
        move(item, "collecting", values={"state": "audited"})
    with pytest.raises(lg.LedgerError, match="column_not_settable"):
        move(item, "collecting", values={"mode": "dry_run"})
    with pytest.raises(lg.LedgerError, match="value_rejected"):
        move(item, "collecting", values={"retry_kind": "teleport"})
    assert item.get_run(KEY).state == "requested"


def test_generation_date_and_finalized_at_are_write_once(ledger):
    item = ledger()
    item.create_run("example-co", PERIOD_ID)
    move(item, "collecting", values={"generation_date": "2026-11-02"})
    with pytest.raises(lg.LedgerError, match="value_rejected"):
        move(item, "rendering", values={"generation_date": "2026-12-25"})
    move(item, "rendering", values={"generation_date": "2026-11-02"})  # the same value again is harmless
    move(item, "rendered")
    move(item, "finalizing", values={"finalized_at": "2026-11-02T01:00:00+00:00"})
    with pytest.raises(lg.LedgerError, match="value_rejected"):
        move(item, "finalized", values={"finalized_at": "2027-01-01T00:00:00+00:00"})
    assert item.get_run(KEY).generation_date == "2026-11-02"
    with pytest.raises(sqlite3.IntegrityError):
        item._con.execute("UPDATE runs SET generation_date = '2020-01-01'")


def test_failure_and_retry_start_a_new_attempt_and_clear_the_failure(ledger):
    item = ledger()
    item.create_run("example-co", PERIOD_ID)
    walk(item, "collecting")
    failed = move(item, "failed", values={"failed_stage": "collecting", "error_category": "source_temporary",
                                         "error_code": "gsc_http_503", "retry_eligible": 1, "retry_kind": "collection",
                                         "retry_after": "2026-11-02T02:00:00+00:00"})
    assert failed.finished_at and failed.attempt_count == 1
    with pytest.raises(lg.InvalidTransitionError, match="attempt_rule"):
        move(item, "requested")  # leaving failed needs new_attempt=True
    retried = move(item, "requested", new_attempt=True, code="retry_admitted")
    assert (retried.state, retried.attempt_count, retried.error_category, retried.retry_eligible, retried.finished_at) == (
        "requested", 2, None, 0, None)
    assert [(a.attempt_no, a.outcome, a.error_category) for a in item.attempts_for(KEY)] == [
        (1, "failed", "source_temporary"), (2, None, None)]
    with pytest.raises(lg.InvalidTransitionError, match="attempt_rule"):
        move(item, "collecting", new_attempt=True)  # a new attempt only leaves failed/blocked


def test_runs_and_events_cannot_be_deleted_and_events_cannot_be_changed(ledger):
    item = ledger()
    item.create_run("example-co", PERIOD_ID)
    for sql in ("DELETE FROM runs", "UPDATE events SET code = 'x'", "DELETE FROM events"):
        with pytest.raises(sqlite3.IntegrityError):
            item._con.execute(sql)
    assert len(item.events_for(KEY)) == 1


def test_event_codes_are_a_fixed_vocabulary(ledger):
    item = ledger()
    item.create_run("example-co", PERIOD_ID)
    for bad in ("Has Space", "person@example.test", "x" * 65, ""):
        with pytest.raises(c.ContractError):
            item.append_event(kind="info", code=bad, run_key=KEY)


def test_a_failed_transaction_leaves_nothing_behind(ledger):
    item = ledger()
    item.create_run("example-co", PERIOD_ID)
    version = item.get_run(KEY).state_version
    with pytest.raises(c.ContractError):
        move(item, "collecting", code="Not Allowed")  # the event is rejected after the run row was updated: rolled back
    assert item.get_run(KEY).state == "requested" and item.get_run(KEY).state_version == version
    assert len(item.events_for(KEY)) == 1


# --- outbox -------------------------------------------------------------------------------------------------------------------------

def test_status_messages_are_queued_in_the_same_transaction_and_deduplicated(ledger):
    item = ledger()
    item.create_run("example-co", PERIOD_ID)
    move(item, "collecting", outbox=lambda run: {"state": run.state})
    assert [json.loads(o.payload_json) for o in item.all_outbox()] == [{"state": "collecting"}]
    item.enqueue_outbox(destination="make_status", payload={"x": 1}, dedupe_key="same", run_key=KEY)
    item.enqueue_outbox(destination="make_status", payload={"x": 2}, dedupe_key="same", run_key=KEY)
    assert len(item.all_outbox()) == 2
    with pytest.raises(lg.StaleStateError):
        item.transition(KEY, "rendering", expected_state="requested", expected_version=0, code="x",
                        outbox=lambda run: {"state": "never"})
    assert len(item.all_outbox()) == 2  # nothing queued by the refused transition


def test_outbox_delivery_and_retry_scheduling(ledger):
    clock = Clock()
    item = ledger(clock=clock)
    item.enqueue_outbox(destination="make_status", payload={"n": 1}, dedupe_key="a")
    item.enqueue_outbox(destination="make_delivery_ready", payload={"n": 2}, dedupe_key="b")
    pending = item.pending_outbox()
    assert [p.dedupe_key for p in pending] == ["a", "b"]
    item.mark_outbox_delivered(pending[0].outbox_id)
    item.mark_outbox_failed(pending[1].outbox_id, category="webhook_unavailable", retry_in_seconds=600)
    assert item.pending_outbox() == []
    clock.advance(minutes=11)
    again = item.pending_outbox()
    assert [(o.dedupe_key, o.attempts, o.last_error_category) for o in again] == [("b", 1, "webhook_unavailable")]


# --- locks --------------------------------------------------------------------------------------------------------------------------

def test_a_lock_has_one_holder_and_its_owner_may_renew(ledger):
    a, b = ledger(owner_id="a"), ledger(owner_id="b")
    a.acquire_lock("runner:global", ttl_seconds=600)
    with pytest.raises(lg.LockHeldError):
        b.acquire_lock("runner:global", ttl_seconds=600)
    a.acquire_lock("runner:global", ttl_seconds=600)  # re-entrant for its owner
    a.heartbeat("runner:global", ttl_seconds=600)
    with pytest.raises(lg.LockHeldError, match="lock_lost"):
        b.heartbeat("runner:global", ttl_seconds=600)
    b.release_lock("runner:global")  # not its lock: nothing happens
    assert a.get_lock("runner:global").owner_id == "a"
    a.release_lock("runner:global")
    b.acquire_lock("runner:global", ttl_seconds=600)


def test_different_clients_have_independent_locks_and_names_are_checked(ledger):
    a, b = ledger(owner_id="a"), ledger(owner_id="b")
    a.acquire_lock("client:one", ttl_seconds=600)
    b.acquire_lock("client:two", ttl_seconds=600)
    for bad in ("one", "client:" + "x" * 100, 5):
        with pytest.raises(lg.LedgerError, match="lock_name_invalid"):
            a.acquire_lock(bad, ttl_seconds=60)


def test_an_expired_lock_of_a_dead_holder_is_taken_over_but_a_live_holder_is_not(ledger):
    clock = Clock()
    holder = ledger(owner_id="old", clock=clock, pid=4242)
    taker = ledger(owner_id="new", clock=clock, pid_alive=lambda pid: False)
    careful = ledger(owner_id="careful", clock=clock, pid_alive=lambda pid: True)
    holder.acquire_lock("client:one", ttl_seconds=60)
    clock.advance(seconds=30)
    with pytest.raises(lg.LockHeldError):
        taker.acquire_lock("client:one", ttl_seconds=60)  # not expired yet
    clock.advance(minutes=5)
    with pytest.raises(lg.LockHeldError):
        careful.acquire_lock("client:one", ttl_seconds=60)  # expired, but the holder is still running
    taker.acquire_lock("client:one", ttl_seconds=60)
    assert taker.get_lock("client:one").owner_id == "new"
    assert ("lock", "lock_takeover") in [(e.kind, e.code) for e in taker.events_for()]


def test_a_lock_of_another_host_is_never_judged_stale_by_process_id(ledger):
    clock = Clock()
    remote = ledger(owner_id="r", clock=clock, host="other-machine")
    local = ledger(owner_id="l", clock=clock, host="this-machine", pid_alive=lambda pid: False)
    remote.acquire_lock("client:one", ttl_seconds=1)
    clock.advance(hours=1)
    with pytest.raises(lg.LockHeldError):
        local.acquire_lock("client:one", ttl_seconds=60)


def test_the_lock_context_manager_releases(ledger):
    item = ledger()
    with item.lock("client:one", ttl_seconds=60):
        assert item.get_lock("client:one") is not None
    assert item.get_lock("client:one") is None
    with pytest.raises(RuntimeError):
        with item.lock("client:one", ttl_seconds=60):
            raise RuntimeError("boom")
    assert item.get_lock("client:one") is None


def test_the_windows_safe_process_check_works_for_this_process():
    import os
    assert lg.pid_is_alive(os.getpid()) is True
    assert lg.pid_is_alive(2 ** 22 + 12345) in (True, False)  # an implausible id: never raises, never kills anything


# --- recovery -----------------------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("stuck,resume", [("collecting", "requested"), ("rendering", "requested"),
                                          ("finalizing", "rendered"), ("auditing", "finalized"),
                                          ("publishing", "audited")])
def test_a_run_left_in_progress_by_a_dead_process_is_moved_back_to_its_resume_state(ledger, stuck, resume):
    clock = Clock()
    dead = ledger(owner_id="dead", clock=clock)
    dead.create_run("example-co", PERIOD_ID)
    dead.acquire_lock("client:example-co", ttl_seconds=60)
    walk(dead, *c.PROGRESS_STATES[1:c.PROGRESS_STATES.index(stuck) + 1])
    clock.advance(hours=1)
    survivor = ledger(owner_id="alive", clock=clock, pid_alive=lambda pid: False)
    assert survivor.recover_interrupted() == [(KEY, stuck, resume)]
    run = survivor.get_run(KEY)
    assert run.state == resume and run.attempt_count == 2
    assert survivor.get_lock("client:example-co") is None
    previous = "succeeded" if stuck == "publishing" else "interrupted"  # an audited run's attempt was already closed
    assert [a.outcome for a in survivor.attempts_for(KEY)][-2:] == [previous, None]
    assert ("recovery", "interrupted_run_recovered") in [(e.kind, e.code) for e in survivor.events_for(KEY)]
    assert survivor.recover_interrupted() == []  # idempotent


def test_recovery_leaves_runs_alone_whose_owner_is_alive_or_is_this_process(ledger):
    clock = Clock()
    live = ledger(owner_id="live", clock=clock)
    live.create_run("example-co", PERIOD_ID)
    live.acquire_lock("client:example-co", ttl_seconds=600)
    walk(live, "collecting")
    other = ledger(owner_id="other", clock=clock)
    assert other.recover_interrupted() == []  # unexpired lease of another owner
    assert live.recover_interrupted() == []  # our own lock
    clock.advance(hours=1)
    assert other.recover_interrupted() == []  # expired, but the holder is still running (pid alive)
    assert other.get_run(KEY).state == "collecting"


def test_recovery_only_touches_in_progress_runs(ledger):
    item = ledger()
    item.create_run("example-co", PERIOD_ID)
    walk(item, "collecting", "rendering", "rendered")
    assert item.recover_interrupted() == [] and item.get_run(KEY).state == "rendered"


# --- deliveries (storage only; no upload exists in R8.3) ---------------------------------------------------------------------------------

def delivery(**changes):
    values = dict(delivery_key=f"{KEY}/{'9d8c7b6a5f4e3d2c'}/5a1b2c3d4e5f6a7b", run_key=KEY, pdf_sha256="9" * 64,
                  pdf_bytes=10, manifest_sha256="a" * 64, drive_folder_id="folder", drive_file_id="file",
                  file_name="a.pdf", uploaded_at="2026-11-02T01:07:40+00:00", recipient_set_hash16="5a1b2c3d4e5f6a7b")
    values.update(changes)
    return values


def test_a_delivery_key_is_unique_and_a_repeat_with_other_content_is_refused(ledger):
    item = ledger()
    item.create_run("example-co", PERIOD_ID)
    first = item.record_delivery(**delivery())
    assert first.upload_confirmed == 1 and item.get_delivery(first.delivery_key).file_name == "a.pdf"
    assert item.record_delivery(**delivery()).delivery_key == first.delivery_key  # idempotent
    with pytest.raises(lg.LedgerError, match="delivery_conflict"):
        item.record_delivery(**delivery(drive_file_id="another"))
    with pytest.raises(lg.LedgerError, match="delivery_conflict"):  # same report and recipients under a new key
        item.record_delivery(**delivery(delivery_key=f"{KEY}/x/y"))
    with pytest.raises(lg.LedgerError, match="delivery_conflict"):  # unknown run
        item.record_delivery(**delivery(delivery_key="k2", run_key="nobody/" + PERIOD_ID,
                                        recipient_set_hash16="ffffffffffffffff"))
