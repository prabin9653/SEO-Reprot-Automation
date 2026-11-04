import dataclasses
import datetime as dt
import hashlib
import json
import re
import sys
from decimal import Decimal
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from seo_report import render_input as ri
from seo_report import render_input_hash as rh
from test_render_input import A, B, C, D, fingerprints, sample


def digest(value):
    return rh.render_input_sha256(value)


def replaced(value, **changes):
    return dataclasses.replace(value, **changes)


# --- the hash itself -------------------------------------------------------------------------------------------------------------

def test_the_hash_is_a_stable_64_character_lowercase_hex_digest():
    value = digest(sample())
    assert re.fullmatch(r"[0-9a-f]{64}", value) and value == digest(sample())
    assert value == hashlib.sha256(rh.canonical_render_input_json(sample()).encode("utf-8")).hexdigest()


def test_the_hash_has_a_pinned_value_so_canonicalization_changes_are_deliberate():
    # If this fails, the canonical form changed: bump FORMAT_VERSION and update the value on purpose.
    assert digest(sample()) == "529010074f815c6e586d33a85ef5afe8cc8303e11932309cdb4a462356ce8ef9"


def test_the_payload_carries_a_format_identifier_and_version():
    payload = json.loads(rh.canonical_render_input_json(sample()))
    assert (payload["format"], payload["version"]) == ("jasper.render-input", 2) == (rh.FORMAT_NAME, rh.FORMAT_VERSION)
    assert set(payload) == {"format", "version", "input"}


def test_a_different_format_version_would_change_every_hash(monkeypatch):
    before = digest(sample())
    monkeypatch.setattr(rh, "FORMAT_VERSION", 3)
    assert digest(sample()) != before


