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

import test_history_rules as thr
from seo_report.insights import history_engine as he
from seo_report.insights import history_rules as hr
from seo_report.insights.rules import Insight, ReviewerNote, Tone

REASONS = ["invalid_evidence", "untraced_number", "unsafe_wording", "missing_baseline_period",
           "duplicate_of_base_insight", "inconsistent_comparison", "invalid_identity"]


def cand(section="search_console", key="history.gsc.clicks", priority=10, text=None, **overrides):
    values = dict(
        section=section, key=key, dimension="gsc.totals", text=text or f"Statement for {key}.", tone=Tone.NEUTRAL,
        priority=priority, evidence={"current_value": 6, "baseline_value": 4, "direction": 1, "baseline_days": 28},
        baseline_period_start="2026-09-01", baseline_period_end="2026-09-28", base_keys=("gsc.clicks",))
    values.update(overrides)
    return hr.ClientHistoryCandidate(**values)


def finding(key="history.review.a", priority=100, **overrides):
    values = dict(section="history", key=key, kind="baselines_differ", dimension=None, message=f"Message for {key}.",
                  priority=priority, evidence={"baseline_runs": 2}, blocked_reason=None,
                  baseline_period_start="2026-09-01", baseline_period_end="2026-09-28", baseline_run_id="run-1")
    values.update(overrides)
    return hr.ReviewerHistoryFinding(**values)


def result(candidates=(), findings=(), notes=(), failures=()):
    return hr.HistoryRuleResult(tuple(candidates), tuple(findings), tuple(notes), tuple(failures))


def compose(rule_output, **kwargs):
    return he.compose_history_insights(rule_output=rule_output, **kwargs)


def keys(composed):
    return [s.key for s in composed.client_statements]


# --- structure ------------------------------------------------------------------------------------------------------

def test_the_result_has_separate_client_and_reviewer_sides():
    composed = compose(result([cand()], [finding()], [ReviewerNote("info", "history", "suppressed", "n")],
                              [hr.HistoryValidationFailure("history.x", "unsafe_wording")]))
    assert [f.name for f in dataclasses.fields(he.HistoryInsights)] == ["client", "reviewer", "rules_version"]
    assert isinstance(composed.client, he.ClientHistory) and isinstance(composed.reviewer, he.ReviewerHistory)
    assert [f.name for f in dataclasses.fields(he.ClientHistory)] == ["statements"]
    assert [f.name for f in dataclasses.fields(he.ReviewerHistory)] == [
        "findings", "notes", "validation_failures", "statement_audit"]
    assert composed.client_statements is composed.client.statements
    assert composed.reviewer_findings is composed.reviewer.findings
    assert composed.notes is composed.reviewer.notes and composed.validation_failures is composed.reviewer.validation_failures
    assert composed.rules_version == "1"


def test_the_client_statement_type_has_no_reviewer_fields():
    names = {f.name for f in dataclasses.fields(he.ClientHistoryStatement)}
    assert names == {"section", "key", "dimension", "text", "tone", "priority", "evidence", "baseline_period_start",
                     "baseline_period_end", "rules_version", "group"}
    assert not names & {"base_keys", "audience", "blocked_reason", "baseline_run_id", "kind", "message", "notes",
                        "run_id", "diagnostics"}


def test_the_results_are_frozen():
    composed = compose(result([cand()], [finding()]))
    for target, name in ((composed, "client"), (composed.client, "statements"), (composed.reviewer, "findings"),
                         (composed.client_statements[0], "text"), (composed.reviewer_findings[0], "message")):
        with pytest.raises(dataclasses.FrozenInstanceError):
            setattr(target, name, None)
    assert isinstance(composed.client_statements, tuple) and isinstance(composed.reviewer_findings, tuple)


# --- client composition ----------------------------------------------------------------------------------------------

def test_one_candidate_is_retained():
    composed = compose(result([cand()]))
    assert keys(composed) == ["history.gsc.clicks"]
    assert [a.selected for a in composed.reviewer.statement_audit] == [True]


def test_two_candidates_in_a_section_are_retained():
    composed = compose(result([cand(key="history.gsc.impressions", priority=20), cand()]))
    assert keys(composed) == ["history.gsc.clicks", "history.gsc.impressions"]


