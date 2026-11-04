"""Live-verification attestation (R8.7c): the only way to clear the production-gate requirement `live_google_not_verified`.

What it is
  A small record, protected with Windows DPAPI (current user, this machine), stored under `%LOCALAPPDATA%\\JasperSEOReporting\\attestation\\`, that says:
  "on <time>, on THIS machine, as THIS Windows user, genuine read-only Google calls succeeded for the OAuth refresh, the reporting workbook, Search Console
  and GA4 for these clients". The production gate (`preflight.production_gate`) accepts it, for seven days, and only as proof of that one requirement.

What it is NOT
  * It is never created by this stage: there is **no command, flag, environment variable or setting that creates or approves one**. Nothing in `src/`
    calls `issue_attestation` yet; the future live-verification workflow (R8.7e) will, after it has really performed the checks. A test keeps that list of
    callers explicit (`tests/test_attestation.py::test_only_the_allowed_modules_use_the_issuing_functions`).
  * It does not clear `credentials_not_relocated` or any other blocker, and it is not production readiness.
  * Evaluating it (`evaluate`) is read-only: no network, no browser, no write, no extension of the expiry.

Trust model, stated honestly
  DPAPI binds the protected bytes to the Windows user and machine and detects any modification, so other users, other machines, copied files and hand-edited
  files are rejected. It does **not** stop malicious code that runs as the same Windows user (such code could call `issue_attestation` itself). That residual
  risk is documented in docs/r8/security_and_authentication.md; `origin` ("live" only) and the evidence checks below are guards against mistakes and against
  accidental use of synthetic data, not against a hostile process with the user's rights.

Contents of the protected payload (schema `jasper.live-verification-attestation`, version 1; JSON, sorted keys)
  origin           "live" (anything else, notably "synthetic", is refused)
  issued_at / expires_at   UTC `YYYY-MM-DDTHH:MM:SS+00:00`; `expires_at - issued_at` is exactly `VALIDITY` (7 days)
  binding          SHA-256 over (MachineGuid, user SID) with a version label; the raw identifiers are never stored or shown
  credentials      16-hex fingerprints of the configured OAuth client-file path and cached-sign-in path (paths only; the files are never opened)
  workbook16       fingerprint of the reporting workbook id;  scopes16  fingerprint of `auth.SCOPES`
  checks           [{service, outcome, completed_at}] for oauth_refresh, sheets_read, search_console_read, ga4_read
  clients          [{client_key, gsc16, ga416}]: the clients whose Search Console and GA4 properties were read, as fingerprints
  nonce            random

Failure codes are fixed lower-case identifiers (`CODES`); no message contains a path, identifier, token or payload.
"""

import ctypes
import datetime as dt
import hashlib
import json
import os
import re
import secrets
import stat
import tempfile
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path

SCHEMA = "jasper.live-verification-attestation"
VERSION = 1
FILE_MAGIC = b"JASPERATT"
FILE_FORMAT = 1
FILE_NAME = "live_verification.attest"
FOLDER_NAME = "attestation"
MAX_FILE_BYTES = 64 * 1024
# Seven days: long enough for a monthly operator to verify, run and review within one working week, short enough that a revoked consent, a changed
# property or a token that expires after 7 days in OAuth "Testing" status cannot be hidden behind an old record. Fixed in code, not configurable.
VALIDITY = dt.timedelta(days=7)
MAX_SKEW = dt.timedelta(minutes=5)  # tolerated clock difference for timestamps that lie ahead of "now"
EVIDENCE_WINDOW = dt.timedelta(minutes=30)  # all checks must have completed within this span, and recently when the attestation is issued
REQUIRED_CHECKS = ("oauth_refresh", "sheets_read", "search_console_read", "ga4_read")
ORIGINS = ("live", "synthetic")
_TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\+00:00")
_FP = re.compile(r"[0-9a-f]{16}")

