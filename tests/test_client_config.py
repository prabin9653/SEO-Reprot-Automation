import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from seo_report import client_config as cc
from seo_report.snapshots.snapshot import CLIENT_KEY_RE

VALID = """\
clients:
  - name: "Example Client"
    client_key: "example-client"
"""


def write(tmp_path, text, name="clients.yaml"):
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def entry(name="Example Co", key="example-co"):
    return f'  - name: "{name}"\n    client_key: "{key}"\n'


@pytest.fixture
def registry(tmp_path):
    return cc.load_client_registry(write(tmp_path, VALID))


# --- loading and lookup ----------------------------------------------------------------------------------------------

def test_a_valid_example_client_configuration_loads(registry):
    assert len(registry) == 1
    assert registry.entries == (cc.ClientEntry("Example Client", "example-client"),)


def test_lookup_by_exact_name_returns_the_configured_key(registry):
    assert registry.key_for("Example Client") == "example-client"


def test_several_clients_can_be_configured_without_code_changes(tmp_path):
    text = "clients:\n" + entry("Example Client", "example-client") + entry("Example Co", "example-co") + entry("X & Y", "x_y-2")
    loaded = cc.load_client_registry(write(tmp_path, text))
    assert [e.client_key for e in loaded.entries] == ["example-client", "example-co", "x_y-2"]
    assert loaded.key_for("X & Y") == "x_y-2" and loaded.key_for("Example Co") == "example-co"


def test_the_project_configuration_file_is_valid():
    loaded = cc.load_client_registry(ROOT / "config" / "clients.yaml.example")
    assert loaded.key_for("Example Client") == "example-client"


def test_the_path_may_be_given_as_text(tmp_path):
    assert cc.load_client_registry(str(write(tmp_path, VALID))).key_for("Example Client") == "example-client"


def test_an_unknown_client_name_raises_a_specific_error(registry):
    with pytest.raises(cc.UnknownClientError, match="no client_key is configured for the client name 'Other Co'"):
        registry.key_for("Other Co")
    assert issubclass(cc.UnknownClientError, cc.ClientConfigError)


@pytest.mark.parametrize("altered", [
    "example client", "EXAMPLE CLIENT", "Example client", " Example Client", "Example Client ", "Example  Client", "Example\tClient", "Example Client\n",
    "", "Example", "Example Client Inc",
])
def test_client_name_matching_is_exact(registry, altered):
    with pytest.raises(cc.UnknownClientError):
        registry.key_for(altered)


def test_a_near_match_is_never_used_but_is_mentioned_as_a_hint(registry):
    with pytest.raises(cc.UnknownClientError, match="differs only in case or spacing exists: 'Example Client'"):
        registry.key_for("example  client")


def test_lookup_requires_text(registry):
    for value in (None, 5, b"Example Client", ["Example Client"]):
        with pytest.raises(cc.ClientConfigError, match="must be text"):
            registry.key_for(value)


def test_lookup_is_deterministic(registry):
    assert [registry.key_for("Example Client") for _ in range(3)] == ["example-client"] * 3
    assert cc.load_client_registry(ROOT / "config" / "clients.yaml.example").entries == \
        cc.load_client_registry(ROOT / "config" / "clients.yaml.example").entries


# --- file problems -----------------------------------------------------------------------------------------------------

def test_a_missing_file_is_reported_clearly(tmp_path):
    with pytest.raises(cc.ClientConfigError, match="client configuration file not found"):
        cc.load_client_registry(tmp_path / "missing.yaml")


def test_a_directory_is_not_a_configuration_file(tmp_path):
    with pytest.raises(cc.ClientConfigError, match="is not a file"):
        cc.load_client_registry(tmp_path)


@pytest.mark.parametrize("path", [None, ""])
def test_a_path_must_be_supplied(path):
    with pytest.raises(cc.ClientConfigError, match="path to the client configuration file is required"):
        cc.load_client_registry(path)


