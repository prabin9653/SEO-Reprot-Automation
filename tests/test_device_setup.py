"""`seo setup device`: the per-device setup, with no network, no installs and no real credential. Everything happens in temporary folders; the (real) bundled fonts are
copied and verified, the permission change is replaced by a recording fake."""
import json
import re
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from seo_report import cli, client_config, device_setup as ds, runner_config as rc  # noqa: E402
from seo_report.cli_exit import Exit  # noqa: E402
from test_r86d_report_status_recovery import cmd, home, loaded, nothing_leaves_the_machine  # noqa: E402,F401
from seo_report.client_loader import Rejection  # noqa: E402

SHEET_ID = "1AbCdEfGhIjKlMnOpQrStUvWxYz0123456789"
SHEET_URL = f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/edit?gid=0#gid=0"


class Acl:
    def __init__(self, ok=True):
        self.ok, self.calls = ok, []

    def __call__(self, folder, environ):
        self.calls.append(Path(folder))
        return self.ok


@pytest.fixture
def device(tmp_path):
    local = tmp_path / "AppData"
    local.mkdir()
    return {"LOCALAPPDATA": str(local), "USERNAME": "tester"}, local / "JasperSEOReporting"


def by_name(result):
    return {s.name: s for s in result.steps}


# === the bundled fonts =========================================================================================================

def test_the_bundled_fonts_are_the_exact_files_the_report_needs():
    for label, filename, _weight, sha in ds.FONTS:
        assert ds.sha256_of(ROOT / "assets" / "fonts" / filename) == sha, label
    assert (ROOT / "assets" / "fonts" / ds.LICENSE_NAME).is_file()


# === a first run ===============================================================================================================

def test_a_first_run_creates_the_folders_fonts_and_a_valid_settings_file(device):
    env, base = device
    acl = Acl()
    result = ds.run_device_setup(env, spreadsheet=SHEET_URL, acl_applier=acl)
    steps = by_name(result)
    assert not result.problems
    assert steps["folders"].status == ds.CREATED and all((base / n).is_dir() for n in ("config", "credentials", "fonts"))
    assert acl.calls == [base / "credentials"] and steps["credentials_folder"].status == ds.OK
    for label, filename, _w, sha in ds.FONTS:
        assert steps[f"font_{label}"].status == ds.CREATED and ds.sha256_of(base / "fonts" / filename) == sha
    assert (base / "fonts" / ds.LICENSE_NAME).is_file()
    assert steps["settings"].status == ds.CREATED and result.config_path == base / "config" / "runner.yaml"
    settings = rc.load_settings(result.config_path, environ=env)
    assert settings.reporting_spreadsheet_id == SHEET_ID
    assert settings.oauth_client_path == base / "credentials" / "client.json" and settings.oauth_cache_path == base / "credentials" / "login.json"
    assert [f.path for f in settings.fonts] == [base / "fonts" / "DejaVuSans.ttf", base / "fonts" / "DejaVuSans-Bold.ttf"]
    assert not (base / "config" / "runner.yaml.new").exists()


def test_what_only_a_person_can_provide_is_listed_as_to_do_with_the_fix(device):
    env, base = device
    steps = by_name(ds.run_device_setup(env, spreadsheet=SHEET_ID, acl_applier=Acl(), project_root=ROOT))
    assert steps["client_file"].status == ds.TODO and "team lead" in steps["client_file"].fix and "never e-mail" in steps["client_file"].fix
    assert steps["sign_in"].status == ds.TODO and "setup auth" in steps["sign_in"].fix
    (base / "credentials" / "client.json").write_text("{}", encoding="utf-8")
    (base / "credentials" / "login.json").write_text("{}", encoding="utf-8")
    steps = by_name(ds.run_device_setup(env, spreadsheet=SHEET_ID, acl_applier=Acl(), project_root=ROOT))
    assert steps["client_file"].status == ds.OK and steps["sign_in"].status == ds.OK


