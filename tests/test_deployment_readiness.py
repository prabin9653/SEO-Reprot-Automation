"""R8.4.6: dependency reproducibility, asset provisioning and the read-only deployment preflight (no installation, no network,
no credential file, no production run; temporary folders only)."""
import dataclasses
import hashlib
import importlib
import sys
from importlib import metadata as md
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))

import check_deployment as cd
import dependency_report as dr
from seo_report import asset_fingerprints as af
from seo_report import runner_config as rc
from test_pdf_renderer import FONT_FILES, LOGO
from test_pipeline import RUN_KEY, go, make_settings, world  # noqa: F401


# --- dependencies ---------------------------------------------------------------------------------------------------------

def test_every_pin_in_requirements_matches_the_tested_environment():
    pins = dr.read_pins(ROOT / "requirements.txt")
    assert pins and all(md.version(name) == version for name, version in pins.items())


def test_every_third_party_module_imported_by_the_source_is_covered_by_a_pin():
    report = dr.build_report()
    assert report["imported_not_pinned"] == [] and report["missing"] == [] and report["mismatched"] == {}
    owners = dr.owners_of(dr.third_party_imports(ROOT / "src"))
    pins = set(dr.read_pins(ROOT / "requirements.txt"))
    assert all(o & pins for o in owners.values()), {m: o for m, o in owners.items() if not o & pins}


def test_the_runtime_lock_is_the_exact_installed_closure():
    lock = dr.read_pins(ROOT / "requirements-lock.txt")
    closure = dr.closure(dr.read_pins(ROOT / "requirements.txt"))
    assert lock == {name: version for name, version in closure.items()}
    assert all(md.version(name) == version for name, version in lock.items())
    assert set(dr.read_pins(ROOT / "requirements.txt")) <= set(lock)


def test_the_dev_lock_adds_only_test_tools_to_the_runtime_lock():
    runtime, dev = dr.read_pins(ROOT / "requirements-lock.txt"), dr.read_pins(ROOT / "requirements-dev-lock.txt")
    assert runtime.items() <= dev.items()
    assert {"pytest", "matplotlib"} <= set(dev) and not ({"pytest", "matplotlib"} & set(runtime))
    assert dr.read_pins(ROOT / "requirements-dev.txt") == {"pytest": md.version("pytest"), "matplotlib": md.version("matplotlib")}
    assert "-r requirements.txt" in (ROOT / "requirements-dev.txt").read_text(encoding="utf-8")


def test_lock_files_are_exact_complete_and_free_of_ranges_and_urls():
    for name in ("requirements-lock.txt", "requirements-dev-lock.txt"):
        entries = [line for line in (ROOT / name).read_text(encoding="utf-8").splitlines() if line and not line.startswith("#")]
        assert entries and all(line.count("==") == 1 and not any(c in line for c in "<>~! @/:") for line in entries), name


def test_the_lock_writer_refuses_to_run_when_the_environment_is_inconsistent(monkeypatch, tmp_path):
    monkeypatch.setattr(dr, "ROOT", tmp_path)
    (tmp_path / "requirements.txt").write_text("pytest==0.0.1\n", encoding="utf-8")
    (tmp_path / "src").mkdir()
    assert dr.main(["--write-lock"]) == 1
    assert not (tmp_path / "requirements-lock.txt").exists()


def test_the_report_script_never_installs_or_uses_the_network():
    source = (ROOT / "scripts" / "dependency_report.py").read_text(encoding="utf-8")
    code = source.split('"""', 2)[2]
    for word in ("subprocess", "pip.main", "urllib", "requests", "socket", "os.system", "ensurepip", "import pip"):
        assert word not in code, word


def test_the_runtime_does_not_need_the_test_tools():
    runtime = dr.read_pins(ROOT / "requirements-lock.txt")
    for path in (ROOT / "src").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert "import pytest" not in text and "import matplotlib" not in text, path.name
    assert "matplotlib" not in runtime and "pytest" not in runtime


def test_the_python_runtime_the_dependencies_were_verified_on():
    assert sys.version_info[:2] == (3, 14)  # requirements.txt was verified on 3.14.7; other versions are unverified


# --- assets ---------------------------------------------------------------------------------------------------------------

def fonts(tmp_path, *names):
    return tuple(rc.FontSetting(Path(n).stem, tmp_path / n, "700" if "Bold" in n else "400") for n in names)


def test_the_bundled_logo_and_the_dejavu_files_are_fingerprinted_by_sha256_of_their_bytes():
    assert af.sha256_of_file(LOGO, role="logo") == hashlib.sha256(LOGO.read_bytes()).hexdigest()
    for label, path in FONT_FILES.items():
        assert af.sha256_of_file(path, role=label) == hashlib.sha256(Path(path).read_bytes()).hexdigest()
    fingerprints = af.measure_fingerprints(template_version="r6.0", logo_path=LOGO, font_paths=FONT_FILES)
    assert {f.name for f in fingerprints.fonts} == {"DejaVuSans", "DejaVuSans-Bold"}


