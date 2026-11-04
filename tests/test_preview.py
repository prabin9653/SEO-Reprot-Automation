"""R8.7h.2: `seo-report report preview`, tested OFFLINE.

No Google call is possible (sockets and the sign-in flow are forbidden by the autouse guard imported from test_attestation; the workbook, Search Console and GA4
are fakes). Chromium is real in the rendering tests, but only on synthetic data in temporary folders. No production state exists in these tests: every
location is a temporary folder, and tripwires fail the test if a ledger, a history write, a finalization or a lock is attempted. The command has NEVER been
run against real data (that is R8.7h.3).
"""
import dataclasses
import datetime as dt
import errno
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from seo_report import cli, client_loader, contracts, ledger, pipeline, preview, render_artifacts
from seo_report import pdf_renderer as pr
from seo_report import pdf_validation as pv
from seo_report import report_finalize
from seo_report.cli_exit import Exit
from seo_report.snapshots import store as snapshot_store
from test_attestation import GATE_SEAMS, client_evidence, evidence, issue, nothing_leaves_the_machine, run_cli  # noqa: F401  (the autouse guard is imported on purpose)
from test_pdf_renderer import LOGO, SOURCES, render, with_fingerprints
from test_pipeline import default_collect
from test_r86d_report_status_recovery import PERIOD, PROBES_OK, RUN, loaded, tree
from test_r86d_report_status_recovery import home as base_home
from test_render_artifacts import StubRenderer
from test_render_input import sample

UTC = dt.timezone.utc
PREVIEW = ["report", "preview", "--client", "alpha-co", "--period", PERIOD]


def now():
    return dt.datetime.now(UTC).replace(microsecond=0)


@pytest.fixture
def home(base_home):
    """The synthetic home of the other stages, but with a SHORT `%LOCALAPPDATA%` (a short temporary folder). The pipeline refuses file names that would pass the
    Windows 259-character path limit, and pytest's own temporary folders are long; the real `%LOCALAPPDATA%` is short enough. The production folders of the
    settings stay where they were (and stay empty)."""
    short = Path(tempfile.mkdtemp(prefix="jp"))
    base_home.env = {"LOCALAPPDATA": str(short)}
    base_home.short = short
    yield base_home
    shutil.rmtree(short, ignore_errors=True)  # this test's own temporary folder


def attest(home, **kw):
    moment = now()
    return issue(home, now=moment, ev=evidence(home.settings, at=moment - dt.timedelta(minutes=1), **kw))


def previews(home) -> Path:
    return home.short / "JasperSEOReporting" / "previews"


def preview_folders(home):
    return sorted(previews(home).glob("alpha-co/*/preview-*")) if previews(home).exists() else []


def production_state(home):
    """Everything outside the preview folder: the production output/work/history/ledger folders and the attestation."""
    state = {f"tmp/{k}": v for k, v in tree(home.tmp).items()}
    state.update({f"short/{k}": v for k, v in tree(home.short).items() if "previews" not in k.split("\\")})
    return state


@pytest.fixture
def tripwires(monkeypatch):
    """Any production write path fails the test."""
    def forbid(name):
        def trip(*args, **kwargs):
            raise AssertionError(f"production path used by a preview: {name}")
        return trip
    monkeypatch.setattr(ledger.Ledger, "__init__", forbid("Ledger"))
    monkeypatch.setattr(pipeline, "run_client", forbid("pipeline.run_client"))
    monkeypatch.setattr(report_finalize, "finalize_report", forbid("finalize_report"))
    for name in ("write", "add_event"):
        monkeypatch.setattr(snapshot_store.SnapshotStore, name, forbid("SnapshotStore." + name))
    from seo_report import runner
    monkeypatch.setattr(runner, "run_batch", forbid("run_batch"))


def result_of(code, capsys):
    out = capsys.readouterr().out
    return code, (json.loads(out) if out.strip() else {})


def go(home, monkeypatch, capsys, *, argv=None, **kw):
    kw.setdefault("result", loaded("Alpha Co"))
    return result_of(run_cli(home, monkeypatch, argv or PREVIEW, **kw), capsys)


# === the command line ===============================================================================================================

