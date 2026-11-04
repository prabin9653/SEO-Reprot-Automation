import datetime as dt
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import test_engine as te  # shared synthetic fixtures (helper functions only)
import test_snapshot as ts
from seo_report.periods import report_periods
from seo_report.snapshots import snapshot as snap
from seo_report.snapshots import store as st


# --- helpers ---------------------------------------------------------------------------------------------------

def make_store(path, start=1, **kwargs):
    """A store with predictable run IDs (run-001, ...) and timestamps."""
    ids = iter(f"run-{i:03d}" for i in range(start, start + 200))
    ticks = iter(f"2026-10-30T{i // 3600:02d}:{i % 3600 // 60:02d}:{i % 60:02d}+00:00" for i in range(10_000))
    return st.SnapshotStore(path, now=lambda: next(ticks), new_run_id=lambda: next(ids), **kwargs)


@pytest.fixture
def db(tmp_path):
    return tmp_path / "history" / "seo_reports.sqlite"


@pytest.fixture
def store(db):
    with make_store(db) as opened:
        yield opened


def count(store, table, where="", params=()):
    return store._con.execute(f'SELECT COUNT(*) FROM "{table}" {where}', params).fetchone()[0]


def tamper(path, statements, drop_triggers=()):
    """Edit the database directly, the way damage or a careless manual edit would."""
    con = sqlite3.connect(path, isolation_level=None)
    for table in drop_triggers:
        for op in ("update", "delete"):
            con.execute(f'DROP TRIGGER IF EXISTS "{table}_no_{op}"')
    for statement in statements:
        con.execute(statement)
    con.close()


def rich_snapshot(client_key="example-co"):
    gsc, ga4 = ts.rich_data()
    return ts.build(client_key=client_key, gsc=gsc, ga4=ga4)


PERIODS_B = report_periods(dt.date(2026, 10, 13), dt.date(2026, 11, 9))
PERIODS_C = report_periods(dt.date(2026, 11, 10), dt.date(2026, 12, 7))


def snapshot_for(periods, client_key="example-co"):
    gsc, ga4 = ts.shifted(te.live_like_gsc(), te.live_like_ga4(), periods)
    return ts.build(client_key=client_key, gsc=gsc, ga4=ga4, periods=periods)


# --- location, configuration and setup ----------------------------------------------------------------------------

def test_default_path_is_outside_onedrive_and_configurable(tmp_path):
    env = {"LOCALAPPDATA": str(tmp_path / "Local")}
    default = st.resolve_db_path(environ=env)
    assert default == tmp_path / "Local" / "JasperSEOReporting" / "history" / "seo_reports.sqlite"
    assert st.resolve_db_path(environ={**env, st.ENV_DB_PATH: str(tmp_path / "env.sqlite")}) == tmp_path / "env.sqlite"
    assert st.resolve_db_path(tmp_path / "explicit.sqlite", {**env, st.ENV_DB_PATH: "ignored"}) == tmp_path / "explicit.sqlite"
    assert st.resolve_db_path(environ={}).parts[-3:] == ("JasperSEOReporting", "history", "seo_reports.sqlite")


def test_databases_inside_onedrive_are_refused_unless_allowed(tmp_path):
    inside = tmp_path / "OneDrive - Jasper" / "history.sqlite"
    with pytest.raises(st.SnapshotStoreError, match="OneDrive"):
        st.SnapshotStore(inside)
    assert not inside.exists()
    root = tmp_path / "cloud"
    with pytest.raises(st.SnapshotStoreError, match="OneDrive"):
        st.SnapshotStore(root / "h.sqlite", environ={"OneDrive": str(root)})
    assert st.in_cloud_sync_folder(tmp_path / "plain" / "h.sqlite", {"OneDrive": str(root)}) is False
    with make_store(inside, allow_cloud_sync=True) as allowed:
        assert allowed.verify().ok


