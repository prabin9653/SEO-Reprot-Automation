"""Stage 7b: optional direct Google Drive links for the dashboard (the Drive API looks up each copied PDF's ID).

Synthetic throughout: a fake Drive service, temporary folders, the stub renderer; no Google call.
"""
import datetime as dt
import json
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from seo_report import auth, cli, drive_links as dl, shared_reports as sr  # noqa: E402
from seo_report.cli_exit import Exit  # noqa: E402
from test_r86d_report_status_recovery import PERIOD, RUN, NoGoogle, cmd, home, loaded, nothing_leaves_the_machine  # noqa: E402,F401

NOW = dt.datetime(2026, 11, 20, 9, 30, tzinfo=dt.timezone.utc)
NAME = f"alpha-co_{PERIOD}.pdf"
FILE_ID = "1AbCdEfGhIjKlMnOpQrStUvWxYz012345"


class FakeDrive:
    """Answers `files().list(q=...)` from a name -> [ids] table and remembers the queries."""

    def __init__(self, table):
        self.table, self.queries, self._answer = table, [], None

    def files(self):
        return self

    def list(self, q, fields, pageSize, spaces):
        self.queries.append(q)
        name = q.split("'")[1]
        self._answer = {"files": [{"id": i} for i in self.table.get(name, [])]}
        return self

    def execute(self):
        return self._answer


def _paths(tmp_path):
    return auth.CredentialPaths(tmp_path / "creds" / "client.json", tmp_path / "creds" / "token.json", "settings")


class _Settings:
    def __init__(self, tmp_path):
        self.ledger_db_path = tmp_path / "ledger" / "l.sqlite"
        self.ledger_db_path.parent.mkdir(parents=True, exist_ok=True)


def _never():
    raise AssertionError("the Google Drive API must not be used")


# === the permission stays separate and small ===================================================================================

def test_the_reporting_permissions_are_unchanged_and_drive_has_its_own_read_only_scope():
    assert auth.SCOPES == ["https://www.googleapis.com/auth/spreadsheets.readonly", "https://www.googleapis.com/auth/webmasters.readonly",
                           "https://www.googleapis.com/auth/analytics.readonly"]
    assert dl.DRIVE_SCOPES == ["https://www.googleapis.com/auth/drive.metadata.readonly"]


def test_the_drive_token_is_its_own_file_beside_the_reporting_token(tmp_path):
    paths = _paths(tmp_path)
    assert dl.token_path(paths) == tmp_path / "creds" / "token_drive.json" and dl.token_path(paths) != paths.token_file


