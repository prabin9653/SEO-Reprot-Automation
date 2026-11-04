"""The identity of what the renderer draws: rendering fingerprint, render-input construction and bundle consistency (R7.1).

Three things live here, none of which touches finalization, records, sidecars or the filesystem beyond reading the
rendering files:

1. `rendering_fingerprint` / `measure_render_fingerprints`
   A deterministic fingerprint of everything that decides HOW the report is drawn, so a change to the CSS, a template, the
   layout code or the layout version changes the `template_version` of the render input and with it the render-input hash.
   It covers, by an explicit allowlist (never the whole project):
     * every regular file under `templates/report/` (names starting with "." and temporary or backup files such as
       `*~`, `*.tmp`, `*.bak`, `*.swp`, `*.pyc` are skipped)
     * the rendering sources `report_templates.py`, `report_charts.py`, `report_sections.py`, `report_document.py` and
       `keyword_pages.py` (chart generation, section assembly, document assembly, keyword pagination, template rendering)
     * the explicit `RENDER_LAYOUT_VERSION`
   Each file is identified by its path relative to its folder and the SHA-256 of its bytes (line endings normalised to
   "\\n" so a checkout with CRLF gives the same fingerprint). The label is "t-" plus the first 16 hex characters of the
   SHA-256 of the sorted entries, which satisfies the render input's `template_version` pattern. No absolute path,
   environment value or timestamp enters the fingerprint; the extraction, hash, finalization and credential files are
   not part of it.

2. `build_render_input(bundle, ...)`
   The single construction path from a `ReportBundle` to the immutable `ClientRenderInput`: it reuses
   `visual_extraction.extract_visuals` (visuals from `bundle.snapshot`) and `render_input.from_client_content` (content,
   sender, statements), records the snapshot identity as operational metadata (never hashed) and finishes with
   `check_matches_bundle`.

3. `check_matches_bundle(render_input, bundle)`
   Proves a render input was built from this bundle: client identity, reporting and comparison periods, keyword rows and
   footnote, sections, recommendations and the snapshot identity (content hash, client key, period). A mismatch raises
   `RenderIdentityError` with a fixed code naming the group that differs; the message never contains client data.

The hash of the render input is `render_input_hash.render_input_sha256` of the very object that is rendered
(`pdf_renderer.RenderOutcome.render_input_sha256`); nothing here builds a second copy.

Hashed-but-hidden fields (`HIDDEN_FIELDS`) are documented below with their purpose; none is removed here, because that
would change the hash format.
"""

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

from seo_report import asset_fingerprints as af
from seo_report import render_input as ri
from seo_report import report_run
from seo_report import report_templates as rt
from seo_report import visual_extraction as vx

RENDER_LAYOUT_VERSION = "1"
RENDERING_SOURCES = ("report_templates.py", "report_charts.py", "report_sections.py", "report_document.py",
                     "keyword_pages.py")
SOURCE_DIR = Path(__file__).resolve().parent
TEMPLATE_DIR = rt.TEMPLATE_DIR
_SKIPPED_SUFFIXES = (".tmp", ".bak", ".swp", ".pyc", ".orig", ".rej")
LABEL_PREFIX = "t-"
LABEL_HEX_CHARS = 16

# Fields that are part of the render-input hash but have no (or only an indirect, conditional) effect on the drawn pages.
# They stay in the hash (removing one would change the hash format and is proposed separately).
HIDDEN_FIELDS = {
    "ClientRenderInput.comparison_period": "no effect on the pages; records which previous period the comparison used "
                                           "(provenance of the historical statements); proposed for removal separately",
    "RenderPeriod (period)": "indirect: it sets the date range of the daily charts, so only a start before or an end "
                             "after the first and last record changes the drawing",
    "RenderStatement.key": "no effect on the pages; identifies a statement within its section (uniqueness check)",
    "RenderStatement.group": "no effect on the pages; the statement's grouping label from the history rules",
    "RenderRanking.text (kinds other than above_10)": "no effect: the text is drawn only for an above_10 ranking; "
                                                      "for a position or missing ranking it is carried but not shown",
    "RenderSender.website_url": "indirect: drawn only as the link target of the website line, and only for an http(s) "
                                "address (the PDF link annotation); an unsafe value is carried but not linked",
    "RenderSection.title (keywords)": "no effect: the keyword pages use a fixed heading; the other sections' titles are "
                                      "their page headings",
    "RenderSection.paragraphs (status not ok)": "no effect: paragraphs of a section whose status is not 'ok' are not "
                                                "drawn, by design",
    "RenderFingerprints (all)": "indirect: they identify the assets and the rendering code that draw the pages; they "
                                "are not drawn themselves",
}