def test_connection_settings(store):
    pragma = lambda name: store._con.execute(f"PRAGMA {name}").fetchone()[0]
    assert pragma("journal_mode") == "delete"
    assert pragma("foreign_keys") == 1 and pragma("recursive_triggers") == 1
    assert pragma("synchronous") == 2 and pragma("busy_timeout") == 5000
    assert pragma("user_version") == st.SCHEMA_VERSION
    assert store.verify().ok and store.verify().runs_checked == 0


def test_schema_columns_match_the_snapshot_projection(store):
    for table, columns in snap.PROJECTION_COLUMNS.items():
        info = [row[1] for row in store._con.execute(f'PRAGMA table_info("{table}")')]
        assert info[:2] == ["id", "run_id"] and tuple(info[2:]) == columns, table
    assert set(st.ALL_TABLES) == {r[0] for r in store._con.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'")}


def test_reopening_is_idempotent_and_keeps_data(db):
    with make_store(db) as first:
        first.write(ts.build())
    with make_store(db, start=50) as again:
        assert [r.run_id for r in again.list_runs()] == ["run-001"]
        assert count(again, "meta") == 1 and again.verify().ok


def test_other_or_newer_databases_are_rejected(tmp_path):
    foreign = tmp_path / "foreign.sqlite"
    con = sqlite3.connect(foreign)
    con.execute("CREATE TABLE notes (x)")
    con.commit()
    con.close()
    with pytest.raises(st.SnapshotStoreError, match="not a snapshot database"):
        st.SnapshotStore(foreign)
    newer = tmp_path / "newer.sqlite"
    con = sqlite3.connect(newer)
    con.execute("PRAGMA user_version = 99")
    con.close()
    with pytest.raises(st.SnapshotStoreError, match="newer schema"):
        st.SnapshotStore(newer)


# --- writing, round trip and integrity ---------------------------------------------------------------------------------------

def test_write_and_read_back_round_trip(store):
    snapshot = ts.build()
    result = store.write(snapshot)
    assert (result.run_id, result.revision, result.created, result.superseded, result.notes) == (
        "run-001", 1, True, None, ())
    stored = store.get("run-001")
    assert stored.payload == snapshot.payload and stored.content_hash == snapshot.content_hash
    assert (stored.seq, stored.revision, stored.client_key) == (1, 1, "example-co")
    assert (stored.period_start, stored.period_end) == ("2026-09-29", "2026-10-26")
    assert stored.created_at == "2026-10-30T00:00:00+00:00" and stored.prev_chain_hash == snap.GENESIS
    assert stored.quality == "complete" and stored.eligibility == {"rankings": True, "gsc": True, "ga4": True}
    assert stored.chain_hash == snap.chain_hash(
        snap.GENESIS, seq=1, run_id="run-001", client_key="example-co", period_start="2026-09-29",
        period_end="2026-10-26", revision=1, content_hash=snapshot.content_hash, created_at=stored.created_at)
    for table, rows in snap.projection_rows(snapshot.payload).items():
        assert count(store, table, "WHERE run_id = ?", ("run-001",)) == len(rows), table
    assert store.verify().ok and store.verify_run("run-001").ok


def test_the_stored_payload_is_the_canonical_text(store):
    snapshot = ts.build()
    store.write(snapshot)
    text = store._con.execute("SELECT payload_json FROM runs").fetchone()[0]
    assert text == snapshot.payload_json and snap.sha256_hex(text) == snapshot.content_hash


def test_unknown_runs_raise_a_clear_error(store):
    with pytest.raises(st.SnapshotStoreError, match="no snapshot"):
        store.get("missing")
    with pytest.raises(st.SnapshotStoreError, match="no snapshot"):
        store.verify_run("missing")


def test_a_snapshot_whose_content_does_not_match_its_hash_is_refused(store):
    import dataclasses
    snapshot = ts.build()
    forged = dataclasses.replace(snapshot, payload={**snapshot.payload, "extra": 1})
    with pytest.raises(st.SnapshotStoreError, match="does not match its hash"):
        store.write(forged)
    assert store.list_runs() == []


def test_the_hash_chain_links_runs_across_clients(store):
    store.write(ts.build(client_key="example-co"))
    store.write(ts.build(client_key="other-co"))
    store.write(snapshot_for(PERIODS_C, "example-co"))
    runs = [store.get(r.run_id) for r in store.list_runs()]
    assert [r.seq for r in runs] == [1, 2, 3]
    assert runs[1].prev_chain_hash == runs[0].chain_hash and runs[2].prev_chain_hash == runs[1].chain_hash
    assert len({r.chain_hash for r in runs}) == 3
    report = store.verify()
    assert report.ok and report.runs_checked == 3 and report.problems == ()


# --- append-only behaviour ---------------------------------------------------------------------------------------------------------

def test_every_table_rejects_updates_and_deletes(store):
    store.write(rich_snapshot())
    store.add_event("run-001", "reviewed", "checked")
    for table in st.ALL_TABLES:
        assert count(store, table) > 0, f"the fixture must fill {table}"
        column = store._con.execute(f'PRAGMA table_info("{table}")').fetchone()[1]
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            store._con.execute(f'UPDATE "{table}" SET "{column}" = "{column}"')
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            store._con.execute(f'DELETE FROM "{table}"')
    assert store.verify().ok


def test_other_connections_are_stopped_by_the_triggers_too(db, store):
    store.write(ts.build())
    other = sqlite3.connect(db, isolation_level=None)
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        other.execute("UPDATE runs SET quality = 'complete'")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        other.execute("DELETE FROM run_keywords")
    other.close()


def test_replace_and_duplicate_keys_cannot_overwrite_history(store):
    store.write(ts.build())
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        store._con.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('format', '2')")
    with pytest.raises(sqlite3.IntegrityError):
        store._con.execute("INSERT INTO meta (key, value) VALUES ('format', '2')")
    assert store._con.execute("SELECT value FROM meta WHERE key = 'format'").fetchone()[0] == str(snap.FORMAT_VERSION)


def test_a_failed_write_leaves_nothing_behind(store, monkeypatch):
    original = store._insert_projection

    def fail_after_inserting(run_id, payload):
        original(run_id, payload)
        raise RuntimeError("simulated failure")

    monkeypatch.setattr(store, "_insert_projection", fail_after_inserting)
    with pytest.raises(RuntimeError, match="simulated"):
        store.write(rich_snapshot())
    assert store._con.in_transaction is False
    assert store.list_runs() == [] and all(count(store, t) == 0 for t in st.ALL_TABLES if t != "meta")
    monkeypatch.undo()
    result = store.write(rich_snapshot())
    assert store.get(result.run_id).seq == 1 and store.verify().ok


# --- duplicates and revisions ----------------------------------------------------------------------------------------------------------

def test_identical_content_is_not_stored_twice(store):
    snapshot = ts.build()
    first = store.write(snapshot)
    again = store.write(snapshot)
    assert again.created is False and again.run_id == first.run_id == again.duplicate_of and again.revision == 1
    assert len(store.list_runs()) == 1 and count(store, "runs") == 1 and count(store, "run_events") == 0
    with pytest.raises(st.DuplicateSnapshotError, match="identical content"):
        store.write(snapshot, on_duplicate="error")
    with pytest.raises(ValueError, match="on_duplicate"):
        store.write(snapshot, on_duplicate="overwrite")
    assert store.verify().ok


def test_changed_content_adds_a_revision_and_supersedes_the_old_one(store):
    first = store.write(ts.build())
    changed = ts.build(keywords=te.mixed_keywords() + [te.kw("Extra Keyword", "", 4, "Plano")])
    second = store.write(changed)
    assert second.created and second.revision == 2 and second.superseded == first.run_id
    assert [(e.kind, e.detail) for e in store.events(first.run_id)] == [("superseded", second.run_id)]
    latest = store.latest("example-co", "2026-09-29", "2026-10-26")
    assert latest.run_id == second.run_id and latest.payload == changed.payload
    assert store.get(first.run_id).payload == ts.build().payload  # the older revision is untouched
    assert [r.revision for r in store.list_runs("example-co")] == [1, 2]
    assert store.verify().ok


def test_only_the_latest_revision_counts_as_a_duplicate(store):
    one, two = ts.build(), ts.build(keywords=te.mixed_keywords()[:5])
    store.write(one)
    store.write(two)
    back = store.write(one)  # identical to revision 1, but revision 2 is current
    assert back.created and back.revision == 3 and back.superseded == "run-002"
    assert store.write(one).created is False
    assert store.latest("example-co", "2026-09-29", "2026-10-26").revision == 3


def test_voided_runs_are_ignored_by_duplicate_detection_and_lookups(store):
    snapshot = ts.build()
    first = store.write(snapshot)
    store.add_event(first.run_id, "voided", "entered by mistake")
    assert store.latest("example-co", "2026-09-29", "2026-10-26") is None
    assert store.list_runs() == [] and [r.voided for r in store.list_runs(include_voided=True)] == [True]
    again = store.write(snapshot)
    assert again.created and again.revision == 2 and again.superseded is None
    assert store.latest("example-co", "2026-09-29", "2026-10-26").run_id == again.run_id
    assert store.get(first.run_id).voided is True and store.verify().ok


def test_clients_are_separated_by_key_not_by_display_name(store):
    store.write(ts.build(client_key="example-co"))
    other = store.write(ts.build(client_key="other-co"))
    assert other.revision == 1 and other.created
    renamed = store.write(ts.build(client_key="example-co", client=ts.make_client(name="Renamed Co")))
    assert renamed.revision == 2 and renamed.superseded == "run-001"
    assert [r.run_id for r in store.list_runs("example-co")] == ["run-001", "run-003"]
    assert [r.client_key for r in store.list_runs()] == ["example-co", "other-co", "example-co"]


def test_overlapping_periods_are_flagged_but_allowed(store):
    first = store.write(ts.build())
    overlapping = store.write(snapshot_for(PERIODS_B))
    assert overlapping.created and overlapping.revision == 1
    assert overlapping.notes == (f"This period overlaps run {first.run_id} (2026-09-29 to 2026-10-26, revision 1).",)
    assert store.write(snapshot_for(PERIODS_C)).notes == ()
    assert store.write(snapshot_for(PERIODS_B, "other-co")).notes == ()  # other clients never overlap
    store.add_event(first.run_id, "voided", "mistake")
    # voided runs are not reported: run-001 is voided, run-002 and run-003 both overlap 2026-10-20..2026-11-16
    assert store.write(snapshot_for(report_periods(dt.date(2026, 10, 20), dt.date(2026, 11, 16)))).notes == (
        "This period overlaps run run-002 (2026-10-13 to 2026-11-09, revision 1).",
        "This period overlaps run run-003 (2026-11-10 to 2026-12-07, revision 1).")


# --- events ---------------------------------------------------------------------------------------------------------------------------------

def test_status_events_are_append_only_records(store):
    run = store.write(ts.build()).run_id
    store.add_event(run, "rendered", "report.pdf")
    event = store.add_event(run, "reviewed", "approved by a.person@example.test")
    store.add_event(run, "delivered")
    assert event.detail == "approved by [email]"
    stored = store.get(run)
    assert [e.kind for e in stored.events] == ["rendered", "reviewed", "delivered"] and stored.voided is False
    assert [e.detail for e in stored.events] == ["report.pdf", "approved by [email]", None]
    assert store.verify().ok  # events are not part of the content hash


def test_event_rules(store):
    run = store.write(ts.build()).run_id
    with pytest.raises(ValueError, match="kind"):
        store.add_event(run, "edited")
    with pytest.raises(ValueError, match="kind"):
        store.add_event(run, "superseded")  # recorded by the store itself
    with pytest.raises(ValueError, match="reason"):
        store.add_event(run, "voided")
    with pytest.raises(st.SnapshotStoreError, match="no snapshot"):
        store.add_event("missing", "reviewed")
    assert store.events(run) == ()


# --- NULLs, diagnostics and eligibility in the database -----------------------------------------------------------------------------------------

def test_missing_values_are_stored_as_null_not_zero(store):
    store.write(ts.build(keywords=[te.kw("a", "", "", "Plano"), te.kw("b", "", "", "Frisco")]))
    assert store._con.execute("SELECT DISTINCT cur_kind, cur_position, cur_label FROM run_keywords").fetchall() == [
        ("missing", None, None)]
    assert count(store, "run_keywords", "WHERE cur_position = 0 OR prev_position = 0") == 0
    empty = gs_empty_snapshot()
    store.write(empty)
    row = store._con.execute("SELECT status, clicks, impressions, ctr, position FROM gsc_periods "
                             "WHERE run_id = 'run-002' AND which = 'current'").fetchone()
    assert row == ("empty", None, None, None, None)
    assert count(store, "gsc_daily", "WHERE run_id = 'run-002' AND clicks IS NOT NULL") == 0


def gs_empty_snapshot():
    from seo_report.sources import gsc as gs
    empty = gs.PeriodData(te.PERIODS.current, gs.EMPTY_METRICS,
                          daily=[gs.DailyPoint(d, gs.EMPTY_METRICS, False) for d in te.PERIODS.current.dates()],
                          warnings=[gs.DataWarning("empty_period", "No data")])
    gsc = gs.SearchConsoleData("https://example.test/", "siteOwner", empty, te.live_like_gsc().previous)
    return ts.build(client_key="empty-co", gsc=gsc)


def test_unavailable_key_events_are_null(store):
    ga4 = te.live_like_ga4(status="unavailable", current={"whole": te.g4(90, 82, 28, 0.311, None)})
    store.write(ts.build(ga4=ga4))
    assert store._con.execute("SELECT key_events FROM ga4_periods WHERE which = 'current' AND scope = 'site'"
                              ).fetchone() == (None,)


def test_the_ga4_discrepancy_is_stored_as_recorded(store):
    store.write(rich_snapshot())
    assert store._con.execute("SELECT source, which, name, value FROM run_diagnostics "
                              "WHERE source = 'ga4'").fetchall() == [("ga4", "current", "daily_sessions_diff", 1)]
    site = store._con.execute("SELECT sessions FROM ga4_periods WHERE which = 'current' AND scope = 'site'").fetchone()
    daily = store._con.execute("SELECT SUM(sessions) FROM ga4_daily WHERE which = 'current'").fetchone()
    assert site == (90,) and daily == (91,)  # neither number was adjusted to match the other


def test_baseline_eligibility_is_persisted_per_source(store):
    from seo_report.sources import gsc as gs
    incomplete = te.live_like_gsc(current={"warnings": [gs.DataWarning("incomplete_data", "late")]})
    store.write(ts.build(gsc=incomplete, keywords=[te.kw("a"), te.kw("b")]))
    stored = store.get("run-001")
    assert stored.eligibility == {"rankings": False, "gsc": False, "ga4": True} and stored.quality == "partial"
    assert store._con.execute("SELECT eligible_rankings, eligible_gsc, eligible_ga4 FROM runs").fetchone() == (0, 0, 1)


# --- verification catches damage -----------------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("statements, drop, expected", [
    (["UPDATE runs SET payload_json = replace(payload_json, 'Example Co', 'Edited Co')"], ["runs"],
     "payload does not match its content hash"),
    (["UPDATE run_keywords SET cur_position = 1 WHERE ordinal = 5"], ["run_keywords"],
     "rows in run_keywords differ"),
    ([f"UPDATE runs SET chain_hash = '{'0' * 64}'"], ["runs"], "chain hash does not match"),
    (["UPDATE runs SET website = 'https://changed.test/'"], ["runs"], "descriptive columns differ"),
    (["UPDATE gsc_periods SET clicks = 999 WHERE which = 'current'"], ["gsc_periods"], "rows in gsc_periods differ"),
    (["UPDATE run_insights SET text = 'Edited.' WHERE ordinal = 0"], ["run_insights"], "rows in run_insights differ"),
    ([], ["runs"], "append-only triggers are missing"),
])
def test_verification_detects_edits_and_missing_triggers(db, store, statements, drop, expected):
    store.write(ts.build())
    assert store.verify().ok
    tamper(db, statements, drop)
    report = store.verify()
    assert report.ok is False and any(expected in problem for problem in report.problems), report.problems
    if statements:
        assert store.verify_run("run-001").ok is False


