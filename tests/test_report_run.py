import copy
import dataclasses
import enum
import hashlib
import re
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import test_history_access as tha
import test_history_eligibility as he
import test_history_integration as ti
import test_snapshot as ts
from test_history_access import env  # noqa: F401  (the temporary history and output folders)
from seo_report import clients
from seo_report import history_access as ha
from seo_report import rankings as rk
from seo_report import report_run as rr
from seo_report.client_config import ClientEntry, ClientRegistry, UnknownClientError
from seo_report.insights import engine
from seo_report.insights.recommendations import RecommendationConfig
from seo_report.insights.rules import ReviewerNote, RuleOutput
from seo_report.snapshots import baselines as bl
from seo_report.snapshots import comparison as cmp
from seo_report.snapshots import eligibility as el
from seo_report.snapshots import snapshot as snap
from seo_report.snapshots import store as st

CURRENT = he.CURRENT
P_A = ti.P_A
GSC, GA4 = he.complete_data(CURRENT)
REGISTRY = ClientRegistry([ClientEntry("Example Co", "example-co")])


def sheet_keyword(name, previous, current, location="Dallas", row=2):
    return clients.Keyword(name, location, rk.parse_ranking(previous), rk.parse_ranking(current),
                           f"https://example.test/{name.lower().replace(' ', '-')}", "an internal sheet note", row)


KEYWORDS = [
    sheet_keyword("Roof Repair", "", 2, row=2),        # blank previous; history says it was 5: a pending fallback
    sheet_keyword("Fence Repair", 7, 3, row=3),        # sheet previous (7) differs from the baseline's current (5)
    sheet_keyword("Gutter Repair", 3, "Above 10", row=4),
    sheet_keyword("Siding Install", 9, 4, row=5),      # new since the baseline
    sheet_keyword("Roof Install", "", "Above 10", row=6),
    sheet_keyword("Missing Keyword", "", "", row=7),
    sheet_keyword("Same Rank", "", 4, row=8),          # blank previous, same position as in the baseline: movement 0
]
BASELINE_KEYWORDS = [ti.te.kw("Roof Repair", 6, 5, "Dallas"), ti.te.kw("Fence Repair", 6, 5, "Dallas"),
                     ti.te.kw("Gutter Repair", 4, "Above 10", "Dallas"), ti.te.kw("Old Keyword", 1, 1, "Dallas"),
                     ti.te.kw("Same Rank", 4, 4, "Dallas")]


def make_client(keywords=None, **overrides):
    return ts.make_client(keywords=list(KEYWORDS if keywords is None else keywords), **overrides)


def assemble(env, **overrides):
    options = dict(client=make_client(), periods=CURRENT, gsc=GSC, ga4=GA4, registry=REGISTRY,
                   history_db_path=env.db, output_root=env.out)
    options.update(overrides)
    return rr.assemble_report(**options)


def with_history(env, finalized=True):
    """A stored, finalized report for the adjacent earlier period."""
    run = tha.write_run(env, P_A, keywords=BASELINE_KEYWORDS)
    if finalized:
        tha.finalize(env, run)
    return run


def walk(value, seen=None):
    """Every object reachable from `value`, through dataclasses, containers, enums and plain objects."""
    seen = set() if seen is None else seen
    if id(value) in seen:
        return
    seen.add(id(value))
    yield value
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        for f in dataclasses.fields(value):
            yield from walk(getattr(value, f.name), seen)
    elif isinstance(value, dict):
        for k, v in value.items():
            yield from walk(k, seen)
            yield from walk(v, seen)
    elif isinstance(value, (list, tuple, set, frozenset)):
        for item in value:
            yield from walk(item, seen)
    elif isinstance(value, enum.Enum):
        yield from walk(value.value, seen)
    elif hasattr(value, "__dict__") and not isinstance(value, type):
        for v in vars(value).values():
            yield from walk(v, seen)


def strings(value):
    return [item for item in walk(value) if isinstance(item, str)]


def field_names(value):
    names = set()
    for item in walk(value):
        if dataclasses.is_dataclass(item) and not isinstance(item, type):
            names |= {f.name for f in dataclasses.fields(item)}
        elif isinstance(item, dict):
            names |= {k for k in item if isinstance(k, str)}
    return names


class Spy:
    """Records calls to module-level functions of report_run, in order."""

    def __init__(self, monkeypatch):
        self.calls = []
        self.monkeypatch = monkeypatch

    def wrap(self, name, replacement=None):
        original = getattr(rr, name)

        def wrapper(*args, **kwargs):
            self.calls.append((name, args, kwargs))
            return (replacement or original)(*args, **kwargs)

        self.monkeypatch.setattr(rr, name, wrapper)

    def names(self):
        return [name for name, _, _ in self.calls]


@pytest.fixture
def spy(monkeypatch):
    spy = Spy(monkeypatch)
    for name in ("generate_insights", "build_snapshot", "open_history", "compare_with_history"):
        spy.wrap(name)
    return spy


# --- the assembly sequence ---------------------------------------------------------------------------------------------

def test_a_complete_bundle_is_returned(env):
    with_history(env)
    bundle = assemble(env)
    assert isinstance(bundle, rr.ReportBundle)
    assert [f.name for f in dataclasses.fields(bundle)] == [
        "base_insights", "snapshot", "historical_comparison", "history_insights", "client_content",
        "reviewer_report"]
    assert isinstance(bundle.base_insights, engine.ReportInsights)
    assert isinstance(bundle.snapshot, snap.Snapshot)
    assert isinstance(bundle.historical_comparison, cmp.HistoricalComparison)
    assert isinstance(bundle.client_content, rr.ClientContent)
    assert isinstance(bundle.reviewer_report, rr.ReviewerReport)


