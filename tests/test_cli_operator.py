"""Stage 1 of the operator-experience work: `clients add`, `clients check`, `report open`, the standard settings location and the plain-language guidance.

Everything is synthetic: temporary folders, an injected workbook result, no Google call (the module-wide guard forbids sockets), no real credential,
no production folder. The fixtures are those of the report/status tests.
"""
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from seo_report import cli, cli_operator, client_config, client_loader, guidance  # noqa: E402
from seo_report.cli_exit import Exit  # noqa: E402
from seo_report.client_loader import Rejection  # noqa: E402
from test_r86d_report_status_recovery import (PERIOD, PROBES_OK, NoGoogle, cmd, home, loaded,  # noqa: E402,F401
                                              nothing_leaves_the_machine)

def with_unregistered(result, *pairs):
    """The workbook result plus sheet clients that have no key: pairs of (sheet row, Client Name); each also counts as a rejected row, as the loader does."""
    import dataclasses
    rows = tuple(Rejection(None, row, "client_not_in_registry") for row, _ in pairs)
    return dataclasses.replace(result, rejected=result.rejected + rows, unregistered=tuple(pairs))


REGISTRY = lambda home: Path(home.tmp / "clients.yaml")  # noqa: E731


def registry_text(home):
    return REGISTRY(home).read_text(encoding="utf-8")


def names_in(home):
    return [e.name for e in client_config.load_client_registry(REGISTRY(home)).entries]


# === keys ======================================================================================================================

@pytest.mark.parametrize("name,key", [("Acme Floor Covering", "acme-floor-covering"), ("R & D Home Solution", "r-and-d-home-solution"),
                                      ("Sam's Electrical and Solar", "sams-electrical-and-solar"), ("  XY  Roofing 770 ", "xy-roofing-770"),
                                      ("A+Plus!!", "a-plus"), ("---", ""), ("Étude", "tude")])  # an accented letter is not kept: the key is plain ASCII
def test_a_suggested_key_is_lowercase_ascii_with_single_dashes(name, key):
    assert cli_operator.suggest_key(name) == key


def test_a_suggested_key_never_exceeds_the_limit_and_is_valid_when_not_empty():
    key = cli_operator.suggest_key("Very " * 40 + "Long Name")
    assert len(key) <= 63 and not key.endswith("-")
    assert client_config.validate_client_key(key) == key


# === clients add ===============================================================================================================

def test_add_writes_a_valid_entry_keeps_comments_and_makes_a_backup(home, monkeypatch, capsys):
    before = registry_text(home)
    (home.tmp / "clients.yaml").write_text("# keep this comment\n" + before, encoding="utf-8")
    code, data = cmd(home, monkeypatch, ["clients", "add", "--name", "Acme Floor Covering"], capsys=capsys)
    assert code == Exit.OK and data["added"] == [{"name": "Acme Floor Covering", "client_key": "acme-floor-covering"}] and data["backup"] is True
    text = registry_text(home)
    assert text.startswith("# keep this comment\n") and "Acme Floor Covering" in names_in(home)
    assert (home.tmp / "clients.yaml.bak").read_text(encoding="utf-8").startswith("# keep this comment")


def test_add_accepts_an_explicit_key_and_fixes_a_file_without_a_trailing_newline(home, monkeypatch, capsys):
    path = REGISTRY(home)
    path.write_text(path.read_text(encoding="utf-8").rstrip("\n"), encoding="utf-8")
    code, data = cmd(home, monkeypatch, ["clients", "add", "--name", "Sams Electrical", "--key", "sams"], capsys=capsys)
    assert code == Exit.OK and "Sams Electrical" in names_in(home)
    assert dict((e.name, e.client_key) for e in client_config.load_client_registry(path).entries)["Sams Electrical"] == "sams"


def test_add_quotes_names_that_need_it(home, monkeypatch, capsys):
    code, _ = cmd(home, monkeypatch, ["clients", "add", "--name", 'Bob "The: Builder" #1 & Sons', "--key", "bob"], capsys=capsys)
    assert code == Exit.OK and 'Bob "The: Builder" #1 & Sons' in names_in(home)