def test_the_registry_is_reported_present_or_missing(device, tmp_path):
    env, _ = device
    fake_project = tmp_path / "project"
    (fake_project / "assets").mkdir(parents=True)
    import shutil
    shutil.copytree(ROOT / "assets" / "fonts", fake_project / "assets" / "fonts")
    steps = by_name(ds.run_device_setup(env, spreadsheet=SHEET_ID, acl_applier=Acl(), project_root=fake_project))
    assert steps["client_registry"].status == ds.TODO and "all-missing" in steps["client_registry"].fix and "lead" in steps["client_registry"].fix
    (fake_project / "config").mkdir()
    (fake_project / "config" / "clients.yaml").write_text("clients: []", encoding="utf-8")
    assert by_name(ds.run_device_setup(env, spreadsheet=SHEET_ID, acl_applier=Acl(), project_root=fake_project))["client_registry"].status == ds.OK


# === running it again, and never overwriting ===================================================================================

def test_running_it_again_changes_nothing(device):
    env, base = device
    ds.run_device_setup(env, spreadsheet=SHEET_ID, acl_applier=Acl())
    before = {p: p.read_bytes() for p in base.rglob("*") if p.is_file()}
    result = ds.run_device_setup(env, spreadsheet="1ZzZzZzZzZzZzZzZzZzZzZzZzZzZzZzZzZ", acl_applier=Acl())
    steps = by_name(result)
    assert steps["folders"].status == ds.OK and steps["settings"].status == ds.OK and "not changed" in steps["settings"].detail
    assert all(steps[f"font_{label}"].status == ds.OK for label, *_ in ds.FONTS)
    assert {p: p.read_bytes() for p in base.rglob("*") if p.is_file()} == before


def test_an_existing_settings_file_is_never_changed_even_when_invalid(device):
    env, base = device
    (base / "config").mkdir(parents=True)
    config = base / "config" / "runner.yaml"
    config.write_text("paths:\n  nonsense: 1\n", encoding="utf-8")
    steps = by_name(ds.run_device_setup(env, spreadsheet=SHEET_ID, acl_applier=Acl()))
    assert steps["settings"].status == ds.WARNING and "invalid" in steps["settings"].detail and config.read_text(encoding="utf-8") == "paths:\n  nonsense: 1\n"


def test_a_font_that_is_not_the_expected_file_is_reported_and_never_replaced(device):
    env, base = device
    (base / "fonts").mkdir(parents=True)
    (base / "fonts" / "DejaVuSans.ttf").write_bytes(b"not the font")
    result = ds.run_device_setup(env, spreadsheet=SHEET_ID, acl_applier=Acl())
    steps = by_name(result)
    assert steps["font_DejaVuSans"].status == ds.PROBLEM and (base / "fonts" / "DejaVuSans.ttf").read_bytes() == b"not the font"
    assert steps["font_DejaVuSans-Bold"].status == ds.CREATED and result.problems


def test_a_tampered_bundled_font_is_never_installed(device, tmp_path):
    env, base = device
    fake_project = tmp_path / "project"
    (fake_project / "assets" / "fonts").mkdir(parents=True)
    (fake_project / "assets" / "fonts" / "DejaVuSans.ttf").write_bytes(b"altered")
    result = ds.run_device_setup(env, spreadsheet=SHEET_ID, acl_applier=Acl(), project_root=fake_project)
    steps = by_name(result)
    assert steps["font_DejaVuSans"].status == ds.PROBLEM and not (base / "fonts" / "DejaVuSans.ttf").exists()
    assert steps["font_DejaVuSans-Bold"].status == ds.PROBLEM and not list((base / "fonts").glob("*.tmp"))


# === the workbook argument =====================================================================================================

@pytest.mark.parametrize("value,expected", [(SHEET_ID, SHEET_ID), (f"  {SHEET_ID}  ", SHEET_ID), (SHEET_URL, SHEET_ID),
                                            (f"https://docs.google.com/spreadsheets/d/{SHEET_ID}", SHEET_ID),
                                            ("https://evil.example/spreadsheets/d/" + SHEET_ID, None), ("short", None), ("REPLACE_WITH_REPORTING_SPREADSHEET_ID", None),
                                            ("", None), (None, None), (5, None), (SHEET_ID + " --force", None),
                                            ("http://docs.google.com/spreadsheets/d/" + SHEET_ID, None)])
def test_the_workbook_is_taken_from_an_id_or_a_sheets_address_only(value, expected):
    assert ds.parse_spreadsheet(value) == expected


