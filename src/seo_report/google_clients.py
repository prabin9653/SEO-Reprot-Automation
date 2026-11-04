"""The Google client factory of the production runner (R8.6b).

One small class that turns the configured credential locations into the three clients the report needs, and one function that turns those
clients into the `Services.collect` callable the pipeline already accepts. It adds **no authentication logic** of its own:

    credentials   -> `auth.get_credentials(paths=...)`            (non-interactive; raises `AuthRequiredError` / `AuthTemporaryError`)
    Sheets        -> `sources.sheets.build_service(credentials)`   (Sheets API v4, read-only scope)
    Search Console-> `sources.gsc.build_service(credentials)`      (Search Console API v1, read-only scope)
    GA4           -> `sources.ga4.build_client(credentials)`       (Analytics Data API, read-only scope)

Properties
  * **Lazy.** Nothing is imported from Google or built until a client is asked for, and `import seo_report.google_clients` loads no Google
    library. Credentials are loaded once per factory instance (the library refreshes an expired access token itself) and not stored anywhere else;
    there is no module-level client or cache.
  * **No network at construction.** The discovery documents are bundled with the client library (`cache_discovery=False`) and the GA4 client opens its
    channel on first use, so building a client makes no request; the first request happens inside the existing fetch functions, which keep their
    retry behaviour, and any error from them is classified by `source_collector.classify_error` exactly as before.
  * **Injectable.** Every dependency can be replaced: `credentials_provider`, and one builder per client. Tests (and any future caller) pass fakes.
  * **Never interactive.** The only sign-in code is `auth.run_setup`, which this module never calls.
  * **Authentication failures stay `auth_required`.** A `StageError` raised while building is not caught here: it reaches the pipeline, which
    records a failed run with category `auth_required` (and the runner stops the batch).
"""

from dataclasses import dataclass, field
from typing import Callable

@dataclass
class GoogleClientFactory:
    """Builds the Sheets, Search Console and GA4 clients on demand from the configured credential locations."""

    paths: object | None = None  # auth.CredentialPaths; None = the fallback locations
    credentials_provider: Callable | None = None  # () -> credentials; default: auth.get_credentials(paths=...)
    credentials_status_provider: Callable | None = None  # () -> (credentials, "ok" | "not_needed"); tests only; default: auth.get_credentials_with_status
    sheets_builder: Callable | None = None  # (credentials) -> Sheets service
    gsc_builder: Callable | None = None  # (credentials) -> Search Console service
    ga4_builder: Callable | None = None  # (credentials) -> Analytics Data client
    _credentials: object = field(default=None, init=False, repr=False)
    _refresh_outcome: object = field(default=None, init=False, repr=False)

    def credentials_with_status(self):
        """`(credentials, refresh outcome)`, loaded on first use (non-interactive). The outcome is "not_needed" or "ok" (a refresh was required and
        succeeded) when it is known, else None (credentials that were already loaded, or an injected provider that does not say). A failed refresh raises,
        as for `credentials()`. Only the live verification asks for the outcome; every other caller keeps using `credentials()`."""
        if self._credentials is None:
            if self.credentials_status_provider is None and self.credentials_provider is None:
                from seo_report import auth
                self._credentials, self._refresh_outcome = auth.get_credentials_with_status(paths=self.paths)
            else:
                self.credentials()
        return self._credentials, self._refresh_outcome

    def credentials(self):
        """The credentials for this factory, loaded on first use (non-interactive)."""
        if self._credentials is None:
            if self.credentials_status_provider is not None:
                self._credentials, self._refresh_outcome = self.credentials_status_provider()
            elif self.credentials_provider is not None:
                self._credentials = self.credentials_provider()
            else:
                from seo_report import auth
                self._credentials = auth.get_credentials(paths=self.paths)
        return self._credentials

    def sheets(self):
        builder = self.sheets_builder
        if builder is None:
            from seo_report.sources import sheets
            builder = sheets.build_service
        return builder(self.credentials())

    def search_console(self):
        builder = self.gsc_builder
        if builder is None:
            from seo_report.sources import gsc
            builder = gsc.build_service
        return builder(self.credentials())

    def analytics(self):
        builder = self.ga4_builder
        if builder is None:
            from seo_report.sources import ga4
            builder = ga4.build_client
        return builder(self.credentials())


def make_collect(factory: GoogleClientFactory, *, fetch_gsc=None, fetch_ga4=None, sleep=None):
    """The production `Services.collect`: `(client_run, today) -> SourceResult`.

    Builds the Search Console and GA4 clients on the first call (lazily, once), then delegates to the existing
    `source_collector.collect_sources`, which validates the property and the periods, retries 429/5xx, classifies errors and treats a valid
    zero-result response as valid. `fetch_gsc`, `fetch_ga4` and `sleep` are passed through for offline tests."""
    from seo_report import source_collector  # imported when the collector is made, not when this module is imported

    clients: dict = {}
    options = {k: v for k, v in (("fetch_gsc", fetch_gsc), ("fetch_ga4", fetch_ga4), ("sleep", sleep)) if v is not None}

    def collect(client_run, today):
        if not clients:  # built together so that a missing sign-in fails before any request is made for either source
            clients["gsc"], clients["ga4"] = factory.search_console(), factory.analytics()
        return source_collector.collect_sources(client_run.client, client_run.periods, gsc_service=clients["gsc"],
                                                ga4_client=clients["ga4"], today=today, **options)

    return collect


def load_workbook_clients(factory: GoogleClientFactory, *, spreadsheet_id, registry, default_sender, only=None):
    """The reporting workbook's clients through the existing loader, reading with the factory's Sheets client.

    Read-only. Raises `client_loader.WorkbookUnavailable` for credential, permission, network or quota problems and `WorkbookError` for header problems."""
    from seo_report import client_loader
    return client_loader.load_from_workbook(spreadsheet_id, registry=registry, service=_LazySheets(factory), default_sender=default_sender,
                                            only=only)


class _LazySheets:
    """Defers building the Sheets client until the loader first reads (so credential errors are classified inside the loader's guard)."""

    def __init__(self, factory):
        self._factory = factory
        self._service = None

    def spreadsheets(self):
        if self._service is None:
            self._service = self._factory.sheets()
        return self._service.spreadsheets()