def test_the_loader_neither_creates_files_nor_reads_configuration_at_import(tmp_path):
    before = sorted(p.name for p in tmp_path.iterdir())
    with pytest.raises(cc.ClientConfigError):
        cc.load_client_registry(tmp_path / "missing.yaml")
    assert sorted(p.name for p in tmp_path.iterdir()) == before
    source = (ROOT / "src" / "seo_report" / "client_config.py").read_text(encoding="utf-8")
    for forbidden in ("mkdir", "open(", "write_text", "import requests", "googleapiclient", "sqlite3", "os.environ",
                      "import socket", "import urllib"):
        assert forbidden not in source


@pytest.mark.parametrize("text", [
    "clients: [",
    "clients:\n  - name: 'Example Client\n    client_key: example-client\n",
    "clients:\n\t- name: x\n",
    "clients: {a: 1\n",
    ": : :\n",
])
def test_malformed_yaml_is_rejected(tmp_path, text):
    with pytest.raises(cc.ClientConfigError, match="malformed YAML"):
        cc.load_client_registry(write(tmp_path, text))


def test_malformed_yaml_errors_name_the_line(tmp_path):
    with pytest.raises(cc.ClientConfigError, match=r"clients\.yaml: malformed YAML at line \d+, column \d+"):
        cc.load_client_registry(write(tmp_path, "clients: [\n"))


def test_unsafe_yaml_tags_are_not_executed(tmp_path):
    with pytest.raises(cc.ClientConfigError, match="malformed YAML"):
        cc.load_client_registry(write(tmp_path, "clients: !!python/object/apply:os.getcwd []\n"))


def test_a_duplicate_yaml_key_is_rejected_instead_of_silently_overriding(tmp_path):
    text = "clients:\n  - name: 'Example Client'\n    client_key: 'example-client'\n    client_key: 'other'\n"
    with pytest.raises(cc.ClientConfigError, match="duplicate key 'client_key'"):
        cc.load_client_registry(write(tmp_path, text))
    with pytest.raises(cc.ClientConfigError, match="duplicate key 'clients'"):
        cc.load_client_registry(write(tmp_path, VALID + VALID.replace("example-client", "second")))


def test_non_utf8_and_oversized_files_are_rejected(tmp_path):
    binary = tmp_path / "binary.yaml"
    binary.write_bytes(b"clients:\n  - name: \xff\xfe\n")
    with pytest.raises(cc.ClientConfigError, match="not valid UTF-8"):
        cc.load_client_registry(binary)
    big = write(tmp_path, "# " + "x" * cc.MAX_CONFIG_BYTES + "\n", "big.yaml")
    with pytest.raises(cc.ClientConfigError, match="larger than"):
        cc.load_client_registry(big)


# --- structure ---------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("text", ["", "   \n", "# only a comment\n", "---\n", "null\n"])
def test_an_empty_configuration_is_rejected(tmp_path, text):
    with pytest.raises(cc.ClientConfigError, match="configuration is empty"):
        cc.load_client_registry(write(tmp_path, text))


@pytest.mark.parametrize("text, message", [
    ("- name: a\n", "must be a mapping"),
    ("just text\n", "must be a mapping"),
    ("42\n", "must be a mapping"),
    ("other: 1\n", "unexpected top-level field"),
    ("clients:\n  - name: a\n    client_key: b\nextra: 1\n", "unexpected top-level field"),
    ("{}\n", "needs a 'clients' list"),
    ("clients: ar\n", "'clients' must be a list"),
    ("clients: {name: a}\n", "'clients' must be a list"),
    ("clients: []\n", "at least one client"),
    ("clients:\n  - just text\n", "entry 1 must be a mapping"),
    ("clients:\n  - [a, b]\n", "entry 1 must be a mapping"),
    ("clients:\n  -\n", "entry 1 must be a mapping"),
])
def test_unexpected_structures_are_rejected(tmp_path, text, message):
    with pytest.raises(cc.ClientConfigError, match=message):
        cc.load_client_registry(write(tmp_path, text))


