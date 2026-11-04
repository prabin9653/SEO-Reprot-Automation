"""R8.4.3: canonical path validation, storage isolation and network-drive refusal (temporary folders only).

Links are created inside pytest's temporary folder and nothing outside it is touched. Tests that need a capability the
machine genuinely lacks (symbolic links without the privilege, junctions, 8.3 short names) skip with the reason.
"""
import ctypes
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from seo_report import ledger as lg
from seo_report import pipeline
from seo_report import runner_config as rc

WINDOWS = os.name == "nt"
requires_windows = pytest.mark.skipif(not WINDOWS, reason="Windows path semantics")


def junction(link: Path, target: Path) -> Path:
    result = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], capture_output=True)
    if result.returncode != 0:
        pytest.skip("directory junctions cannot be created here")
    return link


def symlink(link: Path, target: Path) -> Path:
    try:
        os.symlink(target, link, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symbolic links need a privilege this account does not have")
    return link


def short_name(path: Path) -> str | None:
    if not WINDOWS:
        return None
    buffer = ctypes.create_unicode_buffer(32768)
    ctypes.windll.kernel32.GetShortPathNameW(str(path), buffer, len(buffer))
    return buffer.value or None


@pytest.fixture
def local(tmp_path):
    base = tmp_path / "local"
    for name in ("output", "history", "ledger", "work"):
        (base / name).mkdir(parents=True)
    return base


def settings(local, tmp_path, **paths):
    data = {"paths": {"output_root": str(local / "output"), "history_db": str(local / "history" / "h.sqlite"),
                      "ledger_db": str(local / "ledger" / "l.sqlite"), "work_root": str(local / "work"), **paths}}
    return rc.parse_settings(data, environ={"LOCALAPPDATA": str(local)})


def refused(local, tmp_path, code, **paths):
    """`code` may be a tuple: an earlier, already existing protection (OneDrive, the output-root rules) may answer first."""
    with pytest.raises(rc.RunnerConfigError) as caught:
        settings(local, tmp_path, **paths)
    assert caught.value.code in ((code,) if isinstance(code, str) else code), caught.value
    return caught.value


# --- canonical form ---------------------------------------------------------------------------------------------------------

def test_ordinary_local_paths_are_accepted(local, tmp_path):
    result = settings(local, tmp_path)
    assert result.ledger_db_path.name == "l.sqlite"


@requires_windows
def test_case_differences_do_not_hide_an_overlap(local, tmp_path):
    shouting = str(local / "OUTPUT" / "Ledger.sqlite").upper().replace("LOCAL", "local")
    refused(local, tmp_path, "ledger_overlaps_output", ledger_db=shouting)


def test_the_canonical_key_ignores_case_on_windows_and_never_creates_anything(tmp_path):
    missing = tmp_path / "does" / "not" / "exist.sqlite"
    key = rc.canonical_key(missing)
    assert not (tmp_path / "does").exists()
    if WINDOWS:
        assert rc.canonical_key(str(missing).upper()) == key


def test_a_symlink_to_the_output_folder_is_an_overlap(local, tmp_path):
    link = symlink(tmp_path / "alias", local / "output")
    refused(local, tmp_path, "work_root_overlaps", work_root=str(link / "work"))
    refused(local, tmp_path, "ledger_overlaps_output", ledger_db=str(link / "l.sqlite"))


@requires_windows
def test_a_junction_to_the_output_folder_is_an_overlap(local, tmp_path):
    link = junction(tmp_path / "alias", local / "output")
    refused(local, tmp_path, "work_root_overlaps", work_root=str(link / "work"))
    refused(local, tmp_path, "ledger_overlaps_output", ledger_db=str(link / "l.sqlite"))


@requires_windows
def test_a_junction_into_the_project_is_inside_the_project(local, tmp_path):
    link = junction(tmp_path / "proj", rc.PROJECT_ROOT)
    refused(local, tmp_path, "path_inside_project", ledger_db=str(link / "l.sqlite"))
    refused(local, tmp_path, "path_inside_project", work_root=str(link / "w"))
    refused(local, tmp_path, ("path_inside_project", "path_in_cloud_sync"), history_db=str(link / "src" / "h.sqlite"))
    refused(local, tmp_path, ("path_inside_project", "output_root_invalid"), output_root=str(link / "out"))


@requires_windows
def test_a_nonexistent_destination_under_an_aliased_parent_resolves_through_the_parent(local, tmp_path):
    link = junction(tmp_path / "alias", local / "output")
    deep = link / "not" / "yet" / "created" / "l.sqlite"
    assert not deep.exists()
    assert rc.canonical_key(deep) == rc.canonical_key(local / "output" / "not" / "yet" / "created" / "l.sqlite")
    refused(local, tmp_path, "ledger_overlaps_output", ledger_db=str(deep))
    assert not (local / "output" / "not").exists()  # validation created nothing


def test_a_parent_directory_symlink_is_resolved(local, tmp_path):
    outer = symlink(tmp_path / "outer", local)
    refused(local, tmp_path, "ledger_overlaps_output", ledger_db=str(outer / "output" / "sub" / "l.sqlite"))


@requires_windows
def test_an_8_3_short_name_is_the_same_folder(local, tmp_path):
    long_dir = local / "Production Output Folder"
    long_dir.mkdir()
    short = short_name(long_dir)
    if not short or Path(short).name.lower() == long_dir.name.lower():
        pytest.skip("8.3 short names are disabled on this volume")
    assert rc.canonical_key(short) == rc.canonical_key(long_dir)
    refused(local, tmp_path, "ledger_overlaps_output", output_root=str(long_dir),
            ledger_db=str(Path(short) / "l.sqlite"))


@pytest.mark.parametrize("bad", ["rel/path.sqlite", "l.sqlite"])
def test_relative_paths_are_refused(local, tmp_path, bad):
    refused(local, tmp_path, "path_not_absolute", ledger_db=bad)


def test_parent_references_are_ambiguous_and_refused(local, tmp_path):
    refused(local, tmp_path, "path_ambiguous", ledger_db=str(local / "ledger" / ".." / "output" / "l.sqlite"))
    with pytest.raises(rc.RunnerConfigError) as caught:
        rc.canonical_path(str(local / "ledger" / ".." / "x"))
    assert caught.value.code == "path_ambiguous"


@requires_windows
@pytest.mark.parametrize("name", ["out.", "out ", "ledger:stream", "NUL", "con.txt"])
def test_windows_name_aliases_are_ambiguous(local, tmp_path, name):
    with pytest.raises(rc.RunnerConfigError) as caught:
        rc.canonical_path(local / name)
    assert caught.value.code == "path_ambiguous"


def test_a_dangling_link_fails_closed(local, tmp_path):
    target = tmp_path / "gone"
    target.mkdir()
    link = junction(tmp_path / "dangling", target) if WINDOWS else symlink(tmp_path / "dangling", target)
    target.rmdir()
    with pytest.raises(rc.RunnerConfigError) as caught:
        rc.canonical_path(link / "x.sqlite")
    assert caught.value.code == "path_ambiguous"
    assert rc._overlaps(link / "x", local / "output") is True  # an unresolvable path counts as an overlap


def test_an_unresolvable_input_fails_closed(monkeypatch, tmp_path):
    def broken(path, strict=False):
        raise OSError("cannot resolve")
    monkeypatch.setattr(os.path, "realpath", broken)
    with pytest.raises(rc.RunnerConfigError) as caught:
        rc.canonical_path(tmp_path)
    assert caught.value.code == "path_ambiguous"
    for bad in ("", None, 5):
        with pytest.raises(rc.RunnerConfigError):
            rc.canonical_path(bad)


# --- storage isolation --------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("name,code,paths", [
    ("ledger in project", "path_inside_project", lambda l: {"ledger_db": str(rc.PROJECT_ROOT / "l.sqlite")}),
    ("history in project", ("path_inside_project", "path_in_cloud_sync"), lambda l: {"history_db": str(rc.PROJECT_ROOT / "h.sqlite")}),
    ("work in project", "path_inside_project", lambda l: {"work_root": str(rc.PROJECT_ROOT / "w")}),
    ("output in project", ("path_inside_project", "output_root_invalid"), lambda l: {"output_root": str(rc.PROJECT_ROOT / "o")}),
    ("ledger in output", "ledger_overlaps_output", lambda l: {"ledger_db": str(l / "output" / "l.sqlite")}),
    ("history in output", ("history_overlaps_output", "output_root_invalid"), lambda l: {"history_db": str(l / "output" / "h.sqlite")}),
    ("ledger is history", "ledger_overlaps_history", lambda l: {"ledger_db": str(l / "history" / "h.sqlite")}),
    ("work in output", "work_root_overlaps", lambda l: {"work_root": str(l / "output" / "work")}),
    ("work holds ledger", "work_root_overlaps", lambda l: {"work_root": str(l / "ledger")}),
    ("work holds history", "work_root_overlaps", lambda l: {"work_root": str(l / "history")}),
    ("output holds work", ("work_root_overlaps", "output_root_invalid"), lambda l: {"output_root": str(l)}),
])
def test_production_storage_must_not_overlap_itself_or_the_project(local, tmp_path, name, code, paths):
    refused(local, tmp_path, code, **paths(local))