def test_three_or_more_candidates_are_limited_to_two():
    many = [cand(key=f"history.gsc.k{n}", priority=10 * (n + 1)) for n in range(5)]
    composed = compose(result(many))
    assert keys(composed) == ["history.gsc.k0", "history.gsc.k1"]
    omitted = [(a.key, a.omitted_reason) for a in composed.reviewer.statement_audit if not a.selected]
    assert omitted == [("history.gsc.k2", "section_limit"), ("history.gsc.k3", "section_limit"),
                       ("history.gsc.k4", "section_limit")]
    assert he.MAX_STATEMENTS_PER_SECTION == 2 == hr.MAX_STATEMENTS_PER_SECTION


def test_lower_numeric_priority_wins():
    low, mid, high = cand(key="history.gsc.z", priority=5), cand(key="history.gsc.b", priority=30), \
        cand(key="history.gsc.a", priority=90)
    composed = compose(result([high, mid, low]))
    assert keys(composed) == ["history.gsc.z", "history.gsc.b"]  # 5 and 30 beat 90, although "a" sorts first


def test_equal_priorities_use_stable_alphabetical_key_order():
    three = [cand(key="history.gsc.c"), cand(key="history.gsc.a"), cand(key="history.gsc.b")]
    composed = compose(result(three))
    assert keys(composed) == ["history.gsc.a", "history.gsc.b"]
    assert [a.key for a in composed.reviewer.statement_audit] == ["history.gsc.a", "history.gsc.b", "history.gsc.c"]


def test_section_order_is_search_console_geography_ga4():
    mixed = [cand("ga4", "history.ga4.sessions", 10), cand("geography", "history.geo.target_share", 10),
             cand("search_console", "history.gsc.clicks", 10)]
    assert keys(compose(result(mixed))) == ["history.gsc.clicks", "history.geo.target_share", "history.ga4.sessions"]
    assert he._SECTION_ORDER == {"keywords": 0, "search_console": 1, "geography": 2, "ga4": 3}


def test_priority_only_matters_within_a_section():
    mixed = [cand("ga4", "history.ga4.sessions", 1), cand("search_console", "history.gsc.clicks", 99)]
    assert keys(compose(result(mixed))) == ["history.gsc.clicks", "history.ga4.sessions"]


def test_each_section_gets_its_own_two_statement_limit():
    candidates = [cand(section, f"history.{tag}.{n}", n)
                  for section, tag in (("search_console", "gsc"), ("geography", "geo"), ("ga4", "ga4"))
                  for n in (1, 2, 3)]
    composed = compose(result(candidates))
    assert len(composed.client_statements) == 6
    assert [s.section for s in composed.client_statements] == ["search_console"] * 2 + ["geography"] * 2 + ["ga4"] * 2
    assert [s.priority for s in composed.client_statements] == [1, 2, 1, 2, 1, 2]
    assert sum(1 for a in composed.reviewer.statement_audit if a.omitted_reason == "section_limit") == 3


def test_the_candidate_group_is_preserved():
    composed = compose(result([cand(), cand(key="history.gsc.impressions", group="since_last_report")]))
    assert {s.group for s in composed.client_statements} == {"since_last_report"}
    other = compose(result([cand(group="custom_group")]))
    assert other.client_statements[0].group == "custom_group"


def test_exact_text_and_all_metadata_are_preserved():
    original = cand(text="  Odd  spacing — kept “exactly”, 1,234 times.  ", tone=Tone.POSITIVE, priority=7,
                    dimension="gsc.countries", rules_version="1")
    (statement,) = compose(result([original])).client_statements
    assert statement.text == original.text
    assert (statement.section, statement.key, statement.dimension, statement.tone, statement.priority) == (
        original.section, original.key, original.dimension, original.tone, original.priority)
    assert statement.evidence == original.evidence
    assert (statement.baseline_period_start, statement.baseline_period_end) == ("2026-09-01", "2026-09-28")
    assert (statement.rules_version, statement.group) == ("1", "since_last_report")


def test_evidence_is_copied_not_shared_and_values_are_not_changed():
    original = cand(evidence={"share_current": 0.1 + 0.2, "current_value": 10**12, "direction": -1})
    (statement,) = compose(result([original])).client_statements
    assert statement.evidence == original.evidence and statement.evidence is not original.evidence
    assert statement.evidence["share_current"] == 0.30000000000000004
    statement.evidence["current_value"] = 0
    assert original.evidence["current_value"] == 10**12


