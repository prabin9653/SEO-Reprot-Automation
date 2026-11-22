"""Stage 7: finished reports are copied to an optional shared folder (for example Google Drive) and a read-only dashboard lists them.

Synthetic throughout: temporary folders, the stub renderer and permissive gate of the run tests, no Google call.
"""
import datetime as dt
import hashlib
import json
import re
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from seo_report import cli, runner_config as rc, shared_reports as sr  # noqa: E402
from seo_report.cli_exit import Exit  # noqa: E402
from test_r86d_report_status_recovery import PERIOD, RUN, cmd, home, ledger_of, loaded, nothing_leaves_the_machine  # noqa: E402,F401

NOW = dt.datetime(2026, 11, 20, 9, 30, tzinfo=dt.timezone.utc)
PDF = b"%PDF-1.7 synthetic report bytes"


def _make_report(root: Path, key="alpha-co", period=PERIOD, data=PDF):
    folder = root / "reports" / key / period
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "report.pdf").write_bytes(data)
    return folder / "report.pdf"


# === the copy ==================================================================================================================

def test_a_report_is_copied_verified_and_never_overwritten(tmp_path):
    out, shared = tmp_path / "out", tmp_path / "shared"
    original = _make_report(out)
    assert sr.copy_report(shared, out, "alpha-co", PERIOD) == sr.COPIED
    target = shared / "alpha-co" / f"alpha-co_{PERIOD}.pdf"
    assert target.read_bytes() == PDF and original.read_bytes() == PDF
    assert sr.copy_report(shared, out, "alpha-co", PERIOD) == sr.SAME
    target.write_bytes(b"someone edited this")
    assert sr.copy_report(shared, out, "alpha-co", PERIOD) == sr.DIFFERS
    assert target.read_bytes() == b"someone edited this"  # left alone
    assert sorted(p.name for p in (shared / "alpha-co").iterdir()) == [f"alpha-co_{PERIOD}.pdf"]  # no temporary file left


def test_nothing_is_copied_for_a_missing_or_invalid_report(tmp_path):
    out, shared = tmp_path / "out", tmp_path / "shared"
    assert sr.copy_report(shared, out, "alpha-co", PERIOD) == sr.MISSING
    for key, period in (("../x", PERIOD), ("alpha-co", "bad"), ("Alpha Co", PERIOD)):
        assert sr.copy_report(shared, out, key, period) == sr.MISSING
    assert not shared.exists()


def test_a_copy_that_does_not_match_is_not_kept(tmp_path, monkeypatch):
    out, shared = tmp_path / "out", tmp_path / "shared"
    _make_report(out)
    real = sr._sha256
    calls = []

    def flaky(path):
        calls.append(path.name)
        return real(path) if path.name == "report.pdf" else "0" * 64  # the copy "differs" from the original

    monkeypatch.setattr(sr, "_sha256", flaky)
    assert sr.copy_report(shared, out, "alpha-co", PERIOD) == sr.FAILED
    assert [p.name for p in (shared / "alpha-co").iterdir()] == []  # no partial file, no target


def test_an_unusable_shared_folder_never_raises(tmp_path):
    out = tmp_path / "out"
    _make_report(out)
    blocker = tmp_path / "a-file"
    blocker.write_text("x", encoding="utf-8")
    assert sr.copy_report(blocker, out, "alpha-co", PERIOD) == sr.FAILED


def test_a_symlinked_report_is_not_followed(tmp_path):
    out, shared = tmp_path / "out", tmp_path / "shared"
    secret = tmp_path / "secret.pdf"
    secret.write_bytes(b"not a report")
    folder = out / "reports" / "alpha-co" / PERIOD
    folder.mkdir(parents=True)
    try:
        (folder / "report.pdf").symlink_to(secret)
    except (OSError, NotImplementedError):
        pytest.skip("symbolic links are not available here")
    assert sr.copy_report(shared, out, "alpha-co", PERIOD) == sr.MISSING


# === the dashboard =============================================================================================================

def _entries():
    return {"alpha-co": [{"period_id": PERIOD, "finished_at": "2026-11-17T20:45:00Z", "generated_by": "maria", "shared": True},
                         {"period_id": "2026-08-04_2026-08-31", "finished_at": None, "generated_by": None, "shared": True}],
            "beta-co": [{"period_id": PERIOD, "finished_at": None, "generated_by": None, "shared": False}]}


