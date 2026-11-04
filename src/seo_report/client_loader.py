"""Per-client loading of the reporting workbook (R8.3).

`clients.build_clients` and `clients.select_clients` stop at the first bad row, so one misconfigured client would stop
every client [verified]. This module keeps the parser contract unchanged and isolates clients from each other: it
splits the two tabs' rows by client name and calls the existing `build_clients` once per client with only that
client's rows. A malformed client becomes a `Rejection` with a fixed code; the others load normally.

What a `ClientRun` holds: the stable `client_key` (from `config/clients.yaml`, never generated from the name), the
parsed `clients.Client` (with its keywords and the sender resolved by the existing rules) and the report period
(explicit Report Start/End Date, exactly 28 days). It carries **no recipient**: the workbook's `Manager Email` /
`Manager Phone` are the sender's details and are used only for the report's sender block. Client recipients belong to the
operational workbook's `Client Delivery Settings` tab (a later stage).

Rejections and skips never contain cell values or client names: a fixed code, the client key when known, and the sheet
row number.
"""

import dataclasses
from dataclasses import dataclass

from seo_report import clients as cl
from seo_report import contracts
from seo_report.client_config import ClientConfigError, ClientRegistry
from seo_report.periods import PeriodError, ReportPeriods, report_periods
from seo_report.rankings import RankingError