def test_base_keys_stay_available_for_audit_only():
    composed = compose(result([cand(base_keys=("geo.target_market",), key="history.geo.x", section="geography")]))
    (audit,) = composed.reviewer.statement_audit
    assert audit.base_keys == ("geo.target_market",) and audit.selected and audit.omitted_reason is None
    assert "geo.target_market" not in repr(composed.client)


def test_input_order_does_not_change_the_output():
    candidates = [cand(section, f"history.{tag}.{n}", n % 2)
                  for section, tag in (("search_console", "gsc"), ("geography", "geo"), ("ga4", "ga4"))
                  for n in range(4)]
    expected = compose(result(candidates))
    generator = random.Random(7)
    for _ in range(25):
        shuffled = list(candidates)
        generator.shuffle(shuffled)
        assert compose(result(shuffled)) == expected


def test_ties_are_broken_the_same_way_for_any_input_order():
    same_key = [cand(text="B text"), cand(text="A text"), cand(text="C text")]
    first = compose(result(same_key))
    for ordering in (same_key, same_key[::-1], [same_key[1], same_key[2], same_key[0]]):
        assert compose(result(ordering)) == first
    assert first.client_statements[0].text == "A text"


# --- reviewer composition ----------------------------------------------------------------------------------------------

def test_all_reviewer_findings_are_retained_with_their_metadata():
    findings = [
        finding("history.review.gsc_clicks", 110, section="search_console", kind="gsc_historical_change",
                dimension="gsc.totals", blocked_reason="live_previous_not_empty", baseline_run_id="run-9",
                evidence={"current_value": 6, "baseline_value": 4}),
        finding("history.review.ranking_new_keywords", 220, section="keywords", kind="ranking_new_keywords",
                evidence={"new_keywords": 3}, baseline_run_id=None, baseline_period_start=None,
                baseline_period_end=None)]
    composed = compose(result(findings=findings))
    assert composed.reviewer_findings == tuple(findings)
    first = composed.reviewer_findings[0]
    assert (first.kind, first.blocked_reason, first.baseline_run_id) == ("gsc_historical_change",
                                                                         "live_previous_not_empty", "run-9")
    assert (first.baseline_period_start, first.baseline_period_end) == ("2026-09-01", "2026-09-28")
    assert first.evidence == {"current_value": 6, "baseline_value": 4} and first.message == findings[0].message


def test_reviewer_findings_stay_separate_from_client_statements():
    composed = compose(result([cand()], [finding(), finding("history.review.b", 50)]))
    assert len(composed.client_statements) == 1 and len(composed.reviewer_findings) == 2
    assert not any(isinstance(item, hr.ReviewerHistoryFinding) for item in composed.client_statements)
    assert not any(isinstance(item, he.ClientHistoryStatement) for item in composed.reviewer_findings)
    client_dump = repr(composed.client)
    for forbidden in ("history.review", "run-1", "Message for", "blocked_reason", "baseline_run_id"):
        assert forbidden not in client_dump


def test_reviewer_findings_are_not_turned_into_client_statements():
    composed = compose(result(findings=[finding(), finding("history.review.b", 5, section="geography")]))
    assert composed.client_statements == () and len(composed.reviewer_findings) == 2


def test_reviewer_findings_are_ordered_by_priority_then_key():
    shuffled = [finding("history.review.c", 20), finding("history.review.a", 20), finding("history.review.z", 5),
                finding("history.review.b", 300)]
    composed = compose(result(findings=shuffled))
    assert [f.key for f in composed.reviewer_findings] == ["history.review.z", "history.review.a",
                                                           "history.review.c", "history.review.b"]
    assert compose(result(findings=shuffled[::-1])) == composed


def test_reviewer_findings_are_not_discarded_by_the_client_limit():
    candidates = [cand(key=f"history.gsc.k{n}", priority=n) for n in range(5)]
    findings = [finding(f"history.review.f{n}", n) for n in range(5)]
    composed = compose(result(candidates, findings))
    assert len(composed.client_statements) == 2 and len(composed.reviewer_findings) == 5