@pytest.mark.parametrize("body, missing", [
    ("  - client_key: 'example-client'\n", "['name']"),
    ("  - name: 'Example Client'\n", "['client_key']"),
    ("  - {}\n", "['name', 'client_key']"),
])
def test_missing_required_fields_are_rejected(tmp_path, body, missing):
    with pytest.raises(cc.ClientConfigError, match=f"entry 1 is missing required field.*{missing[1:-1]}"):
        cc.load_client_registry(write(tmp_path, "clients:\n" + body))


def test_unexpected_entry_fields_are_rejected(tmp_path):
    text = "clients:\n  - name: 'Example Client'\n    client_key: 'example-client'\n    display_name: 'AR'\n"
    with pytest.raises(cc.ClientConfigError, match=r"entry 1 has unexpected field\(s\) \['display_name'\]"):
        cc.load_client_registry(write(tmp_path, text))


# --- names ----------------------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("name", ["", "   ", "\t"])
def test_an_empty_client_name_is_rejected(tmp_path, name):
    with pytest.raises(cc.ClientConfigError, match="name must not be empty"):
        cc.load_client_registry(write(tmp_path, "clients:\n" + entry(name, "example-client")))


@pytest.mark.parametrize("name", [" Example Client", "Example Client ", "  Example Client  "])
def test_names_with_surrounding_whitespace_are_rejected_not_trimmed(tmp_path, name):
    with pytest.raises(cc.ClientConfigError, match="must not start or end with whitespace"):
        cc.load_client_registry(write(tmp_path, "clients:\n" + entry(name, "example-client")))


def test_a_name_with_control_characters_or_excess_length_is_rejected(tmp_path):
    with pytest.raises(cc.ClientConfigError, match="control characters"):
        cc.load_client_registry(write(tmp_path, 'clients:\n  - name: "AR\\x07Roofing"\n    client_key: "example-client"\n'))
    with pytest.raises(cc.ClientConfigError, match="longer than"):
        cc.load_client_registry(write(tmp_path, "clients:\n" + entry("x" * (cc.MAX_NAME_CHARS + 1), "example-client")))


@pytest.mark.parametrize("value", ["123", "true", "null", "[a]", "{a: b}", "1.5"])
def test_non_text_names_and_keys_are_rejected(tmp_path, value):
    with pytest.raises(cc.ClientConfigError, match="name must be text|name must not be empty"):
        cc.load_client_registry(write(tmp_path, f"clients:\n  - name: {value}\n    client_key: 'example-client'\n"))
    with pytest.raises(cc.ClientConfigError, match="client_key must be text|client_key must not be empty"):
        cc.load_client_registry(write(tmp_path, f"clients:\n  - name: 'Example Client'\n    client_key: {value}\n"))


# --- keys ------------------------------------------------------------------------------------------------------------------------

def test_an_empty_client_key_is_rejected(tmp_path):
    with pytest.raises(cc.ClientConfigError, match="client_key must not be empty"):
        cc.load_client_registry(write(tmp_path, "clients:\n" + entry("Example Client", "")))


@pytest.mark.parametrize("key", [
    "Example-Client", "example client", "example/client", "example\\client", "example.client", "example:client", "-ar", "_ar", "ar*", "ár",
    "example-client\n", " example-client", "example-client ", "x" * 64, "..", "a/../b",
])
def test_invalid_client_key_characters_are_rejected(tmp_path, key):
    text = yaml.safe_dump({"clients": [{"name": "Example Client", "client_key": key}]})  # exact characters, any quoting
    with pytest.raises(cc.ClientConfigError, match="client_key .* is invalid"):
        cc.load_client_registry(write(tmp_path, text))


@pytest.mark.parametrize("key", ["a", "0", "example-client", "client_1", "a-b_c-9", "x" * 63])
def test_valid_client_keys_are_accepted(key):
    assert cc.validate_client_key(key) == key


