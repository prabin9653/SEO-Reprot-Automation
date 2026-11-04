"""Shared test configuration.

R8.6c activates structured logging at the start of every CLI command, which creates the log folder under `%LOCALAPPDATA%\\JasperSEOReporting\\logs`.
Tests must never write there, so by default `cli.activate_logging` is replaced by a no-op that writes nothing (it records the state "active" so that `doctor` is not told logging failed). The tests that exercise the real
activation (tests/test_r86c_logging_and_preflight.py) keep a reference to the original function and call it with a temporary `LOCALAPPDATA`.
"""
import pytest


@pytest.fixture(autouse=True)
def cli_logging_is_off_unless_a_test_turns_it_on(monkeypatch):
    try:
        from seo_report import cli
    except Exception:  # collection of unrelated tests must not depend on the CLI
        return
    monkeypatch.setattr(cli, "activate_logging", lambda ctx: ctx.extra.setdefault("logging", {"active": True, "code": None}) and False)  # reported active, writes nothing
