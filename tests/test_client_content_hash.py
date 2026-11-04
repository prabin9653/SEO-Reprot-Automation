import copy
import dataclasses
import datetime as dt
import json
import random
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import test_history_engine as teng
import test_report_run as trr
from test_history_access import env  # noqa: F401  (temporary history and output folders)
from seo_report import client_content_hash as cch
from seo_report import rankings as rk
from seo_report import report_run
from seo_report.insights import history_engine as he
from seo_report.insights import history_rules as hr
from seo_report.insights.engine import SectionInsights
from seo_report.insights.recommendations import Recommendation
from seo_report.insights.rules import Insight, ReviewerNote, Tone
from seo_report.periods import Period, ReportPeriods

PERIODS = ReportPeriods(Period(dt.date(2026, 9, 29), dt.date(2026, 10, 26)),
                        Period(dt.date(2026, 9, 1), dt.date(2026, 9, 28)))


def insight(key="gsc.clicks", **overrides):
    values = dict(section="search_console", key=key, text="The website earned 6 organic clicks.", tone=Tone.NEUTRAL,
                  priority=10, evidence={"current": 6, "previous": None, "direction": "not_comparable"},
                  tags=frozenset({"gsc_clicks"}))
    values.update(overrides)
    return Insight(**values)


def section(key="search_console", **overrides):
    values = dict(key=key, title="Google Search Console", paragraphs=("First paragraph.", "Second paragraph."),
                  insights=(insight(),), omitted=("gsc.ctr",), status="ok", has_strength=True)
    values.update(overrides)
    return SectionInsights(**values)


def recommendation(key="improve_ctr", **overrides):
    values = dict(key=key, title="Refresh titles", action="Rewrite the titles.", trigger="Selected because x.",
                  evidence={"gsc.zero_click_queries.zero_click_queries": 3, "local_keywords.keywords": ("a", "b")},
                  supporting=("gsc.zero_click_queries",), kind="evidence", priority=20)
    values.update(overrides)
    return Recommendation(**values)


def row(keyword="roof repair", **overrides):
    values = dict(keyword=keyword, target_location="Dallas", target_url="https://example.test/roofing",
                  previous=rk.parse_ranking(5), current=rk.parse_ranking(3), movement=2)
    values.update(overrides)
    return report_run.RankingRow(**values)


def statement(**overrides):
    values = dict(section="search_console", key="history.gsc.clicks", dimension="gsc.totals",
                  text="Organic clicks were up from 4 in the report for September 1–28, 2026.", tone=Tone.POSITIVE,
                  priority=10, evidence={"current_value": 6, "baseline_value": 4, "direction": 1, "baseline_days": 28},
                  baseline_period_start="2026-09-01", baseline_period_end="2026-09-28", rules_version="1",
                  group="since_last_report")
    values.update(overrides)
    return he.ClientHistoryStatement(**values)


def content(**overrides):
    values = dict(
        client=report_run.ClientIdentity("Example Co", "https://example.test/", "United States", "Dallas"),
        periods=PERIODS, sections=(section(), section("ga4", title="Google Analytics 4", paragraphs=("GA4 text.",),
                                                       insights=(insight("ga4.sessions", section="ga4"),))),
        recommendations=(recommendation(), recommendation("local_seo", priority=40)),
        ranking_rows=(row(), row("gutter repair", previous=rk.parse_ranking(""), current=rk.parse_ranking("Above 10"),
                                 movement=None)),
        historical_statements=(), ranking_markers=(), ranking_footnote=None)
    values.update(overrides)
    return report_run.ClientContent(**values)


def digest(c, **kwargs):
    return cch.client_content_sha256(c, **kwargs)


# --- the contract itself ---------------------------------------------------------------------------------------------

def test_the_hash_is_a_full_lowercase_sha256_of_the_canonical_text():
    c = content()
    result = digest(c)
    assert re.fullmatch(r"[0-9a-f]{64}", result)
    import hashlib
    assert result == hashlib.sha256(cch.canonical_client_json(c).encode("utf-8")).hexdigest()


def test_identical_content_gives_the_same_hash_every_time():
    assert digest(content()) == digest(content()) == digest(copy.deepcopy(content()))
    assert cch.canonical_client_json(content()) == cch.canonical_client_json(content())


