import copy
import dataclasses
import random
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import test_history_engine as teng
import test_history_rules as thr
import test_record_schema as trs
from seo_report import metrics as mt
from seo_report import record_schema
from seo_report.insights import engine
from seo_report.insights import history_engine as he
from seo_report.insights import history_recommendations as hrec
from seo_report.insights import history_rules as hr
from seo_report.insights.recommendations import Recommendation, RecommendationConfig
from seo_report.snapshots import comparison as cmp
from seo_report.snapshots import eligibility as el


def rec(key, priority=10, evidence=None, kind="evidence"):
    return Recommendation(key=key, title=f"Title {key}", action=f"Action for {key}.", trigger="Selected because x.",
                          evidence=dict(evidence or {}), supporting=(), kind=kind, priority=priority)


ALL_KEYS = ["accelerate_positions", "improve_ctr", "review_declining_keywords", "local_seo", "broaden_landing_pages",
            "improve_engagement", "review_key_events", "standing.backlinks", "standing.technical"]


def all_recommendations():
    return [rec(key, 10 * (n + 1), {"local_keywords.keywords": ("roof repair",)} if key == "local_seo" else {},
                "standing" if key.startswith("standing.") else "evidence") for n, key in enumerate(ALL_KEYS)]


def neutral_base(tmp_path):
    """A real comparison in which every dimension has an eligible baseline."""
    s = thr.scenario(tmp_path)
    assert all(status.eligible for status in s.comparison.status.values())
    # The fixture data really changes between the periods; make the figures the recommendations relate to neutral so
    # each test sets exactly the movement it needs.
    neutral = with_gsc_changes(s.comparison, position=position(29.0, 29.0), ctr=rate(0.004, 0.004))
    neutral = with_ga4_changes(neutral, engagement_rate=rate(0.3, 0.3, minimum=20, delta=0.05))
    assert neutral.gsc.totals.changes["position"].favorable is None
    return neutral


@pytest.fixture
def base(tmp_path):
    return neutral_base(tmp_path)


@pytest.fixture
def uuid_base(tmp_path, monkeypatch):
    """The same comparison, but with UUID run IDs like the real store creates (the finalized record requires them)."""
    import test_history_access as tha
    monkeypatch.setattr(thr.tss, "make_store", lambda path, **kwargs: tha.new_store(path))
    comparison = neutral_base(tmp_path)
    assert re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
                        comparison.status["gsc.totals"].baseline.run_id)
    return comparison


def with_gsc_changes(comparison, **changes):
    totals = dataclasses.replace(comparison.gsc.totals, changes={**comparison.gsc.totals.changes, **changes})
    return dataclasses.replace(comparison, gsc=dataclasses.replace(comparison.gsc, totals=totals))


def with_ga4_changes(comparison, **changes):
    site = dataclasses.replace(comparison.ga4.site, changes={**comparison.ga4.site.changes, **changes})
    return dataclasses.replace(comparison, ga4=dataclasses.replace(comparison.ga4, site=site))


def with_rankings(comparison, **lists):
    return dataclasses.replace(comparison, rankings=dataclasses.replace(comparison.rankings, **lists))


def with_status(comparison, dimension, **changes):
    status = dict(comparison.status)
    status[dimension] = dataclasses.replace(status[dimension], **changes)
    return dataclasses.replace(comparison, status=status)


def position(current, previous):
    return mt.compare_position(current, previous)


def rate(current, previous, volume=1000, minimum=500, delta=0.001):
    return mt.compare_rate(current, previous, current_volume=volume, previous_volume=volume, min_volume=minimum,
                           min_delta=delta)


def keyword(name, location="Dallas", current="above_10", baseline=7, state="not_comparable"):
    def view(kind, value):
        return None if kind is None else {"kind": kind, "position": value, "label": "Above 10" if kind == "above_10" else None}
    base_kind = "position" if isinstance(baseline, int) else baseline
    return cmp.KeywordComparison(name, location, state, view(current, None if current == "above_10" else 3),
                                 view(base_kind, baseline if isinstance(baseline, int) else None), None, "reason", None)


def annotate(recs, comparison, history_insights=None):
    return hrec.annotate_recommendations(recommendations=recs, comparison=comparison, history_insights=history_insights)


def by_key(result):
    return {a.recommendation_key: a for a in result.annotations}


# --- recommendation preservation -------------------------------------------------------------------------------------

def test_one_annotation_per_recommendation_in_the_recommendations_own_order(base):
    recs = all_recommendations()
    result = annotate(recs, base)
    assert [a.recommendation_key for a in result.annotations] == ALL_KEYS
    assert len(result.annotations) == len(recs) == 9
    reordered = list(reversed(recs))
    assert [a.recommendation_key for a in annotate(reordered, base).annotations] == list(reversed(ALL_KEYS))


