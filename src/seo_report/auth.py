"""Google OAuth credentials for the reporting sources (R8.4.5).

Two entry points, deliberately separate:

  * `get_credentials()` is what the Sheets, Search Console and GA4 clients call. It is NON-INTERACTIVE: it loads the cached
    token, refreshes it if it has expired, and otherwise raises `AuthRequiredError` (a `contracts.StageError` with the
    category `auth_required`). It can never open a browser or wait for a person, so an unattended run (a scheduled task, the
    batch runner) fails fast with a structured, diagnosable outcome, and the batch stops as the execution contract says.
  * `run_setup()` is the only code that starts the interactive OAuth flow. It is called explicitly by a person
    (`python -m seo_report.auth setup`), refuses to run without an attended console, and is never reached from
    `get_credentials()` or from any runner module. `google_auth_oauthlib` is imported only inside it.

Failure codes (`AuthRequiredError.code`; the category is always `auth_required` unless stated), fixed and free of secrets:

  auth_token_missing          no cached token (setup has not been run on this machine/account)
  auth_credentials_unreadable the token file exists but cannot be read (permissions, locked)
  auth_credentials_invalid    the token file is malformed or incomplete
  auth_refresh_unavailable    the token expired and holds no refresh token
  auth_credentials_revoked    Google answered `invalid_grant` (revoked, expired for good, password changed)
  auth_refresh_failed         the refresh was refused for another reason (client disabled, consent screen, ...)
  auth_interactive_blocked    interactive sign-in was requested without an attended console
  auth_network_error          category `source_temporary`: the refresh could not reach Google (retry later)

Messages never contain token, client-secret or account values; the original exception stays reachable as `__cause__` for
a debugger, and only exception class names and the fixed codes are logged.

Scopes and the Google Cloud project are exactly those of the cached token's client; `SCOPES` is unchanged.

Credential storage, a design for LATER (nothing is moved, copied or rotated by this stage)
  * Today the token is `<project>/token.json` and the client secret `<project>/credentials/client_secret.json`: inside the
    project folder, which sits in a OneDrive folder on the author's machine. Both are git-ignored but are still synchronised.
  * Target: the interactive user's `%LOCALAPPDATA%\\JasperSEOReporting\\credentials\\` (outside OneDrive and the project),
    created with an ACL that grants Full Control only to that user (`icacls /inheritance:r /grant:r %USERNAME%:F`).
    The runner and the setup command must run as the SAME Windows account: DPAPI and the ACL are per account, so a Scheduled
    Task must be configured "run as" that user ("run whether the user is logged on or not" needs the stored password).
  * Better still, keep the refresh token in Windows Credential Manager (`keyring`, DPAPI-protected, per user) and only the
    non-secret client id/scopes in a file. This needs the `keyring` package, which this stage does not install.
  * A service account would avoid refresh and interactive consent, but the sources read Search Console and GA4 data that
    people have granted to a person's Google account; a service account must be added to each property as a user. That is a
    business/access decision (and changes the identity that appears in client properties), so it is NOT assumed here.
  * Token refresh: access tokens last about an hour; the refresh token is long-lived but is revoked by a password change,
    by removing the app, or after 7 days when the consent screen is in "Testing" status. Publishing the consent screen
    to "In production" (a Google Cloud setting, not changed here) removes the 7-day expiry; until then expect
    `auth_credentials_revoked` weekly and re-run setup.
  * The refreshed token is written back atomically (temporary file + replace); a failed write is logged and the valid
    in-memory credentials are still used for the run.
"""

import logging
import os
import re
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

from google.auth import exceptions as auth_exceptions
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials

from seo_report.contracts import StageError

LOGGER = logging.getLogger("seo_report.auth")

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets.readonly",
    "https://www.googleapis.com/auth/webmasters.readonly",
    "https://www.googleapis.com/auth/analytics.readonly",
]

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CLIENT_SECRET_PATH = PROJECT_ROOT / "credentials" / "client_secret.json"
TOKEN_PATH = PROJECT_ROOT / "token.json"


# --- where the files are (R8.6b) -------------------------------------------------------------------------------------------------
#
# The OAuth client file ("client secret") and the cached sign-in ("token") are located by `CredentialPaths`. With no explicit
# configuration the locations are exactly the module constants above (the migration-compatible fallback, inside the project). The
# runner settings can name other locations (`paths.oauth_client_file`, `paths.oauth_cache_file`); the attended setup command and
# normal reporting use the SAME `CredentialPaths`. Paths are validated here as text; files are never opened to validate them, and
# neither paths nor contents are ever put in an error message or a log record.

_VARIABLE = re.compile(r"%([A-Za-z_][A-Za-z0-9_]*)%")


@dataclass(frozen=True)
class CredentialPaths:
    client_file: Path
    token_file: Path
    source: str = "default"  # default | settings | mixed


