"""R7.4: finalization with a render input (schema version 2, sidecar-checked), next to the unchanged version 1 flow.

Offline and synthetic: the renderer is a stub that writes a tiny valid PDF, the drafts and sidecars are made by the real
R7.2 functions (`produce_report_draft`), and everything lives in temporary folders. No browser, no network.
"""
import dataclasses
import datetime as dt
import hashlib
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from seo_report import finalization_audit as fa
from seo_report import finalize_files as ff
from seo_report import record_schema as rs
from seo_report import record_writer as rw
from seo_report import render_artifacts as ra
from seo_report import render_identity as idn
from seo_report import render_input_hash as rh
from seo_report import render_state as rstate
from seo_report import report_finalize as rf
from seo_report.insights.history_engine import ClientHistory, ClientHistoryStatement
from seo_report.insights.rules import Tone
from test_finalization_audit import audit, tree
from test_history_access import env  # noqa: F401
from test_render_artifacts import StubRenderer, tiny_pdf
from test_render_identity import FONTS, LOGO, PERSON
from test_report_finalize import (ctx, files, fin, finalized_ids, pdf, put_draft, read_record,  # noqa: F401
                                  records, rendered, runs, sha, sup)

RENDERER = {"browser": "156.0.8078.4", "playwright": "1.64.1", "python": "3.14.0", "system": "Windows"}
_FINGERPRINTS = []


def fingerprints():
    if not _FINGERPRINTS:
        _FINGERPRINTS.append(idn.measure_render_fingerprints(logo_path=LOGO, font_paths=FONTS))
    return _FINGERPRINTS[0]


def make_input(c, day=30, history_insights="composed"):
    insights = c.composed if history_insights == "composed" else history_insights
    return idn.build_render_input(c.bundle, sender=PERSON, generation_date=dt.date(2026, 10, day),
                                  history_insights=insights, fingerprints=fingerprints())


def fixed_renderer(data):
    """A renderer stub whose PDF is exactly `data`."""
    def render(render_input, *, workdir, logo_path, fonts):
        path = Path(workdir) / "report.pdf"
        path.write_bytes(data)
        return SimpleNamespace(ok=True, pdf_path=path, pdf_sha256=hashlib.sha256(data).hexdigest(),
                               byte_count=len(data), browser_version="156.0.8078.4", issues=(),
                               render_input_sha256=rh.render_input_sha256(render_input))
    return render


_COUNTER = [0]


def produce(c, value, renderer=None, **kw):
    _COUNTER[0] += 1
    work = c.env.tmp / f"work{_COUNTER[0]}"
    work.mkdir()
    options = dict(workdir=work, logo_path=None, fonts=(), renderer=renderer or StubRenderer(),
                   finalized_render_hashes=rstate.finalized_render_hashes(c.reviewer))
    options.update(kw)
    return ra.produce_report_draft(c.reports, value, **options)


def sidecar_path(c, value):
    return c.reports / ra.sidecar_name(rh.render_input_sha256(value))


