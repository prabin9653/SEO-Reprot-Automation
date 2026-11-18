"""One-time, per-device setup: `seo setup device` (stage 4 of the operator-experience work).

A teammate's computer needs, besides the installed program (`scripts/install_device.ps1`): the per-user folders, the two report fonts, a settings file, the team's
OAuth client file and a Google sign-in. This module does the parts that can be done safely by a command and tells the person, in order, what is left.

What it does
  * creates `%LOCALAPPDATA%\\JasperSEOReporting\\{config,credentials,fonts}` (refusing a OneDrive location) and, when it can, restricts the credentials folder to the
    current Windows account (`icacls`, the documented credential procedure; a failure is a warning with the command to run by hand);
  * copies the two bundled DejaVu fonts into `...\\fonts` after checking their SHA-256 (the report is rendered with exactly these bytes), and never overwrites a
    font that is already there;
  * writes `...\\config\\runner.yaml` (workbook id, font and credential locations; everything else uses the defaults) and validates it with the real settings
    loader before it is put in place. An existing settings file is never changed;
  * reports whether the client file, the Google sign-in and the client registry are present.

What it never does
  * It never reads, prints, copies or creates a credential, a token or a client secret, and never contacts Google. Only the presence of the two credential files is
    looked at (file metadata).
  * It installs nothing and changes no system setting other than the credentials-folder permission above.
  * It never overwrites an existing settings file, font or credential.
"""

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from seo_report import runner_config as rc
from seo_report.snapshots.store import in_cloud_sync_folder

# The report is rendered with these exact font files (docs/r8/dependency_and_asset_readiness.md): DejaVu Sans 2.35 as bundled with matplotlib 3.11.2.
FONTS = (("DejaVuSans", "DejaVuSans.ttf", "400", "3fdf69cabf06049ea70a00b5919340e2ce1e6d02b0cc3c4b44fb6801bd1e0d22"),
         ("DejaVuSans-Bold", "DejaVuSans-Bold.ttf", "700", "b184b89e3c1075f22f6b71575b6fc20d4972b3cfd3b23322ca6fd596dcaef167"))
LICENSE_NAME = "LICENSE_DEJAVU"
CLIENT_FILE, SIGN_IN_FILE = "client.json", "login.json"
_SHEET_ID = re.compile(r"[A-Za-z0-9_-]{20,100}")
_SHEET_URL = re.compile(r"https://docs\.google\.com/spreadsheets/d/([A-Za-z0-9_-]{20,100})(?:[/?#].*)?")

OK, CREATED, TODO, WARNING, PROBLEM = "ok", "created", "todo", "warning", "problem"


@dataclass
class Step:
    name: str
    status: str
    detail: str
    fix: str | None = None


@dataclass
class SetupResult:
    steps: list = field(default_factory=list)
    config_path: Path | None = None

    def add(self, name, status, detail, fix=None):
        self.steps.append(Step(name, status, detail, fix))

    @property
    def problems(self) -> list:
        return [s for s in self.steps if s.status == PROBLEM]

    @property
    def todos(self) -> list:
        return [s for s in self.steps if s.status == TODO]


