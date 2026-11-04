"""Rendering the report HTML to a PDF with the installed Chromium, and measuring what the browser really laid out (R6).

`render_report_pdf(render_input, workdir=..., logo_path=..., fonts=...)` takes a `ClientRenderInput` and renders the
complete report (cover, keyword pages, Search Console, geography, GA4, recommendations, contact):

  1. `workdir` must be an existing, EMPTY folder outside the project tree (the caller owns it; the tests use a
     temporary folder). All generated files go there and nowhere else
  2. the logo and the font files are COPIED into it (the originals are only read); their SHA-256 values must equal the
     fingerprints in the render input, otherwise nothing is rendered
  3. the HTML is the R5 document with relative links to the copies; it is written without overwriting anything
  4. the installed Playwright/Chromium opens it from disk with every request except local files and data URLs aborted;
     the page is measured (`MEASURE_SCRIPT`), the PDF is produced, and the fonts the browser actually used are read
     from the browser itself (`CSS.getPlatformFontsForNode`)
  5. the creation and modification dates are replaced by a fixed value from the generation date (same length, offsets
     intact: the R0 approach), so the same input renders to the same bytes
  6. the PDF is inspected and the measurements are analysed (`pdf_validation`); problems are returned as
     `LayoutIssue`s. Nothing is shrunk, hidden, truncated or moved to make content fit

No network, Google API, database, finalization or project output folder is involved. The result holds paths inside
the work folder; it is not part of the finalization record.
"""

import datetime as dt
import hashlib
import html as html_escape
import os
import re
import threading
import time
import shutil
from dataclasses import dataclass
from pathlib import Path

from seo_report import asset_fingerprints as af
from seo_report import keyword_pages as kp
from seo_report import pdf_validation as pv
from seo_report import render_input as ri
from seo_report import report_document as rd
from seo_report import report_templates as rt
from seo_report.render_input_hash import render_input_sha256

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DOCUMENT_NAME = "report.html"
RAW_PDF_NAME = "report.raw.pdf"
PDF_NAME = "report.pdf"
EXPECTED_FONT_FAMILY = "DejaVu Sans"


class PdfRenderError(RuntimeError):
    """Rendering could not start or finish. `code` is fixed; the message holds no client text."""

    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code


DEFAULT_TIMEOUT_SECONDS = 300.0  # the hard limit for one render, browser start to browser shutdown
SHUTDOWN_GRACE_SECONDS = 15.0  # the extra limit for closing the browser once the work is done
PHASES = ("launch", "navigation", "evaluation", "pdf", "shutdown")


class RenderTimeoutError(RuntimeError):
    """The browser did not finish within the limit. `phase` says where (`PHASES`); no client text is included.

    Deliberately NOT a `PdfRenderError`: the draft builder turns those into a plain `render_failed`, whereas this one
    reaches the render service with its phase. It is raised before any PDF is handed over, so nothing is promoted."""

    def __init__(self, phase: str):
        super().__init__(f"render_timeout: {phase}")
        self.code = "render_timeout"
        self.phase = phase


class _Watchdog:
    """The hard timeout. ONE daemon thread per render, started before the browser and always joined afterwards.

    It sleeps until the current deadline; if the deadline passes it runs `kill`, which ends the browser processes that this
    render started, so the blocked Playwright call in the main thread fails instead of hanging. The main thread moves the
    deadline (`arm`) between phases and ends the thread with `cancel` in a `finally`. The thread touches nothing else."""

    def __init__(self, seconds: float, kill):
        self._cond = threading.Condition()
        self._deadline = time.monotonic() + seconds
        self._kill, self._done, self.fired = kill, False, False
        self._thread = threading.Thread(target=self._run, name="render-watchdog", daemon=True)

    def start(self):
        self._thread.start()

    def arm(self, seconds: float):
        with self._cond:
            self._deadline = time.monotonic() + seconds
            self._cond.notify_all()

    def _run(self):
        with self._cond:
            while not self._done:
                remaining = self._deadline - time.monotonic()
                if remaining > 0:
                    self._cond.wait(remaining)
                    continue
                self.fired = True
                break
        if self.fired:
            try:
                self._kill()
            except Exception:  # the kill is best effort; the main thread still reports the timeout
                pass

    def cancel(self):
        with self._cond:
            self._done = True
            self._cond.notify_all()
        self._thread.join(timeout=10)


