"""R8.3: per-client loading of the reporting workbook (raw rows, no Google calls)."""
import dataclasses
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from seo_report import clients as cl
from seo_report import contracts
from seo_report.client_config import ClientEntry, ClientRegistry
from seo_report.client_loader import (ClientRun, LoadResult, Rejection, WorkbookError, load_from_workbook,
                                      load_run_clients)
from test_pipeline import SENDER

CLIENT_HEADER = ["Client Name", "Website", "Project Manager", "Manager Email", "Manager Phone", "GSC Property",
                 "GA4 Property ID", "Target Country", "Target City", "Report Start Date", "Report End Date", "Status"]
KEYWORD_HEADER = ["Client Name", "Keyword", "Target Location", "Previous Ranking", "Current Ranking", "Target URL", "Notes"]
REGISTRY = ClientRegistry([ClientEntry("Alpha Co", "alpha-co"), ClientEntry("Beta Co", "beta-co"),
                           ClientEntry("Gamma Co", "gamma-co"), ClientEntry("Delta Co", "delta-co")])


def client_row(name="Alpha Co", **over):
    values = {"Client Name": name, "Website": "https://alpha.test/", "Project Manager": "Manager Person",
              "Manager Email": "manager@example.test", "Manager Phone": "555-010-0123",
              "GSC Property": "https://alpha.test/", "GA4 Property ID": 111111111, "Target Country": "United States",
              "Target City": "Dallas", "Report Start Date": "2026-09-29", "Report End Date": "2026-10-26",
              "Status": "Active"}
    values.update(over)
    return [values[h] for h in CLIENT_HEADER]


def keyword_row(name="Alpha Co", keyword="roof repair", previous=3, current=2):
    return [name, keyword, "Dallas", previous, current, "https://alpha.test/roof", "note"]


def load(clients, keywords, **kw):
    return load_run_clients([CLIENT_HEADER, *clients], [KEYWORD_HEADER, *keywords], registry=REGISTRY,
                            default_sender=SENDER, **kw)


def rejected(result):
    return {(r.client_key, r.sheet_row, r.code) for r in result.rejected}


# --- the normal case ------------------------------------------------------------------------------------------------------------

def test_clients_load_with_a_stable_key_keywords_and_an_explicit_28_day_period():
    result = load([client_row()], [keyword_row(), keyword_row(keyword="gutter repair")])
    assert result.rejected == () and result.skipped == () and list(result.clients) == ["alpha-co"]
    run = result.clients["alpha-co"]
    assert isinstance(run, ClientRun) and run.client_key == "alpha-co"
    assert [k.keyword for k in run.client.keywords] == ["roof repair", "gutter repair"]
    assert run.periods.current.days == 28 and run.periods.previous.days == 28
    assert run.period_id == "2026-09-29_2026-10-26" and run.run_key == "alpha-co/2026-09-29_2026-10-26"
    assert run.client.ga4_property_id == "111111111"
    assert run.client.sender.name == "Manager Person"  # resolved by the existing sender rules


def test_the_key_comes_from_the_registry_not_from_the_name():
    result = load([client_row("Alpha Co")], [keyword_row()])
    assert next(iter(result.clients)) == REGISTRY.key_for("Alpha Co") == "alpha-co"


def test_original_workbook_row_numbers_are_restored():
    result = load([client_row("Beta Co", **{"GSC Property": "https://b.test/"}), client_row("Alpha Co")],
                  [keyword_row("Alpha Co"), keyword_row("Beta Co"), keyword_row("Alpha Co", "second")])
    alpha, beta = result.clients["alpha-co"].client, result.clients["beta-co"].client
    assert (alpha.sheet_row, beta.sheet_row) == (3, 2)
    assert [k.sheet_row for k in alpha.keywords] == [2, 4] and [k.sheet_row for k in beta.keywords] == [3]


