"""R8.3: the resumable pipeline `run_client` and the dry run, with offline fakes and temporary folders only.

The helpers at the top (`make_world`, `req`, `default_collect`, ...) are shared by the other R8.3 test files.
Nothing here calls Google, Make.com or any network service, and the renderer is a stub except in the single
real-Chromium test at the end.
"""
import dataclasses
import datetime as dt
import errno
import hashlib
import itertools
import json
import logging
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from google.auth.exceptions import RefreshError

from seo_report import clients as cl
from seo_report import contracts
from seo_report import finalization_audit as fa
from seo_report import finalize_files as ff
from seo_report import ledger as ledger_module
from seo_report import output_layout as layout
from seo_report import pdf_validation as pv
from seo_report import pipeline
from seo_report import record_writer as rw
from seo_report import render_artifacts as ra
from seo_report import source_collector as sc
from seo_report.client_config import ClientEntry, ClientRegistry
from seo_report.client_loader import ClientRun, LoadResult
from seo_report.runner_config import parse_settings
from seo_report.snapshots import store as st
from seo_report.sources import gsc as gsc_source
from seo_report.sources import ga4 as ga4_source
from test_gsc import FakeService
from test_pdf_renderer import FONT_FILES, LOGO
from test_render_artifacts import StubRenderer
from test_report_run import CURRENT, GA4, GSC, KEYWORDS

SENDER = cl.Sender(name="Sample Sender", title="PM", address_lines=("1 Sample Way",), phone="(000) 000-0000",
                   email="sender@example.test", website_label="Sample", website_url="https://sender.example",
                   profiles=(cl.ContactProfile("United States", "Project Manager | Sample Co", ("1 Sample Way",), "Sample",
                                               "https://sender.example"),))
PERIOD_ID = contracts.period_id_of(CURRENT.current.start, CURRENT.current.end)
UTC = dt.timezone.utc


class Clock:
    """A deterministic aware-UTC clock that moves forward on every reading."""

    def __init__(self, start=None, step: float = 1.0):
        self.now = start or dt.datetime(2026, 11, 2, 1, 0, 0, tzinfo=UTC)
        self.step = dt.timedelta(seconds=step)

    def __call__(self):
        value = self.now
        self.now += self.step
        return value

    def advance(self, **kwargs):
        self.now += dt.timedelta(**kwargs)


def make_client(name="Example Co", **overrides):
    values = dict(name=name, website="https://example.test/", gsc_property="https://example.test/",
                  ga4_property_id="123456789", status="active", sender=SENDER, target_country="United States",
                  target_city="Dallas", report_start=CURRENT.current.start, report_end=CURRENT.current.end,
                  sheet_row=2, keywords=list(KEYWORDS))
    values.update(overrides)
    return cl.Client(**values)


def key_of(name: str) -> str:
    return name.lower().replace(" ", "-")


def default_collect(client_run, today):
    return sc.collect_sources(
        client_run.client, client_run.periods, gsc_service=None, ga4_client=None, today=today,
        fetch_gsc=lambda service, site, periods, **kw: dataclasses.replace(GSC, site_url=site),
        fetch_ga4=lambda client, prop, periods, **kw: dataclasses.replace(GA4, property_id=str(prop)))


def make_settings(tmp_path, **execution):
    local = tmp_path / "local"
    data = {"paths": {"output_root": str(local / "output"), "history_db": str(local / "history" / "seo_reports.sqlite"),
                      "ledger_db": str(local / "ledger" / "ledger.sqlite"), "work_root": str(local / "work")},
            "render": {"logo": str(LOGO), "fonts": [
                {"label": "DejaVuSans", "path": str(FONT_FILES["DejaVuSans"]), "weight": "400"},
                {"label": "DejaVuSans-Bold", "path": str(FONT_FILES["DejaVuSans-Bold"]), "weight": "700"}]},
            "execution": {"inter_client_pause_seconds": 0, "client_timeout_seconds": 80000,
                          "batch_deadline_seconds": 600000, "lock_ttl_seconds": 600, **execution}}
    return parse_settings(data, environ={"LOCALAPPDATA": str(local)})


def make_world(tmp_path, *, names=("Example Co",), collect=default_collect, renderer="stub", clock=None,
               services_options=None, execution=None, clients=None):
    clock = clock or Clock()
    settings = make_settings(tmp_path, **(execution or {}))
    registry = ClientRegistry([ClientEntry(name, key_of(name)) for name in names])
    loaded = clients if clients is not None else LoadResult(
        {key_of(n): ClientRun(key_of(n), make_client(n), CURRENT) for n in names}, (), ())
    options = dict(settings=settings, registry=registry, clients=loaded, clock=clock, collect=collect,
                   renderer=StubRenderer() if renderer == "stub" else renderer, sleep=lambda s: None)
    options.update(services_options or {})
    services = pipeline.Services(**options)
    ledger = ledger_module.Ledger(settings.ledger_db_path, clock=clock)
    return SimpleNamespace(settings=settings, registry=registry, services=services, ledger=ledger, clock=clock,
                           tmp=tmp_path, names=names)