def _windows_process_api():
    """kernel32 with explicit argument and return types (64-bit handles are not `int`), or None off Windows."""
    if os.name != "nt":
        return None
    import ctypes
    from ctypes import wintypes
    api = ctypes.WinDLL("kernel32", use_last_error=True)
    api.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    api.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    api.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.c_void_p]
    api.Process32FirstW.restype = wintypes.BOOL
    api.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.c_void_p]
    api.Process32NextW.restype = wintypes.BOOL
    api.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    api.OpenProcess.restype = wintypes.HANDLE
    api.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    api.TerminateProcess.restype = wintypes.BOOL
    api.CloseHandle.argtypes = [wintypes.HANDLE]
    api.CloseHandle.restype = wintypes.BOOL
    return api


def _process_parents() -> dict[int, int]:
    """{pid: parent pid} for every process (Windows toolhelp snapshot); empty when it cannot be taken."""
    api = _windows_process_api()
    if api is None:
        return {}
    import ctypes
    from ctypes import wintypes

    class ProcessEntry(ctypes.Structure):
        _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD), ("th32ProcessID", wintypes.DWORD),
                    ("th32DefaultHeapID", ctypes.c_size_t), ("th32ModuleID", wintypes.DWORD),
                    ("cntThreads", wintypes.DWORD), ("th32ParentProcessID", wintypes.DWORD),
                    ("pcPriClassBase", wintypes.LONG), ("dwFlags", wintypes.DWORD), ("szExeFile", wintypes.WCHAR * 260)]

    snapshot = api.CreateToolhelp32Snapshot(0x2, 0)  # TH32CS_SNAPPROCESS
    if not snapshot or snapshot == wintypes.HANDLE(-1).value:
        return {}
    parents = {}
    try:
        entry = ProcessEntry()
        entry.dwSize = ctypes.sizeof(ProcessEntry)
        more = api.Process32FirstW(snapshot, ctypes.byref(entry))
        while more:
            parents[int(entry.th32ProcessID)] = int(entry.th32ParentProcessID)
            more = api.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        api.CloseHandle(snapshot)
    return parents


def _descendants() -> set[int]:
    """Every process that descends from this one right now."""
    parents, mine, found = _process_parents(), os.getpid(), set()
    changed = True
    while changed:
        changed = False
        for pid, parent in parents.items():
            if pid not in found and (parent == mine or parent in found):
                found.add(pid)
                changed = True
    return found


def _terminate_new_descendants(baseline: set[int]) -> int:
    """Terminate the processes this render started (descendants that were not there before it). Returns how many. Only
    processes that descend from THIS process are ever touched; where this cannot be done safely (not Windows) nothing is."""
    api = _windows_process_api()
    if api is None:
        return 0
    count = 0
    for _ in range(2):  # a second pass catches anything started while the first was running
        for pid in sorted(_descendants() - baseline, reverse=True):
            handle = api.OpenProcess(0x0001, False, pid)  # PROCESS_TERMINATE
            if handle:
                try:
                    count += 1 if api.TerminateProcess(handle, 1) else 0
                finally:
                    api.CloseHandle(handle)
    return count


@dataclass(frozen=True)
class FontSource:
    """A font file to copy into the work folder. `label` must equal the font's name in the render input's fingerprints."""

    label: str
    path: object  # str or PathLike; only read
    family: str = EXPECTED_FONT_FAMILY
    weight: str = "400"
    style: str = "normal"


