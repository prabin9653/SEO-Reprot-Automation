import hashlib
import json
import os
import re
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import test_history_eligibility as he
import test_history_integration as ti
import test_record_schema as trs
import test_snapshot_store as tss
from seo_report import history_access as ha
from seo_report import output_layout as layout
from seo_report.snapshots import history
from seo_report.snapshots import store as st

P_A, P_B = ti.P_A, ti.P_B


def uid(n: int) -> str:
    return f"0198c1a2-7b3c-7d4e-8f5a-{n:012d}"


def new_store(db):
    ids = iter(uid(i) for i in range(1, 300))
    ticks = iter(f"2026-10-30T{i // 3600:02d}:{i % 3600 // 60:02d}:{i % 60:02d}+00:00" for i in range(10_000))
    return st.SnapshotStore(db, now=lambda: next(ticks), new_run_id=lambda: next(ids))


@pytest.fixture
def env(tmp_path):
    db = tmp_path / "history" / "seo_reports.sqlite"
    out = tmp_path / "output"
    store = new_store(db)
    opened = []

    def reader():
        raw = st.SnapshotReader(db)
        wrapped = ha.FinalizedOnlyReader(raw, out)
        opened.append(wrapped)
        return wrapped

    yield SimpleNamespace(db=db, out=out, store=store, reader=reader, tmp=tmp_path)
    for item in opened:
        item.close()
    store.close()


def finalize(env, run_id, *, pdf=None, pdf_name="report.pdf", preserved=False, disk_pdf=None, record=True, event=True,
             mutate=None, record_text=None, event_detail=None, event_kind="rendered", record_sha=None):
    """Make a stored run look finalized: a reviewer record, a PDF and a rendered event (each can be withheld)."""
    run = env.store.get(run_id)
    pdf = pdf if pdf is not None else f"%PDF-1.4 synthetic report for {run_id}\n%%EOF\n".encode()
    sha = record_sha or hashlib.sha256(pdf).hexdigest()
    if preserved:  # an older report that a newer revision replaced is kept under its superseded name
        pdf_name = f"report.superseded.{sha[:16]}.pdf"
    reports = layout.report_dir(env.out, run.client_key, run.period_start, run.period_end)
    reviewer = layout.reviewer_dir(env.out, run.client_key, run.period_start, run.period_end)
    if pdf_name:
        reports.mkdir(parents=True, exist_ok=True)
        (reports / pdf_name).write_bytes(pdf if disk_pdf is None else disk_pdf)
    rec = trs.consistent_record(())
    rec["client_key"] = run.client_key
    rec["report_period"] = {"start": run.period_start, "end": run.period_end}
    rec["snapshot"] = {"run_id": run_id, "revision": run.revision, "content_hash": run.content_hash,
                       "quality": run.quality}
    rec["finalization"].update(pdf_sha256=sha, pdf_bytes=len(pdf))
    rec["finalization"]["rendered_event"]["detail"] = f"Report rendered, PDF {sha}"
    if mutate:
        mutate(rec)
    path = reviewer / layout.reviewer_record_name(run_id, sha)
    if record:
        reviewer.mkdir(parents=True, exist_ok=True)
        path.write_text(record_text if record_text is not None else json.dumps(rec, sort_keys=True),
                        encoding="utf-8")
    if event:
        env.store.add_event(run_id, event_kind, event_detail if event_detail is not None
                            else f"Report rendered, PDF {sha}")
    return sha, path


def write_run(env, periods=P_A, **kwargs):
    return env.store.write(ti.make_snapshot(periods, **kwargs)).run_id


def excluded(reader, client_key=None):
    return {e.run_id: e.reason_code for e in reader.excluded_unfinalized(client_key)}


def listed(reader):
    return [info.run_id for info in reader.list_runs("example-co", include_voided=True)]


def baselines(result):
    return set(ti.baseline_ids(result).values())


def file_digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def tree(*roots):
    items = []
    for base in roots:
        base = Path(base)
        if base.exists():
            items += sorted(str(p.relative_to(base.parent)) for p in [base, *base.rglob("*")])
    return items


# --- database probe -----------------------------------------------------------------------------------------------

def test_probe_statuses_are_the_four_documented_values():
    assert ha.PROBE_STATUSES == ("available", "missing", "unavailable", "schema_failure")


def test_probe_missing_database_and_nothing_is_created(tmp_path):
    db = tmp_path / "does" / "not" / "exist.sqlite"
    result = ha.probe_history(db)
    assert result.status == "missing" and not result.available
    assert not db.exists() and not db.parent.exists() and not (tmp_path / "does").exists()


def test_probe_valid_database_is_available(env):
    write_run(env)
    result = ha.probe_history(env.db)
    assert result.status == "available" and result.available
    assert result.user_version == st.SCHEMA_VERSION and result.problems == ()


