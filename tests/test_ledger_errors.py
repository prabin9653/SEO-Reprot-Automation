"""R8.4.1: transaction safety of the ledger and the way ledger errors reach a client's response and the batch.

Controlled fakes stand in for SQLite failures that cannot be produced on demand (a failing COMMIT or ROLLBACK); busy
and disk-full conditions are also produced for real with temporary databases. Nothing here touches production storage.
"""
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from seo_report import contracts
from seo_report import ledger as lg
from seo_report import pipeline, runner
from seo_report.sources import gsc as gsc_source
from test_pipeline import Clock, PERIOD_ID, failing_collect, go, paths, req, world  # noqa: F401

KEY = f"example-co/{PERIOD_ID}"
NAMES = ("Alpha Co", "Beta Co", "Gamma Co", "Delta Co")
KEYS = ("alpha-co", "beta-co", "gamma-co", "delta-co")


def operational(message, code=None):
    error = sqlite3.OperationalError(message)
    if code is not None:
        error.sqlite_errorcode = code
    return error


class FaultyConnection:
    """Delegates to a real connection but raises a chosen error for chosen statements."""

    def __init__(self, real, faults):
        self._real, self.faults = real, dict(faults)
        self.statements = []

    def execute(self, sql, *args):
        self.statements.append(sql)
        fault = self.faults.get(sql.split()[0].upper() if sql.split()[0].upper() in ("COMMIT", "ROLLBACK", "BEGIN") else sql)
        if fault is not None:
            raise fault
        return self._real.execute(sql, *args)

    def __getattr__(self, name):
        return getattr(self._real, name)


@pytest.fixture
def ledger(tmp_path):
    made = []

    def build(name="ledger.sqlite", **kwargs):
        kwargs.setdefault("clock", Clock())
        item = lg.Ledger(tmp_path / "ledger" / name, **kwargs)
        made.append(item)
        return item
    yield build
    for item in made:
        item.close()


def fresh_run_count(led):
    other = sqlite3.connect(led.path)
    try:
        return other.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
    finally:
        other.close()


# --- classification ---------------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("error,code", [
    (operational("database is locked", 5), "ledger_busy"), (operational("x", 6), "ledger_busy"),
    (operational("x", 5 | (1 << 8)), "ledger_busy"),  # extended result codes keep their primary code
    (operational("database or disk is full", 13), "ledger_disk_full"), (operational("disk I/O error", 10), "ledger_io_error"),
    (operational("x", 10 | (2 << 8)), "ledger_io_error"), (operational("database disk image is malformed", 11), "ledger_corrupt"),
    (operational("file is not a database", 26), "ledger_corrupt"), (operational("unable to open database file", 14), "ledger_unavailable"),
    (operational("attempt to write a readonly database", 8), "ledger_unavailable"),
    (operational("database is locked"), "ledger_busy"), (operational("database or disk is full"), "ledger_disk_full"),
    (operational("disk I/O error"), "ledger_io_error"), (operational("database disk image is malformed"), "ledger_corrupt"),
    (operational("unable to open database file"), "ledger_unavailable"),
    (operational("no such table: runs"), "ledger_error"), (sqlite3.DatabaseError("something else"), "ledger_error"),
    (sqlite3.IntegrityError("UNIQUE constraint failed"), "ledger_constraint_failed"),
    (sqlite3.ProgrammingError("Cannot operate on a closed database."), "ledger_connection_unusable")])
def test_sqlite_errors_are_classified_by_result_code_then_text(error, code):
    assert lg.classify_sqlite_error(error) == code
    wrapped = lg.wrap_sqlite_error(error)
    assert wrapped.code == code and str(wrapped) == code  # a fixed code, never SQL or database text


def test_the_error_flags_separate_transient_storage_and_logical_errors():
    busy, full, io = lg.LedgerError("ledger_busy"), lg.LedgerError("ledger_disk_full"), lg.LedgerError("ledger_io_error")
    assert (busy.storage, busy.transient) == (True, True)
    for fatal in (full, io, lg.LedgerError("ledger_corrupt"), lg.LedgerError("ledger_unavailable"),
                  lg.LedgerError("ledger_connection_unusable"), lg.LedgerError("ledger_error")):
        assert (fatal.storage, fatal.transient) == (True, False), fatal.code
    for logical in (lg.StaleStateError("stale_state"), lg.LockHeldError("lock_lost"), lg.LedgerError("run_exists"),
                    lg.LedgerError("idempotency_key_reused"), lg.InvalidTransitionError("transition_not_allowed")):
        assert (logical.storage, logical.transient) == (False, False), logical.code
    assert lg.LedgerError("custom", storage=True, transient=True).transient is True