def test_the_help_is_offline_and_lists_the_command(monkeypatch, capsys):
    monkeypatch.setattr(pipeline, "dry_run_client", lambda *a, **k: (_ for _ in ()).throw(AssertionError("the preview must not run for help")))
    monkeypatch.setattr(client_loader, "load_from_workbook", lambda *a, **k: (_ for _ in ()).throw(AssertionError("Google must not be read for help")))
    assert ("report", "preview") in cli.default_handlers()
    text = ""
    for argv in (["report", "--help"], ["report", "preview", "--help"]):
        assert cli.main(argv, environ={}) == 0
        text += capsys.readouterr().out
    assert "preview" in text and "NOT FINAL" in text and "--client" in text and "--period" in text and "--production" not in text.split("preview")[-1]


def test_client_and_period_are_required_and_validated(home, capsys):
    base = ["report", "preview", "--config", str(home.path)]
    assert cli.main(base + ["--period", PERIOD], environ=home.env) == Exit.USAGE  # no client
    assert cli.main(base + ["--client", "alpha-co"], environ=home.env) == Exit.USAGE  # no period
    assert cli.main(base + ["--client", "alpha-co", "--period", "2026-01-01_2026-01-10"], environ=home.env) == Exit.USAGE  # not 28 days
    assert cli.main(base + ["--client", "Bad Key", "--period", PERIOD], environ=home.env) == Exit.USAGE
    for extra in (["--production"], ["--force"], ["--output", "x"], ["--scratch-dir", "x"]):
        assert cli.main(base + ["--client", "alpha-co", "--period", PERIOD] + extra, environ=home.env) == Exit.USAGE  # no option moves or confirms anything
    assert cli.main(["report", "preview", "--client", "alpha-co", "--period", PERIOD], environ={}) == Exit.CONFIG_INVALID  # no settings file: before any Google
    assert not previews(home).exists()


def test_an_unregistered_client_is_rejected_before_google(home, monkeypatch, capsys):
    monkeypatch.setattr(client_loader, "load_from_workbook", lambda *a, **k: (_ for _ in ()).throw(AssertionError("Google was read")))
    attest(home)
    code, data = go(home, monkeypatch, capsys, argv=["report", "preview", "--client", "zeta-co", "--period", PERIOD], result=None)
    assert code == Exit.CONFIG_INVALID and data["error"]["code"] == "client_unknown" and not previews(home).exists()


def test_a_client_that_is_not_eligible_in_the_workbook_is_rejected(home, monkeypatch, capsys):
    attest(home)
    code, data = go(home, monkeypatch, capsys, result=loaded())  # the workbook yields no runnable client
    assert code == Exit.CONFIG_INVALID and data["error"]["code"] == "client_not_eligible" and not previews(home).exists()


def test_a_period_that_differs_from_the_workbook_is_rejected(home, monkeypatch, capsys):
    attest(home)
    code, data = go(home, monkeypatch, capsys, argv=["report", "preview", "--client", "alpha-co", "--period", "2026-10-27_2026-11-23"])
    assert code == Exit.CONFIG_INVALID and data["error"]["code"] == "period_differs_from_workbook" and not previews(home).exists()


def test_without_a_valid_attestation_nothing_is_read_or_rendered(home, monkeypatch, capsys):
    monkeypatch.setattr(client_loader, "load_from_workbook", lambda *a, **k: (_ for _ in ()).throw(AssertionError("Google was read")))
    code, data = result_of(run_cli(home, monkeypatch, PREVIEW), capsys)  # no attestation exists
    assert code == Exit.PREFLIGHT_FAILED and data["details"]["blockers"] == ["live_google_not_verified"] and not previews(home).exists()
    monkeypatch.undo()
    attest(home)
    code, data = go(home, monkeypatch, capsys)
    assert code == Exit.OK  # with one it works


def test_an_expired_attestation_is_refused(home, monkeypatch, capsys):
    monkeypatch.setattr(client_loader, "load_from_workbook", lambda *a, **k: (_ for _ in ()).throw(AssertionError("Google was read")))
    attest(home)
    later = now() + dt.timedelta(days=8)
    code = cli.main(["--json", *PREVIEW, "--config", str(home.path)], environ=home.env, gate=None, preflight_probes={**PROBES_OK, **GATE_SEAMS},
                    clock=lambda: later, client_factory=None)
    data = json.loads(capsys.readouterr().out)
    assert code == Exit.PREFLIGHT_FAILED and "attestation_expired" in data["details"]["blockers"] and not previews(home).exists()