@pytest.fixture
def world(tmp_path):
    made = []

    def build(**kwargs):
        w = make_world(tmp_path, **kwargs)
        made.append(w)
        return w
    yield build
    for w in made:
        w.ledger.close()


_COUNTER = itertools.count(1)


def req(client_key="example-co", *, action="run", key=None, period_id=PERIOD_ID, replace_existing=False, when=None):
    n = next(_COUNTER)
    return contracts.RunRequest(
        request_id=str(uuid.UUID(int=n)), idempotency_key=key or f"test:{client_key}:{action}:{n}", client_key=client_key,
        period_id=period_id, action=action, requested_at=when or "2026-11-02T00:59:00+00:00",
        requested_by="make:test", replace_existing=replace_existing)


def go(w, request=None, **kw):
    request = request or req()
    return pipeline.run_client(request.client_key, request.period_id, services=kw.pop("services", w.services),
                               ledger=w.ledger, request=request, **kw)


def paths(w, client_key="example-co"):
    root = w.settings.output_root
    s, e = CURRENT.current.start, CURRENT.current.end
    return (layout.report_dir(root, client_key, s, e), layout.reviewer_dir(root, client_key, s, e),
            layout.attempt_dir(root, client_key, s, e))


def tree(folder):
    folder = Path(folder)
    if not folder.exists():
        return None
    return {str(p.relative_to(folder)): (p.read_bytes() if p.is_file() else None) for p in sorted(folder.rglob("*"))}


class NthOp(ff.FileOps):
    """Fails the nth call (1-based) of one file operation."""

    def __init__(self, method, n):
        self.method, self.n, self.count = method, n, 0

    def _tick(self, name):
        if name == self.method:
            self.count += 1
            if self.count == self.n:
                raise OSError(5, "injected")

    def create_exclusive(self, path):
        self._tick("create_exclusive")
        return super().create_exclusive(path)

    def rename_noreplace(self, source, destination):
        self._tick("rename_noreplace")
        super().rename_noreplace(source, destination)

    def write(self, handle, data):
        self._tick("write")
        super().write(handle, data)


def flaky_store(method):
    """A snapshot store whose `write` or `add_event` fails once."""
    state = {"armed": True}

    def factory(path, **kwargs):
        class Store(st.SnapshotStore):
            def write(self, snapshot, **kw):
                if method == "write" and state["armed"]:
                    state["armed"] = False
                    raise st.SnapshotStoreError("injected")
                return super().write(snapshot, **kw)

            def add_event(self, run_id, kind, detail=None):
                if method == "add_event" and state["armed"]:
                    state["armed"] = False
                    raise st.SnapshotStoreError("injected")
                return super().add_event(run_id, kind, detail)
        return Store(path, **kwargs)
    return factory


def states(w, run_key=None):
    return [(e.from_state, e.to_state) for e in w.ledger.events_for(run_key or f"example-co/{PERIOD_ID}")
            if e.kind in ("state_change", "recovery")]


RUN_KEY = f"example-co/{PERIOD_ID}"


# --- success ----------------------------------------------------------------------------------------------------------------

def test_a_run_reaches_finalized_and_audited_status_with_every_identity_recorded(world):
    w = world()
    response = go(w)
    assert (response.state, response.outcome, response.ok) == ("audited", "succeeded", True)
    contracts.validate_response(response.to_dict())
    run = w.ledger.get_run(RUN_KEY)
    assert run.state == "audited" and run.attempt_count == 1 and run.error_category is None
    reports, reviewer, _ = paths(w)
    pdf_files = {p.name: p.read_bytes() for p in reports.iterdir()}
    assert set(pdf_files) == {"report.pdf", ra.sidecar_name(run.render_input_sha256)}
    record = rw.parse_record_bytes((reviewer / run.record_name).read_bytes())
    fin = record["finalization"]
    assert record["schema_version"] == 2 and record["snapshot"]["run_id"] == run.engine_run_id
    assert (fin["render_input_sha256"], fin["pdf_sha256"], fin["pdf_bytes"]) == (
        run.render_input_sha256, run.pdf_sha256, run.pdf_bytes)
    assert run.pdf_sha256 == hashlib.sha256((reports / "report.pdf").read_bytes()).hexdigest()
    sidecar = ra.read_sidecar(reports, ra.sidecar_name(run.render_input_sha256))
    assert (sidecar.pdf_sha256, sidecar.pdf_bytes, sidecar.generation_date.isoformat()) == (
        run.pdf_sha256, run.pdf_bytes, run.generation_date)
    assert (run.audit_outcome, run.audit_finalized_valid, run.audit_verification_complete) == ("consistent", 1, 1)
    assert response.identity["render_input_sha256"] == run.render_input_sha256 and response.run_id == run.engine_run_id