def test_the_recommendations_are_not_changed(base):
    recs = all_recommendations()
    before = copy.deepcopy(recs)
    snapshot = [(id(r), dataclasses.asdict(r)) for r in recs]
    changed = with_gsc_changes(base, position=position(35, 29), ctr=rate(0.002, 0.004))
    annotate(recs, changed)
    assert recs == before and [(id(r), dataclasses.asdict(r)) for r in recs] == snapshot
    assert [r.key for r in recs] == ALL_KEYS and [r.priority for r in recs] == [10 * (n + 1) for n in range(9)]
    assert [r.action for r in recs] == [f"Action for {k}." for k in ALL_KEYS]


def test_no_recommendation_is_created_or_removed(base):
    recs = all_recommendations()[:3]
    result = annotate(recs, with_gsc_changes(base, position=position(35, 29)))
    assert {a.recommendation_key for a in result.annotations} == {r.key for r in recs}
    assert annotate([], base).annotations == ()
    assert annotate((), None).annotations == ()


def test_annotations_are_not_added_to_the_recommendation_objects(base):
    recs = all_recommendations()
    names = {f.name for f in dataclasses.fields(Recommendation)}
    annotate(recs, with_gsc_changes(base, position=position(35, 29)))
    assert all(set(vars(r)) == names for r in recs)
    assert {f.name for f in dataclasses.fields(hrec.RecommendationAnnotation)} == {"recommendation_key", "history_support"}
    assert not isinstance(hrec.RecommendationAnnotation("x"), Recommendation)


def test_repeated_or_keyless_recommendations_are_programming_errors(base):
    with pytest.raises(ValueError, match="more than once"):
        annotate([rec("improve_ctr"), rec("improve_ctr")], base)
    with pytest.raises(TypeError):
        annotate([object()], base)
    with pytest.raises(TypeError):
        annotate([SimpleNamespaceWithKey("")], base)


class SimpleNamespaceWithKey:
    def __init__(self, key):
        self.key = key


# --- matching ---------------------------------------------------------------------------------------------------------

def test_gsc_position_history_is_matched_to_the_position_recommendation(base):
    worse = with_gsc_changes(base, position=position(35.0, 29.0))
    annotation = by_key(annotate(all_recommendations(), worse))["accelerate_positions"]
    (item,) = annotation.history_support
    assert (item.classification, item.source, item.subject, item.dimension) == ("supports", "gsc_position", None,
                                                                                "gsc.totals")
    assert item.evidence == {"current_value": 35.0, "baseline_value": 29.0, "direction": 1, "baseline_days": 28}
    assert annotation.status == "supports"


def test_gsc_ctr_history_is_matched_to_the_click_through_recommendation(base):
    fell = with_gsc_changes(base, ctr=rate(0.002, 0.004))
    (item,) = by_key(annotate(all_recommendations(), fell))["improve_ctr"].history_support
    assert (item.classification, item.source, item.dimension) == ("supports", "gsc_ctr", "gsc.totals")
    assert item.evidence["direction"] == -1 and item.evidence["current_value"] == 0.002


def test_ga4_engagement_history_is_matched_only_to_the_engagement_recommendation(base):
    fell = with_ga4_changes(base, engagement_rate=rate(0.2, 0.4, minimum=20, delta=0.05))
    result = by_key(annotate(all_recommendations(), fell))
    (item,) = result["improve_engagement"].history_support
    assert (item.classification, item.source, item.dimension) == ("supports", "ga4_engagement_rate", "ga4.site")
    others = [a for k, a in result.items() if k != "improve_engagement" and a.history_support]
    assert others == []


def test_unrelated_gsc_evidence_is_not_matched_to_a_ga4_recommendation(base):
    changed = with_gsc_changes(base, position=position(35, 29), ctr=rate(0.002, 0.004))
    result = by_key(annotate(all_recommendations(), changed))
    assert result["improve_engagement"].history_support == ()
    assert {i.source for a in result.values() for i in a.history_support} == {"gsc_position", "gsc_ctr"}
    assert result["accelerate_positions"].history_support[0].source == "gsc_position"
    assert result["improve_ctr"].history_support[0].source == "gsc_ctr"  # each only its own metric


def test_geography_evidence_is_not_matched_to_a_landing_page_recommendation(base):
    share = rate(0.8, 0.4, minimum=500, delta=0.05)
    changed = dataclasses.replace(base, gsc=dataclasses.replace(base.gsc, target_country_share=share))
    result = by_key(annotate(all_recommendations(), changed))
    assert result["broaden_landing_pages"].history_support == ()
    assert all(not a.history_support for a in result.values())  # nothing relates to the country share