def test_the_dashboard_lists_clients_links_and_escapes_everything():
    names = {"alpha-co": 'Alpha <script>alert(1)</script> & "Co"', "beta-co": "Beta Co"}
    page = sr.render_dashboard(_entries(), names, NOW)
    assert "<script>alert(1)</script>" not in page and "&lt;script&gt;alert(1)&lt;/script&gt;" in page
    assert f'href="{sr.DRIVE_SEARCH}alpha-co_{PERIOD}.pdf"' in page and f'href="{sr.DRIVE_SEARCH}alpha-co_2026-08-04_2026-08-31.pdf"' in page
    assert f"beta-co_{PERIOD}" not in page and "not in this folder yet" in page  # no link to a file that is not there
    assert 'rel="noopener noreferrer"' in page
    assert 'datetime="2026-11-17T20:45:00Z"' in page and "maria" in page and "Updated" in page
    assert page.index("Alpha") < page.index("Beta Co")
    assert sr.render_dashboard({}, {}, NOW).count("No finished reports yet.") == 1


def test_the_dashboard_cannot_contact_anything_and_pins_its_one_script():
    page = sr.render_dashboard(_entries(), {"alpha-co": "Alpha Co"}, NOW)
    csp = re.search(r'Content-Security-Policy" content="([^"]+)"', page).group(1)
    assert "default-src 'none'" in csp and "connect-src" not in csp and "img-src 'none'" in csp
    script = re.search(r"<script>(.*)</script>", page, re.S).group(1)
    import base64
    assert f"'sha256-{base64.b64encode(hashlib.sha256(script.encode('utf-8')).digest()).decode()}'" in csp
    assert page.count("<script") == 1
    assert not re.search(r"src=|fetch\(|XMLHttpRequest|innerHTML|eval\(", page)
    urls = re.findall(r"https?://[^\"' <]+", page)  # the only addresses are the links that open a report in the viewer's own Drive
    assert urls and all(u.startswith(sr.DRIVE_SEARCH) for u in urls)


def test_the_dashboard_is_replaced_atomically_and_failures_never_raise(tmp_path):
    assert sr.write_dashboard(tmp_path / "s", _entries(), {}, NOW) is True
    assert sr.write_dashboard(tmp_path / "s", {}, {}, NOW) is True  # intentionally replaced
    assert "No finished reports yet." in (tmp_path / "s" / "dashboard.html").read_text(encoding="utf-8")
    assert [p.name for p in (tmp_path / "s").iterdir()] == ["dashboard.html"]
    blocker = tmp_path / "file"
    blocker.write_text("x", encoding="utf-8")
    assert sr.write_dashboard(blocker, _entries(), {}, NOW) is False


# === the setting ===============================================================================================================

def _settings(home, **paths):
    data = json.loads(json.dumps(home.data))
    data["paths"].update(paths)
    path = home.tmp / "variant.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return rc.load_settings(path, environ=home.env)


def test_the_shared_reports_folder_is_optional_absolute_and_never_a_storage_or_credential_folder(home):
    assert home.settings.reports_dir is None
    shared = home.tmp / "drive" / "SEO Reports"
    assert _settings(home, shared_reports_dir=str(shared)).reports_dir == shared
    for bad, code in ((str(home.local / "output" / "x"), "reports_dir_in_storage"), (str(home.local / "ledger"), "reports_dir_in_storage"),
                      (str(home.creds), "reports_dir_holds_credentials"), (str(ROOT / "config"), "reports_dir_in_project"),
                      ("relative/folder", "path_not_absolute")):
        with pytest.raises(rc.RunnerConfigError) as raised:
            _settings(home, shared_reports_dir=bad)
        assert raised.value.code == code, bad


# === end to end ================================================================================================================

def _share(home, folder):
    home.data["paths"]["shared_reports_dir"] = str(folder)
    home.path.write_text(yaml.safe_dump(home.data), encoding="utf-8")


