"""Non-secret settings of the production runner (R8.3).

`load_settings(path)` / `parse_settings(data)` turn a YAML mapping (see `config/runner.yaml.example`) into one frozen
`RunnerSettings`. Nothing is created, read from the credential store or written: paths are only validated.

  * Credentials never live here. Any key that names a secret (secret, token, password, credential, api key, private
    key) or any value that looks like a token is refused with `credential_in_config`, naming the key path only.
  * Locations follow the existing storage layout: the output root and the history database resolve exactly as
    `output_layout.resolve_output_root` / `snapshots.store.resolve_db_path` do (explicit value, environment variable,
    `%LOCALAPPDATA%\\JasperSEOReporting\\...`), and OneDrive folders are refused.
  * The execution ledger is a different file from the history database; it must not overlap the history database, the
    output root, the work root or the project tree, and must not sit in a OneDrive folder.
  * Every path must be absolute (Windows drive paths and UNC paths are accepted as `pathlib` understands them).

Errors are `RunnerConfigError(code, field)`: a fixed code and the setting's name, never its value.
"""

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from seo_report import output_layout as layout
from seo_report.snapshots.store import in_cloud_sync_folder, resolve_db_path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
ENV_LEDGER_DB = "JASPER_SEO_LEDGER_DB"
MAX_CONFIG_BYTES = 256 * 1024

TOP_LEVEL = ("paths", "render", "workbooks", "execution")
PATH_KEYS = ("output_root", "history_db", "ledger_db", "work_root", "clients_config", "sender_config", "oauth_client_file",
             "oauth_cache_file", "shared_activity_dir", "shared_reports_dir")
RENDER_KEYS = ("logo", "fonts")
FONT_KEYS = ("label", "path", "weight")
WORKBOOK_KEYS = ("reporting_spreadsheet_id", "operational_spreadsheet_id")
EXECUTION_KEYS = ("lock_ttl_seconds", "inter_client_pause_seconds", "client_timeout_seconds", "batch_deadline_seconds",
                  "max_automatic_attempts", "retry_delays_seconds", "stage_timeouts", "heartbeat_interval_seconds")
STAGE_KEYS = ("collecting", "rendering", "finalizing", "auditing", "publishing")

_SECRET_KEY = re.compile(r"secret|token|password|passwd|credential|api[_-]?key|private[_-]?key", re.IGNORECASE)
_SECRET_VALUE = re.compile(r"ya29\.|1//[\w-]{10,}|GOCSPX-|-----BEGIN [A-Z ]*PRIVATE KEY|Bearer\s+\S", re.IGNORECASE)
_SPREADSHEET_ID = re.compile(r"[A-Za-z0-9_-]{20,100}")
_LABEL = re.compile(r"[A-Za-z0-9._-]{1,64}")


class RunnerConfigError(ValueError):
    """A setting is missing or invalid. `code` is fixed; `field` names the setting; the value is never included."""

    def __init__(self, code: str, field: str = ""):
        super().__init__(f"{code}: {field}" if field else code)
        self.code = code
        self.field = field


@dataclass(frozen=True)
class FontSetting:
    label: str
    path: Path
    weight: str = "400"


@dataclass(frozen=True)
class StageTimeouts:
    collecting: float = 600.0
    rendering: float = 300.0
    finalizing: float = 180.0
    auditing: float = 120.0
    publishing: float = 300.0


