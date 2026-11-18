"""Stage 5: who ran each report (operator labels in the ledger), and the optional shared activity log that tells teammates a report already exists on another computer.

Synthetic throughout: temporary folders, the stub renderer and permissive gate of the run tests, no Google call.
"""
import dataclasses
import datetime as dt
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from seo_report import activity, cli, contracts, identity, runner_config as rc  # noqa: E402
from seo_report.cli_exit import Exit  # noqa: E402
from test_r86d_report_status_recovery import PERIOD, RUN, cmd, home, ledger_of, loaded, nothing_leaves_the_machine, via_sources  # noqa: E402,F401
from seo_report.sources import gsc as gsc_source  # noqa: E402

NOW = dt.datetime(2026, 11, 17, 20, 45, tzinfo=dt.timezone.utc)


# === operator labels ===========================================================================================================

@pytest.mark.parametrize("environ,expected", [
    ({"USERNAME": "pat"}, "pat"), ({"USERNAME": "Maria Lopez"}, "Maria_Lopez"), ({"USERNAME": "  j.doe  "}, "j.doe"),
    ({"JASPER_SEO_OPERATOR": "maria", "USERNAME": "shared-pc"}, "maria"), ({"JASPER_SEO_OPERATOR": "a@b.example", "USERNAME": "bob"}, "bob"),
    ({"USERNAME": "x" * 100}, "x" * 40), ({"USERNAME": "J\u00fcrgen"}, "J_rgen"), ({"USERNAME": "../../etc"}, "etc"), ({"USERNAME": "a:b|c"}, "a_b_c")])
def test_the_operator_name_is_the_windows_account_reduced_to_safe_characters(environ, expected):
    assert identity.operator_name(environ) == expected


def test_without_any_name_it_falls_back_to_the_system_user_then_unknown(monkeypatch):
    monkeypatch.setattr(identity.getpass, "getuser", lambda: "sysuser")
    assert identity.operator_name({}) == "sysuser" and identity.operator_name({"USERNAME": "///"}) == "sysuser"
    monkeypatch.setattr(identity.getpass, "getuser", lambda: (_ for _ in ()).throw(OSError("no user")))
    assert identity.operator_name({}) == "unknown"


@pytest.mark.parametrize("environ", [{"USERNAME": "Maria Lopez"}, {"USERNAME": "x" * 200, "JASPER_SEO_SOURCE": "web"}, {"USERNAME": "a:b"}, {"JASPER_SEO_OPERATOR": "o.k_-1"}])
def test_the_label_always_fits_the_ledgers_requester_pattern(environ):
    label = identity.requested_by(environ)
    assert contracts._REQUESTER.fullmatch(label) and label.split(":")[0] in ("cli", "web")


def test_the_source_is_web_only_when_the_page_says_so_and_names_are_shown_without_it():
    assert identity.requested_by({"USERNAME": "bob"}) == "cli:bob" and identity.requested_by({"USERNAME": "bob", "JASPER_SEO_SOURCE": "web"}) == "web:bob"
    assert identity.requested_by({"USERNAME": "bob", "JASPER_SEO_SOURCE": "evil"}) == "cli:bob"
    assert [identity.display_name(x) for x in ("web:maria", "cli:operator", "maria", "", None, "other:x", "cli:")] == ["maria", "operator", "maria", "", "", "other:x", "cli:"]


def _req(by, outcome=None, raw=None):
    return SimpleNamespace(requested_by=by, response_json=raw if raw is not None else (json.dumps({"outcome": outcome}) if outcome else None))


def test_the_author_is_the_person_whose_request_finished_the_report_not_a_later_replay():
    requests = [_req("cli:maria", "failed"), _req("web:maria", "succeeded"), _req("cli:joe", "already_complete"), _req("cli:ann", "already_complete")]
    assert identity.generated_by(requests) == "maria"
    assert identity.generated_by([_req("cli:a", "failed"), _req("cli:b", None)]) is None
    assert identity.generated_by([]) is None and identity.generated_by(None) is None
    assert identity.generated_by([_req("cli:a", raw="not json"), _req("cli:b", raw="[1]"), _req("cli:c", raw='"x"')]) is None
    assert identity.generated_by([_req("cli:a", "succeeded"), _req("cli:b", "succeeded")]) == "b"  # the latest finisher (a replacement after a supersede)