def test_verification_detects_a_removed_run(db, store):
    for key in ("example-co", "other-co", "third-co"):
        store.write(ts.build(client_key=key))
    children = [f"DELETE FROM {t} WHERE run_id = 'run-002'" for t in snap.PROJECTION_COLUMNS]
    tamper(db, [*children, "DELETE FROM runs WHERE run_id = 'run-002'"], st.ALL_TABLES)
    report = store.verify()
    assert report.ok is False
    assert any("gap before run run-003" in p for p in report.problems)
    assert any("run run-003: the chain does not link" in p for p in report.problems)
    assert store.verify_run("run-003").ok is False


def test_verification_detects_a_non_canonical_or_invalid_payload(db, store):
    store.write(ts.build())
    tamper(db, ["UPDATE runs SET payload_json = payload_json || ' '"], ["runs"])
    assert any("canonical form" in p for p in store.verify().problems)
    tamper(db, ["UPDATE runs SET payload_json = 'not json'"], [])
    assert any("not valid JSON" in p for p in store.verify().problems)


def test_verification_cannot_catch_a_full_rewrite_of_the_chain(db, store):
    """The documented limit: a consistent rewrite of the whole chain is not detectable."""
    store.write(ts.build())
    original = store.get("run-001")
    edited = ts.build(client=ts.make_client(name="Edited Co"))
    new_chain = snap.chain_hash(snap.GENESIS, seq=1, run_id="run-001", client_key="example-co",
                                period_start="2026-09-29", period_end="2026-10-26", revision=1,
                                content_hash=edited.content_hash, created_at=original.created_at)
    children = [f"DELETE FROM {t} WHERE run_id = 'run-001'" for t in snap.PROJECTION_COLUMNS]
    tamper(db, [*children, "DELETE FROM runs WHERE run_id = 'run-001'"], st.ALL_TABLES)
    con = sqlite3.connect(db, isolation_level=None)
    columns = ", ".join(f'"{c}"' for c in st._RUN_FIELDS)
    values = (1, "run-001", *snap.run_columns(edited.payload), 1, edited.content_hash, snap.GENESIS, new_chain,
              original.created_at, edited.payload_json)
    con.execute(f"INSERT INTO runs ({columns}) VALUES ({', '.join('?' for _ in values)})", values)
    for table, rows in snap.projection_rows(edited.payload).items():
        cols = ", ".join(f'"{c}"' for c in ("run_id", *snap.PROJECTION_COLUMNS[table]))
        con.executemany(f'INSERT INTO "{table}" ({cols}) VALUES ({", ".join("?" for _ in range(len(snap.PROJECTION_COLUMNS[table]) + 1))})',
                        [("run-001", *row) for row in rows])
    assert store.verify().ok is False  # while the triggers are gone, verification says so
    for statement in st._schema_statements():  # a determined rewrite also restores the triggers
        con.execute(statement)
    con.close()
    assert store.verify().ok  # documented: the chain detects inconsistencies, not a complete rewrite