@dataclass(frozen=True)
class RenderOutcome:
    html_path: Path
    pdf_path: Path
    pdf_sha256: str
    byte_count: int
    render_input_sha256: str  # the hash of the very ClientRenderInput object that was rendered
    browser_version: str  # the Chromium build that drew the pages, for example "156.0.8078.4"
    pdf: pv.PdfInfo
    page_kinds: tuple[str, ...]  # cover, keywords or report, in document order
    page_texts: tuple[str, ...]  # the visible text of each page body (the PDF is printed from the same DOM)
    measures: tuple[dict, ...]
    estimates: tuple[pv.EstimateComparison, ...]
    capacity_warnings: tuple[pv.EstimateComparison, ...]  # pages measured above the planner's usable height
    fonts_used: tuple[tuple[str, int, bool], ...]  # (family, glyphs, is a downloaded font), most glyphs first
    issues: tuple[pv.LayoutIssue, ...]
    blocked_requests: int
    watermark_pages: int = 0  # pages on which the preview label was measured (0 for a production render, which has no label)

    @property
    def ok(self) -> bool:
        return not self.issues


MEASURE_SCRIPT = r"""
() => {
  const px = (v) => parseFloat(v) || 0;
  return [...document.querySelectorAll('section.page')].map((page) => {
    const body = page.querySelector('.page__body');
    const frame = page.querySelector('.page__frame');
    const pr = page.getBoundingClientRect(), br = body.getBoundingClientRect(), fr = frame.getBoundingClientRect();
    const all = [...body.querySelectorAll('*')];
    let contentBottom = br.top, contentRight = 0, extent = 0;
    for (const el of all) {
      const r = el.getBoundingClientRect();
      if (r.width === 0 && r.height === 0) continue;
      contentBottom = Math.max(contentBottom, r.bottom);
      contentRight = Math.max(contentRight, r.right);
      extent = Math.max(extent, r.bottom + px(getComputedStyle(el).marginBottom) - br.top);
    }
    const kids = [...body.children].map((el, i) => ({i, r: el.getBoundingClientRect()})).filter(k => k.r.height > 0);
    kids.sort((a, b) => a.r.top - b.r.top);
    const overlaps = [];
    for (let k = 1; k < kids.length; k++) {
      const by = kids[k - 1].r.bottom - kids[k].r.top;
      if (by > 0.75) overlaps.push({first: kids[k - 1].i, second: kids[k].i, by});
    }
    const clippedRows = [];
    for (const table of body.querySelectorAll('table')) {
      const name = table.closest('[data-table]') ? table.closest('[data-table]').dataset.table : 'keywords';
      [...table.querySelectorAll('tbody tr')].forEach((tr, index) => {
        const over = tr.getBoundingClientRect().bottom - br.bottom;
        if (over > 0.75) clippedRows.push({table: name, index, over});
      });
    }
    const svgs = [...body.querySelectorAll('svg')].map((svg) => {
      const r = svg.getBoundingClientRect(), vb = svg.viewBox.baseVal;
      let outside = 0;
      for (const t of svg.querySelectorAll('text')) {
        const b = t.getBBox();
        if (b.x < -0.5 || b.y < -0.5 || b.x + b.width > vb.width + 0.5 || b.y + b.height > vb.height + 0.5) outside++;
      }
      const title = svg.querySelector('title');
      return {id: title && title.id ? title.id : 'svg', width: r.width, height: r.height,
              shapes: svg.querySelectorAll('line,polyline,circle,rect,text,path').length, textOutside: outside};
    });
    const clippedText = [];
    for (const el of body.querySelectorAll('td,th,p,h1,h2,a,li,div,span')) {
      if (el.clientWidth > 0 && el.scrollWidth - el.clientWidth > 1)
        clippedText.push({tag: el.tagName.toLowerCase(), by: el.scrollWidth - el.clientWidth});
    }
    const images = [...page.querySelectorAll('img')];
    return {
      kind: page.dataset.pageKind, pageWidth: pr.width, pageHeight: pr.height,
      bodyTop: br.top, bodyBottom: br.bottom, clientHeight: body.clientHeight, scrollHeight: body.scrollHeight,
      frameRight: fr.right, contentBottom, contentRight, contentExtent: extent,
      hasHeader: !!page.querySelector('.page-header'), hasFooter: !!page.querySelector('.page-footer'),
      logoLoaded: images.length > 0 && images.every(i => i.complete && i.naturalWidth > 0),
      textLength: body.innerText.trim().length, text: body.innerText, tableRows: body.querySelectorAll('tbody tr').length,
      svgCount: svgs.length, svgs, overlaps, clippedRows, clippedText: clippedText.slice(0, 20), fonts: []
    };
  });
}
"""