def test_the_required_dejavu_files_are_the_regular_and_bold_faces():
    assert cd.REQUIRED_FONT_FILES == ("DejaVuSans.ttf", "DejaVuSans-Bold.ttf")
    assert {Path(p).name for p in FONT_FILES.values()} == set(cd.REQUIRED_FONT_FILES)


def test_the_preflight_accepts_the_real_asset_files():
    configured = tuple(rc.FontSetting(label, path, "700" if "Bold" in label else "400") for label, path in FONT_FILES.items())
    findings = cd.check_assets(LOGO, configured)
    assert findings and all(status == "ready" for _, status, _ in findings)
    assert {item for item, _, _ in findings} == {"logo", "font:DejaVuSans", "font:DejaVuSans-Bold"}


@pytest.mark.parametrize("break_it,item,code", [
    ("logo_missing", "logo", "file_not_found"), ("font_missing", "font:DejaVuSans", "file_not_found"),
    ("font_is_folder", "font:DejaVuSans", "not_a_regular_file")])
def test_the_preflight_reports_missing_or_unusable_assets_with_a_clear_code(tmp_path, break_it, item, code):
    logo = tmp_path / "logo.png"
    logo.write_bytes(LOGO.read_bytes())
    regular, bold = tmp_path / "DejaVuSans.ttf", tmp_path / "DejaVuSans-Bold.ttf"
    for target, source in ((regular, FONT_FILES["DejaVuSans"]), (bold, FONT_FILES["DejaVuSans-Bold"])):
        target.write_bytes(Path(source).read_bytes())
    if break_it == "logo_missing":
        logo.unlink()
    elif break_it == "font_missing":
        regular.unlink()
    else:
        regular.unlink()
        regular.mkdir()
    findings = cd.check_assets(logo, fonts(tmp_path, "DejaVuSans.ttf", "DejaVuSans-Bold.ttf"))
    assert (item, "needs_configuration", code) in findings


def test_the_preflight_flags_missing_font_configuration_and_missing_required_faces(tmp_path):
    assert ("fonts", "needs_configuration", "fonts_not_configured") in cd.check_assets(LOGO, ())
    only_regular = tmp_path / "DejaVuSans.ttf"
    only_regular.write_bytes(Path(FONT_FILES["DejaVuSans"]).read_bytes())
    findings = cd.check_assets(LOGO, fonts(tmp_path, "DejaVuSans.ttf"))
    assert ("font-file:DejaVuSans-Bold.ttf", "needs_configuration", "required_font_file_not_configured") in findings


def test_today_a_run_without_fonts_stops_at_the_render_stage_with_a_configuration_error(world):
    w = world()
    settings = dataclasses.replace(w.settings, fonts=())
    response = go(w, services=dataclasses.replace(w.services, settings=settings, renderer=None))
    assert (response.state, response.error["category"], response.error["code"]) == ("failed", "config_invalid", "fonts_not_configured")
    assert response.error["stage"] == "rendering"  # found only after collection: the preflight script finds it earlier


def test_today_a_missing_font_file_surfaces_as_a_render_input_error_at_the_render_stage(world):
    w = world()
    broken = tuple(dataclasses.replace(f, path=f.path.with_name("missing.ttf")) for f in w.settings.fonts)
    settings = dataclasses.replace(w.settings, fonts=broken)
    response = go(w, services=dataclasses.replace(w.services, settings=settings))
    assert (response.state, response.error["category"], response.error["code"]) == ("failed", "render_failed", "render_input_invalid")


def test_the_default_logo_is_the_project_asset_and_exists():
    settings = make_settings(Path(__import__("tempfile").mkdtemp()))
    assert settings.logo_path == LOGO and LOGO.is_file()


# --- the preflight itself -----------------------------------------------------------------------------------------------------------

def test_the_preflight_changes_nothing_and_never_names_a_credential_file(tmp_path, monkeypatch):
    settings = make_settings(tmp_path)
    before = sorted(p for p in tmp_path.rglob("*"))
    findings = cd.check_storage(settings)
    assert {item for item, _, _ in findings} == {"output_root", "work_root", "history_db_folder", "ledger_db_folder"}
    assert sorted(p for p in tmp_path.rglob("*")) == before  # nothing was created
    source = (ROOT / "scripts" / "check_deployment.py").read_text(encoding="utf-8")
    code = source.split('"""', 2)[2]
    assert "token.json" not in code and "client_secret" not in code and "TOKEN_PATH" not in code


def test_chromium_detection_reads_only_the_playwright_folder(tmp_path):
    assert cd.check_chromium({"LOCALAPPDATA": str(tmp_path)})[1] == "needs_configuration"
    (tmp_path / "ms-playwright" / "chromium-1234").mkdir(parents=True)
    assert cd.check_chromium({"LOCALAPPDATA": str(tmp_path)}) == ("chromium", "ready", "chromium-1234")
    assert cd.check_chromium({"PLAYWRIGHT_BROWSERS_PATH": str(tmp_path / "ms-playwright")})[1] == "ready"