def test_the_steps_run_in_the_documented_order(env, spy):
    with_history(env)

    class SpyRegistry(ClientRegistry):
        def key_for(self, name):
            spy.calls.append(("key_for", (name,), {}))
            return super().key_for(name)

    assemble(env, registry=SpyRegistry([ClientEntry("Example Co", "example-co")]))
    assert spy.names() == ["key_for", "generate_insights", "build_snapshot", "open_history", "compare_with_history"]


def test_the_client_key_comes_from_the_registry_not_from_the_name(env):
    registry = ClientRegistry([ClientEntry("Example Co", "stable-key_9")])
    bundle = assemble(env, registry=registry)
    assert bundle.reviewer_report.client_key == "stable-key_9"
    assert bundle.snapshot.client_key == "stable-key_9" == bundle.reviewer_report.snapshot.client_key
    assert bundle.snapshot.payload["client"]["client_key"] == "stable-key_9"


def test_base_insights_come_from_the_existing_engine_unchanged(env, spy):
    bundle = assemble(env)
    (_, _, kwargs), = [c for c in spy.calls if c[0] == "generate_insights"]
    assert kwargs["gsc"] is GSC and kwargs["ga4"] is GA4 and kwargs["target_country"] == "United States"
    assert tuple(kwargs["keywords"]) == tuple(KEYWORDS)
    direct = engine.generate_insights(gsc=GSC, ga4=GA4, keywords=KEYWORDS, target_country="United States",
                                      recommendation_config=RecommendationConfig())
    assert bundle.base_insights == direct


def test_the_snapshot_is_built_in_memory_from_those_insights(env, spy):
    bundle = assemble(env)
    (_, _, kwargs), = [c for c in spy.calls if c[0] == "build_snapshot"]
    assert kwargs["insights"] is bundle.base_insights
    assert kwargs["client_key"] == "example-co" and kwargs["periods"] is CURRENT
    direct = snap.build_snapshot(client_key="example-co", client=make_client(), periods=CURRENT, gsc=GSC, ga4=GA4,
                                 keywords=KEYWORDS, insights=bundle.base_insights,
                                 recommendation_config=RecommendationConfig())
    assert bundle.snapshot == direct and bundle.snapshot.content_hash == direct.content_hash


def test_history_is_opened_through_the_stage_6c_2_interface(env, spy):
    with_history(env)
    assemble(env)
    (_, args, kwargs), = [c for c in spy.calls if c[0] == "open_history"]
    assert args == (env.db, env.out)
    assert kwargs["environ"] is None and kwargs["allow_onedrive_output"] is False


def test_the_stage_6b_comparison_gets_the_snapshot_and_the_finalized_only_reader(env, spy):
    with_history(env)
    bundle = assemble(env)
    (_, args, _), = [c for c in spy.calls if c[0] == "compare_with_history"]
    assert args[0] is bundle.snapshot.payload and isinstance(args[1], rr._PayloadRecorder)
    assert isinstance(args[1]._reader, ha.FinalizedOnlyReader)  # only the finalized-only reader is behind it
    assert args[2] == bl.HistoryConfig()


def test_the_comparison_runs_only_when_the_history_is_available(env, spy, tmp_path):
    assemble(env, history_db_path=tmp_path / "none" / "x.sqlite")  # missing
    junk = tmp_path / "junk.sqlite"
    junk.write_text("not a database")
    assemble(env, history_db_path=junk)  # unavailable
    assert "compare_with_history" not in spy.names() and spy.names().count("open_history") == 2
    with_history(env)
    assemble(env)
    assert spy.names().count("compare_with_history") == 1


def test_history_insights_start_empty(env):
    with_history(env)
    bundle = assemble(env)
    assert bundle.history_insights == RuleOutput() and bundle.history_insights.insights == () \
        and bundle.history_insights.notes == ()
    assert assemble(env, history_db_path=env.tmp / "gone.sqlite").history_insights == RuleOutput()


def test_a_recommendation_config_is_used_for_both_the_engine_and_the_snapshot(env, spy):
    config = RecommendationConfig(standing=("backlinks",), target_city="Dallas")
    bundle = assemble(env, recommendation_config=config)
    kwargs = {name: kw for name, _, kw in spy.calls}
    assert kwargs["generate_insights"]["recommendation_config"] is config
    assert kwargs["build_snapshot"]["recommendation_config"] is config
    assert bundle.snapshot.payload["settings"]["recommendations"]["standing"] == ["backlinks"]


def test_thresholds_are_passed_to_the_engine_only_when_given(env, spy):
    assemble(env)
    assert "thresholds" not in [c for c in spy.calls if c[0] == "generate_insights"][0][2]
    from seo_report.metrics import Thresholds
    assemble(env, thresholds=Thresholds())
    assert "thresholds" in [c for c in spy.calls if c[0] == "generate_insights"][1][2]


# --- history states -------------------------------------------------------------------------------------------------------

def test_available_history(env):
    with_history(env)
    bundle = assemble(env)
    status = bundle.reviewer_report.history_status
    assert status.state == "available" and status.probe_status == "available" and status.problems == ()
    assert bundle.historical_comparison is not None and bundle.reviewer_report.history is not None