# --- real SQLite conditions -------------------------------------------------------------------------------------------------------------

def test_a_busy_database_is_a_transient_classified_error_and_the_connection_stays_clean(ledger):
    holder, waiter = ledger(), ledger(busy_timeout=0.05)
    holder._con.execute("BEGIN IMMEDIATE")  # another process is writing
    with pytest.raises(lg.LedgerError) as info:
        waiter.create_run("example-co", PERIOD_ID)
    assert info.value.code == "ledger_busy" and info.value.transient and info.value.storage
    assert isinstance(info.value.__cause__, sqlite3.OperationalError)  # the chain is kept for diagnosis
    assert waiter._con.in_transaction is False and not waiter._unusable
    holder._con.execute("ROLLBACK")
    assert waiter.create_run("example-co", PERIOD_ID).state == "requested"  # the same connection works afterwards


def test_a_full_database_is_a_permanent_classified_error_and_nothing_is_half_written(ledger):
    item = ledger()
    item.create_run("example-co", PERIOD_ID)
    pages = item._con.execute("PRAGMA page_count").fetchone()[0]
    item._con.execute(f"PRAGMA max_page_count={pages}")
    with pytest.raises(lg.LedgerError) as info:
        item.enqueue_outbox(destination="make_status", payload={"x": "y" * 500_000}, dedupe_key="big")
    assert info.value.code == "ledger_disk_full" and info.value.storage and not info.value.transient
    assert isinstance(info.value.__cause__, sqlite3.OperationalError)
    assert item._con.in_transaction is False and item.all_outbox() == []
    item._con.execute("PRAGMA max_page_count=1000000")
    item.enqueue_outbox(destination="make_status", payload={"x": 1}, dedupe_key="small")  # recovered
    assert len(item.all_outbox()) == 1


def test_a_sqlite_error_from_the_body_is_wrapped_rolled_back_and_chained(ledger):
    item = ledger()
    with pytest.raises(lg.LedgerError) as info:
        with item._tx():
            item._con.execute("INSERT INTO locks VALUES ('client:x','o',1,'h','t','t','t')")
            item._con.execute("INSERT INTO missing_table VALUES (1)")
    assert info.value.code == "ledger_error" and isinstance(info.value.__cause__, sqlite3.OperationalError)
    assert item._con.in_transaction is False and item.get_lock("client:x") is None


def test_reads_are_wrapped_too(ledger):
    item = ledger()
    item._con = FaultyConnection(item._con, {"SELECT * FROM runs WHERE run_key = ?": operational("disk image malformed", 11)})
    with pytest.raises(lg.LedgerError) as info:
        item.get_run(KEY)
    assert info.value.code == "ledger_corrupt"
    item.close()
    with pytest.raises(lg.LedgerError, match="ledger_connection_unusable"):
        item.list_runs()


# --- COMMIT and ROLLBACK failures -------------------------------------------------------------------------------------------------------

def test_a_failed_commit_is_rolled_back_classified_and_the_connection_stays_usable(ledger):
    item = ledger()
    real = item._con
    proxy = FaultyConnection(real, {"COMMIT": operational("disk I/O error", 10)})
    item._con = proxy
    with pytest.raises(lg.LedgerError) as info:
        item.create_run("example-co", PERIOD_ID)
    assert info.value.code == "ledger_io_error" and not info.value.transient
    assert isinstance(info.value.__cause__, sqlite3.OperationalError) and "disk I/O error" in str(info.value.__cause__)
    assert real.in_transaction is False and not item._unusable  # no transaction is left open
    assert "ROLLBACK" in proxy.statements and fresh_run_count(item) == 0  # nothing was committed
    proxy.faults.clear()  # the disk recovered
    assert item.create_run("example-co", PERIOD_ID).state == "requested" and fresh_run_count(item) == 1