def test_a_new_report_is_copied_and_listed_but_a_replay_changes_nothing(home, monkeypatch, capsys):
    shared = home.tmp / "drive"
    _share(home, shared)
    code, data = cmd(home, monkeypatch, RUN, result=loaded("Alpha Co"), capsys=capsys)
    assert code == Exit.OK and data["outcome"] == "succeeded"
    pdf = shared / "alpha-co" / f"alpha-co_{PERIOD}.pdf"
    original = home.local / "output" / "reports" / "alpha-co" / PERIOD / "report.pdf"
    assert pdf.is_file() and pdf.read_bytes() == original.read_bytes()
    page = (shared / "dashboard.html").read_text(encoding="utf-8")
    assert f"alpha-co_{PERIOD}.pdf" in page
    before = (pdf.stat().st_mtime_ns, (shared / "dashboard.html").stat().st_mtime_ns)
    code, data = cmd(home, monkeypatch, RUN, result=loaded("Alpha Co"), capsys=capsys)
    assert data["outcome"] == "already_complete"
    assert (pdf.stat().st_mtime_ns, (shared / "dashboard.html").stat().st_mtime_ns) == before


def test_the_run_text_says_what_was_copied(home, monkeypatch, capsys):
    _share(home, home.tmp / "drive")
    code, text = cmd(home, monkeypatch, RUN, result=loaded("Alpha Co"), json_mode=False, capsys=capsys)
    assert code == Exit.OK and f"Copied to the shared folder: alpha-co/{PERIOD}" in text


def test_a_shared_folder_that_cannot_be_used_never_fails_the_report(home, monkeypatch, capsys):
    blocker = home.tmp / "drive-is-a-file"
    blocker.write_text("x", encoding="utf-8")
    _share(home, blocker)
    code, text = cmd(home, monkeypatch, RUN, result=loaded("Alpha Co"), json_mode=False, capsys=capsys)
    assert code == Exit.OK and "WARNING: could not copy to the shared folder" in text
    code, data = cmd(home, monkeypatch, ["status"], capsys=capsys)
    assert data["runs"][0]["audited"] is True


def test_without_the_setting_nothing_is_written_outside_the_normal_storage(home, monkeypatch, capsys):
    code, text = cmd(home, monkeypatch, RUN, result=loaded("Alpha Co"), json_mode=False, capsys=capsys)
    assert code == Exit.OK and "shared folder" not in text and not (home.tmp / "drive").exists()


def test_export_without_a_folder_explains_what_to_do(home, monkeypatch, capsys):
    code, data = cmd(home, monkeypatch, ["report", "export"], capsys=capsys)
    assert code == Exit.CONFIG_INVALID and data["error"]["code"] == "shared_reports_not_configured"
    assert "shared_reports_dir" in data["error"]["fix"] and data["error"]["meaning"]


def test_export_copies_every_recorded_report_once_and_skips_unrecorded_folders(home, monkeypatch, capsys):
    cmd(home, monkeypatch, RUN, result=loaded("Alpha Co"), capsys=capsys)  # made before the folder was configured
    _make_report(home.local / "output", key="stray-co", period=PERIOD)  # a folder the ledger knows nothing about
    shared = home.tmp / "drive"
    _share(home, shared)
    code, data = cmd(home, monkeypatch, ["report", "export"], capsys=capsys)
    assert code == Exit.OK and data["copied"] == [f"alpha-co/{PERIOD}"] and data["not_recorded"] == 1 and data["dashboard"] is True
    assert not (shared / "stray-co").exists()
    code, data = cmd(home, monkeypatch, ["report", "export"], capsys=capsys)
    assert code == Exit.OK and data["copied"] == [] and data["already_there"] == 1


def test_export_reports_a_file_that_differs_and_leaves_it_alone(home, monkeypatch, capsys):
    cmd(home, monkeypatch, RUN, result=loaded("Alpha Co"), capsys=capsys)
    shared = home.tmp / "drive"
    (shared / "alpha-co").mkdir(parents=True)
    (shared / "alpha-co" / f"alpha-co_{PERIOD}.pdf").write_bytes(b"other")
    _share(home, shared)
    code, data = cmd(home, monkeypatch, ["report", "export"], capsys=capsys)
    assert code == Exit.CONFIG_INVALID and data["differs"] == [f"alpha-co/{PERIOD}"]
    assert (shared / "alpha-co" / f"alpha-co_{PERIOD}.pdf").read_bytes() == b"other"