def test_missing_history_database(env, tmp_path):
    missing = tmp_path / "nowhere" / "history.sqlite"
    bundle = assemble(env, history_db_path=missing)
    status = bundle.reviewer_report.history_status
    assert (status.state, status.probe_status) == ("missing", "missing")
    assert bundle.historical_comparison is None and bundle.reviewer_report.history is None
    assert not missing.parent.exists()


def test_unavailable_database(env, tmp_path):
    junk = tmp_path / "junk.sqlite"
    junk.write_text("not a database " * 20)
    bundle = assemble(env, history_db_path=junk)
    assert bundle.reviewer_report.history_status.state == "unavailable"
    assert bundle.historical_comparison is None


def test_a_database_path_that_is_a_folder_is_unavailable(env, tmp_path):
    assert assemble(env, history_db_path=tmp_path).reviewer_report.history_status.state == "unavailable"


def test_schema_failure(env):
    with_history(env)
    raw = sqlite3.connect(env.db)
    raw.execute('DROP TABLE "run_notes"')
    raw.close()
    status = assemble(env).reviewer_report.history_status
    assert status.state == "schema_failure" and "missing table: run_notes" in status.problems


def test_an_incompatible_schema_version_is_a_schema_failure(env):
    with_history(env)
    raw = sqlite3.connect(env.db)
    raw.execute("PRAGMA user_version=2")
    raw.close()
    bundle = assemble(env)
    assert bundle.reviewer_report.history_status.state == "schema_failure" and bundle.historical_comparison is None


def test_an_expected_comparison_failure_is_recorded_safely(env, monkeypatch):
    with_history(env)

    def fail(*args, **kwargs):
        raise ha.HistoryAccessError(r"could not read C:\secret\path\history.sqlite near 'SELECT x'")

    monkeypatch.setattr(rr, "compare_with_history", fail)
    bundle = assemble(env)
    status = bundle.reviewer_report.history_status
    assert status.state == "comparison_unavailable" and bundle.historical_comparison is None
    text = " ".join(strings(bundle.reviewer_report))
    assert "secret" not in text and "SELECT" not in text and "history.sqlite" not in text
    assert bundle.reviewer_report.excluded_unfinalized == ()


def test_a_store_error_from_the_comparison_is_also_expected(env, monkeypatch):
    with_history(env)
    monkeypatch.setattr(rr, "compare_with_history", lambda *a, **k: (_ for _ in ()).throw(st.SnapshotStoreError("x")))
    assert assemble(env).reviewer_report.history_status.state == "comparison_unavailable"


@pytest.mark.parametrize("error", [KeyError("k"), TypeError("t"), AttributeError("a"), ZeroDivisionError(), ValueError("v")])
def test_unexpected_errors_in_the_comparison_are_not_swallowed(env, monkeypatch, error):
    with_history(env)
    monkeypatch.setattr(rr, "compare_with_history", lambda *a, **k: (_ for _ in ()).throw(error))
    with pytest.raises(type(error)):
        assemble(env)


def test_unexpected_errors_when_opening_the_history_are_not_swallowed(env, monkeypatch):
    monkeypatch.setattr(rr, "open_history", lambda *a, **k: (_ for _ in ()).throw(TypeError("bug")))
    with pytest.raises(TypeError):
        assemble(env)


def test_an_unusable_output_root_means_unavailable_history_not_a_failed_report(env):
    with_history(env)
    bundle = assemble(env, output_root="relative/out")
    status = bundle.reviewer_report.history_status
    assert status.state == "unavailable" and bundle.historical_comparison is None
    assert "relative" not in repr(bundle.reviewer_report)


def test_a_database_overlapping_the_output_root_means_unavailable_history(env):
    with_history(env)
    assert assemble(env, output_root=env.db.parent).reviewer_report.history_status.state == "unavailable"


def test_the_base_report_is_the_same_whatever_the_history_state(env, tmp_path):
    with_history(env)
    available = assemble(env)
    missing = assemble(env, history_db_path=tmp_path / "nope.sqlite")
    failed = assemble(env, history_db_path=tmp_path)
    for other in (missing, failed):
        assert other.base_insights == available.base_insights
        assert other.snapshot == available.snapshot
        assert other.client_content == available.client_content
        assert other.history_insights == available.history_insights
    assert missing.reviewer_report.base_notes == available.reviewer_report.base_notes


def test_a_safe_reviewer_note_explains_why_history_was_not_used(env, tmp_path):
    bundle = assemble(env, history_db_path=tmp_path / "nope.sqlite")
    notes = bundle.reviewer_report.notes
    assert [(n.severity, n.source, n.category) for n in notes] == [("info", "history", "limitation")]
    assert "does not exist" in notes[0].message and str(tmp_path) not in notes[0].message


def test_every_state_is_a_documented_value(env, tmp_path):
    assert rr.HISTORY_STATES == ("available", "missing", "unavailable", "schema_failure", "comparison_unavailable")
    with_history(env)
    states = {assemble(env).reviewer_report.history_status.state,
              assemble(env, history_db_path=tmp_path / "x.sqlite").reviewer_report.history_status.state}
    assert states <= set(rr.HISTORY_STATES)


def test_the_configured_history_limits_are_used_unchanged(env):
    with_history(env)
    assert assemble(env).historical_comparison.limits == {"lookback_periods": 3, "max_gap_days": 35, "list_limit": 10}
    config = bl.HistoryConfig(lookback_periods=2, max_gap_days=40, list_limit=4)
    bundle = assemble(env, history_config=config)
    expected = {"lookback_periods": 2, "max_gap_days": 40, "list_limit": 4}
    assert bundle.historical_comparison.limits == expected == bundle.reviewer_report.history.limits