def test_a_client_not_covered_by_the_attestation_is_refused(home, monkeypatch, capsys):
    attest(home, clients=[client_evidence("Beta Co")])
    code, data = go(home, monkeypatch, capsys)
    assert code == Exit.PREFLIGHT_FAILED and data["details"]["blockers"] == ["attestation_client_not_covered"] and not previews(home).exists()


def test_a_changed_property_is_refused(home, monkeypatch, capsys):
    attest(home)
    changed = loaded("Alpha Co")
    from test_pipeline import make_client
    changed.clients["alpha-co"] = dataclasses.replace(changed.clients["alpha-co"], client=make_client("Alpha Co", gsc_property="https://other.test/"))
    code, data = go(home, monkeypatch, capsys, result=changed)
    assert code == Exit.PREFLIGHT_FAILED and data["details"]["blockers"] == ["attestation_property_changed"] and not previews(home).exists()


def test_missing_chromium_or_a_failing_launch_is_refused(home, monkeypatch, capsys):
    attest(home)
    code, data = go(home, monkeypatch, capsys, probes={"chromium_folders": lambda env: []})
    assert code == Exit.PREFLIGHT_FAILED and "chromium_missing" in data["details"]["blockers"] and not previews(home).exists()
    code, data = go(home, monkeypatch, capsys, probes={"launch_probe": lambda env: {"launched": False, "code": "chromium_launch_failed"}})
    assert code == Exit.PREFLIGHT_FAILED and not previews(home).exists()


# === success and isolation ==============================================================================================================

def test_a_preview_writes_only_its_own_folder_and_touches_no_production_state(home, monkeypatch, capsys, tripwires):
    attest(home)
    before = production_state(home)
    code, data = go(home, monkeypatch, capsys)
    assert code == Exit.OK and data["status"] == "preview_only" and data["notice"] == "PREVIEW ONLY — NO PRODUCTION REPORT CREATED"
    assert production_state(home) == before  # not one production file, folder or attestation byte changed
    assert not home.ledger_path.exists() and not (home.local / "history" / "h.sqlite").exists()
    assert list((home.local / "output").iterdir()) == [] and list((home.local / "work").iterdir()) == []  # no production folder was created
    folders = preview_folders(home)
    assert len(folders) == 1 and sorted(p.name for p in folders[0].iterdir()) == ["preview-manifest.json", "preview.pdf", "render"]


def test_the_result_and_the_manifest_are_complete_and_free_of_sensitive_data(home, monkeypatch, capsys):
    attest(home)
    code, data = go(home, monkeypatch, capsys)
    assert code == Exit.OK
    folder = preview_folders(home)[0]
    manifest = json.loads((folder / "preview-manifest.json").read_text(encoding="utf-8"))
    pdf = (folder / "preview.pdf").read_bytes()
    assert manifest["status"] == "preview_only" and manifest["schema"] == "jasper.report-preview" and manifest["label"] == pr.PREVIEW_LABEL
    assert manifest["client_key"] == "alpha-co" and manifest["period_id"] == PERIOD and manifest["created_at"].endswith("+00:00")
    assert manifest["pdf"]["sha256"] == hashlib.sha256(pdf).hexdigest() == data["pdf_sha256"] and manifest["pdf"]["bytes"] == len(pdf) == data["pdf_bytes"]
    assert manifest["validation"]["result"] == "passed" and manifest["notice"] == preview.NOTICE
    assert data["preview_id"] == manifest["preview_id"] and folder.name == f"preview-{data['preview_id']}"
    assert data["preview_pdf"].startswith("%LOCALAPPDATA%\\JasperSEOReporting\\previews\\alpha-co\\" + PERIOD) and "preview.pdf" in data["preview_pdf"]
    text = json.dumps(manifest) + json.dumps(data)
    for sensitive in ("https://example.test/", "123456789", home.settings.reporting_spreadsheet_id, "sender@example.test", str(home.tmp), "token", "secret"):
        assert sensitive not in text, sensitive