def test_a_failed_commit_followed_by_a_failed_rollback_keeps_the_original_error_and_disables_the_connection(ledger):
    item = ledger()
    real = item._con
    proxy = FaultyConnection(real, {"COMMIT": operational("disk I/O error", 10),
                                    "ROLLBACK": operational("database is locked", 5)})
    item._con = proxy
    with pytest.raises(lg.LedgerError) as info:
        item.create_run("example-co", PERIOD_ID)
    assert info.value.code == "ledger_io_error"  # the COMMIT failure, not the rollback failure
    assert "disk I/O error" in str(info.value.__cause__)
    assert any("rollback failed" in note for note in info.value.__cause__.__notes__)
    assert item._unusable is True
    with pytest.raises(sqlite3.ProgrammingError):  # the connection was closed: SQLite discarded the open transaction
        real.execute("SELECT 1")
    with pytest.raises(lg.LedgerError, match="ledger_connection_unusable"):
        item.create_run("other-co", PERIOD_ID)  # further writes are refused, not silently attempted
    other = lg.Ledger(item.path, clock=Clock())  # a new connection sees a consistent database without the run
    try:
        assert other.list_runs() == [] and other.integrity_check()
    finally:
        other.close()


def test_a_failing_rollback_after_a_body_error_does_not_replace_that_error(ledger):
    item = ledger()
    item._con = FaultyConnection(item._con, {"ROLLBACK": operational("disk I/O error", 10)})
    with pytest.raises(ValueError, match="the original problem") as info:
        with item._tx():
            item._con.execute("INSERT INTO locks VALUES ('client:x','o',1,'h','t','t','t')")
            raise ValueError("the original problem")
    assert any("rollback failed" in note for note in info.value.__notes__) and item._unusable


def test_begin_failures_start_nothing_and_are_classified(ledger):
    item = ledger()
    proxy = FaultyConnection(item._con, {"BEGIN": operational("disk I/O error", 10)})
    item._con = proxy
    with pytest.raises(lg.LedgerError) as info:
        item.create_run("example-co", PERIOD_ID)
    assert info.value.code == "ledger_io_error" and proxy.in_transaction is False and not item._unusable


@pytest.mark.parametrize("exception", [KeyboardInterrupt, SystemExit])
def test_interrupts_roll_back_and_propagate_unchanged(ledger, exception):
    item = ledger()
    with pytest.raises(exception):
        with item._tx():
            item._con.execute("INSERT INTO locks VALUES ('client:x','o',1,'h','t','t','t')")
            raise exception()
    assert item._con.in_transaction is False and item.get_lock("client:x") is None and not item._unusable
    assert item.create_run("example-co", PERIOD_ID).state == "requested"


def test_a_non_sqlite_error_in_the_body_rolls_back_and_is_not_wrapped(ledger):
    item = ledger()
    with pytest.raises(ZeroDivisionError):
        with item._tx():
            item._con.execute("INSERT INTO locks VALUES ('client:x','o',1,'h','t','t','t')")
            1 / 0
    assert item.get_lock("client:x") is None and item._con.in_transaction is False


def test_opening_a_damaged_or_unreadable_file_is_a_classified_error(tmp_path):
    bad = tmp_path / "ledger" / "bad.sqlite"
    bad.parent.mkdir()
    bad.write_bytes(b"this is not a sqlite database " * 50)
    with pytest.raises(lg.LedgerError) as info:
        lg.Ledger(bad, clock=Clock())
    assert info.value.code in ("ledger_corrupt", "ledger_unavailable") and info.value.storage
    assert bad.read_bytes().startswith(b"this is not")  # never touched


# --- a client's run: conflicts concern that client only ---------------------------------------------------------------------------------

def stale_on(w, to_state):
    real = w.ledger.transition

    def transition(run_key, to, **kw):
        if to == to_state:
            raise lg.StaleStateError("stale_state")
        return real(run_key, to, **kw)
    w.ledger.transition = transition


def test_stale_state_in_one_run_is_a_client_level_answer_not_a_ledger_outage(world):
    w = world()
    stale_on(w, "rendering")
    request = req()
    response = go(w, request)
    assert (response.outcome, response.error["category"], response.error["code"]) == (
        "rejected", "run_in_progress", "stale_state")
    contracts.validate_response(response.to_dict())
    assert response.state == "collecting" and response.error["stage"] == "collecting"
    run = w.ledger.get_run(KEY)
    assert run.state == "collecting" and run.error_category is None  # not marked failed: someone else moved it
    assert w.ledger.get_request(request.request_id).status == "accepted"  # the same key can be tried again
    reports, reviewer, _ = paths(w)
    assert not any(reports.iterdir()) and not w.settings.history_db_path.exists()  # the run did not continue
    assert w.ledger.get_lock("client:example-co") is None and w.ledger.get_lock("runner:global") is None