# --- the preview label (R8.7h.2) ------------------------------------------------------------------------------------------------
#
# A preview is the production render plus ONE small label in the empty top margin of every page (the header starts 10 mm from the top; the label sits at
# 2.2-7.5 mm), added to the HTML after the production document has been built. It is only added when `render_report_pdf(..., preview_label=...)` is
# called; without it nothing here runs and the output is byte-for-byte what it always was. The label is measured in the browser on every page (present
# exactly once, visible, inside the page, not intersecting the header row, rule, body or footer) and any failure is a layout issue, so a preview whose
# label is missing, hidden or overlapping is never accepted.

PREVIEW_LABEL = "PREVIEW \u2014 NOT FINAL"
_PREVIEW_CSS = """
.preview-watermark { position: absolute; top: 2.2mm; left: 0; right: 0; width: max-content; margin: 0 auto; z-index: 5; pointer-events: none;
  padding: 0.6mm 4mm; border: 0.3mm solid var(--accent); color: var(--accent); background: var(--paper);
  font-size: 8pt; font-weight: 700; letter-spacing: 0.4mm; line-height: 1.3; white-space: nowrap; }
"""

WATERMARK_SCRIPT = r"""
(label) => [...document.querySelectorAll('section.page')].map((page) => {
  const marks = [...page.querySelectorAll('[data-preview-watermark]')];
  const out = {count: marks.length, text: marks.length ? marks[0].textContent : '', visible: false, inside: false, clear: true};
  if (!marks.length) return out;
  const pr = page.getBoundingClientRect(), r = marks[0].getBoundingClientRect(), cs = getComputedStyle(marks[0]);
  out.visible = cs.display !== 'none' && cs.visibility === 'visible' && parseFloat(cs.opacity) > 0.9 && r.width > 0 && r.height > 0;
  out.inside = r.left >= pr.left && r.right <= pr.right && r.top >= pr.top && r.bottom <= pr.bottom;
  for (const el of page.querySelectorAll('.page-header__row, .page-header__rule, .page__body, .page-footer')) {
    const o = el.getBoundingClientRect();
    if (o.width > 0 && o.height > 0 && r.left < o.right && r.right > o.left && r.top < o.bottom && r.bottom > o.top) out.clear = false;
  }
  return out;
})
"""


def inject_preview_label(html: str, label: str) -> str:
    """The document with the preview label inside every `section.page` and its style rule; raises when it cannot be placed."""
    badge = f'<div class="preview-watermark" data-preview-watermark role="note">{html_escape.escape(label)}</div>'
    changed, count = re.subn(r'(<section class="page [^"]*"[^>]*>)', lambda m: m.group(1) + badge, html)
    if count == 0 or "</style>" not in changed:
        raise PdfRenderError("preview_label_failed", "the preview label could not be placed in the document")
    return changed.replace("</style>", _PREVIEW_CSS + "</style>", 1)