@pytest.mark.parametrize("name,key,expected", [("Alpha Co", None, "client_already_registered"), ("alpha  co", None, "client_name_similar"),
                                               ("Brand New", "beta-co", "client_key_in_use")])
def test_add_refuses_duplicates_and_changes_nothing(home, monkeypatch, capsys, name, key, expected):
    before = registry_text(home)
    argv = ["clients", "add", "--name", name] + (["--key", key] if key else [])
    code, data = cmd(home, monkeypatch, argv, capsys=capsys)
    assert code == Exit.CONFIG_INVALID and data["error"]["code"] == expected
    assert registry_text(home) == before and not (home.tmp / "clients.yaml.bak").exists()


@pytest.mark.parametrize("name", [" padded ", "", "x" * 201])
def test_add_refuses_an_unusable_name(home, monkeypatch, capsys, name):
    before = registry_text(home)
    code, data = cmd(home, monkeypatch, ["clients", "add", "--name", name], capsys=capsys)
    assert code in (Exit.USAGE, Exit.CONFIG_INVALID) and registry_text(home) == before


def test_add_refuses_a_name_that_makes_no_key_and_a_bad_explicit_key(home, monkeypatch, capsys):
    before = registry_text(home)
    code, data = cmd(home, monkeypatch, ["clients", "add", "--name", "!!!"], capsys=capsys)
    assert code == Exit.USAGE and data["error"]["code"] == "client_key_invalid"
    code, _ = cmd(home, monkeypatch, ["clients", "add", "--name", "Ok Co", "--key", "Bad Key"], capsys=capsys)
    assert code == Exit.USAGE and registry_text(home) == before


def test_add_refuses_a_broken_existing_registry_and_does_not_touch_it(home, monkeypatch, capsys):
    REGISTRY(home).write_text("clients:\n  - name: \"A\"\n    clientkey: \"a\"\n", encoding="utf-8")
    before = registry_text(home)
    code, data = cmd(home, monkeypatch, ["clients", "add", "--name", "New Co"], capsys=capsys)
    assert code == Exit.CONFIG_INVALID and data["error"]["code"] == "client_registry_invalid" and registry_text(home) == before


def test_add_needs_exactly_one_of_name_or_all_missing(home, monkeypatch, capsys):
    assert cmd(home, monkeypatch, ["clients", "add"], capsys=capsys)[0] == Exit.USAGE
    assert cmd(home, monkeypatch, ["clients", "add", "--name", "X", "--all-missing"], capsys=capsys)[0] == Exit.USAGE
    assert cmd(home, monkeypatch, ["clients", "add", "--all-missing", "--key", "k"], capsys=capsys)[0] == Exit.USAGE
    assert cmd(home, monkeypatch, ["clients", "add", "--name", "X", "--yes"], capsys=capsys)[0] == Exit.USAGE


def test_add_all_missing_shows_a_plan_first_and_writes_only_with_yes(home, monkeypatch, capsys):
    missing = with_unregistered(loaded("Alpha Co", rejected=[Rejection("alpha-co", 12, "keywords_missing")]),
                                (9, "Zeta Co"), (10, "Zeta Co"), (11, "R & D Home Solution"))
    before = registry_text(home)
    code, data = cmd(home, monkeypatch, ["clients", "add", "--all-missing"], result=missing, capsys=capsys)
    assert code == Exit.OK and data["written"] is False and [a["name"] for a in data["added"]] == ["R & D Home Solution", "Zeta Co"]
    assert registry_text(home) == before
    code, data = cmd(home, monkeypatch, ["clients", "add", "--all-missing", "--yes"], result=missing, capsys=capsys)
    assert code == Exit.OK and data["written"] is True and "Zeta Co" in names_in(home) and "R & D Home Solution" in names_in(home)
    code, data = cmd(home, monkeypatch, ["clients", "add", "--all-missing", "--yes"], result=loaded("Alpha Co"), capsys=capsys)
    assert code == Exit.OK and data["added"] == []


