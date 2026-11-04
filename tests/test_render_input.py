import dataclasses
import datetime as dt
import sys
from decimal import Decimal
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from seo_report import clients
from seo_report import render_input as ri

A, B, C, D = "a" * 64, "b" * 64, "c" * 64, "d" * 64


def fingerprints(**overrides):
    values = dict(template_version="r1.0", logo_sha256=A,
                  fonts=(ri.FontFingerprint("DejaVuSans-Bold", C), ri.FontFingerprint("DejaVuSans", B)))
    values.update(overrides)
    return ri.RenderFingerprints(**values)


def sample(**overrides):
    values = dict(
        client=ri.RenderClient("Sample Roofing Co", "https://sample.example"),
        period=ri.RenderPeriod(dt.date(2026, 9, 29), dt.date(2026, 10, 26)),
        comparison_period=ri.RenderPeriod(dt.date(2026, 9, 1), dt.date(2026, 9, 28)),
        sender=ri.RenderSender("Sample Sender", "Project Manager | Sample Co", ("1 Sample Way", "Sample City, TX 00000"),
                               "(000) 000-0000", "sender@example.test", "Sample Co", ""),
        generation_date=dt.date(2026, 10, 30),
        keywords=(
            ri.RenderKeywordRow("roof repair", "Sample City", ri.RenderRanking("position", 5),
                                ri.RenderRanking("position", 3), 2),
            ri.RenderKeywordRow("gutter install", "", ri.RenderRanking("above_10", None, "Above 10"),
                                ri.RenderRanking("position", 8), None),
            ri.RenderKeywordRow("siding", "Other Town", ri.RenderRanking("missing"), ri.RenderRanking("missing"), None)),
        ranking_footnote="Rankings are positions in Google results.",
        sections=(
            ri.RenderSection("keywords", "Keyword Rankings", "ok", ("First paragraph.", "Second paragraph.")),
            ri.RenderSection("search_console", "Google Search Console", "ok", ("Search paragraph.",)),
            ri.RenderSection("geography", "Geographical Performance", "unavailable", ()),
            ri.RenderSection("ga4", "Google Analytics 4", "ok", ("Analytics paragraph.",))),
        recommendations=(ri.RenderRecommendation("Improve titles", "Rewrite the title tags."),
                         ri.RenderRecommendation("Build links", "Earn two local links.")),
        historical_statements=(
            ri.RenderStatement("keywords", "keywords.up", "Rankings improved since last month.", "rankings",
                               dt.date(2026, 9, 1), dt.date(2026, 9, 28)),
            ri.RenderStatement("search_console", "gsc.clicks", "Clicks rose.", "traffic", dt.date(2026, 9, 1),
                               dt.date(2026, 9, 28))),
        search_console=ri.SearchConsoleVisuals(
            "available", 120, 4000, Decimal("0.03"), Decimal("12.5"),
            (ri.DailyPoint(dt.date(2026, 10, 1), 4, 150), ri.DailyPoint(dt.date(2026, 10, 2), 6, 170))),
        geography=ri.GeographyVisuals(
            "available", (ri.CountryRow("United States", 100, 3000), ri.CountryRow("Canada", 20, 1000)),
            (ri.TruncationNote("countries", "source_limit"), ri.TruncationNote("countries", "row_limit", 7))),
        ga4=ri.Ga4Visuals("available", 300, 250, Decimal("0.55"),
                          (ri.ChannelRow("Organic Search", 200, 150, 120), ri.ChannelRow("Direct", 100, 100, 40)),
                          (ri.Ga4DailyPoint(dt.date(2026, 10, 1), 10), ri.Ga4DailyPoint(dt.date(2026, 10, 2), 12)),
                          (ri.TruncationNote("channels", "row_limit", 3), ri.TruncationNote("daily", "source_limit"))),
        fingerprints=fingerprints(),
        operational=ri.RenderOperationalMetadata("sample-co", "0198c1a2-7b3c-7d4e-8f5a-1234567890ab", 1, D))
    values.update(overrides)
    return ri.ClientRenderInput(**values)


# --- construction and defaults ------------------------------------------------------------------------------------------------