def _watermark_issues(state, label: str, page_count: int) -> list:
    if not isinstance(state, list) or len(state) != page_count:
        return [pv.LayoutIssue(0, "preview_label_missing", "the preview label could not be measured on every page")]
    found = []
    for number, page in enumerate(state, start=1):
        if page.get("count") != 1 or page.get("text") != label:
            found.append(pv.LayoutIssue(number, "preview_label_missing", f"the preview label appears {page.get('count')} time(s) or with other text"))
        elif not page.get("visible"):
            found.append(pv.LayoutIssue(number, "preview_label_hidden", "the preview label is not visible"))
        elif not page.get("inside"):
            found.append(pv.LayoutIssue(number, "preview_label_outside_page", "the preview label lies outside the page"))
        elif not page.get("clear"):
            found.append(pv.LayoutIssue(number, "preview_label_overlap", "the preview label overlaps the header, body or footer"))
    return found


_MARK_SCRIPT = r"""
() => {
  let n = 0;
  for (const el of document.querySelectorAll('section.page *')) {
    if ([...el.childNodes].some(c => c.nodeType === 3 && c.textContent.trim().length > 0)) { el.setAttribute('data-r6-text', ''); n++; }
  }
  return n;
}
"""


def _check_workdir(workdir) -> Path:
    path = Path(workdir)
    if not path.is_absolute() or not path.is_dir():
        raise PdfRenderError("workdir_invalid", "the work folder must be an existing absolute folder")
    resolved = path.resolve()
    if resolved == PROJECT_ROOT or PROJECT_ROOT in resolved.parents:
        raise PdfRenderError("workdir_inside_project", "the work folder must be outside the project tree")
    if any(resolved.iterdir()):
        raise PdfRenderError("workdir_not_empty", "the work folder must be empty; nothing is overwritten")
    return resolved


def _copy(source, destination: Path) -> str:
    if destination.exists():
        raise PdfRenderError("would_overwrite", "a file already exists in the work folder")
    shutil.copyfile(source, destination)  # the original is only read
    return af.sha256_of_file(destination, role=destination.name)


def _fonts_by_page(page, cdp, n_pages: int):
    """{page index: {family: [glyphs, custom]}} from the browser's own platform-font report."""
    page.evaluate(_MARK_SCRIPT)
    cdp.send("DOM.enable")
    cdp.send("CSS.enable")
    root = cdp.send("DOM.getDocument", {"depth": 0})["root"]["nodeId"]
    pages = cdp.send("DOM.querySelectorAll", {"nodeId": root, "selector": "section.page"})["nodeIds"]
    result = {}
    for index, page_id in enumerate(pages):
        found = {}
        for node in cdp.send("DOM.querySelectorAll", {"nodeId": page_id, "selector": "[data-r6-text]"})["nodeIds"]:
            for font in cdp.send("CSS.getPlatformFontsForNode", {"nodeId": node})["fonts"]:
                entry = found.setdefault(font["familyName"], [0, False])
                entry[0] += font["glyphCount"]
                entry[1] = entry[1] or bool(font.get("isCustomFont"))
        result[index] = found
    return result


