import dataclasses
import datetime as dt
import hashlib
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import test_engine as te
import test_history_comparison as tc
import test_history_eligibility as he
import test_snapshot as ts
import test_snapshot_store as tss
from seo_report.snapshots import baselines as bl
from seo_report.snapshots import eligibility as el
from seo_report.snapshots import history
from seo_report.snapshots import snapshot as snap
from seo_report.snapshots import store as st
from seo_report.sources import ga4 as ga
from seo_report.sources import gsc as gs

CURRENT = te.PERIODS  # 2026-09-29 .. 2026-10-26
P_A = he.periods_from(dt.date(2026, 9, 1))  # adjacent: gap 0
P_B = he.periods_from(dt.date(2026, 8, 4))  # gap 28
P_C = he.periods_from(dt.date(2026, 7, 7))  # gap 56
P_D = he.periods_from(dt.date(2026, 6, 9))
OVERLAPPING = he.periods_from(dt.date(2026, 10, 13))
FUTURE = he.periods_from(dt.date(2026, 11, 10))
WIDE = bl.HistoryConfig(max_gap_days=1000)


def make_snapshot(periods, key="example-co", gsc=he.UNSET, ga4=he.UNSET, keywords=None, client=None):
    default_gsc, default_ga4 = he.complete_data(periods)
    gsc = default_gsc if gsc is he.UNSET else gsc
    ga4 = default_ga4 if ga4 is he.UNSET else ga4
    gsc, ga4 = he.relabel(gsc, ga4, periods)
    return ts.build(client_key=key, gsc=gsc, ga4=ga4, keywords=keywords, client=client, periods=periods)


def empty_gsc():
    return te.live_like_gsc(current={"warnings": [gs.DataWarning("empty_period", "none")]})


def query_truncated_gsc(periods):
    gsc, _ = he.complete_data(periods)
    return dataclasses.replace(gsc, current=dataclasses.replace(
        gsc.current, warnings=[gs.DataWarning("truncated", "cut", "query")]))


@pytest.fixture
def db(tmp_path):
    return tmp_path / "history" / "seo_reports.sqlite"


@pytest.fixture
def store(db):
    with tss.make_store(db) as opened:
        yield opened


def compare(store, current=None, config=bl.HistoryConfig()):
    return history.compare_with_history(current or he.payload_for(), store, config)


def baseline_ids(result):
    return {d: (s.baseline.run_id if s.baseline else None) for d, s in result.status.items()}