def _credential_path(value, environ) -> Path:
    """An absolute, unambiguous file path from configuration text (with `%VARIABLE%` expanded from `environ`), or StageError."""
    if isinstance(value, bytes) or not isinstance(value, (str, os.PathLike)) or not str(value).strip():
        raise StageError("config_invalid", "credential_path_invalid")
    text = os.fspath(value)
    if "\x00" in text:
        raise StageError("config_invalid", "credential_path_invalid")
    missing = []

    def expand(match):
        found = environ.get(match.group(1))
        if not found:
            missing.append(match.group(1))
            return match.group(0)
        return found

    text = _VARIABLE.sub(expand, text)
    if missing or "%" in text or "$" in text:
        raise StageError("config_invalid", "credential_path_variable_unset")
    path = Path(text)
    if not path.is_absolute():
        raise StageError("config_invalid", "credential_path_not_absolute")
    if ".." in re.split(r"[\\/]+", text):
        raise StageError("config_invalid", "credential_path_ambiguous")
    if text.endswith(("/", "\\")) or not path.name or path.name in (".", ".."):
        raise StageError("config_invalid", "credential_path_invalid")  # a folder, not a file
    return path


def default_paths() -> CredentialPaths:
    """The fallback: the module constants as they are NOW (so tests and callers that replace them keep working)."""
    return CredentialPaths(Path(CLIENT_SECRET_PATH), Path(TOKEN_PATH), "default")


def resolve_paths(client_file=None, token_file=None, environ=None) -> CredentialPaths:
    """Explicit locations where given, the fallback for the rest. Raises `StageError` (category `config_invalid`) for a bad path."""
    environ = os.environ if environ is None else environ
    fallback = default_paths()
    client = _credential_path(client_file, environ) if client_file is not None else fallback.client_file
    token = _credential_path(token_file, environ) if token_file is not None else fallback.token_file
    given = (client_file is not None) + (token_file is not None)
    return CredentialPaths(client, token, {0: "default", 1: "mixed", 2: "settings"}[given])


def paths_from_settings(settings, environ=None) -> CredentialPaths:
    """The locations configured in `RunnerSettings` (`oauth_client_path`, `oauth_cache_path`), else the fallback."""
    return resolve_paths(getattr(settings, "oauth_client_path", None), getattr(settings, "oauth_cache_path", None), environ)


def path_status(path) -> str:
    """'present' | 'missing' | 'not_a_file' | 'unreadable': from file-system metadata only; the file is never opened."""
    try:
        info = os.stat(path)
    except (FileNotFoundError, NotADirectoryError):
        return "missing"
    except OSError:
        return "unreadable"
    import stat as _stat
    return "present" if _stat.S_ISREG(info.st_mode) else "not_a_file"


def describe_paths(paths: CredentialPaths) -> dict:
    """What can be shown to an operator without exposing anything: the origin of the configuration and the file states."""
    return {"configuration": paths.source, "client_file": path_status(paths.client_file), "token_file": path_status(paths.token_file),
            "outside_project": not _inside(paths.token_file, PROJECT_ROOT) and not _inside(paths.client_file, PROJECT_ROOT)}


def _inside(path, folder) -> bool:
    try:
        return Path(os.path.normcase(os.path.abspath(path))).is_relative_to(Path(os.path.normcase(os.path.abspath(folder))))
    except (ValueError, OSError):
        return False


class AuthRequiredError(StageError):
    """Credentials are missing or unusable and a person must act. Category `auth_required`; `code` is fixed."""

    def __init__(self, code: str):
        super().__init__("auth_required", code)


class AuthTemporaryError(StageError):
    """Google could not be reached to refresh the token. Category `source_temporary`: retry later, no person needed."""

    def __init__(self, code: str = "auth_network_error"):
        super().__init__("source_temporary", code)


def _write_token(path: Path, creds: Credentials) -> None:
    """Replace the token file atomically; the old file stays intact if anything fails."""
    fd, temporary = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(creds.to_json())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def _load(path: Path) -> Credentials:
    try:
        return Credentials.from_authorized_user_file(str(path), SCOPES)
    except (FileNotFoundError, NotADirectoryError) as error:
        raise AuthRequiredError("auth_token_missing") from error
    except PermissionError as error:
        raise AuthRequiredError("auth_credentials_unreadable") from error
    except (ValueError, KeyError, TypeError, AttributeError) as error:  # malformed JSON or shape, a missing field
        raise AuthRequiredError("auth_credentials_invalid") from error
    except OSError as error:
        raise AuthRequiredError("auth_credentials_unreadable") from error


def get_credentials(*, token_path=None, refresh_request=None, paths: CredentialPaths | None = None) -> Credentials:
    """Valid credentials from the cached token, refreshing it when needed. NEVER interactive.

    Raises `AuthRequiredError` when a person must act and `AuthTemporaryError` when only the network is at fault.
    The token location is, in order: `token_path` (tests), `paths.token_file` (the configured locations), else the fallback
    `TOKEN_PATH`. Callers that pass nothing behave exactly as before R8.6b."""
    return _credentials_and_outcome(token_path, refresh_request, paths)[0]