def many_keywords(count, previous=""):
    return [sheet_keyword(f"Keyword {n:02d}", previous, n + 1, row=n + 2) for n in range(count)]


def many_baseline(count):
    return [ti.te.kw(f"Keyword {n:02d}", "", n + 1, "Dallas") for n in range(count)]


def test_the_list_limit_does_not_depend_on_the_number_of_sheet_keywords(env):
    tha.finalize(env, tha.write_run(env, P_A, keywords=many_baseline(25)))
    few = assemble(env, client=make_client(many_keywords(3)))
    lots = assemble(env, client=make_client(many_keywords(25)))
    assert few.historical_comparison.limits == lots.historical_comparison.limits
    assert lots.historical_comparison.limits["list_limit"] == 10 == lots.reviewer_report.history.limits["list_limit"]


def test_gsc_and_ga4_comparisons_are_unaffected_by_the_number_of_sheet_keywords(env):
    tha.finalize(env, tha.write_run(env, P_A, keywords=many_baseline(25)))
    few = assemble(env, client=make_client(many_keywords(3))).historical_comparison
    lots = assemble(env, client=make_client(many_keywords(25))).historical_comparison
    assert few.gsc == lots.gsc and few.ga4 == lots.ga4
    for name in ("countries", "queries", "pages"):
        assert len(getattr(lots.gsc, name).matched) <= 10
    assert len(lots.ga4.landing_pages.matched) <= 10 and len(lots.ga4.channels.matched) <= 10
    assert len(lots.rankings.moved) <= 10  # Stage 6b's own list stays capped


def test_every_eligible_sheet_keyword_is_considered_even_beyond_the_list_limit_and_with_zero_movement(env):
    run = tha.write_run(env, P_A, keywords=many_baseline(25))
    tha.finalize(env, run)
    bundle = assemble(env, client=make_client(many_keywords(25)))
    pending = bundle.reviewer_report.ranking_crosscheck.pending_fallbacks
    assert [p.keyword for p in pending] == [f"Keyword {n:02d}" for n in range(25)]  # all 25, in sheet order
    assert {p.movement for p in pending} == {0}  # every one of them is a zero-movement keyword
    assert all(p.current_position == p.baseline_position for p in pending)
    assert all(p.baseline_run_id == run for p in pending)
    assert len(bundle.historical_comparison.rankings.moved) == 0  # Stage 6b lists none of them


def test_zero_movement_keyword_is_a_pending_fallback_candidate(env):
    with_history(env)
    pending = {p.keyword: p for p in assemble(env).reviewer_report.ranking_crosscheck.pending_fallbacks}
    same = pending["Same Rank"]
    assert (same.current_position, same.baseline_position, same.movement) == (4, 4, 0)
    row = {r.keyword: r for r in assemble(env).client_content.ranking_rows}["Same Rank"]
    assert row.previous.kind is rk.RankingKind.MISSING and row.movement is None  # the sheet stays blank


def test_keywords_with_a_sheet_previous_ranking_are_not_fallback_candidates(env):
    with_history(env)
    keywords = [sheet_keyword("Same Rank", 6, 4), sheet_keyword("Roof Repair", 9, 2)]
    check = assemble(env, client=make_client(keywords)).reviewer_report.ranking_crosscheck
    assert check.pending_fallbacks == ()


def test_fallback_candidates_follow_the_stage_6b_matching_rules(env):
    run = tha.write_run(env, P_A, keywords=[
        ti.te.kw("Plain", 1, 4, "Dallas"), ti.te.kw("  PLAIN   two ", 1, 6, " Dallas "),
        ti.te.kw("Twice", 1, 3, "Dallas"), ti.te.kw("Twice", 1, 3, "Dallas"),   # duplicated in the baseline
        ti.te.kw("Over Ten", 1, "Above 10", "Dallas"), ti.te.kw("No Rank", 1, "", "Dallas"),
        ti.te.kw("Other Place", 1, 8, "Dallas")])
    tha.finalize(env, run)
    keywords = [
        sheet_keyword("plain", "", 4), sheet_keyword("Plain Two", "", 6, location="dallas"),   # normalized match
        sheet_keyword("Twice", "", 3),                      # ambiguous in the baseline
        sheet_keyword("Over Ten", "", 5),                   # baseline is Above 10
        sheet_keyword("No Rank", "", 5),                    # baseline has no ranking
        sheet_keyword("Other Place", "", 8, location="Fort Worth"),   # same keyword, other location: new
        sheet_keyword("Brand New", "", 2),                  # not in the baseline
        sheet_keyword("Now Above", "", "Above 10"),         # not numeric now
        sheet_keyword("Dup Sheet", "", 3), sheet_keyword("Dup Sheet", "", 3)]
    check = assemble(env, client=make_client(keywords)).reviewer_report.ranking_crosscheck
    assert [p.keyword for p in check.pending_fallbacks] == ["plain", "Plain Two"]
    assert [p.movement for p in check.pending_fallbacks] == [0, 0]


def test_baseline_values_come_from_the_payload_stage_6b_verified_not_from_an_extra_read(env, monkeypatch):
    with_history(env)
    reads = []
    original = ha.FinalizedOnlyReader.get

    def counting_get(self, run_id):
        reads.append(run_id)
        return original(self, run_id)

    monkeypatch.setattr(ha.FinalizedOnlyReader, "get", counting_get)
    bundle = assemble(env)
    baseline_ids = {ref.run_id for _, ref in bundle.reviewer_report.history.baselines}
    assert sorted(reads) == sorted(baseline_ids)  # exactly the runs Stage 6b itself loaded, once each
    assert bundle.reviewer_report.ranking_crosscheck.pending_fallbacks