def test_the_manager_email_is_the_senders_and_the_loaded_client_has_no_recipient():
    run = load([client_row()], [keyword_row()]).clients["alpha-co"]
    assert run.client.sender.email == "manager@example.test"
    assert not hasattr(run, "recipient") and not hasattr(run.client, "recipient")
    assert not [f.name for f in dataclasses.fields(run.client) if "recipient" in f.name or "client_email" in f.name]
    assert not [f.name for f in dataclasses.fields(run) if "email" in f.name or "recipient" in f.name]
    # the address appears only in the sender block that the report prints
    assert "manager@example.test" not in repr(run.periods) and run.client_key != "manager@example.test"


def test_the_only_filter_loads_one_client_and_does_not_validate_others():
    result = load([client_row(), client_row("Beta Co", **{"Report Start Date": "garbage"})],
                  [keyword_row(), keyword_row("Beta Co")], only="alpha-co")
    assert list(result.clients) == ["alpha-co"] and result.rejected == ()


# --- isolation --------------------------------------------------------------------------------------------------------------------

def test_a_malformed_client_does_not_affect_any_other_client():
    rows = [client_row("Alpha Co"), client_row("Beta Co", **{"Report Start Date": "not a date"}),
            client_row("Gamma Co", **{"GA4 Property ID": "abc"}), client_row("Delta Co")]
    keywords = [keyword_row(n) for n in ("Alpha Co", "Beta Co", "Gamma Co", "Delta Co")]
    result = load(rows, keywords)
    assert sorted(result.clients) == ["alpha-co", "delta-co"]
    assert rejected(result) == {("beta-co", 3, "period_dates_invalid"), ("gamma-co", 4, "ga4_property_invalid")}
    # the good clients are identical to what they would be alone
    alone = load([client_row("Alpha Co")], [keyword_row("Alpha Co")]).clients["alpha-co"]
    assert result.clients["alpha-co"].client.keywords == alone.client.keywords
    assert result.clients["alpha-co"].periods == alone.periods


@pytest.mark.parametrize("over,code", [
    ({"Website": ""}, "website_missing"), ({"GSC Property": ""}, "gsc_property_missing"),
    ({"GA4 Property ID": ""}, "ga4_property_missing"), ({"Report Start Date": ""}, "period_dates_missing"),
    ({"Report End Date": ""}, "period_dates_missing"), ({"Report End Date": "2026-10-27"}, "period_not_28_days"),
    ({"Report Start Date": "2026-10-26", "Report End Date": "2026-09-29"}, "period_not_28_days")])
def test_each_missing_or_invalid_setting_is_a_fixed_rejection(over, code):
    result = load([client_row(**over)], [keyword_row()])
    assert result.clients == {} and rejected(result) == {("alpha-co", 2, code)}


def test_a_client_without_keywords_and_a_keyword_for_an_unknown_client():
    result = load([client_row("Alpha Co"), client_row("Beta Co")], [keyword_row("Beta Co"), keyword_row("Nobody Co")])
    assert sorted(result.clients) == ["beta-co"]
    assert rejected(result) == {("alpha-co", 2, "keywords_missing"), (None, 3, "keyword_client_unknown")}


def test_unregistered_duplicate_empty_and_inactive_clients():
    result = load([client_row("Alpha Co"), client_row("alpha  co"), client_row("Unlisted Co"), client_row("Beta Co", Status="Paused"),
                   client_row("Gamma Co", Status=""), [None] * 12, client_row("   ")],
                  [keyword_row("Alpha Co"), keyword_row("Unlisted Co"), keyword_row("Beta Co"), keyword_row("Gamma Co")])
    assert result.clients == {}
    assert rejected(result) == {("alpha-co", 2, "duplicate_client"), (None, 4, "client_not_in_registry"),
                                (None, 8, "client_name_empty")}
    assert set(result.skipped) == {("beta-co", "status_not_active"), ("gamma-co", "status_not_active")}


def test_a_bad_ranking_value_rejects_only_that_client():
    result = load([client_row("Alpha Co"), client_row("Beta Co")],
                  [keyword_row("Alpha Co", previous="not a rank!!"), keyword_row("Beta Co")])
    assert sorted(result.clients) == ["beta-co"] and len(result.rejected) == 1
    assert result.rejected[0].client_key == "alpha-co"