def test_the_text_output_carries_the_notice_and_the_facts(home, monkeypatch, capsys):
    attest(home)
    monkeypatch.setattr(client_loader, "load_from_workbook", lambda *a, **k: loaded("Alpha Co"))
    code = cli.main(PREVIEW + ["--config", str(home.path)], environ=home.env, gate=None, preflight_probes={**PROBES_OK, **GATE_SEAMS},
                    services_options={"renderer": StubRenderer(), "collect": default_collect, "sleep": lambda s: None}, clock=lambda: now(), client_factory=None)
    out = capsys.readouterr().out
    assert code == Exit.OK and out.count("PREVIEW ONLY — NO PRODUCTION REPORT CREATED") == 2
    for fact in ("Client alpha-co, period " + PERIOD, "Preview ID: ", "Preview PDF: ", "Pages: ", "Validation: passed", "SHA-256: "):
        assert fact in out


def test_two_previews_never_share_or_overwrite_a_folder(home, monkeypatch, capsys, tripwires):
    attest(home)
    first = go(home, monkeypatch, capsys)[1]
    marker = preview_folders(home)[0] / "preview.pdf"
    before = marker.read_bytes()
    second = go(home, monkeypatch, capsys)[1]
    assert first["preview_id"] != second["preview_id"] and len(preview_folders(home)) == 2 and marker.read_bytes() == before


def test_an_existing_preview_folder_is_never_overwritten(home, monkeypatch, capsys):
    attest(home)
    fixed = dt.datetime(2026, 11, 2, 12, 0, 0, tzinfo=UTC)
    monkeypatch.setattr(preview.uuid, "uuid4", lambda: type("U", (), {"hex": "abcdef0123456789"})())
    existing = previews(home) / "alpha-co" / PERIOD / "preview-20261102T120000Z-abcdef01"
    existing.mkdir(parents=True)
    (existing / "keep.txt").write_text("precious", encoding="utf-8")
    with pytest.raises(preview.PreviewFailed) as caught:
        preview.run_preview(settings=home.settings, environ=home.env, services=None, client_key="alpha-co", period_id=PERIOD,
                            renderer=preview.PreviewRenderer(home.settings, inner=StubRenderer()), clock=lambda: fixed)
    assert (caught.value.stage, caught.value.code, caught.value.exit_code) == ("storage", "preview_id_collision", Exit.LEDGER_UNAVAILABLE)
    assert [p.name for p in existing.iterdir()] == ["keep.txt"] and (existing / "keep.txt").read_text(encoding="utf-8") == "precious"


def test_unsafe_preview_locations_are_refused(home, tmp_path):
    for environ, label in (({}, "no LOCALAPPDATA"), ({"LOCALAPPDATA": str(ROOT / "zz_unused")}, "inside the project"),
                           ({"LOCALAPPDATA": str(tmp_path / "OneDrive" / "local")}, "inside OneDrive"), ({"LOCALAPPDATA": "relative"}, "relative")):
        with pytest.raises(preview.PreviewFailed) as caught:
            preview.previews_root(environ)
        assert caught.value.code == "preview_location_unsafe", label
    assert not (ROOT / "zz_unused").exists()
    inside_output = dataclasses.replace(home.settings, output_root=previews(home) / "alpha-co")
    with pytest.raises(preview.PreviewFailed) as caught:
        preview.run_preview(settings=inside_output, environ=home.env, services=None, client_key="alpha-co", period_id=PERIOD,
                            renderer=preview.PreviewRenderer(home.settings, inner=StubRenderer()))
    assert caught.value.code == "preview_location_unsafe" and not previews(home).exists()


def test_the_preview_never_runs_any_production_path(home, monkeypatch, capsys, tripwires):
    """The tripwires raise on a ledger, a lock, a history write, a finalization and the batch runner; the run still succeeds."""
    attest(home)
    assert go(home, monkeypatch, capsys)[0] == Exit.OK
    from seo_report import preview as module
    source = Path(module.__file__).read_text(encoding="utf-8").split('"""', 2)[2]
    handler = (ROOT / "src" / "seo_report" / "cli_report.py").read_text(encoding="utf-8").split("def handle_preview")[1].split("# --- report run")[0]
    for text in (source, handler):
        for word in ("Ledger(", "open_ledger", "acquire_lock", "SnapshotStore", "finalize_report", "register_request", "smtplib", "webhook"):
            assert word not in text, word
        assert re.search(r"(?<![a-z_])run_client\(", text) is None  # only the dry run (`dry_run_client`) is used


