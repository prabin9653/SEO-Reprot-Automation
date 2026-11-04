"""R8.3: the render service (stub renderer; temporary folders only)."""
import dataclasses
import datetime as dt
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from seo_report import contracts
from seo_report import output_layout as layout
from seo_report import pdf_renderer as pr
from seo_report import render_artifacts as ra
from seo_report import render_input_hash as rh
from seo_report import render_service
from seo_report import report_service
from test_pipeline import Clock, PERIOD_ID, default_collect, go, make_world, req, world  # noqa: F401
from test_render_artifacts import StubRenderer
from test_report_run import CURRENT

D1, D2 = dt.date(2026, 11, 2), dt.date(2026, 11, 9)


def prepared(w, key="example-co"):
    run = w.services.clients.clients[key]
    assembled = report_service.assemble(run, default_collect(run, D1), registry=w.registry, settings=w.settings)
    reports = layout.report_dir(w.settings.output_root, key, CURRENT.current.start, CURRENT.current.end)
    reviewer = layout.reviewer_dir(w.settings.output_root, key, CURRENT.current.start, CURRENT.current.end)
    reports.mkdir(parents=True)
    reviewer.mkdir(parents=True)
    return run, assembled, reports, reviewer


def render(w, run, assembled, day=D1, attempt=1, **kw):
    kw.setdefault("renderer", StubRenderer())
    return render_service.render(assembled, run, settings=w.settings, generation_date=day, attempt=attempt, **kw)


def test_the_render_input_returned_is_exactly_the_one_produced_with_the_draft_and_sidecar(world):
    w = world()
    run, assembled, reports, _ = prepared(w)
    result = render(w, run, assembled)
    assert result.render_input is result.drafted.render_input and result.generation_date == D1
    assert result.drafted.outcome == "created" and result.drafted.created_sidecar and result.drafted.handed_off
    digest = rh.render_input_sha256(result.render_input)
    assert result.drafted.render_input_sha256 == digest
    assert sorted(p.name for p in reports.iterdir()) == [ra.sidecar_name(digest), "report.unarchived.pdf"]
    sidecar = ra.read_sidecar(reports, ra.sidecar_name(digest))
    assert (sidecar.generation_date, sidecar.pdf_sha256) == (D1, result.drafted.pdf_sha256)
    assert result.render_input.operational.snapshot_content_hash == assembled.bundle.snapshot.content_hash


def test_a_retry_on_a_later_date_keeps_the_original_render_identity(world):
    w = world()
    run, assembled, reports, _ = prepared(w)
    first = render(w, run, assembled, D1)
    sidecar_before = (reports / ra.sidecar_name(first.drafted.render_input_sha256)).read_bytes()
    again = render(w, run, assembled, D2, attempt=2)
    assert again.drafted.outcome == "already_prepared" and again.drafted.candidate_matches is False
    assert again.generation_date == D1 and again.drafted.render_input_sha256 == first.drafted.render_input_sha256
    assert rh.render_input_sha256(again.render_input) == rh.render_input_sha256(first.render_input)
    assert (reports / ra.sidecar_name(first.drafted.render_input_sha256)).read_bytes() == sidecar_before
    assert len([p for p in reports.iterdir() if p.name.startswith("report.render-input.")]) == 1


def test_a_crash_between_sidecar_and_draft_resumes_with_the_sidecars_input(world):
    w = world()
    run, assembled, reports, _ = prepared(w)
    first = render(w, run, assembled, D1)
    (reports / "report.unarchived.pdf").rename(reports / "moved-aside.bin")
    resumed = render(w, run, assembled, D2, attempt=2)
    assert resumed.drafted.outcome == "resumed" and resumed.generation_date == D1
    assert resumed.drafted.render_input_sha256 == first.drafted.render_input_sha256
    assert (reports / "report.unarchived.pdf").read_bytes() == (reports / "moved-aside.bin").read_bytes()


def test_each_call_gets_a_fresh_empty_work_folder_outside_the_project(world):
    w = world()
    run, assembled, reports, _ = prepared(w)
    folders = [render_service.new_workdir(w.settings, run.run_key, 1) for _ in range(3)]
    assert len(set(folders)) == 3 and all(f.is_dir() and not any(f.iterdir()) for f in folders)
    assert all(str(f).startswith(str(w.settings.work_root)) and f.parent.name == run.run_key.replace("/", "__")
               for f in folders)
    assert ROOT not in folders[0].parents