def test_add_all_missing_skips_a_name_whose_key_collides(home, monkeypatch, capsys):
    clash = with_unregistered(loaded("Alpha Co"), (9, "Alpha-Co"))  # key alpha-co is taken by "Alpha Co"
    code, data = cmd(home, monkeypatch, ["clients", "add", "--all-missing", "--yes"], result=clash, capsys=capsys)
    assert code == Exit.OK and data["added"] == [] and data["skipped"][0]["name"] == "Alpha-Co"


# === clients check =============================================================================================================

def test_check_lists_every_kind_of_client_and_fails_only_for_real_problems(home, monkeypatch, capsys):
    result = with_unregistered(loaded("Alpha Co", skipped=[("beta-co", "status_not_active")], rejected=[Rejection("gamma-co", 4, "keywords_missing")]),
                               (7, "Zeta Co"))
    code, data = cmd(home, monkeypatch, ["clients", "check"], result=result, capsys=capsys)
    by_status = {}
    for row in data["clients"]:
        by_status.setdefault(row["status"], []).append(row)
    assert [r["client_key"] for r in by_status["ready"]] == ["alpha-co"] and by_status["ready"][0]["report"] == "not_run"
    assert by_status["ready"][0]["google_verified"] is False and by_status["ready"][0]["verify_code"] == "attestation_missing"
    assert [(r["client_key"], r["code"]) for r in by_status["needs_fix"]] == [("gamma-co", "keywords_missing")]
    assert [(r["name"], r["code"]) for r in by_status["not_registered"]] == [("Zeta Co", "client_not_in_registry")]
    assert [r["client_key"] for r in by_status["skipped"]] == ["beta-co"]
    assert [r["client_key"] for r in by_status["not_in_sheet"]] == ["delta-co"]
    assert code == Exit.CONFIG_INVALID and data["ok"] is False and data["counts"]["ready"] == 1


def test_check_is_clean_when_only_skipped_or_missing_rows_exist(home, monkeypatch, capsys):
    code, data = cmd(home, monkeypatch, ["clients", "check"], result=loaded("Alpha Co", skipped=[("beta-co", "status_not_active")]), capsys=capsys)
    assert code == Exit.OK and data["ok"] is True


def test_check_text_output_explains_each_problem_in_plain_words(home, monkeypatch, capsys):
    result = with_unregistered(loaded("Alpha Co", rejected=[Rejection("gamma-co", 4, "keywords_missing")]), (7, "Zeta Co"))
    code, text = cmd(home, monkeypatch, ["clients", "check"], result=result, json_mode=False, capsys=capsys)
    assert code == Exit.CONFIG_INVALID
    assert "READY" in text and "alpha-co" in text and "FIX" in text and "no keyword rows" in text
    assert "NOT KEYED" in text and "clients add" in text and "verify live" in text


def test_check_for_one_client_ignores_the_others(home, monkeypatch, capsys):
    result = with_unregistered(loaded("Alpha Co"), (7, "Zeta Co"))
    code, data = cmd(home, monkeypatch, ["clients", "check", "--client", "alpha-co"], result=result, capsys=capsys)
    assert code == Exit.OK and [r["client_key"] for r in data["clients"]] == ["alpha-co"]


def test_check_never_writes_anything(home, monkeypatch, capsys):
    from test_r86d_report_status_recovery import tree
    before = tree(home.tmp)
    cmd(home, monkeypatch, ["clients", "check"], result=loaded("Alpha Co"), capsys=capsys)
    assert tree(home.tmp) == before and not home.ledger_path.exists()


@pytest.mark.parametrize("state,expected", [("audited", "done"), ("delivered", "done"), ("collecting", "interrupted"), ("failed", "failed"), ("blocked", "blocked")])
def test_a_report_state_comes_from_the_ledger(state, expected):
    ledger = SimpleNamespace(latest_run=lambda key, period: SimpleNamespace(state=state))
    assert cli_operator._report_state(ledger, "k", PERIOD) == expected
    assert cli_operator._report_state(SimpleNamespace(latest_run=lambda key, period: None), "k", PERIOD) == "not_run"
    assert cli_operator._report_state(None, "k", PERIOD) == "not_run"


# === report open ===============================================================================================================