def test_probe_uses_the_store_path_conventions(env, tmp_path):
    assert ha.probe_history(environ={"JASPER_SEO_HISTORY_DB": str(env.db)}).status == "available"
    assert ha.probe_history(None, environ={"JASPER_SEO_HISTORY_DB": str(tmp_path / "none.sqlite"),
                                           "LOCALAPPDATA": str(tmp_path / "la")}).status == "missing"
    assert ha.probe_history(environ={"LOCALAPPDATA": str(tmp_path / "la")}).status == "missing"
    assert not (tmp_path / "la").exists()


def test_probe_directory_is_unavailable(tmp_path):
    folder = tmp_path / "a_folder"
    folder.mkdir()
    assert ha.probe_history(folder).status == "unavailable"


def test_probe_file_that_is_not_a_database_is_unavailable(tmp_path):
    junk = tmp_path / "junk.sqlite"
    junk.write_text("this is not a database " * 50)
    before = file_digest(junk)
    result = ha.probe_history(junk)
    assert result.status == "unavailable"
    assert file_digest(junk) == before


def test_probe_locked_database_is_unavailable(env):
    write_run(env)
    holder = sqlite3.connect(env.db, isolation_level=None)
    try:
        holder.execute("BEGIN EXCLUSIVE")
        result = ha.probe_history(env.db, busy_timeout=0.05)
    finally:
        holder.execute("ROLLBACK")
        holder.close()
    assert result.status == "unavailable"
    assert ha.probe_history(env.db).status == "available"


def test_probe_empty_file_is_a_schema_failure_and_stays_empty(tmp_path):
    empty = tmp_path / "empty.sqlite"
    empty.write_bytes(b"")
    result = ha.probe_history(empty)
    assert result.status == "schema_failure" and result.user_version == 0
    assert empty.stat().st_size == 0


def test_probe_incompatible_user_version(env):
    write_run(env)
    raw = sqlite3.connect(env.db)
    raw.execute("PRAGMA user_version=2")
    raw.close()
    result = ha.probe_history(env.db)
    assert result.status == "schema_failure" and result.user_version == 2
    assert "schema version" in result.detail


def test_probe_missing_required_table(env):
    write_run(env)
    raw = sqlite3.connect(env.db)
    raw.execute('DROP TABLE "run_notes"')
    raw.close()
    result = ha.probe_history(env.db)
    assert result.status == "schema_failure"
    assert "missing table: run_notes" in result.problems


def test_probe_missing_required_column(env):
    write_run(env)
    raw = sqlite3.connect(env.db)
    raw.execute('ALTER TABLE "run_notes" DROP COLUMN "message"')
    raw.close()
    result = ha.probe_history(env.db)
    assert result.status == "schema_failure"
    assert result.problems == ("missing column: run_notes.message",)


def test_probe_missing_append_only_trigger(env):
    write_run(env)
    raw = sqlite3.connect(env.db)
    raw.execute('DROP TRIGGER "runs_no_update"')
    raw.close()
    result = ha.probe_history(env.db)
    assert result.status == "schema_failure" and result.problems == ("missing trigger: runs_no_update",)


def test_probe_checks_every_table_and_trigger_of_the_store():
    expected = ha._expected_columns()
    assert set(expected) == set(st.ALL_TABLES)
    assert all("run_id" in cols or table in ("meta", "runs") for table, cols in expected.items())
    assert len(st.TRIGGER_NAMES) == 2 * len(st.ALL_TABLES)


def test_probe_leaves_the_database_and_folder_untouched(env):
    write_run(env)
    folder = env.db.parent
    before = (file_digest(env.db), env.db.stat().st_mtime_ns, tree(folder))
    raw = sqlite3.connect(env.db)
    version = raw.execute("PRAGMA user_version").fetchone()[0]
    raw.close()
    for _ in range(3):
        assert ha.probe_history(env.db).available
    assert (file_digest(env.db), env.db.stat().st_mtime_ns, tree(folder)) == before
    raw = sqlite3.connect(env.db)
    assert raw.execute("PRAGMA user_version").fetchone()[0] == version == st.SCHEMA_VERSION
    raw.close()


def test_probe_does_not_change_a_database_with_a_schema_problem(env):
    write_run(env)
    raw = sqlite3.connect(env.db)
    raw.execute("PRAGMA user_version=2")
    raw.close()
    before = (file_digest(env.db), tree(env.db.parent))
    assert ha.probe_history(env.db).status == "schema_failure"
    assert (file_digest(env.db), tree(env.db.parent)) == before
    raw = sqlite3.connect(env.db)
    assert raw.execute("PRAGMA user_version").fetchone()[0] == 2
    raw.close()


def test_probe_messages_do_not_leak_paths_or_raw_errors(env, tmp_path):
    junk = tmp_path / "secret_client_name.sqlite"
    junk.write_text("x" * 200)
    results = [ha.probe_history(junk), ha.probe_history(tmp_path / "gone.sqlite"), ha.probe_history(tmp_path)]
    for result in results:
        text = repr(result)
        assert str(tmp_path) not in text and "secret_client_name" not in text
        assert "sqlite3" not in text.lower() and "Error" not in text and "file is not a database" not in text
        assert result.problems == ()