def test_canonical_json_is_compact_sorted_and_not_ascii_escaped():
    value = sample(client=ri.RenderClient("Crème — 日本", "https://x.example"))
    text = rh.canonical_render_input_json(value)
    assert " " not in text.split('"client"')[0] and "\\u" not in text and "Crème — 日本" in text
    assert text == json.dumps(json.loads(text), sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def test_operational_metadata_is_not_in_the_payload():
    text = rh.canonical_render_input_json(sample())
    assert "operational" not in text and "0198c1a2" not in text and D not in text and "sample-co" not in text
    assert "snapshot_content_hash" not in text and "run_id" not in text


def test_no_absolute_paths_or_secret_like_text_enter_the_payload():
    text = rh.canonical_render_input_json(sample())
    assert "\\\\" not in text and "C:" not in text and "token" not in text.lower() and "password" not in text.lower()


# --- inputs that must change the hash ------------------------------------------------------------------------------------------------------

def changed_inputs():
    base = sample()
    yield "client name", replaced(base, client=ri.RenderClient("Another Co", base.client.website))
    yield "client website", replaced(base, client=ri.RenderClient(base.client.name, "https://other.example"))
    yield "period", replaced(base, period=ri.RenderPeriod(dt.date(2026, 9, 30), base.period.end))
    yield "comparison period", replaced(base, comparison_period=ri.RenderPeriod(dt.date(2026, 9, 2), dt.date(2026, 9, 28)))
    yield "keyword text", replaced(base, keywords=(replaced(base.keywords[0], keyword="roof repairs"),) + base.keywords[1:])
    yield "keyword location", replaced(base, keywords=(replaced(base.keywords[0], location="Elsewhere"),) + base.keywords[1:])
    yield "keyword current", replaced(base, keywords=(replaced(base.keywords[0], current=ri.RenderRanking("position", 4)),) + base.keywords[1:])
    yield "keyword movement", replaced(base, keywords=(replaced(base.keywords[0], movement=3),) + base.keywords[1:])
    yield "ranking footnote", replaced(base, ranking_footnote="Another footnote.")
    yield "section paragraph", replaced(base, sections=(replaced(base.sections[0], paragraphs=("First paragraph.", "Edited.")),) + base.sections[1:])
    yield "section title", replaced(base, sections=(replaced(base.sections[0], title="Keywords"),) + base.sections[1:])
    yield "recommendation title", replaced(base, recommendations=(replaced(base.recommendations[0], title="Improve headings"),) + base.recommendations[1:])
    yield "recommendation action", replaced(base, recommendations=(replaced(base.recommendations[0], action="Rewrite everything."),) + base.recommendations[1:])
    yield "sender name", replaced(base, sender=replaced(base.sender, name="Someone Else"))
    yield "sender phone", replaced(base, sender=replaced(base.sender, phone="(111) 111-1111"))
    yield "sender email", replaced(base, sender=replaced(base.sender, email="other@example.test"))
    yield "sender address", replaced(base, sender=replaced(base.sender, address_lines=("2 Other Way",)))
    yield "generation date", replaced(base, generation_date=dt.date(2026, 10, 31))
    yield "historical statement text", replaced(base, historical_statements=(replaced(base.historical_statements[0], text="Changed."),) + base.historical_statements[1:])
    yield "historical statement removed", replaced(base, historical_statements=base.historical_statements[:1])
    yield "search console clicks", replaced(base, search_console=replaced(base.search_console, clicks=121))
    yield "search console daily point", replaced(base, search_console=replaced(base.search_console, daily=(ri.DailyPoint(dt.date(2026, 10, 1), 5, 150),) + base.search_console.daily[1:]))
    yield "search console unavailable", replaced(base, search_console=ri.SearchConsoleVisuals.unavailable())
    yield "geography country", replaced(base, geography=replaced(base.geography, countries=(ri.CountryRow("United States", 101, 3000), base.geography.countries[1])))
    yield "search console incomplete", replaced(base, search_console=replaced(base.search_console, status="incomplete"))
    yield "geography incomplete", replaced(base, geography=replaced(base.geography, status="incomplete"))
    yield "ga4 incomplete", replaced(base, ga4=replaced(base.ga4, status="incomplete"))
    yield "search console missing position", replaced(base, search_console=replaced(base.search_console, average_position=None))
    yield "search console missing ctr", replaced(base, search_console=replaced(base.search_console, ctr=None))
    yield "search console missing clicks", replaced(base, search_console=replaced(base.search_console, clicks=None))
    yield "ga4 missing users", replaced(base, ga4=replaced(base.ga4, users=None))
    yield "ga4 missing engagement rate", replaced(base, ga4=replaced(base.ga4, engagement_rate=None))
    yield "geography truncation removed", replaced(base, geography=replaced(base.geography, truncation=()))
    yield "geography truncation reason", replaced(base, geography=replaced(base.geography, truncation=(ri.TruncationNote("countries", "snapshot_cap"), base.geography.truncation[1])))
    yield "geography omitted rows", replaced(base, geography=replaced(base.geography, truncation=(ri.TruncationNote("countries", "row_limit", 8), base.geography.truncation[1])))
    yield "ga4 truncation removed", replaced(base, ga4=replaced(base.ga4, truncation=()))
    yield "ga4 truncated dataset", replaced(base, ga4=replaced(base.ga4, truncation=(ri.TruncationNote("channels", "row_limit", 3), ri.TruncationNote("channels", "source_limit"))))
    yield "missing day", replaced(base, search_console=replaced(base.search_console, daily=base.search_console.daily[:1]))
    yield "zero instead of a missing day", replaced(base, search_console=replaced(base.search_console, daily=base.search_console.daily + (ri.DailyPoint(dt.date(2026, 10, 3), 0, 0),)))
    yield "geography unavailable", replaced(base, geography=ri.GeographyVisuals.unavailable())
    yield "ga4 sessions", replaced(base, ga4=replaced(base.ga4, sessions=301))
    yield "ga4 channel", replaced(base, ga4=replaced(base.ga4, channels=(ri.ChannelRow("Organic Search", 201, 150, 120), base.ga4.channels[1])))
    yield "ga4 unavailable", replaced(base, ga4=ri.Ga4Visuals.unavailable())
    yield "template version", replaced(base, fingerprints=fingerprints(template_version="r1.1"))
    yield "logo fingerprint", replaced(base, fingerprints=fingerprints(logo_sha256=B))
    yield "font fingerprint", replaced(base, fingerprints=fingerprints(fonts=(ri.FontFingerprint("DejaVuSans-Bold", C), ri.FontFingerprint("DejaVuSans", D))))
    yield "font added", replaced(base, fingerprints=fingerprints(fonts=base.fingerprints.fonts + (ri.FontFingerprint("Extra", A),)))


@pytest.mark.parametrize("label,value", list(changed_inputs()), ids=[label for label, _ in changed_inputs()])
def test_changing_a_client_visible_or_rendering_value_changes_the_hash(label, value):
    assert digest(value) != digest(sample()), label


def test_all_changed_inputs_give_distinct_hashes():
    hashes = [digest(value) for _, value in changed_inputs()]
    assert len(set(hashes)) == len(hashes) and digest(sample()) not in hashes


@pytest.mark.parametrize("operational", [
    ri.RenderOperationalMetadata(), ri.RenderOperationalMetadata("other-client", None, None, None),
    ri.RenderOperationalMetadata(None, "0198c1a2-7b3c-7d4e-8f5a-999999999999", 7, A)])
def test_changing_operational_metadata_does_not_change_the_hash(operational):
    assert digest(replaced(sample(), operational=operational)) == digest(sample())


# --- order, equality, optional values, decimals, unicode -----------------------------------------------------------------------------------

def test_equivalent_inputs_give_identical_json_and_hashes():
    first, second = sample(), sample()
    assert first is not second and rh.canonical_render_input_json(first) == rh.canonical_render_input_json(second)
    assert digest(first) == digest(second)


def test_lists_and_tuples_give_the_same_hash():
    base = sample()
    as_lists = replaced(base, keywords=list(base.keywords), sections=list(base.sections))
    assert digest(as_lists) == digest(base)


@pytest.mark.parametrize("field", ["keywords", "sections", "recommendations", "historical_statements"])
def test_order_of_ordered_collections_is_preserved_in_the_hash(field):
    base = sample()
    items = getattr(base, field)
    assert len(items) > 1
    assert digest(replaced(base, **{field: tuple(reversed(items))})) != digest(base)


def test_paragraph_daily_and_country_order_matter():
    base = sample()
    assert digest(replaced(base, sections=(replaced(base.sections[0], paragraphs=("Second paragraph.", "First paragraph.")),) + base.sections[1:])) != digest(base)
    with pytest.raises(ri.RenderInputError):  # the daily series is chronological by contract, so it cannot be reordered
        replaced(base, search_console=replaced(base.search_console, daily=tuple(reversed(base.search_console.daily))))
    assert digest(replaced(base, geography=replaced(base.geography, countries=tuple(reversed(base.geography.countries))))) != digest(base)


def test_font_order_does_not_matter_because_fonts_are_a_set():
    fonts = (ri.FontFingerprint("A-font", A), ri.FontFingerprint("B-font", B))
    assert digest(replaced(sample(), fingerprints=fingerprints(fonts=fonts))) == \
        digest(replaced(sample(), fingerprints=fingerprints(fonts=tuple(reversed(fonts)))))


def test_dictionary_insertion_order_cannot_matter_because_no_dictionary_is_part_of_the_input():
    from test_render_input import sample as make
    kinds = set()

    def walk(item):
        kinds.add(type(item))
        if dataclasses.is_dataclass(item):
            for f in dataclasses.fields(item):
                walk(getattr(item, f.name))
        elif isinstance(item, tuple):
            for child in item:
                walk(child)

    walk(make())
    assert dict not in kinds
    payload = rh.build_payload(make())
    shuffled = json.loads(json.dumps(payload), object_pairs_hook=lambda pairs: dict(reversed(pairs)))
    assert json.dumps(shuffled, sort_keys=True, ensure_ascii=False, separators=(",", ":")) == \
        rh.canonical_render_input_json(make())


def test_missing_optional_values_are_handled_consistently():
    bare = ri.RenderSender("N")
    value = replaced(sample(), sender=bare, ranking_footnote=None)
    payload = json.loads(rh.canonical_render_input_json(value))["input"]
    assert payload["sender"] == {"address_lines": [], "email": None, "name": "N", "phone": None, "title": None,
                                 "website_label": None, "website_url": None}
    assert payload["ranking_footnote"] is None
    assert digest(value) == digest(replaced(sample(), sender=ri.RenderSender("N", None, [], None, None, None, None),
                                            ranking_footnote=None))


def test_empty_text_and_null_are_different():
    assert digest(replaced(sample(), ranking_footnote="")) != digest(replaced(sample(), ranking_footnote=None))
    assert digest(replaced(sample(), sender=replaced(sample().sender, website_url=""))) != \
        digest(replaced(sample(), sender=replaced(sample().sender, website_url=None)))


@pytest.mark.parametrize("a,b", [(Decimal("0.03"), Decimal("0.030")), (Decimal("1"), Decimal("1.00")), (Decimal(1), 1),
                                 (Decimal("100"), Decimal("1E+2")), (Decimal("0"), Decimal("0.0")), (Decimal("0"), Decimal("-0"))])
def test_equal_decimals_hash_the_same_whatever_their_spelling(a, b):
    base = sample()
    assert digest(replaced(base, search_console=replaced(base.search_console, ctr=a))) == \
        digest(replaced(base, search_console=replaced(base.search_console, ctr=b)))


def test_different_decimals_hash_differently_and_use_plain_notation():
    base = sample()
    one = replaced(base, search_console=replaced(base.search_console, ctr=Decimal("0.03")))
    two = replaced(base, search_console=replaced(base.search_console, ctr=Decimal("0.031")))
    assert digest(one) != digest(two)
    text = rh.canonical_render_input_json(replaced(base, search_console=replaced(base.search_console, ctr=Decimal("1E+2"))))
    assert '"ctr":"100"' in text and "E+" not in text


def test_unicode_is_written_exactly_and_consistently():
    composed = replaced(sample(), client=ri.RenderClient("Café", "https://x.example"))
    decomposed = replaced(sample(), client=ri.RenderClient("Café", "https://x.example"))
    assert digest(composed) == digest(composed) and digest(composed) != digest(decomposed)  # no normalisation
    assert "Café" in rh.canonical_render_input_json(composed)
    emoji = replaced(sample(), ranking_footnote="Up \U0001f680 — “quoted”")
    assert digest(emoji) == digest(replaced(sample(), ranking_footnote="Up \U0001f680 — “quoted”"))


def test_text_is_not_trimmed_or_case_folded():
    base = sample()
    assert digest(replaced(base, ranking_footnote="Note ")) != digest(replaced(base, ranking_footnote="Note"))
    assert digest(replaced(base, ranking_footnote="note")) != digest(replaced(base, ranking_footnote="Note"))


def test_dates_are_written_as_iso_text():
    payload = json.loads(rh.canonical_render_input_json(sample()))["input"]
    assert payload["generation_date"] == "2026-10-30" and payload["period"]["start"] == "2026-09-29"


# --- unsupported and invalid input -----------------------------------------------------------------------------------------------------

def test_only_a_client_render_input_can_be_hashed():
    for bad in (None, {}, "text", sample().client, [sample()]):
        with pytest.raises(rh.RenderInputHashError):
            rh.render_input_sha256(bad)


def bypass(value, **changes):
    """A copy with values set without validation, as a buggy caller or later change might produce."""
    copy_ = dataclasses.replace(value)
    for name, item in changes.items():
        object.__setattr__(copy_, name, item)
    return copy_


@pytest.mark.parametrize("name,bad", [
    ("ranking_footnote", 1.5), ("ranking_footnote", {"a": 1}), ("ranking_footnote", b"bytes"),
    ("generation_date", dt.datetime(2026, 10, 30, 1, 2, 3)), ("generation_date", "2026-10-30"),
    ("keywords", [object()]), ("ranking_footnote", {1, 2}), ("ranking_footnote", float("nan")),
    ("ranking_footnote", "lone \ud800 surrogate")])
def test_unsupported_values_fail_clearly_and_are_never_turned_into_text(name, bad):
    with pytest.raises(rh.RenderInputHashError) as caught:
        rh.render_input_sha256(bypass(sample(), **{name: bad}))
    assert "render_input." in str(caught.value) and str(bad) not in str(caught.value)


def test_float_and_non_finite_decimals_never_reach_the_hash():
    base = sample()
    for bad in (0.03, Decimal("NaN"), Decimal("Infinity")):
        broken = bypass(base.search_console, ctr=bad)
        with pytest.raises(rh.RenderInputHashError):
            rh.render_input_sha256(bypass(base, search_console=broken))


def test_a_non_model_dataclass_is_refused():
    @dataclasses.dataclass(frozen=True)
    class Foreign:
        x: int = 1

    with pytest.raises(rh.RenderInputHashError):
        rh.render_input_sha256(bypass(sample(), client=Foreign()))


def test_the_hash_reads_no_clock_and_draws_no_random_value(monkeypatch):
    import time

    monkeypatch.setattr(time, "time", lambda: (_ for _ in ()).throw(AssertionError("the clock was read")))
    first = digest(sample())
    second = digest(sample())
    assert first == second
    source = (ROOT / "src" / "seo_report" / "render_input_hash.py").read_text(encoding="utf-8")
    source = re.sub(r'""".*?"""', "", source, flags=re.S)  # the documentation may name what is not used
    for word in ("now(", "today(", "time.time", "utcnow", "random", "uuid", "secrets", "os.environ", "open(",
                 "Path(", "requests", "socket"):
        assert word not in source, word


# --- field classification safeguard ------------------------------------------------------------------------------------------------------

def test_every_model_field_is_classified_and_nothing_stale_is_classified():
    assert rh.classification_problems() == []
    assert {c.__name__ for c in ri.model_classes()} == set(rh.FIELD_CLASSES)


def test_a_new_unclassified_field_is_detected_and_blocks_hashing():
    @dataclasses.dataclass(frozen=True)
    class RenderClient(ri.RenderClient):  # same name as the real class, one more field
        nickname: str = "x"

    assert rh.classification_problems([RenderClient]) == ["RenderClient.nickname: the field is not classified"]
    # the hash itself refuses an instance whose class is not the registered model
    with pytest.raises(rh.RenderInputHashError):
        rh.render_input_sha256(bypass(sample(), client=RenderClient("N", "https://x.example")))


def test_new_fields_on_the_real_classes_are_detected(monkeypatch):
    # Remove one real classification and add a bogus one: both directions must be reported.
    table = {name: dict(fields) for name, fields in rh.FIELD_CLASSES.items()}
    del table["RenderSender"]["email"]
    table["RenderSender"]["fax"] = rh.INCLUDED
    table.pop("RenderPeriod")
    table["Ghost"] = {"x": rh.INCLUDED}
    table["RenderClient"]["name"] = "maybe"
    problems = rh.classification_problems(classification=table)
    assert "RenderSender.email: the field is not classified" in problems
    assert "RenderSender.fax: classified but the field does not exist" in problems
    assert "RenderPeriod: the class is not classified" in problems
    assert "Ghost: classified but the class does not exist" in problems
    assert any("RenderClient.name" in p and "'included' or 'excluded'" in p for p in problems)
    monkeypatch.setattr(rh, "FIELD_CLASSES", table)
    with pytest.raises(rh.RenderInputHashError):
        rh.render_input_sha256(sample())


# The behavioural check inspects the real model objects: it changes every field of every nested model, one at a time,
# and requires the hash to change exactly for the fields classified as included.

def alternatives(value):
    if isinstance(value, bool):
        yield not value
    elif isinstance(value, int):
        yield value + 1
    elif isinstance(value, Decimal):
        yield value + 1
    elif isinstance(value, dt.date):
        yield value + dt.timedelta(days=1)
    elif isinstance(value, str):
        if re.fullmatch(r"[0-9a-f]{64}", value):
            yield ("1" if value[0] != "1" else "2") + value[1:]
        yield value + "x"
        yield "incomplete" if value != "incomplete" else "available"
        yield "ok" if value != "ok" else "unavailable"
    elif isinstance(value, tuple):
        if value:
            yield value[:-1]
        if all(isinstance(item, str) for item in value):
            yield value + ("extra",)
    elif value is None:
        yield "x"
        yield 1
        yield Decimal(1)
        yield dt.date(2026, 1, 1)


def walk(item, path=()):
    if dataclasses.is_dataclass(item) and not isinstance(item, type):
        for f in dataclasses.fields(item):
            value = getattr(item, f.name)
            yield path + (f.name,), type(item).__name__, f.name, value
            yield from walk(value, path + (f.name,))
    elif isinstance(item, tuple):
        for index, child in enumerate(item):
            yield from walk(child, path + (index,))


def set_at(item, path, value):
    head, rest = path[0], path[1:]
    if isinstance(item, tuple):
        items = list(item)
        items[head] = value if not rest else set_at(items[head], rest, value)
        return tuple(items)
    return dataclasses.replace(item, **{head: value if not rest else set_at(getattr(item, head), rest, value)})


def test_every_field_changes_the_hash_exactly_when_it_is_classified_as_included():
    base = sample()
    base_hash = digest(base)
    checked, skipped = {}, []
    for path, cls_name, field_name, value in walk(base):
        key = f"{cls_name}.{field_name}"
        expected = rh.FIELD_CLASSES[cls_name][field_name]
        done = False
        for candidate in alternatives(value):
            try:
                changed = set_at(base, path, candidate)
            except ri.RenderInputError:
                continue
            if changed == base:
                continue
            outcome = digest(changed) != base_hash
            assert outcome == (expected == rh.INCLUDED), f"{key} at {path} is classified {expected} but hash changed={outcome}"
            checked[key] = expected
            done = True
            break
        if not done:
            skipped.append(key)
    every_field = {f"{c.__name__}.{f.name}" for c in ri.model_classes() for f in dataclasses.fields(c)}
    # every field was exercised at least once (a few fields whose value depends on a sibling are covered by name below)
    assert set(checked) | set(skipped) == every_field
    # fields whose value is a nested model are judged through the nested fields; the others depend on a sibling field
    nested_models = {f"{cls_name}.{field_name}" for _, cls_name, field_name, value in walk(base)
                     if dataclasses.is_dataclass(value)}
    sibling_dependent = {"RenderRanking.kind", "RenderRanking.position", "RenderRanking.text",
                         "RenderFingerprints.page_size", "TruncationNote.reason", "TruncationNote.omitted_rows",
                         "DailyPoint.day", "Ga4DailyPoint.day"}  # a day must stay ascending relative to its neighbours
    assert set(skipped) <= sibling_dependent | nested_models, sorted(set(skipped) - sibling_dependent - nested_models)
    assert {k for k, v in checked.items() if v == rh.EXCLUDED} == {
        "RenderOperationalMetadata.client_key", "RenderOperationalMetadata.run_id",
        "RenderOperationalMetadata.revision", "RenderOperationalMetadata.snapshot_content_hash"}
    assert rh.FIELD_CLASSES["ClientRenderInput"]["operational"] == rh.EXCLUDED
    assert len(checked) >= 60


def test_availability_completeness_partial_values_and_truncation_each_affect_the_hash():
    base = sample()
    assert len({digest(base), digest(replaced(base, search_console=replaced(base.search_console, status="incomplete"))),
                digest(replaced(base, search_console=ri.SearchConsoleVisuals.unavailable()))}) == 3
    full = replaced(base, search_console=replaced(base.search_console, average_position=Decimal("0")))
    missing = replaced(base, search_console=replaced(base.search_console, average_position=None))
    assert digest(full) != digest(missing) and digest(missing) != digest(base)  # zero, missing and a value differ
    assert digest(replaced(base, geography=replaced(base.geography, truncation=()))) != digest(base)
    text = rh.canonical_render_input_json(missing)
    assert '"average_position":null' in text and '"status":"available"' in text


def test_a_missing_day_and_a_measured_zero_hash_differently():
    base = sample()
    series = base.search_console.daily
    gap = replaced(base, search_console=replaced(base.search_console, daily=(series[0], ri.DailyPoint(dt.date(2026, 10, 4), 0, 0))))
    zero_fill = replaced(base, search_console=replaced(base.search_console, daily=(series[0], ri.DailyPoint(dt.date(2026, 10, 3), 0, 0), ri.DailyPoint(dt.date(2026, 10, 4), 0, 0))))
    assert digest(gap) != digest(zero_fill)


def test_truncation_notes_are_written_with_their_reason_and_exact_omitted_count():
    payload = json.loads(rh.canonical_render_input_json(sample()))["input"]
    assert payload["geography"]["truncation"] == [
        {"dataset": "countries", "omitted_rows": 7, "reason": "row_limit"},
        {"dataset": "countries", "omitted_rows": None, "reason": "source_limit"}]


def test_truncation_note_order_does_not_change_the_hash():
    base = sample()
    notes = base.geography.truncation
    assert digest(replaced(base, geography=replaced(base.geography, truncation=tuple(reversed(notes))))) == digest(base)


def test_sibling_dependent_fields_are_covered_explicitly():
    base = sample()
    assert digest(replaced(base, search_console=ri.SearchConsoleVisuals.unavailable())) != digest(base)
    assert digest(replaced(base, ga4=ri.Ga4Visuals.unavailable())) != digest(base)
    assert digest(replaced(base, geography=ri.GeographyVisuals.unavailable())) != digest(base)
    row = base.keywords[0]
    assert digest(replaced(base, keywords=(replaced(row, current=ri.RenderRanking("above_10", None, "Above 10")),) + base.keywords[1:])) != digest(base)
    assert digest(replaced(base, keywords=(replaced(row, current=ri.RenderRanking("position", 3, "text")),) + base.keywords[1:])) != digest(base)
    assert digest(replaced(base, keywords=(replaced(row, current=ri.RenderRanking("position", 4)),) + base.keywords[1:])) != digest(base)