def make_pdf(home, key="alpha-co", period=PERIOD, body=b"%PDF-1.4"):
    folder = Path(home.settings.output_root) / "reports" / key / period
    folder.mkdir(parents=True)
    (folder / "report.pdf").write_bytes(body)
    return folder / "report.pdf"


def run_open(home, monkeypatch, capsys, argv, opened):
    full = ["--json"] + argv + ["--config", str(home.path)]
    code = cli.main(full, environ=home.env, client_factory=NoGoogle(), preflight_probes=PROBES_OK, clock=home.clock)
    return code, json.loads(capsys.readouterr().out)


def test_open_finds_the_latest_report_and_hands_it_to_the_viewer(home, monkeypatch, capsys):
    older = make_pdf(home, period="2026-08-01_2026-08-28")
    newer = make_pdf(home)
    opened = []
    monkeypatch.setattr(cli_operator, "_start_file", lambda path: opened.append(path))
    code, data = run_open(home, monkeypatch, capsys, ["report", "open", "--client", "alpha-co"], opened)
    assert code == Exit.OK and data["period_id"] == PERIOD and data["opened"] is True and data["other_periods"] == ["2026-08-01_2026-08-28"]
    assert opened == [str(newer.resolve())] and older.exists()


def test_open_can_pick_a_period_and_can_only_print(home, monkeypatch, capsys):
    make_pdf(home, period="2026-08-01_2026-08-28")
    make_pdf(home)
    opened = []
    monkeypatch.setattr(cli_operator, "_start_file", lambda path: opened.append(path))
    code, data = run_open(home, monkeypatch, capsys, ["report", "open", "--client", "alpha-co", "--period", "2026-08-01_2026-08-28", "--no-open"], opened)
    assert code == Exit.OK and data["period_id"] == "2026-08-01_2026-08-28" and data["opened"] is False and opened == []


def test_open_reports_a_missing_report_clearly_and_ignores_drafts(home, monkeypatch, capsys):
    folder = Path(home.settings.output_root) / "reports" / "alpha-co" / PERIOD
    folder.mkdir(parents=True)
    (folder / "report.unarchived.pdf").write_bytes(b"draft")  # a draft is not a finished report
    monkeypatch.setattr(cli_operator, "_start_file", lambda path: (_ for _ in ()).throw(AssertionError("opened")))
    code, data = run_open(home, monkeypatch, capsys, ["report", "open", "--client", "alpha-co"], [])
    assert code == Exit.CONFIG_INVALID and data["error"]["code"] == "report_not_found"
    code, data = run_open(home, monkeypatch, capsys, ["report", "open", "--client", "beta-co", "--period", PERIOD], [])
    assert code == Exit.CONFIG_INVALID and data["error"]["code"] == "report_not_found"


def test_open_needs_a_valid_client_key_and_period(home, monkeypatch, capsys):
    assert cli.main(["report", "open", "--client", "../x", "--config", str(home.path)], environ=home.env) == Exit.USAGE
    assert cli.main(["report", "open", "--client", "alpha-co", "--period", "2026-1-1_x", "--config", str(home.path)], environ=home.env) == Exit.USAGE


# === the standard settings location ===========================================================================================

def test_the_standard_settings_file_is_used_only_when_it_exists_in_the_per_user_folder(home, monkeypatch, capsys):
    standard = Path(home.local) / "JasperSEOReporting" / "config" / "runner.yaml"
    assert cli.standard_config_path(home.env) is None
    code = cli.main(["--json", "clients", "list"], environ=home.env, client_factory=NoGoogle())
    err = capsys.readouterr()
    assert code == Exit.CONFIG_INVALID and json.loads(err.out)["error"]["code"] == "config_not_given"
    standard.parent.mkdir(parents=True)
    standard.write_text(home.path.read_text(encoding="utf-8"), encoding="utf-8")
    assert cli.standard_config_path(home.env) == standard
    monkeypatch.setattr(client_loader, "load_from_workbook", lambda *a, **k: loaded("Alpha Co"))
    code = cli.main(["--json", "clients", "list"], environ=home.env, client_factory=NoGoogle())
    assert code == Exit.OK and json.loads(capsys.readouterr().out)["clients"][0]["client_key"] == "alpha-co"