def test_a_complete_input_is_built():
    value = sample()
    assert value.generation_date == dt.date(2026, 10, 30) and len(value.keywords) == 3
    assert [f.name for f in value.fingerprints.fonts] == ["DejaVuSans", "DejaVuSans-Bold"]  # sorted by label


def test_unavailable_visuals_are_constructed_explicitly():
    value = sample(search_console=ri.SearchConsoleVisuals.unavailable(), geography=ri.GeographyVisuals.unavailable(),
                   ga4=ri.Ga4Visuals.unavailable())
    assert value.search_console.status == "unavailable" and value.ga4.channels == ()
    assert value.geography.truncation == () and value.ga4.truncation == ()


def test_operational_metadata_defaults_to_empty():
    value = dataclasses.replace(sample(), operational=ri.RenderOperationalMetadata())
    assert value.operational == ri.RenderOperationalMetadata(None, None, None, None)


# --- immutability --------------------------------------------------------------------------------------------------------------------

def test_the_top_level_object_is_frozen():
    value = sample()
    with pytest.raises(dataclasses.FrozenInstanceError):
        value.generation_date = dt.date(2027, 1, 1)
    with pytest.raises(dataclasses.FrozenInstanceError):
        value.keywords = ()


def test_every_nested_model_is_frozen():
    value = sample()
    nested = [value.client, value.period, value.sender, value.keywords[0], value.keywords[0].current,
              value.sections[0], value.recommendations[0], value.historical_statements[0], value.search_console,
              value.search_console.daily[0], value.geography.countries[0], value.ga4, value.ga4.channels[0],
              value.ga4.daily[0], value.fingerprints, value.fingerprints.fonts[0], value.operational]
    for item in nested:
        first = dataclasses.fields(item)[0].name
        with pytest.raises(dataclasses.FrozenInstanceError):
            setattr(item, first, getattr(item, first))


def test_collections_are_tuples_that_cannot_be_changed():
    value = sample()
    for collection in (value.keywords, value.sections, value.recommendations, value.historical_statements,
                       value.sender.address_lines, value.sections[0].paragraphs, value.search_console.daily,
                       value.geography.countries, value.ga4.channels, value.ga4.daily, value.fingerprints.fonts):
        assert type(collection) is tuple
        assert not hasattr(collection, "append")


def test_lists_are_copied_so_the_callers_list_cannot_change_the_object():
    paragraphs, rows = ["one", "two"], [ri.RenderRecommendation("t", "a")]
    section = ri.RenderSection("keywords", "Keyword Rankings", "ok", paragraphs)
    value = sample(recommendations=rows)
    paragraphs.append("three")
    rows.append(ri.RenderRecommendation("x", "y"))
    assert section.paragraphs == ("one", "two") and len(value.recommendations) == 1


def test_there_are_no_mutable_containers_anywhere_in_the_model():
    def walk(item):
        yield item
        if dataclasses.is_dataclass(item):
            for f in dataclasses.fields(item):
                yield from walk(getattr(item, f.name))
        elif isinstance(item, tuple):
            for child in item:
                yield from walk(child)

    kinds = {type(x) for x in walk(sample())}
    assert not kinds & {list, dict, set, bytearray}
    assert float not in kinds