CODES = {
    "attestation_missing": "no verification attestation exists",
    "attestation_location_unsafe": "the attestation storage location is not acceptable (it must be a local per-user folder outside the project and OneDrive)",
    "attestation_inaccessible": "the attestation file cannot be read",
    "attestation_permissions_unsafe": "the attestation file or its folder can be accessed by other accounts",
    "attestation_corrupt": "the attestation file is malformed",
    "attestation_unsupported_version": "the attestation uses an unsupported schema or format version",
    "attestation_unprotect_failed": "the attestation was modified, or was protected by another Windows account or machine",
    "attestation_protection_unavailable": "Windows data protection (DPAPI) is not available",
    "attestation_identity_unavailable": "the Windows machine and account identity cannot be established",
    "attestation_not_live": "the attestation does not come from a live verification",
    "attestation_incomplete_evidence": "the attestation lacks successful evidence for a required Google service",
    "attestation_binding_mismatch": "the attestation belongs to a different Windows account or machine",
    "attestation_expired": "the attestation has expired; verify again",
    "attestation_future": "the attestation is dated in the future",
    "attestation_clock_suspect": "the system clock appears to have moved backwards; the attestation is not trusted",
    "attestation_validity_invalid": "the attestation validity period is not the permitted one",
    "attestation_workbook_changed": "the reporting workbook differs from the one that was verified",
    "attestation_credentials_changed": "the credential locations differ from the ones that were verified",
    "attestation_scopes_changed": "the requested Google scopes differ from the ones that were verified",
    "attestation_client_not_covered": "this client was not part of the verification",
    "attestation_property_changed": "a Search Console or GA4 property differs from the one that was verified",
    "attestation_check_failed": "the attestation could not be evaluated",
}