def test_an_explicit_config_still_wins_and_no_environment_means_no_default(home, tmp_path):
    assert cli.standard_config_path({}) is None
    assert cli.standard_config_path({"LOCALAPPDATA": ""}) is None


# === guidance ==================================================================================================================

LOADER_CODES = {"client_not_in_registry", "status_not_active", "website_missing", "gsc_property_missing", "ga4_property_missing", "keywords_missing",
                "period_dates_missing", "period_not_28_days", "period_dates_invalid", "ga4_property_invalid", "duplicate_client", "ranking_invalid",
                "keyword_empty", "client_name_empty", "keyword_client_unknown", "client_row_invalid", "header_missing", "header_invalid"}
GATE_AND_AUTH_CODES = {"auth_credentials_revoked", "auth_token_missing", "gsc_property_not_accessible", "live_google_not_verified", "attestation_expired",
                       "attestation_client_not_covered", "credentials_not_relocated", "period_differs_from_workbook", "client_unknown", "already_complete"}


def test_every_workbook_row_code_and_the_common_failures_have_guidance():
    assert LOADER_CODES | GATE_AND_AUTH_CODES <= guidance.known_codes()
    for code in guidance.known_codes():
        item = guidance.explain(code)
        assert item.meaning.endswith(".") and item.fix.endswith((".", ")")), code
        assert "@" not in item.meaning + item.fix and "\\" not in item.meaning + item.fix


def test_the_loader_only_produces_codes_that_have_guidance():
    source = (ROOT / "src" / "seo_report" / "client_loader.py").read_text(encoding="utf-8")
    import re
    produced = set(re.findall(r'"([a-z0-9_]+(?:_missing|_invalid|_empty|_unknown|_registry|_28_days|_active|_client))"', source))
    assert produced and produced <= guidance.known_codes() | {"header_invalid", "header_missing"}


def test_unknown_codes_have_no_guidance_and_never_raise():
    assert guidance.explain("totally_unknown") is None and guidance.explain(None) is None and guidance.explain(5) is None
    assert guidance.row_fix("totally_unknown") == "totally_unknown"


def test_an_error_shows_its_meaning_and_fix_in_text_and_json(home, monkeypatch, capsys):
    # the list command reads the workbook, so the failure is injected through the loader
    monkeypatch.setattr(client_loader, "load_from_workbook", lambda *a, **k: (_ for _ in ()).throw(client_loader.WorkbookUnavailable("auth_required", "auth_credentials_revoked")))
    capsys.readouterr()
    code = cli.main(["clients", "list", "--config", str(home.path)], environ=home.env, client_factory=NoGoogle())
    err = capsys.readouterr().err
    assert code == Exit.AUTH_REQUIRED and "what it means:" in err and "what to do:" in err and "setup auth" in err
    code = cli.main(["--json", "clients", "list", "--config", str(home.path)], environ=home.env, client_factory=NoGoogle())
    body = json.loads(capsys.readouterr().out)
    assert body["error"]["code"] == "auth_credentials_revoked" and "setup auth" in body["error"]["fix"] and body["error"]["meaning"]


def test_an_error_without_guidance_is_unchanged(home, monkeypatch, capsys):
    code, data = cmd(home, monkeypatch, ["clients", "validate", "--client", "nobody-co"], capsys=capsys)
    assert code == Exit.CONFIG_INVALID and data["error"]["code"] == "client_unknown" and "fix" in data["error"]  # this one has guidance
    ctx_error = cli.CliError(Exit.CONFIG_INVALID, "x", "some_code_without_guidance")
    ctx = SimpleNamespace(args=SimpleNamespace(json=True))
    cli.fail(ctx, ctx_error)
    assert json.loads(capsys.readouterr().out)["error"] == {"code": "some_code_without_guidance", "message": "x"}


