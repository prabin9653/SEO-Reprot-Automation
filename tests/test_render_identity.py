import dataclasses
import datetime as dt
import hashlib
import re
import shutil
import sys
from pathlib import Path

import matplotlib
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from seo_report import clients
from seo_report import pdf_renderer as pr
from seo_report import render_identity as idn
from seo_report import render_input as ri
from seo_report import render_input_hash as rh
from seo_report import report_document as rd
from seo_report import visual_extraction as vx
import test_report_run as trr  # noqa: F401
import test_reviewer_record as trv
from test_history_access import env  # noqa: F401
from test_render_input import sample
from test_render_input_hash import alternatives, set_at, walk
from test_report_document import full
from test_report_templates import ASSETS

LOGO = ROOT / "assets" / "jasper-logo.png"
FONT_DIR = Path(matplotlib.__file__).parent / "mpl-data" / "fonts" / "ttf"
FONTS = {"DejaVuSans": FONT_DIR / "DejaVuSans.ttf", "DejaVuSans-Bold": FONT_DIR / "DejaVuSans-Bold.ttf"}
PERSON = clients.Sender(name="Sample Sender", title="PM", address_lines=("1 Sample Way",), phone="(000) 000-0000",
                        email="sender@example.test", website_label="Sample", website_url="https://sender.example")
GENERATED = dt.date(2026, 10, 30)


# --- a small rendering tree for the fingerprint tests ----------------------------------------------------------------------------------

def make_tree(tmp_path):
    templates, sources = tmp_path / "templates", tmp_path / "src"
    (templates / "sub").mkdir(parents=True)
    sources.mkdir()
    (templates / "report.css").write_text("body { color: #000; }\n", encoding="utf-8")
    (templates / "page.html").write_text("<p>{{ x }}</p>\n", encoding="utf-8")
    (templates / "sub" / "inner.html").write_text("<i></i>\n", encoding="utf-8")
    for name in idn.RENDERING_SOURCES:
        (sources / name).write_text(f"# {name}\nVALUE = 1\n", encoding="utf-8")
    (sources / "unrelated.py").write_text("# not a rendering file\n", encoding="utf-8")
    return templates, sources


def fp(tmp_path, **kw):
    templates, sources = make_tree(tmp_path) if not (tmp_path / "templates").exists() else (tmp_path / "templates", tmp_path / "src")
    return idn.rendering_fingerprint(template_dir=templates, source_dir=sources, **kw)


# --- fingerprint -------------------------------------------------------------------------------------------------------------------------

def test_identical_rendering_files_give_identical_fingerprints(tmp_path):
    first = fp(tmp_path)
    assert first == fp(tmp_path) == idn.rendering_fingerprint(template_dir=tmp_path / "templates", source_dir=tmp_path / "src")
    assert re.fullmatch(r"t-[0-9a-f]{16}", first.label) and first.digest.startswith(first.label[2:])
    assert first.layout_version == idn.RENDER_LAYOUT_VERSION


def test_identical_copies_in_other_folders_give_the_same_fingerprint(tmp_path):
    one, two = tmp_path / "one", tmp_path / "two"
    one.mkdir(), two.mkdir()
    make_tree(one)
    shutil.copytree(one / "templates", two / "templates")
    shutil.copytree(one / "src", two / "src")
    assert fp(one) == fp(two)  # the location does not matter, only names and bytes


def test_a_css_change_changes_the_fingerprint(tmp_path):
    before = fp(tmp_path)
    (tmp_path / "templates" / "report.css").write_text("body { color: #111; }\n", encoding="utf-8")
    assert fp(tmp_path).label != before.label


def test_a_template_change_or_a_new_template_changes_the_fingerprint(tmp_path):
    before = fp(tmp_path)
    (tmp_path / "templates" / "sub" / "inner.html").write_text("<b></b>\n", encoding="utf-8")
    changed = fp(tmp_path)
    assert changed.label != before.label
    (tmp_path / "templates" / "new.html").write_text("<p></p>\n", encoding="utf-8")
    assert fp(tmp_path).label != changed.label


