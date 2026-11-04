"""Local, read-only deployment preflight (R8.6c): the engine behind `doctor` and the readiness part of `system-info`.

It looks at metadata and local configuration only. It never contacts Google, never starts authentication, never opens a credential or token file (file
states come from `os.stat`), never refreshes anything, never writes a ledger, report or configuration, never installs anything, and creates nothing in
production storage. The single active probe is the optional browser launch, which runs in a separate process with its own temporary folder that this
module creates and removes; it makes no network request.

Findings
  `Finding(section, component, status, code, message, action)` with
    status  ready | warning | blocked | unverified
              ready       the component is present and passes its local checks
              warning     a non-blocking issue (visible, never changes the exit code by itself)
              blocked     a required component is missing or invalid
              unverified  needs a live environment or an action this check is not allowed to perform (Google sign-in, a clean machine, ...)
    code    a fixed lower-case identifier (`[a-z0-9_]+`); no value, path or secret is ever part of a code or message
    message a safe explanation; action: the suggested next step (may be empty)

Overall readiness (`overall_readiness`) is derived from the findings only: `blocked` if any is blocked, else `warning` if any is a warning, else `ready`.
`unverified` findings do not lower it but are always listed, and **`production_ready` is always false in R8.6c**: the outstanding requirements (live Google
verification, credential relocation, report-execution commands, Make.com, Drive, e-mail) are reported in `outstanding`.

Exit-code priority (first match wins; documented in docs/r8/r86_cli_reference.md)
    1  an internal error while checking           -> INTERNAL_ERROR (1)
    2  a blocked configuration finding            -> CONFIG_INVALID (3)    (no/invalid settings file, client registry, sender config, workbook id)
    3  any other blocked finding                  -> PREFLIGHT_FAILED (4)
    4  the only blocked finding is missing sign-in -> AUTH_REQUIRED (5)    (`auth_token_missing`, `auth_credentials_*`)
    5  otherwise (ready, warnings, unverified)    -> OK (0)

JSON report: schema `jasper.preflight-report` version 1 (see `build_report`).
"""

import dataclasses
import datetime as dt
import importlib.metadata as metadata
import importlib.util
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from seo_report import asset_fingerprints as af
from seo_report import runner_config as rc
from seo_report.cli_exit import Exit

SCHEMA = "jasper.preflight-report"
VERSION = 1
STATUSES = ("ready", "warning", "blocked", "unverified")
SECTIONS = ("runtime", "dependencies", "browser", "assets", "storage", "authentication", "configuration", "logging")
SECTION_TITLES = {"runtime": "Application and Python runtime", "dependencies": "Dependencies", "browser": "Playwright and Chromium",
                  "assets": "Fonts and logo", "storage": "Storage and filesystem", "authentication": "Authentication configuration",
                  "configuration": "Client configuration", "logging": "Logging"}

TESTED_PYTHON = (3, 14)  # the suite passes on 3.14.7; other versions are not claimed to work
TESTED_MACHINES = ("AMD64", "x86_64")
MIN_FREE_BYTES = 1 << 30  # 1 GiB: a documented placeholder; real per-month needs are unmeasured (docs/r8/deployment_readiness.md)
LAUNCH_TIMEOUT_SECONDS = 90

# SHA-256 of the files the report was tested with (docs/r8/dependency_and_asset_readiness.md). A different file is not an error, but it changes the
# render identity of every future report, so it is a visible warning.
REFERENCE_HASHES = {
    "logo": "e7d5a332cd6aa1df034fa6326e4b61c1f2aae3219b0bf410e0b7461ce813f320",
    "DejaVuSans": "3fdf69cabf06049ea70a00b5919340e2ce1e6d02b0cc3c4b44fb6801bd1e0d22",
    "DejaVuSans-Bold": "b184b89e3c1075f22f6b71575b6fc20d4972b3cfd3b23322ca6fd596dcaef167"}
REQUIRED_FONT_FILES = {"DejaVuSans": "DejaVuSans.ttf", "DejaVuSans-Bold": "DejaVuSans-Bold.ttf"}

# findings that mean "a person must sign in" rather than "the environment is broken"
AUTH_CODES = frozenset({"auth_token_missing", "auth_credentials_unreadable", "auth_credentials_invalid"})
CONFIG_CODES = frozenset({"config_not_given", "config_not_found", "client_registry_invalid", "sender_config_invalid",
                          "reporting_workbook_not_configured"})
OUTSTANDING = ("live_google_not_verified", "credentials_not_relocated", "make_not_connected", "drive_not_connected", "email_not_connected")


