"""R8.4.4 (D6): process liveness on Windows and lease renewal during long stages (temporary folders and fakes only).

The Windows API is exercised through a fake that returns the documented error codes; one test also asks the real API about
this very process. Nothing here ends, signals or touches any other process.
"""
import ctypes
import dataclasses
import datetime as dt
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from seo_report import contracts
from seo_report import ledger as lg
from seo_report import pipeline
from seo_report import runner_config as rc
from test_pipeline import PERIOD_ID, RUN_KEY, Clock, default_collect, go, make_settings, paths, req, world  # noqa: F401

UTC = dt.timezone.utc
WINDOWS = os.name == "nt"
requires_windows = pytest.mark.skipif(not WINDOWS, reason="Windows API")


class FakeApi:
    """OpenProcess / GetExitCodeProcess / GetProcessTimes / CloseHandle with scripted answers."""

    def __init__(self, handle=1234, error=0, exit_code=259, exit_ok=True, created=None):
        self.handle, self.error, self.exit_code, self.exit_ok, self.created = handle, error, exit_code, exit_ok, created
        self.closed, self.opened = [], []

    def OpenProcess(self, access, inherit, pid):
        self.opened.append((access, inherit, pid))
        return self.handle

    def last_error(self):
        return self.error

    def GetExitCodeProcess(self, handle, ref):
        ref._obj.value = self.exit_code
        return self.exit_ok

    def GetProcessTimes(self, handle, created, *others):
        ticks = int((self.created - dt.datetime(1601, 1, 1, tzinfo=UTC)).total_seconds() * 1e7)
        created._obj.dwHighDateTime, created._obj.dwLowDateTime = ticks >> 32, ticks & 0xFFFFFFFF
        return True

    def CloseHandle(self, handle):
        self.closed.append(handle)
        return True


# --- A: process liveness ------------------------------------------------------------------------------------------------------