def test_the_recorder_passes_the_four_reader_calls_through(env):
    run = with_history(env)
    reader = ha.FinalizedOnlyReader(st.SnapshotReader(env.db), env.out)
    try:
        recorder = rr._PayloadRecorder(reader)
        assert recorder.list_runs("example-co", include_voided=True) == reader.list_runs("example-co",
                                                                                         include_voided=True)
        assert recorder.verify_run(run) == reader.verify_run(run)
        assert recorder.events(run) == reader.events(run)
        stored = recorder.get(run)
        assert stored == reader.get(run) and recorder.payloads == {run: stored.payload}
    finally:
        reader.close()


def test_no_fallback_candidates_without_an_eligible_rankings_baseline(env, tmp_path):
    assert assemble(env, history_db_path=tmp_path / "x.sqlite").reviewer_report.ranking_crosscheck \
        .pending_fallbacks == ()
    tha.write_run(env, P_A, keywords=BASELINE_KEYWORDS)  # stored but never finalized
    assert assemble(env).reviewer_report.ranking_crosscheck.pending_fallbacks == ()


def test_a_comparison_without_a_rankings_baseline_gives_none(env):
    tha.finalize(env, tha.write_run(env, P_A, keywords=[]))  # no keywords tracked in the baseline
    check = assemble(env).reviewer_report.ranking_crosscheck
    assert check.pending_fallbacks == ()


# --- the sheet rows in the client content -----------------------------------------------------------------------------------

def sheet_client(keyword_rows):
    import test_clients as tc
    (client,) = tc.build([tc.client_row(name="Example Co")], keyword_rows)
    return client


def test_target_location_and_target_url_stay_distinct_and_correct(env):
    rows = [
        ["Example Co", "roof repair", "Fort Worth", 5, 3, "https://example.test/roofing", "an internal note"],
        ["Example Co", "gutters", "", "Above 10", 8, "https://example.test/gutters", ""],
        ["Example Co", "fences", "Dallas", "", "", "", ""],
    ]
    client = sheet_client(rows)
    table = assemble(env, client=client).client_content.ranking_rows
    assert [(r.keyword, r.target_location, r.target_url) for r in table] == [
        ("roof repair", "Fort Worth", "https://example.test/roofing"),
        ("gutters", "", "https://example.test/gutters"),
        ("fences", "Dallas", "")]
    for row, source in zip(table, client.keywords):
        assert row.target_location == source.target_location and row.target_url == source.target_url
        assert row.target_location != row.target_url or row.target_location == ""
        assert row.previous is source.previous and row.current is source.current
        assert row.movement == rk.movement(source.previous, source.current)
    assert table[0].movement == 2 and table[1].movement is None and table[2].movement is None
    assert "an internal note" not in "\n".join(strings(table))


def test_a_blank_target_location_is_not_filled_from_the_url(env):
    client = sheet_client([["Example Co", "gutters", "", 4, 2, "https://example.test/gutters", ""]])
    (row,) = assemble(env, client=client).client_content.ranking_rows
    assert row.target_location == "" and row.target_url == "https://example.test/gutters"


def test_target_location_survives_the_snapshot_and_history_untouched(env):
    with_history(env)
    client = sheet_client([["Example Co", "Roof Repair", "Dallas", "", 2, "https://example.test/r", ""]])
    bundle = assemble(env, client=client)
    (row,) = bundle.client_content.ranking_rows
    assert row.target_location == "Dallas" and row.target_url == "https://example.test/r"
    (pending,) = bundle.reviewer_report.ranking_crosscheck.pending_fallbacks
    assert pending.target_location == "Dallas" and "example.test" not in pending.target_location

# --- client and reviewer separation ---------------------------------------------------------------------------------------

def test_client_content_has_exactly_the_approved_fields():
    assert [f.name for f in dataclasses.fields(rr.ClientContent)] == [
        "client", "periods", "sections", "recommendations", "ranking_rows", "historical_statements",
        "ranking_markers", "ranking_footnote"]
    assert [f.name for f in dataclasses.fields(rr.ClientIdentity)] == ["name", "website", "target_country",
                                                                       "target_city"]
    assert [f.name for f in dataclasses.fields(rr.RankingRow)] == [
        "keyword", "target_location", "target_url", "previous", "current", "movement"]


def test_client_content_holds_no_internal_identifiers_paths_or_reviewer_text(env):
    with_history(env)
    bundle = assemble(env)
    reviewer = bundle.reviewer_report
    text = "\n".join(strings(bundle.client_content))
    forbidden = [bundle.snapshot.content_hash, str(env.db), str(env.out), str(env.tmp), "seo_reports.sqlite",
                 *[b.run_id for _, b in reviewer.history.baselines], *[n.message for n in reviewer.base_notes],
                 *[n.message for n in reviewer.notes], "sender@example.test", "(555) 010-0199", "Sender Person",
                 "manager@example.test", "Manager Person", "555-010-0123", "topsecret", "ya29.", "1//fake",
                 "SpreadsheetIdSecret", "123456789", "an internal sheet note"]
    for item in forbidden:
        assert item not in text, item
    assert not re.search(r"[0-9a-f]{64}", text) and not re.search(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}", text)


def test_client_content_has_no_internal_field_names(env):
    with_history(env)
    names = field_names(assemble(env).client_content)
    internal = {"run_id", "revision", "content_hash", "diagnostics", "notes", "validation_failures", "metrics",
                "baseline", "baselines", "skipped", "excluded_unfinalized", "history_status", "client_key",
                "quality", "sender", "email", "phone", "gsc_property", "ga4_property_id", "sheet_row"}
    assert not (names & internal), names & internal


