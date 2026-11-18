import datetime as dt
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from seo_report.clients import (
    ContactProfile,
    Sender,
    SheetDataError,
    build_clients,
    load_default_sender,
    resolve_sender,
    select_clients,
)
from seo_report.rankings import NOT_RECORDED, RankingError, RankingKind

DEFAULT = Sender(
    name="Pat Default",
    title="Account Lead",
    address_lines=("1 Example Way", "Sampletown"),
    phone="555-0100",
    email="pat@example.test",
    website_label="Example Agency",
    website_url=None,
    profiles=(
        ContactProfile("Testland", "Project Manager | Example Agency", ("1 Example Way", "Sampletown"), "Example Agency",
                       "https://agency.example/"),
        ContactProfile("Otherland", "Project Manager | Example Agency", ("2 Other Road",), "https://other.example/",
                       "https://other.example/"),
    ),
)

CLIENT_HEADER = [
    "Client Name", "Website", "Project Maanger", "Manager Email", "Manager Phone",
    "GSC Property", "GA4 Property ID", "Target Country", "Target City",
    "Report Start Date", "Report End Date", "Status",
]
KEYWORD_HEADER = [
    "Client Name", "Keyword", "Target Location", "Previous Ranking",
    "Current Ranking", "Target URL", "Notes",
]


def client_row(name="Example Co", status="Active", manager="Pat Manager", email="", phone="", ga4=123456789):
    return [
        name, "https://example.test/", manager, email, phone, "https://example.test/",
        ga4, "Testland", "Sample City", 46294, 46321, status,
    ]


def keyword_row(client="Example Co", keyword="widget repair", previous="", current=""):
    return [client, keyword, "Sample City", previous, current]


def build(client_rows, keyword_rows):
    return build_clients([CLIENT_HEADER, *client_rows], [KEYWORD_HEADER, *keyword_rows], DEFAULT)


def test_parses_client_dates_and_ga4_id():
    (client,) = build([client_row()], [keyword_row()])
    assert client.name == "Example Co"
    assert client.ga4_property_id == "123456789"
    assert client.report_start == dt.date(2026, 9, 29)
    assert client.report_end == dt.date(2026, 10, 26)
    assert client.status_key == "active"


def test_accepts_corrected_manager_header_spelling():
    header = ["Project Manager" if h == "Project Maanger" else h for h in CLIENT_HEADER]
    (client,) = build_clients(
        [header, client_row(manager="Sam Other", email="sam@example.test")],
        [KEYWORD_HEADER, keyword_row()],
        DEFAULT,
    )
    assert client.sender.name == "Sam Other"


def test_missing_required_header_lists_headers_found():
    header = [h for h in CLIENT_HEADER if h != "GSC Property"]
    with pytest.raises(SheetDataError) as error:
        build_clients([header], [KEYWORD_HEADER], DEFAULT)
    assert "gsc_property" in str(error.value)
    assert "Client Name" in str(error.value)


def test_duplicate_header_is_rejected():
    with pytest.raises(SheetDataError, match="more than once"):
        build_clients([CLIENT_HEADER + ["Status"]], [KEYWORD_HEADER], DEFAULT)


def test_duplicate_client_is_rejected():
    with pytest.raises(SheetDataError, match="duplicate client"):
        build([client_row(), client_row(name=" example  co ")], [])


def test_invalid_ranking_names_sheet_row_column_and_value():
    with pytest.raises(RankingError) as error:
        build([client_row()], [keyword_row(), keyword_row(keyword="gadget", current="Page 2, Rank 11")])
    message = str(error.value)
    assert "'Keyword Ranking Sheet' row 3" in message
    assert "'Current Ranking'" in message
    assert "'Page 2, Rank 11'" in message


def test_rankings_parsed_per_keyword():
    (client,) = build(
        [client_row()],
        [
            keyword_row(previous=16, current="Page 1, Rank 3"),
            keyword_row(keyword="b", previous="Above 10", current=""),
        ],
    )
    first, second = client.keywords
    assert (first.previous.position, first.current.position) == (16, 3)
    assert second.previous.kind is RankingKind.ABOVE_10
    assert second.current.display() == NOT_RECORDED