@requires_windows
def test_the_windows_api_is_declared_with_explicit_types():
    from ctypes import wintypes
    api = lg._windows_api()
    assert api.OpenProcess.restype is wintypes.HANDLE
    assert list(api.OpenProcess.argtypes) == [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    assert api.GetExitCodeProcess.restype is wintypes.BOOL and len(api.GetExitCodeProcess.argtypes) == 2
    assert len(api.GetProcessTimes.argtypes) == 5 and api.GetProcessTimes.restype is wintypes.BOOL
    assert list(api.CloseHandle.argtypes) == [wintypes.HANDLE] and api.CloseHandle.restype is wintypes.BOOL


def test_access_denied_means_the_process_exists():
    api = FakeApi(handle=0, error=5)
    assert lg.probe_pid(4242, api) == "alive"


def test_a_missing_process_is_dead_only_for_the_not_found_error():
    assert lg.probe_pid(4242, FakeApi(handle=0, error=87)) == "dead"
    for other in (0, 1, 6, 1450, 99999):  # any other failure proves nothing
        assert lg.probe_pid(4242, FakeApi(handle=0, error=other)) == "unknown"


def test_an_exit_code_decides_alive_or_dead_and_an_unreadable_one_is_unknown():
    assert lg.probe_pid(7, FakeApi(exit_code=259)) == "alive"
    assert lg.probe_pid(7, FakeApi(exit_code=0)) == "dead"
    assert lg.probe_pid(7, FakeApi(exit_code=3221225477)) == "dead"
    assert lg.probe_pid(7, FakeApi(exit_ok=False)) == "unknown"


def test_the_handle_is_always_closed_and_only_limited_query_access_is_requested():
    for api in (FakeApi(exit_code=259), FakeApi(exit_code=1), FakeApi(exit_ok=False)):
        lg.probe_pid(7, api)
        assert api.closed == [api.handle] and api.opened == [(0x1000, False, 7)]
    none = FakeApi(handle=0, error=5)
    lg.probe_pid(7, none)
    assert none.closed == []


@pytest.mark.parametrize("pid", [0, -1, None, "12", 1.5, True, 2 ** 32, 2 ** 70])
def test_an_invalid_process_id_is_unknown_and_counts_as_alive(pid):
    api = FakeApi()
    assert lg.probe_pid(pid, api) == "unknown" and api.opened == []
    assert lg.pid_is_alive(pid) is True


def test_ambiguity_is_treated_as_alive(monkeypatch):
    for answer, expected in (("unknown", True), ("alive", True), ("dead", False)):
        monkeypatch.setattr(lg, "probe_pid", lambda pid, api=None, a=answer: a)
        assert lg.pid_is_alive(99) is expected


def test_liveness_never_uses_os_kill(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("os.kill must not be used")
    monkeypatch.setattr(os, "kill", forbidden)
    assert lg.pid_is_alive(os.getpid()) is True
    lg.pid_is_alive(2 ** 22 + 12345)


def test_the_real_probe_sees_this_process_alive_and_a_finished_child_dead():
    assert lg.probe_pid(os.getpid()) == "alive"
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait()
    time.sleep(0.2)
    assert lg.probe_pid(child.pid) in ("dead", "unknown")  # an id may be reused at once; it is never reported alive wrongly
    assert lg.probe_pid(child.pid) != "alive" or lg.process_start_time(child.pid) is not None


def test_process_creation_time_is_read_from_filetime():
    created = dt.datetime(2026, 10, 1, 12, 30, 15, tzinfo=UTC)
    assert lg.process_start_time(55, FakeApi(created=created)) == created
    assert lg.process_start_time(0, FakeApi(created=created)) is None


@requires_windows
def test_this_process_started_before_now_and_after_boot():
    started = lg.process_start_time(os.getpid())
    assert started is not None and started <= dt.datetime.now(UTC) and dt.datetime.now(UTC) - started < dt.timedelta(days=7)


def make_ledger(path, *, owner, clock, **kw):
    return lg.Ledger(path, clock=clock, owner_id=owner, **kw)


def expired_lock(tmp_path, clock, *, holder_alive, host="same", started=None, pid_started=None):
    path = tmp_path / "ledger.sqlite"
    holder = make_ledger(path, owner="holder", clock=clock, host="h1", pid=4321, pid_alive=lambda p: True)
    holder.acquire_lock("client:example-co", ttl_seconds=60)
    clock.advance(hours=1)  # the lease has expired
    other = make_ledger(path, owner="other", clock=clock, host="h1" if host == "same" else "h2", pid=1,
                        pid_alive=lambda p: holder_alive, pid_started=pid_started)
    return holder, other


def test_an_expired_lease_with_a_live_holder_is_not_taken(tmp_path):
    clock = Clock()
    holder, other = expired_lock(tmp_path, clock, holder_alive=True)
    with pytest.raises(lg.LockHeldError):
        other.acquire_lock("client:example-co", ttl_seconds=60)
    assert other.get_lock("client:example-co").owner_id == "holder"
    holder.close(), other.close()


def test_an_expired_lease_with_a_dead_holder_on_this_host_is_taken(tmp_path):
    clock = Clock()
    holder, other = expired_lock(tmp_path, clock, holder_alive=False)
    other.acquire_lock("client:example-co", ttl_seconds=60)
    assert other.get_lock("client:example-co").owner_id == "other"
    assert "lock_takeover" in [e.code for e in other.events_for()]
    holder.close(), other.close()


def test_a_dead_holder_with_an_unexpired_lease_is_not_taken(tmp_path):
    clock = Clock()
    path = tmp_path / "l.sqlite"
    holder = make_ledger(path, owner="holder", clock=clock, host="h1", pid=4321)
    holder.acquire_lock("client:example-co", ttl_seconds=3600)
    other = make_ledger(path, owner="other", clock=clock, host="h1", pid=1, pid_alive=lambda p: False)
    with pytest.raises(lg.LockHeldError):
        other.acquire_lock("client:example-co", ttl_seconds=60)
    holder.close(), other.close()


def test_a_lock_of_another_host_is_never_taken_even_when_expired_and_the_pid_is_unknown_here(tmp_path):
    clock = Clock()
    holder, other = expired_lock(tmp_path, clock, holder_alive=False, host="different")
    with pytest.raises(lg.LockHeldError):
        other.acquire_lock("client:example-co", ttl_seconds=60)
    holder.close(), other.close()


def test_a_reused_process_id_is_recognised_by_its_creation_time(tmp_path):
    clock = Clock()
    # the pid is alive but its process was created long AFTER the lock was acquired: it cannot be the holder
    holder, other = expired_lock(tmp_path, clock, holder_alive=True,
                                 pid_started=lambda pid: clock.now + dt.timedelta(hours=2))
    other.acquire_lock("client:example-co", ttl_seconds=60)
    assert other.get_lock("client:example-co").owner_id == "other"
    holder.close(), other.close()


@pytest.mark.parametrize("started", ["before", "none", "close", "error"])
def test_a_live_process_that_may_be_the_holder_is_not_replaced(tmp_path, started):
    clock = Clock()

    def when(pid):
        if started == "error":
            raise OSError("cannot read")
        return {"before": clock.now - dt.timedelta(days=3), "none": None,
                "close": clock.now - dt.timedelta(hours=1) + dt.timedelta(seconds=2)}[started]

    holder, other = expired_lock(tmp_path, clock, holder_alive=True, pid_started=when)
    with pytest.raises(lg.LockHeldError):
        other.acquire_lock("client:example-co", ttl_seconds=60)
    holder.close(), other.close()


# --- B: lease renewal --------------------------------------------------------------------------------------------------------

def real_clock():
    return dt.datetime.now(UTC)


@pytest.fixture
def pair(tmp_path):
    path = tmp_path / "ledger.sqlite"
    opened = []

    def build(**kw):
        led = lg.Ledger(path, clock=kw.pop("clock", real_clock), **kw)
        opened.append(led)
        return led
    yield build
    for led in opened:
        led.close()


def keeper_threads():
    return [t for t in threading.enumerate() if t.name == "ledger-lease-keeper"]


def test_the_heartbeat_checks_ownership_and_the_lease_identity(pair):
    mine, other = pair(owner_id="mine"), pair(owner_id="other")
    lease = mine.acquire_lock("client:x", ttl_seconds=60)
    mine.heartbeat("client:x", ttl_seconds=60, acquired_at=lease.acquired_at)
    with pytest.raises(lg.LockHeldError) as caught:
        other.heartbeat("client:x", ttl_seconds=60)
    assert caught.value.code == "lock_lost"
    with pytest.raises(lg.LockHeldError):
        mine.heartbeat("client:x", ttl_seconds=60, acquired_at="2000-01-01T00:00:00+00:00")  # a different lease
    mine.release_lock("client:x")
    with pytest.raises(lg.LockHeldError):
        mine.heartbeat("client:x", ttl_seconds=60)


def test_a_lease_is_renewed_during_a_stage_longer_than_the_lease(pair):
    mine, taker = pair(owner_id="mine"), pair(owner_id="taker", pid_alive=lambda p: False)
    mine.acquire_lock("client:x", ttl_seconds=1)
    before = mine.get_lock("client:x")
    with mine.keep_alive(["client:x"], ttl_seconds=1, interval_seconds=0.1) as keeper:
        time.sleep(1.8)  # well past the original one-second lease
        keeper.check()
        held = mine.get_lock("client:x")
        # Timestamps are truncated to whole seconds (`contracts.timestamp_of`), and the ledger itself treats a lease as alive while
        # `expires_at >= now` (`Ledger._stale`). So: "alive" is `>=` on the second grid, and renewal is proved by the stored values having moved
        # past the original lease. The last renewal is at least ~1.7 s after acquisition, so its truncated heartbeat and expiry are at least one
        # whole second beyond the originals; an unrenewed lease would leave both equal to `before` and fail these strict comparisons.
        assert held.expires_at >= contracts.timestamp_of(real_clock())
        assert held.expires_at > before.expires_at and held.heartbeat_at > before.heartbeat_at
        with pytest.raises(lg.LockHeldError):
            taker.acquire_lock("client:x", ttl_seconds=1)  # a second process cannot take a live execution
    assert not keeper_threads()


def test_a_lease_that_ends_in_the_current_second_is_still_held_and_is_free_in_the_next(pair):
    """Regression for the same-second case: truncated timestamps can be equal, and equal means 'not yet expired'."""
    now = [dt.datetime(2026, 1, 1, 0, 0, 0, 900_000, tzinfo=UTC)]
    clock = lambda: now[0]  # noqa: E731
    mine, taker = pair(owner_id="mine", clock=clock), pair(owner_id="taker", clock=clock, pid_alive=lambda p: False)
    mine.acquire_lock("client:x", ttl_seconds=1)
    lease = mine.get_lock("client:x")
    now[0] = dt.datetime(2026, 1, 1, 0, 0, 1, 50_000, tzinfo=UTC)
    assert lease.expires_at ==contracts.timestamp_of(now[0]) == "2026-01-01T00:00:01+00:00"  # equal on the one-second grid
    with pytest.raises(lg.LockHeldError):
        taker.acquire_lock("client:x", ttl_seconds=1)
    now[0] = dt.datetime(2026, 1, 1, 0, 0, 2, 0, tzinfo=UTC)
    assert taker.acquire_lock("client:x", ttl_seconds=1).owner_id == "taker"  # one whole second later it has expired


def test_without_renewal_the_same_lease_would_have_expired_and_been_taken(pair):
    mine, taker = pair(owner_id="mine"), pair(owner_id="taker", pid_alive=lambda p: False)
    mine.acquire_lock("client:x", ttl_seconds=1)
    time.sleep(2.3)
    taker.acquire_lock("client:x", ttl_seconds=1)  # sanity: this is what renewal prevents
    assert taker.get_lock("client:x").owner_id == "taker"


def test_losing_the_lease_is_detected_and_reported(pair):
    mine, thief = pair(owner_id="mine"), pair(owner_id="thief", pid_alive=lambda p: False)
    mine.acquire_lock("client:x", ttl_seconds=1)
    with mine.keep_alive(["client:x"], ttl_seconds=1, interval_seconds=0.05) as keeper:
        mine._con.execute("UPDATE locks SET owner_id = 'thief' WHERE lock_name = 'client:x'")  # ownership changes hands
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            try:
                keeper.check()
            except lg.LockHeldError as error:
                assert error.code == "lock_lost" and keeper.lost_reason == "lease_lost"
                break
            time.sleep(0.05)
        else:
            pytest.fail("the lost lease was not noticed")
    assert mine.get_lock("client:x").owner_id == "thief"  # the keeper never took it back
    assert not keeper_threads()


def test_a_lease_released_and_acquired_again_is_a_different_lease(pair):
    mine = pair(owner_id="mine")
    mine.acquire_lock("client:x", ttl_seconds=30)
    with mine.keep_alive(["client:x"], ttl_seconds=30, interval_seconds=0.05) as keeper:
        mine.release_lock("client:x")
        time.sleep(1.1)  # lease identity is the acquisition time, kept to the second
        mine.acquire_lock("client:x", ttl_seconds=30)
        time.sleep(0.3)
        with pytest.raises(lg.LockHeldError):
            keeper.check()


def test_a_keeper_needs_leases_that_are_ours_right_now(pair):
    mine, other = pair(owner_id="mine"), pair(owner_id="other")
    other.acquire_lock("client:x", ttl_seconds=30)
    with pytest.raises(lg.LockHeldError):
        with mine.keep_alive(["client:x"], ttl_seconds=30, interval_seconds=0.1):
            pass
    with pytest.raises(lg.LockHeldError):
        with mine.keep_alive(["client:missing"], ttl_seconds=30, interval_seconds=0.1):
            pass
    assert not keeper_threads()


def test_persistent_renewal_failures_end_in_a_lost_lease_before_the_lease_runs_out(pair, monkeypatch):
    mine = pair(owner_id="mine")
    mine.acquire_lock("client:x", ttl_seconds=0.6)
    real = lg.Ledger.heartbeat

    def busy(self, *args, **kwargs):
        raise lg.LedgerError("ledger_busy")

    monkeypatch.setattr(lg.Ledger, "heartbeat", busy)
    with mine.keep_alive(["client:x"], ttl_seconds=0.6, interval_seconds=0.05) as keeper:
        time.sleep(0.8)
        with pytest.raises(lg.LockHeldError):
            keeper.check()
        assert keeper.lost_reason == "renewal_failed"
    monkeypatch.setattr(lg.Ledger, "heartbeat", real)


@pytest.mark.parametrize("interrupt", [KeyboardInterrupt, SystemExit])
def test_an_interrupt_inside_the_block_still_stops_the_thread(pair, interrupt):
    mine = pair(owner_id="mine")
    mine.acquire_lock("client:x", ttl_seconds=30)
    with pytest.raises(interrupt):
        with mine.keep_alive(["client:x"], ttl_seconds=30, interval_seconds=0.05):
            assert len(keeper_threads()) == 1
            raise interrupt()
    assert not keeper_threads()
    mine.release_lock("client:x")


def test_a_dead_runs_lease_is_not_extended_by_anyone_else(pair):
    mine, survivor = pair(owner_id="mine"), pair(owner_id="survivor", pid_alive=lambda p: False)
    mine.acquire_lock("client:x", ttl_seconds=1)  # no keeper: the process "died"
    time.sleep(2.3)
    assert survivor.get_lock("client:x").expires_at < contracts.timestamp_of(real_clock())
    survivor.acquire_lock("client:x", ttl_seconds=30)  # expired and the holder is gone: taken over, never extended


# --- the pipeline uses the keeper -------------------------------------------------------------------------------------------

def slow_world(world, work):
    w = world(execution={"heartbeat_interval_seconds": 1})
    return w, dataclasses.replace(w.services, collect=work(w))


def test_a_long_stage_keeps_the_client_lease_and_the_run_completes(world):
    seen = {}

    def work(w):
        def collect(client_run, today):
            w.clock.advance(seconds=900)  # the stage "runs" longer than the 600 s lease
            time.sleep(2.6)
            lock = w.ledger.get_lock("client:example-co")
            seen["expires"], seen["now"] = lock.expires_at, contracts.timestamp_of(w.clock())
            return default_collect(client_run, today)
        return collect

    w, services = slow_world(world, work)
    response = go(w, services=services)
    assert response.state == "audited", response.error
    assert seen["expires"] > seen["now"]  # renewed in the background, not only at a stage boundary
    assert not keeper_threads() and w.ledger.get_lock("client:example-co") is None


def test_a_lost_lease_stops_the_run_before_any_further_write(world):
    def work(w):
        def collect(client_run, today):
            other = lg.Ledger(w.settings.ledger_db_path, clock=w.clock)
            other._con.execute("DELETE FROM locks WHERE lock_name = 'client:example-co'")  # the lease is gone
            other.close()
            time.sleep(2.6)
            return default_collect(client_run, today)
        return collect

    w, services = slow_world(world, work)
    response = go(w, services=services)
    assert (response.outcome, response.error["category"]) == ("rejected", "run_in_progress")
    assert w.ledger.get_run(RUN_KEY).state == "collecting"  # never moved on: no render, no draft, no finalization
    reports, reviewer, _ = paths(w)
    assert not reports.exists() or not [p for p in reports.iterdir() if p.is_file()]
    assert not keeper_threads()


@pytest.mark.parametrize("interrupt", [KeyboardInterrupt, SystemExit])
def test_an_interrupt_in_a_stage_releases_every_lock_and_stops_the_keeper(world, interrupt):
    def collect(client_run, today):
        raise interrupt()
    w = world(collect=collect)
    with pytest.raises(interrupt):
        go(w)
    assert w.ledger.get_lock("client:example-co") is None and w.ledger.get_lock("runner:global") is None
    assert not keeper_threads()


def test_a_batch_lock_held_by_the_runner_is_renewed_too(world):
    w = world(execution={"heartbeat_interval_seconds": 1})
    w.ledger.acquire_lock("runner:global", ttl_seconds=600)
    batch = dataclasses.replace(w.services, global_lock_held=True)
    seen = {}

    def collect(client_run, today):
        w.clock.advance(seconds=900)
        time.sleep(2.6)
        seen["expires"], seen["now"] = w.ledger.get_lock("runner:global").expires_at, contracts.timestamp_of(w.clock())
        return default_collect(client_run, today)

    assert go(w, services=dataclasses.replace(batch, collect=collect)).state == "audited"
    assert seen["expires"] > seen["now"] and w.ledger.get_lock("runner:global") is not None
    w.ledger.release_lock("runner:global")


# --- D: configuration ----------------------------------------------------------------------------------------------------------

def parse(tmp_path, **execution):
    return make_settings(tmp_path, **execution)


def test_the_heartbeat_interval_has_a_safe_default_that_scales_with_the_lease(tmp_path):
    assert parse(tmp_path).heartbeat_interval_seconds == 60.0
    assert parse(tmp_path, lock_ttl_seconds=30).heartbeat_interval_seconds == 7.5
    assert parse(tmp_path, heartbeat_interval_seconds=20).heartbeat_interval_seconds == 20.0


@pytest.mark.parametrize("value,code", [(0, "number_out_of_range"), (0.5, "number_out_of_range"), (-5, "number_out_of_range"),
                                        (7200, "number_out_of_range"), ("fast", "number_invalid"), (True, "number_invalid"),
                                        (201, "heartbeat_interval_too_long")])
def test_invalid_heartbeat_intervals_are_refused(tmp_path, value, code):
    with pytest.raises(rc.RunnerConfigError) as caught:
        parse(tmp_path, heartbeat_interval_seconds=value)  # the lease is 600 s: at most 200 s between renewals
    assert caught.value.code == code and caught.value.field == "execution.heartbeat_interval_seconds"


def test_the_interval_is_checked_against_the_configured_lease(tmp_path):
    with pytest.raises(rc.RunnerConfigError) as caught:
        parse(tmp_path, lock_ttl_seconds=30, heartbeat_interval_seconds=11)
    assert caught.value.code == "heartbeat_interval_too_long"