def test_findings_evidence_is_copied_and_exact():
    original = finding(evidence={"baseline_runs": 2, "daily_sessions_diff": -3})
    composed = compose(result(findings=[original]))
    kept = composed.reviewer_findings[0]
    assert kept == original and kept.evidence is not original.evidence
    kept.evidence["baseline_runs"] = 99
    assert original.evidence["baseline_runs"] == 2


def test_exact_repeats_of_a_finding_are_collapsed_but_different_ones_are_kept():
    one = finding()
    composed = compose(result(findings=[one, copy.deepcopy(one), finding(message="Different message.")]))
    assert len(composed.reviewer_findings) == 2


# --- notes and validation failures ----------------------------------------------------------------------------------------

def test_reviewer_notes_are_preserved():
    notes = [ReviewerNote("warning", "history", "suppressed", "Second."), ReviewerNote("info", "history", "limitation", "First.")]
    composed = compose(result(notes=notes))
    assert sorted(composed.notes, key=lambda n: n.message) == sorted(notes, key=lambda n: n.message)
    assert [n.severity for n in composed.notes] == ["info", "warning"]  # canonical order
    assert compose(result(notes=notes[::-1])).notes == composed.notes


def test_all_seven_validation_reason_codes_are_preserved():
    failures = [hr.HistoryValidationFailure(f"history.k{n}", reason) for n, reason in enumerate(REASONS)]
    composed = compose(result(failures=failures[::-1]))
    assert [f.reason for f in composed.validation_failures] == REASONS
    assert {f.reason for f in composed.validation_failures} == set(hr.VALIDATION_REASONS)
    assert all(f.reason in hr.VALIDATION_REASONS for f in composed.validation_failures)


def test_the_same_statement_may_keep_several_failure_reasons():
    failures = [hr.HistoryValidationFailure("history.gsc.clicks", "unsafe_wording"),
                hr.HistoryValidationFailure("history.gsc.clicks", "invalid_evidence")]
    composed = compose(result(failures=failures))
    assert [(f.key, f.reason) for f in composed.validation_failures] == [
        ("history.gsc.clicks", "invalid_evidence"), ("history.gsc.clicks", "unsafe_wording")]


def test_two_identical_reviewer_notes_remain_two_entries():
    note = ReviewerNote("info", "history", "suppressed", "Same note.")
    composed = compose(result(notes=[note, ReviewerNote("info", "history", "suppressed", "Same note.")]))
    assert composed.notes == (note, note) and len(composed.notes) == 2
    three = compose(result(notes=[note, note, note]))
    assert len(three.notes) == 3


def test_two_identical_validation_failures_remain_two_entries():
    failure = hr.HistoryValidationFailure("history.gsc.clicks", "unsafe_wording")
    composed = compose(result(failures=[failure, hr.HistoryValidationFailure("history.gsc.clicks", "unsafe_wording")]))
    assert composed.validation_failures == (failure, failure) and len(composed.validation_failures) == 2
    assert [f.reason for f in composed.validation_failures] == ["unsafe_wording"] * 2


def test_distinct_notes_and_failures_are_all_preserved_alongside_repeats():
    notes = [ReviewerNote("warning", "history", "suppressed", "B."), ReviewerNote("info", "history", "limitation", "A."),
             ReviewerNote("info", "history", "limitation", "A."), ReviewerNote("info", "ga4", "diagnostic", "C.")]
    failures = [hr.HistoryValidationFailure(f"history.k{n % 3}", REASONS[n % 7]) for n in range(10)]
    failures += [failures[0], failures[0]]
    composed = compose(result(notes=notes, failures=failures))
    assert len(composed.notes) == 4 and len(composed.validation_failures) == 12
    assert sorted(composed.notes, key=repr) == sorted(notes, key=repr)
    assert sorted(composed.validation_failures, key=repr) == sorted(failures, key=repr)
    assert all(f.reason in hr.VALIDATION_REASONS for f in composed.validation_failures)


