"""Stable client keys, configured by hand in a YAML file.

The file maps each exact Client Name from the Google Sheet to a permanent `client_key`:

    clients:
      - name: "Example Client"
        client_key: "example-client"

Keys are never generated or derived from names, and names are matched exactly: no case folding and no
whitespace normalization. A client key names report folders and snapshot history, so it must never be
changed once a snapshot or report exists for it.

A client key uses lowercase letters, digits, "-" and "_", starts with a letter or digit, and has 1 to 63
characters (the same format the snapshot store enforces). Names reserved by Windows for devices (CON,
NUL, COM1 and so on) are also refused, because a key is used as a directory name.

Nothing here reads configuration at import time, touches the network, or creates files or directories.
"""

from dataclasses import dataclass
from pathlib import Path

import yaml

from seo_report.snapshots.snapshot import CLIENT_KEY_RE

MAX_CONFIG_BYTES = 1_048_576
MAX_NAME_CHARS = 200
TOP_LEVEL_FIELDS = ("clients",)
ENTRY_FIELDS = ("name", "client_key")
WINDOWS_RESERVED_NAMES = frozenset(
    ["con", "prn", "aux", "nul"] + [f"com{i}" for i in range(1, 10)] + [f"lpt{i}" for i in range(1, 10)]
)


class ClientConfigError(ValueError):
    """The client configuration is missing, malformed or invalid."""


class UnknownClientError(ClientConfigError):
    """No client key is configured for the requested client name."""


@dataclass(frozen=True)
class ClientEntry:
    name: str
    client_key: str


def validate_client_name(name, *, where: str = "client name") -> str:
    """Return `name` if it is usable as an exact sheet client name, otherwise raise ClientConfigError."""
    if not isinstance(name, str):
        raise ClientConfigError(f"{where} must be text (put it in quotes), got {type(name).__name__}")
    if not name.strip():
        raise ClientConfigError(f"{where} must not be empty")
    if name != name.strip():
        raise ClientConfigError(f"{where} {name!r} must not start or end with whitespace; it has to match the "
                                "sheet's Client Name exactly")
    if len(name) > MAX_NAME_CHARS:
        raise ClientConfigError(f"{where} is longer than {MAX_NAME_CHARS} characters")
    if any(ord(char) < 32 or ord(char) == 127 for char in name):
        raise ClientConfigError(f"{where} {name!r} must not contain control characters")
    return name


def validate_client_key(key, *, where: str = "client_key") -> str:
    """Return `key` if it is a valid client key, otherwise raise ClientConfigError."""
    if not isinstance(key, str):
        raise ClientConfigError(f"{where} must be text (put it in quotes), got {type(key).__name__}")
    if not key:
        raise ClientConfigError(f"{where} must not be empty")
    if not CLIENT_KEY_RE.fullmatch(key):
        raise ClientConfigError(
            f"{where} {key!r} is invalid: use 1 to 63 lowercase letters, digits, '-' or '_', starting with a "
            "letter or digit")
    if key in WINDOWS_RESERVED_NAMES:
        raise ClientConfigError(f"{where} {key!r} is a reserved Windows device name and cannot be used as a "
                                "directory name")
    return key


class ClientRegistry:
    """The validated set of configured clients, with exact-name lookup."""

    def __init__(self, entries):
        entries = tuple(entries)
        if not entries:
            raise ClientConfigError("at least one client must be configured")
        by_name, by_key = {}, {}
        for position, entry in enumerate(entries, start=1):
            if not isinstance(entry, ClientEntry):
                raise ClientConfigError(f"entry {position} is not a ClientEntry")
            validate_client_name(entry.name, where=f"entry {position}: name")
            validate_client_key(entry.client_key, where=f"entry {position}: client_key")
            if entry.name in by_name:
                raise ClientConfigError(f"entry {position}: the client name {entry.name!r} is already configured "
                                        f"in entry {by_name[entry.name]}")
            if entry.client_key in by_key:
                raise ClientConfigError(f"entry {position}: the client_key {entry.client_key!r} is already used by "
                                        f"entry {by_key[entry.client_key]}")
            by_name[entry.name] = position
            by_key[entry.client_key] = position
        self._entries = entries
        self._keys = {entry.name: entry.client_key for entry in entries}

    @property
    def entries(self) -> tuple[ClientEntry, ...]:
        return self._entries

    def __len__(self) -> int:
        return len(self._entries)

    def key_for(self, client_name: str) -> str:
        """Return the configured key for an exact client name, or raise UnknownClientError."""
        if not isinstance(client_name, str):
            raise ClientConfigError(f"client name must be text, got {type(client_name).__name__}")
        try:
            return self._keys[client_name]
        except KeyError:
            pass
        message = (f"no client_key is configured for the client name {client_name!r}; add it to the client "
                   "configuration (names must match the sheet's Client Name exactly)")
        folded = " ".join(client_name.split()).casefold()
        similar = [entry.name for entry in self._entries if " ".join(entry.name.split()).casefold() == folded]
        if similar:  # a hint only: a near match is never used
            message += f"; a configured name that differs only in case or spacing exists: {similar[0]!r}"
        raise UnknownClientError(message)