@dataclass(frozen=True)
class RunnerSettings:
    output_root: Path
    history_db_path: Path
    ledger_db_path: Path
    work_root: Path
    clients_config_path: Path
    sender_config_path: Path
    logo_path: Path
    fonts: tuple[FontSetting, ...]
    reporting_spreadsheet_id: str | None = None
    operational_spreadsheet_id: str | None = None
    lock_ttl_seconds: float = 600.0
    inter_client_pause_seconds: float = 1.0
    client_timeout_seconds: float = 1500.0
    batch_deadline_seconds: float = 21600.0
    max_automatic_attempts: int = 3
    retry_delays_seconds: tuple[int, ...] = (300, 900)
    stage_timeouts: StageTimeouts = field(default_factory=StageTimeouts)
    # Where the Google OAuth client file and the cached sign-in live (R8.6b). None = the migration-compatible fallback inside the project
    # (`auth.CLIENT_SECRET_PATH` / `auth.TOKEN_PATH`). The recommended future location is %LOCALAPPDATA%\JasperSEOReporting\credentials\.
    oauth_client_path: Path | None = None
    oauth_cache_path: Path | None = None
    heartbeat_interval_seconds: float = 60.0  # how often a running client's leases are renewed (at most a third of the lease)
    # An optional folder the team shares (a synchronised or network folder) where each computer appends a small "report generated" line, so teammates can see
    # that a report already exists elsewhere (`activity`). None = off: nothing is shared.
    activity_dir: Path | None = None
    # An optional folder the team shares (for example Google Drive for desktop) where each finished report PDF is COPIED and a read-only `dashboard.html` is kept. The
    # original report stays in the output folder. None = off.
    reports_dir: Path | None = None


# --- helpers ----------------------------------------------------------------------------------------------------------------

def _fail(code, field_name):
    raise RunnerConfigError(code, field_name)


def _mapping(value, allowed, field_name):
    if value is None:
        return {}
    if not isinstance(value, dict):
        _fail("object_expected", field_name)
    for key in value:
        if not isinstance(key, str):
            _fail("key_invalid", field_name)
    extra = sorted(key for key in value if key not in allowed)
    if extra:
        _fail("setting_unknown", f"{field_name}.{extra[0]}" if field_name else extra[0])
    return value


def _scan_for_secrets(value, where=""):
    if isinstance(value, dict):
        for key, item in value.items():
            path = f"{where}.{key}" if where else str(key)
            if isinstance(key, str) and _SECRET_KEY.search(key):
                _fail("credential_in_config", path)
            _scan_for_secrets(item, path)
    elif isinstance(value, (list, tuple)):
        for position, item in enumerate(value):
            _scan_for_secrets(item, f"{where}[{position}]")
    elif isinstance(value, str) and _SECRET_VALUE.search(value):
        _fail("credential_in_config", where or "value")


def _absolute(value, field_name) -> Path:
    if not isinstance(value, (str, os.PathLike)) or not str(value).strip():
        _fail("path_invalid", field_name)
    path = Path(os.path.expandvars(os.fspath(value)))
    if "%" in str(path) or not path.is_absolute():
        _fail("path_not_absolute", field_name)  # an unexpanded %VARIABLE% also lands here
    if ".." in re.split(r"[\\/]+", str(path)):
        _fail("path_ambiguous", field_name)  # `..` after a link means something else to the OS than to a text collapse
    return Path(os.path.abspath(path))


# --- canonical paths and storage locations (R8.4.3) ---------------------------------------------------------------------------
#
# Two spellings can name one folder: case differences, 8.3 short names, symbolic links, directory junctions and aliased
# parents. Overlap checks therefore compare CANONICAL paths: the deepest existing ancestor is resolved by the operating
# system (read-only: nothing is created, opened for writing or followed beyond a stat) and the not-yet-existing rest is
# appended. Anything that cannot be resolved with certainty is refused (`path_ambiguous`), never guessed.

_RESERVED_NAMES = frozenset({"CON", "PRN", "AUX", "NUL", *(f"COM{n}" for n in range(1, 10)), *(f"LPT{n}" for n in range(1, 10))})
_FIXED = "fixed"


def _strip_extended(text: str) -> str:
    if text.upper().startswith("\\\\?\\UNC\\"):
        return "\\\\" + text[8:]
    if text.startswith("\\\\?\\") or text.startswith("\\\\.\\"):
        return text[4:]
    return text


def is_unc_path(path) -> bool:
    """True for UNC spellings (`//server/share`, and the extended `UNC` form): a lexical test with no network access."""
    text = os.fspath(path).replace("/", "\\")
    if text.upper().startswith("\\\\?\\UNC\\"):
        return True
    return text.startswith("\\\\") and not text.startswith(("\\\\?\\", "\\\\.\\"))


