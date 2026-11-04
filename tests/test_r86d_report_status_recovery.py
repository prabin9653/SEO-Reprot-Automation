"""R8.6d: `report dry-run|run|batch`, `status`, `recovery inspect|acknowledge|cancel`, and the production gate in front of them.

Every execution here is SYNTHETIC: temporary storage, a stub renderer, fake source data and a permissive gate injected through the `gate=` test seam of
`cli.main` (no command-line argument or environment variable can reach it). Tests that use the REAL gate prove that it blocks. No Google call is made
(a guard forbids sockets and the sign-in flow), no real credential file or production folder is touched.
"""
import dataclasses
import datetime as dt
import hashlib
import json
import os
import socket
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from seo_report import auth, cli, contracts
from seo_report import client_loader
from seo_report import finalize_files as ff
from seo_report import google_clients as gc
from seo_report import ledger as lg
from seo_report import output_layout as layout
from seo_report import pdf_renderer as pr
from seo_report import pipeline, preflight as pf
from seo_report import source_collector as sc
from seo_report.cli_exit import Exit
from seo_report.client_loader import ClientRun, LoadResult, Rejection
from seo_report.periods import report_periods
from seo_report.sources import gsc as gsc_source
from test_pdf_renderer import FONT_FILES, LOGO
from test_pipeline import Clock, NthOp, default_collect, key_of, make_client
from test_render_artifacts import StubRenderer
from test_report_run import CURRENT

PERIOD = "2026-09-29_2026-10-26"
OTHER_PERIOD = (dt.date(2026, 10, 27), dt.date(2026, 11, 23))
NAMES = ("Alpha Co", "Beta Co", "Gamma Co", "Delta Co")
PROBES_OK = {"chromium_folders": lambda env: ["chromium-1234"], "launch_probe": lambda env: {"launched": True, "browser": "156.0.0.0"}}
PERMIT = lambda inp: (True, Exit.OK, [])  # noqa: E731  the injected gate of the synthetic integration tests