REFRESH_NOT_NEEDED = "not_needed"  # the cached credentials were already valid: no refresh was attempted and nothing was written
REFRESH_OK = "ok"  # the cached credentials needed a refresh and it succeeded (the refreshed token is then cached again)


def get_credentials_with_status(*, token_path=None, refresh_request=None, paths: CredentialPaths | None = None):
    """`(credentials, outcome)` with exactly the behaviour, parameters and errors of `get_credentials`. `outcome` is `REFRESH_NOT_NEEDED` when the cached
    credentials were valid and `REFRESH_OK` when a refresh was required and succeeded. A failed refresh raises, as always: it is never reported as an
    outcome. The outcome says nothing else and contains no token or secret."""
    return _credentials_and_outcome(token_path, refresh_request, paths)


def _credentials_and_outcome(token_path, refresh_request, paths):
    path = Path(token_path) if token_path is not None else (paths.token_file if paths is not None else Path(TOKEN_PATH))
    creds = _load(path)
    if creds.valid:
        return creds, REFRESH_NOT_NEEDED
    if not (creds.expired and creds.refresh_token):
        LOGGER.warning("authentication unusable: no refresh token")
        raise AuthRequiredError("auth_refresh_unavailable")
    try:
        creds.refresh(refresh_request if refresh_request is not None else Request())
    except auth_exceptions.RefreshError as error:
        revoked = any(isinstance(part, dict) and part.get("error") == "invalid_grant" for part in error.args[1:])
        code = "auth_credentials_revoked" if revoked else "auth_refresh_failed"
        LOGGER.warning("authentication refresh refused: %s (%s)", code, type(error).__name__)
        raise AuthRequiredError(code) from error
    except (auth_exceptions.TransportError, auth_exceptions.TimeoutError) as error:
        LOGGER.warning("authentication refresh unreachable (%s)", type(error).__name__)
        raise AuthTemporaryError() from error
    except auth_exceptions.GoogleAuthError as error:
        LOGGER.warning("authentication unusable (%s)", type(error).__name__)
        raise AuthRequiredError("auth_credentials_invalid") from error
    if not creds.valid:
        raise AuthRequiredError("auth_refresh_failed")
    try:
        _write_token(path, creds)
    except OSError as error:  # the credentials are valid for this run even if they cannot be cached
        LOGGER.warning("the refreshed token could not be saved (%s)", type(error).__name__)
    return creds, REFRESH_OK


# --- explicit, attended setup (the only interactive code) ----------------------------------------------------------------------

def _attended() -> bool:
    try:
        return bool(sys.stdin and sys.stdin.isatty() and sys.stdout and sys.stdout.isatty())
    except (AttributeError, ValueError):
        return False


def run_setup(*, client_secret_path=None, token_path=None, attended: bool | None = None, flow_factory=None,
              paths: CredentialPaths | None = None) -> Path:
    """Sign in once in a browser and cache the token. Call only from the setup command, by a person at the console.

    Refuses (`auth_interactive_blocked`) when no attended console exists, which is always the case for a scheduled task or
    the batch runner. `attended` and `flow_factory` exist for tests. Returns the token path; prints nothing secret."""
    if not (_attended() if attended is None else attended):
        raise AuthRequiredError("auth_interactive_blocked")
    configured = paths if paths is not None else default_paths()  # the same locations normal reporting reads
    secret = Path(client_secret_path) if client_secret_path is not None else configured.client_file
    path = Path(token_path) if token_path is not None else configured.token_file
    if flow_factory is None:
        from google_auth_oauthlib.flow import InstalledAppFlow  # imported here and nowhere else
        flow_factory = InstalledAppFlow.from_client_secrets_file
    try:
        flow = flow_factory(str(secret), SCOPES)
    except (FileNotFoundError, NotADirectoryError) as error:
        raise AuthRequiredError("auth_token_missing") from error
    except PermissionError as error:
        raise AuthRequiredError("auth_credentials_unreadable") from error
    except (ValueError, KeyError, TypeError) as error:
        raise AuthRequiredError("auth_credentials_invalid") from error
    creds = flow.run_local_server(port=0)
    path.parent.mkdir(parents=True, exist_ok=True)  # setup may be the first thing to use a newly configured credentials folder
    _write_token(path, creds)
    return path


def main(argv=None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args != ["setup"]:
        print("usage: python -m seo_report.auth setup   (interactive Google sign-in, run by a person)")
        return 2
    try:
        run_setup()
    except AuthRequiredError as error:
        print(f"setup failed: {error.code}")
        return 1
    print("setup complete: the token was cached")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