def test_client_content_holds_none_of_the_reviewer_or_internal_types(env):
    with_history(env)
    bundle = assemble(env)
    banned = (engine.ReportInsights, snap.Snapshot, cmp.HistoricalComparison, cmp.DimensionStatus, ReviewerNote,
              bl.BaselineRef, bl.SkippedRun, ha.ExcludedRun, rr.ReviewerReport, rr.SnapshotIdentity,
              rr.HistoryStatus, rr.HistoryReview, rr.PendingFallback, rr.RankingCrosscheck, RuleOutput)
    from seo_report.metrics import ReportMetrics
    for item in walk(bundle.client_content):
        assert not isinstance(item, banned + (ReportMetrics,)), type(item)


def test_client_content_and_reviewer_report_are_separate_types_that_share_no_fields(env):
    client_fields = {f.name for f in dataclasses.fields(rr.ClientContent)}
    reviewer_fields = {f.name for f in dataclasses.fields(rr.ReviewerReport)}
    assert not client_fields & reviewer_fields
    assert not issubclass(rr.ClientContent, rr.ReviewerReport) and not issubclass(rr.ReviewerReport, rr.ClientContent)
    with_history(env)
    bundle = assemble(env)
    assert bundle.client_content is not bundle.reviewer_report
    assert not any(value is bundle.reviewer_report for value in walk(bundle.client_content))
    assert not any(value is bundle.client_content for value in walk(bundle.reviewer_report))


def test_client_content_does_not_expose_the_raw_historical_comparison(env):
    with_history(env)
    bundle = assemble(env)
    assert bundle.historical_comparison is not None
    assert not any(item is bundle.historical_comparison for item in walk(bundle.client_content))
    assert not any(isinstance(item, cmp.HistoricalComparison) for item in walk(bundle.client_content))
    assert not hasattr(bundle.client_content, "historical_comparison")


def test_client_facing_historical_content_is_empty_even_with_history(env):
    with_history(env)
    content = assemble(env).client_content
    assert content.historical_statements == () and content.ranking_markers == () and content.ranking_footnote is None


def test_client_content_carries_the_base_report_and_nothing_historical(env):
    with_history(env)
    bundle = assemble(env)
    content = bundle.client_content
    assert content.sections == tuple(bundle.base_insights.sections)
    assert content.recommendations == tuple(bundle.base_insights.recommendations)
    assert content.periods is CURRENT
    assert content.client == rr.ClientIdentity("Example Co", "https://example.test/", "United States", "Dallas")
    assert content.client.target_country == "United States"


def test_the_reviewer_report_holds_the_internal_metadata(env):
    run = with_history(env)
    unfinalized = tha.write_run(env, ti.P_B, keywords=BASELINE_KEYWORDS)
    reviewer = assemble(env).reviewer_report
    snapshot = reviewer.snapshot
    assert (snapshot.run_id, snapshot.revision) == (None, None)  # nothing is stored in this stage
    assert snapshot.content_hash and snapshot.quality in ("complete", "partial", "incomplete")
    assert (snapshot.period_start, snapshot.period_end) == (CURRENT.current.start.isoformat(),
                                                           CURRENT.current.end.isoformat())
    history = reviewer.history
    assert [s.dimension for s in history.dimension_status] == list(el.DIMENSIONS)
    assert {dim: ref.run_id for dim, ref in history.baselines}["rankings.keywords"] == run
    assert history.limits["lookback_periods"] == 3 and history.diagnostics["baselines"].keys() == {run}
    assert [e.run_id for e in reviewer.excluded_unfinalized] == [unfinalized]
    assert reviewer.excluded_unfinalized[0].reason_code == "no_reviewer_record"
    assert reviewer.base_notes and isinstance(reviewer.diagnostics, dict)


def test_dimension_eligibility_and_skipped_runs_are_kept_for_the_reviewer(env):
    with_history(env)
    first = tha.write_run(env, ti.P_B)
    tha.finalize(env, first)
    env.store.add_event(first, "voided", "mistake")
    review = assemble(env).reviewer_report.history
    assert any(s.eligible for s in review.dimension_status)
    assert {s.run_id: s.reason for s in review.skipped_runs}[first] == "voided"


def test_reviewer_data_has_no_credentials_raw_errors_or_contact_details(env, tmp_path):
    with_history(env)
    for bundle in (assemble(env), assemble(env, history_db_path=tmp_path / "x.sqlite")):
        text = "\n".join(strings(bundle.reviewer_report))
        for secret in ("sender@example.test", "manager@example.test", "555-010", "topsecret", "ya29.", "1//fake",
                       "SpreadsheetIdSecret", "Traceback", str(env.tmp)):
            assert secret not in text, secret


def test_no_contact_or_sender_details_are_copied_anywhere_in_the_bundle_views(env):
    bundle = assemble(env)
    for view in (bundle.client_content, bundle.reviewer_report):
        text = "\n".join(strings(view))
        assert "sender@example.test" not in text and "Sender Person" not in text and "Manager Person" not in text


# --- ranking behavior -----------------------------------------------------------------------------------------------------

def test_sheet_rankings_are_passed_through_unchanged(env):
    with_history(env)
    rows = assemble(env).client_content.ranking_rows
    assert len(rows) == len(KEYWORDS)
    for row, source in zip(rows, KEYWORDS):
        assert (row.keyword, row.target_location, row.target_url) == (source.keyword, source.target_location,
                                                                      source.target_url)
        assert row.previous is source.previous and row.current is source.current
        assert row.movement == rk.movement(source.previous, source.current)