def test_a_technical_recommendation_is_not_supported_by_unrelated_ranking_movement(base):
    moved = with_rankings(base, moved=(dataclasses.replace(keyword("roof repair", current="position", baseline=2),
                                                           state="declined", movement=-5),))
    changed = with_gsc_changes(moved, position=position(40, 20))
    result = by_key(annotate(all_recommendations(), changed))
    assert result["standing.technical"].history_support == () and result["standing.backlinks"].history_support == ()
    assert result["review_declining_keywords"].history_support == ()


def test_recommendations_without_an_establishable_relation_never_receive_support(base):
    assert set(hrec.RELATIONS) == {"accelerate_positions", "improve_ctr", "improve_engagement", "local_seo"}
    everything = with_ga4_changes(with_gsc_changes(base, position=position(40, 20), ctr=rate(0.001, 0.004)),
                                  engagement_rate=rate(0.1, 0.5, minimum=20, delta=0.05))
    result = by_key(annotate(all_recommendations(), everything))
    for key in ("review_declining_keywords", "broaden_landing_pages", "review_key_events", "standing.backlinks",
                "standing.technical"):
        assert result[key].history_support == () and result[key].status == "no_usable_support"


def test_a_keyword_recommendation_gets_evidence_only_for_its_own_keywords(base):
    listed = (keyword("Roof Repair", baseline=7), keyword("Gutter Repair", baseline=4))
    changed = with_rankings(base, not_comparable=listed)
    (item,) = by_key(annotate(all_recommendations(), changed))["local_seo"].history_support
    assert (item.source, item.subject, item.classification, item.dimension) == (
        "ranking_keyword", "roof repair", "supports", "rankings.keywords")
    assert item.evidence == {"baseline_value": 7, "baseline_days": 28}
    other = [rec("local_seo", evidence={"local_keywords.keywords": ("gutter repair",)})]
    (other_item,) = by_key(annotate(other, changed))["local_seo"].history_support
    assert other_item.subject == "gutter repair" and other_item.evidence["baseline_value"] == 4
    nothing = [rec("local_seo", evidence={"local_keywords.keywords": ("siding install",)})]
    assert by_key(annotate(nothing, changed))["local_seo"].history_support == ()


def test_keyword_matching_is_case_and_spacing_insensitive_but_exact(base):
    changed = with_rankings(base, not_comparable=(keyword("  ROOF   repair "),))
    assert by_key(annotate(all_recommendations(), changed))["local_seo"].history_support
    partial = with_rankings(base, not_comparable=(keyword("roof repair services"),))
    assert by_key(annotate(all_recommendations(), partial))["local_seo"].history_support == ()


@pytest.mark.parametrize("item", [
    keyword("roof repair", baseline=14),              # was already beyond page 1
    keyword("roof repair", baseline=11),
    keyword("roof repair", baseline="above_10"),      # not numeric before
    keyword("roof repair", baseline="missing"),
    keyword("roof repair", current="position"),       # not "Above 10" now
    keyword("roof repair", current="missing"),
    dataclasses.replace(keyword("roof repair"), baseline=None),
    dataclasses.replace(keyword("roof repair"), current=None),
])
def test_keyword_history_that_does_not_establish_the_relation_gives_no_support(base, item):
    assert by_key(annotate(all_recommendations(), with_rankings(base, not_comparable=(item,))))[
        "local_seo"].history_support == ()


def test_page_one_boundary_for_keywords(base):
    for baseline_position, expected in ((1, 1), (10, 1), (11, 0)):
        changed = with_rankings(base, not_comparable=(keyword("roof repair", baseline=baseline_position),))
        assert len(by_key(annotate(all_recommendations(), changed))["local_seo"].history_support) == expected


def test_ambiguous_keyword_relationships_produce_no_usable_support(base):
    twice = with_rankings(base, not_comparable=(keyword("roof repair", "Dallas"), keyword("roof repair", "Plano")))
    assert by_key(annotate(all_recommendations(), twice))["local_seo"].history_support == ()
    across = with_rankings(base, not_comparable=(keyword("roof repair"),), new_keywords=(keyword("roof repair"),))
    assert by_key(annotate(all_recommendations(), across))["local_seo"].history_support == ()