def test_layout_issues_block_the_draft(world):
    w = world()
    run, assembled, reports, _ = prepared(w)
    with pytest.raises(contracts.StageError) as info:
        render(w, run, assembled, renderer=StubRenderer(issues=("overflow",)))
    assert (info.value.category, info.value.code) == ("layout_issues", "render_has_layout_issues")
    assert list(reports.iterdir()) == []


def test_a_renderer_failure_is_a_retryable_render_failure(world):
    w = world()
    run, assembled, reports, _ = prepared(w)

    def failing(render_input, *, workdir, logo_path, fonts):
        raise pr.PdfRenderError("browser_unavailable", "no browser")
    with pytest.raises(contracts.StageError) as info:
        render(w, run, assembled, renderer=failing)
    assert (info.value.category, info.value.code) == ("render_failed", "render_failed")
    browser = type("TargetClosedError", (Exception,), {"__module__": "playwright._impl._errors"})

    def crashing(render_input, *, workdir, logo_path, fonts):
        raise browser("closed")
    with pytest.raises(contracts.StageError) as info:
        render(w, run, assembled, renderer=crashing)
    assert (info.value.category, info.value.code) == ("render_failed", "browser_error")


def test_unexpected_renderer_errors_are_not_hidden(world):
    w = world()
    run, assembled, _, _ = prepared(w)

    def buggy(render_input, *, workdir, logo_path, fonts):
        raise ZeroDivisionError("bug")
    with pytest.raises(ZeroDivisionError):
        render(w, run, assembled, renderer=buggy)


def test_missing_fonts_in_the_settings_are_a_configuration_error(world):
    w = world()
    run, assembled, _, _ = prepared(w)
    settings = dataclasses.replace(w.settings, fonts=())
    with pytest.raises(contracts.StageError) as info:
        render_service.render(assembled, run, settings=settings, generation_date=D1, attempt=1, renderer=StubRenderer())
    assert (info.value.category, info.value.code) == ("config_invalid", "fonts_not_configured")


def test_an_unreadable_logo_is_a_render_input_failure(world, tmp_path):
    w = world()
    run, assembled, _, _ = prepared(w)
    settings = dataclasses.replace(w.settings, logo_path=tmp_path / "no-such-logo.png")
    with pytest.raises(contracts.StageError) as info:
        render_service.render(assembled, run, settings=settings, generation_date=D1, attempt=1, renderer=StubRenderer())
    assert (info.value.category, info.value.code) == ("render_failed", "render_input_invalid")


def test_an_input_that_was_already_finalized_is_never_rendered_again(world):
    w = world()
    assert go(w).state == "audited"
    run = w.services.clients.clients["example-co"]
    assembled = report_service.assemble(run, default_collect(run, D1), registry=w.registry, settings=w.settings)
    with pytest.raises(contracts.StageError) as info:
        render(w, run, assembled, D1)
    assert (info.value.category, info.value.code) == ("finalization_conflict", "render_input_already_finalized")


def test_an_incomplete_reviewer_scan_stops_the_render(world, tmp_path):
    w = world()
    run, assembled, reports, reviewer = prepared(w)
    missing = tmp_path / "nowhere" / "reviewer"
    with pytest.raises(contracts.StageError) as info:
        render(w, run, assembled, reviewer_dir=missing)
    assert info.value.category == "render_failed" and info.value.code.startswith("reviewer_scan_")
    assert not missing.exists()


def test_scratch_folders_can_replace_the_production_folders(world, tmp_path):
    w = world()
    run = w.services.clients.clients["example-co"]
    assembled = report_service.assemble(run, default_collect(run, D1), registry=w.registry, settings=w.settings)
    scratch = tmp_path / "scratch"
    reports, reviewer, work = scratch / "reports", scratch / "reviewer", scratch / "work"
    for folder in (reports, reviewer, work):
        folder.mkdir(parents=True)
    result = render(w, run, assembled, reports_dir=reports, reviewer_dir=reviewer, workdir=work)
    assert result.drafted.ok and (reports / "report.unarchived.pdf").exists()
    assert not w.settings.output_root.exists() and not w.settings.work_root.exists()
