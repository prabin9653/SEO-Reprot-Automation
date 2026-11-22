"""Direct Google Drive links for the dashboard's reports (optional, stage 7b).

The dashboard links each report to Google Drive. Without a file's Drive ID the best a link can do is a Drive search for its unique name. With the ID, a link opens the PDF at
once (`https://drive.google.com/file/d/<ID>/view`). This module finds the IDs with the Google Drive API.

Design rules
  * A SEPARATE sign-in. The reporting token (Sheets, Search Console, GA4) and the 7-day verification are untouched: the Drive permission lives in its own token file next to
    it (`token_drive.json`), created only by `seo setup drive`. Reporting never reads it and never needs it.
  * The permission is `drive.metadata.readonly`: file names and IDs, no file content, no changes. Google's lowest permission that can find a file the Drive app created.
  * Only the unique names of finished reports are looked up (`<client-key>_<period>.pdf`, validated characters only). A name that matches more than one file, or none, gets no
    direct link and falls back to the search link. Found IDs are cached in a local file so each report is looked up once.
  * Best effort by design: a missing sign-in, a disabled API, no network or a file Drive has not synchronised yet never fails a report or an export. It returns a short fixed
    note and the dashboard simply keeps the search link for that report.
"""

import json
import logging
import os
import re
import tempfile
from pathlib import Path

LOGGER = logging.getLogger("seo_report.drive_links")
DRIVE_SCOPES = ["https://www.googleapis.com/auth/drive.metadata.readonly"]
FILE_URL = "https://drive.google.com/file/d/{id}/view"
CACHE_NAME = "drive_file_ids.json"
_ID = re.compile(r"[A-Za-z0-9_-]{10,80}")
_NAME = re.compile(r"[a-z0-9][a-z0-9_-]{0,62}_\d{4}-\d{2}-\d{2}_\d{4}-\d{2}-\d{2}\.pdf")
MAX_LOOKUPS = 200

NOTE_NEEDS_SETUP = "drive_signin_missing"
NOTE_UNAVAILABLE = "drive_lookup_unavailable"


def valid_id(value) -> bool:
    return isinstance(value, str) and bool(_ID.fullmatch(value))


def file_url(file_id: str) -> str:
    if not valid_id(file_id):
        raise ValueError("invalid Drive file id")
    return FILE_URL.format(id=file_id)


def token_path(paths) -> Path:
    """The separate Drive token next to the reporting token (`token.json` -> `token_drive.json`)."""
    token = Path(paths.token_file)
    return token.with_name(f"{token.stem}_drive{token.suffix}")


def cache_path(settings) -> Path:
    """A local file in the per-device ledger folder (never synchronised, never in the shared folder)."""
    return Path(settings.ledger_db_path).parent / CACHE_NAME


def load_cache(path) -> dict:
    """Cached `file name -> Drive ID`, validated; anything unreadable or malformed is ignored."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {name: value for name, value in data.items() if isinstance(name, str) and _NAME.fullmatch(name) and valid_id(value)}


def _save_cache(path, cache: dict) -> None:
    path = Path(path)
    fd, temporary = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(cache, handle, sort_keys=True, indent=1)
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def build_service(credentials):
    from googleapiclient.discovery import build  # imported lazily, like the other Google clients
    return build("drive", "v3", credentials=credentials, cache_discovery=False)


def find_id(service, name: str):
    """The Drive ID of the single non-trashed PDF with this exact name, or None (not found, or more than one)."""
    if not _NAME.fullmatch(name):
        return None
    response = service.files().list(q=f"name = '{name}' and mimeType = 'application/pdf' and trashed = false", fields="files(id)", pageSize=3,
                                    spaces="drive").execute()
    files = response.get("files", []) if isinstance(response, dict) else []
    found = [item.get("id") for item in files if isinstance(item, dict) and valid_id(item.get("id"))]
    return found[0] if len(found) == 1 else None


def resolve(settings, paths, names, *, service_factory=None, credentials_getter=None):
    """`(ids, note)`: Drive IDs (`name -> id`) for as many of these unique file names as can be found, and None or a fixed note code saying why the rest could not be.
    Never raises. `service_factory` and `credentials_getter` exist for tests."""
    wanted = sorted({n for n in names if isinstance(n, str) and _NAME.fullmatch(n)})[:MAX_LOOKUPS]
    ids = {}
    try:
        cache_file = cache_path(settings)
        cache = load_cache(cache_file)
    except Exception:  # noqa: BLE001
        return ids, NOTE_UNAVAILABLE
    ids.update({n: cache[n] for n in wanted if n in cache})
    missing = [n for n in wanted if n not in ids]
    if not missing:
        return ids, None
    try:
        from seo_report import auth
        getter = credentials_getter or (lambda: auth.get_credentials_for_scopes(DRIVE_SCOPES, token_path=token_path(paths)))
        try:
            credentials = getter()
        except auth.AuthRequiredError:
            return ids, NOTE_NEEDS_SETUP
        service = (service_factory or build_service)(credentials)
        changed = False
        for name in missing:
            found = find_id(service, name)
            if found:
                ids[name] = found
                cache[name] = found
                changed = True
        if changed:
            _save_cache(cache_file, cache)
        return ids, None
    except Exception:  # noqa: BLE001 - the API may be disabled, the network down or Drive not yet synchronised: the search link stays
        LOGGER.warning("direct Drive links could not be looked up", extra={"fields": {"code": NOTE_UNAVAILABLE}})
        return ids, NOTE_UNAVAILABLE