class AttestationError(Exception):
    """A fixed `code` from `CODES`; never carries a path, identifier or payload."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def fingerprint(value) -> str:
    """16 hex characters that identify a value without revealing it (used for workbook, property and path identities)."""
    return hashlib.sha256(b"jasper-identity-v1\x00" + str(value).encode("utf-8")).hexdigest()[:16]


def _stamp(moment: dt.datetime) -> str:
    return moment.astimezone(dt.timezone.utc).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%S+00:00")


def _parse_stamp(text) -> dt.datetime:
    if not isinstance(text, str) or not _TIMESTAMP.fullmatch(text):
        raise AttestationError("attestation_corrupt")
    try:
        return dt.datetime.strptime(text, "%Y-%m-%dT%H:%M:%S+00:00").replace(tzinfo=dt.timezone.utc)
    except ValueError:  # the right shape but not a real date
        raise AttestationError("attestation_corrupt") from None


# --- evidence --------------------------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class CheckResult:
    """One successful read-only check. `outcome` is "ok" (the call succeeded) or, for `oauth_refresh` only, "not_needed" (the cached token was still valid)."""

    service: str
    outcome: str
    completed_at: dt.datetime


@dataclass(frozen=True)
class ClientEvidence:
    """A client whose Search Console and GA4 properties were read. The raw property identifiers stay in memory; only fingerprints are stored."""

    client_key: str
    gsc_property: str
    ga4_property_id: str


@dataclass(frozen=True)
class VerificationEvidence:
    """What the future live-verification workflow (R8.7e) hands to `issue_attestation` after it has really performed the checks.

    Build it with `from_live_checks` only. `synthetic` exists for unit tests and produces evidence that `issue_attestation` always refuses."""

    origin: str
    checks: tuple
    workbook_id: str
    clients: tuple
    scopes: tuple

    @classmethod
    def from_live_checks(cls, *, checks, workbook_id, clients, scopes=None):
        from seo_report import auth
        return cls("live", tuple(checks), workbook_id, tuple(clients), tuple(sorted(scopes if scopes is not None else auth.SCOPES)))

    @classmethod
    def synthetic(cls, *, checks, workbook_id, clients, scopes=None):
        from seo_report import auth
        return cls("synthetic", tuple(checks), workbook_id, tuple(clients), tuple(sorted(scopes if scopes is not None else auth.SCOPES)))


def _validate_evidence(evidence, now: dt.datetime) -> None:
    from seo_report import auth
    from seo_report.client_config import ClientConfigError, validate_client_key
    if not isinstance(evidence, VerificationEvidence) or evidence.origin != "live":
        raise AttestationError("attestation_not_live")
    results = {}
    for check in evidence.checks:
        if (not isinstance(check, CheckResult) or not isinstance(check.completed_at, dt.datetime) or check.completed_at.tzinfo is None
                or check.completed_at.utcoffset() != dt.timedelta(0)):
            raise AttestationError("attestation_incomplete_evidence")
        allowed = ("ok", "not_needed") if check.service == "oauth_refresh" else ("ok",)
        if check.outcome not in allowed:
            raise AttestationError("attestation_incomplete_evidence")
        results[check.service] = check
    if not set(REQUIRED_CHECKS) <= set(results):
        raise AttestationError("attestation_incomplete_evidence")
    times = [results[name].completed_at for name in REQUIRED_CHECKS]
    if max(times) > now + MAX_SKEW or max(times) - min(times) > EVIDENCE_WINDOW or now - min(times) > EVIDENCE_WINDOW:
        raise AttestationError("attestation_incomplete_evidence")  # not fresh, not contemporaneous, or dated in the future
    if not isinstance(evidence.workbook_id, str) or not evidence.workbook_id.strip() or not evidence.clients:
        raise AttestationError("attestation_incomplete_evidence")
    seen = set()
    for client in evidence.clients:
        try:
            if not isinstance(client, ClientEvidence):
                raise ClientConfigError("client_key_invalid")
            validate_client_key(client.client_key, where="evidence")
        except ClientConfigError:
            raise AttestationError("attestation_incomplete_evidence") from None
        if client.client_key in seen or not str(client.gsc_property).strip() or not str(client.ga4_property_id).strip():
            raise AttestationError("attestation_incomplete_evidence")
        seen.add(client.client_key)
    if tuple(sorted(evidence.scopes)) != tuple(sorted(auth.SCOPES)):
        raise AttestationError("attestation_scopes_changed")


# --- Windows identity, DPAPI and ACLs (standard library only) ---------------------------------------------------------------------------

def _windows():
    if os.name != "nt":
        raise AttestationError("attestation_protection_unavailable")


def windows_identity() -> tuple:
    """(MachineGuid, user SID) of this machine and the current Windows account, or `AttestationError("attestation_identity_unavailable")`."""
    try:
        _windows()
        import winreg
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Cryptography", 0, winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as key:
            guid = str(winreg.QueryValueEx(key, "MachineGuid")[0]).strip().lower()
        sid = _current_user_sid()
    except AttestationError:
        raise
    except Exception:
        raise AttestationError("attestation_identity_unavailable") from None
    if not guid or not re.fullmatch(r"S-1-5-[0-9-]+", sid):
        raise AttestationError("attestation_identity_unavailable")
    return guid, sid


def _current_user_sid() -> str:
    advapi32, kernel32 = ctypes.WinDLL("advapi32", use_last_error=True), ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.LocalFree.argtypes, kernel32.LocalFree.restype = [ctypes.c_void_p], ctypes.c_void_p
    advapi32.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
    advapi32.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    advapi32.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.LPWSTR)]
    token = wintypes.HANDLE()
    if not advapi32.OpenProcessToken(kernel32.GetCurrentProcess(), 0x0008, ctypes.byref(token)):  # TOKEN_QUERY
        raise AttestationError("attestation_identity_unavailable")
    try:
        size = wintypes.DWORD()
        advapi32.GetTokenInformation(token, 1, None, 0, ctypes.byref(size))  # TokenUser: ask for the size
        buffer = ctypes.create_string_buffer(size.value)
        if not size.value or not advapi32.GetTokenInformation(token, 1, buffer, size, ctypes.byref(size)):
            raise AttestationError("attestation_identity_unavailable")
        sid_pointer = ctypes.c_void_p.from_buffer(buffer).value  # TOKEN_USER begins with the PSID
        text = wintypes.LPWSTR()
        if not advapi32.ConvertSidToStringSidW(sid_pointer, ctypes.byref(text)):
            raise AttestationError("attestation_identity_unavailable")
        try:
            return text.value
        finally:
            kernel32.LocalFree(ctypes.cast(text, ctypes.c_void_p))
    finally:
        kernel32.CloseHandle(token)


def binding_of(guid: str, sid: str) -> str:
    """The machine-and-account binding: a one-way digest, so neither identifier is stored, logged or shown."""
    return hashlib.sha256(b"jasper-attestation-binding-v1\x00" + guid.encode() + b"\x00" + sid.upper().encode()).hexdigest()[:32]


class _Blob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


class DpapiProtector:
    """Windows DPAPI (`CryptProtectData` / `CryptUnprotectData`) with the current user's key, no UI, and an application-specific entropy."""

    def _api(self):
        _windows()
        crypt32, kernel32 = ctypes.WinDLL("crypt32", use_last_error=True), ctypes.WinDLL("kernel32", use_last_error=True)
        pointer = ctypes.POINTER(_Blob)
        crypt32.CryptProtectData.argtypes = [pointer, wintypes.LPCWSTR, pointer, ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, pointer]
        crypt32.CryptUnprotectData.argtypes = [pointer, ctypes.c_void_p, pointer, ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, pointer]
        crypt32.CryptProtectData.restype = crypt32.CryptUnprotectData.restype = wintypes.BOOL
        kernel32.LocalFree.argtypes, kernel32.LocalFree.restype = [ctypes.c_void_p], ctypes.c_void_p
        return crypt32, kernel32

    @staticmethod
    def _blob(data: bytes):
        buffer = ctypes.create_string_buffer(data, len(data))
        return _Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char))), buffer  # keep `buffer` alive while the blob is used

    def _run(self, function: str, data: bytes, entropy: bytes, failure: str) -> bytes:
        crypt32, kernel32 = self._api()
        source, keep_source = self._blob(data)
        extra, keep_extra = self._blob(entropy)
        out = _Blob()
        call = getattr(crypt32, function)
        ok = call(ctypes.byref(source), None, ctypes.byref(extra), None, None, 0x1, ctypes.byref(out)) if function == "CryptUnprotectData" else \
            call(ctypes.byref(source), "jasper-seo-attestation", ctypes.byref(extra), None, None, 0x1, ctypes.byref(out))  # 0x1 = CRYPTPROTECT_UI_FORBIDDEN
        del keep_source, keep_extra
        if not ok:
            raise AttestationError(failure)
        try:
            return ctypes.string_at(out.pbData, out.cbData)
        finally:
            kernel32.LocalFree(ctypes.cast(out.pbData, ctypes.c_void_p))

    def protect(self, data: bytes, entropy: bytes) -> bytes:
        return self._run("CryptProtectData", data, entropy, "attestation_protection_unavailable")

    def unprotect(self, blob: bytes, entropy: bytes) -> bytes:
        return self._run("CryptUnprotectData", blob, entropy, "attestation_unprotect_failed")