def test_rejections_hold_no_names_or_cell_values():
    result = load([client_row("Alpha Co", **{"Report Start Date": "secret-cell-value"})], [keyword_row()])
    text = repr(result.rejected)
    for needle in ("Alpha", "secret-cell-value", "alpha.test", "manager@example.test"):
        assert needle not in text
    assert all(isinstance(r, Rejection) and set(vars(r)) == {"client_key", "sheet_row", "code"} for r in result.rejected)


# --- workbook-level problems --------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("client_rows,keyword_rows", [
    ([], [KEYWORD_HEADER]), ([CLIENT_HEADER], []), ([[None, ""]], [KEYWORD_HEADER]), ([CLIENT_HEADER], [[""]])])
def test_a_missing_header_stops_everything(client_rows, keyword_rows):
    with pytest.raises(WorkbookError) as info:
        load_run_clients(client_rows, keyword_rows, registry=REGISTRY, default_sender=SENDER)
    assert info.value.code == "header_missing"


def test_a_required_column_missing_or_duplicated_stops_everything():
    short = [h for h in CLIENT_HEADER if h != "Status"]
    with pytest.raises(WorkbookError) as info:
        load_run_clients([short], [KEYWORD_HEADER], registry=REGISTRY, default_sender=SENDER)
    assert info.value.code == "header_invalid"
    with pytest.raises(WorkbookError):
        load_run_clients([CLIENT_HEADER + ["Status"]], [KEYWORD_HEADER], registry=REGISTRY, default_sender=SENDER)
    with pytest.raises(WorkbookError):
        load_run_clients([CLIENT_HEADER], [["Keyword", "Previous Ranking", "Current Ranking"]], registry=REGISTRY,
                         default_sender=SENDER)


def test_the_misspelled_manager_header_and_extra_columns_are_accepted():
    header = [("Project Maanger" if h == "Project Manager" else h) for h in CLIENT_HEADER] + ["Internal Notes"]
    result = load_run_clients([header, client_row() + ["whatever"]], [KEYWORD_HEADER, keyword_row()], registry=REGISTRY,
                              default_sender=SENDER)
    assert list(result.clients) == ["alpha-co"] and result.clients["alpha-co"].client.sender.name == "Manager Person"


def test_workbook_loading_uses_an_injected_service_and_only_reads():
    calls = []

    class Request:
        def __init__(self, rows):
            self.rows = rows

        def execute(self):
            return {"values": self.rows}

    class Values:
        def get(self, spreadsheetId, range, valueRenderOption):
            calls.append((spreadsheetId, range, valueRenderOption))
            return Request([CLIENT_HEADER, client_row()] if "Client Configuration" in range
                           else [KEYWORD_HEADER, keyword_row()])

    service = SimpleNamespace(spreadsheets=lambda: SimpleNamespace(values=lambda: Values()))
    result = load_from_workbook("a" * 30, registry=REGISTRY, service=service, default_sender=SENDER)
    assert list(result.clients) == ["alpha-co"]
    assert [c[1] for c in calls] == ["'Client Configuration'", "'Keyword Ranking Sheet'"]
    assert {c[2] for c in calls} == {"UNFORMATTED_VALUE"}  # only reads; the reporting scope is read-only


def test_the_existing_parser_contract_is_unchanged():
    assert cl.CLIENT_COLUMNS["status"] == ("status",) and cl.CLIENT_REQUIRED == (
        "client_name", "website", "gsc_property", "ga4_property_id", "status")
    assert cl.KEYWORD_REQUIRED == ("client_name", "keyword", "previous_ranking", "current_ranking")
    whole = cl.build_clients([CLIENT_HEADER, client_row()], [KEYWORD_HEADER, keyword_row()], SENDER)
    assert whole[0].sheet_row == 2 and contracts.period_id_of(whole[0].report_start, whole[0].report_end)


def test_sheet_clients_without_a_key_are_listed_by_name_outside_the_rejections():
    result = load([client_row("Alpha Co"), client_row("Zeta Co")], [keyword_row(), keyword_row("Zeta Co")])
    assert result.unregistered == ((3, "Zeta Co"),)
    assert (None, 3, "client_not_in_registry") in rejected(result)
    assert "Zeta" not in repr(result.rejected)  # rejections stay free of names; only `unregistered` carries them, for the operator's console
