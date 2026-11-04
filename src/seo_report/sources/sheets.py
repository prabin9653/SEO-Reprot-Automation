"""Read-only access to the reporting workbook (Google Sheets API)."""

from seo_report.clients import (
    CLIENT_TAB,
    KEYWORD_TAB,
    Sender,
    build_clients,
    load_default_sender,
)


def build_service(credentials=None):
    # Imported lazily so the parsing code and its tests need no credentials.
    from googleapiclient.discovery import build

    if credentials is None:  # same shape as sources.gsc.build_service / sources.ga4.build_client (R8.6b: injectable credentials)
        from seo_report.auth import get_credentials

        credentials = get_credentials()
    return build("sheets", "v4", credentials=credentials, cache_discovery=False)


def read_tab(service, spreadsheet_id: str, tab: str) -> list[list]:
    """Return every row of a tab, with dates and numbers unformatted (row 1 = header)."""
    quoted = "'" + tab.replace("'", "''") + "'"
    response = (
        service.spreadsheets()
        .values()
        .get(spreadsheetId=spreadsheet_id, range=quoted, valueRenderOption="UNFORMATTED_VALUE")
        .execute()
    )
    return response.get("values", [])


def load_clients(spreadsheet_id: str, service=None, default_sender: Sender | None = None):
    service = service or build_service()
    default_sender = default_sender or load_default_sender()
    return build_clients(
        read_tab(service, spreadsheet_id, CLIENT_TAB),
        read_tab(service, spreadsheet_id, KEYWORD_TAB),
        default_sender,
    )