def _entropy(binding: str) -> bytes:
    return b"jasper-seo-attestation-v1\x00" + binding.encode("ascii")



def read_sddl(path) -> str:
    """The owner and DACL of `path` as an SDDL string (Windows security APIs; nothing is changed)."""
    _windows()
    advapi32, kernel32 = ctypes.WinDLL("advapi32", use_last_error=True), ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.LocalFree.argtypes, kernel32.LocalFree.restype = [ctypes.c_void_p], ctypes.c_void_p
    advapi32.GetNamedSecurityInfoW.argtypes = [wintypes.LPCWSTR, ctypes.c_int, wintypes.DWORD] + [ctypes.c_void_p] * 5
    advapi32.GetNamedSecurityInfoW.restype = wintypes.DWORD
    advapi32.ConvertSecurityDescriptorToStringSecurityDescriptorW.argtypes = [ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD,
                                                                              ctypes.POINTER(wintypes.LPWSTR), ctypes.c_void_p]
    descriptor = ctypes.c_void_p()
    if advapi32.GetNamedSecurityInfoW(os.fspath(path), 1, 0x1 | 0x4, None, None, None, None, ctypes.byref(descriptor)) != 0:  # OWNER | DACL
        raise AttestationError("attestation_inaccessible")
    try:
        text = wintypes.LPWSTR()
        if not advapi32.ConvertSecurityDescriptorToStringSecurityDescriptorW(descriptor, 1, 0x1 | 0x4, ctypes.byref(text), None):
            raise AttestationError("attestation_inaccessible")
        try:
            return text.value
        finally:
            kernel32.LocalFree(ctypes.cast(text, ctypes.c_void_p))
    finally:
        kernel32.LocalFree(descriptor)