def test_rows_do_not_depend_on_the_history(env, tmp_path):
    with_history(env)
    assert assemble(env).client_content.ranking_rows == assemble(
        env, history_db_path=tmp_path / "x.sqlite").client_content.ranking_rows


def test_blank_and_non_numeric_rankings_get_no_movement(env):
    with_history(env)
    by_name = {r.keyword: r for r in assemble(env).client_content.ranking_rows}
    assert by_name["Roof Repair"].previous.kind is rk.RankingKind.MISSING and by_name["Roof Repair"].movement is None
    assert by_name["Gutter Repair"].movement is None and by_name["Roof Install"].movement is None
    assert by_name["Missing Keyword"].movement is None
    assert by_name["Fence Repair"].movement == 4 and by_name["Siding Install"].movement == 5


def test_a_historical_fallback_is_recorded_only_as_a_pending_decision(env):
    run = with_history(env)
    bundle = assemble(env)
    check = bundle.reviewer_report.ranking_crosscheck
    assert check.fallback_used == 0
    pending = {p.keyword: p for p in check.pending_fallbacks}
    assert set(pending) == {"Roof Repair", "Same Rank"}
    fallback = pending["Roof Repair"]
    assert (fallback.current_position, fallback.baseline_position, fallback.movement) == (2, 5, 3)
    assert fallback.baseline_run_id == run and fallback.baseline_revision == 1
    assert (fallback.baseline_period_start, fallback.baseline_period_end) == (
        P_A.current.start.isoformat(), P_A.current.end.isoformat())
    row = {r.keyword: r for r in bundle.client_content.ranking_rows}["Roof Repair"]
    assert row.previous.kind is rk.RankingKind.MISSING and row.movement is None  # the sheet value stays blank
    assert not any(isinstance(item, rr.PendingFallback) for item in walk(bundle.client_content))


def test_a_sheet_versus_history_mismatch_is_reviewer_only(env):
    with_history(env)
    bundle = assemble(env)
    check = bundle.reviewer_report.ranking_crosscheck
    assert ("Fence Repair", "Dallas") in check.mismatched_keywords
    assert check.sheet_previous_mismatch >= 1
    assert "Fence Repair" in {r.keyword for r in bundle.client_content.ranking_rows}  # the row itself is unchanged
    assert not any(isinstance(item, rr.RankingCrosscheck) for item in walk(bundle.client_content))
    row = {r.keyword: r for r in bundle.client_content.ranking_rows}["Fence Repair"]
    assert row.previous.position == 7  # the sheet's Previous Ranking is not replaced by the history's 5


def test_new_and_lost_keywords_and_unusable_values_get_no_fabricated_numbers(env):
    with_history(env)
    check = assemble(env).reviewer_report.ranking_crosscheck
    assert check.new_keywords >= 1 and check.no_longer_tracked >= 1
    pending = {p.keyword for p in check.pending_fallbacks}
    assert not pending & {"Siding Install", "Gutter Repair", "Roof Install", "Missing Keyword", "Old Keyword"}


def test_duplicate_sheet_rows_are_never_matched_to_history(env):
    with_history(env)
    keywords = [sheet_keyword("Roof Repair", "", 2, row=2), sheet_keyword("Roof Repair", "", 3, row=3)]
    check = assemble(env, client=make_client(keywords)).reviewer_report.ranking_crosscheck
    assert check.pending_fallbacks == ()


def test_a_blank_previous_ranking_stays_blank_without_history(env, tmp_path):
    bundle = assemble(env, history_db_path=tmp_path / "x.sqlite")
    assert bundle.reviewer_report.ranking_crosscheck == rr.RankingCrosscheck()
    assert bundle.reviewer_report.ranking_crosscheck.pending_fallbacks == ()


def test_ineligible_ranking_history_gives_no_pending_fallbacks(env):
    tha.write_run(env, P_A, keywords=[ti.te.kw("Other", 1, 1, "Dallas")])  # unfinalized: not a baseline
    check = assemble(env).reviewer_report.ranking_crosscheck
    assert check.pending_fallbacks == () and check.sheet_previous_mismatch in (None, 0)


# --- immutability, persistence and determinism ------------------------------------------------------------------------------

def test_inputs_are_not_mutated(env):
    with_history(env)
    client, gsc, ga4 = make_client(), copy.deepcopy(GSC), copy.deepcopy(GA4)
    before = (copy.deepcopy(client), copy.deepcopy(gsc), copy.deepcopy(ga4), copy.deepcopy(CURRENT))
    assemble(env, client=client, gsc=gsc, ga4=ga4)
    assert (client, gsc, ga4, CURRENT) == before


def test_reviewer_copies_do_not_alias_the_base_insights_or_the_comparison(env):
    with_history(env)
    bundle = assemble(env)
    base_before = copy.deepcopy(bundle.base_insights.diagnostics)
    comparison_before = copy.deepcopy(bundle.historical_comparison.diagnostics)
    reviewer = bundle.reviewer_report
    assert reviewer.diagnostics == bundle.base_insights.diagnostics
    assert reviewer.diagnostics is not bundle.base_insights.diagnostics
    reviewer.diagnostics["injected"] = 1
    reviewer.history.diagnostics["injected"] = 1
    reviewer.history.limits["injected"] = 1
    assert bundle.base_insights.diagnostics == base_before
    assert bundle.historical_comparison.diagnostics == comparison_before
    assert "injected" not in bundle.historical_comparison.limits