def test_the_state_history_is_the_documented_chain_and_every_change_is_queued_for_status(world):
    w = world()
    go(w)
    assert states(w) == [(None, "requested"), ("requested", "collecting"), ("collecting", "rendering"),
                         ("rendering", "rendered"), ("rendered", "finalizing"), ("finalizing", "finalized"),
                         ("finalized", "auditing"), ("auditing", "audited")]
    outbox = w.ledger.all_outbox()
    assert [json.loads(o.payload_json)["state"] for o in outbox] == [
        "collecting", "rendering", "rendered", "finalizing", "finalized", "auditing", "audited"]
    for row in outbox:
        contracts.validate_response(json.loads(row.payload_json))
    assert len({o.dedupe_key for o in outbox}) == len(outbox)


def test_the_pipeline_stops_before_delivery(world):
    w = world()
    response = go(w)
    assert response.state == "audited" and response.delivery is None
    assert not any(r.state in ("publishing", "ready_for_delivery", "delivered") for r in w.ledger.list_runs())
    assert w.ledger._con.execute("SELECT COUNT(*) FROM deliveries").fetchone()[0] == 0
    ready = dataclasses.replace(response, state="ready_for_delivery", audit=None).to_dict()
    with pytest.raises(contracts.ContractError):  # a delivery-ready claim without a passing audit is refused
        contracts.validate_response(ready)


def test_a_run_without_a_request_object_is_recorded_like_any_other(world):
    w = world()
    response = pipeline.run_client("example-co", PERIOD_ID, services=w.services, ledger=w.ledger)
    assert response.state == "audited"
    assert w.ledger.requests_by_key(response.idempotency_key).status == "completed"
    assert response.idempotency_key.startswith("manual:example-co/")


def test_responses_hold_no_client_data_paths_or_secrets(world):
    w = world()
    text = json.dumps(go(w).to_dict()) + "".join(o.payload_json for o in w.ledger.all_outbox())
    text += "".join(e.detail_json + e.code for e in w.ledger.events_for())
    for secret in ("Example Co", str(w.tmp), "sender@example.test", "example.test", "123456789", "C:\\"):
        assert secret not in text


# --- sources: unavailable data blocks everything after collection ---------------------------------------------------------------

def failing_collect(gsc_error=None, ga4_error=None):
    def collect(client_run, today):
        def fetch_gsc(service, site, periods, **kw):
            if gsc_error:
                raise gsc_error
            return dataclasses.replace(GSC, site_url=site)

        def fetch_ga4(client, prop, periods, **kw):
            if ga4_error:
                raise ga4_error
            return dataclasses.replace(GA4, property_id=str(prop))
        return sc.collect_sources(client_run.client, client_run.periods, gsc_service=None, ga4_client=None, today=today,
                                  fetch_gsc=fetch_gsc, fetch_ga4=fetch_ga4)
    return collect


@pytest.mark.parametrize("gsc_error,ga4_error,category,code", [
    (gsc_source.SearchConsoleError("x", 403), None, "source_denied", "gsc_http_403"),
    (None, ga4_source.Ga4Error("x", 404), "source_denied", "ga4_http_404"),
    (gsc_source.SearchConsoleError("x", 503), None, "source_temporary", "gsc_http_503"),
    (None, ga4_source.Ga4Error("x", incompatible=True), "source_incompatible", "ga4_schema_incompatible"),
    (RefreshError("x"), None, "auth_required", "auth_refresh_failed"),
    (gsc_source.SearchConsoleError("x", 403), ga4_source.Ga4Error("x", 503), "source_temporary", "ga4_http_503")])
def test_a_wholly_unavailable_source_fails_the_run_before_any_report_exists(world, gsc_error, ga4_error, category, code):
    w = world(collect=failing_collect(gsc_error, ga4_error))
    response = go(w)
    assert (response.state, response.outcome) == ("failed", "failed")
    assert (response.error["category"], response.error["code"], response.error["stage"]) == (
        category, code, "collecting")
    contracts.validate_response(response.to_dict())
    assert response.retry["eligible"] == contracts.ERROR_CATEGORIES[category][0]
    reports, reviewer, _ = paths(w)
    assert not any(reports.iterdir()) and not any(reviewer.iterdir())  # nothing rendered or finalized
    assert not w.settings.history_db_path.exists()  # no snapshot was stored
    assert w.ledger.get_run(RUN_KEY).state == "failed"


def test_a_failed_run_has_no_delivery_readiness_and_is_not_audited(world):
    w = world(collect=failing_collect(gsc_source.SearchConsoleError("x", 403)))
    response = go(w)
    run = w.ledger.get_run(RUN_KEY)
    assert response.delivery is None and response.audit is None and run.audit_outcome is None
    assert run.state not in ("audited", "publishing", "ready_for_delivery", "delivered")


def test_a_valid_zero_result_response_is_not_unavailable(world):
    def collect(client_run, today):
        empty = FakeService(lambda body: {})
        return sc.collect_sources(
            client_run.client, client_run.periods, gsc_service=empty, ga4_client=None, today=today,
            sleep=lambda s: None, fetch_ga4=lambda client, prop, periods, **kw: dataclasses.replace(
                GA4, property_id=str(prop)))
    w = world(collect=collect)
    response = go(w)
    assert response.state == "audited" and response.outcome == "succeeded"
    assert {"code": "source_empty", "source": "gsc"} in response.warnings