def apply_private_acl(path, sid: str) -> None:
    """Replace the DACL of `path` (a folder) with a protected one that grants full control to `sid` and SYSTEM only, inherited by new files."""
    _windows()
    advapi32, kernel32 = ctypes.WinDLL("advapi32", use_last_error=True), ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.LocalFree.argtypes, kernel32.LocalFree.restype = [ctypes.c_void_p], ctypes.c_void_p
    advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_void_p, ctypes.c_void_p]
    advapi32.GetSecurityDescriptorDacl.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.BOOL), ctypes.POINTER(ctypes.c_void_p),
                                                   ctypes.POINTER(wintypes.BOOL)]
    advapi32.SetNamedSecurityInfoW.argtypes = [wintypes.LPCWSTR, ctypes.c_int, wintypes.DWORD] + [ctypes.c_void_p] * 4
    advapi32.SetNamedSecurityInfoW.restype = wintypes.DWORD
    descriptor = ctypes.c_void_p()
    if not advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW(f"D:P(A;OICI;FA;;;{sid})(A;OICI;FA;;;SY)", 1, ctypes.byref(descriptor), None):
        raise AttestationError("attestation_permissions_unsafe")
    try:
        present, defaulted, dacl = wintypes.BOOL(), wintypes.BOOL(), ctypes.c_void_p()
        if not advapi32.GetSecurityDescriptorDacl(descriptor, ctypes.byref(present), ctypes.byref(dacl), ctypes.byref(defaulted)) or not present:
            raise AttestationError("attestation_permissions_unsafe")
        if advapi32.SetNamedSecurityInfoW(os.fspath(path), 1, 0x4 | 0x80000000, None, None, dacl, None) != 0:  # DACL | PROTECTED_DACL
            raise AttestationError("attestation_permissions_unsafe")
    finally:
        kernel32.LocalFree(descriptor)


_PRIVATE_TRUSTEES = {"SY", "S-1-5-18", "BA", "S-1-5-32-544", "CO", "S-1-3-0", "OW", "S-1-3-4"}
_OWNER_TRUSTEES = {"SY", "S-1-5-18", "BA", "S-1-5-32-544"}
# The ACE types the security design permits in a DACL. Everything else (conditional XA/XD, callback, resource-attribute, scoped-policy, audit,
# alarm, mandatory-label or any unknown type) is treated as UNSAFE: an entry that is not understood must never be read as harmless.
_ALLOW_ACE_TYPES = frozenset({"A", "OA"})  # access allowed / object access allowed: the trustee is checked
_DENY_ACE_TYPES = frozenset({"D", "OD"})  # access denied: can only remove access, ignored
_DACL_FLAGS = re.compile(r"(?:P|AI|AR)*")
_ACE_LIST = re.compile(r"(?:\([^()]*\))*")


def _split_sddl(text):
    """{'O': owner, 'G': group, 'D': dacl, 'S': sacl} from an SDDL string, looking for the section markers only outside parentheses; None when the
    text is malformed (unbalanced parentheses, text before the first section, a repeated section)."""
    depth, marks = 0, []
    for index, char in enumerate(text):
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth < 0:
                return None
        elif depth == 0 and char in "OGDS" and text[index + 1:index + 2] == ":":
            marks.append((char, index))
    if depth != 0 or not marks or marks[0][1] != 0:
        return None
    parts = {}
    for number, (char, index) in enumerate(marks):
        end = marks[number + 1][1] if number + 1 < len(marks) else len(text)
        if char in parts:
            return None
        parts[char] = text[index + 2:end]
    return parts