def test_the_run_is_picked_up_again_by_recovery_after_a_stale_state_stop(world):
    w = world()
    stale_on(w, "rendering")
    request = req()
    assert go(w, request).outcome == "rejected"
    del w.ledger.transition  # the instance attribute shadowing the method goes away
    again = go(w, request)
    assert again.state == "audited" and again.outcome == "succeeded"
    assert w.ledger.get_run(KEY).attempt_count == 2  # the stuck attempt was closed as interrupted


def test_losing_the_client_lock_stops_that_run_at_once(world):
    w = world()
    calls = []
    real = w.ledger.heartbeat

    def heartbeat(name, **kw):
        calls.append(name)
        if len(calls) >= 3:  # the lease of the client lock was taken over mid-run
            raise lg.LockHeldError("lock_lost")
        return real(name, **kw)
    w.ledger.heartbeat = heartbeat
    response = go(w)
    assert (response.outcome, response.error["category"], response.error["code"]) == ("rejected", "run_in_progress", "lock_lost")
    run = w.ledger.get_run(KEY)
    assert run.state not in ("finalizing", "finalized", "auditing", "audited")  # not continued after the loss
    assert run.state in ("collecting", "rendering", "rendered") and run.error_category is None
    assert w.ledger.get_lock("client:example-co") is None  # our own lock is still released


def test_a_busy_ledger_gives_a_retryable_unrecorded_answer(world):
    w = world()

    def busy(request):
        raise lg.LedgerError("ledger_busy")
    w.ledger.register_request = busy
    response = go(w)
    assert (response.outcome, response.error["category"], response.error["code"]) == (
        "rejected", "lock_unavailable", "ledger_busy")
    assert w.ledger.list_runs() == [] and not w.settings.output_root.exists()


def test_a_ledger_that_cannot_be_used_still_raises_with_its_classification(world):
    w = world()

    def broken(request):
        raise lg.LedgerError("ledger_io_error")
    w.ledger.register_request = broken
    with pytest.raises(lg.LedgerError) as info:
        go(w)
    assert info.value.code == "ledger_io_error"


# --- recording a failure never hides the original failure ---------------------------------------------------------------------------------

def fail_on_failure(w, error):
    real = w.ledger.transition

    def transition(run_key, to, **kw):
        if to in ("failed", "blocked"):
            raise error
        return real(run_key, to, **kw)
    w.ledger.transition = transition


def test_a_failure_that_cannot_be_recorded_is_still_reported_as_the_original_failure(world):
    w = world(collect=failing_collect(gsc_source.SearchConsoleError("x", 403)))
    fail_on_failure(w, lg.LedgerError("ledger_busy"))
    response = go(w)
    assert (response.outcome, response.error["category"], response.error["code"]) == (
        "failed", "source_denied", "gsc_http_403")
    assert response.error["stage"] == "collecting" and w.ledger.get_run(KEY).state == "collecting"
    contracts.validate_response(response.to_dict())


def test_an_unexpected_error_whose_recording_fails_keeps_the_original_code_and_the_logged_traceback(world, caplog):
    def collect(client_run, today):
        raise ValueError("a bug")
    w = world(collect=collect)
    fail_on_failure(w, lg.StaleStateError("stale_state"))
    with caplog.at_level("ERROR", logger="seo_report.pipeline"):
        response = go(w)
    assert (response.outcome, response.error["category"], response.error["code"]) == (
        "failed", "internal_error", "exception_value_error")
    assert any(r.exc_info and "a bug" in str(r.exc_info[1]) for r in caplog.records)


def test_when_the_storage_dies_while_recording_the_original_failure_is_the_cause(world):
    w = world(collect=failing_collect(gsc_source.SearchConsoleError("x", 403)))
    fail_on_failure(w, lg.LedgerError("ledger_disk_full"))
    with pytest.raises(lg.LedgerError) as info:
        go(w)
    assert info.value.code == "ledger_disk_full"
    assert isinstance(info.value.__cause__, contracts.StageError) and info.value.__cause__.code == "gsc_http_403"
    assert any("source_denied/gsc_http_403" in note for note in info.value.__notes__)


def test_the_same_holds_for_an_unexpected_error(world):
    def collect(client_run, today):
        raise ValueError("a bug")
    w = world(collect=collect)
    fail_on_failure(w, lg.LedgerError("ledger_io_error"))
    with pytest.raises(lg.LedgerError) as info:
        go(w)
    assert isinstance(info.value.__cause__, ValueError) and any("exception_value_error" in n for n in info.value.__notes__)