def test_incomplete_data_is_reported_as_a_warning_and_the_run_continues(world):
    def collect(client_run, today):
        def fetch_gsc(service, site, periods, **kw):
            current = dataclasses.replace(GSC.current, warnings=[gsc_source.DataWarning("incomplete_data", "partial")])
            return dataclasses.replace(GSC, site_url=site, current=current)
        return sc.collect_sources(client_run.client, client_run.periods, gsc_service=None, ga4_client=None,
                                  today=today, fetch_gsc=fetch_gsc,
                                  fetch_ga4=lambda c, p, periods, **kw: dataclasses.replace(GA4, property_id=str(p)))
    w = world(collect=collect)
    response = go(w)
    assert response.state == "audited" and {"code": "source_incomplete", "source": "gsc"} in response.warnings


def test_a_response_for_the_wrong_property_is_invalid_not_used(world):
    def collect(client_run, today):
        return sc.collect_sources(
            client_run.client, client_run.periods, gsc_service=None, ga4_client=None, today=today,
            fetch_gsc=lambda s, site, periods, **kw: dataclasses.replace(GSC, site_url="https://other.test/"),
            fetch_ga4=lambda c, p, periods, **kw: dataclasses.replace(GA4, property_id=str(p)))
    w = world(collect=collect)
    response = go(w)
    assert response.error["category"] == "source_invalid" and response.error["code"] == "gsc_response_invalid"


# --- validation and admission ---------------------------------------------------------------------------------------------------

def test_an_unknown_client_and_a_different_period_are_rejected_without_a_run(world):
    w = world()
    unknown = go(w, req("nobody"))
    assert (unknown.outcome, unknown.error["code"]) == ("rejected", "client_unknown")
    other_period = contracts.period_id_of(CURRENT.current.start + dt.timedelta(days=1),
                                          CURRENT.current.end + dt.timedelta(days=1))
    moved = go(w, req(period_id=other_period))
    assert (moved.outcome, moved.error["code"]) == ("rejected", "period_differs_from_workbook")
    assert w.ledger.list_runs() == []


def test_a_client_rejected_by_the_loader_is_reported_as_config_invalid(world):
    from seo_report.client_loader import Rejection
    w = world(clients=LoadResult({}, (Rejection("example-co", 2, "ga4_property_missing"),), ()))
    response = go(w)
    assert (response.outcome, response.error["category"], response.error["code"]) == (
        "rejected", "config_invalid", "ga4_property_missing")


def test_publish_and_dry_run_actions_are_not_production_runs(world):
    w = world()
    assert go(w, req(action="publish")).error["code"] == "publish_not_supported"
    assert go(w, req(action="dry_run")).error["code"] == "action_not_supported"
    assert w.ledger.list_runs() == []


def test_validate_and_status_do_not_change_runs(world):
    w = world()
    validated = go(w, req(action="validate"))
    assert (validated.mode, validated.outcome) == ("read_only", "succeeded") and w.ledger.list_runs() == []
    assert go(w, req(action="status")).error["code"] == "no_run"
    go(w)
    status = go(w, req(action="status"))
    assert (status.mode, status.state, status.outcome) == ("read_only", "audited", "succeeded")


def test_a_retry_needs_a_failed_run_and_a_run_request_needs_retry_for_failed_runs(world):
    w = world(names=("Example Co", "Other Co"), collect=failing_collect(gsc_source.SearchConsoleError("x", 503)))
    assert go(w, req(action="retry")).error["code"] == "no_run"
    assert go(w).state == "failed"
    again = go(w)  # a plain run on a failed, retry-eligible run is refused: the retry is an explicit, auditable action
    assert (again.outcome, again.error["code"]) == ("rejected", "use_retry")
    other_key = f"other-co/{PERIOD_ID}"  # blocked runs need an operator, for both actions
    w.ledger.create_run("other-co", PERIOD_ID)
    run = w.ledger.get_run(other_key)
    run = w.ledger.transition(other_key, "collecting", expected_state="requested", expected_version=run.state_version,
                              code="x")
    w.ledger.transition(other_key, "blocked", expected_state="collecting", expected_version=run.state_version, code="x",
                        values={"failed_stage": "collecting", "error_category": "audit_failed", "error_code": "x"})
    for action in ("retry", "run"):
        assert go(w, req("other-co", action=action)).error["code"] == "operator_required"


# --- duplicates and concurrency -----------------------------------------------------------------------------------------------

def test_the_same_request_twice_is_replayed_and_changes_nothing(world):
    w = world()
    request = req()
    first = go(w, request)
    snapshot = (tree(w.settings.output_root), w.settings.history_db_path.read_bytes(), len(w.ledger.events_for()),
                len(w.ledger.all_outbox()))
    again = go(w, request)
    assert again.replayed is True and again.state == "audited"
    assert dataclasses.replace(again, replayed=False) == first
    assert (tree(w.settings.output_root), w.settings.history_db_path.read_bytes(), len(w.ledger.events_for()),
            len(w.ledger.all_outbox())) == snapshot