def test_probe_uses_a_read_only_connection(monkeypatch, env):
    write_run(env)
    seen = {}
    real = sqlite3.connect

    def spy(target, *args, **kwargs):
        seen.setdefault("uri", target)
        seen["kwargs"] = kwargs
        con = real(target, *args, **kwargs)
        return con

    monkeypatch.setattr(ha.sqlite3, "connect", spy)
    assert ha.probe_history(env.db).available
    assert seen["uri"].endswith("?mode=ro") and seen["kwargs"].get("uri") is True


# --- opening and the read-only reader -----------------------------------------------------------------------------

def test_open_history_returns_no_reader_unless_the_database_is_available(tmp_path):
    out = tmp_path / "out"
    missing = ha.open_history(tmp_path / "nope" / "db.sqlite", out)
    assert missing.probe.status == "missing" and missing.reader is None
    assert not (tmp_path / "nope").exists() and not out.exists()
    junk = tmp_path / "junk.sqlite"
    junk.write_text("not a database")
    assert ha.open_history(junk, out).probe.status == "unavailable"
    assert ha.open_history(junk, out).reader is None


def test_open_history_available_gives_a_finalized_only_reader(env):
    write_run(env)
    access = ha.open_history(env.db, env.out)
    try:
        assert access.probe.available and isinstance(access.reader, ha.FinalizedOnlyReader)
        assert access.reader.list_runs("example-co") == []
    finally:
        access.close()
    assert not env.out.exists()  # reading never creates the output folders


def test_open_history_refuses_an_output_root_that_overlaps_the_database(env):
    write_run(env)
    with pytest.raises(layout.OutputLayoutError, match="overlaps"):
        ha.open_history(env.db, env.db.parent)


def test_open_history_never_closes_over_a_schema_failure(env):
    write_run(env)
    raw = sqlite3.connect(env.db)
    raw.execute("PRAGMA user_version=2")
    raw.close()
    access = ha.open_history(env.db, env.out)
    assert access.probe.status == "schema_failure" and access.reader is None
    access.close()


def test_the_connection_is_read_only_and_query_only(env):
    write_run(env)
    access = ha.open_history(env.db, env.out)
    try:
        con = access.reader._reader._con
        assert con.execute("PRAGMA query_only").fetchone()[0] == 1
        for statement in ("INSERT INTO meta (key, value) VALUES ('x', 'y')",
                          "CREATE TABLE intruder (a INTEGER)",
                          "PRAGMA user_version = 9",
                          "INSERT INTO run_events (run_id, kind, created_at) VALUES ('a', 'voided', 'now')"):
            with pytest.raises(sqlite3.Error):
                con.execute(statement)
    finally:
        access.close()


def test_the_public_reader_exposes_only_the_methods_stage_6b_needs(env):
    reader = env.reader()
    public = {name for name in dir(reader) if not name.startswith("_")}
    assert public == {"list_runs", "verify_run", "get", "events", "excluded_unfinalized", "close"}
    for name in ("add_event", "write", "backup", "verify", "latest", "_con", "path", "_initialize"):
        assert not hasattr(reader, name)
    with pytest.raises(AttributeError):
        reader.anything = 1  # no __dict__: nothing can be attached to the wrapper


def test_the_reader_cannot_write_through_its_public_methods(env):
    run = write_run(env)
    finalize(env, run)
    before = (file_digest(env.db), env.db.stat().st_mtime_ns)
    reader = env.reader()
    reader.list_runs("example-co")
    reader.verify_run(run)
    reader.get(run)
    reader.events(run)
    reader.excluded_unfinalized()
    assert (file_digest(env.db), env.db.stat().st_mtime_ns) == before


def test_reading_creates_no_files_or_folders(env):
    run = write_run(env)
    other = write_run(env, P_B)
    finalize(env, run)
    before = tree(env.out, env.db.parent)
    reader = env.reader()
    history.compare_with_history(he.payload_for(), reader)
    reader.excluded_unfinalized()
    assert tree(env.out, env.db.parent) == before
    assert other in excluded(reader)


def test_the_module_source_has_no_write_operations():
    source = Path(ha.__file__).read_text(encoding="utf-8")
    code = re.sub(r'""".*?"""', "", source, flags=re.S)
    for pattern in (r"\.mkdir\(", r"write_text", r"write_bytes", r"open\([^)]*['\"][wax]", r"os\.(remove|rename|replace)",
                    r"unlink", r"shutil", r"INSERT ", r"UPDATE ", r"DELETE ", r"CREATE ", r"DROP ",
                    r"PRAGMA (?!query_only)\w+ ?="):
        assert not re.search(pattern, code), pattern


def test_the_underlying_stage_6b_modules_are_not_changed_by_this_stage():
    for name in ("baselines", "comparison", "history", "store", "eligibility", "snapshot"):
        text = (ROOT / "src" / "seo_report" / "snapshots" / f"{name}.py").read_text(encoding="utf-8")
        assert "history_access" not in text and "FinalizedOnlyReader" not in text