# --- validation ----------------------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("build", [
    lambda: ri.RenderClient("", "https://x.example"), lambda: ri.RenderClient("Name", "   "),
    lambda: ri.RenderClient(5, "https://x.example"), lambda: ri.RenderClient("Na\x00me", "https://x.example"),
    lambda: ri.RenderClient("bad \ud800 text", "https://x.example"),
    lambda: ri.RenderPeriod(dt.date(2026, 2, 2), dt.date(2026, 2, 1)),
    lambda: ri.RenderPeriod(dt.datetime(2026, 2, 1), dt.date(2026, 2, 2)),
    lambda: ri.RenderPeriod("2026-02-01", dt.date(2026, 2, 2)),
    lambda: ri.RenderRanking("position"), lambda: ri.RenderRanking("position", 0),
    lambda: ri.RenderRanking("above_10", 3, "Above 10"), lambda: ri.RenderRanking("bogus"),
    lambda: ri.RenderRanking("position", True),
    lambda: ri.RenderKeywordRow("k", "l", "5", ri.RenderRanking("missing"), None),
    lambda: ri.RenderKeywordRow("", "l", ri.RenderRanking("missing"), ri.RenderRanking("missing"), None),
    lambda: ri.RenderSection("Bad Key", "T", "ok", ()), lambda: ri.RenderSection("k", "T", "maybe", ()),
    lambda: ri.RenderSection("k", "T", "ok", "one paragraph as text"), lambda: ri.RenderSection("k", "T", "ok", (1,)),
    lambda: ri.RenderRecommendation("", "a"), lambda: ri.RenderRecommendation("t", ""),
    lambda: ri.RenderStatement("keywords", "k", "t", "g", dt.date(2026, 2, 2), dt.date(2026, 2, 1)),
    lambda: ri.RenderSender("", None), lambda: ri.RenderSender("n", address_lines="one line"),
    lambda: ri.RenderSender("n", address_lines=(1,)),
    lambda: ri.DailyPoint(dt.date(2026, 1, 1), -1, 0), lambda: ri.DailyPoint(dt.date(2026, 1, 1), 1.5, 0),
    lambda: ri.DailyPoint(dt.date(2026, 1, 1), True, 0),
    lambda: ri.CountryRow("", 1, 1), lambda: ri.ChannelRow("c", 1, -1, 1),
])
def test_invalid_values_are_rejected(build):
    with pytest.raises(ri.RenderInputError):
        build()


@pytest.mark.parametrize("value", [0.03, float("nan"), Decimal("NaN"), Decimal("Infinity"), Decimal("-1"), "0.03", True])
def test_decimals_must_be_finite_non_negative_and_not_floats(value):
    with pytest.raises(ri.RenderInputError):
        ri.SearchConsoleVisuals("available", 1, 1, value, Decimal(1), ())


def test_integers_are_accepted_for_decimal_fields_and_become_decimals():
    visual = ri.SearchConsoleVisuals("available", 1, 1, 1, 2, ())
    assert visual.ctr == Decimal(1) and isinstance(visual.average_position, Decimal)


def test_the_three_visual_states_are_exactly_these():
    assert ri.VISUAL_STATUSES == ("available", "incomplete", "unavailable")
    for status in ("ok", "complete", "partial", "", None, 1):
        for build in (lambda s: ri.SearchConsoleVisuals(s, 1), lambda s: ri.GeographyVisuals(s, (ri.CountryRow("US", 1, 1),)),
                      lambda s: ri.Ga4Visuals(s, 1)):
            with pytest.raises(ri.RenderInputError):
                build(status)


def test_unavailable_visuals_carry_no_data():
    with pytest.raises(ri.RenderInputError):
        ri.SearchConsoleVisuals("unavailable", clicks=1)
    with pytest.raises(ri.RenderInputError):
        ri.SearchConsoleVisuals("unavailable", daily=(ri.DailyPoint(dt.date(2026, 1, 1), 0, 0),))
    with pytest.raises(ri.RenderInputError):
        ri.GeographyVisuals("unavailable", (ri.CountryRow("US", 1, 1),))
    with pytest.raises(ri.RenderInputError):
        ri.GeographyVisuals("unavailable", truncation=(ri.TruncationNote("countries", "source_limit"),))
    with pytest.raises(ri.RenderInputError):
        ri.Ga4Visuals("unavailable", daily=(ri.Ga4DailyPoint(dt.date(2026, 1, 1), 1),))
    with pytest.raises(ri.RenderInputError):
        ri.Ga4Visuals("unavailable", channels=(ri.ChannelRow("Direct", 1, 1, 1),))
    with pytest.raises(ri.RenderInputError):
        ri.Ga4Visuals("unavailable", truncation=(ri.TruncationNote("daily", "source_limit"),))