# === the shared activity log ===================================================================================================

ENV_MARIA = {"USERNAME": "maria", "COMPUTERNAME": "LAPTOP-1"}


def test_a_finished_report_is_recorded_as_one_small_line_in_the_operators_own_file(tmp_path):
    assert activity.record_done(tmp_path / "shared", ENV_MARIA, client_key="alpha-co", period_id=PERIOD, pdf16="0123456789abcdef", now=NOW) is True
    files = list((tmp_path / "shared").iterdir())
    assert [f.name for f in files] == ["activity-maria-LAPTOP-1.jsonl"]
    line = files[0].read_text(encoding="utf-8")
    assert line.count("\n") == 1 and json.loads(line) == {"v": 1, "kind": "report_done", "client_key": "alpha-co", "period_id": PERIOD, "operator": "maria", "device": "LAPTOP-1",
                                                           "at": "2026-11-17T20:45:00+00:00", "pdf16": "0123456789abcdef"}
    activity.record_done(tmp_path / "shared", ENV_MARIA, client_key="beta-co", period_id=PERIOD, pdf16=None, now=NOW)
    assert len(files[0].read_text(encoding="utf-8").splitlines()) == 2  # appended, never rewritten


def test_each_person_and_computer_writes_only_their_own_file(tmp_path):
    for env in (ENV_MARIA, {"USERNAME": "joe", "COMPUTERNAME": "LAPTOP-2"}, {"USERNAME": "maria", "COMPUTERNAME": "DESKTOP-9"}):
        activity.record_done(tmp_path, env, client_key="alpha-co", period_id=PERIOD, pdf16=None, now=NOW)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["activity-joe-LAPTOP-2.jsonl", "activity-maria-DESKTOP-9.jsonl", "activity-maria-LAPTOP-1.jsonl"]


def test_the_line_holds_no_path_email_credential_or_client_name(tmp_path):
    env = {**ENV_MARIA, "LOCALAPPDATA": "C:\\Users\\maria\\AppData\\Local", "GOOGLE_TOKEN": "ya29.secret"}
    activity.record_done(tmp_path, env, client_key="alpha-co", period_id=PERIOD, pdf16="0123456789abcdef", now=NOW)
    text = (tmp_path / "activity-maria-LAPTOP-1.jsonl").read_text(encoding="utf-8")
    assert set(json.loads(text)) == {"v", "kind", "client_key", "period_id", "operator", "device", "at", "pdf16"}
    assert "C:\\" not in text and "ya29" not in text and "@" not in text


@pytest.mark.parametrize("directory", [None, "file"])
def test_writing_never_raises_when_the_folder_is_unusable(tmp_path, directory):
    target = None
    if directory == "file":
        target = tmp_path / "not-a-folder"
        target.write_text("x", encoding="utf-8")
    assert activity.record_done(target, ENV_MARIA, client_key="alpha-co", period_id=PERIOD, pdf16=None, now=NOW) is False


def test_nothing_is_written_for_an_invalid_client_or_period(tmp_path):
    for key, period in (("../x", PERIOD), ("alpha-co", "bad"), ("Alpha Co", PERIOD)):
        assert activity.record_done(tmp_path, ENV_MARIA, client_key=key, period_id=period, pdf16=None, now=NOW) is False
    assert list(tmp_path.iterdir()) == []


def _event(**over):
    base = {"v": 1, "kind": "report_done", "client_key": "alpha-co", "period_id": PERIOD, "operator": "bob", "device": "LAPTOP-2", "at": "2026-11-17T21:00:00+00:00", "pdf16": None}
    base.update(over)
    return json.dumps(base)