# --- finalized-only filtering ---------------------------------------------------------------------------------------

def test_a_valid_finalized_run_is_included_and_readable(env):
    run = write_run(env)
    finalize(env, run)
    reader = env.reader()
    infos = reader.list_runs("example-co")
    assert [i.run_id for i in infos] == [run]
    assert reader.excluded_unfinalized() == ()
    assert reader.verify_run(run).ok
    assert reader.get(run).run_id == run
    assert [e.kind for e in reader.events(run)] == ["rendered"]


def test_infos_are_the_stores_own_with_original_revisions(env):
    first = write_run(env)
    second = write_run(env, keywords=[ti.te.kw("changed", "", 3, "X")])
    finalize(env, first, preserved=True)
    finalize(env, second)
    raw = st.SnapshotReader(env.db)
    expected = {i.run_id: i for i in raw.list_runs("example-co", include_voided=True)}
    raw.close()
    got = {i.run_id: i for i in env.reader().list_runs("example-co", include_voided=True)}
    assert got == expected
    assert [got[first].revision, got[second].revision] == [1, 2]


def test_no_reviewer_record_is_excluded(env):
    run = write_run(env)
    finalize(env, run, record=False)
    reader = env.reader()
    assert reader.list_runs("example-co") == [] and excluded(reader) == {run: "no_reviewer_record"}


def test_a_reviewer_record_for_another_run_does_not_count(env):
    run, other = write_run(env), write_run(env, P_B)
    finalize(env, other)
    assert excluded(env.reader())[run] == "no_reviewer_record"


@pytest.mark.parametrize("text", [
    "{not json", "", "[]", "null", '{"a": 1, "a": 2}', '{"schema_version": NaN}', "﻿{}", "{}"])
def test_invalid_reviewer_json_is_excluded(env, text):
    run = write_run(env)
    finalize(env, run, record_text=text)
    reader = env.reader()
    assert reader.list_runs("example-co") == [] and excluded(reader) == {run: "record_invalid"}


def test_undecodable_and_oversized_reviewer_files_are_excluded(env):
    run = write_run(env)
    sha, path = finalize(env, run, record_text="{}")
    path.write_bytes(b"\xff\xfe\x00bad")
    assert excluded(env.reader()) == {run: "record_invalid"}
    path.write_bytes(b" " * (ha.MAX_RECORD_BYTES + 1))
    assert excluded(env.reader()) == {run: "record_invalid"}


def test_schema_invalid_reviewer_record_is_excluded(env):
    run = write_run(env)
    finalize(env, run, mutate=lambda rec: rec.pop("history"))
    assert excluded(env.reader()) == {run: "record_invalid"}


@pytest.mark.parametrize("change", [
    lambda rec: rec.update(finalized=True),
    lambda rec: rec["finalization"]["pdf_checks"].update(header=False),
    lambda rec: rec.update(record_type="attempt"),
    lambda rec: rec["snapshot"].update(quality="great"),
    lambda rec: rec.update(schema_version=2),
])
def test_records_that_fail_the_stage_6c_1_validator_are_excluded(env, change):
    run = write_run(env)
    finalize(env, run, mutate=change)
    assert excluded(env.reader()) == {run: "record_invalid"}


@pytest.mark.parametrize("change", [
    lambda rec: rec["snapshot"].update(run_id=uid(250)),
    lambda rec: rec["snapshot"].update(revision=7),
    lambda rec: rec["snapshot"].update(content_hash="e" * 64),
    lambda rec: rec["snapshot"].update(quality="incomplete" if rec["snapshot"]["quality"] != "incomplete" else "partial"),
    lambda rec: rec.update(client_key="another-client"),
    lambda rec: rec["report_period"].update(end="2030-01-01"),
    lambda rec: rec["report_period"].update(start="2000-01-01"),
])
def test_record_that_does_not_match_the_stored_run_is_excluded(env, change):
    run = write_run(env)
    finalize(env, run, mutate=change)
    reader = env.reader()
    assert reader.list_runs("example-co") == [] and excluded(reader) == {run: "record_mismatch"}


def test_record_whose_file_name_hash_differs_from_its_pdf_hash_is_excluded(env):
    run = write_run(env)
    sha, path = finalize(env, run)
    other = hashlib.sha256(b"other").hexdigest()
    path.rename(path.with_name(layout.reviewer_record_name(run, other)))
    assert excluded(env.reader()) == {run: "record_mismatch"}


def test_stored_run_that_was_altered_no_longer_matches_its_record(env):
    run = write_run(env)
    finalize(env, run)
    tss.tamper(env.db, [f"UPDATE runs SET content_hash = '{'a' * 64}' WHERE run_id = '{run}'"], ["runs"])
    assert excluded(env.reader()) == {run: "record_mismatch"}


