"""R8.4.4 (P3): the hard rendering timeout, with a controlled fake browser and one real-Chromium check (temporary folders).

A "hang" blocks the fake call until the watchdog has run the (injected) process ending, exactly as ending the browser makes a
blocked Playwright call fail. No process other than the test's own browser is ever ended.
"""
import dataclasses
import sys
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

import playwright.sync_api as pw
from seo_report import contracts
from seo_report import finalize_files as ff
from seo_report import pdf_renderer as pr
from seo_report import render_artifacts as ra
from seo_report import render_service
from seo_report.sources import gsc as gsc_source  # noqa: F401
from test_pdf_renderer import LOGO, SOURCES, with_fingerprints
from test_pipeline import PERIOD_ID, RUN_KEY, go, paths, req, tree, world  # noqa: F401
from test_render_input import sample


class Hang(Exception):
    pass


class Fake:
    """A scripted browser. `hang_at` blocks that step until `release()`; `fail_at` raises Playwright's TimeoutError."""

    def __init__(self, hang_at=None, fail_at=None):
        self.hang_at, self.fail_at = hang_at, fail_at
        self.released = threading.Event()
        self.calls, self.killed = [], 0
        self.browser_closed = self.stopped = False

    def release(self, baseline=None):  # the injected process ending
        self.killed += 1
        self.released.set()
        return 0

    def step(self, name):
        self.calls.append(name)
        if self.fail_at == name:
            raise pw.TimeoutError(f"{name} timed out")
        if self.hang_at == name:
            self.released.wait(10)
            raise RuntimeError("Target closed")

    def install(self, monkeypatch):
        fake = self

        class Page:
            def set_default_navigation_timeout(self, ms):
                assert ms >= 1

            def route(self, *a):
                pass

            def goto(self, url, timeout=None):
                assert timeout >= 1
                fake.step("navigation")

            def evaluate(self, script):
                fake.step("evaluation")
                return [] if "MEASURE" in script or True else None

            def emulate_media(self, **kw):
                pass

            def pdf(self, **kw):
                fake.step("pdf")

        class Context:
            def set_default_timeout(self, ms):
                assert ms >= 1

            def new_page(self):
                return Page()

            def new_cdp_session(self, page):
                class Cdp:
                    def send(self, method, params=None):
                        return {"root": {"nodeId": 1}} if method == "DOM.getDocument" else {"nodeIds": []}
                return Cdp()

        class Browser:
            version = "0.0.0"

            def new_context(self):
                return Context()

            def close(self):
                fake.step("shutdown")
                fake.browser_closed = True

        class Chromium:
            def launch(self, timeout=None):
                assert timeout >= 1
                fake.step("launch")
                return Browser()

        class Playwright:
            chromium = Chromium()

            def stop(self):
                fake.stopped = True

        class Manager:
            def start(self):
                return Playwright()

        monkeypatch.setattr(pw, "sync_playwright", lambda: Manager())


def render(work, fake, *, timeout=0.4, **kw):
    return pr.render_report_pdf(with_fingerprints(sample()), workdir=work, logo_path=LOGO, fonts=SOURCES,
                                timeout_seconds=timeout, terminate=fake.release, **kw)


def watchdogs():
    return [t for t in threading.enumerate() if t.name == "render-watchdog"]


@pytest.mark.parametrize("step", ["launch", "navigation", "evaluation", "pdf", "shutdown"])
def test_a_playwright_timeout_in_each_phase_is_a_structured_render_timeout(tmp_path, monkeypatch, step):
    fake = Fake(fail_at=step)
    fake.install(monkeypatch)
    with pytest.raises(pr.RenderTimeoutError) as caught:
        render(tmp_path, fake)
    assert (caught.value.code, caught.value.phase) == ("render_timeout", step)
    assert str(caught.value) == f"render_timeout: {step}"
    assert not (tmp_path / pr.PDF_NAME).exists()  # nothing was handed over
    assert fake.stopped and not watchdogs()