def test_the_canonical_text_is_compact_sorted_json_that_keeps_text_as_written():
    text = cch.canonical_client_json(content(ranking_footnote="† café — “quoted”  "))
    parsed = json.loads(text)
    assert text == json.dumps(parsed, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    assert "† café — “quoted”  " in text and ": " not in text.replace("“quoted”", "")
    assert parsed["format"] == cch.CLIENT_CONTENT_FORMAT == 1


def test_the_payload_has_exactly_the_documented_top_level_fields():
    payload = cch.build_client_payload(content())
    assert list(payload) == ["format", "client", "periods", "sections", "recommendations", "ranking_rows",
                             "historical_statements", "ranking_markers", "ranking_footnote"]
    assert payload["periods"] == {"current": {"start": "2026-09-29", "end": "2026-10-26"},
                                  "previous": {"start": "2026-09-01", "end": "2026-09-28"}}
    assert payload["ranking_rows"][1]["previous"] == {"kind": "missing", "position": None, "text": None}
    assert payload["ranking_rows"][1]["current"] == {"kind": "above_10", "position": None, "text": "Above 10"}


# --- stability ---------------------------------------------------------------------------------------------------------

def test_dictionary_insertion_order_does_not_change_the_hash():
    forward = {"a": 1, "b": 2, "c": {"x": 1, "y": 2}}
    backward = {"c": {"y": 2, "x": 1}, "b": 2, "a": 1}
    one = content(recommendations=(recommendation(evidence=forward),),
                  sections=(section(insights=(insight(evidence=forward),)),))
    two = content(recommendations=(recommendation(evidence=backward),),
                  sections=(section(insights=(insight(evidence=backward),)),))
    assert digest(one) == digest(two)


def test_sets_are_written_in_a_fixed_order():
    one = content(recommendations=(recommendation(evidence={"s": {"b", "a", "c"}}),))
    two = content(recommendations=(recommendation(evidence={"s": frozenset({"c", "a", "b"})}),))
    assert digest(one) == digest(two)
    assert json.loads(cch.canonical_client_json(one))["recommendations"][0]["evidence"]["s"] == ["a", "b", "c"]


def test_tuples_and_lists_are_equivalent_containers():
    assert digest(content(recommendations=(recommendation(evidence={"k": ("a", "b")}),))) == digest(
        content(recommendations=(recommendation(evidence={"k": ["a", "b"]}),)))


# --- list order is display order ---------------------------------------------------------------------------------------

@pytest.mark.parametrize("field,items", [
    ("sections", (section("a", title="A"), section("b", title="B"))),
    ("recommendations", (recommendation("one"), recommendation("two"))),
    ("ranking_rows", (row("k1"), row("k2"))),
])
def test_reordering_displayed_lists_changes_the_hash(field, items):
    assert digest(content(**{field: items})) != digest(content(**{field: tuple(reversed(items))}))


def test_reordering_paragraphs_insights_and_statements_changes_the_hash():
    assert digest(content(sections=(section(paragraphs=("one", "two")),))) != digest(
        content(sections=(section(paragraphs=("two", "one")),)))
    two_insights = (insight("a"), insight("b"))
    assert digest(content(sections=(section(insights=two_insights),))) != digest(
        content(sections=(section(insights=tuple(reversed(two_insights))),)))
    statements = (statement(), statement(key="history.gsc.impressions", priority=20))
    assert digest(content(), historical_statements=statements) != digest(
        content(), historical_statements=tuple(reversed(statements)))


# --- client-visible changes alter the hash -------------------------------------------------------------------------------------

@pytest.mark.parametrize("change", [
    lambda: content(client=report_run.ClientIdentity("Example Co.", "https://example.test/", "United States", "Dallas")),
    lambda: content(client=report_run.ClientIdentity("Example Co", "https://example.test/x", "United States", "Dallas")),
    lambda: content(client=report_run.ClientIdentity("Example Co", "https://example.test/", "Canada", "Dallas")),
    lambda: content(client=report_run.ClientIdentity("Example Co", "https://example.test/", "United States", "")),
    lambda: content(periods=ReportPeriods(Period(dt.date(2026, 9, 30), dt.date(2026, 10, 26)), PERIODS.previous)),
    lambda: content(periods=ReportPeriods(PERIODS.current, Period(dt.date(2026, 9, 1), dt.date(2026, 9, 27)))),
    lambda: content(ranking_footnote="note"),
])
def test_changes_to_identity_periods_and_footnote_alter_the_hash(change):
    assert digest(change()) != digest(content())


@pytest.mark.parametrize("overrides", [
    dict(keyword="roof repairs"), dict(target_location="Plano"), dict(target_location=""),
    dict(target_url="https://example.test/other"), dict(previous=rk.parse_ranking(6)),
    dict(current=rk.parse_ranking(4)), dict(current=rk.parse_ranking("Above 10")),
    dict(previous=rk.parse_ranking("")), dict(movement=3), dict(movement=None), dict(movement=0)])
def test_changes_to_ranking_values_alter_the_hash(overrides):
    changed = content(ranking_rows=(row(**overrides), content().ranking_rows[1]))
    assert digest(changed) != digest(content())


@pytest.mark.parametrize("overrides", [
    dict(title="Search Console"), dict(paragraphs=("First paragraph.",)), dict(paragraphs=("First paragraph.", "Other.")),
    dict(status="unavailable"), dict(has_strength=False), dict(key="other_key"),
    dict(insights=(insight(text="The website earned 7 organic clicks."),)),
    dict(insights=(insight(tone=Tone.POSITIVE),)), dict(insights=(insight(priority=11),)),
    dict(insights=(insight(evidence={"current": 7, "previous": None, "direction": "not_comparable"}),)),
    dict(insights=(insight(key="gsc.other"),)), dict(insights=())])
def test_changes_to_client_facing_insights_alter_the_hash(overrides):
    changed = content(sections=(section(**overrides), content().sections[1]))
    assert digest(changed) != digest(content())


@pytest.mark.parametrize("overrides", [
    dict(title="Other title"), dict(action="Other action."), dict(trigger="Selected because y."), dict(priority=21),
    dict(kind="standing"), dict(key="other"), dict(evidence={"gsc.zero_click_queries.zero_click_queries": 4}),
    dict(evidence={})])
def test_changes_to_recommendations_alter_the_hash(overrides):
    changed = content(recommendations=(recommendation(**overrides), content().recommendations[1]))
    assert digest(changed) != digest(content())


def test_adding_or_removing_items_alters_the_hash():
    base = content()
    assert digest(content(recommendations=base.recommendations[:1])) != digest(base)
    assert digest(content(ranking_rows=base.ranking_rows[:1])) != digest(base)
    assert digest(content(sections=base.sections[:1])) != digest(base)
    assert digest(content(recommendations=(*base.recommendations, recommendation("extra")))) != digest(base)


@pytest.mark.parametrize("overrides", [
    dict(text="Organic clicks were up from 5 in the report for September 1–28, 2026."), dict(tone=Tone.NEUTRAL),
    dict(priority=11), dict(key="history.gsc.other"), dict(section="geography"),
    dict(evidence={"current_value": 7, "baseline_value": 4, "direction": 1, "baseline_days": 28}),
    dict(baseline_period_start="2026-08-31"), dict(baseline_period_end="2026-09-27"), dict(group="other_group")])
def test_changes_to_client_facing_historical_statements_alter_the_hash(overrides):
    assert digest(content(), historical_statements=(statement(**overrides),)) != digest(
        content(), historical_statements=(statement(),))
    assert digest(content(), historical_statements=(statement(),)) != digest(content())


def test_statements_may_come_from_the_content_or_from_the_argument_with_the_same_hash():
    from_content = content(historical_statements=(statement(),))
    assert digest(from_content) == digest(content(), historical_statements=(statement(),))
    assert digest(content(), historical_statements=()) == digest(content())
    with pytest.raises(cch.ClientContentHashError, match="one place only"):
        digest(from_content, historical_statements=(statement(),))


def test_composed_statements_from_the_history_engine_are_accepted():
    composed = he.compose_history_insights(rule_output=hr.HistoryRuleResult(
        client_candidates=(teng.cand(),), reviewer_findings=(teng.finding(),)))
    assert re.fullmatch(r"[0-9a-f]{64}", digest(content(), historical_statements=composed.client_statements))


# --- excluded bookkeeping and reviewer-only data cannot change the hash ----------------------------------------------------------

def test_internal_bookkeeping_on_client_objects_is_not_hashed():
    base = digest(content(), historical_statements=(statement(),))
    other = content(
        sections=(section(omitted=("something.else", "and.more"), insights=(insight(tags=frozenset({"x", "y"})),)),
                  content().sections[1]),
        recommendations=(recommendation(supporting=("another.insight",)), content().recommendations[1]))
    assert digest(other, historical_statements=(statement(dimension="gsc.countries", rules_version="9"),)) == base
    renamed = content(sections=(section(insights=(dataclasses.replace(insight(), section="geography"),)),
                                content().sections[1]))
    assert digest(renamed, historical_statements=(statement(),)) == base


def test_the_field_tables_cover_every_field_of_every_hashed_type_exactly():
    types = {"ClientContent": report_run.ClientContent, "ClientIdentity": report_run.ClientIdentity,
             "ReportPeriods": ReportPeriods, "Period": Period, "SectionInsights": SectionInsights, "Insight": Insight,
             "Recommendation": Recommendation, "RankingRow": report_run.RankingRow, "Ranking": rk.Ranking,
             "ClientHistoryStatement": he.ClientHistoryStatement}
    assert set(cch.INCLUDED_FIELDS) == set(types)
    for name, cls in types.items():
        real = {f.name for f in dataclasses.fields(cls)}
        included = set(cch.INCLUDED_FIELDS[name])
        excluded = set(cch.EXCLUDED_FIELDS.get(name, ()))
        assert not included & excluded, name
        assert included | excluded == real, f"{name}: a field was added or removed; decide whether it is hashed"


def test_reviewer_only_material_is_out_of_reach_and_the_function_refuses_it():
    for wrong in (object(), {"client": {}}, None, "content", report_run.ReviewerReport):
        with pytest.raises(cch.ClientContentHashError, match="ClientContent"):
            digest(wrong)


def test_a_whole_bundle_is_refused_not_hashed(env):
    bundle = trr.assemble(env)
    with pytest.raises(cch.ClientContentHashError, match="ClientContent"):
        digest(bundle)
    with pytest.raises(cch.ClientContentHashError):
        digest(bundle.reviewer_report)
    with pytest.raises(cch.ClientContentHashError):
        digest(bundle.snapshot)


def test_reviewer_notes_diagnostics_and_identifiers_of_a_real_bundle_cannot_change_the_hash(env, tmp_path):
    trr.with_history(env)
    bundle = trr.assemble(env)
    base = digest(bundle.client_content)
    reviewer = bundle.reviewer_report
    changed = dataclasses.replace(
        reviewer,
        client_key="another-key",
        snapshot=dataclasses.replace(reviewer.snapshot, run_id="0198c1a2-7b3c-7d4e-8f5a-000000000999", revision=9,
                                     content_hash="f" * 64, quality="incomplete"),
        notes=(ReviewerNote("warning", "history", "limitation", "A different reviewer note."),),
        base_notes=(), validation_failures=(("k", "r"),), diagnostics={"ga4": {"current": {"daily_sessions_diff": 99}}},
        excluded_unfinalized=(), ranking_crosscheck=report_run.RankingCrosscheck(fallback_used=5))
    altered = dataclasses.replace(bundle, reviewer_report=changed, snapshot=dataclasses.replace(
        bundle.snapshot, content_hash="e" * 64), historical_comparison=None)
    assert digest(altered.client_content) == base == digest(bundle.client_content)
    assert altered.reviewer_report != reviewer  # the reviewer side really did change


def test_the_same_client_content_gives_the_same_hash_whatever_the_history_state(env, tmp_path):
    trr.with_history(env)
    available = trr.assemble(env)
    missing = trr.assemble(env, history_db_path=tmp_path / "none.sqlite")
    assert available.reviewer_report != missing.reviewer_report
    assert digest(available.client_content) == digest(missing.client_content)


def test_recommendation_history_annotations_cannot_change_the_hash(env):
    from seo_report.insights import history_recommendations as hrec
    bundle = trr.assemble(env)
    base = digest(bundle.client_content)
    annotations = hrec.annotate_recommendations(recommendations=bundle.base_insights.recommendations,
                                                comparison=bundle.historical_comparison)
    assert annotations.annotations is not None
    assert digest(bundle.client_content) == base


def test_timestamps_and_run_identifiers_do_not_appear_in_the_payload(env):
    trr.with_history(env)
    bundle = trr.assemble(env)
    text = cch.canonical_client_json(bundle.client_content)
    assert not re.search(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}", text)
    assert not re.search(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", text)
    assert not re.search(r"[0-9a-f]{64}", text)
    forbidden = [bundle.snapshot.content_hash, str(env.db), str(env.tmp), "run_id", "revision", "content_hash",
                 "baseline_run_id", "validation", "diagnostics", "history_support", "sender@example.test",
                 "555-010", "topsecret"]
    forbidden += [n.message for n in bundle.reviewer_report.base_notes] + [n.message for n in bundle.reviewer_report.notes]
    for item in forbidden:
        assert item not in text, item


def test_the_canonical_payload_of_a_real_bundle_holds_only_client_content(env):
    bundle = trr.assemble(env)
    payload = cch.build_client_payload(bundle.client_content)
    assert set(payload) == {"format", "client", "periods", "sections", "recommendations", "ranking_rows",
                            "historical_statements", "ranking_markers", "ranking_footnote"}
    assert set(payload["client"]) == {"name", "website", "target_country", "target_city"}
    keys = set()

    def walk(value):
        if isinstance(value, dict):
            keys.update(value)
            for item in value.values():
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    walk(payload)
    internal = {"run_id", "revision", "content_hash", "quality", "client_key", "notes", "tags", "omitted",
                "supporting", "dimension", "rules_version", "baseline_run_id", "history_support", "diagnostics"}
    assert not keys & internal, keys & internal


# --- empty, null and zero are different things -------------------------------------------------------------------------------------

def test_null_empty_zero_and_missing_are_all_distinguished():
    variants = [
        content(ranking_footnote=None), content(ranking_footnote=""), content(ranking_footnote=" "),
        content(ranking_footnote="0")]
    assert len({digest(v) for v in variants}) == 4
    movements = [digest(content(ranking_rows=(row(movement=m),))) for m in (None, 0, 0.0, -0.0, 1, 1.0)]
    assert len(set(movements)) == 6  # null, 0, 0.0, -0.0, 1 and 1.0 are all different
    evidence = [digest(content(recommendations=(recommendation(evidence=e),)))
                for e in ({}, {"a": None}, {"a": 0}, {"a": ""}, {"a": []}, {"a": False})]
    assert len(set(evidence)) == 6
    texts = [digest(content(sections=(section(paragraphs=p),))) for p in ((), ("",), (" ",), ("\n",))]
    assert len(set(texts)) == 4


def test_text_is_never_trimmed_or_normalized():
    assert digest(content(ranking_footnote="a b")) != digest(content(ranking_footnote="a  b"))
    assert digest(content(ranking_footnote="Note")) != digest(content(ranking_footnote="note"))
    assert digest(content(ranking_footnote="café")) != digest(content(ranking_footnote="café"))
    assert digest(content(ranking_footnote="x")) != digest(content(ranking_footnote="x "))


def test_a_content_with_no_sections_rows_or_recommendations_is_valid():
    empty = content(sections=(), recommendations=(), ranking_rows=())
    payload = cch.build_client_payload(empty)
    assert payload["sections"] == [] and payload["recommendations"] == [] and payload["ranking_rows"] == []
    assert re.fullmatch(r"[0-9a-f]{64}", digest(empty))


# --- rejected inputs ------------------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_non_finite_numbers_are_rejected_everywhere(value):
    for bad in (content(recommendations=(recommendation(evidence={"a": value}),)),
                content(sections=(section(insights=(insight(evidence={"a": [value]}),)),)),
                content(ranking_rows=(row(movement=value),)),
                content(recommendations=(recommendation(priority=value),))):
        with pytest.raises(cch.ClientContentHashError, match="finite"):
            digest(bad)
    with pytest.raises(cch.ClientContentHashError, match="finite"):
        digest(content(), historical_statements=(statement(evidence={"a": value}),))


@pytest.mark.parametrize("value", [object(), b"bytes", 1 + 2j, lambda: 1, Path("x"), dt.datetime(2026, 1, 1),
                                   dt.timedelta(1), type("Odd", (), {})()])
def test_unsupported_objects_are_rejected_without_echoing_them(value):
    secret_looking = type("Secret", (), {"__repr__": lambda self: "TOKEN-ya29.secret"})()
    for bad in (content(recommendations=(recommendation(evidence={"a": value}),)),
                content(sections=(section(insights=(insight(evidence={"a": {"b": value}}),)),))):
        with pytest.raises(cch.ClientContentHashError) as raised:
            digest(bad)
        assert "unsupported value of type" in str(raised.value)
    with pytest.raises(cch.ClientContentHashError) as raised:
        digest(content(recommendations=(recommendation(evidence={"a": secret_looking}),)))
    assert "ya29" not in str(raised.value) and "TOKEN" not in str(raised.value) and "Secret" in str(raised.value)


def test_non_text_dictionary_keys_are_rejected():
    for key in (1, None, ("a",), 1.5):
        with pytest.raises(cch.ClientContentHashError, match="keys must be text"):
            digest(content(recommendations=(recommendation(evidence={key: 1}),)))


def test_text_that_cannot_be_encoded_is_rejected():
    with pytest.raises(cch.ClientContentHashError, match="UTF-8"):
        digest(content(ranking_footnote="broken \ud800 surrogate"))


def test_wrong_object_types_inside_the_content_are_rejected():
    bad_cases = [
        content(client="Example Co"), content(periods=(PERIODS.current, PERIODS.previous)),
        content(sections=("text",)), content(sections=(object(),)), content(recommendations=({"key": "x"},)),
        content(ranking_rows=("row",)), content(ranking_rows=(row(previous="5"),)),
        content(ranking_footnote=5), content(ranking_footnote=["note"])]
    for bad in bad_cases:
        with pytest.raises(cch.ClientContentHashError):
            digest(bad)
    with pytest.raises(cch.ClientContentHashError):
        digest(content(), historical_statements=("a statement",))
    with pytest.raises(cch.ClientContentHashError):
        digest(content(), historical_statements=({"text": "x"},))
    with pytest.raises(cch.ClientContentHashError):
        digest(content(), historical_statements=(teng.cand(),))  # a rule candidate is not a client statement


def test_ranking_markers_are_refused_until_they_have_a_defined_shape():
    with pytest.raises(cch.ClientContentHashError, match="ranking_markers"):
        digest(content(ranking_markers=(object(),)))
    with pytest.raises(cch.ClientContentHashError, match="ranking_markers"):
        digest(content(ranking_markers=("marker",)))


def test_error_messages_hold_no_values_and_the_error_is_a_value_error(tmp_path):
    with pytest.raises(cch.ClientContentHashError) as raised:
        digest(content(recommendations=(recommendation(evidence={"a": float("nan")}),)))
    assert str(raised.value) == "recommendations[0].evidence.a: must be a finite number"
    with pytest.raises(cch.ClientContentHashError) as raised:
        digest(content(ranking_footnote=5))
    assert str(raised.value) == "ranking_footnote: must be text or null, got int"
    assert issubclass(cch.ClientContentHashError, ValueError) and str(tmp_path) not in str(raised.value)

# --- immutability and purity -----------------------------------------------------------------------------------------------------------

def test_the_input_is_not_mutated():
    c = content(recommendations=(recommendation(evidence={"b": 2, "a": {"d": 1, "c": [3, 2, 1]}}),))
    statements = (statement(),)
    before = (copy.deepcopy(c), copy.deepcopy(statements))
    cch.client_content_sha256(c, historical_statements=statements)
    cch.build_client_payload(c, historical_statements=statements)["client"]["name"] = "changed"
    assert (c, statements) == before
    assert list(c.recommendations[0].evidence) == ["b", "a"]  # the caller's insertion order is untouched


def test_the_returned_payload_shares_nothing_with_the_input():
    c = content()
    payload = cch.build_client_payload(c)
    payload["sections"][0]["paragraphs"].append("injected")
    payload["recommendations"][0]["evidence"]["injected"] = 1
    assert "injected" not in c.sections[0].paragraphs
    assert "injected" not in c.recommendations[0].evidence


def test_shuffled_construction_order_of_dictionaries_gives_one_hash():
    items = [(f"k{n}", n) for n in range(30)]
    expected = digest(content(recommendations=(recommendation(evidence=dict(items)),)))
    generator = random.Random(5)
    for _ in range(20):
        shuffled = list(items)
        generator.shuffle(shuffled)
        assert digest(content(recommendations=(recommendation(evidence=dict(shuffled)),))) == expected


def test_the_module_is_pure_and_never_uses_the_builtin_hash():
    source = (ROOT / "src" / "seo_report" / "client_content_hash.py").read_text(encoding="utf-8")
    code = re.sub(r'""".*?"""', "", source, flags=re.S)
    for pattern in (r"\.now\(", r"today\(", r"\btime\.", r"\bopen\(", r"write_text", r"write_bytes", r"\.mkdir\(",
                    r"sqlite", r"requests", r"googleapiclient", r"random", r"\bos\.", r"environ", r"uuid",
                    r"(?<![\w.])hash\(", r"__hash__", r"\bid\("):
        assert not re.search(pattern, code), pattern