def test_missing_pdf_is_excluded(env):
    run = write_run(env)
    finalize(env, run, pdf_name=None)
    reader = env.reader()
    assert reader.list_runs("example-co") == [] and excluded(reader) == {run: "final_pdf_missing"}


def test_pdf_with_other_content_is_excluded_as_a_hash_mismatch(env):
    run = write_run(env)
    pdf = b"%PDF-1.4 genuine\n%%EOF\n"
    forged = b"%PDF-1.4 forgery\n%%EOF\n"
    assert len(pdf) == len(forged) and pdf != forged
    finalize(env, run, pdf=pdf, disk_pdf=forged)  # same length, other bytes
    assert excluded(env.reader()) == {run: "pdf_hash_mismatch"}


def test_the_full_sha256_is_compared_not_the_16_character_prefix(env):
    run = write_run(env)
    pdf = b"%PDF-1.4 prefix test\n%%EOF\n"
    real = hashlib.sha256(pdf).hexdigest()
    forged = real[:16] + ("0" if real[16] != "0" else "1") + real[17:]  # same pdf16, different full hash
    assert layout.hash16_of(forged) == layout.hash16_of(real) and forged != real
    finalize(env, run, pdf=pdf, record_sha=forged)
    assert excluded(env.reader()) == {run: "pdf_hash_mismatch"}
    finalize_ok_run = write_run(env, P_B)
    finalize(env, finalize_ok_run, pdf=pdf)
    assert finalize_ok_run in listed(env.reader())


def test_missing_rendered_event_is_excluded(env):
    run = write_run(env)
    finalize(env, run, event=False)
    reader = env.reader()
    assert reader.list_runs("example-co") == [] and excluded(reader) == {run: "no_rendered_event"}


@pytest.mark.parametrize("detail", [
    "Report rendered, PDF " + "d" * 64,  # another full hash
    "Report rendered",
    None,
])
def test_rendered_event_with_another_hash_is_excluded(env, detail):
    run = write_run(env)
    sha, _ = finalize(env, run, event=False)
    env.store.add_event(run, "rendered", detail)
    assert excluded(env.reader()) == {run: "no_rendered_event"}


def test_rendered_event_with_only_a_hash_prefix_is_excluded(env):
    run = write_run(env)
    sha, _ = finalize(env, run, event=False)
    env.store.add_event(run, "rendered", f"Report rendered, PDF {sha[:16]} / {sha[:63]}")
    assert excluded(env.reader()) == {run: "no_rendered_event"}


def test_hash_must_be_a_whole_token_in_the_event_detail(env):
    run = write_run(env)
    sha, _ = finalize(env, run, event=False)
    env.store.add_event(run, "rendered", f"Report rendered, PDF a{sha}")
    assert excluded(env.reader()) == {run: "no_rendered_event"}


def test_only_a_rendered_event_counts(env):
    run = write_run(env)
    sha, _ = finalize(env, run, event_kind="reviewed")
    assert excluded(env.reader()) == {run: "no_rendered_event"}


def test_the_event_detail_survives_the_store_masker_unchanged(env):
    run = write_run(env)
    sha, _ = finalize(env, run)
    assert env.store.events(run)[0].detail == f"Report rendered, PDF {sha}"


def test_a_late_rendered_event_makes_the_run_finalized(env):
    run = write_run(env)
    sha, _ = finalize(env, run, event=False)
    reader = env.reader()
    assert excluded(reader) == {run: "no_rendered_event"}
    env.store.add_event(run, "rendered", f"Report rendered, PDF {sha}")
    assert listed(reader) == [run] and excluded(reader) == {}


def test_checks_report_the_first_failing_step_in_order(env):
    # No PDF and no event: the PDF check comes first.
    run = write_run(env)
    finalize(env, run, pdf_name=None, event=False)
    assert excluded(env.reader()) == {run: "final_pdf_missing"}
    # Wrong PDF content and no event: the hash mismatch comes before the event check.
    other = write_run(env, P_B)
    finalize(env, other, disk_pdf=b"completely different", event=False)
    assert excluded(env.reader())[other] == "pdf_hash_mismatch"


def test_reason_codes_are_exact_and_stable():
    assert ha.REASON_ORDER == ("no_reviewer_record", "record_invalid", "record_mismatch", "final_pdf_missing",
                               "pdf_hash_mismatch", "no_rendered_event")
    assert set(ha.REASON_ORDER) - {"no_reviewer_record", "record_invalid", "record_mismatch"} <= \
        set(ha.record_schema.EXCLUSION_REASON_CODES)
    assert set(ha.REASON_ORDER) == set(ha.record_schema.EXCLUSION_REASON_CODES)


def test_exclusion_entries_fit_the_reviewer_record_schema(env):
    run = write_run(env)
    finalize(env, run, record=False)
    entries = [e.as_record() for e in env.reader().excluded_unfinalized()]
    assert entries == [{"run_id": run, "revision": 1, "period_start": P_A.current.start.isoformat(),
                        "period_end": P_A.current.end.isoformat(), "reason_code": "no_reviewer_record"}]
    record = trs.consistent_record(())
    record["history"]["excluded_unfinalized"] = entries
    ha.record_schema.validate_finalized_record(record)