def canonical_path(path, field_name: str = "") -> Path:
    """The canonical absolute form of `path`, resolving links, junctions, 8.3 names and case through the existing
    ancestors. Raises `RunnerConfigError("path_ambiguous")` when that cannot be done with certainty."""
    try:
        raw = os.fspath(path)
    except TypeError:
        _fail("path_ambiguous", field_name)
    if not isinstance(raw, str) or not raw or "\x00" in raw or not os.path.isabs(raw):
        _fail("path_ambiguous", field_name)
    _, rest = os.path.splitdrive(raw)
    parts = [p for p in re.split(r"[\\/]+", rest) if p and p != "."]
    if ".." in parts:
        _fail("path_ambiguous", field_name)
    if os.name == "nt":
        for part in parts:  # Windows silently drops trailing dots/spaces and treats `:` and device names specially
            if part.endswith((".", " ")) or ":" in part or part.split(".")[0].rstrip().upper() in _RESERVED_NAMES:
                _fail("path_ambiguous", field_name)
    head, tail = os.path.abspath(raw), []
    try:
        while True:
            try:
                os.stat(head)
                break
            except (FileNotFoundError, NotADirectoryError):
                try:
                    os.lstat(head)
                except OSError:
                    pass
                else:
                    _fail("path_ambiguous", field_name)  # a dangling link: where it would lead is unknown
                parent, name = os.path.split(head)
                if parent == head or not name:
                    _fail("path_ambiguous", field_name)
                tail.append(name)
                head = parent
        resolved = os.path.realpath(head, strict=True)
    except (OSError, ValueError, RuntimeError):
        _fail("path_ambiguous", field_name)
    return Path(_strip_extended(resolved)).joinpath(*reversed(tail))


def canonical_key(path, field_name: str = "") -> str:
    return os.path.normcase(str(canonical_path(path, field_name)))


def keys_overlap(a: str, b: str) -> bool:
    return a == b or a.startswith(b.rstrip(os.sep) + os.sep) or b.startswith(a.rstrip(os.sep) + os.sep)


def _key(path: Path) -> str:
    return canonical_key(path)


def _overlaps(left: Path, right: Path) -> bool:
    """True when the canonical paths are equal or one contains the other. If either cannot be resolved with certainty the
    answer is True (fail closed)."""
    try:
        return keys_overlap(canonical_key(left), canonical_key(right))
    except RunnerConfigError:
        return True


def windows_drive_type(root: str) -> str:
    """'fixed', 'remote', 'removable', 'cdrom', 'ramdisk', 'no_root' or 'unknown' for a drive root such as `C:\\`
    (Windows `GetDriveTypeW`). Anything that cannot be determined is 'unknown'."""
    if os.name != "nt":
        return "unknown"
    try:
        import ctypes
        code = ctypes.windll.kernel32.GetDriveTypeW(ctypes.c_wchar_p(root))
    except (AttributeError, OSError, ImportError, ValueError):
        return "unknown"
    return {2: "removable", 3: _FIXED, 4: "remote", 5: "cdrom", 6: "ramdisk", 1: "no_root"}.get(code, "unknown")


drive_type_of = windows_drive_type  # replaced by tests; always looked up at call time


def _check_drive(path: str, field_name: str) -> None:
    if is_unc_path(path):
        _fail("path_on_network", field_name)
    drive, _ = os.path.splitdrive(_strip_extended(path))
    if not (len(drive) == 2 and drive[1] == ":" and drive[0].isalpha()):
        _fail("drive_type_unknown", field_name)
    try:
        kind = drive_type_of(drive.upper() + "\\")
    except Exception:  # an unusable probe is an unknown drive, never a pass
        kind = "unknown"
    if kind == "remote":
        _fail("path_on_network", field_name)
    if kind != _FIXED:  # removable, optical, RAM, missing or undeterminable: a database does not belong there
        _fail("drive_type_unknown", field_name)


