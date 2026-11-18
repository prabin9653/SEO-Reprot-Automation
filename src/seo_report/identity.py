"""Who is running a command: a short, safe label for the person at this computer (stage 5).

The label goes into the local ledger (`requested_by`) and, when a shared activity folder is configured, into the team's activity log. It is the Windows account name
(or the value of `JASPER_SEO_OPERATOR`, for a shared Windows account), reduced to the characters the ledger accepts. It is never an e-mail address and never read
from a credential. `JASPER_SEO_SOURCE=web` is set by the web page so a run says whether it was started from the page or from the console.
"""

import getpass
import re

ENV_OPERATOR = "JASPER_SEO_OPERATOR"
ENV_SOURCE = "JASPER_SEO_SOURCE"
_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")


def _clean(text, limit: int) -> str:
    cleaned = _UNSAFE.sub("_", str(text or "")).strip("._-")[:limit].strip("._-")
    return cleaned


def operator_name(environ) -> str:
    """The operator's name for this computer: `JASPER_SEO_OPERATOR`, else the Windows account name, else `unknown`. At most 40 characters of `A-Za-z0-9._-`."""
    for candidate in (environ.get(ENV_OPERATOR), environ.get("USERNAME")):
        cleaned = _clean(candidate, 40)
        if cleaned and "@" not in str(candidate):  # an address is not a name; never record one
            return cleaned
    try:
        return _clean(getpass.getuser(), 40) or "unknown"
    except Exception:  # no user name can be determined (a service, a container)
        return "unknown"


def device_name(environ) -> str:
    return _clean(environ.get("COMPUTERNAME"), 40) or "unknown-device"


def source(environ) -> str:
    return "web" if environ.get(ENV_SOURCE) == "web" else "cli"


def requested_by(environ) -> str:
    """`cli:name` or `web:name`: fits the ledger's `requested_by` pattern (letters, digits and : . _ - , at most 64 characters)."""
    return f"{source(environ)}:{operator_name(environ)}"


def generated_by(requests) -> str | None:
    """Who generated a report, from the ledger's `run`/`retry` requests of that run (oldest first): the person whose request FINISHED it (outcome `succeeded`; later
    requests that only found it `already_complete` are not the author). None when no request finished it. The result is the person part of the label."""
    import json
    author = None
    for request in requests or ():
        try:
            outcome = (json.loads(request.response_json) or {}).get("outcome") if request.response_json else None
        except (ValueError, AttributeError, TypeError):
            outcome = None
        if outcome == "succeeded":
            author = request.requested_by
    return display_name(author) if author else None


def display_name(requested_by_value) -> str:
    """The person part of a `requested_by` label (`web:maria` -> `maria`); anything unrecognised is shown as is."""
    text = str(requested_by_value or "")
    head, sep, tail = text.partition(":")
    return tail if sep and head in ("cli", "web") and tail else text
