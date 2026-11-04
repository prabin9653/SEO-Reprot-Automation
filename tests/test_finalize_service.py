"""R8.3: the finalization service maps the engine's results; real finalization runs in temporary folders."""
import dataclasses
import datetime as dt
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from seo_report import contracts
from seo_report import finalize_service
from seo_report import output_layout as layout
from seo_report import record_writer as rw
from seo_report import render_artifacts as ra
from seo_report import render_input_hash as rh
from seo_report import render_service
from seo_report import report_finalize as rf
from seo_report import report_service
from test_history_access import env  # noqa: F401
from test_pipeline import default_collect, world  # noqa: F401
from test_render_artifacts import StubRenderer
from test_report_run import CURRENT, with_history

D1, D2 = dt.date(2026, 11, 2), dt.date(2026, 11, 9)
STAMP = "2026-11-02T01:30:00+00:00"


def prepare(w, day=D1, key="example-co", settings=None):
    settings = settings or w.settings
    run = w.services.clients.clients[key]
    assembled = report_service.assemble(run, default_collect(run, D1), registry=w.registry, settings=settings)
    start, end = CURRENT.current.start, CURRENT.current.end
    for folder in (layout.report_dir(settings.output_root, key, start, end), layout.reviewer_dir(settings.output_root, key, start, end),
                   layout.attempt_dir(settings.output_root, key, start, end)):
        folder.mkdir(parents=True, exist_ok=True)
    rendered = render_service.render(assembled, run, settings=dataclasses.replace(settings), generation_date=day,
                                     attempt=1, renderer=StubRenderer())
    return run, assembled, rendered


def reassemble(w, run):
    """The bundle of a retry: the in-memory bundle is never reused after the history changed (a finalized run now exists)."""
    return report_service.assemble(run, default_collect(run, D1), registry=w.registry, settings=w.settings)


def finalize(w, run, assembled, rendered, settings=None, **kw):
    kw.setdefault("finalized_at", STAMP)
    kw.setdefault("replace_existing", False)
    return finalize_service.finalize(assembled, rendered, run, settings=settings or w.settings, **kw)


def fake_result(outcome, category=None, incomplete=False):
    return rf.FinalizationResult(outcome=outcome, finalized=False, step="x", category=category, status_code=None,
                                 incomplete=incomplete)


def error_of(function):
    with pytest.raises(contracts.StageError) as info:
        function()
    return info.value.category, info.value.code


# --- real finalization ------------------------------------------------------------------------------------------------------------------

def test_the_exact_draft_and_render_input_are_finalized_into_a_version_2_record(world):
    w = world()
    run, assembled, rendered = prepare(w)
    verdict = finalize(w, run, assembled, rendered)
    assert verdict.outcome == "finalized" and verdict.result.finalized
    digest = rh.render_input_sha256(rendered.render_input)
    reviewer = layout.reviewer_dir(w.settings.output_root, "example-co", CURRENT.current.start, CURRENT.current.end)
    record = rw.parse_record_bytes((reviewer / verdict.record_name).read_bytes())
    assert record["schema_version"] == 2 and record["finalization"]["render_input_sha256"] == digest
    assert (record["finalization"]["pdf_sha256"], record["finalization"]["pdf_bytes"]) == (
        rendered.drafted.pdf_sha256, rendered.drafted.pdf_bytes) == (verdict.pdf_sha256, verdict.pdf_bytes)
    assert record["finalization"]["finalized_at"] == STAMP and record["snapshot"]["run_id"] == verdict.run_id


def test_finalizing_the_same_input_again_is_already_finalized_and_changes_nothing(world):
    w = world()
    run, assembled, rendered = prepare(w)
    first = finalize(w, run, assembled, rendered)
    before = {p: p.read_bytes() for p in w.settings.output_root.rglob("*") if p.is_file()}
    db = w.settings.history_db_path.read_bytes()
    again = finalize(w, run, reassemble(w, run), rendered, finalized_at="2030-01-01T00:00:00+00:00")
    assert again.outcome == "already_finalized" and (again.run_id, again.record_name) == (first.run_id, first.record_name)
    assert {p: p.read_bytes() for p in w.settings.output_root.rglob("*") if p.is_file()} == before
    assert w.settings.history_db_path.read_bytes() == db


def test_a_changed_report_needs_replace_existing_and_the_old_one_is_kept(world):
    w = world()
    run, assembled, first_render = prepare(w)
    finalize(w, run, assembled, first_render)
    second_render = render_service.render(assembled, run, settings=w.settings, generation_date=D2, attempt=2,
                                          renderer=StubRenderer())
    assert second_render.drafted.render_input_sha256 != first_render.drafted.render_input_sha256
    assert error_of(lambda: finalize(w, run, reassemble(w, run), second_render)) == (
        "finalization_conflict", "existing_report_differs")
    verdict = finalize(w, run, reassemble(w, run), second_render, replace_existing=True)
    reports = layout.report_dir(w.settings.output_root, "example-co", CURRENT.current.start, CURRENT.current.end)
    names = {p.name for p in reports.iterdir()}
    assert "report.pdf" in names and any(n.startswith("report.superseded.") for n in names)
    assert verdict.pdf_sha256 == second_render.drafted.pdf_sha256