def test_the_number_of_keyword_items_is_capped(base):
    names = tuple(f"keyword {n}" for n in range(9))
    recs = [rec("local_seo", evidence={"local_keywords.keywords": names})]
    changed = with_rankings(base, not_comparable=tuple(keyword(n) for n in names))
    items = by_key(annotate(recs, changed))["local_seo"].history_support
    assert len(items) == hrec.MAX_KEYWORD_ITEMS == 5
    assert [i.subject for i in items] == sorted(names)[:5]


def test_a_recommendation_without_keyword_evidence_gets_nothing(base):
    changed = with_rankings(base, not_comparable=(keyword("roof repair"),))
    for evidence in ({}, {"local_keywords.keywords": ()}, {"local_keywords.keywords": ("", "  ")},
                     {"local_keywords.keywords": (5, None)}):
        assert by_key(annotate([rec("local_seo", evidence=evidence)], changed))["local_seo"].history_support == ()


# --- support classification --------------------------------------------------------------------------------------------

def test_supporting_evidence_is_classified_as_supports(base):
    result = by_key(annotate(all_recommendations(), with_gsc_changes(base, position=position(35, 29))))
    assert result["accelerate_positions"].status == "supports"
    assert result["accelerate_positions"].history_support[0].classification == "supports"


def test_evidence_that_moves_the_other_way_is_classified_as_qualifies(base):
    better = with_gsc_changes(base, position=position(20.0, 29.0), ctr=rate(0.006, 0.004))
    result = by_key(annotate(all_recommendations(), better))
    for key, source in (("accelerate_positions", "gsc_position"), ("improve_ctr", "gsc_ctr")):
        (item,) = result[key].history_support
        assert (item.classification, item.source, result[key].status) == ("qualifies", source, "qualifies")
        assert item.evidence["direction"] in (-1, 1)


def test_a_figure_that_did_not_change_meaningfully_gives_no_support(base):
    flat = with_gsc_changes(base, position=position(29.5, 29.0), ctr=rate(0.0041, 0.004))
    assert flat.gsc.totals.changes["position"].favorable is None
    result = by_key(annotate(all_recommendations(), flat))
    assert result["accelerate_positions"].history_support == () and result["improve_ctr"].history_support == ()
    assert result["accelerate_positions"].status == "no_usable_support"


def test_the_three_statuses_and_their_definitions():
    assert (hrec.STATUS_SUPPORTS, hrec.STATUS_QUALIFIES, hrec.STATUS_NONE) == ("supports", "qualifies",
                                                                               "no_usable_support")
    assert hrec.CLASSIFICATIONS == ("supports", "qualifies")
    mixed = hrec.RecommendationAnnotation("k", (
        hrec.HistorySupport("supports", "gsc_ctr", None, "gsc.totals", "run", "2026-09-01", "2026-09-28", {}),
        hrec.HistorySupport("qualifies", "gsc_position", None, "gsc.totals", "run", "2026-09-01", "2026-09-28", {})))
    assert mixed.status == "qualifies"
    assert hrec.RecommendationAnnotation("k").status == "no_usable_support"


def test_no_relevant_historical_evidence_gives_no_usable_support(base):
    result = annotate(all_recommendations(), base)
    assert all(a.history_support == () and a.status == "no_usable_support" for a in result.annotations)
    assert annotate(all_recommendations(), None).annotations == tuple(
        hrec.RecommendationAnnotation(k) for k in ALL_KEYS)


@pytest.mark.parametrize("dimension,change", [("gsc.totals", {"position": position(35, 29)}),
                                              ("gsc.totals", {"ctr": rate(0.002, 0.004)})])
def test_an_ineligible_dimension_never_gives_support(base, dimension, change):
    changed = with_status(with_gsc_changes(base, **change), dimension, eligible=False,
                          reasons=("no eligible earlier report within the lookback",), baseline=None)
    result = annotate(all_recommendations(), changed)
    assert all(a.history_support == () for a in result.annotations)


def test_a_missing_baseline_never_gives_support(base):
    changed = with_status(with_gsc_changes(base, position=position(35, 29)), "gsc.totals", baseline=None)
    assert by_key(annotate(all_recommendations(), changed))["accelerate_positions"].history_support == ()
    gone = with_status(with_rankings(base, not_comparable=(keyword("roof repair"),)), el.RANKINGS, baseline=None)
    assert by_key(annotate(all_recommendations(), gone))["local_seo"].history_support == ()
    off = with_status(with_rankings(base, not_comparable=(keyword("roof repair"),)), el.RANKINGS, eligible=False)
    assert by_key(annotate(all_recommendations(), off))["local_seo"].history_support == ()