def test_reading_treats_the_shared_folder_as_untrusted(tmp_path):
    good = _event()
    (tmp_path / "activity-bob-LAPTOP-2.jsonl").write_text("\n".join([
        good, "not json", "[]", "{}", _event(v=2), _event(kind="other"), _event(client_key="../../x"), _event(client_key="Alpha Co"), _event(period_id="x"),
        _event(operator="a b"), _event(operator="<script>"), _event(device="x" * 41), _event(at="yesterday"), _event(at="2026-11-17T21:00:00+02:00"),
        _event(operator=5), _event(pdf16="zz"), ""]), encoding="utf-8")
    events = activity.read_events(tmp_path)
    assert [(e["client_key"], e["operator"]) for e in events] == [("alpha-co", "bob"), ("alpha-co", "bob")]  # the good line and the one whose bad pdf16 is dropped
    assert events[1]["pdf16"] is None
    (tmp_path / "notes.txt").write_text(good, encoding="utf-8")
    (tmp_path / "activity-bad name.jsonl").write_text(good, encoding="utf-8")
    (tmp_path / "activity-x-y.jsonl.bak").write_text(good, encoding="utf-8")
    (tmp_path / "activity-folder-x.jsonl").mkdir()
    assert len(activity.read_events(tmp_path)) == 2


def test_reading_skips_oversized_binary_and_endless_files(tmp_path, monkeypatch):
    (tmp_path / "activity-big-A.jsonl").write_text(_event() + "\n" * 5, encoding="utf-8")
    monkeypatch.setattr(activity, "MAX_FILE_BYTES", 10)
    assert activity.read_events(tmp_path) == []
    monkeypatch.undo()
    (tmp_path / "activity-bin-A.jsonl").write_bytes(b"\xff\xfe\x00bad")
    assert [e["operator"] for e in activity.read_events(tmp_path)] == ["bob"]
    monkeypatch.setattr(activity, "MAX_LINES", 3)
    (tmp_path / "activity-many-A.jsonl").write_text("\n".join(_event(operator=f"p{n}") for n in range(50)), encoding="utf-8")
    assert len(activity.read_events(tmp_path)) <= 4
    assert activity.read_events(tmp_path / "missing") == [] and activity.read_events(None) == []


def test_only_a_report_from_a_different_computer_counts_as_elsewhere():
    events = [json.loads(_event(device="LAPTOP-1", at="2026-11-17T20:00:00+00:00")), json.loads(_event(device="LAPTOP-2", operator="bob", at="2026-11-17T21:00:00+00:00")),
              json.loads(_event(device="LAPTOP-3", operator="ann", at="2026-11-17T22:00:00+00:00")), json.loads(_event(client_key="beta-co", device="LAPTOP-9"))]
    assert activity.latest_elsewhere(events, "alpha-co", PERIOD, "LAPTOP-1")["operator"] == "ann"
    assert activity.latest_elsewhere(events, "alpha-co", PERIOD, "LAPTOP-3")["operator"] == "bob"
    assert activity.latest_elsewhere(events[:1], "alpha-co", PERIOD, "LAPTOP-1") is None
    assert activity.latest_elsewhere(events, "alpha-co", "2026-01-01_2026-01-28", "LAPTOP-1") is None


# === the setting ===============================================================================================================

def _settings(home, **paths):
    data = json.loads(json.dumps(home.data))
    data["paths"].update(paths)
    path = home.tmp / "variant.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return rc.load_settings(path, environ=home.env)


def test_the_shared_folder_is_optional_absolute_and_never_a_storage_or_credential_folder(home):
    assert home.settings.activity_dir is None
    shared = home.tmp / "team-share"
    assert _settings(home, shared_activity_dir=str(shared)).activity_dir == shared
    for bad, code in ((str(home.local / "output" / "x"), "activity_dir_in_storage"), (str(home.local / "ledger"), "activity_dir_in_storage"),
                      (str(home.local / "work"), "activity_dir_in_storage"), (str(home.creds), "activity_dir_holds_credentials"),
                      ("relative/folder", "path_not_absolute")):
        with pytest.raises(rc.RunnerConfigError) as raised:
            _settings(home, shared_activity_dir=bad)
        assert raised.value.code == code, bad
    with pytest.raises(rc.RunnerConfigError):
        _settings(home, shared_activity_dir=str(home.tmp / "a" / ".." / "b"))


# === end to end: a synthetic run ===============================================================================================