@pytest.mark.parametrize("name", idn.RENDERING_SOURCES)
def test_a_change_in_any_rendering_source_changes_the_fingerprint(tmp_path, name):
    before = fp(tmp_path)
    (tmp_path / "src" / name).write_text("VALUE = 2\n", encoding="utf-8")
    assert fp(tmp_path).label != before.label


def test_the_layout_version_changes_the_fingerprint(tmp_path):
    assert fp(tmp_path, layout_version="1").label != fp(tmp_path, layout_version="2").label
    assert fp(tmp_path, layout_version="2") == fp(tmp_path, layout_version="2")


def test_unrelated_files_do_not_affect_the_fingerprint(tmp_path):
    before = fp(tmp_path)
    (tmp_path / "src" / "unrelated.py").write_text("# changed\n", encoding="utf-8")
    (tmp_path / "src" / "another.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "elsewhere.txt").write_text("outside both folders\n", encoding="utf-8")
    (tmp_path / "token.json").write_text("{}", encoding="utf-8")
    assert fp(tmp_path) == before


def test_temporary_hidden_and_backup_files_in_the_template_folder_are_ignored(tmp_path):
    before = fp(tmp_path)
    folder = tmp_path / "templates"
    for name in (".hidden.html", "page.html~", "scratch.tmp", "old.bak", "x.swp", "m.pyc", "old.orig"):
        (folder / name).write_text("temporary", encoding="utf-8")
    (folder / "__pycache__").mkdir()
    (folder / "__pycache__" / "x.html").write_text("cache", encoding="utf-8")
    assert fp(tmp_path) == before


def test_line_endings_do_not_change_the_fingerprint(tmp_path):
    css = tmp_path / "templates" / "report.css"
    make_tree(tmp_path) if not css.exists() else None
    css.write_bytes(b"body { color: #000; }\n")
    lf = fp(tmp_path)
    css.write_bytes(b"body { color: #000; }\r\n")
    assert fp(tmp_path) == lf
    css.write_bytes(b"body { color: #000; }  \n")  # a real difference (trailing spaces) is still detected
    assert fp(tmp_path).label != lf.label


def test_a_file_rename_changes_the_fingerprint(tmp_path):
    before = fp(tmp_path)
    (tmp_path / "templates" / "page.html").rename(tmp_path / "templates" / "page2.html")
    assert fp(tmp_path).label != before.label


def test_missing_or_empty_inputs_fail_with_fixed_codes(tmp_path):
    templates, sources = make_tree(tmp_path)
    (sources / idn.RENDERING_SOURCES[0]).unlink()
    with pytest.raises(idn.RenderIdentityError) as caught:
        idn.rendering_fingerprint(template_dir=templates, source_dir=sources)
    assert caught.value.code == "rendering_source_missing"
    with pytest.raises(idn.RenderIdentityError) as caught:
        idn.rendering_fingerprint(template_dir=tmp_path / "nope", source_dir=sources)
    assert caught.value.code == "template_folder_missing"
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(idn.RenderIdentityError) as caught:
        idn.rendering_fingerprint(template_dir=empty, source_dir=sources)
    assert caught.value.code == "template_folder_empty"
    for bad in ("", "x" * 20, "bad version", None, 1):
        with pytest.raises(idn.RenderIdentityError):
            idn.rendering_fingerprint(template_dir=templates, source_dir=sources, layout_version=bad)


def test_the_real_fingerprint_covers_exactly_the_allowlisted_files():
    real = idn.rendering_fingerprint()
    names = [n for n, _ in real.files]
    assert names == sorted(names)
    assert [n for n in names if n.startswith("source/")] == sorted(f"source/{s}" for s in idn.RENDERING_SOURCES)
    assert sorted(n for n in names if n.startswith("templates/")) == sorted(
        f"templates/{p.name}" for p in (ROOT / "templates" / "report").iterdir() if p.is_file())
    assert len(names) == 5 + 7 and all(re.fullmatch(r"[0-9a-f]{64}", d) for _, d in real.files)
    text = repr(real)
    assert str(ROOT) not in text and "\\" not in text and "credential" not in text.lower() and "token" not in text.lower()
    assert real == idn.rendering_fingerprint() and re.fullmatch(r"t-[0-9a-f]{16}", real.label)


def test_the_real_fingerprint_reacts_to_a_copy_of_the_real_files_being_edited(tmp_path):
    templates, sources = tmp_path / "templates", tmp_path / "src"
    shutil.copytree(ROOT / "templates" / "report", templates)
    sources.mkdir()
    for name in idn.RENDERING_SOURCES:
        shutil.copyfile(ROOT / "src" / "seo_report" / name, sources / name)
    copy = idn.rendering_fingerprint(template_dir=templates, source_dir=sources)
    assert copy == idn.rendering_fingerprint()  # the same bytes give the same label
    (templates / "report.css").write_text((templates / "report.css").read_text(encoding="utf-8") + "\n.x { color: #000; }\n",
                                          encoding="utf-8")
    assert idn.rendering_fingerprint(template_dir=templates, source_dir=sources).label != copy.label
    shutil.copyfile(ROOT / "templates" / "report" / "report.css", templates / "report.css")
    with open(sources / "report_charts.py", "a", encoding="utf-8") as handle:
        handle.write("\n# changed\n")
    assert idn.rendering_fingerprint(template_dir=templates, source_dir=sources).label != copy.label


def test_render_fingerprints_carry_the_label_as_the_template_version():
    fingerprints = idn.measure_render_fingerprints(logo_path=LOGO, font_paths=FONTS)
    assert type(fingerprints) is ri.RenderFingerprints
    assert fingerprints.template_version == idn.rendering_fingerprint().label
    assert fingerprints.logo_sha256 == hashlib.sha256(LOGO.read_bytes()).hexdigest()
    assert [f.name for f in fingerprints.fonts] == ["DejaVuSans", "DejaVuSans-Bold"] and fingerprints.page_size == "A4"


def test_a_rendering_change_changes_the_render_input_hash(tmp_path):
    templates, sources = make_tree(tmp_path)
    a = idn.measure_render_fingerprints(logo_path=LOGO, font_paths=FONTS, template_dir=templates, source_dir=sources)
    (templates / "report.css").write_text("body { color: #222; }\n", encoding="utf-8")
    b = idn.measure_render_fingerprints(logo_path=LOGO, font_paths=FONTS, template_dir=templates, source_dir=sources)
    assert a.template_version != b.template_version
    assert rh.render_input_sha256(dataclasses.replace(sample(), fingerprints=a)) != \
        rh.render_input_sha256(dataclasses.replace(sample(), fingerprints=b))


# --- construction and bundle consistency ------------------------------------------------------------------------------------------------------

@pytest.fixture
def made(env):
    bundle, composed, _ = trv.pipeline(env)
    fingerprints = idn.measure_render_fingerprints(logo_path=LOGO, font_paths=FONTS)
    value = idn.build_render_input(bundle, sender=PERSON, generation_date=GENERATED, history_insights=composed,
                                   fingerprints=fingerprints)
    return bundle, composed, fingerprints, value


def test_a_render_input_is_built_from_the_bundle_with_the_supplied_values(made):
    bundle, composed, fingerprints, value = made
    assert type(value) is ri.ClientRenderInput and value.generation_date == GENERATED and value.fingerprints == fingerprints
    assert value.client.name == bundle.client_content.client.name and len(value.keywords) == len(bundle.client_content.ranking_rows)
    assert value.sender.name == "Sample Sender" and value.sender.website_url == "https://sender.example"
    assert [s.text for s in value.historical_statements] == [s.text for s in composed.client_statements]
    assert (value.operational.client_key, value.operational.snapshot_content_hash) == (
        bundle.snapshot.client_key, bundle.snapshot.content_hash)
    assert value.operational.run_id is None and value.operational.revision is None  # unknown before persistence


def test_the_visuals_come_from_the_existing_extraction_with_all_rows(made):
    bundle, _, _, value = made
    visuals = vx.extract_visuals(bundle.snapshot)
    assert (value.search_console, value.geography, value.ga4) == (visuals.search_console, visuals.geography, visuals.ga4)


def test_construction_is_deterministic_and_leaves_its_inputs_unchanged(env):
    bundle, composed, _ = trv.pipeline(env)
    fingerprints = idn.measure_render_fingerprints(logo_path=LOGO, font_paths=FONTS)
    before = (bundle.client_content, composed.client_statements)
    first = idn.build_render_input(bundle, sender=PERSON, generation_date=GENERATED, history_insights=composed, fingerprints=fingerprints)
    second = idn.build_render_input(bundle, sender=PERSON, generation_date=GENERATED, history_insights=composed, fingerprints=fingerprints)
    assert first == second and rh.render_input_sha256(first) == rh.render_input_sha256(second)
    assert before == (bundle.client_content, composed.client_statements)


def test_without_history_insights_no_statements_are_displayed(env):
    bundle, _, _ = trv.pipeline(env, history=False)
    fingerprints = idn.measure_render_fingerprints(logo_path=LOGO, font_paths=FONTS)
    value = idn.build_render_input(bundle, sender=PERSON, generation_date=GENERATED, history_insights=None, fingerprints=fingerprints)
    assert value.historical_statements == ()


def test_the_generation_date_is_exactly_the_supplied_date_and_changes_the_hash(made):
    bundle, composed, fingerprints, value = made
    other = idn.build_render_input(bundle, sender=PERSON, generation_date=GENERATED + dt.timedelta(days=1),
                                   history_insights=composed, fingerprints=fingerprints)
    assert other.generation_date == dt.date(2026, 10, 31) and rh.render_input_sha256(other) != rh.render_input_sha256(value)


def test_construction_checks_its_arguments(made):
    bundle, composed, fingerprints, _ = made
    with pytest.raises(TypeError):
        idn.build_render_input({"x": 1}, sender=PERSON, generation_date=GENERATED, history_insights=None, fingerprints=fingerprints)
    with pytest.raises(ri.RenderInputError):
        idn.build_render_input(bundle, sender="Jimmy", generation_date=GENERATED, history_insights=None, fingerprints=fingerprints)
    with pytest.raises(ri.RenderInputError):
        idn.build_render_input(bundle, sender=PERSON, generation_date="2026-10-30", history_insights=None, fingerprints=fingerprints)


def test_a_matching_bundle_is_accepted(made):
    bundle, _, _, value = made
    assert idn.check_matches_bundle(value, bundle) is None
    assert idn.check_matches_bundle(dataclasses.replace(value, operational=dataclasses.replace(
        value.operational, run_id="0198c1a2-7b3c-7d4e-8f5a-1234567890ab", revision=3)), bundle) is None  # run identity is not compared


def mismatch(value, bundle):
    with pytest.raises(idn.RenderIdentityError) as caught:
        idn.check_matches_bundle(value, bundle)
    return caught.value


def test_every_kind_of_bundle_mismatch_is_rejected_with_a_fixed_code(made):
    bundle, _, _, value = made
    secret = "SECRET-VALUE-XYZ"
    cases = {
        "client": dataclasses.replace(value, client=ri.RenderClient(secret, value.client.website)),
        "client#website": dataclasses.replace(value, client=ri.RenderClient(value.client.name, "https://" + secret + ".example")),
        "periods": dataclasses.replace(value, comparison_period=ri.RenderPeriod(dt.date(2020, 1, 1), dt.date(2020, 1, 2))),
        "keywords": dataclasses.replace(value, keywords=value.keywords[:-1]),
        "keywords#row": dataclasses.replace(value, keywords=(dataclasses.replace(value.keywords[0], keyword=secret),) + value.keywords[1:]),
        "ranking_footnote": dataclasses.replace(value, ranking_footnote=secret),
        "sections": dataclasses.replace(value, sections=(dataclasses.replace(value.sections[0], paragraphs=(secret,)),) + value.sections[1:]),
        "recommendations": dataclasses.replace(value, recommendations=value.recommendations + (ri.RenderRecommendation(secret, "a"),)),
    }
    for label, changed in cases.items():
        error = mismatch(changed, bundle)
        assert (error.code, error.group) == ("render_input_mismatch", label.split("#")[0])
        assert secret not in str(error) + repr(vars(error)) and bundle.client_content.client.name not in str(error)


def test_snapshot_identity_mismatches_are_rejected(made):
    bundle, _, _, value = made
    assert mismatch(dataclasses.replace(value, operational=dataclasses.replace(value.operational, snapshot_content_hash="e" * 64)),
                    bundle).group == "snapshot"
    assert mismatch(dataclasses.replace(value, operational=dataclasses.replace(value.operational, client_key="other-client")),
                    bundle).group == "snapshot"
    assert mismatch(dataclasses.replace(value, operational=ri.RenderOperationalMetadata()), bundle).code == "snapshot_identity_missing"
    moved = dataclasses.replace(value, period=ri.RenderPeriod(value.period.start - dt.timedelta(days=1), value.period.end))
    assert mismatch(moved, bundle).group == "snapshot_period"
    other_bundle = dataclasses.replace(bundle, snapshot=dataclasses.replace(bundle.snapshot, content_hash="d" * 64))
    assert mismatch(value, other_bundle).group == "snapshot"


def test_bundle_check_arguments_and_determinism(made):
    bundle, _, _, value = made
    with pytest.raises(TypeError):
        idn.check_matches_bundle({"x": 1}, bundle)
    with pytest.raises(TypeError):
        idn.check_matches_bundle(value, "bundle")
    changed = dataclasses.replace(value, ranking_footnote="different")
    assert str(mismatch(changed, bundle)) == str(mismatch(changed, bundle))


def test_values_that_do_not_come_from_the_bundle_are_not_compared(made):
    bundle, _, _, value = made
    other = dataclasses.replace(value, generation_date=dt.date(2020, 1, 1), sender=ri.RenderSender("Someone Else"),
                                historical_statements=(), search_console=ri.SearchConsoleVisuals.unavailable())
    assert idn.check_matches_bundle(other, bundle) is None


# --- one input: renderer, hash and HTML -------------------------------------------------------------------------------------------------------

@pytest.fixture(scope="module")
def rendered(tmp_path_factory):
    fingerprints = idn.measure_render_fingerprints(logo_path=LOGO, font_paths=FONTS)
    value = dataclasses.replace(sample(), fingerprints=fingerprints)
    sources = tuple(pr.FontSource(name, path, weight="700" if "Bold" in name else "400") for name, path in FONTS.items())
    try:
        outcome = pr.render_report_pdf(value, workdir=tmp_path_factory.mktemp("identity"), logo_path=LOGO, fonts=sources)
    except Exception as err:
        if type(err).__module__.startswith("playwright"):
            pytest.skip("Chromium could not be started")
        raise
    return value, outcome


def test_the_renderer_reports_the_hash_of_the_exact_object_it_rendered(rendered):
    value, outcome = rendered
    assert outcome.render_input_sha256 == rh.render_input_sha256(value)
    assert re.fullmatch(r"[0-9a-f]{64}", outcome.render_input_sha256) and outcome.ok
    assert outcome.pdf_sha256 != outcome.render_input_sha256  # two different identities: the input and the bytes


def test_the_rendered_html_is_a_function_of_that_object_alone(rendered):
    value, outcome = rendered
    html = outcome.html_path.read_text(encoding="utf-8")
    assert "Sample Roofing Co" in html and "30th Oct, 2026" in html and "Sample Sender" in html
    assert 'href="fonts/DejaVuSans.ttf"' not in html  # font files are referenced from the stylesheet only
    operational_changed = dataclasses.replace(value, operational=ri.RenderOperationalMetadata("other", "run-x", 9, "f" * 64))
    assert rh.render_input_sha256(operational_changed) == outcome.render_input_sha256  # operational data is not hashed
    import inspect
    assert list(inspect.signature(rd.render_report_document).parameters) == ["render_input", "assets"]


def test_a_changed_input_changes_the_hash_the_renderer_would_report(rendered):
    value, outcome = rendered
    for changed in (dataclasses.replace(value, generation_date=dt.date(2026, 10, 31)),
                    dataclasses.replace(value, client=ri.RenderClient("Another Co", value.client.website)),
                    dataclasses.replace(value, fingerprints=dataclasses.replace(value.fingerprints, template_version="t-0000000000000000"))):
        assert rh.render_input_sha256(changed) != outcome.render_input_sha256


# --- hidden fields -----------------------------------------------------------------------------------------------------------------------------

def html_of(value):
    return rd.render_report_document(value, ASSETS)


def test_the_hidden_fields_are_documented_with_a_reason_each():
    assert sorted(idn.HIDDEN_FIELDS) == sorted([
        "ClientRenderInput.comparison_period", "RenderPeriod (period)", "RenderStatement.key", "RenderStatement.group",
        "RenderRanking.text (kinds other than above_10)", "RenderSender.website_url", "RenderSection.title (keywords)",
        "RenderSection.paragraphs (status not ok)", "RenderFingerprints (all)"])
    assert all(len(reason) > 30 for reason in idn.HIDDEN_FIELDS.values())


def test_comparison_period_is_hashed_and_never_drawn():
    base = full()
    changed = dataclasses.replace(base, comparison_period=ri.RenderPeriod(dt.date(2020, 1, 1), dt.date(2020, 1, 2)))
    assert rh.render_input_sha256(changed) != rh.render_input_sha256(base) and html_of(changed) == html_of(base)


def test_the_period_matters_to_the_drawing_only_beyond_the_records():
    base = full()
    last = base.search_console.daily[-1].day
    inside = dataclasses.replace(base, period=ri.RenderPeriod(base.period.start + dt.timedelta(days=1), base.period.end))
    assert rh.render_input_sha256(inside) != rh.render_input_sha256(base) and html_of(inside) == html_of(base)
    beyond = dataclasses.replace(base, period=ri.RenderPeriod(base.period.start, last + dt.timedelta(days=40)))
    assert html_of(beyond) != html_of(base)  # an end after the last record widens the chart axis


def test_statement_key_and_group_are_hashed_and_never_drawn():
    base = full()
    first = base.historical_statements[0]
    for field in ("key", "group"):
        changed = dataclasses.replace(base, historical_statements=(dataclasses.replace(first, **{field: first.key + "x"}),)
                                                                  + base.historical_statements[1:])
        assert rh.render_input_sha256(changed) != rh.render_input_sha256(base) and html_of(changed) == html_of(base)


def test_ranking_text_is_drawn_only_for_above_10():
    base = full(keywords=(ri.RenderKeywordRow("k a", "L", ri.RenderRanking("above_10", None, "Above 10"), ri.RenderRanking("position", 3, None), None),
                          ri.RenderKeywordRow("k b", "L", ri.RenderRanking("missing"), ri.RenderRanking("position", 4), None)))
    shown = dataclasses.replace(base, keywords=(dataclasses.replace(base.keywords[0], previous=ri.RenderRanking("above_10", None, "Above 10+")),)
                                                + base.keywords[1:])
    hidden = dataclasses.replace(base, keywords=(dataclasses.replace(base.keywords[0], current=ri.RenderRanking("position", 3, "free text")),)
                                                + base.keywords[1:])
    assert html_of(shown) != html_of(base) and "Above 10+" in html_of(shown)
    assert html_of(hidden) == html_of(base) and rh.render_input_sha256(hidden) != rh.render_input_sha256(base)


def test_the_sender_website_url_is_drawn_only_as_a_safe_link_target():
    base = full(sender=dataclasses.replace(sample().sender, website_url="https://jasper.example"))
    linked = dataclasses.replace(base, sender=dataclasses.replace(base.sender, website_url="https://jasper.example/x"))
    unsafe = dataclasses.replace(base, sender=dataclasses.replace(base.sender, website_url="javascript:alert(1)"))
    assert html_of(linked) != html_of(base) and 'href="https://jasper.example/x"' in html_of(linked)
    assert "javascript" not in html_of(unsafe)
    for changed in (linked, unsafe):
        assert rh.render_input_sha256(changed) != rh.render_input_sha256(base)


def test_the_keyword_section_title_is_hashed_and_never_drawn_but_other_titles_are():
    base = full()
    keywords_title = dataclasses.replace(base, sections=(dataclasses.replace(base.sections[0], title="Renamed"),) + base.sections[1:])
    assert rh.render_input_sha256(keywords_title) != rh.render_input_sha256(base) and html_of(keywords_title) == html_of(base)
    search_title = dataclasses.replace(base, sections=base.sections[:1] + (dataclasses.replace(base.sections[1], title="Renamed"),) + base.sections[2:])
    assert html_of(search_title) != html_of(base) and 'section__heading">Renamed<' in html_of(search_title)


def test_paragraphs_of_a_section_that_is_not_ok_are_hashed_and_never_drawn():
    base = full()
    off = dataclasses.replace(base.sections[1], status="unavailable", paragraphs=("Hidden by status.",))
    value = dataclasses.replace(base, sections=base.sections[:1] + (off,) + base.sections[2:])
    changed = dataclasses.replace(base, sections=base.sections[:1] + (dataclasses.replace(off, paragraphs=("Other text.",)),) + base.sections[2:])
    assert "Hidden by status." not in html_of(value) and html_of(changed) == html_of(value)
    assert rh.render_input_sha256(changed) != rh.render_input_sha256(value)


def test_fingerprints_are_hashed_and_never_drawn():
    base = full()
    for fingerprints in (dataclasses.replace(base.fingerprints, template_version="t-aaaaaaaaaaaaaaaa"),
                         dataclasses.replace(base.fingerprints, logo_sha256="9" * 64),
                         dataclasses.replace(base.fingerprints, fonts=(ri.FontFingerprint("Other", "8" * 64),))):
        changed = dataclasses.replace(base, fingerprints=fingerprints)
        assert rh.render_input_sha256(changed) != rh.render_input_sha256(base) and html_of(changed) == html_of(base)


def test_operational_metadata_changes_neither_the_hash_nor_the_html():
    base = full()
    changed = dataclasses.replace(base, operational=ri.RenderOperationalMetadata("x-client", "run-1", 5, "c" * 64))
    assert rh.render_input_sha256(changed) == rh.render_input_sha256(base) and html_of(changed) == html_of(base)


# --- the coverage check: every field the HTML depends on is in the hash, every hidden field is documented -------------------------------------------

def coverage_base():
    sections = (ri.RenderSection("keywords", "Title KW", "ok", ("KW paragraph.",)),
                ri.RenderSection("search_console", "Title SC", "ok", ("SC paragraph.",)),
                ri.RenderSection("geography", "Title Geo", "ok", ("Geo paragraph.",)),
                ri.RenderSection("ga4", "Title GA", "ok", ("GA paragraph.",)))
    keywords = (ri.RenderKeywordRow("k one", "Loc", ri.RenderRanking("above_10", None, "Above 10"), ri.RenderRanking("position", 3), 2),
                ri.RenderKeywordRow("k two", "", ri.RenderRanking("missing"), ri.RenderRanking("position", 8), None))
    from test_report_sections import ga4_visual, geo_visual, sc_visual
    return full(keywords=keywords, sections=sections, sender=dataclasses.replace(sample().sender, website_url="https://jasper.example"),
                search_console=sc_visual(n=5), geography=geo_visual(4), ga4=ga4_visual(n=5),
                recommendations=(ri.RenderRecommendation("Rec", "Act."),))


def hidden_path(base, path):
    """Whether the documented rules say a change at this path cannot change the drawing."""
    head = path[0]
    if head in ("comparison_period", "fingerprints"):
        return True
    if head == "period":
        return path[-1] == "start"
    if head == "historical_statements":
        return path[-1] in ("key", "group")
    if head == "sections":
        return len(path) == 3 and path[1] == 0 and path[2] == "title"
    if head == "keywords" and path[-1] == "text":
        return getattr(base.keywords[path[1]], path[2]).kind != "above_10"
    return False


def test_every_field_the_pages_depend_on_is_hashed_and_every_hidden_field_is_documented():
    base = coverage_base()
    base_html, base_hash = html_of(base), rh.render_input_sha256(base)
    shown, hidden, checked, per_key = [], [], 0, {}
    for path, cls, field, value in walk(base):
        if rh.FIELD_CLASSES[cls][field] != rh.INCLUDED or dataclasses.is_dataclass(value):
            continue
        per_key[(cls, field)] = per_key.get((cls, field), 0) + 1
        if per_key[(cls, field)] > 4:  # the first four occurrences of every field cover each distinct situation
            continue
        for candidate in alternatives(value):
            try:
                changed = set_at(base, path, candidate)
            except ri.RenderInputError:
                continue
            if changed == base:
                continue
            try:
                html = html_of(changed)
            except Exception:  # a change that makes the content unplaceable is a visible effect, not a hidden one
                html = None
            checked += 1
            drawn = html != base_html
            if drawn:
                assert rh.render_input_sha256(changed) != base_hash, path  # what is drawn is always hashed
                assert not hidden_path(base, path), f"documented as hidden but drawn: {path}"
                shown.append(path)
            else:
                assert hidden_path(base, path), f"changes the hash, not the pages, and is not documented: {path}"
                hidden.append(path)
            break
    assert checked > 70 and len(shown) > 50 and len(hidden) >= 10
    assert {(p[0], p[-1]) for p in hidden} >= {("comparison_period", "start"), ("historical_statements", "key"),
                                               ("historical_statements", "group"), ("sections", "title")}
    assert {p[0] for p in hidden} <= {"comparison_period", "fingerprints", "period", "historical_statements", "sections", "keywords"}


def test_the_hash_and_pages_depend_only_on_the_render_input_not_on_the_bundle_it_came_from(made):
    bundle, _, _, value = made
    again = dataclasses.replace(value)
    assert html_of(again) == html_of(value) and rh.render_input_sha256(again) == rh.render_input_sha256(value)


# --- purity and scope ----------------------------------------------------------------------------------------------------------------------------

def test_the_identity_module_reads_files_but_writes_none_and_uses_no_clock_or_network(tmp_path):
    source = (ROOT / "src" / "seo_report" / "render_identity.py").read_text(encoding="utf-8")
    code = re.sub(r'""".*?"""', "", source, flags=re.S)
    for word in ("datetime.now", "date.today", "time.time", "random", "uuid", "os.environ", "open(", "write_", ".write(",
                 "mkdir", "unlink", "shutil", "requests", "urllib", "socket", "subprocess", "sqlite", "playwright",
                 "report_finalize", "record_schema", "record_writer", "reviewer_record", "report_promotion",
                 "finalization_audit", "client_content_hash"):
        assert word not in code, word
    fp(tmp_path)  # builds the small tree, then reads it
    after = {p.relative_to(tmp_path).as_posix(): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    fp(tmp_path)
    assert after == {p.relative_to(tmp_path).as_posix(): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}


def test_the_client_content_hash_is_untouched():
    from seo_report import client_content_hash
    assert callable(client_content_hash.client_content_sha256)