def test_the_reporting_credentials_are_still_loaded_with_the_reporting_scopes(tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(auth.Credentials, "from_authorized_user_file", classmethod(lambda cls, path, scopes=None: seen.append(scopes) or (_ for _ in ()).throw(FileNotFoundError())))
    with pytest.raises(auth.AuthRequiredError):
        auth.get_credentials(token_path=tmp_path / "t.json")
    with pytest.raises(auth.AuthRequiredError):
        auth.get_credentials_for_scopes(dl.DRIVE_SCOPES, token_path=tmp_path / "t.json")
    assert seen == [auth.SCOPES, dl.DRIVE_SCOPES]
    import inspect
    assert list(inspect.signature(auth.get_credentials).parameters) == ["token_path", "refresh_request", "paths"]  # the reporting entry point is unchanged


# === finding the id ============================================================================================================

def test_an_id_is_found_only_when_exactly_one_file_has_the_name():
    assert dl.find_id(FakeDrive({NAME: [FILE_ID]}), NAME) == FILE_ID
    assert dl.find_id(FakeDrive({}), NAME) is None
    assert dl.find_id(FakeDrive({NAME: [FILE_ID, "2AbCdEfGhIjKlMnOpQrStUvWx"]}), NAME) is None  # ambiguous: no guess
    assert dl.find_id(FakeDrive({NAME: ["bad id!"]}), NAME) is None


def test_only_validated_report_names_are_ever_sent_to_drive():
    service = FakeDrive({})
    for bad in ("x' or name contains '", "../../a.pdf", "alpha co_2026-10-01_2026-10-28.pdf", "alpha-co_2026.pdf"):
        assert dl.find_id(service, bad) is None
    assert service.queries == []
    assert dl.find_id(service, NAME) is None and len(service.queries) == 1
    assert "trashed = false" in service.queries[0] and "mimeType = 'application/pdf'" in service.queries[0]


def test_ids_are_looked_up_once_and_remembered(tmp_path):
    settings, paths, service = _Settings(tmp_path), _paths(tmp_path), FakeDrive({NAME: [FILE_ID]})
    ids, note = dl.resolve(settings, paths, [NAME], service_factory=lambda creds: service, credentials_getter=lambda: object())
    assert ids == {NAME: FILE_ID} and note is None and len(service.queries) == 1
    again, note = dl.resolve(settings, paths, [NAME], service_factory=lambda creds: _never(), credentials_getter=lambda: _never())
    assert again == {NAME: FILE_ID} and note is None  # from the local cache: no sign-in, no API call
    assert json.loads(dl.cache_path(settings).read_text(encoding="utf-8")) == {NAME: FILE_ID}


def test_a_file_drive_has_not_synchronised_yet_keeps_the_search_link_and_is_retried_later(tmp_path):
    settings, paths = _Settings(tmp_path), _paths(tmp_path)
    ids, note = dl.resolve(settings, paths, [NAME], service_factory=lambda c: FakeDrive({}), credentials_getter=lambda: object())
    assert ids == {} and note is None and not dl.cache_path(settings).exists()
    ids, _ = dl.resolve(settings, paths, [NAME], service_factory=lambda c: FakeDrive({NAME: [FILE_ID]}), credentials_getter=lambda: object())
    assert ids == {NAME: FILE_ID}


def test_without_the_drive_sign_in_nothing_is_looked_up_and_the_note_says_what_to_do(tmp_path):
    settings, paths = _Settings(tmp_path), _paths(tmp_path)  # the Drive token file does not exist
    assert dl.resolve(settings, paths, [NAME], service_factory=lambda c: _never()) == ({}, dl.NOTE_NEEDS_SETUP)


@pytest.mark.parametrize("error", [RuntimeError("boom"), OSError("offline")])
def test_any_other_failure_is_a_note_never_an_error(tmp_path, error):
    settings, paths = _Settings(tmp_path), _paths(tmp_path)

    def broken(creds):
        raise error

    assert dl.resolve(settings, paths, [NAME], service_factory=broken, credentials_getter=lambda: object()) == ({}, dl.NOTE_UNAVAILABLE)


def test_the_cache_is_untrusted_and_invalid_entries_are_ignored(tmp_path):
    settings = _Settings(tmp_path)
    dl.cache_path(settings).write_text(json.dumps({NAME: FILE_ID, "evil name.pdf": FILE_ID, f"beta-co_{PERIOD}.pdf": "x y", "alpha-co_bad.pdf": FILE_ID}), encoding="utf-8")
    assert dl.load_cache(dl.cache_path(settings)) == {NAME: FILE_ID}
    dl.cache_path(settings).write_text("not json", encoding="utf-8")
    assert dl.load_cache(dl.cache_path(settings)) == {}
    dl.cache_path(settings).write_text("[1, 2]", encoding="utf-8")
    assert dl.load_cache(dl.cache_path(settings)) == {}


# === the dashboard links =======================================================================================================

def _entries(file_id):
    return {"alpha-co": [{"period_id": PERIOD, "finished_at": None, "generated_by": None, "shared": True, "file_id": file_id}]}


def test_a_known_id_makes_the_link_open_the_pdf_and_anything_else_falls_back_to_the_search():
    direct = sr.render_dashboard(_entries(FILE_ID), {}, NOW)
    assert f'href="https://drive.google.com/file/d/{FILE_ID}/view"' in direct and sr.DRIVE_SEARCH not in direct
    for bad in (None, "", "short", "has space in it 1234567890", "x" * 200, 12345):
        page = sr.render_dashboard(_entries(bad), {}, NOW)
        assert sr.DRIVE_SEARCH in page and "/file/d/" not in page
    with pytest.raises(ValueError):
        dl.file_url("a b")


def test_publish_uses_the_resolver_and_survives_a_broken_one(tmp_path):
    out, shared = tmp_path / "out", tmp_path / "shared"
    folder = out / "reports" / "alpha-co" / PERIOD
    folder.mkdir(parents=True)
    (folder / "report.pdf").write_bytes(b"%PDF-1.7 x")
    settings = type("S", (), {"reports_dir": shared, "output_root": out})()
    describe = lambda key, period: ("2026-11-17T20:45:00+00:00", "maria")  # noqa: E731
    asked = []
    result = sr.publish(settings, names={}, describe=describe, now=NOW, link_resolver=lambda names: asked.append(names) or ({NAME: FILE_ID}, None))
    assert asked == [[NAME]] and result["direct_links"] == 1 and result["link_note"] is None
    assert f"/file/d/{FILE_ID}/view" in (shared / "dashboard.html").read_text(encoding="utf-8")

    def broken(names):
        raise RuntimeError("x")

    result = sr.publish(settings, names={}, describe=describe, now=NOW, link_resolver=broken)
    assert result["dashboard"] is True and result["direct_links"] == 0 and result["link_note"] == "drive_lookup_unavailable"
    assert sr.DRIVE_SEARCH in (shared / "dashboard.html").read_text(encoding="utf-8")


# === the commands ==============================================================================================================

def _share(home, folder):
    home.data["paths"]["shared_reports_dir"] = str(folder)
    home.path.write_text(yaml.safe_dump(home.data), encoding="utf-8")


def test_export_without_the_drive_sign_in_still_works_and_says_how_to_get_direct_links(home, monkeypatch, capsys):
    cmd(home, monkeypatch, RUN, result=loaded("Alpha Co"), capsys=capsys)
    _share(home, home.tmp / "drive")
    code, data = cmd(home, monkeypatch, ["report", "export"], capsys=capsys)
    assert code == Exit.OK and data["copied"] == [f"alpha-co/{PERIOD}"] and data["direct_links"] == 0 and data["link_note"] == "drive_signin_missing"
    code, text = cmd(home, monkeypatch, ["report", "export"], json_mode=False, capsys=capsys)
    assert code == Exit.OK and "setup drive" in text


def test_export_with_the_drive_sign_in_makes_direct_links_and_remembers_them(home, monkeypatch, capsys):
    cmd(home, monkeypatch, RUN, result=loaded("Alpha Co"), capsys=capsys)
    shared = home.tmp / "drive"
    _share(home, shared)
    service = FakeDrive({NAME: [FILE_ID]})
    monkeypatch.setattr(auth, "get_credentials_for_scopes", lambda *a, **kw: object())
    monkeypatch.setattr(dl, "build_service", lambda creds: service)
    code, data = cmd(home, monkeypatch, ["report", "export"], capsys=capsys)
    assert code == Exit.OK and data["direct_links"] == 1 and data["link_note"] is None
    assert f"/file/d/{FILE_ID}/view" in (shared / "dashboard.html").read_text(encoding="utf-8")
    assert (home.local / "ledger" / dl.CACHE_NAME).is_file()
    code, data = cmd(home, monkeypatch, ["report", "export"], capsys=capsys)
    assert data["direct_links"] == 1 and len(service.queries) == 1  # remembered: no second lookup


def test_a_run_never_fails_because_the_drive_lookup_does(home, monkeypatch, capsys):
    _share(home, home.tmp / "drive")
    monkeypatch.setattr(auth, "get_credentials_for_scopes", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("boom")))
    code, data = cmd(home, monkeypatch, RUN, result=loaded("Alpha Co"), capsys=capsys)
    assert code == Exit.OK and data["outcome"] == "succeeded" and (home.tmp / "drive" / "dashboard.html").is_file()


def _main(home, argv, **seams):
    return cli.main(["--json"] + argv + ["--config", str(home.path)], environ=home.env, client_factory=NoGoogle(), **seams)


def test_setup_drive_signs_in_with_the_drive_scope_into_its_own_token_file(home, capsys):
    calls = []
    code = _main(home, ["setup", "drive"], setup_runner=lambda **kw: calls.append(kw))
    assert code == Exit.OK
    (call,) = calls
    assert call["scopes"] == dl.DRIVE_SCOPES and Path(call["token_path"]).name.endswith("_drive.json")
    assert Path(call["token_path"]) != Path(call["paths"].token_file)
    out = json.loads(capsys.readouterr().out)
    assert out["data"]["permission"] == "drive.metadata.readonly" if "data" in out else out["permission"] == "drive.metadata.readonly"


def test_setup_drive_needs_an_attended_console_like_setup_auth(home, capsys):
    def blocked(**kw):
        raise auth.AuthRequiredError("auth_interactive_blocked")

    assert _main(home, ["setup", "drive"], setup_runner=blocked) == Exit.AUTH_REQUIRED
