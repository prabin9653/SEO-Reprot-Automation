"""A shared reports folder for the team (stage 7): finished PDFs are COPIED there, and a read-only `dashboard.html` lists them.

Set `paths.shared_reports_dir` in the runner settings to a folder everyone can read (for example a Google Drive for desktop folder on the operator computer). Then:

    <shared>/<client-key>/<client-key>_<period>.pdf    a verified copy of each finished report (the name is unique in the whole folder)
    <shared>/dashboard.html               one read-only page: every client's latest report, who made it, when, and a link to the PDF

Rules
  * The original report stays in the output folder and is the source of truth. The copy is made to a temporary name, its SHA-256 is compared with the original, and only
    then is it renamed into place. A file that already exists is NEVER overwritten (identical -> "already there"; different -> reported as `differs`, left alone).
  * Only reports the ledger records as finished and audited are copied; nothing is ever deleted from either folder.
  * The dashboard is generated: it is rewritten (atomically) every time, and nothing in it comes from the shared folder. It holds client names and report dates, so the
    folder must be shared with named people only. It contains no script that contacts anything: one inline script (its hash is pinned by a content-security-policy)
    only shows times in the viewer's own time zone and filters the table.
  * Everything here is best effort when it follows a report run: a missing, offline or read-only folder never fails a report.
"""

import base64
import datetime as dt
import hashlib
import html
import logging
import os
import re
import shutil
import urllib.parse
from pathlib import Path

from seo_report import drive_links
from seo_report.drive_links import file_url

LOGGER = logging.getLogger("seo_report.shared_reports")
COPIED, SAME, DIFFERS, MISSING, FAILED = "copied", "already_there", "differs", "source_missing", "failed"
DASHBOARD_NAME = "dashboard.html"
_KEY = re.compile(r"[a-z0-9][a-z0-9_-]{0,62}")
_PERIOD = re.compile(r"\d{4}-\d{2}-\d{2}_\d{4}-\d{2}-\d{2}")
DRIVE_SEARCH = "https://drive.google.com/drive/search?q="


def pdf_name(client_key: str, period_id: str) -> str:
    """The copy's file name: unique across the whole shared folder (many clients share one period)."""
    return f"{client_key}_{period_id}.pdf"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def source_pdf(output_root, client_key: str, period_id: str) -> Path | None:
    """The finished report's PDF inside the output folder, or None (not there, not a plain file, or outside the folder)."""
    if not (_KEY.fullmatch(str(client_key)) and _PERIOD.fullmatch(str(period_id))):
        return None
    try:
        root = Path(output_root).resolve()
        path = root / "reports" / client_key / period_id / "report.pdf"
        if path.is_symlink() or not path.is_file():
            return None
        resolved = path.resolve()
        resolved.relative_to(root)
        return resolved
    except (OSError, ValueError):
        return None


def copy_report(shared_dir, output_root, client_key: str, period_id: str) -> str:
    """Copy one finished report into the shared folder. Returns COPIED, SAME, DIFFERS, MISSING or FAILED; never raises, never overwrites."""
    source = source_pdf(output_root, client_key, period_id)
    if source is None:
        return MISSING
    part = None
    try:
        folder = Path(shared_dir) / client_key
        target = folder / pdf_name(client_key, period_id)
        digest = _sha256(source)
        if target.exists():
            return SAME if target.is_file() and _sha256(target) == digest else DIFFERS
        folder.mkdir(parents=True, exist_ok=True)
        part = folder / f".{period_id}.part"
        shutil.copyfile(source, part)
        if _sha256(part) != digest:
            return FAILED
        os.rename(part, target)  # on Windows this fails (instead of replacing) if the target appeared in the meantime
        part = None
        return COPIED
    except Exception:  # noqa: BLE001 - best effort by design
        LOGGER.warning("a report could not be copied to the shared folder", extra={"fields": {"code": "shared_reports_copy_failed"}})
        return FAILED
    finally:
        if part is not None:
            try:
                part.unlink()
            except OSError:
                pass


def finished_reports(output_root) -> list[tuple[str, str]]:
    """(client_key, period_id) of every report folder that holds a report.pdf, sorted. Folder names are validated; nothing is opened."""
    found = []
    try:
        base = Path(output_root) / "reports"
        for client in sorted(p for p in base.iterdir() if p.is_dir() and _KEY.fullmatch(p.name)):
            for period in sorted(p for p in client.iterdir() if p.is_dir() and _PERIOD.fullmatch(p.name)):
                if (period / "report.pdf").is_file():
                    found.append((client.name, period.name))
    except OSError:
        pass
    return found