def test_a_new_request_for_a_finished_run_returns_the_result_without_regenerating(world):
    w = world()
    first = go(w)
    run = w.ledger.get_run(RUN_KEY)
    before = (tree(w.settings.output_root), w.settings.history_db_path.read_bytes())
    second = go(w)
    assert second.outcome == "already_complete" and second.ok and second.state == "audited"
    assert second.identity == first.identity and w.ledger.get_run(RUN_KEY).state_version == run.state_version
    assert (tree(w.settings.output_root), w.settings.history_db_path.read_bytes()) == before
    assert len(w.ledger.list_runs()) == 1


def test_one_idempotency_key_cannot_be_reused_for_another_action(world):
    w = world()
    request = req(key="shared-key")
    go(w, request)
    clash = dataclasses.replace(req(), idempotency_key="shared-key", action="status")
    with pytest.raises(ledger_module.LedgerError, match="idempotency_key_reused"):
        go(w, clash)


def test_a_client_that_is_already_being_processed_is_not_started_twice(world):
    w = world()
    other = ledger_module.Ledger(w.settings.ledger_db_path, clock=w.clock, pid_alive=lambda pid: True)
    try:
        other.acquire_lock("client:example-co", ttl_seconds=600)
        request = req()
        response = go(w, request)
        assert (response.outcome, response.error["category"]) == ("rejected", "run_in_progress")
        assert w.ledger.list_runs() == [] and not w.settings.history_db_path.exists()
        assert w.ledger.get_request(request.request_id).status == "accepted"  # not final: the same key may be tried again
        other.release_lock("client:example-co")
        assert go(w, request).state == "audited"
    finally:
        other.close()


def test_the_runner_wide_lock_also_blocks_a_second_process(world):
    w = world()
    other = ledger_module.Ledger(w.settings.ledger_db_path, clock=w.clock, pid_alive=lambda pid: True)
    try:
        other.acquire_lock("runner:global", ttl_seconds=600)
        assert go(w).error["category"] == "run_in_progress"
    finally:
        other.close()


def test_locks_are_released_after_every_run(world):
    w = world(collect=failing_collect(gsc_source.SearchConsoleError("x", 403)))
    go(w)
    assert w.ledger.get_lock("client:example-co") is None and w.ledger.get_lock("runner:global") is None


# --- render identity and retries --------------------------------------------------------------------------------------------------

def test_a_retry_after_an_interrupted_draft_handoff_keeps_the_original_render_identity(world):
    w = world(services_options={"file_ops": NthOp("write", 2)})  # write 1 is the sidecar, write 2 the draft copy
    first = go(w)
    assert (first.state, first.error["category"], first.error["code"]) == ("failed", "render_failed", "handoff_incomplete")
    run = w.ledger.get_run(RUN_KEY)
    reports, _, _ = paths(w)
    sidecars = [p for p in reports.iterdir() if p.name.startswith("report.render-input.")]
    assert len(sidecars) == 1 and run.generation_date == "2026-11-02"
    sidecar_bytes = sidecars[0].read_bytes()
    w.clock.advance(days=3)  # the retry happens on another day
    clean = dataclasses.replace(w.services, file_ops=None)
    retried = go(w, req(action="retry"), services=clean)
    # finalization completed with the original identity; the audit then blocks delivery readiness because the engine
    # keeps the partial draft file left by the interruption (it never deletes anything) and the audit reports it
    assert (retried.state, retried.error["category"]) == ("blocked", "audit_failed")
    run = w.ledger.get_run(RUN_KEY)
    assert run.generation_date == "2026-11-02" and run.attempt_count == 2 and run.record_name
    assert sidecars[0].read_bytes() == sidecar_bytes
    assert ra.read_sidecar(reports, sidecars[0].name).generation_date == dt.date(2026, 11, 2)
    assert sidecars[0].name == ra.sidecar_name(run.render_input_sha256)
    assert [p.name for p in reports.iterdir() if ff.is_partial_name(p.name)]  # kept, not deleted


@pytest.mark.parametrize("boundary", ["snapshot", "event", "promotion"])
def test_an_interrupted_finalization_is_retried_with_the_original_identity(world, boundary):
    options = {"snapshot": {"store_factory": flaky_store("write")}, "event": {"store_factory": flaky_store("add_event")},
               "promotion": {"file_ops": NthOp("rename_noreplace", 3)}}[boundary]
    w = world(services_options=options)
    first = go(w)
    assert (first.state, first.error["category"]) == ("failed", "finalization_failed")
    assert first.retry == {"eligible": True, "kind": "finalization", "after": first.retry["after"]}
    run = w.ledger.get_run(RUN_KEY)
    finalized_at, generation, render_hash = run.finalized_at, run.generation_date, run.render_input_sha256
    assert finalized_at and generation and render_hash
    w.clock.advance(hours=5)
    retried = go(w, req(action="retry"), services=dataclasses.replace(w.services, file_ops=None) if boundary == "promotion"
                 else w.services)
    assert retried.state == "audited", retried.error
    run = w.ledger.get_run(RUN_KEY)
    assert (run.finalized_at, run.generation_date, run.render_input_sha256) == (finalized_at, generation, render_hash)
    assert run.attempt_count == 2
    _, reviewer, _ = paths(w)
    assert len([p for p in reviewer.iterdir() if p.name.startswith("reviewer.")]) == 1
    record = rw.parse_record_bytes((reviewer / run.record_name).read_bytes())
    assert record["finalization"]["finalized_at"] == finalized_at