class RenderIdentityError(ValueError):
    """The render identity cannot be established. `code` is fixed; `group` names what differs; no client data."""

    def __init__(self, code: str, group: str):
        super().__init__(f"{code}: {group}")
        self.code = code
        self.group = group


# --- rendering fingerprint ------------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class RenderingFingerprint:
    label: str  # "t-<16 hex>", the `template_version` of the render input
    digest: str  # the full SHA-256 the label is taken from
    layout_version: str
    files: tuple[tuple[str, str], ...]  # ("templates/<relative path>" or "source/<name>", SHA-256), sorted


def _file_digest(path: Path) -> str:
    try:
        data = path.read_bytes()
    except OSError:
        raise RenderIdentityError("rendering_file_unreadable", path.name) from None
    return hashlib.sha256(data.replace(b"\r\n", b"\n")).hexdigest()


def _template_files(folder: Path):
    if not folder.is_dir():
        raise RenderIdentityError("template_folder_missing", "templates")
    found = []
    for path in sorted(folder.rglob("*")):
        if not path.is_file() or any(part.startswith(".") or part == "__pycache__" for part in path.relative_to(folder).parts):
            continue
        if path.name.endswith("~") or path.name.endswith(_SKIPPED_SUFFIXES):
            continue
        found.append(path)
    if not found:
        raise RenderIdentityError("template_folder_empty", "templates")
    return found


def rendering_fingerprint(*, template_dir=None, source_dir=None, sources=None,
                          layout_version: str = RENDER_LAYOUT_VERSION) -> RenderingFingerprint:
    """The fingerprint of the rendering files (see the module docstring). The arguments exist for tests; the defaults
    are the project's `templates/report/` and the allowlisted rendering sources."""
    if not isinstance(layout_version, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,15}", layout_version):
        raise RenderIdentityError("layout_version_invalid", "layout_version")
    template_root = Path(template_dir) if template_dir is not None else TEMPLATE_DIR
    source_root = Path(source_dir) if source_dir is not None else SOURCE_DIR
    names = tuple(RENDERING_SOURCES if sources is None else sources)
    entries = [(f"templates/{p.relative_to(template_root).as_posix()}", _file_digest(p))
               for p in _template_files(template_root)]
    for name in names:
        path = source_root / name
        if not path.is_file():
            raise RenderIdentityError("rendering_source_missing", name)
        entries.append((f"source/{name}", _file_digest(path)))
    entries.sort()
    text = "\n".join([f"layout:{layout_version}"] + [f"{name}:{digest}" for name, digest in entries])
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return RenderingFingerprint(LABEL_PREFIX + digest[:LABEL_HEX_CHARS], digest, layout_version, tuple(entries))


def measure_render_fingerprints(*, logo_path, font_paths, page_size: str = "A4", **fingerprint_options) -> ri.RenderFingerprints:
    """`RenderFingerprints` with the logo and font hashes (`asset_fingerprints.measure_fingerprints`) and the rendering
    fingerprint as its `template_version`. `fingerprint_options` are passed to `rendering_fingerprint`."""
    label = rendering_fingerprint(**fingerprint_options).label
    return af.measure_fingerprints(template_version=label, logo_path=logo_path, font_paths=font_paths,
                                   page_size=page_size)


# --- bundle consistency ------------------------------------------------------------------------------------------------------------