def file_hash(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


# --- basics ----------------------------------------------------------------------------------------------------------------------

def test_an_empty_history_gives_no_baselines(store):
    result = compare(store)
    assert set(baseline_ids(result).values()) == {None}
    assert [n.message for n in result.notes] == ["No earlier report is available as a baseline for this client."]


def test_the_nearest_earlier_report_is_the_baseline(store):
    store.write(make_snapshot(P_B))
    store.write(make_snapshot(P_A))
    result = compare(store)
    assert set(baseline_ids(result).values()) == {"run-002"}
    ref = result.status["gsc.totals"].baseline
    assert (ref.period_start, ref.period_end, ref.gap_days, ref.rank, ref.same_length) == (
        "2026-09-01", "2026-09-28", 0, 1, True)
    assert result.gsc.totals.changes["clicks"].comparable and result.ga4.site.changes["sessions"].comparable


def test_other_clients_are_never_used(store):
    store.write(make_snapshot(P_A, key="other-co"))
    assert set(baseline_ids(compare(store)).values()) == {None}


def test_comparing_is_read_only_and_leaves_the_history_untouched(db, store):
    store.write(make_snapshot(P_A))
    store.write(make_snapshot(P_B))
    before, runs = file_hash(db), store.list_runs(include_voided=True)
    with st.SnapshotReader(db) as reader:
        via_reader = history.compare_with_history(he.payload_for(), reader)
    via_store = compare(store)
    assert file_hash(db) == before and store.list_runs(include_voided=True) == runs
    assert via_reader == via_store and store.verify().ok


def test_the_current_report_can_be_stored_or_not(store):
    store.write(make_snapshot(P_A))
    before = compare(store)
    store.write(make_snapshot(CURRENT))
    store.write(make_snapshot(CURRENT, keywords=[te.kw("changed", "", 3, "X")]))  # a second revision of the same period
    after = compare(store, store.latest("example-co", "2026-09-29", "2026-10-26").payload)
    assert baseline_ids(after) == baseline_ids(before)
    assert {s.reason for s in after.skipped} == {"superseded by revision 2", "same period as the current report"}


def test_an_unsupported_current_format_is_refused(store):
    with pytest.raises(ValueError, match="not supported"):
        compare(store, dict(he.payload_for(), format=99))


# --- excluded and superseded runs ------------------------------------------------------------------------------------------------

def test_voided_reports_are_skipped_in_favor_of_older_ones(store):
    store.write(make_snapshot(P_B))
    store.write(make_snapshot(P_A))
    store.add_event("run-002", "voided", "entered by mistake")
    result = compare(store)
    assert set(baseline_ids(result).values()) == {"run-001"}
    assert {s.run_id: s.reason for s in result.skipped} == {"run-002": "voided"}
    assert any("older report than the nearest earlier one" in m.message for m in result.notes)


def test_only_the_latest_revision_of_a_period_is_a_baseline(store):
    store.write(make_snapshot(P_A))
    store.write(make_snapshot(P_A, keywords=[te.kw("changed", "", 3, "X")]))
    result = compare(store)
    assert set(baseline_ids(result).values()) == {"run-002"} and result.status["gsc.totals"].baseline.revision == 2
    assert {s.run_id: s.reason for s in result.skipped} == {"run-001": "superseded by revision 2"}


def test_a_revision_does_not_silently_replace_a_better_older_report(store):
    store.write(make_snapshot(P_B))  # run-001
    store.write(make_snapshot(P_A))  # run-002 (revision 1)
    store.write(make_snapshot(P_A, gsc=empty_gsc()))  # run-003: a worse re-run, now the latest revision
    result = compare(store)
    ids = baseline_ids(result)
    assert ids["gsc.totals"] == "run-001"  # the superseded revision is not resurrected; the older period is used
    assert ids["ga4.site"] == "run-003" and ids["rankings.keywords"] == "run-003"
    assert any(n.message.startswith("gsc.totals: the baseline is run run-001") for n in result.notes)
    store.add_event("run-003", "voided", "bad re-run during an outage")
    restored = compare(store)
    assert set(baseline_ids(restored).values()) == {"run-002"}  # voiding the bad revision restores the good one


def test_same_overlapping_and_future_periods_are_not_baselines(store):
    store.write(make_snapshot(P_A))  # run-001
    store.write(make_snapshot(OVERLAPPING))  # run-002
    store.write(make_snapshot(FUTURE))  # run-003
    store.write(make_snapshot(CURRENT))  # run-004
    result = compare(store)
    assert set(baseline_ids(result).values()) == {"run-001"}
    assert {s.run_id: s.reason for s in result.skipped} == {
        "run-002": "overlaps the current period", "run-003": "later than the current period",
        "run-004": "same period as the current report"}


def test_regenerating_an_old_report_ignores_newer_data(store):
    store.write(make_snapshot(P_C))  # run-001
    store.write(make_snapshot(P_B))  # run-002: the report being regenerated
    store.write(make_snapshot(P_A))  # run-003: newer than the report being regenerated
    result = compare(store, he.payload_for(P_B))
    assert set(baseline_ids(result).values()) == {"run-001"}
    assert {s.run_id: s.reason for s in result.skipped} == {
        "run-002": "same period as the current report", "run-003": "later than the current period"}


# --- lookback, gap and verification ---------------------------------------------------------------------------------------------------

def test_the_lookback_and_the_gap_limit_apply_to_stored_runs(store):
    for periods in (P_D, P_C, P_B, P_A):
        store.write(make_snapshot(periods))
    wide = compare(store, config=WIDE)
    assert set(baseline_ids(wide).values()) == {"run-004"}
    assert {s.run_id: s.reason for s in wide.skipped} == {"run-001": "outside the lookback of 3 earlier periods"}
    default = compare(store)  # the gap limit is 35 days: 56 days away is too far, 28 days is not
    assert set(baseline_ids(default).values()) == {"run-004"}
    assert {s.run_id: s.reason for s in default.skipped} == {
        "run-001": "outside the lookback of 3 earlier periods", "run-002": "a gap of 56 days exceeds the 35-day limit"}


def test_a_report_beyond_the_gap_limit_is_no_baseline(store):
    store.write(make_snapshot(P_C))
    result = compare(store)
    assert set(baseline_ids(result).values()) == {None}
    assert result.status["gsc.totals"].reasons == ("no eligible earlier report within the lookback",)
    assert [s.reason for s in result.skipped] == ["a gap of 56 days exceeds the 35-day limit"]


def test_a_damaged_baseline_is_skipped_with_a_warning_and_never_used(db, store):
    store.write(make_snapshot(P_B))  # run-001
    store.write(make_snapshot(P_A))  # run-002
    tss.tamper(db, ["UPDATE runs SET payload_json = replace(payload_json, 'Example Co', 'Edited Co') "
                    "WHERE run_id = 'run-002'"], ["runs"])
    result = compare(store)
    assert "run-002" not in set(baseline_ids(result).values())
    assert set(baseline_ids(result).values()) == {"run-001"} and result.status["gsc.totals"].baseline.rank == 2
    damaged = [s for s in result.skipped if s.run_id == "run-002"]
    assert len(damaged) == 1 and damaged[0].reason.startswith("failed verification") and damaged[0].reason.endswith("not used")
    assert any(n.severity == "warning" and "Run run-002 was not used: failed verification" in n.message
               for n in result.notes)
    assert any("older report than the nearest earlier one" in n.message for n in result.notes)


def test_without_any_verified_candidate_nothing_is_compared(db, store):
    store.write(make_snapshot(P_A))
    tss.tamper(db, [f"UPDATE runs SET chain_hash = '{'0' * 64}'"], ["runs"])
    result = compare(store)
    assert set(baseline_ids(result).values()) == {None} and result.gsc.totals.changes == {}
    assert any("failed verification" in s.reason for s in result.skipped)


def test_runs_in_an_unsupported_format_are_skipped(store):
    other = make_snapshot(P_A)
    payload = dict(other.payload, format=99)
    store.write(dataclasses.replace(other, payload=payload, content_hash=snap.compute_content_hash(payload)))
    store.write(make_snapshot(P_B))
    result = compare(store)
    assert set(baseline_ids(result).values()) == {"run-002"}
    assert {s.run_id: s.reason for s in result.skipped} == {"run-001": "unsupported snapshot format"}


# --- per-dimension baselines -----------------------------------------------------------------------------------------------------------

def test_a_truncated_query_list_keeps_the_totals_baseline_and_borrows_an_older_query_baseline(store):
    store.write(make_snapshot(P_B))  # run-001
    store.write(make_snapshot(P_A, gsc=query_truncated_gsc(P_A)))  # run-002
    result = compare(store)
    ids = baseline_ids(result)
    assert ids["gsc.totals"] == ids["gsc.countries"] == ids["gsc.pages"] == "run-002"
    assert ids["gsc.queries"] == "run-001" and result.status["gsc.queries"].baseline.rank == 2
    assert result.gsc.totals.changes["clicks"].comparable and result.gsc.queries.status.eligible
    assert set(ids.values()) == {"run-001", "run-002"}


def test_each_source_can_use_a_different_report(store):
    store.write(make_snapshot(P_B))  # run-001
    store.write(make_snapshot(P_A, ga4=None))  # run-002: no GA4 data
    ids = baseline_ids(compare(store))
    assert all(ids[d] == "run-001" for d in el.GA4_DIMENSIONS)
    assert all(ids[d] == "run-002" for d in (*el.GSC_DIMENSIONS[:1], el.RANKINGS))


def test_baselines_for_another_property_are_not_used_for_that_source(store):
    other_site = dataclasses.replace(he.complete_data(P_A)[0], site_url="https://other.test/")
    store.write(make_snapshot(P_B))
    store.write(make_snapshot(P_A, gsc=other_site))
    ids = baseline_ids(compare(store))
    assert ids["gsc.totals"] == "run-001" and ids["ga4.site"] == "run-002"


def test_differences_between_the_runs_are_noted(store):
    ga4 = he.complete_data(P_A)[1]
    zoned = dataclasses.replace(ga4, current=dataclasses.replace(ga4.current, time_zone="UTC"))
    store.write(make_snapshot(P_A, ga4=zoned, client=ts.make_client(target_country="Canada")))
    messages = [n.message for n in compare(store).notes]
    assert "The target country differs in baseline run run-001." in messages
    assert "The GA4 reporting time zone differs in baseline run run-001." in messages


# --- diagnostics, keywords and privacy --------------------------------------------------------------------------------------------------

def test_the_ga4_discrepancy_in_a_stored_baseline_is_preserved(store):
    sessions = [3 + (1 if i < 7 else 0) for i in range(28)]  # 91 over the days, 90 in total
    store.write(make_snapshot(P_A, ga4=tc.ga4_daily(P_A, sessions, diagnostics={"daily_sessions_diff": 1})))
    result = compare(store)
    assert result.diagnostics["baselines"]["run-001"]["ga4"]["current"] == {"daily_sessions_diff": 1}
    assert any("+1 in baseline run run-001" in n.message and "not reconciled" in n.message for n in result.notes)
    assert result.ga4.site.baseline["sessions"] == 90 and result.ga4.site.changes["sessions"].previous == 90


def test_keyword_rankings_use_the_stored_baseline_without_inventing_movements(store):
    store.write(make_snapshot(P_A, keywords=[te.kw("a", "", 7, "X"), te.kw("b", "", "Above 10", "Y"),
                                             te.kw("c", "", "", "Z")]))
    current = he.payload_for(keywords=[te.kw("a", 7, 4, "X"), te.kw("b", "Above 10", 3, "Y"), te.kw("c", "", 5, "Z"),
                                       te.kw("d", "", 1, "W")])
    rankings = compare(store, current).rankings
    assert [(m.keyword, m.movement) for m in rankings.moved] == [("a", 3)]
    assert {i.keyword for i in rankings.not_comparable} == {"b", "c"} and all(i.movement is None for i in rankings.not_comparable)
    assert [k.keyword for k in rankings.new_keywords] == ["d"]


def test_results_are_deterministic_across_separate_stores(tmp_path):
    results = []
    for name in ("one", "two"):
        with tss.make_store(tmp_path / name / "h.sqlite") as store:
            store.write(make_snapshot(P_B))
            store.write(make_snapshot(P_A, gsc=query_truncated_gsc(P_A)))
            results.append(compare(store, he.payload_for(keywords=tc.CURRENT_KEYWORDS)))
    assert results[0] == results[1]


def test_results_hold_no_secrets_contact_details_or_unsafe_values(store):
    store.write(make_snapshot(P_A, client=ts.make_client(), keywords=tc.BASELINE_KEYWORDS))
    text = repr(compare(store, he.payload_for(keywords=tc.CURRENT_KEYWORDS, client=ts.make_client())))
    for forbidden in ts.FORBIDDEN_STRINGS:
        assert forbidden not in text, forbidden
    assert ts._EMAIL_LIKE.findall(text) == [] and ts.phone_like(text) == []


def test_history_module_only_reads_the_store():
    source = (ROOT / "src" / "seo_report" / "snapshots" / "history.py").read_text(encoding="utf-8")
    for forbidden in (".write(", "add_event", "backup(", "INSERT", "UPDATE", "DELETE", "import sqlite3", "print(",
                      "googleapiclient", "import time", "import random"):
        assert forbidden not in source
    assert "list_runs" in source and "verify_run" in source