def test_an_interrupted_record_write_leaves_a_partial_that_blocks_delivery_readiness(world):
    w = world(services_options={"file_ops": NthOp("rename_noreplace", 4)})  # sidecar, draft, PDF, then the record
    first = go(w)
    assert first.state == "failed" and first.error["category"] == "finalization_failed"
    retried = go(w, req(action="retry"), services=dataclasses.replace(w.services, file_ops=None))
    # the engine never deletes the partial record; the audit reports it, so the run cannot become delivery-ready
    assert (retried.state, retried.outcome, retried.error["category"]) == ("blocked", "blocked", "audit_failed")
    run = w.ledger.get_run(RUN_KEY)
    assert run.audit_outcome is None and run.state == "blocked"


def test_a_crash_in_the_middle_of_finalization_is_recovered_by_the_next_process(world, monkeypatch):
    w = world(services_options={"store_factory": lambda *a, **k: (_ for _ in ()).throw(KeyboardInterrupt())})
    monkeypatch.setattr(ledger_module.Ledger, "release_lock", lambda self, name: None)  # the "dead" process kept its locks
    with pytest.raises(KeyboardInterrupt):
        go(w)
    run = w.ledger.get_run(RUN_KEY)
    assert run.state == "finalizing"
    original = (run.generation_date, run.finalized_at, run.render_input_sha256)
    monkeypatch.undo()
    w.clock.advance(hours=2)  # leases expire
    survivor = ledger_module.Ledger(w.settings.ledger_db_path, clock=w.clock, pid_alive=lambda pid: False)
    try:
        services = dataclasses.replace(w.services, store_factory=None)
        response = pipeline.run_client("example-co", PERIOD_ID, services=services, ledger=survivor, request=req())
        assert response.state == "audited", response.error
        run = survivor.get_run(RUN_KEY)
        assert (run.generation_date, run.finalized_at, run.render_input_sha256) == original
        kinds = [(e.kind, e.code) for e in survivor.events_for(RUN_KEY)]
        assert ("recovery", "interrupted_run_recovered") in kinds
        assert [a.outcome for a in survivor.attempts_for(RUN_KEY)] == ["interrupted", "succeeded"]  # the resume is attempt 2
        assert run.attempt_count == 2
    finally:
        survivor.close()


def test_a_crash_right_after_the_engine_finalized_is_recovered_without_a_second_finalization(world, monkeypatch):
    from seo_report import finalize_service
    w = world()
    real = finalize_service.finalize

    def finalize_then_die(*args, **kwargs):
        real(*args, **kwargs)  # the engine completed (snapshot, event, PDF, record) ...
        raise KeyboardInterrupt()  # ... but the process died before the ledger heard about it

    monkeypatch.setattr(finalize_service, "finalize", finalize_then_die)
    monkeypatch.setattr(ledger_module.Ledger, "release_lock", lambda self, name: None)
    with pytest.raises(KeyboardInterrupt):
        go(w)
    assert w.ledger.get_run(RUN_KEY).state == "finalizing"
    reports, reviewer, _ = paths(w)
    records_before = sorted(p.name for p in reviewer.iterdir())
    pdf_before = (reports / "report.pdf").read_bytes()
    monkeypatch.undo()
    w.clock.advance(hours=2)
    survivor = ledger_module.Ledger(w.settings.ledger_db_path, clock=w.clock, pid_alive=lambda pid: False)
    try:
        response = pipeline.run_client("example-co", PERIOD_ID, services=w.services, ledger=survivor, request=req())
        assert response.state == "audited", response.error
        run = survivor.get_run(RUN_KEY)
        assert sorted(p.name for p in reviewer.iterdir()) == records_before and (reports / "report.pdf").read_bytes() == pdf_before
        assert run.record_name in records_before
        events = w.settings.history_db_path
        assert events.exists() and len(survivor.attempts_for(RUN_KEY)) == 2
    finally:
        survivor.close()

def test_a_crash_while_collecting_is_recovered_and_the_run_restarts_from_the_beginning(world, monkeypatch):
    def boom(client_run, today):
        raise KeyboardInterrupt()
    w = world(collect=boom)
    monkeypatch.setattr(ledger_module.Ledger, "release_lock", lambda self, name: None)
    with pytest.raises(KeyboardInterrupt):
        go(w)
    assert w.ledger.get_run(RUN_KEY).state == "collecting"
    monkeypatch.undo()
    w.clock.advance(hours=2)
    survivor = ledger_module.Ledger(w.settings.ledger_db_path, clock=w.clock, pid_alive=lambda pid: False)
    try:
        response = pipeline.run_client("example-co", PERIOD_ID, ledger=survivor, request=req(),
                                       services=dataclasses.replace(w.services, collect=default_collect))
        assert response.state == "audited"
    finally:
        survivor.close()