def test_exclusion_details_hold_no_paths_or_raw_errors(env):
    runs = [write_run(env, p) for p in (P_A, P_B)]
    finalize(env, runs[0], record_text="{broken")
    finalize(env, runs[1], pdf_name=None)
    for entry in env.reader().excluded_unfinalized():
        text = repr(entry.as_record())
        assert str(env.tmp) not in text and "Traceback" not in text and "JSON" not in text


def test_voided_unfinalized_runs_are_not_reported(env):
    run = write_run(env)
    env.store.add_event(run, "voided", "entered by mistake")
    assert env.reader().excluded_unfinalized() == ()


# --- file names -----------------------------------------------------------------------------------------------------

def sha16_of(pdf):
    return hashlib.sha256(pdf).hexdigest()[:16]


@pytest.mark.parametrize("name_for", [
    lambda h: "report.pdf",
    lambda h: f"report.superseded.{h}.pdf",
    lambda h: f"report.superseded.{h}.2.pdf",
    lambda h: f"report.superseded.{h}.17.pdf",
    lambda h: f"report.superseded.{h}.99.pdf",
])
def test_every_approved_pdf_file_name_is_recognized(env, name_for):
    run = write_run(env)
    pdf = b"%PDF-1.4 name test\n%%EOF\n"
    finalize(env, run, pdf=pdf, pdf_name=name_for(sha16_of(pdf)))
    assert listed(env.reader()) == [run]


@pytest.mark.parametrize("name_for", [
    lambda h: f"report.superseded.{h}.1.pdf",
    lambda h: f"report.superseded.{h}.0.pdf",
    lambda h: f"report.superseded.{h}.100.pdf",
    lambda h: f"report.superseded.{h}.02.pdf",
    lambda h: f"report.superseded.{h}.copy.pdf",
    lambda h: f"report.superseded.{h}.pdf.copy",
    lambda h: f"report.superseded.{h}.2.pdf.bak",
    lambda h: f"report.superseded.{h.upper()}.pdf",
    lambda h: f"report.superseded.{'0' * 16}.pdf",
    lambda h: f"report.superseded.{h[:15]}.pdf",
    lambda h: f"report.duplicate.{h}.pdf",
    lambda h: f"report.duplicate.{h}.2.pdf",
    lambda h: "report.unarchived.pdf",
    lambda h: "report.unarchived.pdf.partial",
    lambda h: "REPORT.PDF",
    lambda h: "Report.pdf",
    lambda h: "report.PDF",
    lambda h: "report (1).pdf",
    lambda h: "report.pdf.bak",
    lambda h: f"{h}.pdf",
])
def test_unapproved_pdf_file_names_are_not_recognized(env, name_for):
    run = write_run(env)
    pdf = b"%PDF-1.4 name test\n%%EOF\n"
    finalize(env, run, pdf=pdf, pdf_name=name_for(sha16_of(pdf)))
    reader = env.reader()
    assert reader.list_runs("example-co") == [] and excluded(reader) == {run: "final_pdf_missing"}


def test_a_folder_named_like_the_report_is_not_a_pdf(env):
    run = write_run(env)
    sha, _ = finalize(env, run, pdf_name=None)
    run_info = env.store.get(run)
    folder = layout.report_dir(env.out, run_info.client_key, run_info.period_start, run_info.period_end)
    (folder / "report.pdf").mkdir(parents=True)
    assert excluded(env.reader()) == {run: "final_pdf_missing"}


def test_a_symbolic_link_is_not_a_pdf(env):
    run = write_run(env)
    pdf = b"%PDF-1.4 link target\n%%EOF\n"
    finalize(env, run, pdf=pdf, pdf_name=None)
    info = env.store.get(run)
    folder = layout.report_dir(env.out, info.client_key, info.period_start, info.period_end)
    folder.mkdir(parents=True)
    target = env.tmp / "target.pdf"
    target.write_bytes(pdf)
    try:
        (folder / "report.pdf").symlink_to(target)
    except OSError:
        pytest.skip("symbolic links are not available here")
    assert excluded(env.reader()) == {run: "final_pdf_missing"}


def test_a_superseded_copy_of_another_hash_is_not_used(env):
    run = write_run(env)
    pdf = b"%PDF-1.4 mine\n%%EOF\n"
    finalize(env, run, pdf=pdf, pdf_name=f"report.superseded.{'f' * 16}.pdf")
    assert excluded(env.reader()) == {run: "final_pdf_missing"}


