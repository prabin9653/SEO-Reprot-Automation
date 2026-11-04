"""R8.4.2: ledger schema version 2 (generations, links, acknowledgement) and the migration from version 1."""
import datetime as dt
import json
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from seo_report import contracts
from seo_report import ledger as lg

PERIOD = "2026-09-29_2026-10-26"
KEY = f"example-co/{PERIOD}"
UTC = dt.timezone.utc


class Clock:
    def __init__(self):
        self.now = dt.datetime(2026, 11, 2, 1, 0, 0, tzinfo=UTC)

    def __call__(self):
        self.now += dt.timedelta(seconds=1)
        return self.now


@pytest.fixture
def ledger(tmp_path):
    opened = lg.Ledger(tmp_path / "ledger.sqlite", clock=Clock())
    yield opened
    opened.close()


def blocked_run(ledger, *, category="audit_failed", code="x", stage="auditing"):
    run = ledger.create_run("example-co", PERIOD)
    run = ledger.transition(run.run_key, "collecting", expected_state="requested", expected_version=0, code="c")
    return ledger.transition(run.run_key, "blocked", expected_state="collecting", expected_version=run.state_version,
                             code="b", values={"failed_stage": stage, "error_category": category, "error_code": code})


# --- version 1 as it was shipped before this stage -------------------------------------------------------------------------

V1_REQUESTS = """CREATE TABLE requests (
      request_id TEXT PRIMARY KEY CHECK (length(request_id) = 36),
      idempotency_key TEXT NOT NULL UNIQUE CHECK (length(idempotency_key) BETWEEN 1 AND 128),
      client_key TEXT NOT NULL,
      period_id TEXT NOT NULL CHECK (period_id GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]_[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
      action TEXT NOT NULL CHECK (action IN ('validate','dry_run','run','retry','publish','status')),
      requested_by TEXT NOT NULL, requested_at TEXT NOT NULL, received_at TEXT NOT NULL,
      options_json TEXT NOT NULL DEFAULT '{}',
      status TEXT NOT NULL CHECK (status IN ('accepted','rejected','completed','replayed_only')),
      reject_code TEXT, run_key TEXT REFERENCES runs(run_key), response_json TEXT, responded_at TEXT)"""

V1_RUNS = """CREATE TABLE runs (
      run_key TEXT PRIMARY KEY, client_key TEXT NOT NULL, period_start TEXT NOT NULL, period_end TEXT NOT NULL,
      state TEXT NOT NULL CHECK (state IN ('requested','collecting','rendering','rendered','finalizing','finalized',
                          'auditing','audited','publishing','ready_for_delivery','delivered','failed','blocked','cancelled')),
      state_version INTEGER NOT NULL DEFAULT 0, attempt_count INTEGER NOT NULL DEFAULT 1, failed_stage TEXT,
      error_category TEXT, error_code TEXT, retry_eligible INTEGER NOT NULL DEFAULT 0 CHECK (retry_eligible IN (0,1)),
      retry_kind TEXT, retry_after TEXT, generation_date TEXT, finalized_at TEXT,
      replace_existing INTEGER NOT NULL DEFAULT 0 CHECK (replace_existing IN (0,1)),
      render_input_sha256 TEXT, render_format_name TEXT, render_format_version INTEGER,
      engine_run_id TEXT, engine_revision INTEGER, pdf_sha256 TEXT, pdf_bytes INTEGER, record_name TEXT,
      audit_outcome TEXT, audit_finalized_valid INTEGER, audit_verification_complete INTEGER, audited_at TEXT,
      mode TEXT NOT NULL DEFAULT 'production' CHECK (mode = 'production'),
      created_at TEXT NOT NULL, updated_at TEXT NOT NULL, finished_at TEXT,
      UNIQUE (client_key, period_start, period_end),
      CHECK (period_start <= period_end),
      CHECK (run_key = client_key || '/' || period_start || '_' || period_end))"""