def unsafe_acl(sddl: str, user_sid: str) -> bool:
    """True unless the owner and DACL are positively understood and give access only to the user, SYSTEM and Administrators. Fails closed: a missing
    DACL (which means "everyone"), a malformed string, an ACE that is not exactly six fields, a conditional or unknown ACE type, an allow entry for
    any other trustee (or with no trustee) are all unsafe. Deny entries are ignored (they can only remove access)."""
    if not isinstance(sddl, str) or not sddl:
        return True
    parts = _split_sddl(sddl)
    if parts is None or "D" not in parts or "O" not in parts:
        return True
    user = str(user_sid).upper()
    if parts["O"].strip().upper() not in _OWNER_TRUSTEES | {user}:
        return True
    allowed = _PRIVATE_TRUSTEES | {user}
    flags = _DACL_FLAGS.match(parts["D"])
    entries = parts["D"][flags.end():]
    if not _ACE_LIST.fullmatch(entries):  # also rejects conditional ACEs, whose condition contains parentheses, and stray text
        return True
    for entry in re.findall(r"\(([^()]*)\)", entries):
        fields = entry.split(";")
        if len(fields) != 6:
            return True
        kind, trustee = fields[0].strip().upper(), fields[5].strip().upper()
        if kind in _ALLOW_ACE_TYPES:
            if trustee not in allowed:
                return True
        elif kind not in _DENY_ACE_TYPES:
            return True
    return False


# --- storage ---------------------------------------------------------------------------------------------------------------------

def store_path(environ=None) -> Path:
    """The attestation file: `%LOCALAPPDATA%\\JasperSEOReporting\\attestation\\live_verification.attest`, after location checks. Raises `attestation_location_unsafe`."""
    from seo_report import runner_config as rc
    environ = os.environ if environ is None else environ
    if not environ.get("LOCALAPPDATA"):
        raise AttestationError("attestation_location_unsafe")  # never fall back to a home-relative or project-relative folder
    path = rc.default_local_base(environ) / FOLDER_NAME / FILE_NAME
    try:
        canonical = rc.canonical_path(path)
        if os.path.normcase(str(canonical)) != os.path.normcase(os.path.abspath(path)):
            raise AttestationError("attestation_location_unsafe")  # reached through a link or alias
        if rc.keys_overlap(rc.canonical_key(path), rc.canonical_key(rc.PROJECT_ROOT)):
            raise AttestationError("attestation_location_unsafe")
        if rc.in_cloud_sync_folder(canonical.parent, environ):
            raise AttestationError("attestation_location_unsafe")
        rc.require_local_database(canonical)  # local fixed drive (the same rule as the databases)
    except AttestationError:
        raise
    except Exception:
        raise AttestationError("attestation_location_unsafe") from None
    return path


# --- verdict ---------------------------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Verdict:
    """The outcome of evaluating the stored attestation. `status`: valid | missing | invalid. Contains no payload and no identifier."""

    status: str
    code: str
    issued_at: str | None = None
    expires_at: str | None = None
    age_seconds: int | None = None
    clients: tuple = ()  # ((client_key, gsc16, ga416), ...)

    @property
    def valid(self) -> bool:
        return self.status == "valid"

    def public(self) -> dict:
        return {"status": self.status, "code": self.code, "issued_at": self.issued_at, "expires_at": self.expires_at, "age_seconds": self.age_seconds,
                "clients": [c[0] for c in self.clients]}


def _invalid(code: str) -> Verdict:
    return Verdict("invalid", code)


def evaluate(settings, environ=None, *, now=None, protector=None, identity=None, acl_reader=None) -> Verdict:
    """Read and fully validate the stored attestation against the current machine, account, time and configuration. Read-only; never raises (an
    unexpected problem is `attestation_check_failed`). `protector`, `identity` and `acl_reader` are replaceable by tests only."""
    try:
        return _evaluate(settings, environ, now, protector, identity, acl_reader)
    except AttestationError as error:
        return _invalid(error.code)
    except Exception:
        return _invalid("attestation_check_failed")