# === failures ===========================================================================================================================

def folder_of(home, data):
    """The folder named in a failure's details (two previews started within one second sort arbitrarily by name)."""
    return Path(data["details"]["preview_folder"].replace("%LOCALAPPDATA%", str(home.short)))


def manifest_of(home):
    folder = preview_folders(home)[0]
    return folder, json.loads((folder / "preview-manifest.json").read_text(encoding="utf-8"))


def test_workbook_failures_stop_before_any_preview_folder(home, monkeypatch, capsys):
    attest(home)
    for category, exit_code in (("auth_required", Exit.AUTH_REQUIRED), ("source_denied", Exit.SOURCE_UNAVAILABLE)):
        def broken(*a, **k):
            raise client_loader.WorkbookUnavailable(category, "synthetic")
        monkeypatch.setattr(client_loader, "load_from_workbook", broken)
        code = run_cli(home, monkeypatch, PREVIEW)
        capsys.readouterr()
        assert code == exit_code and not previews(home).exists()


def test_search_console_and_ga4_failures_end_in_a_failed_manifest_and_no_pdf(home, monkeypatch, capsys, tripwires):
    attest(home)
    from seo_report import source_collector as sc
    for source, code_name, category, exit_code in (("gsc", "gsc_property_not_accessible", "source_denied", Exit.SOURCE_UNAVAILABLE),
                                                   ("ga4", "ga4_http_403", "source_denied", Exit.SOURCE_UNAVAILABLE),
                                                   ("gsc", "gsc_http_503", "source_temporary", Exit.SOURCE_UNAVAILABLE)):
        def collect(client_run, today, s=source, c=code_name, cat=category):
            return sc.SourceResult(None, None, failures=(sc.SourceFailure(s, cat, c),))
        before = len(preview_folders(home))
        code, data = go(home, monkeypatch, capsys, options={"collect": collect})
        assert code == exit_code and data["error"]["code"] == code_name and data["details"]["production_report_created"] is False
        folder = folder_of(home, data)
        manifest = json.loads((folder / "preview-manifest.json").read_text(encoding="utf-8"))
        assert len(preview_folders(home)) == before + 1 and manifest["status"] == "failed" and manifest["failure"]["code"] == code_name
        assert not (folder / "preview.pdf").exists()


def test_incomplete_source_data_is_a_visible_warning_not_a_failure(home, monkeypatch, capsys):
    attest(home)
    from seo_report import source_collector as sc

    def collect(client_run, today):
        result = default_collect(client_run, today)
        return dataclasses.replace(result, warnings=tuple(result.warnings) + (sc.SourceWarning("gsc", "source_incomplete"),))
    code, data = go(home, monkeypatch, capsys, options={"collect": collect})
    assert code == Exit.OK and "source_incomplete" in data["warnings"]
    folder, manifest = manifest_of(home)
    assert manifest["status"] == "preview_only" and "source_incomplete" in manifest["warnings"]


def test_rendering_and_validation_failures_leave_no_preview_pdf(home, monkeypatch, capsys):
    attest(home)
    issue_item = pv.LayoutIssue(2, "vertical_overflow", "synthetic")
    cases = {"browser failure": (lambda: _Raises(pr.PdfRenderError("browser_failed", "synthetic")), Exit.REPORT_FAILED),
             "layout issues": (lambda: StubRenderer(issues=(issue_item,)), None)}
    for label, (build, expected) in cases.items():
        code, data = go(home, monkeypatch, capsys, options={"renderer": build()})
        assert code != Exit.OK and data["details"]["production_report_created"] is False, label
        if expected:
            assert code == expected, label
        folder = folder_of(home, data)
        assert not (folder / "preview.pdf").exists() and json.loads((folder / "preview-manifest.json").read_text(encoding="utf-8"))["status"] == "failed", label