class WorkbookError(ValueError):
    """The workbook cannot be used at all (header problems affect every client). `code` is fixed."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class WorkbookUnavailable(WorkbookError):
    """The workbook could not be READ (credentials, permission, network, quota). `category` is one of
    `contracts.ERROR_CATEGORIES` (`auth_required`, `source_temporary`, `source_denied`, ...) and `code` is fixed; an
    `auth_required` outcome means a person must act and the batch must not start. The original error is `__cause__`."""

    def __init__(self, category: str, code: str):
        super().__init__(code)
        self.category = category


@dataclass(frozen=True)
class ClientRun:
    client_key: str
    client: cl.Client
    periods: ReportPeriods

    @property
    def period_id(self) -> str:
        return contracts.period_id_of(self.periods.current.start, self.periods.current.end)

    @property
    def run_key(self) -> str:
        return contracts.run_key_of(self.client_key, self.period_id)


@dataclass(frozen=True)
class Rejection:
    client_key: str | None
    sheet_row: int | None
    code: str


@dataclass(frozen=True)
class LoadResult:
    clients: dict  # client_key -> ClientRun
    rejected: tuple  # Rejection
    skipped: tuple  # (client_key, code) for clients that are valid but not due for processing (status not active)


def _blank(cell) -> bool:
    return cell is None or (isinstance(cell, str) and not cell.strip())


def _column(header_row, aliases):
    for position, cell in enumerate(header_row):
        if not _blank(cell) and cl.normalize(cell) in aliases:
            return position
    raise WorkbookError("header_invalid")


def _classify(error: Exception) -> str:
    text = str(error).lower()
    for needle, code in (("invalid date", "period_dates_invalid"), ("ga4 property", "ga4_property_invalid"),
                         ("duplicate client", "duplicate_client"), ("ranking", "ranking_invalid"),
                         ("keyword is empty", "keyword_empty"), ("client name is empty", "client_name_empty")):
        if needle in text:
            return code
    return "client_row_invalid"


def load_run_clients(client_rows, keyword_rows, *, registry: ClientRegistry, default_sender: cl.Sender,
                     only: str | None = None) -> LoadResult:
    """Parse both tabs (raw rows, header first) into per-client `ClientRun`s. `only` restricts to one `client_key`."""
    if not client_rows or not keyword_rows or all(_blank(c) for c in client_rows[0]) \
            or all(_blank(c) for c in keyword_rows[0]):
        raise WorkbookError("header_missing")
    try:
        cl.build_clients([client_rows[0]], [keyword_rows[0]], default_sender)  # header-only check: applies to everyone
    except cl.SheetDataError:
        raise WorkbookError("header_invalid") from None
    name_col = _column(client_rows[0], cl.CLIENT_COLUMNS["client_name"])
    kw_name_col = _column(keyword_rows[0], cl.KEYWORD_COLUMNS["client_name"])

    rejected, skipped, groups, kw_groups = [], [], {}, {}
    for number, row in enumerate(client_rows[1:], start=2):
        if all(_blank(c) for c in row):
            continue
        name = row[name_col] if name_col < len(row) else None
        if _blank(name):
            rejected.append(Rejection(None, number, "client_name_empty"))
            continue
        groups.setdefault(cl.normalize(name), []).append((number, row, str(name).strip()))
    for number, row in enumerate(keyword_rows[1:], start=2):
        if all(_blank(c) for c in row):
            continue
        name = row[kw_name_col] if kw_name_col < len(row) else None
        if _blank(name):
            rejected.append(Rejection(None, number, "client_name_empty"))
            continue
        kw_groups.setdefault(cl.normalize(name), []).append((number, row))
    for norm, rows in kw_groups.items():
        if norm not in groups:
            rejected.extend(Rejection(None, number, "keyword_client_unknown") for number, _ in rows)

    loaded = {}
    for norm, rows in groups.items():
        first_row, display_name = rows[0][0], rows[0][2]
        try:
            client_key = registry.key_for(display_name)
        except ClientConfigError:
            rejected.append(Rejection(None, first_row, "client_not_in_registry"))
            continue
        if only is not None and client_key != only:
            continue
        if len(rows) > 1:
            rejected.append(Rejection(client_key, first_row, "duplicate_client"))
            continue
        keyword_rows_of = kw_groups.get(norm, [])
        try:
            built = cl.build_clients([client_rows[0], rows[0][1]], [keyword_rows[0], *[r for _, r in keyword_rows_of]],
                                     default_sender)
        except (cl.SheetDataError, RankingError) as error:  # a bad ranking cell is raised by the ranking parser
            rejected.append(Rejection(client_key, first_row, _classify(error)))
            continue
        client = built[0]
        client.sheet_row = first_row  # the isolated parse numbers rows from 2; restore the workbook's own numbers
        client.keywords = [dataclasses.replace(k, sheet_row=keyword_rows_of[i][0]) for i, k in enumerate(client.keywords)]
        if client.status_key != cl.ACTIVE_STATUS:
            skipped.append((client_key, "status_not_active"))
            continue
        problem = next((code for code, value in (("website_missing", client.website),
                                                 ("gsc_property_missing", client.gsc_property),
                                                 ("ga4_property_missing", client.ga4_property_id),
                                                 ("keywords_missing", client.keywords),
                                                 ("period_dates_missing", client.report_start and client.report_end))
                        if not value), None)
        if problem:
            rejected.append(Rejection(client_key, first_row, problem))
            continue
        try:
            periods = report_periods(client.report_start, client.report_end)
        except PeriodError:
            rejected.append(Rejection(client_key, first_row, "period_not_28_days"))
            continue
        loaded[client_key] = ClientRun(client_key, client, periods)
    return LoadResult(loaded, tuple(rejected), tuple(skipped))


def load_from_workbook(spreadsheet_id: str, *, registry: ClientRegistry, service=None,
                       default_sender: cl.Sender | None = None, only: str | None = None) -> LoadResult:
    """Read the reporting workbook (read-only) and load the clients. `service` is injectable; without it the existing
    `sources.sheets.build_service` is used (which needs credentials; tests always inject)."""
    from seo_report import source_collector
    from seo_report.sources import sheets
    default_sender = default_sender or cl.load_default_sender()
    try:
        service = service or sheets.build_service()  # non-interactive: missing/expired credentials raise `auth_required`
        client_rows = sheets.read_tab(service, spreadsheet_id, cl.CLIENT_TAB)
        keyword_rows = sheets.read_tab(service, spreadsheet_id, cl.KEYWORD_TAB)
    except Exception as error:
        known = source_collector.classify_error("sheets", error)
        if known is None:
            raise  # an unrecognised error stays visible
        raise WorkbookUnavailable(*known) from error
    return load_run_clients(client_rows, keyword_rows, registry=registry, default_sender=default_sender, only=only)