def test_the_run_stays_finalized_when_only_a_preserved_copy_remains(env):
    run = write_run(env)
    pdf = b"%PDF-1.4 old report\n%%EOF\n"
    sha, _ = finalize(env, run, pdf=pdf, pdf_name=f"report.superseded.{sha16_of(pdf)}.pdf")
    info = env.store.get(run)
    folder = layout.report_dir(env.out, info.client_key, info.period_start, info.period_end)
    (folder / "report.pdf").write_bytes(b"%PDF-1.4 a newer report\n%%EOF\n")  # a replacement, other content
    assert listed(env.reader()) == [run]


def test_a_matching_copy_is_found_among_decoys(env):
    run = write_run(env)
    pdf = b"%PDF-1.4 decoys\n%%EOF\n"
    h = sha16_of(pdf)
    info = env.store.get(run)
    folder = layout.report_dir(env.out, info.client_key, info.period_start, info.period_end)
    folder.mkdir(parents=True, exist_ok=True)
    for name in (f"report.superseded.{h}.100.pdf", f"report.superseded.{h}.copy", f"report.superseded.{h.upper()}.pdf",
                 "report.pdf"):
        (folder / name).write_bytes(b"not the report")
    finalize(env, run, pdf=pdf, pdf_name=f"report.superseded.{h}.17.pdf")
    assert listed(env.reader()) == [run]


# --- several records -------------------------------------------------------------------------------------------------

def test_one_valid_record_is_enough_when_a_run_was_rendered_twice(env):
    run = write_run(env)
    finalize(env, run, pdf=b"%PDF-1.4 first render\n%%EOF\n", pdf_name=None)  # no PDF for this record
    finalize(env, run, pdf=b"%PDF-1.4 second render\n%%EOF\n", pdf_name="report.pdf")
    assert listed(env.reader()) == [run]


def test_the_reason_of_the_record_that_got_furthest_is_reported(env):
    run = write_run(env)
    finalize(env, run, record_text="{broken")  # record_invalid
    finalize(env, run, pdf=b"%PDF-1.4 other\n%%EOF\n", mutate=lambda rec: rec["snapshot"].update(revision=9))
    assert excluded(env.reader()) == {run: "record_mismatch"}
    finalize(env, run, pdf=b"%PDF-1.4 third\n%%EOF\n", pdf_name=None)
    # report.pdf exists but holds the second render, so the third record gets as far as the hash comparison
    assert excluded(env.reader()) == {run: "pdf_hash_mismatch"}


def test_the_number_of_records_examined_is_capped(env):
    run = write_run(env)
    info = env.store.get(run)
    folder = layout.reviewer_dir(env.out, info.client_key, info.period_start, info.period_end)
    folder.mkdir(parents=True)
    for n in range(ha.MAX_RECORD_CANDIDATES + 5):
        (folder / f"reviewer.{run}.{n:016x}.json").write_text("{broken")
    sha, _ = finalize(env, run, pdf=b"%PDF-1.4 late\n%%EOF\n")  # sorts after the cap
    assert excluded(env.reader()) == {run: "record_invalid"}


def test_record_names_must_match_exactly(env):
    run = write_run(env)
    sha, path = finalize(env, run)
    for bad in (path.with_name(path.name.replace(".json", ".json.bak")), path.with_name(path.name.upper()),
                path.with_name(f"reviewer.{run}.{sha[:15]}.json"), path.with_name(f"reviewer.{run}.json"),
                path.with_name(f"x.reviewer.{run}.{sha[:16]}.json")):
        assert bad.name != path.name
    path.rename(path.with_name(path.name + ".bak"))
    assert excluded(env.reader()) == {run: "no_reviewer_record"}


# --- Stage 6b revision selection and the public interface -------------------------------------------------------------

def test_a_newer_unfinalized_revision_does_not_hide_an_earlier_finalized_one(env):
    first = write_run(env)
    finalize(env, first)
    second = write_run(env, keywords=[ti.te.kw("changed", "", 3, "X")])  # revision 2, never finalized
    assert env.store.get(second).revision == 2
    raw = st.SnapshotReader(env.db)
    try:
        assert baselines(history.compare_with_history(he.payload_for(), raw)) == {second}  # the problem
    finally:
        raw.close()
    reader = env.reader()
    result = history.compare_with_history(he.payload_for(), reader)
    assert baselines(result) == {first}
    assert result.status["gsc.totals"].baseline.revision == 1
    assert excluded(reader) == {second: "no_reviewer_record"}
    assert [e.revision for e in reader.excluded_unfinalized()] == [2]
    assert [s.reason for s in result.skipped] == []  # hidden, not mislabeled as voided or superseded


def test_an_unfinalized_period_does_not_use_up_a_lookback_slot(env):
    older = write_run(env, P_B)
    finalize(env, older)
    newest = write_run(env, P_A)  # nearer, but never finalized
    result = history.compare_with_history(he.payload_for(), env.reader())
    assert baselines(result) == {older} and newest not in baselines(result)