def test_notes_and_failures_are_ordered_deterministically_whatever_the_input_order():
    notes = [ReviewerNote("warning", "history", "suppressed", "B."), ReviewerNote("info", "history", "limitation", "A."),
             ReviewerNote("info", "history", "limitation", "A."), ReviewerNote("info", "ga4", "diagnostic", "C.")]
    failures = [hr.HistoryValidationFailure(f"history.k{n % 3}", REASONS[n % 7]) for n in range(10)]
    failures += [failures[0], failures[0]]
    expected = compose(result(notes=notes, failures=failures))
    generator = random.Random(11)
    for _ in range(25):
        shuffled_notes, shuffled_failures = list(notes), list(failures)
        generator.shuffle(shuffled_notes)
        generator.shuffle(shuffled_failures)
        again = compose(result(notes=shuffled_notes, failures=shuffled_failures))
        assert again.notes == expected.notes and again.validation_failures == expected.validation_failures
    keys_in_order = [(f.key, f.reason) for f in expected.validation_failures]
    assert keys_in_order == sorted(keys_in_order)
    assert [(n.severity, n.source, n.category, n.message) for n in expected.notes] == sorted(
        (n.severity, n.source, n.category, n.message) for n in notes)


def test_finding_deduplication_is_unchanged_while_notes_and_failures_are_not_deduplicated():
    one = finding()
    note = ReviewerNote("info", "history", "suppressed", "n")
    failure = hr.HistoryValidationFailure("history.x", "invalid_evidence")
    composed = compose(result(findings=[one, copy.deepcopy(one), finding(message="Other.")],
                              notes=[note, note], failures=[failure, failure]))
    assert len(composed.reviewer_findings) == 2 and len(composed.notes) == 2 and len(composed.validation_failures) == 2


def test_client_statements_and_the_audit_are_unchanged_by_repeated_notes_and_failures():
    candidates = [cand(key=f"history.gsc.k{n}", priority=n) for n in range(4)] + [cand(key="history.gsc.k0")]
    plain = compose(result(candidates))
    note = ReviewerNote("info", "history", "suppressed", "n")
    other_failure = hr.HistoryValidationFailure("history.unrelated", "invalid_identity")
    noisy = compose(result(candidates, notes=[note, note], failures=[other_failure, other_failure]))
    assert noisy.client == plain.client
    assert noisy.reviewer.statement_audit == plain.reviewer.statement_audit
    assert [(a.key, a.omitted_reason) for a in noisy.reviewer.statement_audit] == [
        ("history.gsc.k0", None), ("history.gsc.k1", None), ("history.gsc.k2", "section_limit"),
        ("history.gsc.k3", "section_limit"), ("history.gsc.k0", "duplicate_key")]  # the repeat has priority 10
    assert keys(noisy) == ["history.gsc.k0", "history.gsc.k1"]

def test_validation_failures_are_never_client_content():
    composed = compose(result([cand()], failures=[hr.HistoryValidationFailure("history.other", "invalid_identity")]))
    assert "invalid_identity" not in repr(composed.client) and "history.other" not in repr(composed.client)


def test_a_blocked_candidate_is_not_brought_back():
    blocked = cand(key="history.gsc.clicks")
    composed = compose(result([blocked, cand(key="history.gsc.impressions", priority=20)],
                              failures=[hr.HistoryValidationFailure("history.gsc.clicks", "unsafe_wording")]))
    assert keys(composed) == ["history.gsc.impressions"]
    assert [(a.key, a.omitted_reason) for a in composed.reviewer.statement_audit if not a.selected] == [
        ("history.gsc.clicks", "validation_failed")]
    assert [f.reason for f in composed.validation_failures] == ["unsafe_wording"]


def test_a_blocked_candidate_does_not_use_up_a_section_slot():
    candidates = [cand(key="history.gsc.a", priority=1), cand(key="history.gsc.b", priority=2),
                  cand(key="history.gsc.c", priority=3)]
    composed = compose(result(candidates, failures=[hr.HistoryValidationFailure("history.gsc.a", "untraced_number")]))
    assert keys(composed) == ["history.gsc.b", "history.gsc.c"]


# --- duplicates and base insights ------------------------------------------------------------------------------------------

def test_a_repeated_candidate_is_used_once():
    one = cand()
    composed = compose(result([one, copy.deepcopy(one), copy.deepcopy(one)]))
    assert keys(composed) == ["history.gsc.clicks"]
    assert [a.omitted_reason for a in composed.reviewer.statement_audit] == [None, "duplicate_key", "duplicate_key"]