def utc_text(timestamp) -> str | None:
    """A ledger timestamp as `2026-11-17T20:45:00Z`, or None when it is missing or unreadable."""
    try:
        return dt.datetime.fromisoformat(timestamp).astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except (TypeError, ValueError):
        return None


def publish(settings, *, names: dict, describe, keys=None, now=None, dashboard: bool = True, link_resolver=None) -> dict:
    """Copy finished reports (all of them, or only these client keys) and rewrite the dashboard. `describe(client_key, period_id)` returns (finished_at, generated_by)
    for a report the ledger records as done, or None for anything else (such a report is neither copied nor listed). `link_resolver(file_names)` (optional) returns
    `(ids, note)`: Google Drive IDs by file name, so the dashboard can link straight to a PDF (see `drive_links`). Never raises."""
    shared = settings.reports_dir
    result = {"copied": [], "already_there": 0, "differs": [], "failed": [], "not_recorded": [], "dashboard": False, "folder": str(shared) if shared else None,
              "link_note": None, "direct_links": 0}
    if shared is None:
        return result
    entries = {}
    for key, period in finished_reports(settings.output_root):
        try:
            info = describe(key, period)
        except Exception:  # noqa: BLE001 - the ledger is a convenience here
            info = None
        if info is None:
            result["not_recorded"].append(f"{key}/{period}")
            continue
        if keys is None or key in keys:
            status = copy_report(shared, settings.output_root, key, period)
            if status == COPIED:
                result["copied"].append(f"{key}/{period}")
            elif status == SAME:
                result["already_there"] += 1
            elif status == DIFFERS:
                result["differs"].append(f"{key}/{period}")
            else:
                result["failed"].append(f"{key}/{period}")
        entries.setdefault(key, []).append({"period_id": period, "finished_at": utc_text(info[0]), "generated_by": info[1],
                                            "shared": (Path(shared) / key / pdf_name(key, period)).is_file()})
    if dashboard and link_resolver is not None:
        try:
            ids, result["link_note"] = link_resolver([pdf_name(k, e["period_id"]) for k, items in entries.items() for e in items if e["shared"]])
            for key, items in entries.items():
                for item in items:
                    item["file_id"] = ids.get(pdf_name(key, item["period_id"])) if item["shared"] else None
                    result["direct_links"] += 1 if item["file_id"] else 0
        except Exception:  # noqa: BLE001 - the search link is the fallback
            result["link_note"] = "drive_lookup_unavailable"
    if dashboard:
        result["dashboard"] = write_dashboard(shared, entries, names, now or dt.datetime.now(dt.timezone.utc))
    return result


# --- the dashboard ------------------------------------------------------------------------------------------------------------------------

_SCRIPT = """(function () {
  var fmt = { year: "numeric", month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", timeZoneName: "short" };
  document.querySelectorAll("time[datetime]").forEach(function (el) {
    var d = new Date(el.getAttribute("datetime"));
    if (!isNaN(d.getTime())) { try { el.textContent = d.toLocaleString(undefined, fmt); } catch (e) {} }
  });
  var box = document.getElementById("filter");
  box.addEventListener("input", function () {
    var q = box.value.trim().toLowerCase();
    document.querySelectorAll("tbody tr").forEach(function (row) {
      row.hidden = q !== "" && row.textContent.toLowerCase().indexOf(q) === -1;
    });
  });
})();"""
_SCRIPT_HASH = "sha256-" + base64.b64encode(hashlib.sha256(_SCRIPT.encode("utf-8")).digest()).decode("ascii")

_STYLE = """:root{--bg:#f6f7f9;--fg:#1d2733;--muted:#5d6b7a;--card:#fff;--line:#dde2e8;--accent:#1a5fb4}
@media (prefers-color-scheme:dark){:root{--bg:#14181d;--fg:#e6eaee;--muted:#98a4b1;--card:#1c2229;--line:#2e3742;--accent:#7db3ff}}
*{box-sizing:border-box}body{margin:0;padding:24px 16px;background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif}
main{max-width:1000px;margin:0 auto}h1{font-size:1.5rem;margin:0 0 4px}p.note{color:var(--muted);margin:4px 0}
input{width:100%;max-width:320px;margin:16px 0 12px;padding:8px 10px;border:1px solid var(--line);border-radius:6px;background:var(--card);color:var(--fg);font:inherit}
.wrap{overflow-x:auto;background:var(--card);border:1px solid var(--line);border-radius:8px}table{width:100%;border-collapse:collapse}
th,td{text-align:left;padding:9px 12px;border-bottom:1px solid var(--line);vertical-align:top}th{color:var(--muted);font-weight:600;font-size:.85rem}
tr:last-child td{border-bottom:0}a{color:var(--accent)}.small{color:var(--muted);font-size:.85rem}.gone{color:var(--muted)}"""


