"""R8.7b: packaging metadata and launch configuration.

These tests read `pyproject.toml`, `requirements*.txt`, `README.md` and the source tree only. **Nothing is installed or built and no environment is
created**, so they prove the metadata is consistent with the project, not that `pip install -e .` works on a clean machine (that remains unverified).
"""
import importlib
import re
import sys
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from seo_report import cli
from seo_report import runner_config as rc

PYPROJECT = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
PROJECT = PYPROJECT["project"]


def pins(path: Path) -> dict:
    """{normalized name: version} from the `name==version` lines of a requirements file (comments and `-r` lines ignored)."""
    found = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if not line or line.startswith("-"):
            continue
        name, _, version = line.partition("==")
        assert version, f"not an exact pin: {line!r}"
        found[normalize(name)] = version.strip()
    return found


def normalize(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name.strip()).lower()


def declared(specs) -> dict:
    result = {}
    for spec in specs:
        name, _, version = spec.partition("==")
        assert version, f"not an exact pin: {spec!r}"
        result[normalize(name)] = version
    return result


def test_the_project_metadata_is_present_and_private():
    assert PROJECT["name"] == "jasper-seo-report" and PROJECT["readme"] == "README.md" and (ROOT / "README.md").is_file()
    assert PYPROJECT["build-system"] == {"requires": ["setuptools>=68"], "build-backend": "setuptools.build_meta"}
    assert "Private :: Do Not Upload" in PROJECT["classifiers"]
    assert PROJECT["requires-python"] == ">=3.14"


def test_the_package_version_equals_the_cli_version():
    assert PROJECT["version"] == cli.APP_VERSION


def test_runtime_dependencies_are_exactly_the_existing_requirements():
    assert declared(PROJECT["dependencies"]) == pins(ROOT / "requirements.txt")
    assert len(PROJECT["dependencies"]) == 13


def test_the_dev_extra_is_exactly_the_existing_dev_requirements():
    assert declared(PROJECT["optional-dependencies"]["dev"]) == pins(ROOT / "requirements-dev.txt")


def test_every_declared_pin_matches_the_runtime_lock_file():
    lock = pins(ROOT / "requirements-lock.txt")
    for name, version in declared(PROJECT["dependencies"]).items():
        assert lock.get(name) == version, name


def test_no_dependency_is_unpinned_or_added_beyond_the_requirements():
    for spec in PROJECT["dependencies"] + PROJECT["optional-dependencies"]["dev"]:
        assert re.fullmatch(r"[A-Za-z0-9_.-]+==[0-9][A-Za-z0-9.]*", spec), spec
    assert set(PROJECT["optional-dependencies"]) == {"dev"} and "dynamic" not in PROJECT


def test_the_console_script_points_at_the_existing_cli_main():
    assert PROJECT["scripts"] == {"seo-report": "seo_report.cli:main"}
    module, _, attribute = PROJECT["scripts"]["seo-report"].partition(":")
    target = getattr(importlib.import_module(module), attribute)
    assert target is cli.main and callable(target)


def test_the_entry_point_runs_with_no_arguments_object_like_a_console_script(capsys):
    """A console script calls `sys.exit(main())`: with `argv=None` main reads `sys.argv[1:]` and returns an int."""
    old = sys.argv
    sys.argv = ["seo-report", "--version"]
    try:
        assert cli.main() == 0
    finally:
        sys.argv = old
    assert f"seo-report {cli.APP_VERSION}" in capsys.readouterr().out


def test_python_dash_m_still_works_through_the_main_module():
    source = (ROOT / "src" / "seo_report" / "__main__.py").read_text(encoding="utf-8")
    assert "from seo_report.cli import main" in source and "sys.exit(main())" in source


def test_the_src_layout_is_configured_and_all_subpackages_are_included():
    find = PYPROJECT["tool"]["setuptools"]["packages"]["find"]
    assert find["where"] == ["src"] and not (set(find) - {"where"})  # no include/exclude/namespaces switch that could drop a subpackage
    assert (ROOT / "src" / "seo_report" / "__init__.py").is_file()
    folders = {p.parent.relative_to(ROOT / "src").as_posix() for p in (ROOT / "src").rglob("*.py")}
    assert {"seo_report", "seo_report/insights", "seo_report/snapshots", "seo_report/sources"} <= folders
    assert not any(p.name == "__init__.py" for p in (ROOT / "src" / "seo_report" / "insights").iterdir())  # namespace subpackages: the reason `find` is relied on


def test_the_checkout_layout_the_code_depends_on_exists():
    """An editable install resolves `parents[2]` to the checkout; a site-packages install would not. Both facts are asserted here."""
    assert rc.PROJECT_ROOT == ROOT and (ROOT / "pyproject.toml").is_file()
    for relative in ("templates/report", "config/clients.yaml", "config/jasper.yaml", "config/runner.yaml.example", "assets/jasper-logo.png",
                     "requirements.txt"):
        assert (ROOT / relative).exists(), relative


def test_the_readme_documents_installation_launch_safety_and_the_limits():
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    for needle in ("py -3.14 -m venv", "pip install -r requirements-lock.txt", "pip install -e . --no-deps", "playwright install chromium",
                   "PYTHONPATH", "seo-report --version", "outside OneDrive", "doctor", "system-info", "setup auth", "setup device", "seo web",
                   "production gate", "live_google_not_verified", "credentials_not_relocated", "DejaVu", "jasper-logo.png",
                   "runner.yaml.example", "dry-run", "team_setup.md", "cheat_sheet.md", "install_device.ps1"):
        assert needle in text, needle
    assert "`-e` (editable)" in text
    # the status is stated honestly: in use, with delivery and the untried parts called out
    lowered = text.lower()
    assert "status: in use" in lowered and "delivery is not implemented" in lowered and "has **not** been verified" in lowered
    assert "not production-ready" not in lowered


def test_the_readme_contains_no_credential_like_text():
    text = (ROOT / "README.md").read_text(encoding="utf-8") + (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    for pattern in (r"ya29\.", r"1//[0-9A-Za-z_-]{20,}", r"GOCSPX-", r"-----BEGIN", r"\"refresh_token\"", r"\"client_secret\"\s*:", r"AIza[0-9A-Za-z_-]{20,}"):
        assert not re.search(pattern, text), pattern


def test_the_credential_files_remain_ignored_by_git():
    ignored = (ROOT / ".gitignore").read_text(encoding="utf-8")
    for entry in ("credentials/", "token*.json", "client_secret*.json", ".venv/", "output/"):
        assert entry in ignored, entry