def test_an_invalidating_caveat_gives_no_support_but_the_informational_note_does_not(base):
    changed = with_gsc_changes(base, position=position(35, 29))
    caveated = with_status(changed, "gsc.totals", caveats=("a sampling caveat",))
    assert by_key(annotate(all_recommendations(), caveated))["accelerate_positions"].history_support == ()
    note = with_status(changed, "gsc.totals", caveats=("only the top rows were stored, so new or lost items cannot "
                                                       "be reported",))
    assert by_key(annotate(all_recommendations(), note))["accelerate_positions"].history_support


def blocked_history(dimension, reason, kind="gsc_historical_change"):
    finding = teng.finding("history.review.x", 110, section="search_console", kind=kind, dimension=dimension,
                           blocked_reason=reason)
    return he.compose_history_insights(rule_output=hr.HistoryRuleResult(reviewer_findings=(finding,)))


@pytest.mark.parametrize("reason", sorted(hrec.QUALITY_BLOCKS))
def test_findings_blocked_by_stage_6c_4_for_data_quality_never_give_support(base, reason):
    changed = with_gsc_changes(base, position=position(35, 29), ctr=rate(0.002, 0.004))
    history = blocked_history("gsc.totals", reason)
    result = annotate(all_recommendations(), changed, history)
    assert all(a.history_support == () for a in result.annotations)
    assert annotate(all_recommendations(), changed, None).annotations[0].history_support  # without the block: support


def test_a_block_on_one_dimension_does_not_remove_support_from_another(base):
    changed = with_ga4_changes(with_gsc_changes(base, position=position(35, 29)),
                               engagement_rate=rate(0.2, 0.4, minimum=20, delta=0.05))
    result = by_key(annotate(all_recommendations(), changed, blocked_history("gsc.totals", "caveat_present")))
    assert result["accelerate_positions"].history_support == () and result["improve_engagement"].history_support


def test_a_client_wording_block_is_not_a_data_quality_block(base):
    changed = with_gsc_changes(base, position=position(35, 29))
    history = blocked_history("gsc.totals", "live_previous_not_empty")
    assert by_key(annotate(all_recommendations(), changed, history))["accelerate_positions"].history_support
    assert "live_previous_not_empty" not in hrec.QUALITY_BLOCKS and hrec.QUALITY_BLOCKS <= set(hr.BLOCK_REASONS)


def test_not_comparable_changes_give_no_support(base):
    unusable = with_gsc_changes(base, position=mt.Change(None, None, None, None, mt.Direction.NOT_COMPARABLE, None),
                                ctr=rate(0.002, 0.004, volume=100))  # below the 500-impression minimum
    assert not unusable.gsc.totals.changes["ctr"].comparable
    result = by_key(annotate(all_recommendations(), unusable))
    assert result["accelerate_positions"].history_support == () and result["improve_ctr"].history_support == ()
    assert annotate(all_recommendations(), with_gsc_changes(base, position=None)).annotations[0].history_support == ()


# --- the exact history_support shape ------------------------------------------------------------------------------------

def test_every_annotation_references_an_existing_unique_recommendation_key(base):
    recs = all_recommendations()
    changed = with_gsc_changes(base, position=position(35, 29), ctr=rate(0.002, 0.004))
    result = annotate(recs, changed)
    keys = [a.recommendation_key for a in result.annotations]
    assert set(keys) <= {r.key for r in recs} and len(keys) == len(set(keys))


def test_history_support_items_have_exactly_the_approved_shape(base):
    recs = all_recommendations()
    changed = with_rankings(with_gsc_changes(base, position=position(35, 29), ctr=rate(0.006, 0.004)),
                            not_comparable=(keyword("roof repair"),))
    entries = annotate(recs, changed).as_record()
    items = [item for entry in entries for item in entry["history_support"]]
    assert {i["source"] for i in items} == {"gsc_position", "gsc_ctr", "ranking_keyword"}
    for item in items:
        assert list(item) == list(hrec.SUPPORT_FIELDS)
        assert hrec.validate_history_support(item) == []
        assert item["classification"] in ("supports", "qualifies") and item["dimension"] in el.DIMENSIONS
        assert set(item["baseline_period"]) == {"start", "end"} and item["baseline_run_id"]
        assert item["rules_version"] == "1"
    for entry in entries:
        assert set(entry) == {"recommendation_key", "history_support"}


def test_evidence_is_traceable_flat_and_registered(base):
    changed = with_rankings(with_gsc_changes(base, position=position(35, 29)),
                            not_comparable=(keyword("roof repair"),))
    result = annotate(all_recommendations(), changed)
    ref_gsc = base.status["gsc.totals"].baseline
    ref_rank = base.status[el.RANKINGS].baseline
    for annotation in result.annotations:
        for item in annotation.history_support:
            ref = ref_rank if item.dimension == el.RANKINGS else ref_gsc
            assert item.baseline_run_id == ref.run_id
            assert (item.baseline_period_start, item.baseline_period_end) == (ref.period_start, ref.period_end)
            assert hr.validate_evidence(item.evidence, audience="reviewer") == []
            assert set(item.evidence) <= set(hr.EVIDENCE_KEYS)
            assert all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in item.evidence.values())