def test_a_response_that_cannot_be_stored_does_not_lose_a_finished_run(world):
    w = world()
    real = w.ledger.finish_request

    def finish(request_id, **kw):
        raise lg.LedgerError("ledger_busy")
    w.ledger.finish_request = finish
    request = req()
    response = go(w, request)
    assert response.state == "audited" and w.ledger.get_run(KEY).state == "audited"
    w.ledger.finish_request = real
    again = go(w, request)  # the same key is admitted again and answered from the finished run
    assert again.outcome == "already_complete" and again.state == "audited"


def test_interrupts_during_a_client_run_still_propagate(world):
    for exception in (KeyboardInterrupt, SystemExit):
        def collect(client_run, today, exception=exception):
            raise exception()
        w = world(collect=collect)
        fail_on_failure(w, lg.LedgerError("ledger_busy"))
        with pytest.raises(exception):
            go(w)


# --- the batch -----------------------------------------------------------------------------------------------------------------------------

def batch(w, *keys, **kw):
    items = [req(key, when=f"2026-11-02T00:5{i}:00+00:00") for i, key in enumerate(keys)]
    return runner.run_batch(items, services=w.services, ledger=w.ledger, **kw), items


def patch_register(w, bad_key, error):
    real = w.ledger.register_request

    def register(request):
        if request.client_key == bad_key:
            raise error
        return real(request)
    w.ledger.register_request = register


def test_a_client_level_conflict_does_not_stop_the_other_clients(world):
    w = world(names=NAMES)
    real = w.ledger.transition

    def transition(run_key, to, **kw):
        if run_key.startswith("beta-co") and to == "rendering":
            raise lg.StaleStateError("stale_state")
        return real(run_key, to, **kw)
    w.ledger.transition = transition
    outcome, _ = batch(w, *KEYS)
    assert not outcome.aborted and outcome.not_started == ()
    assert [(r.client_key, r.outcome) for r in outcome.results] == [
        ("alpha-co", "succeeded"), ("beta-co", "rejected"), ("gamma-co", "succeeded"), ("delta-co", "succeeded")]
    assert outcome.results[1].error["code"] == "stale_state"
    outcome.summary()


def test_a_lost_lock_in_one_client_does_not_stop_the_others(world):
    w = world(names=NAMES)
    real = w.ledger.heartbeat

    def heartbeat(name, **kw):
        if name == "client:gamma-co":
            raise lg.LockHeldError("lock_lost")
        return real(name, **kw)
    w.ledger.heartbeat = heartbeat
    outcome, _ = batch(w, *KEYS)
    assert [(r.client_key, r.outcome) for r in outcome.results] == [
        ("alpha-co", "succeeded"), ("beta-co", "succeeded"), ("gamma-co", "rejected"), ("delta-co", "succeeded")]
    assert outcome.results[2].error["code"] == "lock_lost" and not outcome.aborted


def test_a_logical_ledger_error_for_one_client_is_a_failed_response_and_the_batch_continues(world):
    w = world(names=NAMES)
    patch_register(w, "beta-co", lg.LedgerError("request_id_conflict"))
    outcome, _ = batch(w, *KEYS)
    assert not outcome.aborted and [r.outcome for r in outcome.results] == ["succeeded", "failed", "succeeded", "succeeded"]
    beta = outcome.results[1]
    assert (beta.error["category"], beta.error["code"]) == ("internal_error", "request_id_conflict")
    contracts.validate_batch_summary(outcome.summary())


@pytest.mark.parametrize("code,reason", [("ledger_io_error", "ledger_unavailable"), ("ledger_disk_full", "disk_full"),
                                         ("ledger_corrupt", "ledger_unavailable"), ("ledger_unavailable", "ledger_unavailable"),
                                         ("ledger_connection_unusable", "ledger_unavailable")])
def test_a_genuinely_unusable_ledger_aborts_the_batch_with_the_right_reason(world, code, reason):
    w = world(names=NAMES)
    patch_register(w, "beta-co", lg.LedgerError(code))
    outcome, items = batch(w, *KEYS)
    assert outcome.aborted and outcome.abort_reason == reason
    assert [r.client_key for r in outcome.results] == ["alpha-co"]
    assert outcome.not_started == tuple(i.request_id for i in items[1:])  # the failing request is not marked as started
    assert w.ledger.get_lock("runner:global") is None
    outcome.summary()


