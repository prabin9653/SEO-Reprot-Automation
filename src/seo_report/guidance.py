"""Plain-language guidance for the fixed codes the runner reports (stage 1 of the operator-experience work).

Every code that an operator can meet is mapped to two short sentences: what it means and what to do about it. The text is fixed, contains no paths,
identifiers or secrets, and is only ever shown next to the code (it never replaces it, so logs and scripts keep working).

`explain(code)` returns a `Guidance` or None. It never raises and has no side effects.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Guidance:
    meaning: str
    fix: str


_G = Guidance

# Workbook row problems (client_loader): the fix is always an edit in the Google Sheet or in config/clients.yaml.
_ROWS = {
    "client_not_in_registry": _G("This client is in the sheet but has no stable key yet.",
                                 "Register it: `clients add --name \"<exact sheet name>\"` (or `clients add --all-missing --yes` for every missing client)."),
    "status_not_active": _G("The client's Status in the sheet is not Active, so it is skipped.",
                            "Set Status to Active in the Client Configuration tab when the client should be reported."),
    "website_missing": _G("The client's website is blank in the sheet.", "Fill in the Website column of the Client Configuration tab."),
    "gsc_property_missing": _G("The Search Console property is blank in the sheet.",
                               "Fill in the GSC property exactly as Search Console shows it (for example https://www.example.com/ or sc-domain:example.com)."),
    "ga4_property_missing": _G("The GA4 property ID is blank in the sheet.", "Fill in the numeric GA4 property ID in the Client Configuration tab."),
    "keywords_missing": _G("The client has no keyword rows.", "Add the client's keywords to the Keyword Ranking tab under the same Client Name."),
    "period_dates_missing": _G("The report start or end date is blank.", "Fill in both dates in the Client Configuration tab (28 days, inclusive)."),
    "period_not_28_days": _G("The start and end dates do not cover exactly 28 days.",
                             "Change one date so the period is exactly 28 days, counting both days (for example 2026-10-01 to 2026-10-28)."),
    "period_dates_invalid": _G("A report date is not a valid date.", "Write both dates as YYYY-MM-DD in the Client Configuration tab."),
    "ga4_property_invalid": _G("The GA4 property ID is not a valid number.", "Use only the numeric property ID (digits), not a name or a URL."),
    "duplicate_client": _G("The client appears on more than one row.", "Keep one row per client in the Client Configuration tab."),
    "ranking_invalid": _G("A keyword's ranking cell is not a valid ranking.", "Correct the ranking values in the Keyword Ranking tab."),
    "keyword_empty": _G("A keyword row has an empty keyword.", "Fill in or delete the empty keyword row."),
    "client_name_empty": _G("A row has no Client Name.", "Fill in the Client Name or delete the row."),
    "keyword_client_unknown": _G("A keyword row names a client that is not in the Client Configuration tab.",
                                 "Fix the spelling so it matches the client's name exactly, or add the client."),
    "client_row_invalid": _G("The client's row has a problem the loader could not classify.",
                             "Check Project Manager, Manager Email, Manager Phone and Target Country on that row (Country must be United States or Australia)."),
    "header_missing": _G("A sheet tab has no header row.", "Restore the header row of the Client Configuration and Keyword Ranking tabs."),
    "header_invalid": _G("A required column heading is missing or renamed.", "Restore the original column headings in the workbook."),
}

_ERRORS = {
    # sign-in and Google access
    "auth_credentials_revoked": _G("Google no longer accepts the saved sign-in (it expired or was revoked; with the consent screen in Testing mode this happens weekly).",
                                   "Run `setup auth` and sign in again, then `verify live --client <key> --live`."),
    "auth_token_missing": _G("This device has no saved Google sign-in yet.", "Run `setup auth` once and sign in."),
    "auth_refresh_failed": _G("Google refused to renew the saved sign-in.", "Run `setup auth` and sign in again."),
    "auth_credentials_invalid": _G("The saved sign-in file is damaged.", "Run `setup auth` to create a new one."),
    "auth_credentials_missing": _G("The saved sign-in or the OAuth client file was not found.", "Run `doctor`, then `setup auth`."),
    "auth_credentials_unreadable": _G("The saved sign-in file cannot be read.", "Close other programs using it, or run `setup auth` again."),
    "auth_network_error": _G("Google could not be reached.", "Check the internet connection and try again."),
    "oauth_client_file_missing": _G("The OAuth client file is not where the configuration says.", "Copy the team's client file to the credentials folder, then run `doctor`."),
    "gsc_property_not_accessible": _G("Search Console does not list this property for the signed-in Google account.",
                                      "Make the sheet's GSC property match Search Console exactly (www or not, http or https, trailing slash, or sc-domain:), "
                                      "and make sure the signed-in account has access to it."),
    "ga4_schema_incompatible": _G("The GA4 property does not offer the data this report needs.", "Check that the numeric ID is a GA4 property, not a Universal Analytics one."),
    # verification and the production gate
    "live_google_not_verified": _G("Google access has not been verified on this device recently.", "Run `verify live --client <key> --live` (valid for 7 days)."),
    "attestation_missing": _G("No verification exists on this device.", "Run `verify live --client <key> --live`."),
    "attestation_expired": _G("The 7-day verification has expired.", "Run `verify live --client <key> --live` again."),
    "attestation_client_not_covered": _G("This client was not part of the last verification.", "Run `verify live --client <key> --live` for this client."),
    "attestation_property_changed": _G("The client's Search Console or GA4 property changed since it was verified.",
                                       "Run `verify live --client <key> --live` again."),
    "attestation_workbook_changed": _G("The reporting workbook is not the one that was verified.", "Run `verify live --client <key> --live` again."),
    "credentials_not_relocated": _G("The Google credentials are still inside the project folder.", "Move them to the per-user credentials folder (see `doctor`)."),
    "production_gate_blocked": _G("A safety check stopped the run before anything was started.", "Read the listed blockers, or run `doctor`."),
    # clients and periods
    "client_unknown": _G("That client key is not in the client registry.", "Run `clients list` to see keys, or `clients add --name \"<sheet name>\"`."),
    "client_registry_invalid": _G("The client registry file (config/clients.yaml) is missing or malformed.",
                                  "Open it and check each entry has a name and a client_key and that nothing is listed twice."),
    "client_not_eligible": _G("The client's row in the workbook is not ready to run.", "Run `clients check` to see exactly what to fix."),
    "period_differs_from_workbook": _G("The period you typed is not the one in the sheet.", "Use the exact dates from the sheet (`clients check` shows them), or change the sheet first."),
    "sender_config_invalid": _G("The Jasper contact configuration is missing or malformed.", "Check config/jasper.yaml."),
    "reporting_workbook_not_configured": _G("No reporting workbook is set in the runner settings.", "Set workbooks.reporting_spreadsheet_id in runner.yaml."),
    "config_not_given": _G("No runner settings file was found.", "Create it in the standard location, or pass --config PATH."),
    "config_not_found": _G("The runner settings file does not exist.", "Check the path."),
    "ledger_missing": _G("No report has been run on this device yet.", "Nothing to change."),
}

_SHARED = {
    "shared_reports_not_configured": _G("No shared reports folder is set on this computer, so there is nowhere to copy reports to.",
                                        "Add `shared_reports_dir: \"<folder>\"` under `paths:` in the runner settings (for example your Google Drive folder), then run `report export`."),
}

_OUTCOMES = {
    "already_complete": _G("A finished report already exists for this client and period; finished reports are never overwritten.",
                           "Open it with `report open --client <key>`, or use a different period (change the sheet dates), or make a labelled `report preview`."),
}

_ALL = {**_ROWS, **_ERRORS, **_OUTCOMES, **_SHARED}


def explain(code) -> Guidance | None:
    """The guidance for a fixed code (or outcome), or None when there is none."""
    return _ALL.get(code) if isinstance(code, str) else None


def known_codes() -> frozenset:
    return frozenset(_ALL)


def row_fix(code) -> str:
    """One line for a table: the meaning and the fix, or just the code when it has no guidance."""
    guidance = explain(code)
    return f"{guidance.meaning} {guidance.fix}" if guidance else str(code)