# --- read-only access, backup and privacy ------------------------------------------------------------------------------------------------------------

def test_read_only_reader_retrieves_and_verifies_but_cannot_write(db, store):
    store.write(ts.build())
    store.add_event("run-001", "reviewed", "ok")
    with st.SnapshotReader(db) as reader:
        assert reader.get("run-001").payload == ts.build().payload
        assert reader.latest("example-co", "2026-09-29", "2026-10-26").run_id == "run-001"
        assert [r.run_id for r in reader.list_runs()] == ["run-001"] and reader.verify().ok
        assert [e.kind for e in reader.events("run-001")] == ["reviewed"]
        assert not hasattr(reader, "write") and not hasattr(reader, "add_event")
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            reader._con.execute("INSERT INTO meta (key, value) VALUES ('x', 'y')")
    with pytest.raises(st.SnapshotStoreError, match="not found"):
        st.SnapshotReader(db.parent / "missing.sqlite")


def test_backup_copies_the_database_and_never_overwrites(db, store, tmp_path):
    store.write(rich_snapshot())
    store.write(ts.build(client_key="other-co"))
    target = store.backup(tmp_path / "backups" / "copy.sqlite")
    assert target.exists()
    with st.SnapshotReader(target) as copy:
        assert [r.run_id for r in copy.list_runs()] == ["run-001", "run-002"]
        assert copy.verify().ok and copy.get("run-001").payload == store.get("run-001").payload
    with pytest.raises(st.SnapshotStoreError, match="already exists"):
        store.backup(target)
    with st.SnapshotReader(db) as reader:  # a read-only connection can back up too
        assert reader.backup(tmp_path / "backups" / "from-reader.sqlite").exists()