def test_available_and_incomplete_visuals_need_at_least_one_usable_value():
    for status in ("available", "incomplete"):
        with pytest.raises(ri.RenderInputError):
            ri.SearchConsoleVisuals(status)
        with pytest.raises(ri.RenderInputError):
            ri.GeographyVisuals(status)
        with pytest.raises(ri.RenderInputError):
            ri.Ga4Visuals(status)
        # one usable value is enough
        assert ri.SearchConsoleVisuals(status, clicks=0).clicks == 0
        assert ri.SearchConsoleVisuals(status, daily=(ri.DailyPoint(dt.date(2026, 1, 1), 0, 0),)).daily
        assert ri.GeographyVisuals(status, (ri.CountryRow("US", 0, 0),)).status == status
        assert ri.Ga4Visuals(status, users=0).users == 0
        assert ri.Ga4Visuals(status, channels=(ri.ChannelRow("Direct", 0, 0, 0),)).channels
        assert ri.Ga4Visuals(status, daily=(ri.Ga4DailyPoint(dt.date(2026, 1, 1), 0),)).daily


def test_partial_search_console_totals_are_allowed_and_missing_stays_none():
    visual = ri.SearchConsoleVisuals("available", 120, 3500, Decimal("0.0343"), None)
    assert (visual.clicks, visual.impressions, visual.ctr, visual.average_position) == (120, 3500, Decimal("0.0343"), None)
    only_position = ri.SearchConsoleVisuals("available", None, None, None, Decimal("4.2"))
    assert only_position.clicks is None and only_position.average_position == Decimal("4.2")


def test_partial_ga4_totals_are_allowed_and_missing_stays_none():
    visual = ri.Ga4Visuals("available", 300, None, Decimal("0.5"))
    assert (visual.sessions, visual.users, visual.engagement_rate) == (300, None, Decimal("0.5"))
    assert ri.Ga4Visuals("incomplete", None, None, None, (ri.ChannelRow("Direct", 1, 1, 1),)).sessions is None


def test_zero_is_a_value_and_none_is_missing():
    zero = ri.SearchConsoleVisuals("available", 0, 0, Decimal(0), Decimal(0))
    assert zero.clicks == 0 and zero.clicks is not None and zero != ri.SearchConsoleVisuals("available", None, 0, Decimal(0), Decimal(0))


def test_daily_series_must_be_strictly_increasing():
    one, two = ri.DailyPoint(dt.date(2026, 10, 1), 1, 1), ri.DailyPoint(dt.date(2026, 10, 2), 1, 1)
    assert ri.SearchConsoleVisuals("available", clicks=1, daily=(one, two)).daily == (one, two)
    for bad in ((two, one), (one, one)):
        with pytest.raises(ri.RenderInputError):
            ri.SearchConsoleVisuals("available", clicks=1, daily=bad)
    g1, g2 = ri.Ga4DailyPoint(dt.date(2026, 10, 1), 1), ri.Ga4DailyPoint(dt.date(2026, 10, 2), 1)
    for bad in ((g2, g1), (g1, g1)):
        with pytest.raises(ri.RenderInputError):
            ri.Ga4Visuals("available", sessions=1, daily=bad)


def test_a_missing_day_is_a_gap_in_the_dates_not_a_zero_point():
    days = [dt.date(2026, 10, d) for d in (1, 2, 4, 5)]
    visual = ri.SearchConsoleVisuals("available", clicks=4, daily=tuple(ri.DailyPoint(d, 1, 10) for d in days))
    assert dt.date(2026, 10, 3) not in {p.day for p in visual.daily} and len(visual.daily) == 4


def test_truncation_notes_are_validated():
    ri.TruncationNote("countries", "source_limit")
    ri.TruncationNote("countries", "snapshot_cap")
    assert ri.TruncationNote("countries", "row_limit", 5).omitted_rows == 5
    for build in (lambda: ri.TruncationNote("countries", "bogus"), lambda: ri.TruncationNote("Countries", "source_limit"),
                  lambda: ri.TruncationNote("", "source_limit"), lambda: ri.TruncationNote("countries", "row_limit"),
                  lambda: ri.TruncationNote("countries", "row_limit", 0),
                  lambda: ri.TruncationNote("countries", "source_limit", 5),
                  lambda: ri.TruncationNote("countries", "snapshot_cap", 5),
                  lambda: ri.TruncationNote("countries", "row_limit", True),
                  lambda: ri.TruncationNote("countries", "row_limit", 2.5)):
        with pytest.raises(ri.RenderInputError):
            build()
    assert ri.TRUNCATION_REASONS == ("source_limit", "snapshot_cap", "row_limit")