def test_onedrive_folders_are_still_refused_directly_and_through_an_alias(local, tmp_path):
    synced = tmp_path / "OneDrive - Test" / "data"
    synced.mkdir(parents=True)
    refused(local, tmp_path, "path_in_cloud_sync", ledger_db=str(synced / "l.sqlite"))
    refused(local, tmp_path, "path_in_cloud_sync", history_db=str(synced / "h.sqlite"))
    refused(local, tmp_path, "path_in_cloud_sync", work_root=str(synced / "work"))
    if WINDOWS:
        alias = junction(tmp_path / "plain-name", synced)
        refused(local, tmp_path, "path_in_cloud_sync", ledger_db=str(alias / "l.sqlite"))


def test_configured_paths_are_stored_as_written_not_rewritten(local, tmp_path):
    result = settings(local, tmp_path)
    assert result.ledger_db_path == Path(os.path.abspath(local / "ledger" / "l.sqlite"))
    assert result.work_root == Path(os.path.abspath(local / "work"))


# --- network storage ----------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("unc", ["\\\\server\\share\\l.sqlite", "//server/share/l.sqlite",
                                 "\\\\?\\UNC\\server\\share\\l.sqlite", "\\\\server\\c$\\x\\l.sqlite"])
def test_unc_database_paths_are_refused_without_touching_the_network(local, tmp_path, unc, monkeypatch):
    touched = []
    real_stat = os.stat
    monkeypatch.setattr(os, "stat", lambda p, *a, **k: touched.append(p) or real_stat(p, *a, **k))
    refused(local, tmp_path, "path_on_network", ledger_db=unc)
    assert not [p for p in touched if "server" in str(p)]
    with pytest.raises(rc.RunnerConfigError) as caught:
        settings(local, tmp_path, history_db=unc)
    assert (caught.value.code, caught.value.field) == ("path_on_network", "paths.history_db")