def test_without_a_workbook_nothing_is_written_but_the_rest_is_still_done(device):
    env, base = device
    for spreadsheet in (None, "garbage"):
        result = ds.run_device_setup(env, spreadsheet=spreadsheet, acl_applier=Acl())
        steps = by_name(result)
        assert steps["settings"].status == ds.PROBLEM and "--spreadsheet-id" in steps["settings"].fix and not (base / "config" / "runner.yaml").exists()
        assert steps["folders"].status in (ds.CREATED, ds.OK) and steps["font_DejaVuSans"].status in (ds.CREATED, ds.OK)


# === safety ====================================================================================================================

def test_a_onedrive_location_is_refused_before_anything_is_created(tmp_path):
    local = tmp_path / "OneDrive" / "AppData"
    local.mkdir(parents=True)
    acl = Acl()
    result = ds.run_device_setup({"LOCALAPPDATA": str(local), "USERNAME": "t"}, spreadsheet=SHEET_ID, acl_applier=acl)
    assert [s.name for s in result.problems] == ["folders"] and not (local / "JasperSEOReporting").exists() and acl.calls == []


def test_a_failed_permission_change_is_a_warning_with_the_command_to_run(device):
    env, base = device
    steps = by_name(ds.run_device_setup(env, spreadsheet=SHEET_ID, acl_applier=Acl(ok=False)))
    assert steps["credentials_folder"].status == ds.WARNING and "icacls" in steps["credentials_folder"].fix and "/inheritance:r" in steps["credentials_folder"].fix


def test_the_settings_file_holds_no_credentials_and_is_valid_yaml(device):
    env, base = device
    result = ds.run_device_setup(env, spreadsheet=SHEET_ID, acl_applier=Acl())
    text = result.config_path.read_text(encoding="utf-8")
    data = yaml.safe_load(text)
    assert set(data) == {"paths", "render", "workbooks"} and set(data["paths"]) == {"oauth_client_file", "oauth_cache_file"}
    assert not re.search(r"ya29\.|GOCSPX|BEGIN [A-Z ]*PRIVATE KEY|Bearer", text)
    assert not (base / "credentials" / "client.json").exists() and not (base / "credentials" / "login.json").exists()  # never created by the setup


def test_the_setup_makes_no_network_call_and_reads_no_credential(device, monkeypatch):
    env, base = device
    import socket
    monkeypatch.setattr(socket.socket, "connect", lambda *a, **k: (_ for _ in ()).throw(AssertionError("network")))
    (base / "credentials").mkdir(parents=True)
    (base / "credentials" / "client.json").write_text('{"secret": "x"}', encoding="utf-8")
    (base / "credentials" / "login.json").write_text('{"token": "x"}', encoding="utf-8")
    opened = []
    real_open = Path.open
    monkeypatch.setattr(Path, "open", lambda self, *a, **k: (opened.append(self.name), real_open(self, *a, **k))[1])  # from here on, every file the setup opens is recorded
    ds.run_device_setup(env, spreadsheet=SHEET_ID, acl_applier=Acl())
    assert "client.json" not in opened and "login.json" not in opened


