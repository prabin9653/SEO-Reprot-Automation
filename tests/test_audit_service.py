"""R8.3: the audit gate that decides whether a run may be called audited (read-only)."""
import dataclasses
import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from seo_report import audit_service
from seo_report import finalize_files as ff
from seo_report import output_layout as layout
from test_pipeline import RUN_KEY, go, paths, req, tree, world  # noqa: F401
from test_report_run import CURRENT


def arguments(w):
    run = w.ledger.get_run(RUN_KEY)
    return dict(settings=w.settings, client_key="example-co", period_start=run.period_start, period_end=run.period_end,
                run_id=run.engine_run_id, pdf_sha256=run.pdf_sha256, pdf_bytes=run.pdf_bytes,
                record_name=run.record_name, render_input_sha256=run.render_input_sha256)


def finalized_world(world):
    w = world()
    assert go(w).state == "audited"
    return w


def test_a_finalized_consistent_run_passes_the_gate(world):
    w = finalized_world(world)
    verdict = audit_service.verify_finalization(**arguments(w))
    assert verdict.ok and verdict.reasons == () and verdict.reader_lists_run
    assert (verdict.outcome, verdict.finalized_valid, verdict.verification_complete) == ("consistent", 1, True)
    assert verdict.as_summary() == {"outcome": "consistent", "finalized_valid": 1, "verification_complete": True}


def test_the_gate_is_read_only(world):
    w = finalized_world(world)
    before = (tree(w.settings.output_root), w.settings.history_db_path.read_bytes())
    audit_service.verify_finalization(**arguments(w))
    audit_service.verify_finalization(**arguments(w))
    assert (tree(w.settings.output_root), w.settings.history_db_path.read_bytes()) == before


@pytest.mark.parametrize("change,reason", [
    ({"pdf_sha256": "0" * 64}, "record_identity_differs"), ({"pdf_bytes": 1}, "record_identity_differs"),
    ({"run_id": "0198c1a2-7b3c-7d4e-8f5a-00000000ffff"}, "reader_does_not_list_run"),
    ({"render_input_sha256": "1" * 64}, "record_render_identity_differs"),
    ({"record_name": "reviewer.0198c1a2-7b3c-7d4e-8f5a-00000000ffff.0000000000000000.json"}, "record_not_finalized_valid"),
])
def test_an_identity_that_does_not_match_the_record_fails_the_gate(world, change, reason):
    w = finalized_world(world)
    verdict = audit_service.verify_finalization(**{**arguments(w), **change})
    assert not verdict.ok and reason in verdict.reasons


def test_a_leftover_partial_file_fails_the_gate(world):
    w = finalized_world(world)
    reports, reviewer, _ = paths(w)
    (reviewer / ff.partial_name("reviewer.x.json", "0123456789ab")).write_bytes(b"{")
    verdict = audit_service.verify_finalization(**arguments(w))
    assert not verdict.ok and "audit_attention_needed" in verdict.reasons and "finding_partial_file_present" in verdict.reasons


def test_an_unarchived_draft_after_finalization_fails_the_gate(world):
    w = finalized_world(world)
    reports, _, _ = paths(w)
    shutil.copyfile(reports / "report.pdf", reports / "report.unarchived.pdf")
    verdict = audit_service.verify_finalization(**arguments(w))
    assert not verdict.ok and "finding_unarchived_pdf_present" in verdict.reasons


def test_a_changed_pdf_fails_the_gate(world):
    w = finalized_world(world)
    reports, _, _ = paths(w)
    data = (reports / "report.pdf").read_bytes()
    (reports / "report.pdf").write_bytes(data[:-1] + b" ")
    verdict = audit_service.verify_finalization(**arguments(w))
    assert not verdict.ok and "audit_attention_needed" in verdict.reasons and "finding_pdf_hash_mismatch" in verdict.reasons


def test_a_missing_sidecar_fails_the_gate(world):
    w = finalized_world(world)
    reports, _, _ = paths(w)
    next(p for p in reports.iterdir() if p.name.startswith("report.render-input.")).unlink()
    verdict = audit_service.verify_finalization(**arguments(w))
    assert not verdict.ok and "finding_render_sidecar_missing" in verdict.reasons


def test_a_period_that_is_not_finalized_does_not_pass(world):
    w = world()
    start, end = CURRENT.current.start.isoformat(), CURRENT.current.end.isoformat()
    verdict = audit_service.verify_finalization(
        settings=w.settings, client_key="example-co", period_start=start, period_end=end,
        run_id="0198c1a2-7b3c-7d4e-8f5a-000000001000", pdf_sha256="a" * 64, pdf_bytes=10,
        record_name=f"reviewer.0198c1a2-7b3c-7d4e-8f5a-000000001000.{'a' * 16}.json", render_input_sha256="b" * 64)
    assert not verdict.ok and not verdict.reader_lists_run
    assert {"record_not_finalized_valid", "reader_does_not_list_run", "record_unreadable"} <= set(verdict.reasons)


def test_reasons_are_fixed_codes_without_client_data(world):
    w = finalized_world(world)
    verdict = audit_service.verify_finalization(**{**arguments(w), "pdf_sha256": "0" * 64})
    for reason in verdict.reasons:
        assert reason.replace("_", "").isalnum() and reason == reason.lower()
    assert "Example" not in repr(verdict) and str(w.tmp) not in repr(verdict)