def make_v1(path):
    """A genuine version 1 database: the version 2 statements for every other table, the old tables for these two."""
    con = sqlite3.connect(path, isolation_level=None)
    for statement in lg._DDL:
        head = statement.lstrip()
        if (head.startswith("CREATE TABLE requests") or head.startswith("CREATE TABLE runs") or "ON runs" in head
                or "ON requests" in head):
            continue
        con.execute(statement)
    con.execute(V1_REQUESTS)
    con.execute(V1_RUNS)
    con.execute("CREATE INDEX requests_client_period ON requests(client_key, period_id)")
    con.execute("CREATE INDEX runs_state ON runs(state)")
    con.execute("CREATE TRIGGER runs_no_delete BEFORE DELETE ON runs BEGIN SELECT RAISE(ABORT, 'ledger runs are never deleted'); END")
    now = "2026-11-02T01:00:00+00:00"
    con.execute("INSERT INTO runs(run_key, client_key, period_start, period_end, state, state_version, attempt_count, "
                "generation_date, finalized_at, render_input_sha256, pdf_sha256, pdf_bytes, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (KEY, "example-co", "2026-09-29", "2026-10-26", "audited", 7, 2, "2026-11-02", now, "a" * 64, "b" * 64,
                 10, now, now))
    con.execute("INSERT INTO requests(request_id, idempotency_key, client_key, period_id, action, requested_by, requested_at, "
                "received_at, options_json, status, run_key, response_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                ("00000000-0000-0000-0000-000000000001", "k1", "example-co", PERIOD, "run", "make:x", now, now,
                 '{"replace_existing":false}', "completed", KEY, "{}"))
    con.execute("INSERT INTO run_attempts(run_key, attempt_no, started_at, start_state) VALUES (?,1,?,'requested')", (KEY, now))
    con.execute("INSERT INTO events(at, run_key, kind, code) VALUES (?,?,'state_change','run_created')", (now, KEY))
    con.execute("INSERT INTO outbox(created_at, run_key, destination, dedupe_key, payload_json, next_attempt_at) "
                "VALUES (?,?,'make_status','d1','{}',?)", (now, KEY, now))
    con.execute("PRAGMA user_version=1")
    con.close()


def dump(path):
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    try:
        return {t: [dict(r) for r in con.execute(f"SELECT * FROM {t}")] for t in
                ("runs", "requests", "run_attempts", "events", "outbox")}
    finally:
        con.close()


# --- migration ---------------------------------------------------------------------------------------------------------------

def test_a_fresh_ledger_is_version_2_with_the_new_columns(ledger):
    assert ledger._con.execute("PRAGMA user_version").fetchone()[0] == lg.SCHEMA_VERSION == 2
    columns = {r[1] for r in ledger._con.execute("PRAGMA table_info(runs)")}
    assert {"generation", "supersedes_run_key", "link_kind", "link_operator", "link_reason", "acknowledged", "ack_operator",
            "ack_reason", "ack_at"} <= columns


def test_a_version_1_ledger_is_migrated_with_every_row_preserved(tmp_path):
    path = tmp_path / "old.sqlite"
    make_v1(path)
    before = dump(path)
    ledger = lg.Ledger(path, clock=Clock())
    try:
        assert ledger._con.execute("PRAGMA user_version").fetchone()[0] == 2
        after = dump(path)
        for table in ("requests", "run_attempts", "events", "outbox"):
            assert after[table] == before[table], table
        old, new = before["runs"][0], after["runs"][0]
        assert {k: v for k, v in new.items() if k in old} == old
        assert (new["generation"], new["supersedes_run_key"], new["link_kind"], new["acknowledged"]) == (1, None, None, 0)
        assert ledger.integrity_check() and ledger._con.execute("PRAGMA foreign_key_check").fetchall() == []
        assert ledger._con.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        run = ledger.get_run(KEY)
        assert (run.state, run.state_version, run.attempt_count) == ("audited", 7, 2)
        # the rebuilt tables keep their protections and gain the new ones
        with pytest.raises(sqlite3.IntegrityError):
            ledger._con.execute("DELETE FROM runs WHERE run_key = ?", (KEY,))
        with pytest.raises(sqlite3.IntegrityError):
            ledger._con.execute("UPDATE runs SET generation_date = '2027-01-01' WHERE run_key = ?", (KEY,))
        second = ledger.create_run("example-co", PERIOD, supersedes=KEY, link_kind="supersede", operator="ops:a",
                                   reason="content_error")
        assert second.run_key == KEY + "~2" and second.generation == 2
    finally:
        ledger.close()
    reopened = lg.Ledger(path, clock=Clock())  # a second open changes nothing further
    try:
        assert len(reopened.runs_for_period("example-co", PERIOD)) == 2
    finally:
        reopened.close()