def parse_spreadsheet(value) -> str | None:
    """The spreadsheet ID from a plain ID or a Google Sheets address; None when it is neither."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    match = _SHEET_URL.fullmatch(text)
    if match:
        return match.group(1)
    return text if _SHEET_ID.fullmatch(text) and not text.startswith("REPLACE_WITH") else None


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def restrict_to_current_user(folder: Path, environ) -> bool:
    """Limit a folder to the current account, SYSTEM and Administrators (the documented credential-folder permission). True on success."""
    user = environ.get("USERNAME")
    if os.name != "nt" or not user:
        return False
    try:
        done = subprocess.run(["icacls", str(folder), "/inheritance:r", "/grant:r", f"{user}:(OI)(CI)F", "SYSTEM:(OI)(CI)F", "Administrators:(OI)(CI)F"],
                              capture_output=True, text=True, timeout=30, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return False
    return done.returncode == 0


def settings_text(credentials: Path, fonts: Path, spreadsheet_id: str, shared_folder: str | None = None) -> str:
    q = json.dumps  # a JSON string is also a valid YAML double-quoted string, with every backslash escaped
    lines = ["# Written by `seo setup device`. Settings not listed here use the defaults (storage under %LOCALAPPDATA%\\JasperSEOReporting, the project's",
             "# client registry, sender settings and logo). Never put credentials in this file.", "paths:",
             f"  oauth_client_file: {q(str(credentials / CLIENT_FILE))}", f"  oauth_cache_file: {q(str(credentials / SIGN_IN_FILE))}"]
    if shared_folder:
        lines += [f"  shared_activity_dir: {q(shared_folder)}"]
    lines += ["render:", "  fonts:"]
    for label, filename, weight, _sha in FONTS:
        lines += [f"    - label: {label}", f"      path: {q(str(fonts / filename))}", f"      weight: {q(weight)}"]
    lines += ["workbooks:", f"  reporting_spreadsheet_id: {q(spreadsheet_id)}", ""]
    return "\n".join(lines)


def _copy_verified(source: Path, target: Path, expected_sha: str | None) -> None:
    """Copy through a temporary file in the target folder and put it in place, never replacing an existing file."""
    handle, temporary = tempfile.mkstemp(dir=target.parent, prefix=target.name + ".", suffix=".tmp")
    os.close(handle)
    try:
        shutil.copyfile(source, temporary)
        if expected_sha is not None and sha256_of(Path(temporary)) != expected_sha:
            raise OSError("the copy does not match the expected fingerprint")
        os.rename(temporary, target)  # fails (instead of replacing) if the target appeared in the meantime
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def run_device_setup(environ, *, spreadsheet=None, project_root=None, acl_applier=restrict_to_current_user, show=None, shared_folder=None) -> SetupResult:
    """Do the safe parts of a device setup and describe what is left. `show` turns a path into display text (redaction); `acl_applier` is a test seam."""
    project_root = Path(project_root or rc.PROJECT_ROOT)
    show = show or (lambda path: str(path))
    result = SetupResult()
    base = rc.default_local_base(environ)
    folders = {name: base / name for name in ("config", "credentials", "fonts")}

    version = sys.version_info
    result.add("python", OK if version >= (3, 14) else WARNING, f"Python {version.major}.{version.minor}.{version.micro}",
               None if version >= (3, 14) else "The project is tested with Python 3.14; install it and re-run scripts\\install_device.ps1.")

    if in_cloud_sync_folder(base, environ):
        result.add("folders", PROBLEM, "the per-user folder is inside a OneDrive folder (the databases and credentials must not be synchronised)",
                   "Set LOCALAPPDATA to a local, non-OneDrive location, or run this from a normal Windows account.")
        return result
    made = []
    try:
        for name, folder in folders.items():
            if not folder.is_dir():
                folder.mkdir(parents=True, exist_ok=True)
                made.append(name)
    except OSError:
        result.add("folders", PROBLEM, f"could not create {show(base)}", "Check that this Windows account may write to its own AppData folder.")
        return result
    result.add("folders", CREATED if made else OK, f"{show(base)} ({'created ' + ', '.join(made) if made else 'already there'})")

    restricted = acl_applier(folders["credentials"], environ)
    result.add("credentials_folder", OK if restricted else WARNING,
               "restricted to your Windows account" if restricted else "could not restrict the credentials folder to your account",
               None if restricted else f'Run: icacls "{folders["credentials"]}" /inheritance:r /grant:r "%USERNAME%:(OI)(CI)F" "SYSTEM:(OI)(CI)F" "Administrators:(OI)(CI)F"')

    # fonts: verified copies of the bundled files; an existing font is checked, never replaced
    bundled = project_root / "assets" / "fonts"
    for label, filename, _weight, sha in FONTS:
        target, source = folders["fonts"] / filename, bundled / filename
        if target.exists():
            same = target.is_file() and sha256_of(target) == sha
            result.add(f"font_{label}", OK if same else PROBLEM, f"{filename} is already there" + ("" if same else " but is not the expected file"),
                       None if same else f"Move {show(target)} away and run this again (the report needs exactly the bundled font).")
            continue
        try:
            if not source.is_file() or sha256_of(source) != sha:
                raise OSError("the bundled font is missing or altered")
            _copy_verified(source, target, sha)
            result.add(f"font_{label}", CREATED, f"{filename} copied and verified")
        except OSError:
            result.add(f"font_{label}", PROBLEM, f"{filename} could not be installed from the project's assets\\fonts folder",
                       "Restore assets\\fonts from the project (git checkout or a fresh copy) and run this again.")
    licence = folders["fonts"] / LICENSE_NAME
    if not licence.exists() and (bundled / LICENSE_NAME).is_file():
        try:
            _copy_verified(bundled / LICENSE_NAME, licence, None)
        except OSError:
            pass

    # the settings file
    config = folders["config"] / "runner.yaml"
    result.config_path = config
    if config.exists():
        try:
            rc.load_settings(config, environ=environ)
            result.add("settings", OK, f"{show(config)} already exists and is valid (not changed)")
        except rc.RunnerConfigError as error:
            result.add("settings", WARNING, f"{show(config)} already exists (not changed) but is invalid: {error.code}",
                       "Fix it by hand, or rename it and run this again with --spreadsheet-id.")
    else:
        sheet_id = parse_spreadsheet(spreadsheet) if spreadsheet else None
        shared = str(shared_folder).strip() if shared_folder else None
        if shared and not (Path(shared).is_absolute() and ".." not in Path(shared).parts):
            result.add("settings", PROBLEM, "the shared folder must be a full path such as D:\\TeamShare\\seo-activity or \\\\server\\share\\seo-activity",
                       "Run again with a full --shared-folder path, or leave it out (it can be added to the settings file later).")
        elif not sheet_id:
            result.add("settings", PROBLEM, "the reporting workbook is not given" if not spreadsheet else "that is not a Google Sheets address or spreadsheet ID",
                       "Run again with --spreadsheet-id and the workbook's address (https://docs.google.com/spreadsheets/d/.../edit) or its ID.")
        else:
            temporary = config.with_name("runner.yaml.new")
            try:
                temporary.write_text(settings_text(folders["credentials"], folders["fonts"], sheet_id, shared), encoding="utf-8", newline="\n")
                rc.load_settings(temporary, environ=environ)
                os.replace(temporary, config)
                result.add("settings", CREATED, f"{show(config)} written and validated")
            except (rc.RunnerConfigError, OSError) as error:
                try:
                    temporary.unlink()
                except OSError:
                    pass
                code = getattr(error, "code", type(error).__name__)
                result.add("settings", PROBLEM, f"the settings file could not be created ({code}); nothing was written", "Check the spreadsheet ID and the folders above.")

    # what only a person can provide
    client_file, sign_in = folders["credentials"] / CLIENT_FILE, folders["credentials"] / SIGN_IN_FILE
    result.add("client_file", OK if client_file.is_file() else TODO, "the team's OAuth client file is in place" if client_file.is_file() else "the team's OAuth client file is missing",
               None if client_file.is_file() else f"Get {CLIENT_FILE} from the team lead (through a password manager or another private channel, never e-mail or chat) "
                                                  f"and save it as {show(client_file)}")
    result.add("sign_in", OK if sign_in.is_file() else TODO, "a Google sign-in is saved on this device" if sign_in.is_file() else "no Google sign-in is saved yet",
               None if sign_in.is_file() else "Run `seo setup auth` and sign in with the team's Google account (needs the client file first).")
    registry = project_root / "config" / "clients.yaml"
    result.add("client_registry", OK if registry.is_file() else TODO, "the client registry is present" if registry.is_file() else "the client registry (config\\clients.yaml) is missing",
               None if registry.is_file() else "Copy config\\clients.yaml from the team lead so everyone uses the same client keys "
                                               "(or, after sign-in, run `seo clients add --all-missing --yes` to build it from the sheet).")
    return result