@dataclass(frozen=True)
class Finding:
    section: str
    component: str
    status: str
    code: str
    message: str
    action: str = ""

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)


@dataclass
class Inputs:
    """Everything the checks read. `probes` replaces individual environment probes in tests (name -> callable)."""

    settings: object | None
    config_code: str | None = None  # why `settings` is None (a fixed code), when it is
    config_source: str | None = None
    environ: dict = field(default_factory=dict)
    logging_status: dict | None = None  # {"active": bool, "code": str | None} as set at startup
    launch_browser: bool = True
    app_version: str = "0"
    probes: dict = field(default_factory=dict)
    clock: object | None = None  # () -> aware UTC datetime; the real clock when None

    def probe(self, name, default):
        return self.probes.get(name, default)

    def now(self) -> dt.datetime:
        return (self.clock or (lambda: dt.datetime.now(dt.timezone.utc)))()


def F(section, component, status, code, message, action=""):
    return Finding(section, component, status, code, message, action)


# --- helpers -------------------------------------------------------------------------------------------------------------------

def redact_path(path, environ=None) -> str:
    """A path for display without user-specific prefixes: `%LOCALAPPDATA%\\...`, `<project>\\...`, otherwise `<path>\\<last two parts>`."""
    environ = os.environ if environ is None else environ
    text = os.path.normpath(os.fspath(path))
    for label, root in (("%LOCALAPPDATA%", environ.get("LOCALAPPDATA")), ("<project>", str(rc.PROJECT_ROOT))):
        if root:
            base = os.path.normpath(root)
            if os.path.normcase(text).startswith(os.path.normcase(base) + os.sep) or os.path.normcase(text) == os.path.normcase(base):
                return label + text[len(base):]
    parts = [p for p in text.replace("/", "\\").split("\\") if p and not p.endswith(":")]
    return "<path>\\" + "\\".join(parts[-2:]) if parts else "<path>"