def test_truncation_notes_are_unique_per_dataset_and_reason_and_sorted():
    a, b = ri.TruncationNote("countries", "source_limit"), ri.TruncationNote("countries", "row_limit", 2)
    visual = ri.GeographyVisuals("available", (ri.CountryRow("US", 1, 1),), (a, b))
    assert [n.reason for n in visual.truncation] == ["row_limit", "source_limit"]
    assert visual == ri.GeographyVisuals("available", (ri.CountryRow("US", 1, 1),), (b, a))
    with pytest.raises(ri.RenderInputError):
        ri.GeographyVisuals("available", (ri.CountryRow("US", 1, 1),), (a, a))
    with pytest.raises(ri.RenderInputError):
        ri.GeographyVisuals("available", (ri.CountryRow("US", 1, 1),), ("truncated",))
    assert ri.Ga4Visuals("available", 1, truncation=[ri.TruncationNote("daily", "source_limit")]).truncation[0].dataset == "daily"


def test_incomplete_and_truncated_visuals_keep_their_data():
    visual = ri.GeographyVisuals("incomplete", (ri.CountryRow("US", 1, 1),), (ri.TruncationNote("countries", "snapshot_cap"),))
    assert visual.status == "incomplete" and visual.countries and visual.truncation


@pytest.mark.parametrize("overrides", [
    dict(template_version=""), dict(template_version="has space"), dict(template_version="x" * 40),
    dict(logo_sha256="A" * 64), dict(logo_sha256="a" * 63), dict(logo_sha256=None), dict(page_size="Letter"),
    dict(fonts=()), dict(fonts=(ri.FontFingerprint("F", A), ri.FontFingerprint("F", B))),
    dict(fonts=("DejaVuSans",)), dict(fonts="DejaVuSans")])
def test_invalid_fingerprints_are_rejected(overrides):
    with pytest.raises(ri.RenderInputError):
        fingerprints(**overrides)


@pytest.mark.parametrize("name,sha", [("", A), ("ok", "Z" * 64), ("ok", "a" * 65), ("bad/name", A), ("ok", None)])
def test_invalid_font_fingerprints_are_rejected(name, sha):
    with pytest.raises(ri.RenderInputError):
        ri.FontFingerprint(name, sha)


def test_the_render_input_validates_its_parts():
    for overrides in (dict(client="not a client"), dict(generation_date=dt.datetime(2026, 10, 30, 1, 2)),
                      dict(generation_date="2026-10-30"), dict(generation_date=None), dict(keywords=("row",)),
                      dict(keywords="rows"), dict(search_console=None), dict(operational={"run_id": "x"}),
                      dict(ranking_footnote=5)):
        with pytest.raises(ri.RenderInputError):
            sample(**overrides)


def test_duplicate_section_keys_and_statement_keys_are_rejected():
    section = ri.RenderSection("keywords", "Keyword Rankings", "ok", ())
    with pytest.raises(ri.RenderInputError):
        sample(sections=(section, section))
    statement = ri.RenderStatement("keywords", "k", "t", "g", dt.date(2026, 1, 1), dt.date(2026, 1, 2))
    with pytest.raises(ri.RenderInputError):
        sample(historical_statements=(statement, statement))


def test_error_messages_name_the_field_not_the_value():
    with pytest.raises(ri.RenderInputError) as caught:
        ri.RenderClient("secret-token-ya29.abcdef", 5)
    assert "ya29" not in str(caught.value) and "client.website" in str(caught.value)


def test_unicode_text_and_empty_optionals_are_accepted():
    value = sample(client=ri.RenderClient("Crème — 日本", "https://x.example"), ranking_footnote="",
                   sender=ri.RenderSender("N"))
    assert value.ranking_footnote == "" and value.sender.title is None and value.sender.address_lines == ()


def test_the_module_never_reads_a_clock_or_random_value():
    source = (ROOT / "src" / "seo_report" / "render_input.py").read_text(encoding="utf-8")
    for word in ("datetime.now", "date.today", "time.time", "utcnow", "random", "uuid", "secrets", "os.environ",
                 "open(", "Path(", "requests", "socket"):
        assert word not in source, word