def require_local_database(path, field_name: str = "") -> None:
    """Refuse a SQLite database location that is not on a local fixed disk: UNC paths, mapped network drives (also when
    reached through a link) and drives whose type cannot be determined. The path as written is judged first, without any
    file access, so a dead network share is never touched; then the canonical path is judged again."""
    raw = os.fspath(path)
    _check_drive(raw, field_name)
    _check_drive(str(canonical_path(raw, field_name)), field_name)


def _number(value, field_name, *, low, high, integer=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or (integer and not isinstance(value, int)):
        _fail("number_invalid", field_name)
    if not low <= value <= high:
        _fail("number_out_of_range", field_name)
    return value


def _spreadsheet_id(value, field_name):
    if value is None:
        return None
    if not isinstance(value, str) or not _SPREADSHEET_ID.fullmatch(value):
        _fail("spreadsheet_id_invalid", field_name)
    if value.startswith("REPLACE_WITH"):
        _fail("placeholder_value", field_name)
    return value


def default_local_base(environ=None) -> Path:
    environ = os.environ if environ is None else environ
    base = environ.get("LOCALAPPDATA")
    return (Path(base) if base else Path.home() / ".local" / "share") / "JasperSEOReporting"


# --- parsing ----------------------------------------------------------------------------------------------------------------

def parse_settings(data, *, environ=None) -> RunnerSettings:
    """Validate a settings mapping. `environ` defaults to the process environment (tests pass their own)."""
    environ = os.environ if environ is None else environ
    if data is None:
        data = {}
    _scan_for_secrets(data)
    top = _mapping(data, TOP_LEVEL, "")
    paths = _mapping(top.get("paths"), PATH_KEYS, "paths")
    render = _mapping(top.get("render"), RENDER_KEYS, "render")
    books = _mapping(top.get("workbooks"), WORKBOOK_KEYS, "workbooks")
    execution = _mapping(top.get("execution"), EXECUTION_KEYS, "execution")

    history = _absolute(paths["history_db"], "paths.history_db") if paths.get("history_db") is not None else \
        Path(os.path.abspath(resolve_db_path(None, environ=environ)))
    require_local_database(history, "paths.history_db")
    if in_cloud_sync_folder(history, environ):
        _fail("path_in_cloud_sync", "paths.history_db")
    try:
        output_root = layout.resolve_output_root(None if paths.get("output_root") is None else _absolute(paths["output_root"], "paths.output_root"), environ=environ, db_path=history)
    except layout.OutputLayoutError:
        _fail("output_root_invalid", "paths.output_root")
    base = default_local_base(environ)
    ledger = _absolute(paths["ledger_db"], "paths.ledger_db") if paths.get("ledger_db") is not None else (
        _absolute(environ[ENV_LEDGER_DB], ENV_LEDGER_DB) if environ.get(ENV_LEDGER_DB)
        else Path(os.path.abspath(base / "ledger" / "ledger.sqlite")))
    work_root = _absolute(paths["work_root"], "paths.work_root") if paths.get("work_root") is not None else \
        Path(os.path.abspath(base / "work"))

    require_local_database(ledger, "paths.ledger_db")
    named = (("paths.ledger_db", ledger), ("paths.work_root", work_root), ("paths.history_db", history),
             ("paths.output_root", output_root))
    keys = {name: canonical_key(path, name) for name, path in named}  # unresolvable -> path_ambiguous, naming the setting
    project = canonical_key(PROJECT_ROOT, "project")
    for name, path in named:
        if keys_overlap(keys[name], project):
            _fail("path_inside_project", name)
        if name != "paths.output_root" and in_cloud_sync_folder(path, environ):
            _fail("path_in_cloud_sync", name)
    if keys_overlap(keys["paths.ledger_db"], keys["paths.history_db"]):
        _fail("ledger_overlaps_history", "paths.ledger_db")
    if keys_overlap(keys["paths.ledger_db"], keys["paths.output_root"]):
        _fail("ledger_overlaps_output", "paths.ledger_db")
    if keys_overlap(keys["paths.history_db"], keys["paths.output_root"]):
        _fail("history_overlaps_output", "paths.history_db")
    work = keys["paths.work_root"]
    if any(keys_overlap(work, keys[other]) for other in ("paths.output_root", "paths.ledger_db", "paths.history_db")):
        _fail("work_root_overlaps", "paths.work_root")

    # The Google OAuth client file and cached sign-in (optional). Files, not folders; never opened here; they may not live in the storage
    # folders (a report folder is not a place for credentials) and the two must differ. The fallback (None) stays inside the project.
    oauth_client = oauth_cache = None
    for field_key, name in (("oauth_client_file", "paths.oauth_client_file"), ("oauth_cache_file", "paths.oauth_cache_file")):
        if paths.get(field_key) is None:
            continue
        found = _absolute(paths[field_key], name)
        if found.name in ("", ".") or str(paths[field_key]).rstrip().endswith(("/", "\\")):
            _fail("path_invalid", name)
        found_key = canonical_key(found, name)
        if any(keys_overlap(found_key, keys[other]) for other in ("paths.output_root", "paths.work_root", "paths.ledger_db",
                                                                  "paths.history_db")):
            _fail("credential_path_in_storage", name)
        if oauth_client is None and field_key == "oauth_client_file":
            oauth_client, client_key_text = found, found_key
        else:
            oauth_cache = found
            if oauth_client is not None and found_key == client_key_text:
                _fail("credential_paths_identical", name)

    # The shared activity folder (optional): an absolute folder that may be synchronised (that is its purpose) but is never one of the storage locations, and may not
    # hold a credential location. Only small text lines are written to it, one file per person and computer.
    activity_dir = None
    if paths.get("shared_activity_dir") is not None:
        activity_dir = _absolute(paths["shared_activity_dir"], "paths.shared_activity_dir")
        activity_key = canonical_key(activity_dir, "paths.shared_activity_dir")
        if any(keys_overlap(activity_key, keys[other]) for other in ("paths.output_root", "paths.work_root", "paths.ledger_db", "paths.history_db")):
            _fail("activity_dir_in_storage", "paths.shared_activity_dir")
        for credential in (oauth_client, oauth_cache):
            if credential is not None and keys_overlap(activity_key, canonical_key(credential, "paths.shared_activity_dir")):
                _fail("activity_dir_holds_credentials", "paths.shared_activity_dir")

    # The shared reports folder (optional): finished PDFs are copied here for teammates. Like the activity folder it may be synchronised but is never a storage
    # location, never inside the project, and never holds a credential location.
    reports_dir = None
    if paths.get("shared_reports_dir") is not None:
        reports_dir = _absolute(paths["shared_reports_dir"], "paths.shared_reports_dir")
        reports_key = canonical_key(reports_dir, "paths.shared_reports_dir")
        if any(keys_overlap(reports_key, keys[other]) for other in ("paths.output_root", "paths.work_root", "paths.ledger_db", "paths.history_db")):
            _fail("reports_dir_in_storage", "paths.shared_reports_dir")
        if keys_overlap(reports_key, project):
            _fail("reports_dir_in_project", "paths.shared_reports_dir")
        for credential in (oauth_client, oauth_cache):
            if credential is not None and keys_overlap(reports_key, canonical_key(credential, "paths.shared_reports_dir")):
                _fail("reports_dir_holds_credentials", "paths.shared_reports_dir")

    clients_config = _absolute(paths["clients_config"], "paths.clients_config") if paths.get("clients_config") is not None else \
        PROJECT_ROOT / "config" / "clients.yaml"
    sender_config = _absolute(paths["sender_config"], "paths.sender_config") if paths.get("sender_config") is not None else \
        PROJECT_ROOT / "config" / "jasper.yaml"
    logo = _absolute(render["logo"], "render.logo") if render.get("logo") is not None else PROJECT_ROOT / "assets" / "jasper-logo.png"

    fonts_raw = render.get("fonts")
    fonts = []
    if fonts_raw is not None:
        if not isinstance(fonts_raw, list) or not fonts_raw:
            _fail("fonts_invalid", "render.fonts")
        for position, item in enumerate(fonts_raw):
            where = f"render.fonts[{position}]"
            entry = _mapping(item, FONT_KEYS, where)
            label = entry.get("label")
            if not isinstance(label, str) or not _LABEL.fullmatch(label):
                _fail("font_label_invalid", f"{where}.label")
            weight = entry.get("weight", "400")
            if weight not in ("400", "700"):
                _fail("font_weight_invalid", f"{where}.weight")
            fonts.append(FontSetting(label, _absolute(entry.get("path"), f"{where}.path"), weight))
        if len({f.label for f in fonts}) != len(fonts):
            _fail("font_label_duplicate", "render.fonts")

    stage_raw = _mapping(execution.get("stage_timeouts"), STAGE_KEYS, "execution.stage_timeouts")
    stages = StageTimeouts(**{name: float(_number(stage_raw[name], f"execution.stage_timeouts.{name}", low=1, high=86400))
                              for name in stage_raw})
    ttl = float(_number(execution.get("lock_ttl_seconds", 600), "execution.lock_ttl_seconds", low=30, high=86400))
    heartbeat = float(_number(execution.get("heartbeat_interval_seconds", min(60.0, ttl / 4)),
                              "execution.heartbeat_interval_seconds", low=1, high=3600))
    if heartbeat > ttl / 3:  # a lease must survive at least two missed renewals
        _fail("heartbeat_interval_too_long", "execution.heartbeat_interval_seconds")
    delays = execution.get("retry_delays_seconds", (300, 900))
    if not isinstance(delays, (list, tuple)) or not all(
            isinstance(d, int) and not isinstance(d, bool) and 1 <= d <= 86400 for d in delays):
        _fail("retry_delays_invalid", "execution.retry_delays_seconds")

    return RunnerSettings(
        output_root=output_root, history_db_path=history, ledger_db_path=ledger, work_root=work_root,
        clients_config_path=clients_config, sender_config_path=sender_config, logo_path=logo, fonts=tuple(fonts),
        reporting_spreadsheet_id=_spreadsheet_id(books.get("reporting_spreadsheet_id"),
                                                 "workbooks.reporting_spreadsheet_id"),
        operational_spreadsheet_id=_spreadsheet_id(books.get("operational_spreadsheet_id"),
                                                   "workbooks.operational_spreadsheet_id"),
        lock_ttl_seconds=float(_number(execution.get("lock_ttl_seconds", 600), "execution.lock_ttl_seconds",
                                       low=30, high=86400)),
        inter_client_pause_seconds=float(_number(execution.get("inter_client_pause_seconds", 1),
                                                 "execution.inter_client_pause_seconds", low=0, high=3600)),
        client_timeout_seconds=float(_number(execution.get("client_timeout_seconds", 1500),
                                             "execution.client_timeout_seconds", low=10, high=86400)),
        batch_deadline_seconds=float(_number(execution.get("batch_deadline_seconds", 21600),
                                             "execution.batch_deadline_seconds", low=10, high=7 * 86400)),
        max_automatic_attempts=int(_number(execution.get("max_automatic_attempts", 3),
                                           "execution.max_automatic_attempts", low=1, high=10, integer=True)),
        retry_delays_seconds=tuple(delays), stage_timeouts=stages, heartbeat_interval_seconds=heartbeat,
        oauth_client_path=oauth_client, oauth_cache_path=oauth_cache, activity_dir=activity_dir,
        reports_dir=reports_dir)


def load_settings(path, *, environ=None) -> RunnerSettings:
    """Read a YAML settings file (size-limited, safe loader) and validate it."""
    file = Path(path)
    try:
        if file.stat().st_size > MAX_CONFIG_BYTES:
            _fail("file_too_large", "file")
        data = yaml.safe_load(file.read_text(encoding="utf-8"))
    except RunnerConfigError:
        raise
    except OSError:
        _fail("file_unreadable", "file")
    except (yaml.YAMLError, UnicodeDecodeError):
        _fail("file_invalid", "file")
    return parse_settings(data, environ=environ)
