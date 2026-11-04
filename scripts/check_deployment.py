"""Read-only deployment preflight for the SEO report runner (R8.4.6).

Checks, without creating, changing or deleting anything and without starting a run, a browser, or any network call:

  * the Python runtime and the installed packages against requirements.txt (via scripts/dependency_report.py);
  * the runner settings file (paths valid, outside the project and OneDrive, database drives local);
  * the logo and font files named by the settings: present, regular files, readable, and their SHA-256 fingerprints;
  * the storage folders: whether they exist (they are created by the first run, not by this script);
  * the Playwright Chromium folder: whether a Chromium build is present under %LOCALAPPDATA%\\ms-playwright.

It deliberately does NOT look at the OAuth token or client-secret files: their presence is not checked, and they are never
opened (see docs/r8/credential_relocation_procedure.md for the verification that is done by a person).

    python scripts/check_deployment.py --settings C:\\path\\to\\runner.yaml

Exit code 0 when every check is "ready", 1 otherwise. Statuses: ready | needs_configuration | needs_implementation | unverified.
"""
import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from seo_report import asset_fingerprints as af  # noqa: E402
from seo_report import runner_config as rc  # noqa: E402

REQUIRED_FONT_FILES = ("DejaVuSans.ttf", "DejaVuSans-Bold.ttf")  # the DejaVu Sans regular and bold files the report uses


def check_assets(logo_path, fonts) -> list[tuple[str, str, str]]:
    """[(item, status, code)] for the logo and every configured font. Codes come from `asset_fingerprints` and are fixed."""
    findings = []
    if not fonts:
        findings.append(("fonts", "needs_configuration", "fonts_not_configured"))
    for role, path in [("logo", logo_path)] + [(f"font:{f.label}", f.path) for f in fonts]:
        try:
            digest = af.sha256_of_file(path, role=role)
            findings.append((role, "ready", "sha256:" + digest))
        except af.AssetFingerprintError as error:
            findings.append((role, "needs_configuration", getattr(error, "code", "asset_unusable")))
    present = {Path(os.fspath(f.path)).name for f in fonts}
    for name in REQUIRED_FONT_FILES:
        if fonts and name not in present:
            findings.append((f"font-file:{name}", "needs_configuration", "required_font_file_not_configured"))
    return findings


def check_storage(settings) -> list[tuple[str, str, str]]:
    findings = []
    for item, folder in (("output_root", settings.output_root), ("work_root", settings.work_root),
                         ("history_db_folder", settings.history_db_path.parent),
                         ("ledger_db_folder", settings.ledger_db_path.parent)):
        findings.append((item, "ready" if Path(folder).is_dir() else "needs_configuration",
                         "exists" if Path(folder).is_dir() else "will_be_created_by_the_first_run"))
    return findings


def check_chromium(environ=None) -> tuple[str, str, str]:
    environ = os.environ if environ is None else environ
    base = environ.get("PLAYWRIGHT_BROWSERS_PATH") or str(Path(environ.get("LOCALAPPDATA", "")) / "ms-playwright")
    found = sorted(p.name for p in Path(base).glob("chromium-*")) if Path(base).is_dir() else []
    return ("chromium", "ready" if found else "needs_configuration", found[-1] if found else "run: python -m playwright install chromium")


def check_dependencies() -> list[tuple[str, str, str]]:
    import dependency_report
    report = dependency_report.build_report()
    problems = len(report["mismatched"]) + len(report["imported_not_pinned"]) + len(report["missing"])
    return [("dependencies", "ready" if not problems else "needs_configuration",
             f"{len(report['pins'])} pinned, {len(report['runtime'])} in the closure" if not problems else f"{problems} problem(s)")]


def run(settings_path=None, environ=None) -> list[tuple[str, str, str]]:
    findings = [("python", "ready" if sys.version_info[:2] == (3, 14) else "unverified", sys.version.split()[0])]
    findings += check_dependencies()
    findings.append(check_chromium(environ))
    if settings_path is None:
        findings.append(("settings", "needs_configuration", "no settings file given (--settings)"))
        return findings
    try:
        settings = rc.load_settings(settings_path) if hasattr(rc, "load_settings") else None
    except (rc.RunnerConfigError, OSError, ValueError) as error:
        return findings + [("settings", "needs_configuration", getattr(error, "code", "settings_unreadable"))]
    findings.append(("settings", "ready", "valid"))
    findings += check_assets(settings.logo_path, settings.fonts)
    findings += check_storage(settings)
    return findings


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--settings", help="path of the runner settings YAML file")
    args = parser.parse_args(argv)
    findings = run(args.settings)
    for item, status, detail in findings:
        print(f"{status:22} {item:28} {detail}")
    return 0 if all(status == "ready" for _, status, _ in findings) else 1


if __name__ == "__main__":
    raise SystemExit(main())