def test_the_database_holds_no_secrets_contact_details_or_unsafe_values(store):
    gsc, ga4 = ts.rich_data()
    store.write(ts.build(gsc=gsc, ga4=ga4))
    store.add_event("run-001", "reviewed", "Reviewed by someone@example.test")
    dump = "\n".join(store._con.iterdump())
    for forbidden in ts.FORBIDDEN_STRINGS:
        assert forbidden not in dump, forbidden
    assert ts._EMAIL_LIKE.findall(dump) == [] and ts.phone_like(dump) == []
    assert "[email]" in dump


def test_busy_databases_report_a_clear_error_and_recover(db, store):
    store.write(ts.build())
    blocker = sqlite3.connect(db, isolation_level=None)
    blocker.execute("BEGIN IMMEDIATE")
    other = make_store(db, start=50, busy_timeout=0.1)
    snapshot = ts.build(client_key="other-co")
    with pytest.raises(st.SnapshotStoreError, match="busy or locked"):
        other.write(snapshot)
    assert other._con.in_transaction is False
    blocker.execute("ROLLBACK")
    blocker.close()
    assert other.write(snapshot).created
    other.close()
    assert store.verify().ok


# --- determinism and structure ------------------------------------------------------------------------------------------------------------------------

def test_identical_runs_in_separate_stores_are_byte_for_byte_equal(tmp_path):
    results = []
    for name in ("one", "two"):
        with make_store(tmp_path / name / "h.sqlite") as store:
            store.write(rich_snapshot())
            store.write(ts.build(client_key="other-co"))
            results.append([(r["payload_json"], r["chain_hash"], r["created_at"]) for r in (
                dict(zip(("payload_json", "chain_hash", "created_at"), row)) for row in store._con.execute(
                    "SELECT payload_json, chain_hash, created_at FROM runs ORDER BY seq"))])
    assert results[0] == results[1]


def test_store_module_uses_only_the_standard_library_and_snapshot_code():
    source = (ROOT / "src" / "seo_report" / "snapshots" / "store.py").read_text(encoding="utf-8")
    for forbidden in ("googleapiclient", "google.", "import requests", "import random", "seo_report.sources",
                      "seo_report.clients", "seo_report.auth", "seo_report.insights", "print("):
        assert forbidden not in source
    import re
    assert not re.search(r"INSERT OR REPLACE\s+INTO|\bREPLACE\s+INTO", source)  # only plain INSERT statements
    assert not re.search(r"\bUPDATE\s+\"?\w+\"?\s+SET\b|\bDELETE\s+FROM\b", source)  # the store never edits or removes rows