@pytest.fixture(autouse=True)
def nothing_leaves_the_machine(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("a network connection was attempted")
    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    import google_auth_oauthlib.flow as flow
    monkeypatch.setattr(flow.InstalledAppFlow, "run_local_server", lambda *a, **k: (_ for _ in ()).throw(AssertionError("sign-in flow started")))


class NoGoogle:
    def __getattr__(self, name):
        raise AssertionError(f"Google was contacted ({name})")


@pytest.fixture
def home(tmp_path):
    local = tmp_path / "local"
    for sub in ("output", "work", "history", "ledger"):
        (local / sub).mkdir(parents=True)
    creds = tmp_path / "creds"
    creds.mkdir()
    (creds / "client.json").write_text("{}", encoding="utf-8")
    (creds / "login.json").write_text("{}", encoding="utf-8")
    (tmp_path / "clients.yaml").write_text(yaml.safe_dump({"clients": [{"name": n, "client_key": key_of(n)} for n in NAMES]}), encoding="utf-8")
    (tmp_path / "jasper.yaml").write_text(yaml.safe_dump({"sender": {
        "name": "Sample", "title": "PM", "address_lines": ["1 Way"], "phone": "(000) 000-0000", "email": "sender@example.test",
        "website_label": "S", "website_url": "https://sender.example"},
        "regional_profiles": {"United States": {"title": "Project Manager | Sample", "address_lines": ["1 Way"], "website_label": "S",
                                                "website_url": "https://sender.example"}}}), encoding="utf-8")
    data = {"paths": {"output_root": str(local / "output"), "history_db": str(local / "history" / "h.sqlite"),
                      "ledger_db": str(local / "ledger" / "l.sqlite"), "work_root": str(local / "work"),
                      "clients_config": str(tmp_path / "clients.yaml"), "sender_config": str(tmp_path / "jasper.yaml"),
                      "oauth_client_file": str(creds / "client.json"), "oauth_cache_file": str(creds / "login.json")},
            "render": {"logo": str(LOGO), "fonts": [{"label": "DejaVuSans", "path": str(FONT_FILES["DejaVuSans"]), "weight": "400"},
                                                     {"label": "DejaVuSans-Bold", "path": str(FONT_FILES["DejaVuSans-Bold"]), "weight": "700"}]},
            "execution": {"inter_client_pause_seconds": 0, "client_timeout_seconds": 80000, "batch_deadline_seconds": 600000},
            "workbooks": {"reporting_spreadsheet_id": "1AbCdEfGhIjKlMnOpQrStUvWxYz0123456789"}}
    path = tmp_path / "runner.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    from seo_report import runner_config as rc
    settings = rc.load_settings(path, environ={"LOCALAPPDATA": str(local)})
    return SimpleNamespace(path=path, data=data, tmp=tmp_path, local=local, creds=creds, env={"LOCALAPPDATA": str(local)}, settings=settings,
                           clock=Clock(), ledger_path=local / "ledger" / "l.sqlite")


def loaded(*names, rejected=(), skipped=(), other_period=()):
    runs = {key_of(n): ClientRun(key_of(n), make_client(n), CURRENT) for n in names}
    for n in other_period:
        runs[key_of(n)] = ClientRun(key_of(n), make_client(n, report_start=OTHER_PERIOD[0], report_end=OTHER_PERIOD[1]), report_periods(*OTHER_PERIOD))
    return LoadResult(runs, tuple(rejected), tuple(skipped))


def via_sources(error):
    def fetch(*args, **kwargs):
        raise error

    def collect(client_run, today):
        return sc.collect_sources(client_run.client, client_run.periods, gsc_service=None, ga4_client=None, today=today, fetch_gsc=fetch, fetch_ga4=fetch)
    return collect


def cmd(home, monkeypatch, argv, *, result=None, gate=PERMIT, options=None, json_mode=True, probes=PROBES_OK, factory=None, capsys=None):
    if result is not None:
        monkeypatch.setattr(client_loader, "load_from_workbook", lambda *a, **k: result)
    services = {"renderer": StubRenderer(), "collect": default_collect, "sleep": lambda s: None, **(options or {})}
    full = (["--json"] if json_mode else []) + argv + ["--config", str(home.path)]
    code = cli.main(full, environ=home.env, gate=gate, services_options=services, preflight_probes=probes, clock=home.clock,
                    client_factory=factory or NoGoogle())
    out = capsys.readouterr().out if capsys else ""
    try:
        data = json.loads(out) if json_mode and out else out
    except ValueError:
        data = out
    return code, data


RUN = ["report", "run", "--client", "alpha-co", "--period", PERIOD, "--production"]
BATCH = ["report", "batch", "--period", PERIOD, "--production"]


def tree(folder):
    folder = Path(folder)
    return {str(p.relative_to(folder)): (hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else None) for p in sorted(folder.rglob("*"))}


def ledger_of(home, readonly=True):
    return lg.Ledger(home.ledger_path, clock=home.clock, readonly=readonly)


def reports_dir(home, key="alpha-co"):
    return layout.report_dir(home.settings.output_root, key, CURRENT.current.start, CURRENT.current.end)


# === production gate ===========================================================================================================

def test_the_real_gate_blocks_production_execution_today_with_fixed_blocker_codes(home, monkeypatch, capsys):
    guard = {"collect": lambda *a: (_ for _ in ()).throw(AssertionError("collection started"))}
    before = tree(home.tmp)
    code, data = cmd(home, monkeypatch, RUN, gate=None, options=guard, result=loaded("Alpha Co"), capsys=capsys)
    assert code == Exit.PREFLIGHT_FAILED and data["ok"] is False
    assert data["error"]["code"] == "production_gate_blocked" and data["details"]["blockers"] == ["live_google_not_verified"]
    assert tree(home.tmp) == before and not home.ledger_path.exists()  # no ledger, no folder, no report


def test_the_gate_blocks_every_production_command(home, monkeypatch, capsys):
    for argv in (RUN, BATCH, ["recovery", "acknowledge", "--run", f"alpha-co/{PERIOD}", "--operator", "jw", "--reason", "data_corrected", "--production"],
                 ["recovery", "cancel", "--run", f"alpha-co/{PERIOD}", "--operator", "jw", "--reason", "no_longer_needed", "--production"]):
        code, data = cmd(home, monkeypatch, argv, gate=None, capsys=capsys)
        assert code == Exit.PREFLIGHT_FAILED and "live_google_not_verified" in data["details"]["blockers"], argv
    assert not home.ledger_path.exists()


def test_missing_authentication_missing_assets_and_bad_configuration_map_to_their_exit_codes(home, monkeypatch, capsys):
    (home.creds / "login.json").unlink()
    code, data = cmd(home, monkeypatch, RUN, gate=None, capsys=capsys)
    assert code == Exit.AUTH_REQUIRED and "auth_token_missing" in data["details"]["blockers"]
    (home.creds / "login.json").write_text("{}", encoding="utf-8")
    real = home.settings
    missing = tmp_font = home.tmp / "gone.ttf"
    home.data["render"]["fonts"][1]["path"] = str(missing)
    home.path.write_text(yaml.safe_dump(home.data), encoding="utf-8")
    code, data = cmd(home, monkeypatch, RUN, gate=None, capsys=capsys)
    assert code == Exit.PREFLIGHT_FAILED and "asset_file_not_found" in data["details"]["blockers"]
    home.data["paths"]["ledger_db"] = "relative.sqlite"
    home.path.write_text(yaml.safe_dump(home.data), encoding="utf-8")
    assert cmd(home, monkeypatch, RUN, gate=None, capsys=capsys)[0] == Exit.CONFIG_INVALID
    assert not home.ledger_path.exists()


def test_the_gate_cannot_be_satisfied_without_live_verification_and_clears_credentials_only_when_relocated(home):
    inp = pf.Inputs(settings=home.settings, environ=home.env, app_version="8", probes={**PROBES_OK}, logging_status={"active": True, "code": None})
    allowed, code, blockers = pf.production_gate(inp)
    assert not allowed and code == Exit.PREFLIGHT_FAILED and [b.code for b in blockers] == ["live_google_not_verified"]
    assert pf.GATE_REQUIREMENTS == ("live_google_not_verified", "credentials_not_relocated")
    in_project = dataclasses.replace(home.settings, oauth_client_path=None, oauth_cache_path=None)
    codes = [b.code for b in pf.production_gate(dataclasses.replace(inp, settings=in_project))[2]]
    assert "credentials_not_relocated" in codes and "live_google_not_verified" in codes
    assert [b.code for b in pf.production_gate(pf.Inputs(settings=None, config_code="config_not_given", probes=PROBES_OK))[2]][:1] == ["config_not_given"]


def test_there_is_no_way_around_the_gate_from_the_command_line_or_the_environment(home, monkeypatch, capsys):
    for extra in (["--force"], ["--skip-gate"], ["--no-preflight"], ["--yes"]):
        assert cli.main(RUN + extra, environ=home.env) == Exit.USAGE
    env = {**home.env, "JASPER_SKIP_GATE": "1", "JASPER_PRODUCTION_OVERRIDE": "1", "SEO_REPORT_FORCE": "1"}
    code = cli.main(["--json", *RUN, "--config", str(home.path)], environ=env, preflight_probes=PROBES_OK, client_factory=NoGoogle())
    assert code == Exit.PREFLIGHT_FAILED and not home.ledger_path.exists()


def test_every_production_handler_calls_the_gate_before_touching_anything():
    source = (ROOT / "src" / "seo_report" / "cli_report.py").read_text(encoding="utf-8")
    assert source.count("production_gate") <= 3 and "def require_gate" in source
    for name in ("handle_run", "handle_batch", "_operator_action"):
        body = source.split(f"def {name}(")[1].split("\ndef ")[0]
        assert "require_gate(ctx, settings)" in body, name
        for later in ("open_ledger(", "_read_workbook(", "_services(", "pipeline.run_client(", "run_batch("):
            if later in body:
                assert body.index("require_gate(ctx, settings)") < body.index(later), (name, later)
    cli_source = (ROOT / "src" / "seo_report" / "cli.py").read_text(encoding="utf-8")
    assert "gate" not in cli_source.split("def build_parser")[1].split("def validate_args")[0]  # no option for it in the parser


# === dry-run ====================================================================================================================

def test_dry_run_is_a_plan_with_sources_steps_blockers_and_no_side_effects(home, monkeypatch, capsys):
    monkeypatch.setattr(pipeline, "run_client", lambda *a, **k: (_ for _ in ()).throw(AssertionError("the pipeline ran")))
    before = tree(home.tmp)
    code, data = cmd(home, monkeypatch, ["report", "dry-run", "--client", "alpha-co", "--period", PERIOD], gate=None, capsys=capsys)
    assert code == Exit.OK and data["ok"] is True and data["mode"] == "plan_only"
    assert [s["service"] for s in data["sources"]] == ["Google Sheets (reporting workbook)", "Google Search Console", "Google Analytics 4"]
    assert all(s["access"] == "read-only" for s in data["sources"]) and len(data["steps"]) == 8
    assert data["client_configuration"]["in_registry"] is True and data["client_configuration"]["workbook_row"] == "not_checked"
    assert "live_google_not_verified" in data["production_blockers"] and "no Google call or sign-in" in data["not_performed"]
    assert data["would_write"]["ledger"].startswith("%LOCALAPPDATA%") and str(home.tmp) not in json.dumps(data)
    assert tree(home.tmp) == before and not home.ledger_path.exists()


def test_dry_run_text_explains_what_a_real_run_would_do(home, monkeypatch, capsys):
    code, text = cmd(home, monkeypatch, ["report", "dry-run", "--client", "alpha-co", "--period", PERIOD], json_mode=False, gate=None, capsys=capsys)
    assert code == Exit.OK
    for phrase in ("Dry run (plan only)", "Would use:", "A real run would:", "mandatory audit gate", "It would write (not written now):", "Production blockers:",
                   "Not performed: no Google call or sign-in"):
        assert phrase in text, phrase


def test_dry_run_validates_client_period_and_configuration(home, monkeypatch, capsys):
    assert cmd(home, monkeypatch, ["report", "dry-run", "--client", "nobody-co", "--period", PERIOD], capsys=capsys)[0] == Exit.CONFIG_INVALID
    assert cli.main(["report", "dry-run", "--client", "Bad Key", "--period", PERIOD], environ=home.env) == Exit.USAGE
    assert cli.main(["report", "dry-run", "--client", "alpha-co", "--period", "2026-01-01_2026-01-02"], environ=home.env) == Exit.USAGE
    assert cli.main(["report", "dry-run", "--client", "alpha-co", "--period", PERIOD], environ={}) == Exit.CONFIG_INVALID  # no settings file named
    (home.tmp / "clients.yaml").write_text("clients: []\n", encoding="utf-8")
    assert cmd(home, monkeypatch, ["report", "dry-run", "--client", "alpha-co", "--period", PERIOD], capsys=capsys)[0] == Exit.CONFIG_INVALID


def test_dry_run_reports_preflight_problems_with_their_exit_codes(home, monkeypatch, capsys):
    base = ["report", "dry-run", "--client", "alpha-co", "--period", PERIOD]
    (home.creds / "login.json").unlink()
    assert cmd(home, monkeypatch, base, capsys=capsys)[0] == Exit.AUTH_REQUIRED
    (home.creds / "login.json").write_text("{}", encoding="utf-8")
    code, data = cmd(home, monkeypatch, base, probes={"chromium_folders": lambda e: []}, capsys=capsys)
    assert code == Exit.PREFLIGHT_FAILED and "chromium_missing" in data["preflight"]["blocked"]


def test_the_unsupported_scratch_dir_option_is_refused_not_silently_ignored(home, monkeypatch, capsys):
    monkeypatch.setattr(pipeline, "run_client", lambda *a, **k: (_ for _ in ()).throw(AssertionError("the pipeline ran")))
    before = tree(home.tmp)
    argv = ["--json", "report", "dry-run", "--client", "alpha-co", "--period", PERIOD, "--scratch-dir", str(home.tmp / "scratch"), "--config", str(home.path)]
    code = cli.main(argv, environ=home.env, client_factory=NoGoogle(), preflight_probes=PROBES_OK, clock=home.clock)
    err = capsys.readouterr().err
    assert code == Exit.USAGE and "--scratch-dir is not supported" in err and "renders nothing" in err and "remove the option" in err
    assert tree(home.tmp) == before and not (home.tmp / "scratch").exists() and not home.ledger_path.exists()


def test_dry_run_without_the_option_stays_planning_only_with_no_artifacts(home, monkeypatch, capsys):
    monkeypatch.setattr(pipeline, "run_client", lambda *a, **k: (_ for _ in ()).throw(AssertionError("the pipeline ran")))
    monkeypatch.setattr(pr, "render_pdf", lambda *a, **k: (_ for _ in ()).throw(AssertionError("a PDF was rendered")), raising=False)
    before = tree(home.tmp)
    code, data = cmd(home, monkeypatch, ["report", "dry-run", "--client", "alpha-co", "--period", PERIOD], gate=None, capsys=capsys)
    assert code == Exit.OK and data["mode"] == "plan_only"
    assert tree(home.tmp) == before and not home.ledger_path.exists()
    assert not list(home.local.rglob("*.pdf")) and not list((home.local / "output").iterdir())


# === report run ================================================================================================================

def test_a_synthetic_run_goes_through_the_real_pipeline_to_an_audited_report(home, monkeypatch, capsys):
    code, data = cmd(home, monkeypatch, RUN, result=loaded("Alpha Co"), capsys=capsys)
    assert code == Exit.OK and data["ok"] is True
    assert (data["run_key"], data["state"], data["outcome"], data["audit"]) == (f"alpha-co/{PERIOD}", "audited", "succeeded", "consistent")
    assert data["pdf16"] and data["attempt"] == 1 and data["error"] is None
    with ledger_of(home) as led:
        run = led.get_run(f"alpha-co/{PERIOD}")
        assert run.state == "audited" and run.record_name and led.get_lock("client:alpha-co") is None and led.get_lock("runner:global") is None
    assert (reports_dir(home) / "report.pdf").is_file() and str(home.tmp) not in json.dumps(data)


def test_a_repeated_run_is_idempotent_and_reports_already_complete(home, monkeypatch, capsys):
    cmd(home, monkeypatch, RUN, result=loaded("Alpha Co"), capsys=capsys)
    before = tree(reports_dir(home))
    code, data = cmd(home, monkeypatch, RUN, capsys=capsys)
    assert code == Exit.OK and data["outcome"] == "already_complete" and tree(reports_dir(home)) == before


def test_the_pipeline_receives_the_lazily_built_google_collector_by_default(home, monkeypatch, capsys):
    made = []
    monkeypatch.setattr(gc, "make_collect", lambda factory, **kw: made.append(factory) or default_collect)
    factory = gc.GoogleClientFactory(credentials_provider=lambda: (_ for _ in ()).throw(AssertionError("credentials loaded at build time")))
    services = {"renderer": StubRenderer(), "sleep": lambda s: None}  # no `collect`: the CLI must build it from the factory
    monkeypatch.setattr(client_loader, "load_from_workbook", lambda *a, **k: loaded("Alpha Co"))
    code = cli.main(["--json", *RUN, "--config", str(home.path)], environ=home.env, gate=PERMIT, services_options=services, clock=home.clock,
                    client_factory=factory, preflight_probes=PROBES_OK)
    assert code == Exit.OK and made == [factory]


@pytest.mark.parametrize("name,collect,renderer,file_ops,expected,category", [
    ("auth", lambda cr, today: (_ for _ in ()).throw(auth.AuthRequiredError("auth_token_missing")), None, None, Exit.AUTH_REQUIRED, "auth_required"),
    ("source", via_sources(gsc_source.SearchConsoleError("denied", 403)), None, None, Exit.SOURCE_UNAVAILABLE, "source_denied"),
    ("render", None, lambda *a, **k: (_ for _ in ()).throw(pr.PdfRenderError("browser_failed", "x")), None, Exit.REPORT_FAILED, "render_failed"),
    ("finalization", None, None, NthOp("rename_noreplace", 3), Exit.FINALIZATION_FAILED, "finalization_failed")])
def test_failures_end_with_the_matching_exit_code_and_a_failed_run(home, monkeypatch, capsys, name, collect, renderer, file_ops, expected, category):
    options = {k: v for k, v in (("collect", collect), ("renderer", renderer), ("file_ops", file_ops)) if v is not None}
    code, data = cmd(home, monkeypatch, RUN, result=loaded("Alpha Co"), options=options, capsys=capsys)
    assert code == expected and data["ok"] is False and data["state"] == "failed" and data["error"]["category"] == category
    with ledger_of(home) as led:
        assert led.get_run(f"alpha-co/{PERIOD}").state == "failed"


def test_an_audit_failure_blocks_the_run_and_it_needs_an_operator(home, monkeypatch, capsys):
    reports_dir(home).mkdir(parents=True)
    (reports_dir(home) / ff.partial_name("report.pdf", "0123456789ab")).write_bytes(b"stray")
    code, data = cmd(home, monkeypatch, RUN, result=loaded("Alpha Co"), capsys=capsys)
    assert code == Exit.AUDIT_FAILED and data["state"] == "blocked" and data["audit"] is None
    code, data = cmd(home, monkeypatch, RUN, capsys=capsys)  # running again does not unblock it
    assert code == Exit.OPERATOR_ACTION_REQUIRED and data["error"]["code"] == "operator_required"


def test_a_failed_run_is_retried_by_running_the_command_again(home, monkeypatch, capsys):
    code, data = cmd(home, monkeypatch, RUN, result=loaded("Alpha Co"), options={"file_ops": NthOp("rename_noreplace", 3)}, capsys=capsys)
    assert code == Exit.FINALIZATION_FAILED
    first = ledger_of(home).get_run(f"alpha-co/{PERIOD}")
    code, data = cmd(home, monkeypatch, RUN, options={"file_ops": None}, capsys=capsys)
    assert code == Exit.OK and data["state"] == "audited" and data["attempt"] == 2
    run = ledger_of(home).get_run(f"alpha-co/{PERIOD}")
    assert (run.generation_date, run.render_input_sha256, run.finalized_at) == (first.generation_date, first.render_input_sha256, first.finalized_at)


def test_an_unknown_client_is_a_configuration_error_before_any_google_work(home, monkeypatch, capsys):
    argv = ["report", "run", "--client", "zeta-co", "--period", PERIOD, "--production"]
    assert cmd(home, monkeypatch, argv, result=loaded("Alpha Co"), capsys=capsys)[0] == Exit.CONFIG_INVALID
    assert not home.ledger_path.exists()


def test_google_problems_reading_the_workbook_map_to_exit_codes_without_touching_the_ledger(home, monkeypatch, capsys):
    def unavailable(*a, **k):
        raise client_loader.WorkbookUnavailable("auth_required", "auth_token_missing")
    monkeypatch.setattr(client_loader, "load_from_workbook", unavailable)
    code, data = cmd(home, monkeypatch, RUN, capsys=capsys)
    assert code == Exit.AUTH_REQUIRED and "setup auth" in data["error"]["message"] and not home.ledger_path.exists()


def test_an_interrupt_during_the_run_returns_130_and_releases_the_locks(home, monkeypatch, capsys):
    def interrupt(client_run, today):
        raise KeyboardInterrupt()
    code, _ = cmd(home, monkeypatch, RUN, result=loaded("Alpha Co"), options={"collect": interrupt}, capsys=capsys)
    assert code == Exit.INTERRUPTED
    with ledger_of(home) as led:
        assert led.get_lock("client:alpha-co") is None and led.get_lock("runner:global") is None
        assert led.get_run(f"alpha-co/{PERIOD}").state == "collecting"  # consistent and recoverable by the next run


# === batch =====================================================================================================================

def test_a_batch_runs_clients_sequentially_and_isolates_a_failure(home, monkeypatch, capsys):
    order = []

    def collect(client_run, today):
        order.append(client_run.client_key)
        if client_run.client_key == "beta-co":
            return via_sources(gsc_source.SearchConsoleError("denied", 403))(client_run, today)
        return default_collect(client_run, today)
    bad = Rejection("gamma-co", 4, "ga4_property_missing")
    result = loaded("Alpha Co", "Beta Co", "Delta Co", rejected=[bad], skipped=[("old-co", "status_not_active")], other_period=["Gamma Co"])
    code, data = cmd(home, monkeypatch, BATCH, result=result, options={"collect": collect}, capsys=capsys)
    assert order == ["alpha-co", "beta-co", "delta-co"]  # sorted, one after another
    states = {r["client_key"]: r["state"] for r in data["results"]}
    assert states == {"alpha-co": "audited", "beta-co": "failed", "delta-co": "audited"}
    assert data["not_due_for_this_period"] == ["gamma-co"] and data["rejected"] == [{"client_key": "gamma-co", "sheet_row": 4, "code": "ga4_property_missing"}]
    assert data["skipped"] == [{"client_key": "old-co", "code": "status_not_active"}]
    assert code == Exit.SOURCE_UNAVAILABLE and data["exit_code"] == 6 and data["ok"] is False
    assert "contact" not in json.dumps(data).lower() and str(home.tmp) not in json.dumps(data)


def test_an_invalid_client_does_not_stop_the_valid_ones_but_the_exit_code_flags_it(home, monkeypatch, capsys):
    result = loaded("Alpha Co", rejected=[Rejection("beta-co", 3, "keywords_missing")])
    code, data = cmd(home, monkeypatch, BATCH, result=result, capsys=capsys)
    assert [r["state"] for r in data["results"]] == ["audited"] and code == Exit.CONFIG_INVALID


def test_a_clean_batch_exits_zero(home, monkeypatch, capsys):
    code, data = cmd(home, monkeypatch, BATCH, result=loaded("Alpha Co", "Beta Co"), capsys=capsys)
    assert code == Exit.OK and [r["outcome"] for r in data["results"]] == ["succeeded", "succeeded"] and data["ok"] is True


def test_batch_options_restrict_and_limit_the_clients(home, monkeypatch, capsys):
    result = loaded("Alpha Co", "Beta Co", "Delta Co")
    code, data = cmd(home, monkeypatch, BATCH + ["--client", "beta-co", "--client", "delta-co", "--max-clients", "1"], result=result, capsys=capsys)
    assert [r["client_key"] for r in data["results"]] == ["beta-co"] and code == Exit.OK
    code, data = cmd(home, monkeypatch, BATCH + ["--client", "zeta-co"], result=result, capsys=capsys)
    assert code == Exit.CONFIG_INVALID and data["not_eligible"] == ["zeta-co"]


def test_a_batch_with_nothing_runnable_never_creates_a_ledger(home, monkeypatch, capsys):
    code, data = cmd(home, monkeypatch, BATCH, result=loaded(rejected=[Rejection("alpha-co", 2, "client_row_invalid")]), capsys=capsys)
    assert code == Exit.CONFIG_INVALID and data["processed"] == 0 and not home.ledger_path.exists()


def test_authentication_failure_stops_the_batch_and_leaves_the_rest_unstarted(home, monkeypatch, capsys):
    def collect(client_run, today):
        if client_run.client_key == "beta-co":
            raise auth.AuthRequiredError("auth_token_missing")
        return default_collect(client_run, today)
    code, data = cmd(home, monkeypatch, BATCH, result=loaded("Alpha Co", "Beta Co", "Delta Co"), options={"collect": collect}, capsys=capsys)
    assert code == Exit.AUTH_REQUIRED and data["aborted"] == "auth_required" and data["not_started"] == 1
    assert [r["client_key"] for r in data["results"]] == ["alpha-co", "beta-co"]


def test_a_ledger_failure_is_a_batch_level_stop(home, monkeypatch, capsys):
    monkeypatch.setattr(lg.Ledger, "register_request", lambda self, request: (_ for _ in ()).throw(lg.LedgerError("ledger_unavailable")))
    code, data = cmd(home, monkeypatch, BATCH, result=loaded("Alpha Co", "Beta Co"), capsys=capsys)
    assert code == Exit.LEDGER_UNAVAILABLE and data["aborted"] == "ledger_unavailable" and data["ok"] is False


def test_locks_are_respected_in_a_batch(home, monkeypatch, capsys):
    other = lg.Ledger(home.ledger_path, clock=home.clock, pid_alive=lambda pid: True, owner_id="other")
    other.acquire_lock("client:beta-co", ttl_seconds=600)
    try:
        code, data = cmd(home, monkeypatch, BATCH, result=loaded("Alpha Co", "Beta Co"), capsys=capsys)
    finally:
        other.close()
    states = {r["client_key"]: (r["state"], r["outcome"]) for r in data["results"]}
    assert states["alpha-co"] == ("audited", "succeeded") and states["beta-co"][1] == "rejected" and code == Exit.LOCK_CONFLICT


# === status and recovery fixtures ================================================================================================

def craft(home, key="alpha-co", *, state="blocked", stage="rendering", category="layout_issues", code="render_has_layout_issues", **values):
    """A run made through the ledger alone (synthetic), in a given blocked/failed state."""
    led = lg.Ledger(home.ledger_path, clock=home.clock)
    try:
        run = led.create_run(key, PERIOD)
        run = led.transition(run.run_key, "collecting", expected_state="requested", expected_version=0, code="c",
                             values={"generation_date": "2026-11-02"})
        columns = {"failed_stage": stage, "error_category": category, "error_code": code, **values}
        if state == "failed":
            columns.update(retry_eligible=1, retry_kind="finalization")
        return led.transition(run.run_key, state, expected_state="collecting", expected_version=run.state_version, code="x", values=columns)
    finally:
        led.close()


def file_hash(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


# === status =====================================================================================================================

def test_status_without_a_ledger_says_so_and_creates_nothing(home, monkeypatch, capsys):
    code, data = cmd(home, monkeypatch, ["status"], capsys=capsys)
    assert code == Exit.OK and data["ledger"] == "not_created_yet" and data["runs"] == [] and not home.ledger_path.exists()
    code, data = cmd(home, monkeypatch, ["status", "--run", f"alpha-co/{PERIOD}"], capsys=capsys)
    assert code == Exit.CONFIG_INVALID and data["error"]["code"] == "run_not_found"


def test_status_lists_all_runs_filters_by_client_and_by_run_and_is_read_only(home, monkeypatch, capsys):
    cmd(home, monkeypatch, BATCH, result=loaded("Alpha Co", "Beta Co"), capsys=capsys)
    before = (file_hash(home.ledger_path), os.stat(home.ledger_path).st_mtime_ns, sorted(p.name for p in home.ledger_path.parent.iterdir()))
    code, data = cmd(home, monkeypatch, ["status"], capsys=capsys)
    assert code == Exit.OK and sorted(r["client_key"] for r in data["runs"]) == ["alpha-co", "beta-co"]
    one = data["runs"][0]
    assert one["finalized"] is True and one["audited"] is True and one["delivery"] == "not_tracked" and one["state"] == "audited"
    assert one["pdf16"] and len(one["pdf16"]) == 16 and str(home.tmp) not in json.dumps(data) and "@" not in json.dumps(data)
    code, data = cmd(home, monkeypatch, ["status", "--client", "beta-co"], capsys=capsys)
    assert [r["client_key"] for r in data["runs"]] == ["beta-co"]
    code, data = cmd(home, monkeypatch, ["status", "--run", f"alpha-co/{PERIOD}"], capsys=capsys)
    assert [r["run_key"] for r in data["runs"]] == [f"alpha-co/{PERIOD}"]
    uuid = data["runs"][0]["engine_run_id"]
    code, data = cmd(home, monkeypatch, ["status", "--run", uuid], capsys=capsys)
    assert code == Exit.OK and [r["run_key"] for r in data["runs"]] == [f"alpha-co/{PERIOD}"]
    assert cmd(home, monkeypatch, ["status", "--run", f"nobody-co/{PERIOD}"], capsys=capsys)[0] == Exit.CONFIG_INVALID
    after = (file_hash(home.ledger_path), os.stat(home.ledger_path).st_mtime_ns, sorted(p.name for p in home.ledger_path.parent.iterdir()))
    assert after == before  # not a byte, a timestamp or a journal file changed


def test_status_shows_failures_blocks_and_superseding_relationships(home, monkeypatch, capsys):
    craft(home, "alpha-co", state="failed", stage="finalizing", category="finalization_failed", code="promote_failed")
    craft(home, "beta-co")
    led = lg.Ledger(home.ledger_path, clock=home.clock)
    first = led.get_run(f"alpha-co/{PERIOD}")
    led.transition(first.run_key, "cancelled", expected_state="failed", expected_version=first.state_version, code="c")
    led.create_run("alpha-co", PERIOD, supersedes=first.run_key, link_kind="restart", operator="jw", reason="data_corrected")
    led.close()
    code, data = cmd(home, monkeypatch, ["status", "--client", "alpha-co"], capsys=capsys)
    views = {r["run_key"]: r for r in data["runs"]}
    assert views[f"alpha-co/{PERIOD}"]["state"] == "cancelled" and views[f"alpha-co/{PERIOD}"]["superseded_by"] == f"alpha-co/{PERIOD}~2"
    assert views[f"alpha-co/{PERIOD}~2"]["supersedes"] == f"alpha-co/{PERIOD}" and views[f"alpha-co/{PERIOD}~2"]["link_kind"] == "restart"
    blocked = cmd(home, monkeypatch, ["status", "--client", "beta-co"], capsys=capsys)[1]["runs"][0]
    assert (blocked["state"], blocked["stage"], blocked["error_code"], blocked["finalized"], blocked["audited"]) == (
        "blocked", "rendering", "render_has_layout_issues", False, False)


def test_status_text_never_claims_delivery(home, monkeypatch, capsys):
    cmd(home, monkeypatch, RUN, result=loaded("Alpha Co"), capsys=capsys)
    code, text = cmd(home, monkeypatch, ["status"], json_mode=False, capsys=capsys)
    assert "[finalized, audited]" in text and "Delivery is not tracked" in text and "delivered" not in text.split("Delivery is not tracked")[0]


# === recovery inspect ===========================================================================================================

def test_inspect_lists_blocked_and_failed_runs_with_actions_and_changes_nothing(home, monkeypatch, capsys):
    craft(home, "alpha-co")  # recoverable layout block
    craft(home, "beta-co", category="finalization_conflict", code="render_input_already_finalized", stage="rendering")  # not recoverable by retry
    craft(home, "gamma-co", state="failed", stage="finalizing", category="finalization_failed", code="promote_failed")
    (reports_dir(home, "alpha-co")).mkdir(parents=True)
    stray = reports_dir(home, "alpha-co") / ff.partial_name("report.pdf", "0123456789ab")
    stray.write_bytes(b"stray")
    before = (file_hash(home.ledger_path), tree(home.local), os.stat(home.ledger_path).st_mtime_ns)
    code, data = cmd(home, monkeypatch, ["recovery", "inspect"], capsys=capsys)
    assert code == Exit.OK
    by = {r["client_key"]: r for r in data["runs"]}
    assert by["alpha-co"]["actions"][0] == "acknowledge" and by["alpha-co"]["needs"] == "operator_acknowledgement" and by["alpha-co"]["partial_artifacts"] is True
    assert by["alpha-co"]["cancel_refusal"] is None and "cancel" in by["alpha-co"]["actions"]
    assert by["beta-co"]["needs"] == "condition_not_recoverable_by_retry" and by["beta-co"]["reconciliation_conflict"] is True
    assert by["gamma-co"]["actions"][0] == "report_run_retries" and by["gamma-co"]["lock"] == {"held": False}
    assert (file_hash(home.ledger_path), tree(home.local), os.stat(home.ledger_path).st_mtime_ns) == before and stray.exists()
    code, data = cmd(home, monkeypatch, ["recovery", "inspect", "--client", "gamma-co"], capsys=capsys)
    assert [r["client_key"] for r in data["runs"]] == ["gamma-co"]


def test_inspect_shows_lock_ownership_and_liveness_only_as_safe_facts(home, monkeypatch, capsys):
    craft(home, "alpha-co", state="failed", stage="finalizing", category="finalization_failed", code="x")
    holder = lg.Ledger(home.ledger_path, clock=home.clock, pid_alive=lambda pid: True, owner_id="someone")
    holder.acquire_lock("client:alpha-co", ttl_seconds=600)
    holder.close()
    code, data = cmd(home, monkeypatch, ["recovery", "inspect"], capsys=capsys)
    lock = data["runs"][0]["lock"]
    assert set(lock) == {"held", "lease_expired", "holder"} and lock["held"] is True and lock["holder"] in ("alive", "dead", "unknown")
    assert "someone" not in json.dumps(data)


def test_inspect_without_a_ledger_or_any_problem(home, monkeypatch, capsys):
    assert cmd(home, monkeypatch, ["recovery", "inspect"], capsys=capsys)[1]["ledger"] == "not_created_yet"
    cmd(home, monkeypatch, RUN, result=loaded("Alpha Co"), capsys=capsys)
    code, data = cmd(home, monkeypatch, ["recovery", "inspect"], capsys=capsys)
    assert code == Exit.OK and data["runs"] == []


# === acknowledge and cancel =====================================================================================================

def ack(home, monkeypatch, capsys, run=None, operator="op:jw", reason="blocking_condition_resolved", **kw):
    argv = ["recovery", "acknowledge", "--run", run or f"alpha-co/{PERIOD}", "--operator", operator, "--reason", reason, "--production"]
    return cmd(home, monkeypatch, argv, capsys=capsys, **kw)


def cancel(home, monkeypatch, capsys, run=None, **kw):
    argv = ["recovery", "cancel", "--run", run or f"alpha-co/{PERIOD}", "--operator", "op:jw", "--reason", "no_longer_needed", "--production"]
    return cmd(home, monkeypatch, argv, capsys=capsys, **kw)


def events(home, key):
    with ledger_of(home) as led:
        return [e.code for e in led.events_for(key)]


def test_a_valid_acknowledgement_is_recorded_without_moving_the_run(home, monkeypatch, capsys):
    craft(home)
    version = ledger_of(home).get_run(f"alpha-co/{PERIOD}").state_version
    code, data = ack(home, monkeypatch, capsys)
    assert code == Exit.OK, json.dumps(data)
    assert data["operator_action"] == {"action": "acknowledge", "operator": "op:jw", "reason": "blocking_condition_resolved"}
    run = ledger_of(home).get_run(f"alpha-co/{PERIOD}")
    assert (run.state, run.acknowledged, run.ack_operator, run.state_version) == ("blocked", 1, "op:jw", version + 1)
    assert "run_acknowledged" in events(home, run.run_key) and data["run"]["acknowledged"] is True


def test_acknowledgement_then_run_retries_and_still_passes_the_audit_gate(home, monkeypatch, capsys):
    craft(home)  # a layout block at rendering; re-running renders again
    assert ack(home, monkeypatch, capsys)[0] == Exit.OK
    code, data = cmd(home, monkeypatch, RUN, result=loaded("Alpha Co"), capsys=capsys)
    assert code == Exit.OK and data["state"] == "audited" and data["audit"] == "consistent" and data["attempt"] == 2


def test_a_repeated_acknowledgement_is_idempotent(home, monkeypatch, capsys):
    craft(home)
    ack(home, monkeypatch, capsys)
    code, data = ack(home, monkeypatch, capsys)
    assert code == Exit.OK and data["outcome"] == "already_complete"
    assert events(home, f"alpha-co/{PERIOD}").count("run_acknowledged") == 1


def test_an_unresolved_condition_refuses_the_acknowledgement(home, monkeypatch, capsys):
    craft(home)
    reports_dir(home).mkdir(parents=True)
    stray = reports_dir(home) / ff.partial_name("report.pdf", "0123456789ab")
    stray.write_bytes(b"stray")
    code, data = ack(home, monkeypatch, capsys)
    assert code == Exit.OPERATOR_ACTION_REQUIRED and data["error"]["code"] == "blocking_condition_unresolved"
    assert ledger_of(home).get_run(f"alpha-co/{PERIOD}").acknowledged == 0 and stray.exists()


def test_only_blocked_runs_can_be_acknowledged_and_unrecoverable_ones_are_refused(home, monkeypatch, capsys):
    craft(home, "alpha-co", state="failed", stage="finalizing", category="finalization_failed", code="x")
    craft(home, "beta-co", category="finalization_conflict", code="render_input_already_finalized", stage="rendering")
    assert ack(home, monkeypatch, capsys)[0] == Exit.OPERATOR_ACTION_REQUIRED
    code, data = ack(home, monkeypatch, capsys, run=f"beta-co/{PERIOD}")
    assert code == Exit.OPERATOR_ACTION_REQUIRED and data["error"]["code"] == "condition_not_recoverable"


def test_invalid_operator_and_reason_are_usage_errors_and_a_stale_version_is_a_conflict(home, monkeypatch, capsys):
    craft(home)
    argv = ["recovery", "acknowledge", "--run", f"alpha-co/{PERIOD}", "--production"]
    assert cli.main(argv + ["--operator", "a@b.test", "--reason", "data_corrected"], environ=home.env, gate=PERMIT) == Exit.USAGE
    assert cli.main(argv + ["--operator", "jw", "--reason", "my own words"], environ=home.env, gate=PERMIT) == Exit.USAGE
    monkeypatch.setattr(lg.Ledger, "acknowledge", lambda self, *a, **k: (_ for _ in ()).throw(lg.StaleStateError("stale_state")))
    code, data = ack(home, monkeypatch, capsys)
    assert code in (Exit.LOCK_CONFLICT, Exit.OPERATOR_ACTION_REQUIRED) and ledger_of(home).get_run(f"alpha-co/{PERIOD}").acknowledged == 0


def test_a_run_that_does_not_exist_or_is_not_the_latest_generation_is_refused(home, monkeypatch, capsys):
    assert ack(home, monkeypatch, capsys)[0] == Exit.CONFIG_INVALID  # no ledger at all: nothing is created
    assert not home.ledger_path.exists()
    craft(home)
    assert ack(home, monkeypatch, capsys, run=f"beta-co/{PERIOD}")[1]["error"]["code"] == "run_not_found"
    led = lg.Ledger(home.ledger_path, clock=home.clock)
    first = led.get_run(f"alpha-co/{PERIOD}")
    led.transition(first.run_key, "cancelled", expected_state="blocked", expected_version=first.state_version, code="c")
    led.create_run("alpha-co", PERIOD, supersedes=first.run_key, link_kind="restart", operator="jw", reason="data_corrected")
    led.close()
    code, data = ack(home, monkeypatch, capsys)  # generation 1 is no longer the latest
    assert code == Exit.OPERATOR_ACTION_REQUIRED and data["error"]["code"] == "not_latest_generation"


def test_a_valid_cancellation_changes_only_the_ledger_and_deletes_nothing(home, monkeypatch, capsys):
    craft(home, state="failed", stage="finalizing", category="finalization_failed", code="x")
    reports_dir(home).mkdir(parents=True)
    keep = reports_dir(home) / "report.render-input.keep"
    keep.write_bytes(b"sidecar")
    files = tree(home.local / "output")
    code, data = cancel(home, monkeypatch, capsys)
    assert code == Exit.OK and ledger_of(home).get_run(f"alpha-co/{PERIOD}").state == "cancelled"
    assert tree(home.local / "output") == files and keep.read_bytes() == b"sidecar"
    assert "run_cancelled" in events(home, f"alpha-co/{PERIOD}")
    code, again = cancel(home, monkeypatch, capsys)  # the same request is replayed from the ledger, not applied twice
    assert code == Exit.OK and again["replayed"] is True and events(home, f"alpha-co/{PERIOD}").count("run_cancelled") == 1


def test_an_audited_report_cannot_be_cancelled_and_its_files_are_untouched(home, monkeypatch, capsys):
    cmd(home, monkeypatch, RUN, result=loaded("Alpha Co"), capsys=capsys)
    before = (tree(home.local / "output"), ledger_of(home).get_run(f"alpha-co/{PERIOD}"))
    code, data = cancel(home, monkeypatch, capsys)
    assert code == Exit.OPERATOR_ACTION_REQUIRED and data["error"]["code"] == "not_cancellable"
    assert (tree(home.local / "output"), ledger_of(home).get_run(f"alpha-co/{PERIOD}")) == before


def test_recovery_commands_enforce_the_real_gate_and_leave_the_ledger_alone(home, monkeypatch, capsys):
    craft(home)
    before = (file_hash(home.ledger_path), os.stat(home.ledger_path).st_mtime_ns)
    code, data = ack(home, monkeypatch, capsys, gate=None)
    assert code == Exit.PREFLIGHT_FAILED and data["error"]["code"] == "production_gate_blocked"
    assert cancel(home, monkeypatch, capsys, gate=None)[0] == Exit.PREFLIGHT_FAILED
    assert (file_hash(home.ledger_path), os.stat(home.ledger_path).st_mtime_ns) == before


def test_recovery_needs_the_production_flag_before_anything_else(home, capsys):
    for action in ("acknowledge", "cancel"):
        argv = ["recovery", action, "--run", f"alpha-co/{PERIOD}", "--operator", "jw", "--reason", "no_longer_needed", "--config", str(home.path)]
        assert cli.main(argv, environ=home.env, gate=PERMIT) == Exit.USAGE
    assert not home.ledger_path.exists()


# === general guarantees =========================================================================================================

def test_no_command_in_this_module_writes_a_workbook_or_sends_anything():
    source = (ROOT / "src" / "seo_report" / "cli_report.py").read_text(encoding="utf-8")
    code = source.split('"""', 2)[2]
    for word in ("import requests", "urllib", "socket", "smtplib", "webhook", "unlink", "rmtree", "os.remove", ".values().update", ".values().append", "batchUpdate", "run_local_server",
                 "get_credentials", "from_authorized_user_file"):
        assert word not in code.replace("lines.append(", "").replace("items.append(", "").replace("actions.append(", "").replace("results.append(", ""), word


def test_logging_context_carries_the_client_and_period_for_report_commands(home, monkeypatch, capsys):
    real = [fn for fn in [cli.activate_logging] if fn][0]
    from seo_report import runner
    runner.configure_logging(home.tmp / "logs")
    try:
        cmd(home, monkeypatch, RUN, result=loaded("Alpha Co"), capsys=capsys)
        records = [json.loads(l) for l in (home.tmp / "logs" / "seo_report.log").read_text(encoding="utf-8").splitlines() if l.strip()]
    finally:
        runner.shutdown_logging()
    pipeline_records = [r for r in records if r["logger"] == "seo_report.pipeline"]
    assert pipeline_records and all(r.get("client_key") == "alpha-co" for r in pipeline_records)
