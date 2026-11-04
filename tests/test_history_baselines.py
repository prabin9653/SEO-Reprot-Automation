import dataclasses
import datetime as dt
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import test_engine as te
import test_history_eligibility as he
from seo_report.periods import Period, ReportPeriods
from seo_report.snapshots import baselines as bl
from seo_report.snapshots import eligibility as el
from seo_report.sources import ga4 as ga
from seo_report.sources import gsc as gs

D = dt.date
CUR = ("2026-09-29", "2026-10-26")
CONFIG = bl.HistoryConfig()
WIDE = bl.HistoryConfig(max_gap_days=1000)


def info(run_id, start, end, revision=1, voided=False):
    return SimpleNamespace(run_id=run_id, revision=revision, period_start=start, period_end=end, voided=voided)


def plan(infos, config=CONFIG, current=CUR):
    return bl.plan_candidates(current_start=current[0], current_end=current[1], run_infos=infos, config=config)


def reasons(result):
    return {s.run_id: s.reason for s in result.skipped}


A = ("2026-09-01", "2026-09-28")  # adjacent to the current period
B = ("2026-08-04", "2026-08-31")  # 28 days earlier
C = ("2026-07-07", "2026-08-03")  # 56 days earlier: beyond the 35-day gap limit


# --- candidate planning ----------------------------------------------------------------------------------------------------

def test_candidates_are_ordered_nearest_first_and_ranked_by_the_caller():
    result = plan([info("b", *B), info("a", *A)])
    assert [i.run_id for i in result.ordered] == ["a", "b"] and result.skipped == ()


def test_only_the_latest_non_voided_revision_of_a_period_is_a_candidate():
    result = plan([info("a1", *A, revision=1), info("a2", *A, revision=2), info("a3", *A, revision=3, voided=True)])
    assert [i.run_id for i in result.ordered] == ["a2"]
    assert reasons(result) == {"a1": "superseded by revision 2", "a3": "voided"}


def test_voiding_the_latest_revision_restores_the_previous_one():
    result = plan([info("a1", *A, revision=1), info("a2", *A, revision=2, voided=True)])
    assert [i.run_id for i in result.ordered] == ["a1"] and reasons(result) == {"a2": "voided"}


def test_voided_runs_are_never_candidates():
    result = plan([info("a", *A, voided=True)])
    assert result.ordered == () and reasons(result) == {"a": "voided"}


@pytest.mark.parametrize("period, reason", [
    (CUR, "same period as the current report"),
    (("2026-10-13", "2026-11-09"), "overlaps the current period"),  # overlaps the end
    (("2026-09-15", "2026-10-12"), "overlaps the current period"),  # overlaps the start
    (("2026-10-01", "2026-10-14"), "overlaps the current period"),  # inside the current period
    (("2026-09-01", "2026-11-30"), "overlaps the current period"),  # contains the current period
    (("2026-10-27", "2026-11-23"), "later than the current period"),
    (("2026-11-24", "2026-12-21"), "later than the current period"),
])
def test_same_overlapping_and_later_periods_are_excluded(period, reason):
    result = plan([info("x", *period), info("a", *A)])
    assert [i.run_id for i in result.ordered] == ["a"] and reasons(result) == {"x": reason}


def test_every_revision_of_the_current_period_is_excluded():
    result = plan([info("c1", *CUR, revision=1), info("c2", *CUR, revision=2), info("a", *A)])
    assert [i.run_id for i in result.ordered] == ["a"]
    assert reasons(result) == {"c1": "superseded by revision 2", "c2": "same period as the current report"}


def test_a_period_that_ends_the_day_before_is_adjacent_not_overlapping():
    assert [i.run_id for i in plan([info("a", "2026-09-01", "2026-09-28")]).ordered] == ["a"]
    assert reasons(plan([info("a", "2026-09-02", "2026-09-29")])) == {"a": "overlaps the current period"}


def test_the_gap_limit_is_inclusive_at_35_days():
    ends = {"gap35": D(2026, 9, 29) - dt.timedelta(days=36), "gap36": D(2026, 9, 29) - dt.timedelta(days=37)}
    infos = [info(name, (end - dt.timedelta(days=27)).isoformat(), end.isoformat()) for name, end in ends.items()]
    result = plan(infos)
    assert [i.run_id for i in result.ordered] == ["gap35"]
    assert reasons(result) == {"gap36": "a gap of 36 days exceeds the 35-day limit"}


def test_a_far_earlier_report_is_not_a_baseline():
    result = plan([info("c", *C)])
    assert result.ordered == () and reasons(result) == {"c": "a gap of 56 days exceeds the 35-day limit"}


def test_the_lookback_covers_three_earlier_periods():
    starts = [D(2026, 9, 1) - dt.timedelta(days=28 * i) for i in range(5)]
    infos = [info(f"p{i}", s.isoformat(), (s + dt.timedelta(days=27)).isoformat()) for i, s in enumerate(starts)]
    result = plan(infos, WIDE)
    assert [i.run_id for i in result.ordered] == ["p0", "p1", "p2"]
    assert reasons(result) == {"p3": "outside the lookback of 3 earlier periods",
                               "p4": "outside the lookback of 3 earlier periods"}
    assert [i.run_id for i in plan(infos, bl.HistoryConfig(lookback_periods=1, max_gap_days=1000)).ordered] == ["p0"]