def render_report_pdf(render_input: ri.ClientRenderInput, *, workdir, logo_path, fonts,
                      timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS, terminate=None, preview_label: str | None = None) -> RenderOutcome:
    """Render the whole report to `workdir`/report.pdf and validate it. See the module docstring.

    `timeout_seconds` is a HARD limit for the browser part (start, navigation, measuring, PDF), enforced by Playwright
    timeouts where they exist and by a watchdog thread that ends the browser processes otherwise (a script stuck in the
    page, a hung PDF call). The browser is then given `SHUTDOWN_GRACE_SECONDS` to close. A breach raises
    `RenderTimeoutError(phase)` before any PDF is returned. `terminate(baseline)` replaces the process ending (tests). `preview_label` (default None: the production render, unchanged) adds the preview label to
    every page and measures it there (see above); only the preview command passes it."""
    if preview_label is not None and (not isinstance(preview_label, str) or not 0 < len(preview_label) <= 40):
        raise PdfRenderError("preview_label_invalid", "the preview label must be a short text")
    if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float)) or not 0 < timeout_seconds <= 86400:
        raise PdfRenderError("timeout_invalid", "the render timeout must be between 0 and 86400 seconds")
    if not isinstance(render_input, ri.ClientRenderInput):
        raise TypeError(f"render_input must be a ClientRenderInput, got {type(render_input).__name__}")
    input_hash = render_input_sha256(render_input)  # computed from the same immutable object that is rendered below
    fonts = tuple(fonts)
    if not fonts or any(not isinstance(f, FontSource) for f in fonts):
        raise PdfRenderError("fonts_invalid", "fonts must be a non-empty list of FontSource")
    work = _check_workdir(workdir)
    (work / "assets").mkdir()
    (work / "fonts").mkdir()

    logo_digest = _copy(logo_path, work / "assets" / "jasper-logo.png")
    if logo_digest != render_input.fingerprints.logo_sha256:
        raise PdfRenderError("fingerprint_mismatch", "the logo does not match the logo fingerprint of the render input")
    wanted = {f.name: f.sha256 for f in render_input.fingerprints.fonts}
    faces = []
    for source in fonts:
        name = Path(source.path).name
        digest = _copy(source.path, work / "fonts" / name)
        if wanted.get(source.label) != digest:
            raise PdfRenderError("fingerprint_mismatch", f"font {source.label} does not match its fingerprint")
        faces.append(rt.FontFace(source.family, f"fonts/{name}", source.weight, source.style))
    if set(wanted) != {f.label for f in fonts}:
        raise PdfRenderError("fingerprint_mismatch", "the fonts supplied differ from the fonts in the fingerprints")

    width, height = rt.read_png_size(work / "assets" / "jasper-logo.png")
    assets = rt.TemplateAssets(rt.LogoAsset("assets/jasper-logo.png", width, height), tuple(faces))
    plan = rd.plan_report(render_input, assets)  # raises before anything is written when content cannot be placed
    html = rd.render_report_document(render_input, assets)
    if preview_label is not None:
        html = inject_preview_label(html, preview_label)
    html_path = work / DOCUMENT_NAME
    with open(html_path, "xb") as handle:
        handle.write(html.encode("utf-8"))

    from playwright.sync_api import TimeoutError as PlaywrightTimeout
    from playwright.sync_api import sync_playwright
    blocked: list[str] = []
    watermark_state = None

    def gate(route):
        url = route.request.url
        if url.startswith(("file:", "data:")):
            route.continue_()
        else:
            blocked.append("request")
            route.abort()

    raw_path = work / RAW_PDF_NAME
    baseline = _descendants()
    ender = terminate or _terminate_new_descendants
    dog = _Watchdog(float(timeout_seconds), lambda: ender(baseline))
    phase, started, playwright, browser = "launch", time.monotonic(), None, None
    ms = lambda: max(1, int((timeout_seconds - (time.monotonic() - started)) * 1000))  # noqa: E731

    def shutdown():
        nonlocal phase
        phase = "shutdown"
        dog.arm(min(SHUTDOWN_GRACE_SECONDS, float(timeout_seconds)))
        try:
            if browser is not None:
                browser.close()
        finally:
            if playwright is not None:
                playwright.stop()

    dog.start()
    try:
        try:
            playwright = sync_playwright().start()
            browser = playwright.chromium.launch(timeout=ms())
            browser_version = browser.version
            context = browser.new_context()
            context.set_default_timeout(ms())
            page = context.new_page()
            page.set_default_navigation_timeout(ms())
            page.route("**/*", gate)
            phase = "navigation"
            page.goto(html_path.as_uri(), timeout=ms())
            phase = "evaluation"
            page.evaluate("document.fonts.ready.then(() => true)")
            page.emulate_media(media="print")
            measures = page.evaluate(MEASURE_SCRIPT)
            if preview_label is not None:
                watermark_state = page.evaluate(WATERMARK_SCRIPT, preview_label)
            phase = "pdf"
            page.pdf(path=str(raw_path), prefer_css_page_size=True, print_background=True)
            phase = "evaluation"
            fonts_by_page = _fonts_by_page(page, context.new_cdp_session(page), len(measures))
        except BaseException as error:
            failed_phase = phase
            try:
                shutdown()
            except BaseException:
                pass  # the original failure is the one reported
            if isinstance(error, (KeyboardInterrupt, SystemExit)):
                raise
            if dog.fired or isinstance(error, PlaywrightTimeout):
                raise RenderTimeoutError(failed_phase) from None
            raise
        try:
            shutdown()
        except BaseException as error:
            if isinstance(error, (KeyboardInterrupt, SystemExit)):
                raise
            raise RenderTimeoutError("shutdown") from None
        if dog.fired:  # the deadline passed while the browser was closing: do not trust the outcome
            raise RenderTimeoutError(phase)
    finally:
        dog.cancel()
        if dog.fired:
            ender(baseline)  # nothing started by this render may outlive it

    for index, measure in enumerate(measures):
        measure["fonts"] = [{"family": family, "glyphs": glyphs, "custom": custom}
                            for family, (glyphs, custom) in sorted(fonts_by_page.get(index, {}).items(),
                                                                   key=lambda kv: -kv[1][0])]
    raw = raw_path.read_bytes()
    final = pv.normalize_pdf_dates(raw, render_input.generation_date)
    pdf_path = work / PDF_NAME
    with open(pdf_path, "xb") as handle:
        handle.write(final)
    info = pv.inspect_pdf(final)

    estimates_in = [("cover", None)] + [("keywords", p.estimated_mm) for p in plan.keyword_pages] \
        + [("report", p.estimated_mm) for p in plan.section_pages]
    issues = list(pv.analyse_layout(measures, expected_fonts=(EXPECTED_FONT_FAMILY,), blocked_requests=blocked))
    if preview_label is not None:
        issues.extend(_watermark_issues(watermark_state, preview_label, len(measures)))
    if info.page_count != len(measures) or info.page_count != plan.page_count:
        issues.append(pv.LayoutIssue(0, "page_count_mismatch", f"the PDF has {info.page_count} pages, the document has "
                                                               f"{len(measures)} and the plan {plan.page_count}"))
    for number, box in enumerate(info.page_boxes, start=1):
        if not pv.is_a4(box):
            issues.append(pv.LayoutIssue(number, "pdf_page_not_a4", f"the page box is {box[0]} x {box[1]} pt"))
    if not info.xref_consistent or not info.ends_with_eof:
        issues.append(pv.LayoutIssue(0, "pdf_structure", "the cross-reference table or the end marker is inconsistent"))
    if not any("DejaVuSans" in name for name in info.fonts):
        issues.append(pv.LayoutIssue(0, "font_not_embedded", "the PDF does not embed DejaVu Sans"))
    comparisons = pv.compare_estimates(measures, estimates_in, kp.usable_height_mm(assets))
    # A page whose measured content is above the planner's usable height (the body minus its 8 mm reserve) is a
    # warning, not an issue: the content still fits the real page body unless `vertical_overflow` says otherwise.
    warnings = tuple(item for item in comparisons if not item.within_usable)
    totals: dict[str, list] = {}
    for measure in measures:
        for font in measure["fonts"]:
            entry = totals.setdefault(font["family"], [0, False])
            entry[0] += font["glyphs"]
            entry[1] = entry[1] or font["custom"]
    used = tuple(sorted(((f, g, c) for f, (g, c) in totals.items()), key=lambda t: -t[1]))
    return RenderOutcome(
        html_path=html_path, pdf_path=pdf_path, pdf_sha256=hashlib.sha256(final).hexdigest(), byte_count=len(final),
        render_input_sha256=input_hash, browser_version=browser_version, pdf=info, page_kinds=tuple(m["kind"] for m in measures), page_texts=tuple(m["text"] for m in measures),
        measures=tuple(measures), estimates=comparisons, capacity_warnings=warnings, fonts_used=used,
        issues=tuple(issues), blocked_requests=len(blocked),
        watermark_pages=len(measures) if preview_label is not None and not any(i.code.startswith("preview_label") for i in issues) else 0)