def test_a_repeated_key_with_different_content_is_resolved_deterministically():
    a, b = cand(text="A text", priority=10), cand(text="B text", priority=5)
    first = compose(result([a, b]))
    assert compose(result([b, a])) == first
    assert first.client_statements[0].text == "B text" and first.client_statements[0].priority == 5
    assert [x.omitted_reason for x in first.reviewer.statement_audit] == [None, "duplicate_key"]
    assert first.validation_failures == ()  # no new failure codes are invented


def test_a_repeated_key_does_not_take_a_second_section_slot():
    composed = compose(result([cand(), cand(text="other"), cand(key="history.gsc.impressions", priority=20)]))
    assert keys(composed) == ["history.gsc.clicks", "history.gsc.impressions"]


def test_a_history_key_never_reuses_a_base_insight_key():
    base = [Insight("search_console", "history.gsc.clicks", "A base text.", Tone.NEUTRAL, 10, {})]
    composed = compose(result([cand(), cand(key="history.gsc.impressions", priority=20)]), base_insights=base)
    assert keys(composed) == ["history.gsc.impressions"]
    assert composed.reviewer.statement_audit[0].omitted_reason == "base_key_collision"


def test_base_insights_are_only_read():
    base = [Insight("search_console", "gsc.clicks", "The website earned 6 organic clicks.", Tone.NEUTRAL, 10,
                    {"current": 6})]
    before = copy.deepcopy(base)
    composed = compose(result([cand()]), base_insights=base)
    assert base == before and keys(composed) == ["history.gsc.clicks"]
    assert compose(result([cand()]), base_insights=None) == compose(result([cand()]))


def test_omission_reasons_are_a_closed_set():
    assert he.OMISSION_REASONS == ("section_limit", "duplicate_key", "validation_failed", "base_key_collision")
    candidates = [cand(key=f"history.gsc.k{n}", priority=n) for n in range(4)] + [cand(key="history.gsc.k0")]
    composed = compose(result(candidates, failures=[hr.HistoryValidationFailure("history.gsc.k1", "invalid_evidence")]))
    assert {a.omitted_reason for a in composed.reviewer.statement_audit} <= {None, *he.OMISSION_REASONS}


# --- empty and unusual inputs ------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("rule_output", [None, hr.HistoryRuleResult(), result()])
def test_an_empty_rule_result_gives_an_empty_composition(rule_output):
    composed = compose(rule_output)
    assert composed == he.HistoryInsights()
    assert composed.client_statements == () and composed.reviewer_findings == () and composed.notes == ()
    assert composed.validation_failures == () and composed.reviewer.statement_audit == ()


def test_no_statements_are_invented_when_only_reviewer_material_exists():
    composed = compose(result(findings=[finding()], notes=[ReviewerNote("info", "history", "suppressed", "n")],
                              failures=[hr.HistoryValidationFailure("history.x", "invalid_evidence")]))
    assert composed.client_statements == () and composed.client == he.ClientHistory()


def test_only_candidates_and_nothing_else():
    composed = compose(result([cand()]))
    assert composed.reviewer_findings == () and composed.notes == () and composed.validation_failures == ()


def test_wrong_input_types_are_programming_errors():
    with pytest.raises(TypeError):
        compose({"client_candidates": []})
    with pytest.raises(TypeError):
        compose(result(candidates=["not a candidate"]))
    with pytest.raises(TypeError):
        compose(result(findings=[cand()]))
    with pytest.raises(TypeError):
        compose(result(notes=["note"]))
    with pytest.raises(TypeError):
        compose(result(failures=[("k", "r")]))
    with pytest.raises(ValueError, match="unknown section"):
        compose(result([cand(section="mystery")]))


# --- determinism, purity and real rule output ----------------------------------------------------------------------------------

def test_repeated_composition_is_equivalent_and_the_inputs_are_unchanged():
    rule_output = result([cand(), cand(key="history.gsc.b", priority=5), cand("ga4", "history.ga4.s", 3)],
                         [finding(), finding("history.review.b", 2)], [ReviewerNote("info", "history", "suppressed", "n")],
                         [hr.HistoryValidationFailure("history.x", "invalid_identity")])
    before = copy.deepcopy(rule_output)
    first, second = compose(rule_output), compose(rule_output)
    assert first == second and rule_output == before