@pytest.mark.parametrize("key", ["con", "prn", "aux", "nul", "com1", "com9", "lpt1", "lpt9"])
def test_windows_device_names_are_not_usable_as_keys(key):
    with pytest.raises(cc.ClientConfigError, match="reserved Windows device name"):
        cc.validate_client_key(key)
    assert cc.validate_client_key("com10") == "com10" and cc.validate_client_key("console") == "console"


def _accepted(key):
    try:
        cc.validate_client_key(key)
        return True
    except cc.ClientConfigError:
        return False


def test_the_key_format_is_the_snapshot_stores_format():
    """Apart from the reserved device names, a key is valid exactly when the snapshot store accepts it."""
    for key in ("example-client", "a", "x" * 63, "Ar", "-a", "x" * 64, "", "a b", "a/b", "0"):
        assert bool(CLIENT_KEY_RE.fullmatch(key)) == _accepted(key), key


def test_keys_are_never_generated_from_names(tmp_path):
    with pytest.raises(cc.ClientConfigError, match="missing required field"):
        cc.load_client_registry(write(tmp_path, "clients:\n  - name: 'Example Co'\n"))
    with pytest.raises(cc.ClientConfigError, match="client_key must not be empty"):
        cc.load_client_registry(write(tmp_path, "clients:\n" + entry("Example Co", "")))
    assert not any(hasattr(cc, name) for name in ("slugify", "generate_client_key", "make_key"))


# --- uniqueness -------------------------------------------------------------------------------------------------------------------

def test_duplicate_client_names_are_rejected(tmp_path):
    text = "clients:\n" + entry("Example Client", "example-client") + entry("Example Client", "example-client-2")
    with pytest.raises(cc.ClientConfigError, match="entry 2: the client name 'Example Client' is already configured in entry 1"):
        cc.load_client_registry(write(tmp_path, text))


def test_duplicate_client_keys_are_rejected(tmp_path):
    text = "clients:\n" + entry("Example Client", "example-client") + entry("Other Co", "example-client")
    with pytest.raises(cc.ClientConfigError, match="entry 2: the client_key 'example-client' is already used by entry 1"):
        cc.load_client_registry(write(tmp_path, text))


def test_names_that_differ_only_in_case_are_distinct_exact_names(tmp_path):
    text = "clients:\n" + entry("ABC", "abc-1") + entry("abc", "abc-2")
    loaded = cc.load_client_registry(write(tmp_path, text))
    assert loaded.key_for("ABC") == "abc-1" and loaded.key_for("abc") == "abc-2"


# --- registry built directly ---------------------------------------------------------------------------------------------------

def test_a_registry_built_in_code_is_validated_the_same_way():
    assert cc.ClientRegistry([cc.ClientEntry("A", "a")]).key_for("A") == "a"
    for entries, message in (
        ([], "at least one client"),
        ([cc.ClientEntry("A", "a"), cc.ClientEntry("A", "b")], "already configured"),
        ([cc.ClientEntry("A", "a"), cc.ClientEntry("B", "a")], "already used"),
        ([cc.ClientEntry("A", "BAD KEY")], "is invalid"),
        ([cc.ClientEntry(" A", "a")], "whitespace"),
        (["not an entry"], "not a ClientEntry"),
    ):
        with pytest.raises(cc.ClientConfigError, match=message):
            cc.ClientRegistry(entries)


def test_entries_and_registry_are_immutable_views():
    registry = cc.ClientRegistry([cc.ClientEntry("A", "a")])
    with pytest.raises(AttributeError):
        registry.entries = ()
    with pytest.raises(Exception):
        registry.entries[0].client_key = "b"


def test_parse_client_config_accepts_parsed_data():
    parsed = cc.parse_client_config({"clients": [{"name": "A", "client_key": "a"}]})
    assert parsed.key_for("A") == "a"
    with pytest.raises(cc.ClientConfigError):
        cc.parse_client_config({"clients": [{"name": "A", "client_key": "a", "extra": 1}]})