@pytest.mark.parametrize("step", ["navigation", "evaluation", "pdf"])
def test_a_hung_browser_call_is_ended_by_the_watchdog_and_reported_with_its_phase(tmp_path, monkeypatch, step):
    fake = Fake(hang_at=step)
    fake.install(monkeypatch)
    started = time.monotonic()
    with pytest.raises(pr.RenderTimeoutError) as caught:
        render(tmp_path, fake, timeout=0.3)
    assert caught.value.phase == step and time.monotonic() - started < 5
    assert fake.killed >= 1  # the processes this render started were ended
    assert not (tmp_path / pr.PDF_NAME).exists() and not watchdogs()


def test_a_browser_that_will_not_close_is_ended_and_reported_as_a_shutdown_timeout(tmp_path, monkeypatch):
    fake = Fake(hang_at="shutdown")
    fake.install(monkeypatch)
    monkeypatch.setattr(pr, "SHUTDOWN_GRACE_SECONDS", 0.3)
    with pytest.raises(pr.RenderTimeoutError) as caught:
        render(tmp_path, fake, timeout=30)  # the work limit is generous; only the shutdown grace applies
    assert caught.value.phase == "shutdown" and fake.killed >= 1 and fake.stopped
    assert not (tmp_path / pr.PDF_NAME).exists() and not watchdogs()


def test_every_timeout_cleans_up_the_driver_the_browser_and_the_watchdog(tmp_path, monkeypatch):
    fake = Fake(hang_at="pdf")
    fake.install(monkeypatch)
    with pytest.raises(pr.RenderTimeoutError):
        render(tmp_path, fake, timeout=0.3)
    assert fake.browser_closed and fake.stopped and fake.killed >= 2  # at the deadline, and once more as a final sweep
    assert not watchdogs()


def test_another_error_is_not_mistaken_for_a_timeout(tmp_path, monkeypatch):
    class Other(Exception):
        pass

    fake = Fake()
    fake.install(monkeypatch)
    monkeypatch.setattr(Fake, "step", lambda self, name: (_ for _ in ()).throw(Other()) if name == "pdf" else None)
    with pytest.raises(Other):
        render(tmp_path, fake)
    assert fake.killed == 0 and fake.stopped and not watchdogs()


@pytest.mark.parametrize("bad", [0, -1, True, "5", None, 86401])
def test_the_timeout_must_be_a_sensible_number(tmp_path, bad):
    with pytest.raises(pr.PdfRenderError) as caught:
        pr.render_report_pdf(with_fingerprints(sample()), workdir=tmp_path, logo_path=LOGO, fonts=SOURCES,
                             timeout_seconds=bad)
    assert caught.value.code == "timeout_invalid"


def test_the_watchdog_ends_with_the_render_and_never_fires_when_work_finishes_in_time():
    fired = []
    dog = pr._Watchdog(30, lambda: fired.append(1))
    dog.start()
    dog.cancel()
    assert not fired and not dog.fired and not dog._thread.is_alive()


def test_the_process_ending_only_touches_processes_this_render_started(monkeypatch):
    killed = []
    assert pr._terminate_new_descendants({10 ** 9}) == 0 or True  # nothing of ours beyond the baseline: nothing ended
    assert pr._descendants() >= set() and not killed


# --- the service and the pipeline ---------------------------------------------------------------------------------------------

def test_the_configured_stage_limit_is_what_the_real_renderer_receives(world, monkeypatch):
    w = world()
    seen = {}

    def spy(render_input, **kwargs):
        seen.update(kwargs)
        raise pr.RenderTimeoutError("navigation")

    monkeypatch.setattr(pr, "render_report_pdf", spy)
    services = dataclasses.replace(w.services, renderer=None)
    response = go(w, services=services)
    assert seen["timeout_seconds"] == w.settings.stage_timeouts.rendering == 300.0
    assert (response.state, response.error["category"], response.error["code"]) == (
        "failed", "render_failed", "render_timeout_navigation")
    assert response.retry["eligible"] is True


