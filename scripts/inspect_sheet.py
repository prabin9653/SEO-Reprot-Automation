"""Read-only inspection of the SEO reporting workbook.

Prints header rows, per-column type summaries and a few sample rows for the
two agreed tabs. Personal data is masked. Nothing is written to the workbook
or to disk.
"""

import os
import re
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from seo_report.auth import get_credentials

def get_spreadsheet_id():
    spreadsheet_id = os.environ.get("JASPER_SEO_SPREADSHEET_ID", "").strip()
    if not spreadsheet_id:
        raise SystemExit(
            "Missing JASPER_SEO_SPREADSHEET_ID environment variable."
        )
    return spreadsheet_id

TABS = ["Client Configuration", "Keyword Ranking Sheet"]
FETCH_RANGE = "A1:Z60"
MAX_SAMPLE_ROWS = 4
MAX_CELL_CHARS = 70

EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
PHONE_RE = re.compile(r"\+?\d[\d\s().-]{8,}\d")
SECRET_RE = re.compile(r"\b[A-Za-z0-9_\-]{32,}\b")


def mask(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return "[masked-number]" if len(re.sub(r"\D", "", str(value))) >= 10 else value
    text = str(value)
    text = EMAIL_RE.sub("[email]", text)
    text = PHONE_RE.sub(
        lambda m: "[phone]" if len(re.sub(r"\D", "", m.group())) >= 10 else m.group(), text
    )
    text = SECRET_RE.sub("[redacted]", text)
    return text if len(text) <= MAX_CELL_CHARS else text[:MAX_CELL_CHARS] + "..."


def cell_type(value):
    if value is None or value == "":
        return "empty"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, (int, float)):
        return "number"
    return "text"


def pattern(value):
    """Collapse short text into a format pattern, e.g. 'Above 10' -> 'above ##'."""
    text = str(value).strip().lower()
    return re.sub(r"\d+", "#", text) if len(text) <= 12 else None


def get_cell(row, index):
    return row[index] if index < len(row) else None


def read_tab(service, title, spreadsheet_id):
    rng = f"'{title}'!{FETCH_RANGE}"
    values = service.spreadsheets().values()

    def fetch(render):
        return values.get(
            spreadsheetId=spreadsheet_id, range=rng, valueRenderOption=render
        ).execute().get("values", [])

    return fetch("FORMATTED_VALUE"), fetch("UNFORMATTED_VALUE"), fetch("FORMULA")


def report_tab(title, grid, formatted, raw, formulas):
    print(f"\n=== {title} ===")
    print(f"Grid size: {grid.get('rowCount')} rows x {grid.get('columnCount')} columns")
    if not raw:
        print("No data found in the fetched range.")
        return

    headers = [str(h) for h in raw[0]]
    width = max(len(r) for r in raw)
    headers += [""] * (width - len(headers))
    data_rows = [i for i in range(1, len(raw)) if any(c not in (None, "") for c in raw[i])]
    print(f"Non-empty data rows in first {FETCH_RANGE.split(':')[1]}: {len(data_rows)}")
    print(f"Headers: {[mask(h) for h in headers]}")

    print("\nColumn summary (type counts; short-text patterns):")
    for col, header in enumerate(headers):
        cells = [get_cell(raw[i], col) for i in data_rows]
        counts = Counter(cell_type(c) for c in cells)
        formulas_n = sum(
            1 for i in data_rows if str(get_cell(formulas[i] if i < len(formulas) else [], col) or "").startswith("=")
        )
        patterns = Counter(p for c in cells if cell_type(c) == "text" and (p := pattern(c)))
        line = f"  [{col}] {mask(header) or '(no header)'}: {dict(counts)}"
        if formulas_n:
            line += f", formulas={formulas_n}"
        if patterns:
            line += f", patterns={dict(patterns.most_common(6))}"
        print(line)

    # Prefer rows whose type/pattern shape has not been seen, to expose format variety.
    seen, picked = set(), []
    for i in data_rows:
        shape = tuple(
            (cell_type(get_cell(raw[i], c)), pattern(get_cell(raw[i], c)) if cell_type(get_cell(raw[i], c)) == "text" else None)
            for c in range(width)
        )
        if shape not in seen:
            seen.add(shape)
            picked.append(i)
    for i in data_rows:
        if len(picked) >= MAX_SAMPLE_ROWS:
            break
        if i not in picked:
            picked.append(i)

    print(f"\nSample rows ({min(len(picked), MAX_SAMPLE_ROWS)}):")
    for i in picked[:MAX_SAMPLE_ROWS]:
        parts = []
        for col in range(width):
            raw_value = get_cell(raw[i], col)
            if cell_type(raw_value) == "empty":
                continue
            shown = mask(raw_value)
            shown_fmt = mask(get_cell(formatted[i] if i < len(formatted) else [], col) or "")
            extra = f" (displays {shown_fmt!r})" if str(shown_fmt) != str(shown) else ""
            parts.append(f"{mask(headers[col]) or col}={shown!r}<{cell_type(raw_value)}>{extra}")
        print(f"  row {i + 1}: " + " | ".join(parts))


def main():
    spreadsheet_id = get_spreadsheet_id()
    service = build("sheets", "v4", credentials=get_credentials(), cache_discovery=False)
    try:
        meta = service.spreadsheets().get(
            spreadsheetId=spreadsheet_id, fields="sheets.properties(title,gridProperties)"
        ).execute()
        grids = {s["properties"]["title"]: s["properties"].get("gridProperties", {}) for s in meta["sheets"]}
        missing = [t for t in TABS if t not in grids]
        if missing:
            print(f"Missing tabs: {missing}. Tabs found: {list(grids)}")
            sys.exit(1)
        for title in TABS:
            formatted, raw, formulas = read_tab(service, title, spreadsheet_id)
            report_tab(title, grids[title], formatted, raw, formulas)
    except HttpError as err:
        print(f"Sheets API error: HTTP {err.resp.status} {err.reason}")
        sys.exit(1)


if __name__ == "__main__":
    main()