@pytest.mark.parametrize("field", ["ledger_db", "history_db"])
def test_a_mapped_network_drive_is_refused(local, tmp_path, monkeypatch, field):
    calls = []
    # the history database is judged first (two probes: as written, then canonical); for `ledger_db` only later probes fail
    monkeypatch.setattr(rc, "drive_type_of", lambda root: calls.append(root) or (
        "remote" if field == "history_db" or len(calls) > 2 else "fixed"))
    error = refused(local, tmp_path, "path_on_network", **{field: str(local / "ledger" / "x.sqlite")})
    assert error.field == f"paths.{field}"


def test_a_local_fixed_drive_is_accepted(local, tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(rc, "drive_type_of", lambda root: seen.append(root) or "fixed")
    settings(local, tmp_path)
    assert seen and all(r.endswith(":\\") and len(r) == 3 for r in seen)


@pytest.mark.parametrize("kind", ["unknown", "removable", "cdrom", "ramdisk", "no_root", "surprise", None])
def test_an_unrecognised_or_unsuitable_drive_type_fails_closed(local, tmp_path, monkeypatch, kind):
    monkeypatch.setattr(rc, "drive_type_of", lambda root: kind)
    refused(local, tmp_path, "drive_type_unknown")


def test_a_failing_drive_probe_fails_closed(local, tmp_path, monkeypatch):
    def boom(root):
        raise OSError("probe failed")
    monkeypatch.setattr(rc, "drive_type_of", boom)
    refused(local, tmp_path, "drive_type_unknown")


def test_a_link_that_leads_to_a_network_drive_is_refused(local, tmp_path, monkeypatch):
    monkeypatch.setattr(rc, "canonical_path", lambda path, field_name="": Path("Z:\\share\\l.sqlite"))
    monkeypatch.setattr(rc, "drive_type_of", lambda root: "remote" if root.startswith("Z") else "fixed")
    with pytest.raises(rc.RunnerConfigError) as caught:
        rc.require_local_database(local / "ledger" / "l.sqlite", "paths.ledger_db")
    assert caught.value.code == "path_on_network"


def test_a_path_without_a_drive_letter_cannot_be_judged_and_is_refused(monkeypatch):
    monkeypatch.setattr(rc, "canonical_path", lambda path, field_name="": Path("/just/a/path"))
    with pytest.raises(rc.RunnerConfigError) as caught:
        rc.require_local_database("C:\\x\\l.sqlite", "paths.ledger_db") if WINDOWS else rc.require_local_database(
            "/just/a/path", "paths.ledger_db")
    assert caught.value.code == "drive_type_unknown"


def test_only_database_locations_are_judged_by_drive_type(local, tmp_path, monkeypatch):
    judged = []
    real = rc.require_local_database
    monkeypatch.setattr(rc, "require_local_database", lambda path, field="": judged.append(field) or real(path, field))
    settings(local, tmp_path)
    assert sorted(judged) == ["paths.history_db", "paths.ledger_db"]  # report output and work folders are not


@requires_windows
def test_the_real_drive_probe_reports_the_temp_drive_as_fixed_and_garbage_as_unknown():
    drive = os.path.splitdrive(os.path.abspath(os.environ.get("TEMP", "C:\\")))[0] + "\\"
    assert rc.windows_drive_type(drive) in ("fixed", "removable", "remote", "ramdisk")
    assert rc.windows_drive_type("?:\\nonsense") in ("unknown", "no_root")


def test_the_ledger_itself_refuses_a_network_or_unidentified_location(tmp_path, monkeypatch):
    clock = __import__("datetime").datetime.now(__import__("datetime").timezone.utc)
    with pytest.raises(lg.LedgerError) as caught:
        lg.Ledger(Path("\\\\server\\share\\l.sqlite") if WINDOWS else Path("/x"), clock=lambda: clock)
    assert caught.value.code in ("ledger_path_not_local", "ledger_path_not_absolute")
    monkeypatch.setattr(rc, "drive_type_of", lambda root: "remote")
    with pytest.raises(lg.LedgerError) as caught:
        lg.Ledger(tmp_path / "l.sqlite", clock=lambda: clock)
    assert caught.value.code == "ledger_path_not_local" and not (tmp_path / "l.sqlite").exists()


# --- dry-run scratch isolation ------------------------------------------------------------------------------------------------

@pytest.fixture
def prod(local, tmp_path):
    return settings(local, tmp_path)


def test_a_separate_local_scratch_folder_is_accepted(prod, tmp_path):
    scratch = tmp_path / "scratch"
    assert pipeline._checked_scratch(scratch, prod) == scratch and scratch.is_dir()


def test_scratch_inside_production_storage_or_the_project_is_refused(prod, local, tmp_path):
    for bad in (local / "output" / "s", local / "work" / "s", local / "ledger" / "s", local / "history" / "s",
                rc.PROJECT_ROOT / "s", rc.PROJECT_ROOT, local / "output"):
        assert pipeline._checked_scratch(bad, prod) is None, bad
    assert not (rc.PROJECT_ROOT / "s").exists()


@requires_windows
def test_scratch_case_variants_are_still_inside_production_storage(prod, local):
    assert pipeline._checked_scratch(Path(str(local / "OUTPUT" / "S").upper()), prod) is None


@pytest.mark.parametrize("make", [junction, symlink])
def test_scratch_reached_through_a_link_is_judged_by_where_it_really_is(prod, local, tmp_path, make):
    if make is junction and not WINDOWS:
        pytest.skip("junctions are Windows only")
    link = make(tmp_path / "scratch-alias", local / "work")
    assert pipeline._checked_scratch(link, prod) is None
    assert pipeline._checked_scratch(link / "new" / "deeper", prod) is None  # nonexistent under an aliased parent
    assert not (local / "work" / "new").exists()


@requires_windows
def test_scratch_reached_through_a_junction_to_the_project_is_refused(prod, tmp_path):
    link = junction(tmp_path / "to-project", rc.PROJECT_ROOT)
    assert pipeline._checked_scratch(link / "scratch", prod) is None


def test_scratch_that_is_relative_ambiguous_or_missing_is_refused(prod, tmp_path):
    for bad in (None, Path("relative"), tmp_path / "a" / ".." / "b"):
        assert pipeline._checked_scratch(bad, prod) is None


def test_a_dry_run_with_an_aliased_scratch_is_rejected_before_anything_is_written(world, tmp_path):
    from test_pipeline import go, tree  # noqa: F401
    w = world()
    link = junction(tmp_path / "alias", w.settings.work_root) if WINDOWS else symlink(tmp_path / "alias", w.settings.work_root)
    response = pipeline.run_client("example-co", pipeline_period(), services=w.services, ledger=w.ledger, mode="dry_run",
                                   scratch_dir=link / "scratch")
    assert (response.outcome, response.error["code"]) == ("rejected", "scratch_dir_invalid")
    assert not (w.settings.work_root / "scratch").exists()


def pipeline_period():
    from test_pipeline import PERIOD_ID
    return PERIOD_ID


from test_pipeline import world  # noqa: E402,F401  (fixture)