def pinned_process_start(monkeypatch, clock, offset):
    """The lock holder in these tests is this very test process, so the ledger reads ITS real creation time (real system time) and compares it with the
    lock's acquisition time (the fake clock). Pin the creation time on the fake clock's timeline, so the PID-reuse rule sees the same history whatever the
    real date is. `offset` is relative to the fake clock's first reading, which is when the lock is acquired."""
    acquired = clock.now
    monkeypatch.setattr(ledger_module, "process_start_time", lambda pid, api=None: acquired + offset)


@pytest.mark.parametrize("fake_start", [dt.datetime(2020, 1, 1, 1, 0, 0, tzinfo=UTC), dt.datetime(2026, 11, 2, 1, 0, 0, tzinfo=UTC),
                                        dt.datetime(2040, 6, 1, 1, 0, 0, tzinfo=UTC)])
def test_a_second_process_does_not_take_over_a_live_holder_even_after_the_lease_expired(world, monkeypatch, fake_start):
    clock = Clock(start=fake_start)
    pinned_process_start(monkeypatch, clock, -dt.timedelta(days=1))  # the holder was running long before it took the lock
    w = world(clock=clock)
    other = ledger_module.Ledger(w.settings.ledger_db_path, clock=w.clock, pid_alive=lambda pid: True, owner_id="other")
    try:
        other.acquire_lock("client:example-co", ttl_seconds=1)
        w.clock.advance(hours=1)
        assert go(w).error["category"] == "run_in_progress"
    finally:
        other.close()


def test_a_process_id_created_after_the_lock_was_taken_is_treated_as_reused_whatever_the_real_date(world, monkeypatch):
    """The other half of the same rule, on the same fake timeline: a live process that started AFTER the lock was acquired cannot be its holder."""
    clock = Clock()
    pinned_process_start(monkeypatch, clock, dt.timedelta(minutes=30))
    w = world(clock=clock)
    other = ledger_module.Ledger(w.settings.ledger_db_path, clock=w.clock, pid_alive=lambda pid: True, owner_id="other")
    try:
        other.acquire_lock("client:example-co", ttl_seconds=1)
        w.clock.advance(hours=1)
        assert go(w).error is None  # the lease is expired and the holder's id was reused: the lock is taken over
    finally:
        other.close()


# --- the audit gate -------------------------------------------------------------------------------------------------------------------

def test_a_failed_audit_blocks_the_run_and_never_claims_readiness(world):
    w = world()
    reports, _, _ = paths(w)
    reports.mkdir(parents=True)
    (reports / ff.partial_name("report.pdf", "0123456789ab")).write_bytes(b"stray")  # attention-level finding
    response = go(w)
    assert (response.state, response.outcome, response.error["category"]) == ("blocked", "blocked", "audit_failed")
    assert response.audit is None and response.delivery is None
    assert w.ledger.get_run(RUN_KEY).state == "blocked"


# --- errors: classified, logged, and never swallowed beyond need --------------------------------------------------------------------

def test_an_unexpected_programming_error_is_logged_with_its_traceback_and_returned_as_internal_error(world, caplog):
    def collect(client_run, today):
        raise ValueError("secret-detail-from-a-bug")
    w = world(collect=collect)
    with caplog.at_level(logging.ERROR, logger="seo_report.pipeline"):
        response = go(w)
    assert (response.state, response.error["category"], response.error["code"]) == (
        "failed", "internal_error", "exception_value_error")
    assert "secret-detail-from-a-bug" not in json.dumps(response.to_dict())
    assert any(r.exc_info and "secret-detail-from-a-bug" in str(r.exc_info[1]) for r in caplog.records)
    assert w.ledger.get_run(RUN_KEY).state == "failed"


def test_keyboard_interrupt_and_system_exit_are_not_caught(world):
    for exception in (KeyboardInterrupt, SystemExit):
        def collect(client_run, today, exception=exception):
            raise exception()
        w = world(collect=collect)
        with pytest.raises(exception):
            go(w)


def test_the_client_deadline_is_a_timeout_failure(world):
    clock = Clock(step=1000.0)
    w = world(clock=clock, execution={"client_timeout_seconds": 10})
    response = go(w)
    assert (response.outcome, response.error["category"], response.error["code"]) == ("failed", "timeout", "client_deadline")
    assert response.retry["eligible"] is True and response.retry["kind"] == "collection"
    assert w.ledger.get_run(RUN_KEY).state == "requested"  # no stage had started, nothing was recorded as failed


# --- dry run ------------------------------------------------------------------------------------------------------------------------

def test_a_dry_run_writes_only_to_its_scratch_folder(world, tmp_path):
    w = world()
    scratch = tmp_path / "scratch"
    response = pipeline.run_client("example-co", PERIOD_ID, services=w.services, ledger=None, mode="dry_run",
                                   scratch_dir=scratch)
    assert (response.mode, response.state, response.outcome) == ("dry_run", "rendered", "succeeded")
    contracts.validate_response(response.to_dict())
    assert response.run_id is None and response.identity["record_name"] is None
    assert not w.settings.output_root.exists() and not w.settings.history_db_path.exists()
    assert not w.settings.work_root.exists()
    assert w.ledger.list_runs() == [] and w.ledger.all_outbox() == []
    produced = sorted(str(p.relative_to(scratch)) for p in scratch.rglob("*") if p.is_file())
    assert any(name.endswith("report.unarchived.pdf") for name in produced)
    assert any("report.render-input." in name for name in produced)
    assert not any(Path(name).parent.name == "reports" and Path(name).name == "report.pdf" for name in produced)
    assert not any(Path(name).parent.name == "reviewer" for name in produced)  # nothing was finalized