def _evaluate(settings, environ, now, protector, identity, acl_reader) -> Verdict:
    from seo_report import auth
    environ = os.environ if environ is None else environ
    now = (now or dt.datetime.now(dt.timezone.utc)).astimezone(dt.timezone.utc)
    if settings is None:
        raise AttestationError("attestation_check_failed")
    path = store_path(environ)
    try:
        info = os.lstat(path)
    except (FileNotFoundError, NotADirectoryError):
        return Verdict("missing", "attestation_missing")
    except OSError:
        raise AttestationError("attestation_inaccessible") from None
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:  # link, special file, reparse point
        raise AttestationError("attestation_location_unsafe")
    if info.st_size < len(FILE_MAGIC) + 2 or info.st_size > MAX_FILE_BYTES:
        raise AttestationError("attestation_corrupt")
    guid, sid = (identity or windows_identity)()
    reader = acl_reader or read_sddl
    for target in (path, path.parent):
        if unsafe_acl(reader(target), sid):
            raise AttestationError("attestation_permissions_unsafe")
    if dt.datetime.fromtimestamp(info.st_mtime, dt.timezone.utc) > now + MAX_SKEW:
        raise AttestationError("attestation_clock_suspect")  # the file is newer than "now": the clock has moved back
    try:
        with open(path, "rb") as handle:
            raw = handle.read(MAX_FILE_BYTES + 1)
    except OSError:
        raise AttestationError("attestation_inaccessible") from None
    if len(raw) > MAX_FILE_BYTES or not raw.startswith(FILE_MAGIC):
        raise AttestationError("attestation_corrupt")
    if raw[len(FILE_MAGIC)] != FILE_FORMAT:
        raise AttestationError("attestation_unsupported_version")
    binding = binding_of(guid, sid)
    plain = (protector or DpapiProtector()).unprotect(raw[len(FILE_MAGIC) + 1:], _entropy(binding))
    try:
        payload = json.loads(plain.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise AttestationError("attestation_corrupt") from None
    if not isinstance(payload, dict):
        raise AttestationError("attestation_corrupt")
    if payload.get("schema") != SCHEMA or payload.get("version") != VERSION:
        raise AttestationError("attestation_unsupported_version")
    if payload.get("origin") != "live":
        raise AttestationError("attestation_not_live")
    if payload.get("binding") != binding:
        raise AttestationError("attestation_binding_mismatch")
    issued, expires = _parse_stamp(payload.get("issued_at")), _parse_stamp(payload.get("expires_at"))
    if expires - issued != VALIDITY:
        raise AttestationError("attestation_validity_invalid")
    if issued > now + MAX_SKEW:
        raise AttestationError("attestation_future")
    if now >= expires:
        raise AttestationError("attestation_expired")
    checks = payload.get("checks")
    done = {c.get("service"): c.get("outcome") for c in checks if isinstance(c, dict)} if isinstance(checks, list) else {}
    if not all(done.get(name) in (("ok", "not_needed") if name == "oauth_refresh" else ("ok",)) for name in REQUIRED_CHECKS):
        raise AttestationError("attestation_incomplete_evidence")
    clients = payload.get("clients")
    if not isinstance(clients, list) or not clients:
        raise AttestationError("attestation_incomplete_evidence")
    covered = []
    for entry in clients:
        if not (isinstance(entry, dict) and isinstance(entry.get("client_key"), str) and _FP.fullmatch(str(entry.get("gsc16"))) and
                _FP.fullmatch(str(entry.get("ga416")))):
            raise AttestationError("attestation_corrupt")
        covered.append((entry["client_key"], entry["gsc16"], entry["ga416"]))
    if payload.get("workbook16") != fingerprint(getattr(settings, "reporting_spreadsheet_id", None) or ""):
        raise AttestationError("attestation_workbook_changed")
    paths = auth.paths_from_settings(settings, environ)
    credentials = payload.get("credentials") if isinstance(payload.get("credentials"), dict) else {}
    if credentials != _credential_identity(paths):
        raise AttestationError("attestation_credentials_changed")
    if payload.get("scopes16") != _scopes_identity():
        raise AttestationError("attestation_scopes_changed")
    return Verdict("valid", "attestation_valid", _stamp(issued), _stamp(expires), max(0, int((now - issued).total_seconds())), tuple(sorted(covered)))


def _credential_identity(paths) -> dict:
    return {"client_file16": fingerprint(os.path.normcase(os.path.abspath(paths.client_file))),
            "cache_file16": fingerprint(os.path.normcase(os.path.abspath(paths.token_file)))}


def _scopes_identity() -> str:
    from seo_report import auth
    return fingerprint("\n".join(sorted(auth.SCOPES)))


def client_coverage_code(verdict: Verdict, client_key: str, gsc_property, ga4_property_id) -> str | None:
    """None when the verified clients include `client_key` with these exact Search Console and GA4 properties; otherwise a fixed blocker code."""
    if not verdict.valid:
        return verdict.code if verdict.status == "invalid" else "attestation_missing"
    for key, gsc16, ga416 in verdict.clients:
        if key == client_key:
            if gsc16 == fingerprint(gsc_property) and ga416 == fingerprint(ga4_property_id):
                return None
            return "attestation_property_changed"
    return "attestation_client_not_covered"


# --- issuing (called ONLY by the future live-verification workflow; no command, flag, variable or setting reaches it) ---------------------------

def issue_attestation(evidence, settings, *, environ=None, now=None, protector=None, identity=None, acl_applier=None, acl_reader=None) -> Verdict:
    """Validate genuine live evidence and replace the stored attestation atomically. Returns the verdict of reading it back.

    Refuses synthetic or incomplete evidence, evidence for a different workbook, an unsafe storage location and any identity problem. Never opens a credential
    file, never contacts Google. A new call always writes a fresh attestation with a fresh seven-day validity (expiry is never extended in place)."""
    environ = os.environ if environ is None else environ
    now = (now or dt.datetime.now(dt.timezone.utc)).astimezone(dt.timezone.utc)
    _validate_evidence(evidence, now)
    if evidence.workbook_id != getattr(settings, "reporting_spreadsheet_id", None):
        raise AttestationError("attestation_workbook_changed")
    from seo_report import auth
    path = store_path(environ)
    guid, sid = (identity or windows_identity)()
    binding = binding_of(guid, sid)
    payload = {
        "schema": SCHEMA, "version": VERSION, "origin": "live", "issued_at": _stamp(now), "expires_at": _stamp(now + VALIDITY), "binding": binding,
        "credentials": _credential_identity(auth.paths_from_settings(settings, environ)), "workbook16": fingerprint(evidence.workbook_id),
        "scopes16": _scopes_identity(),
        "checks": [{"service": c.service, "outcome": c.outcome, "completed_at": _stamp(c.completed_at)} for c in sorted(evidence.checks, key=lambda c: c.service)],
        "clients": [{"client_key": c.client_key, "gsc16": fingerprint(c.gsc_property), "ga416": fingerprint(c.ga4_property_id)}
                    for c in sorted(evidence.clients, key=lambda c: c.client_key)],
        "nonce": secrets.token_hex(16)}
    blob = (protector or DpapiProtector()).protect(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8"), _entropy(binding))
    folder = path.parent
    folder.mkdir(parents=True, exist_ok=True)
    (acl_applier or apply_private_acl)(folder, sid)
    handle, temporary = tempfile.mkstemp(dir=str(folder), prefix=FILE_NAME + ".", suffix=".tmp")
    try:
        with os.fdopen(handle, "wb") as out:
            out.write(FILE_MAGIC + bytes([FILE_FORMAT]) + blob)
            out.flush()
            os.fsync(out.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise
    verdict = evaluate(settings, environ, now=now, protector=protector, identity=identity, acl_reader=acl_reader)
    if not verdict.valid:
        raise AttestationError(verdict.code)
    return verdict