def test_a_missing_draft_is_a_render_problem_not_a_conflict(world):
    w = world()
    run, assembled, rendered = prepare(w)
    reports = layout.report_dir(w.settings.output_root, "example-co", CURRENT.current.start, CURRENT.current.end)
    (reports / "report.unarchived.pdf").rename(reports / "aside.bin")
    assert error_of(lambda: finalize(w, run, assembled, rendered)) == ("render_failed", "draft_invalid")
    assert not w.settings.history_db_path.exists()  # nothing was stored


def test_a_bundle_assembled_before_a_finalization_is_stale_afterwards(world):
    w = world()
    run, assembled, rendered = prepare(w)
    finalize(w, run, assembled, rendered)
    assert error_of(lambda: finalize(w, run, assembled, rendered)) == ("stale_bundle", "stale_bundle")


def test_history_that_changed_after_assembly_is_a_stale_bundle(env, world):
    w = world()
    settings = dataclasses.replace(w.settings, history_db_path=env.db, output_root=env.out)
    run, assembled, rendered = prepare(w, settings=settings)
    with_history(env)  # a finalized earlier period appears after the bundle was assembled
    assert error_of(lambda: finalize(w, run, assembled, rendered, settings=settings)) == ("stale_bundle", "stale_bundle")


def test_a_sidecar_for_a_different_report_is_a_conflict_before_anything_is_stored(world):
    w = world()
    run, assembled, rendered = prepare(w)
    reports = layout.report_dir(w.settings.output_root, "example-co", CURRENT.current.start, CURRENT.current.end)
    (reports / ra.sidecar_name(rendered.drafted.render_input_sha256)).unlink()
    category, code = error_of(lambda: finalize(w, run, assembled, rendered))
    assert (category, code) == ("finalization_conflict", "render_sidecar_missing")
    assert not w.settings.history_db_path.exists()


# --- the mapping of results ---------------------------------------------------------------------------------------------------------------

def test_the_version_1_path_is_never_used_and_the_exact_render_input_is_passed(world, monkeypatch):
    w = world()
    run, assembled, rendered = prepare(w)
    seen = {}

    def capture(**kwargs):
        seen.update(kwargs)
        return rf.FinalizationResult(outcome=rf.OUTCOME_FINALIZED, finalized=True, step=None, category=None,
                                     status_code=None, run_id="0198c1a2-7b3c-7d4e-8f5a-000000001000", revision=1,
                                     pdf_sha256="a" * 64, pdf_bytes=5, record_name="reviewer.x.json")
    monkeypatch.setattr(rf, "finalize_report", capture)
    finalize(w, run, assembled, rendered, replace_existing=True, environ={"X": "1"})
    assert seen["render_input"] is rendered.render_input and seen["render_input"] is not None
    assert seen["bundle"] is assembled.bundle and seen["history_insights"] is assembled.history_insights
    assert seen["recommendation_annotations"] is assembled.annotations and seen["replace_existing"] is True
    assert seen["finalized_at"] == STAMP and seen["draft_path"].name == "report.unarchived.pdf"
    assert seen["output_root"] == w.settings.output_root and seen["history_db_path"] == w.settings.history_db_path


@pytest.mark.parametrize("result,expected", [
    (fake_result(rf.OUTCOME_STALE, "stale_bundle"), ("stale_bundle", "stale_bundle")),
    (fake_result(rf.OUTCOME_CONFLICT, "existing_report_differs"), ("finalization_conflict", "existing_report_differs")),
    (fake_result(rf.OUTCOME_CONFLICT, "record_identity_differs"), ("finalization_conflict", "record_identity_differs")),
    (fake_result(rf.OUTCOME_CONFLICT, "reader_record_limit"), ("finalization_conflict", "reader_record_limit")),
    (fake_result(rf.OUTCOME_REJECTED, "render_identity_mismatch"), ("finalization_conflict", "render_identity_mismatch")),
    (fake_result(rf.OUTCOME_REJECTED, "render_sidecar_missing"), ("finalization_conflict", "render_sidecar_missing")),
    (fake_result(rf.OUTCOME_REJECTED, "draft_invalid"), ("render_failed", "draft_invalid")),
    (fake_result(rf.OUTCOME_REJECTED, "draft_partial_conflict"), ("render_failed", "draft_partial_conflict")),
    (fake_result(rf.OUTCOME_REJECTED, "bundle_inconsistent"), ("internal_error", "bundle_inconsistent")),
    (fake_result(rf.OUTCOME_FAILED, "snapshot_persist_failed", incomplete=True), ("finalization_failed", "snapshot_persist_failed")),
    (fake_result(rf.OUTCOME_FAILED, "history_unreadable"), ("finalization_failed", "history_unreadable")),
    (fake_result(rf.OUTCOME_FAILED, None), ("finalization_failed", "finalization_failed"))])
def test_engine_results_map_to_the_runners_categories(world, monkeypatch, result, expected):
    w = world()
    run, assembled, rendered = prepare(w)
    monkeypatch.setattr(rf, "finalize_report", lambda **kw: result)
    assert error_of(lambda: finalize(w, run, assembled, rendered)) == expected


def test_every_mapped_category_is_a_known_error_category(world):
    for category, _ in (("stale_bundle", 0), ("finalization_conflict", 0), ("render_failed", 0), ("internal_error", 0),
                        ("finalization_failed", 0)):
        assert category in contracts.ERROR_CATEGORIES