def read_pins(path) -> dict | None:
    """{name: version} from a requirements file (`name==version` lines), or None when the file is missing/unreadable."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError:
        return None
    pins = {}
    for line in text.splitlines():
        line = line.split("#")[0].strip()
        if "==" in line and not line.startswith(("-", "git+")):
            name, version = line.split("==", 1)
            pins[name.strip().lower().replace("_", "-").replace(".", "-")] = version.strip()
    return pins


def installed_version(name):
    try:
        return metadata.version(name)
    except metadata.PackageNotFoundError:
        return None


# --- runtime -------------------------------------------------------------------------------------------------------------------

def check_runtime(inp: Inputs) -> list[Finding]:
    version = inp.probe("python_version", tuple(sys.version_info[:3]))
    machine = inp.probe("machine", platform.machine())
    system, release = inp.probe("system", platform.system()), inp.probe("release", platform.release())
    out = []
    out.append(F("runtime", "application", "ready", "application_version", f"seo-report {inp.app_version}"))
    if tuple(version[:2]) == TESTED_PYTHON:
        out.append(F("runtime", "python", "ready", "python_supported", f"Python {version[0]}.{version[1]}.{version[2]} (tested on 3.14.7)"))
    else:
        out.append(F("runtime", "python", "unverified", "python_version_untested",
                     f"Python {version[0]}.{version[1]} has not been tested; the tested version is 3.14.x",
                     "Use the tested Python 3.14 virtual environment"))
    if machine in TESTED_MACHINES:
        out.append(F("runtime", "architecture", "ready", "architecture_tested", f"{machine}"))
    else:
        out.append(F("runtime", "architecture", "unverified", "architecture_untested", f"{machine} has not been tested"))
    if system == "Windows" and release == "11":
        out.append(F("runtime", "operating_system", "ready", "os_supported", "Windows 11"))
    elif system == "Windows":
        out.append(F("runtime", "operating_system", "unverified", "os_untested", f"Windows {release} has not been tested", "Use Windows 11"))
    else:
        out.append(F("runtime", "operating_system", "blocked", "os_unsupported", f"{system or 'This system'} is not supported (Windows 11 only)",
                     "Run the application on Windows 11"))
    return out


# --- dependencies --------------------------------------------------------------------------------------------------------------

def check_dependencies(inp: Inputs) -> list[Finding]:
    pins = inp.probe("pins", None)
    if pins is None:
        pins = read_pins(inp.probe("requirements_path", rc.PROJECT_ROOT / "requirements.txt"))
    if pins is None:
        return [F("dependencies", "requirements", "unverified", "requirements_file_missing",
                  "requirements.txt was not found, so installed packages cannot be compared with the pinned versions",
                  "Run the check from the project folder")]
    versions = inp.probe("installed_version", installed_version)
    missing = sorted(name for name in pins if versions(name) is None)
    differ = sorted(name for name, wanted in pins.items() if versions(name) not in (None, wanted))
    out = []
    if missing:
        out.append(F("dependencies", "packages", "blocked", "dependency_missing",
                     f"{len(missing)} required package(s) are not installed: {', '.join(missing)}",
                     "Install the pinned packages: python -m pip install -r requirements-lock.txt"))
    if differ:
        out.append(F("dependencies", "versions", "warning", "dependency_version_differs",
                     f"{len(differ)} package(s) differ from the tested version: {', '.join(differ)}",
                     "Reinstall from requirements-lock.txt; untested versions can change reports"))
    if not missing and not differ:
        out.append(F("dependencies", "packages", "ready", "dependencies_match_pins", f"all {len(pins)} pinned packages are installed at the tested versions"))
    return out


# --- Playwright and Chromium ---------------------------------------------------------------------------------------------------

def chromium_folders(environ) -> list[str]:
    base = environ.get("PLAYWRIGHT_BROWSERS_PATH") or str(Path(environ.get("LOCALAPPDATA", "")) / "ms-playwright")
    try:
        return sorted(p.name for p in Path(base).glob("chromium-*")) if Path(base).is_dir() else []
    except OSError:
        return []


_LAUNCH_SCRIPT = (
    "import json, sys\n"
    "from playwright.sync_api import sync_playwright\n"
    "with sync_playwright() as p:\n"
    "    b = p.chromium.launch()\n"
    "    v = b.version\n"
    "    b.close()\n"
    "print(json.dumps({'launched': True, 'browser': v}))\n")


def launch_probe(environ, timeout=LAUNCH_TIMEOUT_SECONDS) -> dict:
    """Start and stop headless Chromium in a separate process with its own temporary folder (created and removed here). No network request is made."""
    scratch = tempfile.mkdtemp(prefix="seo-doctor-")
    env = {**os.environ, **environ, "TEMP": scratch, "TMP": scratch}
    try:
        result = subprocess.run([sys.executable, "-I", "-c", _LAUNCH_SCRIPT], capture_output=True, text=True, timeout=timeout, env=env,
                                cwd=scratch)
        if result.returncode == 0:
            try:
                return json.loads(result.stdout.strip().splitlines()[-1])
            except (ValueError, IndexError):
                return {"launched": False, "error": "unreadable_probe_output"}
        return {"launched": False, "error": "launch_failed"}
    except subprocess.TimeoutExpired:
        return {"launched": False, "error": "timeout"}
    except OSError:
        return {"launched": False, "error": "probe_not_started"}
    finally:
        shutil.rmtree(scratch, ignore_errors=True)  # only the folder this function created


def check_browser(inp: Inputs) -> list[Finding]:
    available = inp.probe("playwright_available", lambda: importlib.util.find_spec("playwright") is not None)()
    if not available:
        return [F("browser", "playwright", "blocked", "playwright_missing", "the Playwright package is not installed",
                  "python -m pip install -r requirements-lock.txt")]
    version = inp.probe("playwright_version", lambda: installed_version("playwright"))()
    out = [F("browser", "playwright", "ready", "playwright_installed", f"Playwright {version}")]
    folders = inp.probe("chromium_folders", chromium_folders)(inp.environ)
    if not folders:
        out.append(F("browser", "chromium", "blocked", "chromium_missing", "no Chromium build was found for Playwright",
                     "python -m playwright install chromium (needs network access once)"))
        return out
    out.append(F("browser", "chromium", "ready", "chromium_present", f"Chromium build folder present ({folders[-1]})"))
    if not inp.launch_browser:
        out.append(F("browser", "chromium_launch", "unverified", "chromium_launch_not_tested",
                     "starting the browser was skipped", "Run `doctor` without --skip-browser-launch"))
        return out
    result = inp.probe("launch_probe", launch_probe)(inp.environ)
    if result.get("launched"):
        out.append(F("browser", "chromium_launch", "ready", "chromium_launches", f"headless Chromium starts and stops ({result.get('browser', 'version unknown')})"))
    else:
        reason = {"timeout": "chromium_launch_timeout"}.get(result.get("error"), "chromium_launch_failed")
        out.append(F("browser", "chromium_launch", "blocked", reason, "headless Chromium could not be started",
                     "Reinstall Chromium (python -m playwright install chromium) and check security software"))
    return out


# --- assets --------------------------------------------------------------------------------------------------------------------

def check_assets(inp: Inputs) -> list[Finding]:
    settings = inp.settings
    if settings is None:
        return [F("assets", "logo_and_fonts", "unverified", "config_required", "assets are located through the settings file, which is not available",
                  "Fix the configuration first")]
    out, measured = [], {}
    items = [("logo", settings.logo_path)] + [(f.label, f.path) for f in settings.fonts]
    if not settings.fonts:
        out.append(F("assets", "fonts", "blocked", "fonts_not_configured", "no report fonts are configured (render.fonts)",
                     "List DejaVuSans.ttf and DejaVuSans-Bold.ttf under render.fonts"))
    for label, path in items:
        try:
            measured[label] = af.sha256_of_file(path, role=label)
        except af.AssetFingerprintError as error:
            out.append(F("assets", label, "blocked", f"asset_{error.code}", f"{label}: {error.code.replace('_', ' ')}",
                         "Provision the file as described in docs/r8/dependency_and_asset_readiness.md"))
            continue
        expected = REFERENCE_HASHES.get(label)
        if expected is not None and measured[label] != expected:
            out.append(F("assets", label, "warning", "asset_differs_from_tested",
                         f"{label} readable, but it is not the tested file; every future report identity will change",
                         "Use the tested file unless the change is deliberate"))
        else:
            out.append(F("assets", label, "ready", "asset_readable", f"{label} readable; fingerprint {measured[label][:12]}"))
    configured = {Path(os.fspath(f.path)).name for f in settings.fonts}
    for label, filename in REQUIRED_FONT_FILES.items():
        if settings.fonts and filename not in configured:
            out.append(F("assets", label, "blocked", "required_font_missing", f"the required font file {filename} is not configured",
                         "Add it to render.fonts"))
    if measured and not any(f.status == "blocked" for f in out):
        try:  # the renderer's own fingerprint implementation must agree with the hashes just measured
            from seo_report import render_identity as idn
            fingerprints = idn.measure_render_fingerprints(logo_path=settings.logo_path, font_paths={f.label: f.path for f in settings.fonts})
            same = fingerprints.logo_sha256 == measured["logo"] and all(item.sha256 == measured.get(item.name) for item in fingerprints.fonts)
            out.append(F("assets", "fingerprints", "ready" if same else "blocked",
                         "fingerprints_agree" if same else "fingerprints_disagree",
                         "the rendering fingerprints agree with the measured file hashes" if same else
                         "the rendering fingerprints do not agree with the measured file hashes", "" if same else "Do not run reports; report this"))
        except Exception:  # a failure of the engine's own measurement is a blocker, not a crash of the check
            out.append(F("assets", "fingerprints", "blocked", "fingerprints_unmeasurable", "the rendering fingerprints could not be measured",
                         "Check the font and logo files"))
    return out


# --- storage -------------------------------------------------------------------------------------------------------------------

def _nearest_existing(path: Path) -> Path | None:
    current = Path(path)
    for _ in range(64):
        if current.exists():
            return current
        if current.parent == current:
            return None
        current = current.parent
    return None


def check_storage(inp: Inputs) -> list[Finding]:
    settings = inp.settings
    if settings is None:
        return [F("storage", "paths", "unverified", "config_required", "storage locations come from the settings file, which is not available",
                  "Fix the configuration first")]
    access = inp.probe("access", os.access)
    usage = inp.probe("disk_usage", shutil.disk_usage)
    folders = {"output_root": settings.output_root, "work_root": settings.work_root, "history_folder": settings.history_db_path.parent,
               "ledger_folder": settings.ledger_db_path.parent}
    out = [F("storage", "paths", "ready", "paths_validated",
             "storage locations are absolute, canonical, separate from each other, outside the project and OneDrive, and the databases are on a local fixed drive")]
    for name, folder in folders.items():
        folder = Path(folder)
        if folder.exists():
            if not folder.is_dir():
                out.append(F("storage", name, "blocked", "not_a_directory", f"{name} exists but is not a folder", "Choose another location"))
            elif not access(folder, os.W_OK):
                out.append(F("storage", name, "blocked", "directory_not_writable", f"{name} is not writable by this account",
                             "Grant the runner account write access"))
            else:
                out.append(F("storage", name, "ready", "directory_ready", f"{name} exists and is writable"))
            continue
        parent = _nearest_existing(folder)
        if parent is None or not access(parent, os.W_OK):
            out.append(F("storage", name, "blocked", "parent_not_writable", f"{name} does not exist and cannot be created by this account",
                         "Create it or grant write access to its parent"))
        else:
            out.append(F("storage", name, "warning", "directory_will_be_created", f"{name} does not exist yet; the first run creates it",
                         "No action needed"))
    anchor = _nearest_existing(Path(settings.output_root))
    if anchor is not None:
        try:
            free = usage(anchor).free
            if free < MIN_FREE_BYTES:
                out.append(F("storage", "disk_space", "warning", "low_disk_space", f"only {free / 2**30:.1f} GiB free where reports are stored",
                             "Free disk space before running"))
            else:
                out.append(F("storage", "disk_space", "ready", "disk_space_ok", f"{free / 2**30:.0f} GiB free (the monthly need is unmeasured)"))
        except OSError:
            out.append(F("storage", "disk_space", "unverified", "disk_space_unknown", "free disk space could not be read"))
    return out


# --- authentication configuration ------------------------------------------------------------------------------------------------

def check_authentication(inp: Inputs) -> list[Finding]:
    if inp.settings is None:
        return [F("authentication", "credential_locations", "unverified", "config_required",
                  "credential locations come from the settings file, which is not available", "Fix the configuration first")]
    from seo_report import auth
    try:
        paths = auth.paths_from_settings(inp.settings, inp.environ)
    except Exception as error:  # StageError for a malformed location
        return [F("authentication", "credential_locations", "blocked", getattr(error, "code", "credential_path_invalid"),
                  "the credential locations are not valid", "Correct paths.oauth_client_file / paths.oauth_cache_file")]
    state = {"client_file": auth.path_status(paths.client_file), "token_file": auth.path_status(paths.token_file)}
    access = inp.probe("access", os.access)
    out = [F("authentication", "configuration", "ready", "credential_locations_valid",
             f"credential locations come from {'the settings file' if paths.source == 'settings' else 'the migration-compatible default' if paths.source == 'default' else 'a mix of settings and the default'}")]
    if state["client_file"] == "present":
        out.append(F("authentication", "oauth_client_file", "ready", "oauth_client_file_present", "the OAuth client file exists (contents not read)"))
    elif state["client_file"] == "missing":
        out.append(F("authentication", "oauth_client_file", "warning", "oauth_client_file_missing",
                     "the OAuth client file is missing; it is only needed for `setup auth`", "Place it at the configured location before running setup"))
    else:
        out.append(F("authentication", "oauth_client_file", "blocked", "credential_path_unusable", f"the OAuth client location is {state['client_file'].replace('_', ' ')}",
                     "Check the configured path"))
    if state["token_file"] == "present":
        readable = access(paths.token_file, os.R_OK) and access(paths.token_file, os.W_OK)
        out.append(F("authentication", "cached_sign_in", "ready" if readable else "warning",
                     "cached_sign_in_present" if readable else "cached_sign_in_not_writable",
                     "a cached sign-in exists (contents not read, validity not checked)" if readable else
                     "a cached sign-in exists but this account cannot both read and update it",
                     "" if readable else "Fix the file permissions"))
    elif state["token_file"] == "missing":
        out.append(F("authentication", "cached_sign_in", "blocked", "auth_token_missing", "no cached Google sign-in exists",
                     "Run `setup auth` at an interactive console (auth_required)"))
    else:
        out.append(F("authentication", "cached_sign_in", "blocked", "auth_credentials_unreadable", f"the cached sign-in location is {state['token_file'].replace('_', ' ')}",
                     "Check the configured path and permissions"))
    folder = Path(paths.token_file).parent
    if folder.exists() and not access(folder, os.W_OK):
        out.append(F("authentication", "token_folder", "warning", "token_folder_not_writable",
                     "the sign-in folder is not writable, so a refreshed sign-in cannot be saved", "Grant write access"))
    description = auth.describe_paths(paths)
    if not description["outside_project"]:
        out.append(F("authentication", "credential_location", "warning", "credentials_inside_project",
                     "credentials are in the project folder (the migration-compatible fallback), which is under OneDrive on this machine",
                     "Follow docs/r8/credential_relocation_procedure.md when authorised"))
    elif any(rc.in_cloud_sync_folder(Path(location).parent, inp.environ) for location in (paths.client_file, paths.token_file)):  # BOTH files, independently
        out.append(F("authentication", "credential_location", "warning", "credentials_in_cloud_sync", "credentials are inside a cloud-synchronised folder",
                     "Move them to a local, per-user folder"))
    verdict = attestation_verdict(inp)
    if verdict.valid:
        out.append(F("authentication", "live_verification", "ready", "live_google_attested",
                     f"a valid local verification attestation exists (expires {verdict.expires_at}); this check does not contact Google and cannot re-verify it",
                     "Verify again before it expires"))
        return out
    out.append(F("authentication", "live_verification", "unverified", "live_google_not_verified",
                 "Google sign-in and API access have not been verified (this check never contacts Google)", "Verify at an attended session when authorised"))
    if verdict.status == "invalid":
        out.append(F("authentication", "attestation", "warning", verdict.code, _attestation().CODES.get(verdict.code, "the attestation is not valid"),
                     "A new live verification is needed when authorised"))
    return out


def _attestation():
    from seo_report import attestation
    return attestation


def attestation_verdict(inp: Inputs):
    """The verdict on the stored verification attestation (read-only; no network, no browser, nothing written). Missing settings or any unexpected problem
    is a verdict that clears nothing. The probes `attestation_protector`, `attestation_identity` and `attestation_acl_reader` let tests replace Windows services."""
    module = _attestation()
    if inp.settings is None:
        return module.Verdict("missing", "attestation_missing")
    return module.evaluate(inp.settings, inp.environ, now=inp.now(), protector=inp.probe("attestation_protector", None),
                           identity=inp.probe("attestation_identity", None), acl_reader=inp.probe("attestation_acl_reader", None))


def outstanding_for(findings, settings=None) -> list:
    """`OUTSTANDING` without the two entries whose state the preflight can observe:
      * `live_google_not_verified`, when a valid attestation was observed (finding `live_google_attested`);
      * `credentials_not_relocated`, when `credentials_relocated(settings, findings)` holds: the SAME condition the production gate applies.
    Nothing else is ever removed. Without `settings` (None) relocation is never assumed, so the entry stays."""
    attested = any(f.code == "live_google_attested" for f in findings)
    relocated = credentials_relocated(settings, findings)
    return [code for code in OUTSTANDING if not (attested and code == "live_google_not_verified")
            and not (relocated and code == "credentials_not_relocated")]


# --- client and reporting configuration ------------------------------------------------------------------------------------------

def check_configuration(inp: Inputs) -> list[Finding]:
    if inp.settings is None:
        code = inp.config_code or "config_not_given"
        message = {"config_not_given": "no settings file was given (--config or JASPER_SEO_CONFIG)",
                   "config_not_found": "the settings file does not exist"}.get(code, "the settings file is invalid")
        return [F("configuration", "settings", "blocked", code if code in CONFIG_CODES else code, message,
                  "Name a valid settings file; see config/runner.yaml.example")]
    settings = inp.settings
    out = [F("configuration", "settings", "ready", "settings_valid", "the settings file is valid")]
    from seo_report import clients as cl
    from seo_report.client_config import ClientConfigError, load_client_registry
    try:
        registry = load_client_registry(settings.clients_config_path)
        out.append(F("configuration", "client_registry", "ready", "client_registry_valid", f"{len(registry)} client key mapping(s) are valid"))
    except (ClientConfigError, OSError):
        out.append(F("configuration", "client_registry", "blocked", "client_registry_invalid", "the client registry (clients_config) is missing or invalid",
                     "Fix config/clients.yaml"))
    try:
        cl.load_default_sender(settings.sender_config_path)
        out.append(F("configuration", "sender", "ready", "sender_config_valid", "the default sender configuration is valid"))
    except (cl.SheetDataError, OSError, ValueError):
        out.append(F("configuration", "sender", "blocked", "sender_config_invalid", "the sender configuration (sender_config) is missing or invalid",
                     "Fix config/jasper.yaml"))
    if settings.reporting_spreadsheet_id:
        out.append(F("configuration", "reporting_workbook", "ready", "workbook_id_configured", "the reporting workbook identifier is configured"))
    else:
        out.append(F("configuration", "reporting_workbook", "blocked", "reporting_workbook_not_configured",
                     "workbooks.reporting_spreadsheet_id is not configured", "Add the reporting workbook identifier to the settings"))
    out.append(F("configuration", "recipients", "ready", "recipients_separate",
                 "the runner has no recipient setting; the sender's Manager Email is used only as the report's sender contact"))
    out.append(F("configuration", "workbook_rows", "unverified", "workbook_rows_not_checked",
                 "property identifiers, reporting periods and keywords live in the workbook, which this check does not read",
                 "Run `clients validate --online` when authorised"))
    return out


# --- logging -------------------------------------------------------------------------------------------------------------------

def check_logging(inp: Inputs) -> list[Finding]:
    from seo_report import runner
    out = []
    try:
        folder = runner.check_log_dir(environ=inp.environ)
        out.append(F("logging", "log_folder", "ready" if Path(folder).exists() else "warning",
                     "log_folder_ready" if Path(folder).exists() else "log_folder_will_be_created",
                     f"logs go to {redact_path(folder, inp.environ)} as JSON lines (rotating, at most 6 x 5 MB)" if Path(folder).exists() else
                     "the log folder does not exist yet; it is created when a command starts",
                     "" if Path(folder).exists() else "No action needed"))
    except runner.LoggingConfigError as error:
        out.append(F("logging", "log_folder", "blocked", error.code, "the log folder is not acceptable (it must be absolute, outside the project and OneDrive)",
                     "Fix LOCALAPPDATA or the log location"))
    status = inp.logging_status
    if status is None:
        out.append(F("logging", "activation", "unverified", "logging_state_unknown", "logging activation was not observed for this command"))
    elif status.get("active"):
        out.append(F("logging", "activation", "ready", "logging_active", "structured logging is active for this command"))
    else:
        out.append(F("logging", "activation", "blocked", status.get("code") or "logging_not_active",
                     "structured logging could not be started; diagnostic detail would be lost", "Fix the log folder and run the command again"))
    return out


# --- orchestration, readiness, exit code, report ----------------------------------------------------------------------------------

CHECKS = (("runtime", check_runtime), ("dependencies", check_dependencies), ("browser", check_browser), ("assets", check_assets),
          ("storage", check_storage), ("authentication", check_authentication), ("configuration", check_configuration),
          ("logging", check_logging))


def run_preflight(inp: Inputs) -> list[Finding]:
    """Run every check. A check that raises is reported as a blocked internal finding (and makes the exit code INTERNAL_ERROR)."""
    findings = []
    for section, check in CHECKS:
        try:
            findings.extend(check(inp))
        except Exception:
            findings.append(F(section, "check", "blocked", "preflight_internal_error", "this check failed unexpectedly (details are in the log)",
                              "Report this"))
    return findings


def overall_readiness(findings) -> str:
    statuses = {f.status for f in findings}
    return "blocked" if "blocked" in statuses else "warning" if "warning" in statuses else "ready"


def counts(findings) -> dict:
    return {status: sum(1 for f in findings if f.status == status) for status in STATUSES}


def exit_code(findings) -> Exit:
    blocked = [f for f in findings if f.status == "blocked"]
    if any(f.code == "preflight_internal_error" for f in blocked):
        return Exit.INTERNAL_ERROR
    if any(f.section == "configuration" and (f.component == "settings" or f.code in CONFIG_CODES) for f in blocked):
        return Exit.CONFIG_INVALID
    others = [f for f in blocked if f.code not in AUTH_CODES]
    if others:
        return Exit.PREFLIGHT_FAILED
    return Exit.AUTH_REQUIRED if blocked else Exit.OK


def local_gate(inp: Inputs) -> tuple[bool, Exit, list[Finding]]:
    """The LOCAL part of the gate: `(allowed, exit_code, blocking findings)`. Allowed when no preflight finding is blocked; warnings and unverified
    items do not block. Read-only, like the preflight itself."""
    findings = run_preflight(inp)
    blocking = [f for f in findings if f.status == "blocked"]
    return (not blocking, exit_code(findings), blocking)


# Requirements that must be cleared before anything may run in production. `credentials_not_relocated` clears itself once the credential files are
# configured outside the project (and OneDrive). `live_google_not_verified` clears ONLY while a valid verification attestation exists
# (`attestation.evaluate`: DPAPI-protected, bound to this Windows account and machine, seven days, origin "live"); nothing else can clear it, and the
# attestation never clears any other requirement. No attestation exists until the live-verification stage (R8.7e) issues one.
GATE_REQUIREMENTS = ("live_google_not_verified", "credentials_not_relocated")
_GATE_MESSAGES = {
    "live_google_not_verified": ("Google sign-in and API access have not been verified live", "Needs an authorised live verification stage"),
    "credentials_not_relocated": ("the Google credentials are still in the project folder (OneDrive)",
                                  "Relocate them as described in docs/r8/credential_relocation_procedure.md and configure paths.oauth_*")}


def _gate_message(code):
    if code in _GATE_MESSAGES:
        return _GATE_MESSAGES[code]
    return (_attestation().CODES.get(code, "the verification attestation is not valid"), "A new live verification is needed when authorised")


# Findings that mean the credentials are not (yet) where they belong: still in the project folder, in a cloud-synchronised folder, or at an invalid location.
_NOT_RELOCATED_FINDINGS = frozenset({"credentials_inside_project", "credentials_in_cloud_sync", "credential_path_invalid"})


def credentials_relocated(settings, findings) -> bool:
    """The ONE definition of "the Google credentials have been relocated", used by the production gate (`uncleared_requirements`) and by the
    `outstanding` list of `doctor` / `system-info` (`outstanding_for`), so they cannot disagree. True only when the settings are available and none of
    the not-relocated findings was produced for them (that is: both OAuth locations are validly configured outside the project and any
    cloud-synchronised folder). It looks at configured locations and file states only: no file contents, no ACLs."""
    if settings is None:
        return False
    return not ({f.code for f in findings} & _NOT_RELOCATED_FINDINGS)


def uncleared_requirements(inp: Inputs, findings) -> list[str]:
    verdict = attestation_verdict(inp)
    uncleared = [] if verdict.valid else ["live_google_not_verified"] + ([verdict.code] if verdict.status == "invalid" else [])
    if not credentials_relocated(inp.settings, findings):
        uncleared.append("credentials_not_relocated")
    return uncleared


def production_gate(inp: Inputs) -> tuple[bool, Exit, list[Finding]]:
    """The gate in front of every command that changes production state (`report run`, `report batch`, `recovery acknowledge|cancel`):
    `(allowed, exit_code, blockers)`.

    Allowed only when (1) the full local preflight (including the browser launch) has no blocked finding AND (2) every requirement in
    `GATE_REQUIREMENTS` is cleared. `live_google_not_verified` is cleared only by a valid, genuine, unexpired verification attestation (R8.7c; none
    exists yet) and `credentials_not_relocated` is independent of it, so the gate blocks real production execution, with fixed blocker codes. Exit code: the preflight's own code when the preflight is blocked, otherwise PREFLIGHT_FAILED."""
    findings = run_preflight(inp)
    blockers = [f for f in findings if f.status == "blocked"]
    for code in uncleared_requirements(inp, findings):
        message, action = _gate_message(code)
        blockers.append(Finding("production", "requirement", "blocked", code, message, action))
    code = exit_code(findings) if any(f.status == "blocked" for f in findings) else Exit.PREFLIGHT_FAILED
    return (not blockers, code if blockers else Exit.OK, blockers)