def test_a_read_only_open_of_a_version_1_ledger_is_refused_not_migrated(tmp_path):
    path = tmp_path / "old.sqlite"
    make_v1(path)
    with pytest.raises(lg.LedgerError) as caught:
        lg.Ledger(path, clock=Clock(), readonly=True)
    assert caught.value.code == "ledger_schema_unsupported"
    assert sqlite3.connect(path).execute("PRAGMA user_version").fetchone()[0] == 1


def test_a_failed_migration_changes_nothing(tmp_path):
    path = tmp_path / "old.sqlite"
    make_v1(path)
    con = sqlite3.connect(path, isolation_level=None)
    con.execute("PRAGMA foreign_keys=OFF")
    con.execute("UPDATE requests SET run_key = 'example-co/2025-01-01_2025-01-28'")  # a dangling reference
    con.close()
    before = dump(path)
    with pytest.raises(lg.LedgerError) as caught:
        lg.Ledger(path, clock=Clock())
    assert caught.value.code == 'ledger_migration_failed'
    check = sqlite3.connect(path)
    assert check.execute("PRAGMA user_version").fetchone()[0] == 1
    check.close()
    assert dump(path) == before


def test_an_unknown_future_version_is_still_refused(tmp_path):
    path = tmp_path / "future.sqlite"
    first = lg.Ledger(path, clock=Clock())
    first.close()
    con = sqlite3.connect(path)
    con.execute("PRAGMA user_version=3")
    con.commit()
    con.close()
    with pytest.raises(lg.LedgerError) as caught:
        lg.Ledger(path, clock=Clock())
    assert caught.value.code == "ledger_schema_unsupported"


# --- generations and links -------------------------------------------------------------------------------------------------------

def test_generation_one_keeps_the_original_key_and_later_generations_add_a_suffix(ledger):
    first = ledger.create_run("example-co", PERIOD)
    assert (first.run_key, first.generation) == (KEY, 1)
    ledger.transition(KEY, "cancelled", expected_state="requested", expected_version=0, code="x")
    second = ledger.create_run("example-co", PERIOD, supersedes=KEY, link_kind="restart", operator="ops:a",
                               reason="no_longer_needed")
    assert (second.run_key, second.generation, second.supersedes_run_key, second.link_kind) == (KEY + "~2", 2, KEY, "restart")
    assert ledger.latest_run("example-co", PERIOD).run_key == KEY + "~2"
    assert [r.generation for r in ledger.runs_for_period("example-co", PERIOD)] == [1, 2]
    assert ledger.get_run(KEY).state == "cancelled" and ledger.get_run(KEY).state_version == 1


def test_link_arguments_are_validated(ledger):
    ledger.create_run("example-co", PERIOD)
    for kwargs in ({"supersedes": KEY}, {"link_kind": "supersede"}, {"supersedes": KEY, "link_kind": "other"}):
        with pytest.raises(lg.LedgerError) as caught:
            ledger.create_run("example-co", PERIOD, **kwargs)
        assert caught.value.code == "link_invalid"
    with pytest.raises(lg.LedgerError) as caught:
        ledger.create_run("example-co", PERIOD, supersedes=KEY + "~9", link_kind="supersede")
    assert caught.value.code == "run_not_latest"
    with pytest.raises(lg.LedgerError) as caught:
        ledger.create_run("example-co", PERIOD)  # generation 1 exists
    assert caught.value.code == "run_exists"


def test_the_database_itself_rejects_inconsistent_generations(ledger):
    ledger.create_run("example-co", PERIOD)
    insert = ("INSERT INTO runs(run_key, client_key, period_start, period_end, generation, supersedes_run_key, link_kind, "
              "state, created_at, updated_at) VALUES (?, 'example-co', '2026-09-29', '2026-10-26', ?, ?, ?, 'requested', 'n', 'n')")
    for args in ((KEY + "~2", 2, None, None),          # generation 2 without a link
                 (KEY, 1, KEY, "supersede"),            # generation 1 with a link
                 (KEY + "~3", 2, KEY, "supersede"),     # key does not match the generation
                 (KEY + "~2", 2, KEY, None)):           # link without a kind
        with pytest.raises(sqlite3.IntegrityError):
            ledger._con.execute(insert, args)