# --- construction from existing report data ----------------------------------------------------------------------------------------------

from test_history_access import env  # noqa: E402,F401  (temporary history and output folders)
import test_report_run as trr  # noqa: E402
import test_reviewer_record as trv  # noqa: E402


def sender_of(**overrides):
    values = dict(name="Sample Sender", title="PM", address_lines=("1 Sample Way",), phone="(000) 000-0000",
                  email="sender@example.test", website_label="Sample", website_url="")
    values.update(overrides)
    return clients.Sender(**values)


def test_a_real_assembled_report_converts_to_a_render_input(env):
    bundle, composed, _ = trv.pipeline(env)
    content = bundle.client_content
    value = ri.from_client_content(content, sender=sender_of(), generation_date=dt.date(2026, 10, 30),
                                   fingerprints=fingerprints(), historical_statements=composed.client_statements)
    assert value.client.name == content.client.name and len(value.keywords) == len(content.ranking_rows)
    assert [s.key for s in value.sections] == [s.key for s in content.sections]
    assert [(r.title, r.action) for r in value.recommendations] == [(r.title, r.action) for r in content.recommendations]
    assert [s.text for s in value.historical_statements] == [s.text for s in composed.client_statements]
    assert value.period.start == content.periods.current.start
    assert value.comparison_period.end == content.periods.previous.end
    assert value.search_console.status == value.geography.status == value.ga4.status == "unavailable"
    first, row = value.keywords[0], content.ranking_rows[0]
    assert (first.keyword, first.location, first.movement) == (row.keyword, row.target_location or "", row.movement)
    assert first.current.kind == row.current.kind.value and first.current.position == row.current.position


def test_conversion_does_not_change_the_source_objects(env):
    bundle, composed, _ = trv.pipeline(env)
    before = (bundle.client_content, composed.client_statements)
    ri.from_client_content(bundle.client_content, sender=sender_of(), generation_date=dt.date(2026, 10, 30),
                           fingerprints=fingerprints(), historical_statements=composed.client_statements)
    assert (bundle.client_content, composed.client_statements) == before


def test_statements_are_refused_in_two_places_and_markers_are_refused(env):
    bundle, _, _ = trv.pipeline(env)
    composed = trv.statement_composition()
    assert composed.client_statements
    converted = ri.from_client_content(bundle.client_content, sender=sender_of(), generation_date=dt.date(2026, 10, 30),
                                       fingerprints=fingerprints(), historical_statements=composed.client_statements)
    statement, source = converted.historical_statements[0], composed.client_statements[0]
    assert (statement.section, statement.key, statement.text, statement.group) == \
        (source.section, source.key, source.text, source.group)
    assert statement.baseline_start.isoformat() == source.baseline_period_start
    both = dataclasses.replace(bundle.client_content, historical_statements=tuple(composed.client_statements))
    with pytest.raises(ri.RenderInputError):
        ri.from_client_content(both, sender=sender_of(), generation_date=dt.date(2026, 10, 30),
                               fingerprints=fingerprints(), historical_statements=composed.client_statements)
    marked = dataclasses.replace(bundle.client_content, ranking_markers=("m",))
    with pytest.raises(ri.RenderInputError):
        ri.from_client_content(marked, sender=sender_of(), generation_date=dt.date(2026, 10, 30),
                               fingerprints=fingerprints())


def test_conversion_validates_its_arguments(env):
    bundle, _, _ = trv.pipeline(env, history=False)
    kwargs = dict(generation_date=dt.date(2026, 10, 30), fingerprints=fingerprints())
    with pytest.raises(ri.RenderInputError):
        ri.from_client_content(bundle, sender=sender_of(), **kwargs)
    with pytest.raises(ri.RenderInputError):
        ri.from_client_content(bundle.client_content, sender="Jimmy", **kwargs)
    with pytest.raises(ri.RenderInputError):
        ri.from_client_content(bundle.client_content, sender=sender_of(), historical_statements=["text"], **kwargs)
    with pytest.raises(ri.RenderInputError):
        ri.from_client_content(bundle.client_content, sender=sender_of(), generation_date="2026-10-30",
                               fingerprints=fingerprints())