def test_skipped_runs_are_listed_in_a_stable_order():
    infos = [info("z", *CUR), info("m", "2026-11-24", "2026-12-21"), info("a", *A, voided=True)]
    first, second = plan(infos), plan(list(reversed(infos)))
    assert first == second
    assert [s.period_start for s in first.skipped] == sorted(s.period_start for s in first.skipped)


def test_history_config_is_validated():
    for kwargs in ({"lookback_periods": 0}, {"max_gap_days": -1}, {"list_limit": 0}):
        with pytest.raises(ValueError):
            bl.HistoryConfig(**kwargs)
    assert (CONFIG.lookback_periods, CONFIG.max_gap_days, CONFIG.list_limit) == (3, 35, 10)


# --- baseline selection ------------------------------------------------------------------------------------------------------

def periods_for(span):
    start, end = D.fromisoformat(span[0]), D.fromisoformat(span[1])
    return ReportPeriods(Period(start, end), Period(start - dt.timedelta(days=28), start - dt.timedelta(days=1)))


def loaded(run_id, span, rank, **payload_kwargs):
    payload = he.payload_for(periods_for(span), **payload_kwargs)
    return bl.LoadedRun(run_id, 1, span[0], span[1], f"2026-10-30T00:00:0{rank}+00:00", rank, payload)


def current_payload(**kwargs):
    return he.payload_for(**kwargs)


def truncated_gsc(report):
    gsc, _ = he.complete_data()
    return dataclasses.replace(gsc, current=dataclasses.replace(
        gsc.current, warnings=[gs.DataWarning("truncated", "cut", report)]))


def select(candidates, current=None, config=CONFIG, skipped=()):
    return bl.select_baselines(current or current_payload(), candidates, config, skipped)


def test_every_dimension_uses_the_nearest_eligible_report():
    selection = select([loaded("run-a", A, 1), loaded("run-b", B, 2)])
    assert set(selection.baselines) == set(el.DIMENSIONS)
    assert {r.run_id for r in selection.baselines.values()} == {"run-a"}
    ref = selection.baselines["gsc.totals"]
    assert (ref.revision, ref.period_start, ref.period_end, ref.rank, ref.gap_days, ref.same_length, ref.kind) == (
        1, "2026-09-01", "2026-09-28", 1, 0, True, bl.BaselineKind.PREVIOUS_REPORT)
    assert selection.rejections["gsc.totals"] == () and selection.notes == ()


def test_each_dimension_can_use_a_different_baseline():
    selection = select([loaded("run-a", A, 1, gsc=truncated_gsc("query"), ga4=None), loaded("run-b", B, 2)])
    chosen = {d: r.run_id for d, r in selection.baselines.items()}
    assert chosen["gsc.queries"] == "run-b" and chosen["gsc.totals"] == "run-a"
    assert chosen["gsc.pages"] == "run-a" and chosen["rankings.keywords"] == "run-a"
    assert all(chosen[d] == "run-b" for d in el.GA4_DIMENSIONS)  # run-a has no GA4 data
    assert {r.run_id for r in selection.baselines.values()} == {"run-a", "run-b"}


def test_a_truncated_query_list_does_not_invalidate_the_gsc_totals_baseline():
    selection = select([loaded("run-a", A, 1, gsc=truncated_gsc("query")), loaded("run-b", B, 2)])
    assert selection.baselines["gsc.totals"].run_id == "run-a"
    assert selection.baselines["gsc.countries"].run_id == "run-a" and selection.baselines["gsc.pages"].run_id == "run-a"
    assert selection.baselines["gsc.queries"].run_id == "run-b" and selection.baselines["gsc.queries"].rank == 2


def test_rejected_nearer_candidates_are_recorded_with_their_reason():
    selection = select([loaded("run-a", A, 1, gsc=truncated_gsc("query")), loaded("run-b", B, 2)])
    assert selection.rejections["gsc.queries"] == (("run-a", "the Search Console query data was truncated"),)
    assert selection.rejections["gsc.totals"] == ()


def test_choosing_an_older_report_adds_a_reviewer_note():
    selection = select([loaded("run-a", A, 1, gsc=truncated_gsc("query")), loaded("run-b", B, 2)])
    older = [n for n in selection.notes if "an older report than the nearest earlier one" in n.message]
    assert len(older) == 1 and older[0].severity == "info" and older[0].source == "history"
    assert older[0].message.startswith("gsc.queries: the baseline is run run-b (2026-08-04 to 2026-08-31)")
    assert "run run-a: the Search Console query data was truncated" in older[0].message
    gap = [n.message for n in selection.notes if "ends 28 day(s) before" in n.message]
    assert gap == ["Baseline run run-b ends 28 day(s) before the current period starts."]