class _Raises:
    def __init__(self, error):
        self.error = error

    def __call__(self, *a, **k):
        raise self.error


def test_a_disk_write_failure_is_reported_and_leaves_the_folder_marked_failed(home, monkeypatch, capsys):
    attest(home)
    real_open = open

    def full_disk(path, mode="r", *a, **k):
        if str(path).endswith("preview.pdf") and "b" in mode:
            raise OSError(errno.ENOSPC, "No space left on device")
        return real_open(path, mode, *a, **k)
    monkeypatch.setattr("builtins.open", full_disk)
    code, data = go(home, monkeypatch, capsys)
    monkeypatch.undo()
    assert code == Exit.LEDGER_UNAVAILABLE and data["error"]["code"] == "preview_storage_failed"
    folder, manifest = manifest_of(home)
    assert manifest["status"] == "failed" and not (folder / "preview.pdf").exists()


def test_an_interrupted_preview_is_marked_and_keeps_nothing_that_looks_final(home, monkeypatch, capsys):
    attest(home)
    code = run_cli(home, monkeypatch, PREVIEW, result=loaded("Alpha Co"), options={"renderer": _Raises(KeyboardInterrupt())})
    capsys.readouterr()
    assert code == Exit.INTERRUPTED
    folder, manifest = manifest_of(home)
    assert manifest["status"] == "interrupted" and not (folder / "preview.pdf").exists()


def test_a_manifest_that_could_not_be_completed_still_says_incomplete(home, monkeypatch, capsys):
    attest(home)
    real = preview._write_manifest
    calls = []

    def flaky(folder, data, exclusive=False):
        calls.append(data["status"])
        if data["status"] == "preview_only":
            raise OSError(errno.ENOSPC, "full")
        return real(folder, data, exclusive=exclusive)
    monkeypatch.setattr(preview, "_write_manifest", flaky)
    code, data = go(home, monkeypatch, capsys)
    assert code == Exit.LEDGER_UNAVAILABLE and data["error"]["code"] == "preview_storage_failed" and "incomplete" in data["error"]["message"]
    folder, manifest = manifest_of(home)
    assert manifest["status"] == "incomplete" and (folder / "preview.pdf").exists()  # clearly not final


# === the label in real Chromium (synthetic report) ======================================================================================

@pytest.fixture(scope="module")
def renders(tmp_path_factory):
    try:
        production = render(sample(), tmp_path_factory.mktemp("production"))
        labelled = pr.render_report_pdf(with_fingerprints(sample()), workdir=tmp_path_factory.mktemp("labelled"), logo_path=LOGO, fonts=SOURCES,
                                        preview_label=pr.PREVIEW_LABEL)
    except Exception as error:  # the browser is not available here
        if type(error).__module__.startswith("playwright"):
            pytest.skip("Chromium could not be started")
        raise
    return production, labelled


@pytest.fixture(scope="module")
def variants(tmp_path_factory):
    """Further real renders, made at module scope (the browser starts before the per-test network guard): an explicit `preview_label=None`, and three
    deliberately broken labels (hidden, over the body, outside the page) that the measurement must reject."""
    out = {}
    try:
        out["explicit_none"] = pr.render_report_pdf(with_fingerprints(sample()), workdir=tmp_path_factory.mktemp("none"), logo_path=LOGO, fonts=SOURCES,
                                                    preview_label=None)
        original = pr._PREVIEW_CSS
        for name, css in (("hidden", ".preview-watermark { display: none; }"), ("overlapping", ".preview-watermark { top: 60mm; }"),
                          ("outside", ".preview-watermark { top: -40mm; }")):
            with pytest.MonkeyPatch.context() as patch:
                patch.setattr(pr, "_PREVIEW_CSS", original + css)
                out[name] = pr.render_report_pdf(with_fingerprints(sample()), workdir=tmp_path_factory.mktemp(name), logo_path=LOGO, fonts=SOURCES,
                                                 preview_label=pr.PREVIEW_LABEL)
    except Exception as error:
        if type(error).__module__.startswith("playwright"):
            pytest.skip("Chromium could not be started")
        raise
    return out