def test_a_dry_run_never_touches_existing_production_storage(world, tmp_path):
    w = world()
    go(w)  # a real run first
    before = (tree(w.settings.output_root), w.settings.history_db_path.read_bytes(), w.settings.ledger_db_path.read_bytes())
    runs_before, events_before = w.ledger.list_runs(), len(w.ledger.events_for())
    response = pipeline.run_client("example-co", PERIOD_ID, services=w.services, ledger=w.ledger, mode="dry_run",
                                   scratch_dir=tmp_path / "scratch")
    assert response.outcome == "succeeded" and response.mode == "dry_run"
    assert (tree(w.settings.output_root), w.settings.history_db_path.read_bytes()) == before[:2]
    assert len(w.ledger.events_for()) == events_before and w.ledger.list_runs() == runs_before


def test_repeated_dry_runs_use_separate_folders(world, tmp_path):
    w = world()
    for _ in range(2):
        assert pipeline.run_client("example-co", PERIOD_ID, services=w.services, ledger=None, mode="dry_run",
                                   scratch_dir=tmp_path / "scratch").outcome == "succeeded"
    assert len([p for p in (tmp_path / "scratch").iterdir()]) == 2


def test_a_dry_run_refuses_scratch_folders_inside_production_storage(world):
    w = world()
    for bad in (w.settings.output_root / "scratch", w.settings.ledger_db_path.parent / "s", w.settings.work_root / "s",
                ROOT / "scratch", Path("relative"), None):
        response = pipeline.run_client("example-co", PERIOD_ID, services=w.services, ledger=None, mode="dry_run",
                                       scratch_dir=bad)
        assert (response.outcome, response.error["code"]) == ("rejected", "scratch_dir_invalid")
    assert not w.settings.output_root.exists()


def test_a_dry_run_reports_source_failures_without_side_effects(world, tmp_path):
    w = world(collect=failing_collect(gsc_source.SearchConsoleError("x", 403)))
    response = pipeline.run_client("example-co", PERIOD_ID, services=w.services, ledger=None, mode="dry_run",
                                   scratch_dir=tmp_path / "scratch")
    assert (response.outcome, response.error["category"]) == ("failed", "source_denied")
    assert not w.settings.output_root.exists() and not w.settings.history_db_path.exists()


def test_unknown_modes_are_refused(world):
    w = world()
    with pytest.raises(ValueError):
        pipeline.run_client("example-co", PERIOD_ID, services=w.services, ledger=w.ledger, mode="staging")


# --- source checks ------------------------------------------------------------------------------------------------------------------

def test_the_pipeline_and_runner_use_no_threads_network_or_delivery_code():
    import ast
    forbidden = {"threading", "concurrent", "multiprocessing", "asyncio", "requests", "urllib", "http", "smtplib",
                 "socket", "shutil", "googleapiclient"}
    for name in ("pipeline", "runner", "ledger"):
        tree_ = ast.parse((ROOT / "src" / "seo_report" / f"{name}.py").read_text(encoding="utf-8"))
        imported = set()
        for node in ast.walk(tree_):
            if isinstance(node, ast.Import):
                imported |= {a.name.split(".")[0] for a in node.names}
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        # R8.4.4: the ledger owns the one bounded lease-renewal thread (`LeaseKeeper`); nothing else here may use threads
        allowed = {"socket", "threading"} if name == "ledger" else set()
        assert not (imported & (forbidden - allowed)), (name, imported & forbidden)
        calls = {n.func.attr for n in ast.walk(tree_) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
        assert not (calls & {"remove", "unlink", "rmtree", "rmdir"}), (name, calls)


# --- real Chromium ---------------------------------------------------------------------------------------------------------------------

def chromium_available() -> bool:
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as playwright:
            playwright.chromium.launch().close()
        return True
    except Exception:
        return False


def test_real_chromium_integration_reaches_audited_with_a_real_pdf(world):
    if not chromium_available():
        pytest.skip("Chromium could not be started")
    w = world(renderer=None)  # the real renderer
    response = go(w)
    assert (response.state, response.outcome) == ("audited", "succeeded"), response.error
    reports, _, _ = paths(w)
    data = (reports / "report.pdf").read_bytes()
    info = pv.inspect_pdf(data)
    print(f"PIPELINE REAL CHROMIUM: {info.page_count} A4 pages, {len(data)} bytes")
    assert info.page_count >= 3 and all(pv.is_a4(box) for box in info.page_boxes) and info.xref_consistent
    run = w.ledger.get_run(RUN_KEY)
    assert (run.pdf_bytes, run.audit_outcome) == (len(data), "consistent")