def test_keyword_for_unknown_client_is_rejected():
    with pytest.raises(SheetDataError, match="not in 'Client Configuration'"):
        build([client_row()], [keyword_row(client="Nobody")])


def test_blank_rows_skipped_and_row_numbers_are_sheet_rows():
    (client,) = build([client_row()], [[], keyword_row(keyword="a"), ["", "", ""], keyword_row(keyword="b")])
    assert [(k.keyword, k.sheet_row) for k in client.keywords] == [("a", 3), ("b", 5)]


def test_full_keyword_list_is_kept_in_order():
    names = [f"keyword {i}" for i in range(37)]
    (client,) = build([client_row()], [keyword_row(keyword=n) for n in names])
    assert [k.keyword for k in client.keywords] == names


def test_status_selection_defaults_to_active_only():
    clients = build(
        [client_row("A", "Active"), client_row("T", "Testing"), client_row("P", "Paused"), client_row("U", "")],
        [keyword_row(c, "k") for c in "ATPU"],
    )
    result = select_clients(clients)
    assert [c.name for c in result.selected] == ["A"]
    assert {c.name for c, _ in result.skipped} == {"T", "P", "U"}


def test_testing_requires_explicit_override_and_unknown_stays_skipped():
    clients = build(
        [client_row("A", "Active"), client_row("T", " testing "), client_row("X", "Mystery")],
        [keyword_row(c, "k") for c in "ATX"],
    )
    result = select_clients(clients, include_statuses=["Testing"])
    assert [c.name for c in result.selected] == ["A", "T"]
    assert [c.name for c, _ in result.skipped] == ["X"]


def test_selected_client_needs_keywords_and_property_ids():
    with pytest.raises(SheetDataError, match="at least one keyword"):
        select_clients(build([client_row()], []))
    with pytest.raises(SheetDataError, match="GA4 Property ID"):
        select_clients(build([client_row(ga4="")], [keyword_row()]))
    assert select_clients(build([client_row("P", "Paused", ga4="")], [])).selected == []


@pytest.mark.parametrize("blank", ["", "   ", None])
def test_a_missing_or_blank_project_manager_is_rejected_and_the_default_name_is_never_a_fallback(blank):
    with pytest.raises(SheetDataError):
        resolve_sender(DEFAULT, "Testland", blank, "pat@example.test", "555-0100")
    with pytest.raises(SheetDataError) as caught:
        build([client_row(manager=blank, email="x@example.test")], [keyword_row()])
    message = str(caught.value)
    assert "'Client Configuration' row 2" in message and "Project Maanger" in message  # row and column, as in the sheet
    assert "Pat Default" not in message and "x@example.test" not in message and "555" not in message


def test_a_paused_client_without_a_project_manager_does_not_block_the_workbook():
    clients = build([client_row(), client_row(name="Paused Co", status="Paused", manager="")], [keyword_row()])
    assert [c.name for c in clients] == ["Example Co", "Paused Co"] and clients[0].sender.name == "Pat Manager"


def test_the_default_sender_name_is_never_used_even_when_the_row_names_the_same_person():
    sender = resolve_sender(DEFAULT, "Testland", "  pat   DEFAULT ", "", "")
    assert sender.name == "pat   DEFAULT" and sender.email is None and sender.phone is None
    [client] = build([client_row(manager="Sam Other")], [keyword_row()])
    assert client.sender.name == "Sam Other" and "Pat Default" not in repr(client.sender)


def test_the_regional_profile_of_the_target_country_supplies_title_address_and_website():
    sender = resolve_sender(DEFAULT, " testland ", "Sam Other", "", "")
    assert sender.name == "Sam Other" and sender.title == "Project Manager | Example Agency"
    assert sender.email is None and sender.phone is None
    assert sender.address_lines == ("1 Example Way", "Sampletown")
    assert (sender.website_label, sender.website_url) == ("Example Agency", "https://agency.example/")
    other = resolve_sender(DEFAULT, "Otherland", "Sam Other", "", "")
    assert other.address_lines == ("2 Other Road",) and "Example Way" not in repr(other) and "agency.example" not in repr(other)
    assert "Other Road" not in repr(sender) and "other.example" not in repr(sender)