def _run(home, monkeypatch, capsys, env=None, argv=RUN, result=None):
    home.env.update(env or {})
    return cmd(home, monkeypatch, argv, result=result or loaded("Alpha Co"), capsys=capsys)


def _requests(home):
    ledger = ledger_of(home)
    try:
        return ledger.requests_for_run(f"alpha-co/{PERIOD}")
    finally:
        ledger.close()


def test_a_run_is_recorded_under_the_operator_and_the_author_survives_later_replays(home, monkeypatch, capsys):
    code, data = _run(home, monkeypatch, capsys, {"USERNAME": "Maria Lopez"})
    assert code == Exit.OK and data["outcome"] == "succeeded"
    assert [r.requested_by for r in _requests(home)] == ["cli:Maria_Lopez"]
    code, data = _run(home, monkeypatch, capsys, {"USERNAME": "joe", "JASPER_SEO_SOURCE": "web"})
    assert code == Exit.OK and data["outcome"] == "already_complete"
    assert [r.requested_by for r in _requests(home)][-1] == "web:joe"
    code, status = cmd(home, monkeypatch, ["status"], capsys=capsys)
    run = status["runs"][0]
    assert run["generated_by"] == "Maria_Lopez" and run["finalized_at"] and run["audited"] is True


def test_a_failed_run_has_no_author(home, monkeypatch, capsys):
    code, _ = cmd(home, monkeypatch, RUN, result=loaded("Alpha Co"), options={"collect": via_sources(gsc_source.SearchConsoleError("denied", 403))}, capsys=capsys)
    assert code == Exit.SOURCE_UNAVAILABLE
    code, status = cmd(home, monkeypatch, ["status"], capsys=capsys)
    assert status["runs"][0]["audited"] is False and status["runs"][0]["generated_by"] is None


def test_clients_check_shows_who_generated_each_report_here(home, monkeypatch, capsys):
    _run(home, monkeypatch, capsys, {"USERNAME": "maria"})
    code, data = cmd(home, monkeypatch, ["clients", "check"], result=loaded("Alpha Co", "Beta Co"), capsys=capsys)
    rows = {r["client_key"]: r for r in data["clients"] if r["status"] == "ready"}
    assert rows["alpha-co"]["report"] == "done" and rows["alpha-co"]["generated_by"] == "maria" and rows["beta-co"]["generated_by"] is None
    code, text = cmd(home, monkeypatch, ["clients", "check"], result=loaded("Alpha Co"), json_mode=False, capsys=capsys)
    assert "by maria" in text


def _share(home, folder):
    home.data["paths"]["shared_activity_dir"] = str(folder)
    home.path.write_text(yaml.safe_dump(home.data), encoding="utf-8")


def test_a_new_report_is_shared_but_a_replay_or_a_failure_is_not(home, monkeypatch, capsys):
    shared = home.tmp / "shared"
    _share(home, shared)
    code, data = _run(home, monkeypatch, capsys, {"USERNAME": "maria", "COMPUTERNAME": "LAPTOP-1"})
    assert code == Exit.OK
    events = activity.read_events(shared)
    assert [(e["client_key"], e["period_id"], e["operator"], e["device"], e["pdf16"]) for e in events] == [("alpha-co", PERIOD, "maria", "LAPTOP-1", data["pdf16"])]
    _run(home, monkeypatch, capsys, {"USERNAME": "joe"})  # already_complete
    assert len(activity.read_events(shared)) == 1


def test_a_shared_folder_that_cannot_be_written_never_fails_the_report(home, monkeypatch, capsys):
    blocker = home.tmp / "shared-is-a-file"
    blocker.write_text("x", encoding="utf-8")
    _share(home, blocker)
    code, data = _run(home, monkeypatch, capsys, {"USERNAME": "maria"})
    assert code == Exit.OK and data["outcome"] == "succeeded" and data["state"] == "audited"