# --- acknowledgement and transitions ---------------------------------------------------------------------------------------------

def test_acknowledge_requires_a_blocked_run_an_operator_and_a_reason(ledger):
    run = ledger.create_run("example-co", PERIOD)
    with pytest.raises(lg.InvalidTransitionError):
        ledger.acknowledge(KEY, expected_state="requested", expected_version=0, operator="ops:a", reason="operator_decision")
    ledger.transition(KEY, "collecting", expected_state="requested", expected_version=0, code="c")
    run = ledger.transition(KEY, "blocked", expected_state="collecting", expected_version=1, code="b",
                            values={"failed_stage": "auditing", "error_category": "audit_failed", "error_code": "x"})
    for operator, reason in ((None, "operator_decision"), ("ops:a", "free text"), ("", "operator_decision")):
        with pytest.raises(lg.LedgerError) as caught:
            ledger.acknowledge(KEY, expected_state="blocked", expected_version=run.state_version, operator=operator,
                               reason=reason)
        assert caught.value.code == "operator_details_required"
    with pytest.raises(lg.StaleStateError):
        ledger.acknowledge(KEY, expected_state="blocked", expected_version=run.state_version - 1, operator="ops:a",
                           reason="operator_decision")
    assert ledger.get_run(KEY).acknowledged == 0


def test_acknowledgement_ends_with_any_transition_and_is_never_a_state_change(ledger):
    run = blocked_run(ledger)
    acknowledged, changed = ledger.acknowledge(KEY, expected_state="blocked", expected_version=run.state_version,
                                               operator="ops:a", reason="audit_finding_reviewed")
    assert changed and acknowledged.state == "blocked" and acknowledged.acknowledged == 1
    assert acknowledged.state_version == run.state_version + 1 and acknowledged.attempt_count == run.attempt_count
    moved = ledger.transition(KEY, "finalized", expected_state="blocked", expected_version=acknowledged.state_version,
                              code="r", new_attempt=True)
    assert moved.acknowledged == 0 and moved.ack_operator == "ops:a"  # the record of who acknowledged stays


def test_a_blocked_or_failed_run_can_be_cancelled_without_a_new_attempt(ledger):
    run = blocked_run(ledger)
    cancelled = ledger.transition(KEY, "cancelled", expected_state="blocked", expected_version=run.state_version,
                                  code="run_cancelled", event_kind="operator")
    assert cancelled.state == "cancelled" and cancelled.attempt_count == run.attempt_count and cancelled.finished_at
    assert [e.kind for e in ledger.events_for(KEY)][-1] == "operator"
    with pytest.raises(lg.InvalidTransitionError):  # retrying still needs a new attempt, cancelling never starts one
        other = ledger.create_run("example-co", "2026-10-27_2026-11-23")
        ledger.transition(other.run_key, "failed", expected_state="requested", expected_version=0, code="x",
                          new_attempt=True)


def test_cancelling_an_in_progress_run_closes_its_open_attempt(ledger):
    ledger.create_run("example-co", PERIOD)
    run = ledger.transition(KEY, "collecting", expected_state="requested", expected_version=0, code="c")
    ledger.transition(KEY, "cancelled", expected_state="collecting", expected_version=run.state_version, code="x")
    attempts = ledger.attempts_for(KEY)
    assert [a.outcome for a in attempts] == ["interrupted"] and attempts[0].finished_at


def test_operator_details_are_stored_with_the_request(ledger):
    request = contracts.RunRequest(
        request_id="00000000-0000-0000-0000-0000000000aa", idempotency_key="k-op", client_key="example-co",
        period_id=PERIOD, action="cancel", requested_at="2026-11-02T00:59:00+00:00", requested_by="make:test",
        operator="ops:alice", reason="no_longer_needed")
    ledger.register_request(request)
    stored = json.loads(ledger.get_request(request.request_id).options_json)
    assert stored == {"replace_existing": False, "operator": "ops:alice", "reason": "no_longer_needed"}
    assert ledger.get_request(request.request_id).action == "cancel"