def good_item():
    return {"classification": "supports", "source": "gsc_ctr", "subject": None, "dimension": "gsc.totals",
            "baseline_run_id": "run-1", "baseline_period": {"start": "2026-09-01", "end": "2026-09-28"},
            "evidence": {"current_value": 0.002, "baseline_value": 0.004, "direction": -1, "baseline_days": 28},
            "rules_version": "1"}


def test_the_item_validator_accepts_the_exact_shape_and_rejects_everything_else():
    assert hrec.validate_history_support(good_item()) == []
    keyword_item = dict(good_item(), source="ranking_keyword", subject="roof repair", dimension="rankings.keywords")
    assert hrec.validate_history_support(keyword_item) == []
    bad = [
        {k: v for k, v in good_item().items() if k != "evidence"}, dict(good_item(), extra=1),
        dict(good_item(), classification="complicates"), dict(good_item(), source="gsc_clicks"),
        dict(good_item(), subject="x"), dict(keyword_item, subject=None), dict(keyword_item, subject=" "),
        dict(good_item(), dimension="gsc.other"), dict(good_item(), baseline_run_id=""),
        dict(good_item(), baseline_period={"start": "2026-09-28", "end": "2026-09-01"}),
        dict(good_item(), baseline_period={"start": "2026-09-01"}),
        dict(good_item(), evidence={"nested": {"a": 1}}), dict(good_item(), evidence={"current_value": "6"}),
        dict(good_item(), evidence={"current_value": float("nan")}), dict(good_item(), evidence={"made_up": 1}),
        dict(good_item(), rules_version=""), None, [], "x"]
    for item in bad:
        assert hrec.validate_history_support(item), item


# --- compatibility with record_schema (which is unchanged) ------------------------------------------------------------------

def all_sources_comparison(comparison):
    changed = with_gsc_changes(comparison, position=position(35, 29), ctr=rate(0.006, 0.004))
    changed = with_ga4_changes(changed, engagement_rate=rate(0.2, 0.4, minimum=20, delta=0.05))
    return with_rankings(changed, not_comparable=(keyword("roof repair", baseline=7), keyword("gutter repair", baseline=4)))


def producer_recommendations():
    return [rec("accelerate_positions"), rec("improve_ctr"), rec("improve_engagement"),
            rec("local_seo", evidence={"local_keywords.keywords": ("roof repair", "gutter repair")})]


def test_annotations_without_support_are_accepted_by_the_record_schema(base):
    entries = annotate(all_recommendations()[:5], base).as_record()
    assert all(entry["history_support"] == [] for entry in entries)
    record = trs.finalized()
    record["recommendation_annotations"] = entries
    assert record_schema.validate_finalized_record(record) is record


def test_a_valid_non_empty_support_item_is_accepted_by_the_record_schema(uuid_base):
    entries = annotate(all_recommendations(), with_gsc_changes(uuid_base, position=position(35, 29))).as_record()
    populated = [entry for entry in entries if entry["history_support"]]
    assert [entry["recommendation_key"] for entry in populated] == ["accelerate_positions"]
    record = trs.finalized()
    record["recommendation_annotations"] = populated
    assert record_schema.validate_finalized_record(record) is record


def test_every_item_the_public_api_produces_passes_finalized_record_validation(uuid_base):
    result = annotate(producer_recommendations(), all_sources_comparison(uuid_base))
    entries = result.as_record()
    items = [item for entry in entries for item in entry["history_support"]]
    assert {item["source"] for item in items} == set(hrec.SOURCES)
    assert {item["classification"] for item in items} == {"supports", "qualifies"}
    assert sum(1 for item in items if item["source"] == "ranking_keyword") == 2
    record = trs.finalized()
    record["recommendation_annotations"] = entries
    assert record_schema.validate_finalized_record(record) is record
    for item in items:
        assert hrec.validate_history_support(item) == []


def uuid_item(source="gsc_ctr"):
    item = trs.support(source)
    item["classification"] = "supports"
    return item


def schema_accepts(item) -> bool:
    record = trs.finalized()
    record["recommendation_annotations"] = [{"recommendation_key": "k", "history_support": [item]}]
    try:
        record_schema.validate_finalized_record(record)
    except record_schema.RecordSchemaError:
        return False
    return True