def test_clients_check_warns_about_a_report_that_exists_on_another_computer(home, monkeypatch, capsys):
    shared = home.tmp / "shared"
    _share(home, shared)
    shared.mkdir()
    (shared / "activity-bob-LAPTOP-2.jsonl").write_text(_event(client_key="beta-co", period_id=PERIOD) + "\n" + _event(client_key="alpha-co", device="LAPTOP-9") + "\n", encoding="utf-8")
    _run(home, monkeypatch, capsys, {"USERNAME": "maria", "COMPUTERNAME": "LAPTOP-1"})  # alpha-co is DONE here
    code, data = cmd(home, monkeypatch, ["clients", "check"], result=loaded("Alpha Co", "Beta Co", "Gamma Co"), capsys=capsys)
    rows = {r["client_key"]: r for r in data["clients"] if r["status"] == "ready"}
    assert rows["beta-co"]["elsewhere"] == {"operator": "bob", "device": "LAPTOP-2", "at": "2026-11-17T21:00:00+00:00"} and rows["beta-co"]["report"] == "not_run"
    assert rows["alpha-co"]["elsewhere"] is None  # it is done on this computer: nothing to warn about
    assert rows["gamma-co"]["elsewhere"] is None
    code, text = cmd(home, monkeypatch, ["clients", "check"], result=loaded("Beta Co"), json_mode=False, capsys=capsys)
    assert "already generated on another computer (LAPTOP-2) by bob" in text and "second copy" in text


def test_the_same_computer_is_never_warned_about_itself(home, monkeypatch, capsys):
    shared = home.tmp / "shared"
    _share(home, shared)
    shared.mkdir()
    (shared / "activity-bob-LAPTOP-2.jsonl").write_text(_event(client_key="beta-co", device="LAPTOP-1") + "\n", encoding="utf-8")
    home.env["COMPUTERNAME"] = "LAPTOP-1"
    code, data = cmd(home, monkeypatch, ["clients", "check"], result=loaded("Beta Co"), capsys=capsys)
    assert data["clients"][0]["elsewhere"] is None


def test_without_a_shared_folder_nothing_is_read_or_written_outside_the_normal_storage(home, monkeypatch, capsys):
    from test_r86d_report_status_recovery import tree
    _run(home, monkeypatch, capsys, {"USERNAME": "maria"})
    assert home.settings.activity_dir is None and not (home.tmp / "shared").exists()
    code, data = cmd(home, monkeypatch, ["clients", "check"], result=loaded("Alpha Co"), capsys=capsys)
    assert all(r.get("elsewhere") is None for r in data["clients"])


# === device setup and the page =================================================================================================

def test_the_device_setup_can_write_the_shared_folder_into_a_new_settings_file(tmp_path):
    from seo_report import device_setup as ds
    local = tmp_path / "AppData"
    local.mkdir()
    env = {"LOCALAPPDATA": str(local), "USERNAME": "t"}
    shared = tmp_path / "team" / "seo-activity"
    result = ds.run_device_setup(env, spreadsheet="1AbCdEfGhIjKlMnOpQrStUvWxYz0123456789", acl_applier=lambda *a: True, shared_folder=str(shared))
    assert not result.problems and rc.load_settings(result.config_path, environ=env).activity_dir == shared
    other = tmp_path / "AppData2"
    other.mkdir()
    bad = ds.run_device_setup({"LOCALAPPDATA": str(other), "USERNAME": "t"}, spreadsheet="1AbCdEfGhIjKlMnOpQrStUvWxYz0123456789", acl_applier=lambda *a: True, shared_folder="relative\\x")
    assert [s.name for s in bad.problems] == ["settings"] and not (other / "JasperSEOReporting" / "config" / "runner.yaml").exists()


def test_the_web_page_marks_its_runs_as_started_from_the_page_and_shows_the_attribution(monkeypatch):
    from seo_report import webui, webui_page
    seen = {}
    monkeypatch.setattr(webui.subprocess, "run", lambda args, **kw: seen.update(env=kw["env"]) or SimpleNamespace(stdout="{}", returncode=0))
    webui.CliRunner(environ={"USERNAME": "maria"})(["clients", "check"], 5)
    assert seen["env"]["JASPER_SEO_SOURCE"] == "web" and seen["env"]["USERNAME"] == "maria"
    page = webui_page.PAGE
    assert "generated_by" in page and "elsewhere" in page and "second copy" in page and "Generated by" in page