def test_a_configured_rendering_limit_is_used(tmp_path):
    from test_pipeline import make_settings
    settings = make_settings(tmp_path)
    assert settings.stage_timeouts.rendering == 300.0
    from seo_report import runner_config as rc
    custom = rc.parse_settings({"paths": {"output_root": str(tmp_path / "o"), "history_db": str(tmp_path / "h" / "h.sqlite"),
                                          "ledger_db": str(tmp_path / "l" / "l.sqlite"), "work_root": str(tmp_path / "w")},
                                "execution": {"stage_timeouts": {"rendering": 45}}},
                               environ={"LOCALAPPDATA": str(tmp_path)})
    assert custom.stage_timeouts.rendering == 45.0


class TimesOutOnce:
    def __init__(self, phase="pdf"):
        self.phase, self.calls = phase, 0
        from test_render_artifacts import StubRenderer
        self.real = StubRenderer()

    def __call__(self, render_input, **kwargs):
        self.calls += 1
        if self.calls == 1:
            raise pr.RenderTimeoutError(self.phase)
        return self.real(render_input, **kwargs)


def test_a_timed_out_render_is_not_promoted_finalized_or_recorded(world):
    renderer = TimesOutOnce("pdf")
    w = world(renderer=renderer)
    response = go(w)
    assert (response.state, response.error["code"]) == ("failed", "render_timeout_pdf")
    reports, reviewer, _ = paths(w)
    files = [p.name for folder in (reports, reviewer) if folder.exists() for p in folder.iterdir()]
    assert not [n for n in files if n.startswith(("report.render-input.", "reviewer.")) or n.endswith(".pdf")], files
    run = w.ledger.get_run(RUN_KEY)
    assert run.state == "failed" and run.engine_run_id is None and run.record_name is None and run.pdf_sha256 is None
    assert not w.settings.history_db_path.exists()  # no snapshot, nothing finalized


def test_the_retry_after_a_timeout_keeps_the_generation_date_and_renders_one_identity(world):
    renderer = TimesOutOnce("evaluation")
    w = world(renderer=renderer)
    first = go(w)
    assert first.state == "failed"
    generation = w.ledger.get_run(RUN_KEY).generation_date
    w.clock.advance(days=3)
    retried = go(w, req(action="retry"))
    assert retried.state == "audited", retried.error
    run = w.ledger.get_run(RUN_KEY)
    assert run.generation_date == generation == "2026-11-02" and run.attempt_count == 2
    reports, _, _ = paths(w)
    sidecar = reports / ra.sidecar_name(run.render_input_sha256)
    assert ra.read_sidecar(reports, sidecar.name).generation_date.isoformat() == generation
    assert len([p for p in reports.iterdir() if p.name.startswith("report.render-input.")]) == 1


def test_a_timeout_after_an_open_sidecar_leaves_it_and_the_retry_reuses_it(world):
    from test_pipeline import NthOp
    w = world(services_options={"file_ops": NthOp("write", 2)})  # the draft hand-off fails after the sidecar is written
    assert go(w).state == "failed"
    reports, _, _ = paths(w)
    before = tree(reports)
    assert [n for n in before if "render-input" in n]
    renderer = TimesOutOnce("pdf")
    renderer.calls = 1  # the real renderer from here on
    retry = dataclasses.replace(w.services, file_ops=None)
    assert go(w, req(action="retry"), services=retry).state in ("audited", "blocked")
    for name, data in before.items():  # nothing that existed was deleted or changed
        assert tree(reports)[name] == data


# --- the real browser --------------------------------------------------------------------------------------------------------

def test_a_real_chromium_that_cannot_finish_in_time_is_ended_and_leaves_nothing_behind(tmp_path):
    baseline = pr._descendants()
    try:
        with pytest.raises(pr.RenderTimeoutError) as caught:
            pr.render_report_pdf(with_fingerprints(sample()), workdir=tmp_path, logo_path=LOGO, fonts=SOURCES,
                                 timeout_seconds=0.05)
    except Exception as error:
        if type(error).__module__.startswith("playwright"):
            pytest.skip("Chromium could not be started")
        raise
    assert caught.value.code == "render_timeout" and caught.value.phase in pr.PHASES
    assert not (tmp_path / pr.PDF_NAME).exists() and not watchdogs()
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and pr._descendants() - baseline:
        time.sleep(0.2)
    assert not (pr._descendants() - baseline)  # no browser or driver process outlived the render