def mutations():
    """Items that violate a contract both validators share."""
    bad = []
    for field in hrec.SUPPORT_FIELDS:
        item = uuid_item()
        del item[field]
        bad.append(item)
    keyword_item = uuid_item("ranking_keyword")
    bad += [dict(uuid_item(), extra=1), dict(uuid_item(), classification="complicates"),
            dict(uuid_item(), classification=None), dict(uuid_item(), source="gsc_clicks"),
            dict(uuid_item(), subject="roof repair"), dict(keyword_item, subject=None), dict(keyword_item, subject="  "),
            dict(keyword_item, subject=5), dict(uuid_item(), dimension="gsc.other"), dict(uuid_item(), dimension=None),
            dict(uuid_item(), baseline_run_id=""), dict(uuid_item(), baseline_run_id=None),
            dict(uuid_item(), baseline_period={"start": "2026-09-28", "end": "2026-09-01"}),
            dict(uuid_item(), baseline_period={"start": "2026-09-01"}),
            dict(uuid_item(), baseline_period={"start": "x", "end": "2026-09-28"}),
            dict(uuid_item(), baseline_period=None), dict(uuid_item(), rules_version=""),
            dict(uuid_item(), rules_version=None), dict(uuid_item(), evidence=None), dict(uuid_item(), evidence=[]),
            None, [], "x", 5]
    for key, value in (("made_up", 1), ("current_value", "6"), ("current_value", {"a": 1}),
                       ("current_value", float("nan")), ("current_value", float("inf")), ("current_value", None),
                       ("current_value", [1])):
        item = uuid_item()
        item["evidence"][key] = value
        bad.append(item)
    return bad


def test_every_item_the_producer_validator_rejects_is_rejected_by_the_record_schema():
    assert uuid_item() and hrec.validate_history_support(uuid_item()) == [] and schema_accepts(uuid_item()) is True
    for item in mutations():
        assert hrec.validate_history_support(item), item
        assert not schema_accepts(item), item


def test_every_item_the_schema_accepts_for_a_shared_contract_is_rejected_by_the_producer_validator():
    for item in mutations():
        assert not schema_accepts(item)
        assert hrec.validate_history_support(item), item


def test_the_schema_is_deliberately_stricter_than_the_producer_validator_in_these_documented_ways():
    stricter = {
        "baseline_run_id must be a UUID": dict(uuid_item(), baseline_run_id="run-001"),
        "the dimension must match the source": dict(uuid_item(), dimension="ga4.site"),
        "the keyword dimension must match": dict(uuid_item("ranking_keyword"), dimension="gsc.totals"),
    }
    reviewer_only = uuid_item()
    reviewer_only["evidence"]["pending_fallbacks"] = 1
    stricter["a reviewer-only evidence key is not allowed"] = reviewer_only
    unused = uuid_item("ranking_keyword")
    unused["evidence"]["current_value"] = 1
    stricter["a registered key the source does not use is not allowed"] = unused
    missing = uuid_item()
    del missing["evidence"]["direction"]
    stricter["every evidence key of the source is required"] = missing
    direction = uuid_item()
    direction["evidence"]["direction"] = 2
    stricter["direction is -1, 0 or 1"] = direction
    days = uuid_item()
    days["evidence"]["baseline_days"] = 0
    stricter["baseline_days is a positive whole number"] = days
    for reason, item in stricter.items():
        assert hrec.validate_history_support(item) == [], reason  # the producer validator accepts it ...
        assert not schema_accepts(item), reason  # ... the stored record does not


def test_what_the_producer_actually_emits_always_satisfies_the_stricter_schema_rules(uuid_base):
    result = annotate(producer_recommendations(), all_sources_comparison(uuid_base))
    for annotation in result.annotations:
        for item in annotation.history_support:
            assert item.evidence.keys() == set(record_schema.SUPPORT_EVIDENCE_KEYS[item.source])
            assert item.dimension == record_schema.SUPPORT_SOURCE_DIMENSIONS[item.source]
            assert item.evidence["baseline_days"] == 28 and item.evidence.get("direction", 1) in (-1, 1)


def test_the_schema_and_producer_share_their_constants():
    assert record_schema.SUPPORT_FIELDS == hrec.SUPPORT_FIELDS
    assert record_schema.SUPPORT_CLASSIFICATIONS == hrec.CLASSIFICATIONS
    assert record_schema.SUPPORT_SOURCES == hrec.SOURCES
    assert record_schema.MAX_SUPPORT_ITEMS == hrec.MAX_KEYWORD_ITEMS
    assert set(record_schema.SUPPORT_SOURCE_DIMENSIONS) == {relation.source for relations in hrec.RELATIONS.values()
                                                            for relation in relations}
    for relations in hrec.RELATIONS.values():
        for relation in relations:
            assert record_schema.SUPPORT_SOURCE_DIMENSIONS[relation.source] == relation.dimension