def edited(data, change):
    document = json.loads(data)
    change(document)
    return (json.dumps(document, sort_keys=True, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")


def state(c):
    """What a rejected call must leave exactly as it was (the attempt log is a separate, failure-only folder)."""
    return {"reports": files(c.reports), "records": {n: (c.reviewer / n).read_bytes() for n in records(c)},
            "runs": runs(c), "db": c.db.read_bytes() if c.db.exists() else None}


def stop(result, category, outcome=rf.OUTCOME_REJECTED):
    assert (result.outcome, result.category, result.finalized) == (outcome, category, False)
    assert category in rf.CATEGORIES and not result.incomplete
    assert (result.snapshot_created, result.event_recorded, result.pdf_outcome, result.record_name) == (
        None, None, None, None)


# --- version 2 success ---------------------------------------------------------------------------------------------------------------------------

def test_successful_version_2_finalization_matches_record_sidecar_and_pdf(ctx):
    value = make_input(ctx)
    out = produce(ctx, value)
    draft = ctx.draft.read_bytes()
    assert out.outcome == "created"
    result = fin(ctx, render_input=value)
    assert (result.outcome, result.finalized, result.ok) == (rf.OUTCOME_FINALIZED, True, True)
    record = read_record(ctx, result.record_name)
    digest = rh.render_input_sha256(value)
    assert record["schema_version"] == 2
    assert record["finalization"]["render_input_sha256"] == digest
    assert record["finalization"]["render_input_format"] == {"name": rh.FORMAT_NAME, "version": rh.FORMAT_VERSION}
    assert record["finalization"]["client_content_sha256"]  # kept in both versions
    assert (record["finalization"]["pdf_sha256"], record["finalization"]["pdf_bytes"]) == (sha(draft), len(draft))
    assert len(rw.record_identity(record)) == 9 and rw.record_identity(record)[6] == digest
    sidecar = ra.read_sidecar(ctx.reports, ra.sidecar_name(digest))
    assert (sidecar.pdf_sha256, sidecar.pdf_bytes) == (record["finalization"]["pdf_sha256"],
                                                      record["finalization"]["pdf_bytes"])
    assert files(ctx.reports)["report.pdf"] == draft and "report.unarchived.pdf" not in files(ctx.reports)
    assert result.run_id in finalized_ids(ctx)
    report = audit(ctx)
    assert (report.outcome, report.finalized_valid, report.legacy_records) == (fa.OUTCOME_CONSISTENT, 1, 0)
    assert report.by_category(fa.FINALIZED_VALID)[0].record_version == 2


def test_the_finalized_hash_is_discovered_after_a_version_2_finalization(ctx):
    value = make_input(ctx)
    produce(ctx, value)
    assert rstate.finalized_render_hashes(ctx.reviewer) == frozenset()
    fin(ctx, render_input=value)
    assert rstate.finalized_render_hashes(ctx.reviewer) == {rh.render_input_sha256(value)}


# --- version 1 is unchanged ------------------------------------------------------------------------------------------------------------------------

def test_without_a_render_input_the_version_1_flow_and_six_value_identity_are_unchanged(ctx):
    put_draft(ctx, pdf("a"))
    result = fin(ctx)
    record = read_record(ctx, result.record_name)
    assert result.finalized and record["schema_version"] == 1
    assert set(record["finalization"]) == set(rs.FINALIZATION_FIELDS)
    assert len(rw.record_identity(record)) == 6
    assert not [n for n in files(ctx.reports) if n.startswith("report.render-input.")]
    assert rstate.scan_finalized_render_hashes(ctx.reviewer).v1_records == 1


def test_a_supplied_but_invalid_render_input_never_falls_back_to_version_1(ctx):
    put_draft(ctx, pdf("a"))
    before = state(ctx)
    for bad in ("not an input", {"a": 1}, 5, b"x"):
        with pytest.raises(TypeError, match="render_input"):
            fin(ctx, render_input=bad)
    assert state(ctx) == before and records(ctx) == []
    result = fin(ctx, render_input=make_input(ctx))  # a valid input without a sidecar is a stop, not version 1
    stop(result, "render_sidecar_missing")
    assert records(ctx) == [] and ctx.draft.exists()


# --- stops before any change ----------------------------------------------------------------------------------------------------------------------

def test_a_missing_sidecar_stops_before_any_change(ctx):
    value = make_input(ctx)
    put_draft(ctx, pdf("a"))
    before = state(ctx)
    result = fin(ctx, render_input=value)
    stop(result, "render_sidecar_missing")
    assert (result.step, result.status_code) == ("verify_draft", "draft_invalid")
    assert state(ctx) == before and rendered_events(ctx) == []


def rendered_events(c):
    return [event for run in runs(c) for event in rendered(c, run[0])]


@pytest.mark.parametrize("content", [b"{broken", b"", b"[]", b'{"a":1,"a":2}'])
def test_a_malformed_sidecar_stops_before_any_change(ctx, content):
    value = make_input(ctx)
    produce(ctx, value)
    sidecar_path(ctx, value).write_bytes(content)
    before = state(ctx)
    result = fin(ctx, render_input=value)
    stop(result, "render_sidecar_invalid")
    assert (result.step, result.status_code) == ("verify_draft", "draft_invalid")
    assert state(ctx) == before and sidecar_path(ctx, value).read_bytes() == content  # not repaired


def test_an_unreadable_sidecar_is_a_failure_without_an_attempt_code(ctx):
    value = make_input(ctx)
    produce(ctx, value)

    class Unreadable(ff.FileOps):
        def open_read(self, path):
            if "render-input" in Path(path).name:
                raise OSError(13, "injected")
            return super().open_read(path)
    before = state(ctx)
    result = fin(ctx, render_input=value, ops=Unreadable())
    stop(result, "render_sidecar_unreadable", outcome=rf.OUTCOME_FAILED)
    assert result.status_code is None and result.attempt_log == rf.ATTEMPT_NOT_RECORDED
    assert state(ctx) == before


def test_a_sidecar_of_another_render_input_is_an_identity_mismatch(ctx):
    value, other = make_input(ctx), make_input(ctx, day=29)
    produce(ctx, value)
    data = ctx.draft.read_bytes()
    sidecar_path(ctx, value).write_bytes(ra.build_sidecar_bytes(
        other, renderer=RENDERER, pdf_sha256=sha(data), pdf_bytes=len(data)))
    before = state(ctx)
    stop(fin(ctx, render_input=value), "render_identity_mismatch")
    assert state(ctx) == before


def test_a_sidecar_whose_payload_does_not_hash_to_its_declared_hash_is_an_identity_mismatch(ctx):
    value = make_input(ctx)
    produce(ctx, value)
    path = sidecar_path(ctx, value)
    path.write_bytes(edited(path.read_bytes(), lambda d: d["render_input"]["input"]["client"].update(name="Other")))
    before = state(ctx)
    stop(fin(ctx, render_input=value), "render_identity_mismatch")
    assert state(ctx) == before


@pytest.mark.parametrize("change", [lambda d: d["pdf"].update(sha256="9" * 64),
                                    lambda d: d["pdf"].update(bytes=d["pdf"]["bytes"] + 1)])
def test_a_sidecar_pdf_hash_or_size_that_differs_from_the_draft_stops(ctx, change):
    value = make_input(ctx)
    produce(ctx, value)
    path = sidecar_path(ctx, value)
    path.write_bytes(edited(path.read_bytes(), change))
    before = state(ctx)
    stop(fin(ctx, render_input=value), "render_pdf_mismatch")
    assert state(ctx) == before


def test_a_draft_replaced_after_the_sidecar_was_written_is_a_mismatch(ctx):
    value = make_input(ctx)
    produce(ctx, value)
    ctx.draft.write_bytes(pdf("somebody else's draft"))  # a valid PDF, but not the one the sidecar describes
    before = state(ctx)
    stop(fin(ctx, render_input=value), "render_pdf_mismatch")
    assert state(ctx) == before


@pytest.mark.parametrize("change", [lambda d: d["render_input_format"].update(version=1),
                                    lambda d: d["render_input_format"].update(name="other"),
                                    lambda d: d["render_input"].update(version=99)])
def test_an_unsupported_sidecar_format_stops(ctx, change):
    value = make_input(ctx)
    produce(ctx, value)
    path = sidecar_path(ctx, value)
    path.write_bytes(edited(path.read_bytes(), change))
    before = state(ctx)
    stop(fin(ctx, render_input=value), "render_input_format_invalid")
    assert state(ctx) == before


def test_a_format_the_record_schema_does_not_support_stops(ctx, monkeypatch):
    value = make_input(ctx)
    produce(ctx, value)
    monkeypatch.setattr(rs, "SUPPORTED_RENDER_INPUT_VERSIONS", (rh.FORMAT_VERSION + 1,))
    before = state(ctx)
    stop(fin(ctx, render_input=value), "render_input_format_invalid")
    assert state(ctx) == before


def test_a_render_input_that_does_not_match_the_bundle_stops(ctx):
    value = make_input(ctx)
    produce(ctx, value)
    changed = dataclasses.replace(value, client=dataclasses.replace(value.client, name="Another Client"))
    before = state(ctx)
    stop(fin(ctx, render_input=changed), "render_input_bundle_mismatch")
    foreign = dataclasses.replace(value, operational=dataclasses.replace(value.operational,
                                                                         snapshot_content_hash="d" * 64))
    stop(fin(ctx, render_input=foreign), "render_input_bundle_mismatch")
    assert state(ctx) == before


def with_statements(c, text="Rankings improved since the last report."):
    """The composed history of the fixture plus one client statement (the fixture itself carries none)."""
    statement = ClientHistoryStatement(
        section="rankings", key="rankings_gain", dimension="rankings.keywords", text=text, tone=list(Tone)[0],
        priority=1, evidence={"improved": 3}, baseline_period_start="2025-12-01", baseline_period_end="2025-12-31",
        rules_version="1", group="rankings")
    return dataclasses.replace(c.composed, client=ClientHistory(statements=(statement,)))


def test_matching_historical_statements_are_accepted_and_recorded(ctx):
    insights = with_statements(ctx)
    value = make_input(ctx, history_insights=insights)
    assert [s.text for s in value.historical_statements] == ["Rankings improved since the last report."]
    produce(ctx, value)
    result = fin(ctx, render_input=value, history_insights=insights)
    assert result.finalized
    listed = read_record(ctx, result.record_name)["client_facing_history"]
    assert [s["text"] for s in listed] == [s.text for s in value.historical_statements]


def test_a_record_that_would_list_statements_that_were_not_drawn_stops(ctx):
    insights = with_statements(ctx)
    none_drawn = make_input(ctx, history_insights=None)
    assert none_drawn.historical_statements == ()
    produce(ctx, none_drawn)
    before = state(ctx)
    stop(fin(ctx, render_input=none_drawn, history_insights=insights), "render_statements_mismatch")
    assert state(ctx) == before


def test_rendered_statements_that_the_record_would_not_list_or_would_list_differently_stop(ctx):
    insights = with_statements(ctx)
    value = make_input(ctx, history_insights=insights)
    produce(ctx, value)
    before = state(ctx)
    stop(fin(ctx, render_input=value, history_insights=None), "render_statements_mismatch")
    stop(fin(ctx, render_input=value, history_insights=with_statements(ctx, "Rankings fell sharply.")),
         "render_statements_mismatch")
    stop(fin(ctx, render_input=value, history_insights=dataclasses.replace(insights, client=ClientHistory())),
         "render_statements_mismatch")
    assert state(ctx) == before


def test_the_attempt_record_uses_the_existing_status_code(ctx):
    value = make_input(ctx)
    put_draft(ctx, pdf("a"))
    result = fin(ctx, render_input=value)
    assert result.attempt_log == rf.ATTEMPT_WRITTEN
    attempts = [json.loads(p.read_text(encoding="utf-8")) for p in ctx.attempts.iterdir()]
    assert [(a["step"], a["status_code"]) for a in attempts] == [("verify_draft", "draft_invalid")]


# --- sidecar-rebuilt input and retry identity -------------------------------------------------------------------------------------------------------

def test_a_render_input_rebuilt_from_its_sidecar_is_accepted_with_its_snapshot_identity_restored(ctx):
    value = make_input(ctx)
    produce(ctx, value)
    sidecar = ra.read_sidecar(ctx.reports, ra.sidecar_name(rh.render_input_sha256(value)))
    rebuilt = sidecar.render_input
    assert rebuilt.operational.snapshot_content_hash is None
    assert rh.render_input_sha256(rebuilt) == rh.render_input_sha256(value)
    result = fin(ctx, render_input=rebuilt)
    assert result.finalized
    record = read_record(ctx, result.record_name)
    assert record["finalization"]["render_input_sha256"] == rh.render_input_sha256(value)
    assert record["snapshot"]["content_hash"] == ctx.bundle.snapshot.content_hash


def test_a_crash_after_the_sidecar_resumes_with_the_original_hash_and_date_and_finalizes(ctx):
    first = make_input(ctx, day=28)
    produce(ctx, first)
    original = sidecar_path(ctx, first).read_bytes()
    draft = ctx.draft.read_bytes()
    ctx.draft.rename(ctx.reports / "moved-aside.bin")  # the crash: the handoff never happened
    later = make_input(ctx, day=30)  # a retry on another day builds a different candidate
    out = produce(ctx, later)
    assert out.outcome == "resumed" and out.candidate_matches is False
    assert out.render_input_sha256 == rh.render_input_sha256(first) and out.generation_date == dt.date(2026, 10, 28)
    result = fin(ctx, render_input=out.render_input)
    assert result.finalized and files(ctx.reports)["report.pdf"] == draft
    assert sidecar_path(ctx, first).read_bytes() == original
    assert read_record(ctx, result.record_name)["finalization"]["render_input_sha256"] == rh.render_input_sha256(first)
    assert not sidecar_path(ctx, later).exists()


def test_the_candidate_of_a_later_day_is_not_accepted_in_place_of_the_original_sidecar(ctx):
    first, later = make_input(ctx, day=28), make_input(ctx, day=30)
    produce(ctx, first)
    stop(fin(ctx, render_input=later), "render_sidecar_missing")  # its own sidecar does not exist; nothing is guessed
    assert not sidecar_path(ctx, later).exists()


# --- duplicate and supersede ----------------------------------------------------------------------------------------------------------------------

def test_finalizing_the_same_render_input_again_is_already_finalized(ctx):
    value = make_input(ctx)
    produce(ctx, value)
    first = fin(ctx, render_input=value)
    snapshot = state(ctx)
    again = fin(ctx, render_input=value)
    assert (first.outcome, again.outcome) == (rf.OUTCOME_FINALIZED, rf.OUTCOME_ALREADY)
    assert again.record_outcome == rw.OUTCOME_ALREADY and again.pdf_outcome == "already_promoted"
    assert state(ctx) == snapshot and len(records(ctx)) == 1


def test_a_changed_render_input_supersedes_the_old_report_and_keeps_both_identities(ctx):
    old, new = make_input(ctx, day=28), make_input(ctx, day=30)
    produce(ctx, old)
    old_pdf = ctx.draft.read_bytes()
    first = fin(ctx, render_input=old)
    produce(ctx, new)
    new_pdf = ctx.draft.read_bytes()
    second = fin(ctx, render_input=new, replace_existing=True)
    assert first.finalized and second.finalized and old_pdf != new_pdf
    assert files(ctx.reports)["report.pdf"] == new_pdf and files(ctx.reports)[sup(old_pdf)] == old_pdf
    hashes = {read_record(ctx, n)["finalization"]["render_input_sha256"] for n in records(ctx)}
    assert hashes == {rh.render_input_sha256(old), rh.render_input_sha256(new)}
    assert rstate.finalized_render_hashes(ctx.reviewer) == hashes
    report = audit(ctx)
    assert (report.outcome, report.finalized_valid) == (fa.OUTCOME_CONSISTENT, 2)
    assert sidecar_path(ctx, old).exists() and sidecar_path(ctx, new).exists()


def test_a_version_1_record_at_the_target_file_name_is_a_conflict_before_any_change(ctx):
    data = pdf("same bytes")
    put_draft(ctx, data)
    v1 = fin(ctx)
    assert v1.finalized and read_record(ctx, v1.record_name)["schema_version"] == 1
    value = make_input(ctx)
    out = produce(ctx, value, renderer=fixed_renderer(data))  # the same PDF bytes, now with a render identity
    assert out.outcome == "created"
    before = state(ctx)
    events_before = rendered_events(ctx)
    result = fin(ctx, render_input=value)
    assert (result.outcome, result.category, result.finalized) == (rf.OUTCOME_CONFLICT, "record_identity_differs", False)
    assert not result.incomplete and state(ctx) == before and rendered_events(ctx) == events_before
    assert read_record(ctx, records(ctx)[0])["schema_version"] == 1


# --- interruption and retry at every persistence boundary ---------------------------------------------------------------------------------------------

def interrupt_snapshot(c):
    c.faults.arm("write")
    return {}


def interrupt_event(c):
    c.faults.arm("add_event")
    return {}


def interrupt_promote(c):
    return {"ops": FaultOpsFor("rename_noreplace", 0)}


def interrupt_record_create(c):
    return {"ops": FaultOpsFor("create_exclusive", 0)}


def interrupt_record_promote(c):
    return {"ops": FaultOpsFor("rename_noreplace", 1)}  # rename 1 is the PDF, rename 2 the record


class FaultOpsFor(ff.FileOps):
    def __init__(self, method, after):
        self.method, self.after = method, after

    def _maybe(self, name):
        if name == self.method:
            if self.after:
                self.after -= 1
            else:
                raise OSError(13, "injected")

    def create_exclusive(self, path):
        if "reviewer" in str(path) and Path(path).name.startswith("."):
            self._maybe("create_exclusive")
        return super().create_exclusive(path)

    def rename_noreplace(self, source, destination):
        self._maybe("rename_noreplace")
        super().rename_noreplace(source, destination)


@pytest.mark.parametrize("interrupt", [interrupt_snapshot, interrupt_event, interrupt_promote, interrupt_record_create,
                                       interrupt_record_promote])
def test_an_interruption_at_each_boundary_is_resumed_with_the_original_render_identity(ctx, interrupt):
    value = make_input(ctx, day=28)
    produce(ctx, value)
    digest = rh.render_input_sha256(value)
    sidecar_before = sidecar_path(ctx, value).read_bytes()
    failed = fin(ctx, render_input=value, **interrupt(ctx))
    assert not failed.finalized and failed.outcome in (rf.OUTCOME_FAILED, rf.OUTCOME_CONFLICT)
    assert sidecar_path(ctx, value).read_bytes() == sidecar_before  # never rewritten
    # the retry learns the identity from the sidecar, not from today's data
    closed = rstate.finalized_render_hashes(ctx.reviewer)
    opened = rstate.open_sidecars(ctx.reports, finalized=closed)
    assert [s.render_input_sha256 for s in opened] == [digest]
    assert opened[0].generation_date == dt.date(2026, 10, 28)
    result = fin(ctx, render_input=opened[0].render_input)
    assert result.finalized
    record = read_record(ctx, result.record_name)
    assert record["finalization"]["render_input_sha256"] == digest and record["schema_version"] == 2
    assert sidecar_path(ctx, value).read_bytes() == sidecar_before
    assert len(records(ctx)) == 1 and audit(ctx).outcome in (fa.OUTCOME_CONSISTENT, fa.OUTCOME_ATTENTION)
    assert audit(ctx).finalized_valid == 1


def test_a_partial_record_left_by_an_interruption_is_kept_and_the_retry_completes(ctx):
    value = make_input(ctx)
    produce(ctx, value)
    failed = fin(ctx, render_input=value, ops=FaultOpsFor("rename_noreplace", 1))
    assert failed.incomplete and not failed.finalized
    partials = [p.name for p in ctx.reviewer.iterdir() if ff.is_partial_name(p.name)]
    assert len(partials) == 1
    result = fin(ctx, render_input=value)
    assert result.finalized
    assert [p.name for p in ctx.reviewer.iterdir() if ff.is_partial_name(p.name)] == partials  # never deleted


def test_after_a_promotion_without_a_record_the_sidecar_is_still_open_and_a_rerender_is_benign(ctx):
    """G1 (documented, unchanged): the draft was promoted, no record exists yet, so the sidecar is still open."""
    value = make_input(ctx)
    produce(ctx, value)
    failed = fin(ctx, render_input=value, ops=FaultOpsFor("rename_noreplace", 1))
    assert failed.incomplete and "report.unarchived.pdf" not in files(ctx.reports)
    assert files(ctx.reports)["report.pdf"]
    out = produce(ctx, make_input(ctx, day=30))
    assert out.outcome == "resumed" and out.handed_off  # a duplicate draft of the already promoted report
    result = fin(ctx, render_input=out.render_input)
    assert result.finalized and result.duplicate_draft
    assert files(ctx.reports)[result.duplicate_draft] == files(ctx.reports)["report.pdf"]


# --- read-only and no overwrite -------------------------------------------------------------------------------------------------------------------------

def test_the_render_state_helpers_never_change_anything(ctx):
    value = make_input(ctx)
    produce(ctx, value)
    fin(ctx, render_input=value)
    before = tree(ctx)
    rstate.scan_finalized_render_hashes(ctx.reviewer)
    rstate.open_sidecars(ctx.reports)
    rstate.read_sidecar_for(ctx.reports, rh.render_input_sha256(value))
    assert tree(ctx) == before


def test_finalization_never_overwrites_the_sidecar_the_draft_or_the_record(ctx):
    value = make_input(ctx)
    produce(ctx, value)
    sidecar = sidecar_path(ctx, value).read_bytes()
    result = fin(ctx, render_input=value)
    record = (ctx.reviewer / result.record_name).read_bytes()
    pdf_bytes = files(ctx.reports)["report.pdf"]
    for _ in range(2):
        assert fin(ctx, render_input=value).outcome == rf.OUTCOME_ALREADY
    assert sidecar_path(ctx, value).read_bytes() == sidecar
    assert (ctx.reviewer / result.record_name).read_bytes() == record
    assert files(ctx.reports)["report.pdf"] == pdf_bytes and len(records(ctx)) == 1


def test_results_and_attempt_records_hold_no_client_data_or_paths(ctx):
    value = make_input(ctx)
    put_draft(ctx, pdf("a"))
    result = fin(ctx, render_input=value)
    text = repr(result) + "".join(p.read_text(encoding="utf-8") for p in ctx.attempts.iterdir())
    for secret in (value.client.name, str(ctx.env.tmp), "C:\\"):
        assert secret not in text


def test_the_version_1_flow_does_not_import_the_render_stack(ctx):
    source = Path(rf.__file__).read_text(encoding="utf-8")
    top_level = [line for line in source.splitlines() if line.startswith(("import ", "from "))]
    assert not [line for line in top_level if "render_state" in line or "render_identity" in line
                or "render_artifacts" in line]  # imported only when a render input is supplied