@pytest.mark.parametrize("reason", ["voided", "failed verification (1 problem(s)); not used", "unsupported snapshot format"])
def test_a_nearer_report_excluded_for_quality_reasons_is_noted_when_an_older_one_is_used(reason):
    skipped = (bl.SkippedRun("run-a", 1, *A, reason),)
    selection = select([loaded("run-b", B, 1)], skipped=skipped)  # the nearer report never became a candidate
    notes = [n.message for n in selection.notes if "an older report than the nearest earlier one" in n.message]
    assert len(notes) == len(el.DIMENSIONS)
    assert f"run run-a (2026-09-01 to 2026-09-28): {reason}" in notes[0]


@pytest.mark.parametrize("reason", ["same period as the current report", "overlaps the current period",
                                    "later than the current period", "superseded by revision 2",
                                    "outside the lookback of 3 earlier periods", "a gap of 40 days exceeds the 35-day limit"])
def test_structural_exclusions_do_not_count_as_choosing_an_older_report(reason):
    selection = select([loaded("run-b", B, 1)], skipped=(bl.SkippedRun("run-a", 1, *A, reason),))
    assert not any("older report" in n.message for n in selection.notes)


def test_a_voided_revision_of_a_candidate_period_does_not_trigger_the_note():
    skipped = (bl.SkippedRun("run-a2", 2, *A, "voided"),)  # revision 1 of the same period is the candidate
    assert not any("older report" in n.message for n in select([loaded("run-a1", A, 1)], skipped=skipped).notes)


def test_no_candidates_means_no_baselines_and_one_note():
    selection = select([])
    assert all(ref is None for ref in selection.baselines.values())
    assert [n.message for n in selection.notes] == ["No earlier report is available as a baseline for this client."]


def test_no_eligible_candidate_leaves_the_dimension_without_a_baseline():
    empty = te.live_like_gsc(current={"warnings": [gs.DataWarning("empty_period", "none")]})
    selection = select([loaded("run-a", A, 1, gsc=empty), loaded("run-b", B, 2, gsc=empty)])
    assert all(selection.baselines[d] is None for d in el.GSC_DIMENSIONS)
    assert all(selection.baselines[d] is not None for d in el.GA4_DIMENSIONS)
    assert len(selection.rejections["gsc.totals"]) == 2
    assert any(n.message.startswith("gsc.totals: no eligible baseline was found among the 2") for n in selection.notes)


def test_dimensions_the_current_report_cannot_support_are_not_searched():
    selection = select([loaded("run-a", A, 1)], current=current_payload(gsc=None))
    assert all(selection.baselines[d] is None and selection.rejections[d] == () for d in el.GSC_DIMENSIONS)
    assert not any("gsc." in n.message for n in selection.notes)
    assert selection.baselines["ga4.site"].run_id == "run-a"


def test_a_baseline_for_another_property_is_rejected_for_that_source_only():
    other_gsc = dataclasses.replace(he.complete_data()[0], site_url="https://other.test/")
    selection = select([loaded("run-a", A, 1, gsc=other_gsc), loaded("run-b", B, 2)])
    assert selection.baselines["gsc.totals"].run_id == "run-b"
    assert selection.baselines["ga4.site"].run_id == "run-a"
    assert selection.rejections["gsc.totals"] == (("run-a", "the Search Console property differs from the current report's"),)


def test_a_baseline_of_a_different_length_is_flagged():
    long_span = ("2026-08-29", "2026-09-28")  # 31 days
    selection = select([loaded("run-a", long_span, 1)])
    ref = selection.baselines["gsc.totals"]
    assert ref.same_length is False and ref.gap_days == 0
    assert any(n.severity == "warning" and "different number of days" in n.message for n in selection.notes)


def test_skipped_runs_are_passed_through():
    skipped = (bl.SkippedRun("old", 1, *B, "voided"),)
    assert select([loaded("run-a", A, 1)], skipped=skipped).skipped == skipped


def test_selection_is_deterministic_and_does_not_depend_on_candidate_order():
    candidates = [loaded("run-a", A, 1, gsc=truncated_gsc("query")), loaded("run-b", B, 2)]
    assert select(candidates) == select(candidates)
    assert select(list(reversed(candidates))) == select(candidates)  # candidates are ordered by rank


def test_notes_keep_run_ids_and_dates_intact_and_mask_email_addresses():
    message = "baseline run 01a18aac-9ef4-72ac-9487-5babee4e3089 (2026-08-04 to 2026-08-31) and run-001 (2026-09-01)"
    assert bl.note("info", "data_quality", message).message == message  # not mistaken for a phone number
    masked = bl.note("warning", "suppressed", "contact a.person@example.test about it")
    assert masked.message == "contact [email] about it"
    assert (masked.severity, masked.source, masked.category) == ("warning", "history", "suppressed")


def test_baselines_module_is_pure():
    source = (ROOT / "src" / "seo_report" / "snapshots" / "baselines.py").read_text(encoding="utf-8")
    for forbidden in ("import sqlite3", "import time", "import uuid", "import random", "import os", "open(",
                      "print(", "googleapiclient", "seo_report.sources", "seo_report.auth", "today("):
        assert forbidden not in source