def test_the_bundle_and_its_views_are_frozen(env):
    bundle = assemble(env)
    for target, name in ((bundle, "snapshot"), (bundle.client_content, "ranking_rows"),
                         (bundle.reviewer_report, "client_key"), (bundle.reviewer_report.history_status, "state")):
        with pytest.raises(dataclasses.FrozenInstanceError):
            setattr(target, name, None)


def test_nothing_is_persisted_or_created(env):
    with_history(env)
    other = tha.write_run(env, ti.P_B)
    folders = (env.out, env.db.parent)
    reader = st.SnapshotReader(env.db)
    runs_before = [i.run_id for i in reader.list_runs(include_voided=True)]
    events_before = {i: reader.events(i) for i in runs_before}
    reader.close()
    before = (tha.file_digest(env.db), env.db.stat().st_mtime_ns, tha.tree(*folders))
    cwd_before = tha.tree(Path.cwd())
    for _ in range(2):
        assemble(env)
    assert (tha.file_digest(env.db), env.db.stat().st_mtime_ns, tha.tree(*folders)) == before
    assert tha.tree(Path.cwd()) == cwd_before
    reader = st.SnapshotReader(env.db)
    assert [i.run_id for i in reader.list_runs(include_voided=True)] == runs_before
    assert {i: reader.events(i) for i in runs_before} == events_before
    reader.close()
    assert other in runs_before


def test_assembling_without_a_database_creates_nothing(env, tmp_path):
    target = tmp_path / "history_here" / "db.sqlite"
    out = tmp_path / "out_here"
    before = tha.tree(tmp_path)
    assemble(env, history_db_path=target, output_root=out)
    assert tha.tree(tmp_path) == before and not target.parent.exists() and not out.exists()


def test_repeated_assembly_gives_equivalent_results(env):
    with_history(env)
    first, second = assemble(env), assemble(env)
    assert first.client_content == second.client_content
    assert first.reviewer_report == second.reviewer_report
    assert first.snapshot.content_hash == second.snapshot.content_hash
    assert first == second


def test_client_content_does_not_depend_on_volatile_history_details(env):
    with_history(env)
    first = assemble(env).client_content
    tha.write_run(env, ti.P_B)  # more stored history changes nothing the client sees
    assert assemble(env).client_content == first


def test_the_module_has_no_clock_and_no_write_operations():
    source = Path(rr.__file__).read_text(encoding="utf-8")
    code = re.sub(r'""".*?"""', "", source, flags=re.S)
    for pattern in (r"\bdatetime\b", r"\btime\.", r"\.now\(", r"\.mkdir\(", r"write_text", r"write_bytes",
                    r"open\(", r"\.write\(", r"add_event", r"SnapshotStore\(", r"os\.(remove|rename|replace)",
                    r"requests", r"googleapiclient", r"credentials"):
        assert not re.search(pattern, code), pattern


# --- failure behavior ------------------------------------------------------------------------------------------------------

def test_an_unknown_client_stops_the_assembly(env, spy):
    with pytest.raises(UnknownClientError):
        assemble(env, client=make_client(name="Someone Else"))
    assert spy.names() == []


def test_a_name_that_differs_only_in_case_is_not_resolved(env, spy):
    with pytest.raises(UnknownClientError):
        assemble(env, client=make_client(name="example co"))
    assert spy.names() == []


def test_a_base_insight_failure_stops_the_assembly(env, spy, monkeypatch):
    def boom(**kwargs):
        raise RuntimeError("engine failure")

    monkeypatch.setattr(rr, "generate_insights", boom)
    with pytest.raises(RuntimeError, match="engine failure"):
        assemble(env)
    assert "build_snapshot" not in spy.names() and "open_history" not in spy.names()


def test_a_snapshot_failure_stops_the_assembly_before_the_history_is_opened(env, spy):
    wrong_periods = he.periods_from(__import__("datetime").date(2026, 1, 5))
    with pytest.raises(ValueError, match="does not cover the reporting periods"):
        assemble(env, periods=wrong_periods)
    assert "open_history" not in spy.names() and "compare_with_history" not in spy.names()


def test_a_snapshot_builder_error_propagates(env, monkeypatch):
    monkeypatch.setattr(rr, "build_snapshot", lambda **kw: (_ for _ in ()).throw(KeyError("bug")))
    with pytest.raises(KeyError):
        assemble(env)


def test_history_failures_never_prevent_the_base_report(env, monkeypatch, tmp_path):
    expected = assemble(env, history_db_path=tmp_path / "x.sqlite")
    monkeypatch.setattr(rr, "open_history", lambda *a, **k: (_ for _ in ()).throw(
        rr.OutputLayoutError("bad root")))
    bundle = assemble(env)
    assert bundle.base_insights == expected.base_insights and bundle.client_content == expected.client_content
    assert bundle.reviewer_report.history_status.state == "unavailable"


def test_the_history_reader_is_closed_after_assembly(env, monkeypatch):
    with_history(env)
    closed = []
    real = ha.HistoryAccess.close

    def tracking_close(self):
        closed.append(self)
        real(self)

    monkeypatch.setattr(ha.HistoryAccess, "close", tracking_close)
    assemble(env)
    assert len(closed) == 1
    monkeypatch.setattr(rr, "compare_with_history", lambda *a, **k: (_ for _ in ()).throw(TypeError("bug")))
    with pytest.raises(TypeError):
        assemble(env)
    assert len(closed) == 2