def test_two_finalized_revisions_keep_the_normal_stage_6b_selection(env):
    first = write_run(env)
    second = write_run(env, keywords=[ti.te.kw("changed", "", 3, "X")])
    finalize(env, first, preserved=True)
    finalize(env, second)
    result = history.compare_with_history(he.payload_for(), env.reader())
    assert baselines(result) == {second} and result.status["gsc.totals"].baseline.revision == 2
    assert {s.run_id: s.reason for s in result.skipped} == {first: "superseded by revision 2"}


def test_wrapper_results_equal_stage_6b_results_when_everything_is_finalized(env):
    runs = [write_run(env, P_B), write_run(env, P_A)]
    for run in runs:
        finalize(env, run)
    raw = st.SnapshotReader(env.db)
    try:
        direct = history.compare_with_history(he.payload_for(), raw)
    finally:
        raw.close()
    wrapped = history.compare_with_history(he.payload_for(), env.reader())
    assert wrapped == direct


def test_voided_finalized_runs_keep_their_normal_stage_6b_treatment(env):
    older, newer = write_run(env, P_B), write_run(env, P_A)
    finalize(env, older)
    finalize(env, newer)
    env.store.add_event(newer, "voided", "entered by mistake")
    reader = env.reader()
    assert [i.run_id for i in reader.list_runs("example-co")] == [older]
    assert [i.run_id for i in reader.list_runs("example-co", include_voided=True)] == [older, newer]
    result = history.compare_with_history(he.payload_for(), reader)
    assert baselines(result) == {older} and {s.run_id: s.reason for s in result.skipped} == {newer: "voided"}


def test_client_filtering_matches_the_underlying_reader(env):
    mine = write_run(env, key="example-co")
    theirs = write_run(env, key="other-co")
    finalize(env, mine)
    finalize(env, theirs)
    reader = env.reader()
    assert [i.run_id for i in reader.list_runs("example-co")] == [mine]
    assert {i.run_id for i in reader.list_runs()} == {mine, theirs}
    assert excluded(reader, "other-co") == {}


def test_a_damaged_finalized_run_still_fails_the_existing_verification(env):
    older, newer = write_run(env, P_B), write_run(env, P_A)
    finalize(env, older)
    finalize(env, newer)
    tss.tamper(env.db, [f"UPDATE runs SET payload_json = replace(payload_json, 'Example Co', 'Edited Co') "
                        f"WHERE run_id = '{newer}'"], ["runs"])
    reader = env.reader()
    assert listed(reader) == [older, newer]
    assert not reader.verify_run(newer).ok and reader.verify_run(older).ok
    result = history.compare_with_history(he.payload_for(), reader)
    assert baselines(result) == {older}
    damaged = [s for s in result.skipped if s.run_id == newer]
    assert len(damaged) == 1 and damaged[0].reason.startswith("failed verification")


def test_runs_that_were_never_listed_cannot_be_read(env):
    run = write_run(env)
    finalize(env, run, record=False)
    reader = env.reader()
    for call in (reader.verify_run, reader.get, reader.events):
        with pytest.raises(ha.HistoryAccessError, match="not a finalized report"):
            call(run)
    reader.list_runs("example-co")
    for call in (reader.verify_run, reader.get, reader.events):
        with pytest.raises(ha.HistoryAccessError):
            call(run)
    with pytest.raises(ha.HistoryAccessError):
        reader.get("no-such-run")


def test_access_errors_are_snapshot_store_errors_with_fixed_messages(env):
    run = write_run(env)
    finalize(env, run)
    reader = env.reader()
    reader.list_runs("example-co")
    tss.tamper(env.db, [f"UPDATE runs SET payload_json = 'not json' WHERE run_id = '{run}'"], ["runs"])
    with pytest.raises(st.SnapshotStoreError) as raised:
        reader.get(run)
    assert isinstance(raised.value, ha.HistoryAccessError)
    assert str(env.tmp) not in str(raised.value) and "JSON" not in str(raised.value)


def test_a_database_that_disappears_gives_a_safe_error(env):
    write_run(env)
    reader = env.reader()
    reader._reader._con.close()
    with pytest.raises(ha.HistoryAccessError) as raised:
        reader.list_runs("example-co")
    assert "sqlite" not in str(raised.value).lower() and str(env.tmp) not in str(raised.value)


def test_an_unusable_client_key_in_the_store_is_simply_not_finalized(env):
    run = write_run(env, key="con")  # a Windows device name cannot be a folder
    reader = env.reader()
    assert excluded(reader) == {run: "no_reviewer_record"}


def test_the_reader_requires_an_absolute_output_root(env):
    raw = st.SnapshotReader(env.db)
    try:
        with pytest.raises(ha.HistoryAccessError, match="absolute"):
            ha.FinalizedOnlyReader(raw, "relative/out")
    finally:
        raw.close()


def test_results_are_deterministic(env):
    runs = [write_run(env, p) for p in (P_B, P_A)]
    finalize(env, runs[0])
    reader = env.reader()
    assert reader.list_runs("example-co") == reader.list_runs("example-co")
    assert reader.excluded_unfinalized() == reader.excluded_unfinalized()
    assert os.path.exists(env.db)