def test_the_label_is_on_every_page_and_the_preview_validates(renders):
    production, labelled = renders
    pages = labelled.pdf.page_count
    assert labelled.ok and labelled.issues == () and labelled.watermark_pages == pages == 6
    html = labelled.html_path.read_text(encoding="utf-8")
    assert html.count('data-preview-watermark') == pages and html.count(pr.PREVIEW_LABEL) == pages and "PREVIEW" not in production.html_path.read_text(encoding="utf-8")


def test_chromiums_own_font_report_shows_the_label_text_on_every_page(renders):
    production, labelled = renders
    for number, (plain, marked) in enumerate(zip(production.measures, labelled.measures), start=1):
        glyphs = lambda m: sum(font["glyphs"] for font in m["fonts"])  # noqa: E731
        assert glyphs(marked) - glyphs(plain) == len(pr.PREVIEW_LABEL), number  # the label's glyphs were laid out on this page


def test_the_production_render_has_no_label_and_is_unchanged(renders, variants):
    production, labelled = renders
    assert production.watermark_pages == 0 and production.ok and production.pdf_sha256 != labelled.pdf_sha256
    again = variants["explicit_none"]
    assert again.pdf_sha256 == production.pdf_sha256 and again.html_path.read_bytes() == production.html_path.read_bytes()  # an explicit None is the old behaviour


def test_the_label_changes_no_layout_and_the_pdf_stays_valid(renders):
    production, labelled = renders
    for plain, marked in zip(production.measures, labelled.measures):
        for key in ("contentBottom", "contentRight", "contentExtent", "scrollHeight", "tableRows", "svgCount", "textLength", "hasHeader", "hasFooter", "logoLoaded"):
            assert plain[key] == marked[key], key
    info = labelled.pdf
    assert info.page_count == production.pdf.page_count and set(info.page_boxes) == set(production.pdf.page_boxes) and all(pv.is_a4(b) for b in info.page_boxes)
    assert info.xref_consistent and info.ends_with_eof and info.has_image and any("DejaVuSans" in name for name in info.fonts)
    assert labelled.blocked_requests == 0 and [family for family, _, _ in labelled.fonts_used] == ["DejaVu Sans"]  # the expected font and nothing else


@pytest.mark.parametrize("name,code", [("hidden", "preview_label_hidden"), ("overlapping", "preview_label_overlap"), ("outside", "preview_label_outside_page")])
def test_a_missing_hidden_or_overlapping_label_is_never_accepted(variants, name, code):
    outcome = variants[name]
    assert not outcome.ok and outcome.watermark_pages == 0
    assert {i.code for i in outcome.issues if i.code.startswith("preview_label")} == {code}
    assert len([i for i in outcome.issues if i.code == code]) == outcome.pdf.page_count  # reported for every page


def test_the_label_checks_themselves():
    good = {"count": 1, "text": pr.PREVIEW_LABEL, "visible": True, "inside": True, "clear": True}
    assert pr._watermark_issues([good, good], pr.PREVIEW_LABEL, 2) == []
    assert [i.code for i in pr._watermark_issues([good, {**good, "count": 0}], pr.PREVIEW_LABEL, 2)] == ["preview_label_missing"]
    assert [i.code for i in pr._watermark_issues([{**good, "text": "other"}], pr.PREVIEW_LABEL, 1)] == ["preview_label_missing"]
    assert [i.code for i in pr._watermark_issues([{**good, "count": 2}], pr.PREVIEW_LABEL, 1)] == ["preview_label_missing"]
    assert [i.code for i in pr._watermark_issues([good], pr.PREVIEW_LABEL, 2)] == ["preview_label_missing"]  # not measured on every page
    assert pr._watermark_issues("nope", pr.PREVIEW_LABEL, 1)[0].code == "preview_label_missing"
    with pytest.raises(pr.PdfRenderError):
        pr.inject_preview_label("<html><style></style></html>", pr.PREVIEW_LABEL)  # no page to label
    with pytest.raises(pr.PdfRenderError):
        pr.render_report_pdf(with_fingerprints(sample()), workdir=ROOT, logo_path=LOGO, fonts=SOURCES, preview_label="")
    assert pr.PREVIEW_LABEL == "PREVIEW — NOT FINAL"