def _link(key: str, period: str, file_id=None) -> str:
    """A link that opens the report in Google Drive: straight to the PDF when its Drive ID is known, otherwise a search for its unique file name in the viewer's own
    signed-in Drive. Either works from a downloaded copy of this page and needs no Drive for desktop. Built only from validated characters."""
    url = file_url(file_id) if drive_links.valid_id(file_id) else DRIVE_SEARCH + urllib.parse.quote(pdf_name(key, period), safe="")
    return f'<a href="{url}" target="_blank" rel="noopener noreferrer">{html.escape(period.replace("_", " to "))}</a>'


def render_dashboard(entries: dict, names: dict, generated_at: dt.datetime) -> str:
    """The dashboard page as text. `entries` maps client key -> [{period_id, finished_at, generated_by, shared}]. Every dynamic value is escaped."""
    stamp = generated_at.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    rows = []
    for key in sorted(entries, key=lambda k: (str(names.get(k, k)).casefold(), k)):
        periods = sorted(entries[key], key=lambda e: e["period_id"], reverse=True)
        latest, earlier = periods[0], periods[1:]
        name = html.escape(str(names.get(key, key)))
        when = (f'<time datetime="{latest["finished_at"]}">{html.escape(latest["finished_at"].replace("T", " ").replace("Z", " UTC"))}</time>'
                if latest.get("finished_at") else '<span class="gone">unknown</span>')
        by = html.escape(str(latest["generated_by"])) if latest.get("generated_by") else '<span class="gone">unknown</span>'
        report = _link(key, latest["period_id"], latest.get("file_id")) if latest.get("shared") else '<span class="gone">not in this folder yet</span>'
        old = ", ".join(_link(key, e["period_id"], e.get("file_id")) for e in earlier if e.get("shared")) or '<span class="gone">none</span>'
        rows.append(f"<tr><td><strong>{name}</strong><div class=\"small\">{html.escape(key)}</div></td><td>{html.escape(latest['period_id'].replace('_', ' to '))}</td>"
                    f"<td>{when}</td><td>{by}</td><td>{report}</td><td class=\"small\">{old}</td></tr>")
    body = "\n".join(rows) if rows else '<tr><td colspan="6" class="gone">No finished reports yet.</td></tr>'
    csp = f"default-src 'none'; style-src 'unsafe-inline'; script-src '{_SCRIPT_HASH}'; img-src 'none'; base-uri 'none'; form-action 'none'"
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="{csp}">
<title>Jasper SEO Reports</title><style>{_STYLE}</style></head>
<body><main>
<h1>Jasper SEO Reports</h1>
<p class="note">Read-only list of finished reports. Updated <time datetime="{stamp}">{stamp.replace("T", " ").replace("Z", " UTC")}</time>. It changes only when the operator runs or exports reports.</p>
<p class="note">To ask for a new report, set the client's Status and its 28-day dates in the Google Sheet, then tell the operator. The links open each report in Google Drive: be signed in to the team's Google account in this browser.</p>
<input id="filter" type="search" placeholder="Filter by client or person" aria-label="Filter the list">
<div class="wrap"><table><thead><tr><th>Client</th><th>Latest period</th><th>Generated</th><th>By</th><th>Report</th><th>Earlier periods</th></tr></thead>
<tbody>
{body}
</tbody></table></div>
</main><script>{_SCRIPT}</script></body></html>
"""


def write_dashboard(shared_dir, entries: dict, names: dict, now) -> bool:
    """Write `dashboard.html` atomically (the one file here that is intentionally replaced). Returns whether it was written; never raises."""
    temporary = None
    try:
        folder = Path(shared_dir)
        folder.mkdir(parents=True, exist_ok=True)
        temporary = folder / f".{DASHBOARD_NAME}.part"
        temporary.write_text(render_dashboard(entries, names, now), encoding="utf-8", newline="\n")
        os.replace(temporary, folder / DASHBOARD_NAME)
        temporary = None
        return True
    except Exception:  # noqa: BLE001 - best effort by design
        LOGGER.warning("the dashboard could not be written to the shared folder", extra={"fields": {"code": "shared_dashboard_failed"}})
        return False
    finally:
        if temporary is not None:
            try:
                temporary.unlink()
            except OSError:
                pass