# --- reviewer/client separation ---------------------------------------------------------------------------------------------

def test_annotations_are_reviewer_only_and_separate_from_client_content(base):
    from seo_report import report_run
    assert not issubclass(hrec.RecommendationHistoryAnnotations, report_run.ClientContent)
    assert "annotations" not in {f.name for f in dataclasses.fields(report_run.ClientContent)}
    assert "history_support" not in {f.name for f in dataclasses.fields(report_run.ClientContent)}
    assert "history_support" not in {f.name for f in dataclasses.fields(Recommendation)}
    changed = with_gsc_changes(base, position=position(35, 29))
    result = annotate(all_recommendations(), changed)
    assert not any(isinstance(item, Recommendation) for annotation in result.annotations
                   for item in (annotation, *annotation.history_support))


def test_annotations_carry_no_paths_secrets_or_raw_objects(base):
    changed = with_rankings(with_gsc_changes(base, position=position(35, 29)), not_comparable=(keyword("roof repair"),))
    dump = repr(annotate(all_recommendations(), changed))
    for forbidden in ("sqlite", "token", "Traceback", "HistoricalComparison", "DimensionStatus", "Change("):
        assert forbidden not in dump
    assert not re.search(r"[A-Za-z]:\\", dump)


# --- determinism and purity --------------------------------------------------------------------------------------------------

def test_repeated_calls_are_equivalent_and_inputs_are_unchanged(base):
    recs = all_recommendations()
    changed = with_rankings(with_gsc_changes(base, position=position(35, 29)), not_comparable=(keyword("roof repair"),))
    recs_before, comparison_before = copy.deepcopy(recs), copy.deepcopy(changed)
    first, second = annotate(recs, changed), annotate(recs, changed)
    assert first == second and recs == recs_before and changed == comparison_before


def test_shuffled_historical_input_does_not_change_the_result(base):
    names = tuple(f"keyword {n}" for n in range(8))
    recs = [rec("local_seo", evidence={"local_keywords.keywords": names})]
    items = [keyword(n, baseline=n_pos) for n, n_pos in zip(names, (3, 9, 1, 10, 5, 2, 8, 4))]
    expected = annotate(recs, with_rankings(base, not_comparable=tuple(items)))
    generator = random.Random(3)
    for _ in range(20):
        shuffled = list(items)
        generator.shuffle(shuffled)
        assert annotate(recs, with_rankings(base, not_comparable=tuple(shuffled))) == expected
    shuffled_names = list(names)
    generator.shuffle(shuffled_names)
    again = annotate([rec("local_seo", evidence={"local_keywords.keywords": tuple(shuffled_names)})],
                     with_rankings(base, not_comparable=tuple(items)))
    assert again == expected


def test_real_recommendations_and_comparison_are_annotated_without_changing_them(tmp_path):
    s = thr.scenario(tmp_path, current=dict(thr.CURRENT_DEFAULT, empty_previous=False))
    insights = engine.generate_insights(gsc=s.gsc, ga4=s.ga4, keywords=thr.te.mixed_keywords(),
                                        target_country="United States",
                                        recommendation_config=RecommendationConfig(standing=("backlinks", "technical")))
    recs = list(insights.recommendations)
    assert recs
    before = copy.deepcopy(insights)
    result = annotate(recs, s.comparison, he.compose_history_insights(rule_output=thr.evaluate(s)))
    assert [a.recommendation_key for a in result.annotations] == [r.key for r in recs]
    assert insights == before and list(insights.recommendations) == recs
    assert all(hrec.validate_history_support(i) == [] for entry in result.as_record() for i in entry["history_support"])
    assert {a.recommendation_key for a in result.annotations if a.history_support} <= set(hrec.RELATIONS)


def test_the_module_is_pure():
    source = (ROOT / "src" / "seo_report" / "insights" / "history_recommendations.py").read_text(encoding="utf-8")
    code = re.sub(r'""".*?"""', "", source, flags=re.S)
    for pattern in (r"\.now\(", r"today\(", r"\btime\.", r"\bopen\(", r"write_text", r"write_bytes", r"\.mkdir\(",
                    r"sqlite", r"requests", r"googleapiclient", r"random", r"\bos\.", r"environ"):
        assert not re.search(pattern, code), pattern
    assert not re.search(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}", repr(hrec.RecommendationHistoryAnnotations()))