def test_a_blocked_run_lists_a_fix_for_each_blocker(home, monkeypatch, capsys):
    code = cli.main(["report", "run", "--client", "alpha-co", "--period", PERIOD, "--production", "--config", str(home.path)], environ=home.env,
                    preflight_probes=PROBES_OK, client_factory=NoGoogle(), clock=home.clock)
    err = capsys.readouterr().err
    assert code == Exit.PREFLIGHT_FAILED and "live_google_not_verified" in err and "verify live" in err


def test_an_already_complete_run_explains_that_it_is_not_an_error():
    item = {"outcome": "already_complete", "error": None}
    from seo_report import cli_report
    lines = cli_report._advice_lines(item)
    assert any("never overwritten" in line for line in lines) and any("report open" in line for line in lines)
    assert cli_report._advice_lines({"outcome": "succeeded", "error": None}) == []
    assert any("setup auth" in line for line in cli_report._advice_lines({"outcome": "failed", "error": {"code": "auth_credentials_revoked"}}))


# === last report time ==========================================================================================================

def _run(state, **times):
    return SimpleNamespace(state=state, **times)


def test_a_finished_report_shows_when_it_was_finalized_and_other_runs_their_last_update():
    done = _run("audited", finalized_at="2026-11-17T20:45:00+00:00", audited_at="2026-11-17T20:46:00+00:00", finished_at="2026-11-17T20:47:00+00:00",
                updated_at="2026-11-18T09:00:00+00:00")
    assert cli_operator._report_info(SimpleNamespace(latest_run=lambda k, p: done), "k", PERIOD) == ("done", "2026-11-17T20:45:00+00:00")
    failed = _run("failed", updated_at="2026-11-18T09:00:00+00:00", finished_at="2026-11-18T08:59:00+00:00")
    assert cli_operator._report_info(SimpleNamespace(latest_run=lambda k, p: failed), "k", PERIOD) == ("failed", "2026-11-18T09:00:00+00:00")
    fallback = _run("audited", finalized_at=None, audited_at="", finished_at="2026-11-17T20:47:00+00:00", updated_at="x")
    assert cli_operator._report_info(SimpleNamespace(latest_run=lambda k, p: fallback), "k", PERIOD)[1] == "2026-11-17T20:47:00+00:00"
    assert cli_operator._report_info(SimpleNamespace(latest_run=lambda k, p: None), "k", PERIOD) == ("not_run", None)
    assert cli_operator._report_info(None, "k", PERIOD) == ("not_run", None)
    assert cli_operator._report_info(SimpleNamespace(latest_run=lambda k, p: _run("audited")), "k", PERIOD) == ("done", None)  # no usable time: no time shown


def test_the_console_shows_the_time_in_utc_and_ignores_unusable_values():
    assert cli_operator._utc_text("2026-11-17T20:45:00+00:00") == "2026-11-17 20:45 UTC"
    assert cli_operator._utc_text("2026-11-17T15:45:00-05:00") == "2026-11-17 20:45 UTC"
    assert cli_operator._utc_text(None) == "" and cli_operator._utc_text("garbage") == ""


def test_check_rows_carry_the_last_run_time(home, monkeypatch, capsys):
    import dataclasses
    from seo_report import cli_report
    run = _run("audited", finalized_at="2026-11-17T20:45:00+00:00")
    monkeypatch.setattr(cli_report, "open_ledger", lambda ctx, settings, mode: SimpleNamespace(latest_run=lambda k, p: run, close=lambda: None))
    code, data = cmd(home, monkeypatch, ["clients", "check"], result=loaded("Alpha Co"), capsys=capsys)
    row = data["clients"][0]
    assert row["report"] == "done" and row["last_run_at"] == "2026-11-17T20:45:00+00:00"
    code, text = cmd(home, monkeypatch, ["clients", "check"], result=loaded("Alpha Co"), json_mode=False, capsys=capsys)
    assert "(2026-11-17 20:45 UTC)" in text
    del dataclasses


def test_the_page_shows_times_in_the_viewers_time_zone_with_the_zone_name():
    from seo_report import webui_page
    page = webui_page.PAGE
    assert "timeZoneName" in page and "toLocaleString(undefined" in page and "last_run_at" in page and "Generated " in page and "time zone" in page