def test_the_preflight_reports_an_invalid_settings_file_without_raising(tmp_path):
    bad = tmp_path / "runner.yaml"
    bad.write_text("paths:\n  ledger_db: relative/ledger.sqlite\n", encoding="utf-8")
    findings = cd.run(str(bad))
    assert ("settings", "needs_configuration", "path_not_absolute") in findings
    assert cd.run(None)[-1][0] == "settings"


# --- documentation and example-configuration consistency ----------------------------------------------------------------------------

DOCS = ROOT / "docs" / "r8"
DESIGN_DOCS = ("production_architecture", "execution_contract", "ledger_schema", "google_sheets_contract", "make_scenarios",
               "security_and_authentication", "delivery_architecture", "implementation_roadmap")


@pytest.mark.parametrize("name", DESIGN_DOCS)
def test_every_design_document_carries_the_as_built_status_block(name):
    text = (DOCS / f"{name}.md").read_text(encoding="utf-8")
    assert "As-built status (updated R8.4.6)" in text.split("\n", 6)[1] + "\n".join(text.split("\n")[:6])
    assert "as_built_status.md" in text


def test_the_new_documents_exist_and_state_what_was_not_done():
    for name in ("as_built_status", "dependency_and_asset_readiness", "credential_relocation_procedure", "deployment_readiness",
                 "final_readiness_assessment"):
        assert (DOCS / f"{name}.md").stat().st_size > 2000, name
    procedure = (DOCS / "credential_relocation_procedure.md").read_text(encoding="utf-8")
    assert "NOT performed" in procedure and "Proposed" in procedure
    checklist = (DOCS / "deployment_readiness.md").read_text(encoding="utf-8")
    for label in ("Ready", "Needs configuration", "Needs implementation", "Unverified", "Blocked"):
        assert f"**{label}**" in checklist
    for item in ("Python runtime", "Chromium", "Font", "Logo", "Storage", "Database", "Credential relocation", "Scheduled Task",
                 "Logging", "Backups", "Recovery", "Disk-space", "Monitoring", "Client configuration", "Reporting-period"):
        assert item in checklist, item


def test_the_documented_package_counts_match_the_lock_files():
    runtime = len(dr.read_pins(ROOT / "requirements-lock.txt"))
    dev = len(dr.read_pins(ROOT / "requirements-dev-lock.txt"))
    assert (runtime, len(dr.read_pins(ROOT / "requirements.txt")), dev) == (33, 13, 49)
    text = (DOCS / "as_built_status.md").read_text(encoding="utf-8") + (DOCS / "dependency_and_asset_readiness.md").read_text(encoding="utf-8")
    assert "13 direct pins" in text and "(33)" in text and "(49)" in text


def test_the_documented_font_and_logo_hashes_match_the_files_used_by_the_tests():
    text = (DOCS / "dependency_and_asset_readiness.md").read_text(encoding="utf-8")
    for path in (LOGO, FONT_FILES["DejaVuSans"], FONT_FILES["DejaVuSans-Bold"]):
        assert hashlib.sha256(Path(path).read_bytes()).hexdigest() in text, Path(path).name


def test_the_example_settings_parse_with_the_documented_keys(tmp_path):
    import yaml
    data = yaml.safe_load((ROOT / "config" / "runner.yaml.example").read_text(encoding="utf-8"))
    assert data["execution"]["heartbeat_interval_seconds"] == 60 and data["execution"]["stage_timeouts"]["rendering"] == 300
    data.pop("workbooks")  # placeholders are refused by design
    local = tmp_path / "local"
    data["paths"] = {"output_root": str(local / "o"), "history_db": str(local / "h" / "h.sqlite"),
                     "ledger_db": str(local / "l" / "l.sqlite"), "work_root": str(local / "w")}
    data["render"] = {"logo": str(LOGO), "fonts": [{"label": "DejaVuSans", "path": str(FONT_FILES["DejaVuSans"]), "weight": "400"},
                                                    {"label": "DejaVuSans-Bold", "path": str(FONT_FILES["DejaVuSans-Bold"]),
                                                     "weight": "700"}]}
    settings = rc.parse_settings(data, environ={"LOCALAPPDATA": str(local)})
    assert settings.heartbeat_interval_seconds == 60.0 and settings.stage_timeouts.rendering == 300.0


def test_no_document_or_script_contains_a_real_looking_secret():
    pattern = __import__("re").compile(r"ya29\.[\w-]{10,}|1//[\w-]{20,}|GOCSPX-[\w-]{10,}|AIza[0-9A-Za-z_-]{35}")
    for path in list(DOCS.glob("*.md")) + list((ROOT / "scripts").glob("*.py")) + [ROOT / "config" / "runner.yaml.example"]:
        assert not pattern.search(path.read_text(encoding="utf-8")), path.name