def build_report(findings, *, app_version: str, now: dt.datetime | None = None, settings=None) -> dict:
    """The stable JSON document (schema `jasper.preflight-report`, version 1):

        schema, version            "jasper.preflight-report", 1
        generated_at               UTC timestamp YYYY-MM-DDTHH:MM:SS+00:00
        application                {name, version}
        overall_readiness          "ready" | "warning" | "blocked"   (derived from the findings; unverified items do not lower it)
        production_ready           always false in R8.6c
        outstanding                fixed codes of requirements that remain before production (see OUTSTANDING and `outstanding_for`; pass `settings` so that
                                   a relocation observed by the gate's own condition is reflected)
        counts                     {ready, warning, blocked, unverified}
        exit_code                  the exit code `doctor` returns
        findings                   [{section, component, status, code, message, action}]
    """
    now = now or dt.datetime.now(dt.timezone.utc)
    return {"schema": SCHEMA, "version": VERSION, "generated_at": now.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00"),
            "application": {"name": "seo-report", "version": app_version}, "overall_readiness": overall_readiness(findings),
            "production_ready": False, "outstanding": outstanding_for(findings, settings), "counts": counts(findings), "exit_code": int(exit_code(findings)),
            "findings": [f.to_dict() for f in findings]}


def format_text(report: dict) -> list[str]:
    """Human-readable lines grouped in the nine sections."""
    lines = [f"seo-report {report['application']['version']} — deployment preflight ({report['generated_at']})", ""]
    by_section = {s: [f for f in report["findings"] if f["section"] == s] for s in SECTIONS}
    for number, section in enumerate(SECTIONS, start=1):
        lines.append(f"{number}. {SECTION_TITLES[section]}")
        for f in by_section[section]:
            lines.append(f"   [{f['status']:<10}] {f['component']}: {f['message']} ({f['code']})")
            if f["action"]:
                lines.append(f"                next: {f['action']}")
        lines.append("")
    c = report["counts"]
    lines.append("9. Overall readiness")
    lines.append(f"   Local checks: {report['overall_readiness'].upper()}  "
                 f"(ready {c['ready']}, warning {c['warning']}, blocked {c['blocked']}, unverified {c['unverified']})")
    lines.append("   Production-ready: NO — outstanding: " + ", ".join(report["outstanding"]))
    lines.append(f"   Exit code: {report['exit_code']}")
    return lines