def _sender_of(render_input: ri.ClientRenderInput):
    from seo_report.clients import Sender
    s = render_input.sender
    return Sender(name=s.name, title=s.title, address_lines=tuple(s.address_lines), phone=s.phone, email=s.email,
                  website_label=s.website_label, website_url=s.website_url)


def check_matches_bundle(render_input: ri.ClientRenderInput, bundle: report_run.ReportBundle) -> None:
    """Return None when `render_input` was built from `bundle`; otherwise raise `RenderIdentityError`.

    Compared: snapshot identity (operational content hash, client key, period), client name and website, reporting and
    comparison periods, keyword rows, ranking footnote, sections and recommendations. The expected values are produced by
    the same converter (`render_input.from_client_content`) that builds a render input, so there is no second copy of the
    conversion rules. Historical statements, visuals, sender and fingerprints do not come from the bundle's content and
    are not compared here.
    """
    if not isinstance(render_input, ri.ClientRenderInput):
        raise TypeError(f"render_input must be a ClientRenderInput, got {type(render_input).__name__}")
    if not isinstance(bundle, report_run.ReportBundle):
        raise TypeError(f"bundle must be a ReportBundle, got {type(bundle).__name__}")
    snapshot, operational = bundle.snapshot, render_input.operational
    if operational.snapshot_content_hash is None or operational.client_key is None:
        raise RenderIdentityError("snapshot_identity_missing", "operational")
    if (operational.snapshot_content_hash, operational.client_key) != (snapshot.content_hash, snapshot.client_key):
        raise RenderIdentityError("render_input_mismatch", "snapshot")
    expected = ri.from_client_content(
        bundle.client_content, sender=_sender_of(render_input), generation_date=render_input.generation_date,
        fingerprints=render_input.fingerprints)
    if (render_input.period.start.isoformat(), render_input.period.end.isoformat()) != (snapshot.period_start,
                                                                                         snapshot.period_end):
        raise RenderIdentityError("render_input_mismatch", "snapshot_period")
    for group, mine, theirs in (
            ("client", render_input.client, expected.client),
            ("periods", (render_input.period, render_input.comparison_period), (expected.period, expected.comparison_period)),
            ("keywords", render_input.keywords, expected.keywords),
            ("ranking_footnote", render_input.ranking_footnote, expected.ranking_footnote),
            ("sections", render_input.sections, expected.sections),
            ("recommendations", render_input.recommendations, expected.recommendations)):
        if mine != theirs:
            raise RenderIdentityError("render_input_mismatch", group)


def build_render_input(bundle: report_run.ReportBundle, *, sender, generation_date, history_insights,
                       fingerprints: ri.RenderFingerprints) -> ri.ClientRenderInput:
    """The immutable `ClientRenderInput` for `bundle`.

    `sender` is a `clients.Sender`; `generation_date` is the report's date, captured once by the caller (nothing here reads
    a clock); `history_insights` is the composed `HistoryInsights` (or None) whose client statements are displayed;
    `fingerprints` come from `measure_render_fingerprints`. Visuals are extracted from `bundle.snapshot` with all rows
    kept. The snapshot identity is recorded as operational metadata, which is never hashed. The result is checked with
    `check_matches_bundle` before it is returned.
    """
    if not isinstance(bundle, report_run.ReportBundle):
        raise TypeError(f"bundle must be a ReportBundle, got {type(bundle).__name__}")
    statements = None if history_insights is None else tuple(history_insights.client_statements)
    visuals = vx.extract_visuals(bundle.snapshot)
    operational = ri.RenderOperationalMetadata(client_key=bundle.snapshot.client_key,
                                               snapshot_content_hash=bundle.snapshot.content_hash)
    render_input = ri.from_client_content(
        bundle.client_content, sender=sender, generation_date=generation_date, fingerprints=fingerprints,
        historical_statements=statements, search_console=visuals.search_console, geography=visuals.geography,
        ga4=visuals.ga4, operational=operational)
    check_matches_bundle(render_input, bundle)
    return render_input