def test_no_timestamps_and_no_io():
    source = (ROOT / "src" / "seo_report" / "insights" / "history_engine.py").read_text(encoding="utf-8")
    code = re.sub(r'""".*?"""', "", source, flags=re.S)
    for pattern in (r"datetime", r"\.now\(", r"today\(", r"\btime\.", r"\bopen\(", r"write_text", r"write_bytes",
                    r"\.mkdir\(", r"sqlite", r"requests", r"googleapiclient", r"random", r"\bos\.", r"environ"):
        assert not re.search(pattern, code), pattern
    dump = repr(compose(result([cand()], [finding()])))
    assert not re.search(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}", dump)


def test_real_rule_output_is_composed_without_changing_it(tmp_path):
    scenario = thr.scenario(tmp_path, baseline=dict(thr.BASE_DEFAULT), current=dict(thr.CURRENT_DEFAULT, target=1479))
    rules = thr.evaluate(scenario)
    composed = compose(rules, base_insights=scenario.insights)
    assert keys(composed) == [c.key for c in rules.client_candidates]
    assert [s.text for s in composed.client_statements] == [c.text for c in rules.client_candidates]
    assert [s.evidence for s in composed.client_statements] == [c.evidence for c in rules.client_candidates]
    assert composed.reviewer_findings == tuple(sorted(rules.reviewer_findings, key=lambda f: (f.priority, f.key)))
    assert composed.notes == tuple(rules.notes) and composed.validation_failures == tuple(rules.validation_failures)
    assert all(a.selected for a in composed.reviewer.statement_audit)
    assert len(composed.client_statements) == 4


def test_real_rule_output_with_three_candidates_in_a_section_is_limited(tmp_path):
    scenario = thr.scenario(tmp_path, baseline=dict(thr.BASE_DEFAULT, impressions=1000, target=700),
                            current=dict(thr.CURRENT_DEFAULT, impressions=1000, target=800))
    rules = thr.evaluate(scenario)
    extra = dataclasses.replace(rules.client_candidates[0], key="history.gsc.extra", priority=99)
    padded = dataclasses.replace(rules, client_candidates=(*rules.client_candidates, extra))
    composed = compose(padded, base_insights=scenario.insights)
    per_section = {}
    for statement in composed.client_statements:
        per_section[statement.section] = per_section.get(statement.section, 0) + 1
    assert max(per_section.values()) == 2 and "history.gsc.extra" not in keys(composed)
    assert any(a.key == "history.gsc.extra" and a.omitted_reason == "section_limit"
               for a in composed.reviewer.statement_audit)


def test_a_candidate_blocked_by_the_base_duplicate_guard_stays_out(tmp_path):
    scenario = thr.scenario(tmp_path)
    stated = Insight("search_console", "gsc.clicks", "Clicks climbed from four to six", Tone.POSITIVE, 10,
                     {"current": 6, "previous": 4})
    rules = thr.evaluate(scenario, base_insights=[stated])
    assert "history.gsc.clicks" not in [c.key for c in rules.client_candidates]
    composed = compose(rules, base_insights=[stated])
    assert "history.gsc.clicks" not in keys(composed)
    assert ("history.gsc.clicks", "duplicate_of_base_insight") in [
        (f.key, f.reason) for f in composed.validation_failures]


def test_client_statements_hold_no_reviewer_or_internal_material(tmp_path):
    scenario = thr.scenario(tmp_path, current=dict(thr.CURRENT_DEFAULT, diagnostics={"daily_sessions_diff": 3}))
    rules = thr.evaluate(scenario, crosscheck=None)
    composed = compose(rules, base_insights=scenario.insights)
    run_ids = {ref.run_id for ref in (s.baseline for s in scenario.comparison.status.values()) if ref}
    dump = repr(composed.client)
    assert run_ids and not any(run_id in dump for run_id in run_ids)
    assert not re.search(r"[0-9a-f]{64}", dump) and str(tmp_path) not in dump
    for forbidden in ("blocked", "reviewer", "daily_sessions_diff", "HistoricalComparison", "Dimension", "excluded"):
        assert forbidden not in dump
    assert not any(type(item).__name__ in ("HistoricalComparison", "DimensionStatus", "BaselineRef")
                   for statement in composed.client_statements for item in vars(statement).values())