class _StrictLoader(yaml.SafeLoader):
    """A safe YAML loader that refuses a mapping key written twice (PyYAML would keep the last one)."""


def _construct_mapping(loader, node, deep=False):
    seen = set()
    for key_node, _ in node.value:
        key = loader.construct_object(key_node, deep=deep)
        try:
            if key in seen:
                raise yaml.constructor.ConstructorError(
                    None, None, f"found a duplicate key {key!r}", key_node.start_mark)
            seen.add(key)
        except TypeError:  # an unhashable key: the standard loader reports it
            continue
    return yaml.SafeLoader.construct_mapping(loader, node, deep)


_StrictLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_mapping)


def _read_yaml(path: Path):
    label = path.name
    if not path.exists():
        raise ClientConfigError(f"client configuration file not found: {path}")
    if not path.is_file():
        raise ClientConfigError(f"client configuration path is not a file: {path}")
    try:
        size = path.stat().st_size
    except OSError as err:
        raise ClientConfigError(f"{label}: cannot read the file ({type(err).__name__})") from None
    if size > MAX_CONFIG_BYTES:
        raise ClientConfigError(f"{label}: the file is larger than {MAX_CONFIG_BYTES} bytes")
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        raise ClientConfigError(f"{label}: the file is not valid UTF-8 text") from None
    except OSError as err:
        raise ClientConfigError(f"{label}: cannot read the file ({type(err).__name__})") from None
    try:
        return yaml.load(text, Loader=_StrictLoader)
    except yaml.YAMLError as err:
        mark = getattr(err, "problem_mark", None)
        where = f" at line {mark.line + 1}, column {mark.column + 1}" if mark is not None else ""
        problem = getattr(err, "problem", None) or "invalid YAML"
        raise ClientConfigError(f"{label}: malformed YAML{where}: {problem}") from None


def parse_client_config(data) -> ClientRegistry:
    """Validate already-parsed configuration data and return the registry."""
    if data is None:
        raise ClientConfigError("the client configuration is empty")
    if not isinstance(data, dict):
        raise ClientConfigError(f"the client configuration must be a mapping with a 'clients' list, got "
                                f"{type(data).__name__}")
    unexpected = sorted(str(key) for key in data if key not in TOP_LEVEL_FIELDS)
    if unexpected:
        raise ClientConfigError(f"unexpected top-level field(s) {unexpected}; only {list(TOP_LEVEL_FIELDS)} "
                                "is allowed")
    if "clients" not in data:
        raise ClientConfigError("the client configuration needs a 'clients' list")
    clients = data["clients"]
    if not isinstance(clients, list):
        raise ClientConfigError(f"'clients' must be a list, got {type(clients).__name__}")
    if not clients:
        raise ClientConfigError("'clients' must list at least one client")

    entries = []
    for position, item in enumerate(clients, start=1):
        if not isinstance(item, dict):
            raise ClientConfigError(f"entry {position} must be a mapping with 'name' and 'client_key', got "
                                    f"{type(item).__name__}")
        missing = [field for field in ENTRY_FIELDS if field not in item]
        if missing:
            raise ClientConfigError(f"entry {position} is missing required field(s) {missing}")
        extra = sorted(str(field) for field in item if field not in ENTRY_FIELDS)
        if extra:
            raise ClientConfigError(f"entry {position} has unexpected field(s) {extra}; only "
                                    f"{list(ENTRY_FIELDS)} are allowed")
        entries.append(ClientEntry(
            validate_client_name(item["name"], where=f"entry {position}: name"),
            validate_client_key(item["client_key"], where=f"entry {position}: client_key")))
    return ClientRegistry(entries)


def load_client_registry(path) -> ClientRegistry:
    """Load and validate the client configuration from an explicitly supplied YAML path."""
    if path is None or str(path) == "":
        raise ClientConfigError("a path to the client configuration file is required")
    return parse_client_config(_read_yaml(Path(path)))