@pytest.mark.parametrize("country", ["", "Narnia", "Test land"])
def test_a_target_country_without_a_profile_is_rejected_never_defaulted(country):
    with pytest.raises(SheetDataError):
        resolve_sender(DEFAULT, country, "Sam Other", "", "")
    row = client_row()
    row[7] = country
    with pytest.raises(SheetDataError) as caught:
        build([row], [keyword_row()])
    message = str(caught.value)
    assert "'Client Configuration' row 2, column 'Target Country'" in message and "Otherland, Testland" in message
    assert "Example Way" not in message and "Pat Manager" not in message


def test_a_client_that_is_not_reported_needs_neither_manager_nor_profile():
    row = client_row(name="Paused Co", status="Paused", manager="")
    row[7] = "Narnia"
    [client] = build([row], [keyword_row("Paused Co")])
    assert client.sender.name == "" and client.sender.address_lines == ()


def test_the_regional_profiles_are_loaded_from_the_configuration_file(tmp_path):
    config = tmp_path / "jasper.yaml"
    config.write_text("sender:\n  name: Org\nregional_profiles:\n  Land:\n    title: T\n    address_lines: [A, B]\n"
                      "    website_label: W\n    website_url: https://w.example/\n", encoding="utf-8")
    assert load_default_sender(config).profiles == (ContactProfile("Land", "T", ("A", "B"), "W", "https://w.example/"),)
    config.write_text("sender:\n  name: Org\nregional_profiles:\n  Land:\n    title: T\n", encoding="utf-8")
    with pytest.raises(SheetDataError):
        load_default_sender(config)


def test_the_shipped_configuration_has_exactly_the_united_states_and_australia_profiles():
    profiles = {p.country: p for p in load_default_sender().profiles}
    assert sorted(profiles) == ["Australia", "United States"]
    us, au = profiles["United States"], profiles["Australia"]
    assert us.address_lines == ("3402 E Pine Brook Way", "Houston, TX 77059")
    assert (us.website_label, us.website_url) == ("https://jasperitinc.com/",) * 2
    assert au.address_lines == ("Ingleburn NSW 2565",)
    assert (au.website_label, au.website_url) == ("https://jasperitsolutions.com.au/",) * 2
    assert us.title == "Project Manager | Jasper IT Inc."
    assert au.title == "Project Manager | Jasper IT Solutions"


def test_the_manager_columns_c_d_e_are_loaded_into_the_sender_and_trimmed():
    header = ["Client Name", "Website", "Project Manager", "Manager Email", "Manager Phone", "GSC Property",
              "GA4 Property ID", "Target Country", "Status"]
    row = ["Example Co", "https://example.test/", "  Sam Other ", " sam@example.test ", " 555-0199 ",
           "https://example.test/", "123456789", "Testland", "Active"]
    [client] = build_clients([header, row], [KEYWORD_HEADER], DEFAULT)
    assert (client.sender.name, client.sender.email, client.sender.phone) == ("Sam Other", "sam@example.test", "555-0199")


def test_blank_optional_manager_fields_are_allowed_and_a_malformed_email_is_rejected_without_echo():
    ok = build_clients([CLIENT_HEADER, client_row(manager="Sam Other")], [KEYWORD_HEADER], DEFAULT)
    assert ok[0].sender.email is None and ok[0].sender.phone is None
    with pytest.raises(SheetDataError) as caught:
        build_clients([CLIENT_HEADER, client_row(manager="Sam Other", email="not-an-email")], [KEYWORD_HEADER], DEFAULT)
    assert "valid e-mail" in str(caught.value) and "not-an-email" not in str(caught.value)


def test_different_manager_uses_sheet_contact_when_supplied():
    sender = resolve_sender(DEFAULT, "Testland", "Sam Other", "sam@example.test", "555-0199")
    assert (sender.email, sender.phone) == ("sam@example.test", "555-0199")


def test_load_default_sender_leaves_blank_fields_unset(tmp_path):
    config = tmp_path / "jasper.yaml"
    config.write_text(
        "sender:\n  name: Pat Default\n  email: pat@example.test\n  phone: ''\n  website_url: ''\n",
        encoding="utf-8",
    )
    sender = load_default_sender(config)
    assert sender.name == "Pat Default"
    assert sender.phone is None
    assert sender.website_url is None


def test_project_config_loads():
    assert load_default_sender().name