def test_a_busy_ledger_for_one_client_does_not_stop_the_batch(world):
    w = world(names=NAMES)
    patch_register(w, "beta-co", lg.LedgerError("ledger_busy"))
    outcome, _ = batch(w, *KEYS)
    assert not outcome.aborted and [r.outcome for r in outcome.results] == ["succeeded", "rejected", "succeeded", "succeeded"]
    assert outcome.results[1].error["code"] == "ledger_busy"


def test_a_ledger_that_stays_busy_eventually_stops_the_batch(world):
    w = world(names=NAMES + ("Echo Co",))

    def busy(request):
        raise lg.LedgerError("ledger_busy")
    w.ledger.register_request = busy
    outcome, items = batch(w, *KEYS, "echo-co")
    assert outcome.aborted and outcome.abort_reason == "ledger_unavailable"
    assert len(outcome.results) == runner.MAX_CONSECUTIVE_LEDGER_BUSY == 3
    assert len(outcome.not_started) == 2 and all(r.error["code"] == "ledger_busy" for r in outcome.results)


def test_a_success_between_busy_answers_resets_the_streak(world):
    w = world(names=NAMES + ("Echo Co",))
    real = w.ledger.register_request

    def register(request):
        if request.client_key in ("alpha-co", "beta-co", "delta-co", "echo-co"):
            raise lg.LedgerError("ledger_busy")
        return real(request)
    w.ledger.register_request = register
    outcome, _ = batch(w, *KEYS, "echo-co")
    assert not outcome.aborted and [r.outcome for r in outcome.results] == ["rejected", "rejected", "succeeded", "rejected", "rejected"]


def test_a_failure_that_cannot_be_recorded_still_shows_its_original_category_in_the_batch(world):
    w = world(names=("Alpha Co", "Beta Co"), collect=failing_collect(gsc_source.SearchConsoleError("x", 503)))
    fail_on_failure(w, lg.LedgerError("ledger_busy"))
    outcome, _ = batch(w, "alpha-co", "beta-co")
    assert not outcome.aborted
    assert [(r.error["category"], r.error["code"]) for r in outcome.results] == [("source_temporary", "gsc_http_503")] * 2


def test_storage_dying_while_recording_aborts_the_batch_without_losing_the_cause(world):
    w = world(names=("Alpha Co", "Beta Co"), collect=failing_collect(gsc_source.SearchConsoleError("x", 403)))
    fail_on_failure(w, lg.LedgerError("ledger_disk_full"))
    outcome, items = batch(w, "alpha-co", "beta-co")
    assert outcome.aborted and outcome.abort_reason == "disk_full" and outcome.results == ()
    assert outcome.not_started == tuple(i.request_id for i in items)


def test_batch_start_up_errors_are_classified(world):
    w = world(names=NAMES)
    real = w.ledger.acquire_lock

    def acquire(name, **kw):
        raise lg.LedgerError("ledger_disk_full")
    w.ledger.acquire_lock = acquire
    outcome, items = batch(w, *KEYS)
    assert outcome.aborted and outcome.abort_reason == "disk_full" and outcome.results == ()
    w.ledger.acquire_lock = real
    w.ledger.recover_interrupted = lambda: (_ for _ in ()).throw(lg.LedgerError("ledger_io_error"))
    outcome, _ = batch(w, *KEYS)
    assert outcome.aborted and outcome.abort_reason == "ledger_unavailable" and w.ledger.get_lock("runner:global") is None


def test_batch_interrupts_propagate_and_release_the_lock_even_with_a_failing_ledger(world):
    for exception in (KeyboardInterrupt, SystemExit):
        w = world(names=("Alpha Co",))

        def register(request, exception=exception):
            raise exception()
        w.ledger.register_request = register
        with pytest.raises(exception):
            batch(w, "alpha-co")
        assert w.ledger.get_lock("runner:global") is None


def test_the_abort_reasons_used_are_in_the_closed_contract():
    assert {"ledger_unavailable", "disk_full"} <= set(contracts.ABORT_REASONS)
    for category in ("run_in_progress", "lock_unavailable", "internal_error"):
        assert category in contracts.ERROR_CATEGORIES
    response = pipeline.build_response(req(), None, outcome="rejected", updated_at="2026-11-02T01:00:00+00:00",
                                       error=contracts.error_info("lock_unavailable", "ledger_busy", "intake"))
    contracts.validate_response(response.to_dict())