def test_the_permission_change_only_runs_on_windows_with_a_user_name(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(ds.subprocess, "run", lambda *a, **k: calls.append(a[0]) or type("R", (), {"returncode": 0})())
    assert ds.restrict_to_current_user(tmp_path, {}) is False and calls == []
    if ds.os.name == "nt":
        assert ds.restrict_to_current_user(tmp_path, {"USERNAME": "tester"}) is True
        assert calls[0][0] == "icacls" and "/inheritance:r" in calls[0] and "tester:(OI)(CI)F" in calls[0]
        monkeypatch.setattr(ds.subprocess, "run", lambda *a, **k: type("R", (), {"returncode": 5})())
        assert ds.restrict_to_current_user(tmp_path, {"USERNAME": "tester"}) is False


# === the command ===============================================================================================================

def test_the_command_needs_no_settings_file_and_reports_json(device, monkeypatch, capsys):
    env, base = device
    monkeypatch.setattr(ds, "restrict_to_current_user", Acl())
    code = cli.main(["--json", "setup", "device", "--spreadsheet-id", SHEET_URL], environ=env)
    body = json.loads(capsys.readouterr().out)
    assert code == Exit.OK and body["ok"] is True and body["problems"] == 0 and body["ready"] is False and body["todo"] >= 2
    assert (base / "config" / "runner.yaml").is_file()
    assert "AppData" not in json.dumps(body) or "%LOCALAPPDATA%" in json.dumps(body)  # paths are shown redacted


def test_the_command_exits_with_a_configuration_error_when_a_step_has_a_problem(device, monkeypatch, capsys):
    env, base = device
    monkeypatch.setattr(ds, "restrict_to_current_user", Acl())
    code = cli.main(["setup", "device"], environ=env)
    out = capsys.readouterr().out
    assert code == Exit.CONFIG_INVALID and "PROBLEM" in out and "--spreadsheet-id" in out and "->" in out


def test_after_the_setup_the_standard_settings_file_is_found_without_config(device, monkeypatch, capsys):
    env, base = device
    monkeypatch.setattr(ds, "restrict_to_current_user", Acl())
    cli.main(["setup", "device", "--spreadsheet-id", SHEET_ID], environ=env)
    assert cli.standard_config_path(env) == base / "config" / "runner.yaml"


def test_a_complete_device_says_it_is_ready(device, monkeypatch, capsys):
    env, base = device
    monkeypatch.setattr(ds, "restrict_to_current_user", Acl())
    cli.main(["setup", "device", "--spreadsheet-id", SHEET_ID], environ=env)
    (base / "credentials" / "client.json").write_text("{}", encoding="utf-8")
    (base / "credentials" / "login.json").write_text("{}", encoding="utf-8")
    capsys.readouterr()
    code = cli.main(["--json", "setup", "device"], environ=env)
    body = json.loads(capsys.readouterr().out)
    registry_present = (ROOT / "config" / "clients.yaml").is_file()
    assert code == Exit.OK and body["problems"] == 0 and body["ready"] is registry_present


# === building the client registry on a new device ==============================================================================

def test_all_missing_builds_a_registry_on_a_device_that_has_none(home, monkeypatch, capsys):
    (home.tmp / "clients.yaml").unlink()
    result = loaded(rejected=[Rejection(None, 3, "client_not_in_registry"), Rejection(None, 4, "client_not_in_registry")])
    import dataclasses
    result = dataclasses.replace(result, unregistered=((3, "Zeta Co"), (4, "R & D Home Solution")))
    code, data = cmd(home, monkeypatch, ["clients", "add", "--all-missing", "--yes"], result=result, capsys=capsys)
    assert code == Exit.OK and [a["client_key"] for a in data["added"]] == ["r-and-d-home-solution", "zeta-co"] and data["backup"] is False
    registry = client_config.load_client_registry(home.tmp / "clients.yaml")
    assert [(e.name, e.client_key) for e in registry.entries] == [("R & D Home Solution", "r-and-d-home-solution"), ("Zeta Co", "zeta-co")]
    assert "(no registry yet)" not in (home.tmp / "clients.yaml").read_text(encoding="utf-8")


def test_all_missing_on_a_device_without_a_registry_shows_only_a_plan_without_yes(home, monkeypatch, capsys):
    (home.tmp / "clients.yaml").unlink()
    import dataclasses
    result = dataclasses.replace(loaded(), unregistered=((3, "Zeta Co"),))
    code, data = cmd(home, monkeypatch, ["clients", "add", "--all-missing"], result=result, capsys=capsys)
    assert code == Exit.OK and data["written"] is False and not (home.tmp / "clients.yaml").exists()


# === the scripts and the guide =================================================================================================

def test_the_install_script_does_only_what_it_says():
    script = (ROOT / "scripts" / "install_device.ps1").read_text(encoding="utf-8")
    assert script.isascii()
    commands = re.findall(r"^\s*&\s+(\S+)\s+(.+)$", script, flags=re.MULTILINE)
    assert [c[1].split()[0:3] for c in commands if "pip" in c[1]] == [["-m", "pip", "install"]]
    assert "requirements-lock.txt" in script and "playwright install chromium" in script and "-e ." not in script
    for forbidden in ("Invoke-WebRequest", "curl", "iwr", "Set-ExecutionPolicy", "Start-Process", "setup auth --", "client_secret", "token"):
        assert forbidden not in script


def test_the_wrapper_and_the_guide_exist_and_name_the_real_commands():
    guide = (ROOT / "docs" / "team_setup.md").read_text(encoding="utf-8")
    for needle in ("install_device.ps1", "setup device", "setup auth", "doctor", "seo.cmd web", "clients add --all-missing"):
        assert needle in guide
    assert "never e-mail" in guide.lower() or "never e-mail or chat" in guide
