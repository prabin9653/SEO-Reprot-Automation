import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import test_snapshot as ts
import test_snapshot_store as tss
from seo_report.snapshots import snapshot as snap
from seo_report.snapshots import store as st

mask = st.mask_event_detail

UUID = "01a18aac-9ef4-72ac-9487-5babee4e3089"
SHA256 = "abcd1234" * 8  # 64 hex characters
OPAQUE = "abcd1234" * 5  # 40 hex characters: not a SHA-256, so it counts as an opaque token


# --- values that must be preserved ----------------------------------------------------------------------------------

@pytest.mark.parametrize("text", [
    "baseline run-001 (2026-08-04 to 2026-08-31) was used",  # a run ID followed by an ISO date range
    "run-12 and run-000001",
    f"duplicate of {UUID}",
    f"duplicate of {UUID.upper()}",
    f"({UUID}) and [{UUID}]",
    f"hash {SHA256}",
    f"hash {SHA256.upper()} and sha256:{SHA256}",
    "reviewed on 2026-10-30",
    "period 2026-09-01 2026-09-28",
    "period 2026-09-01..2026-09-28 and 2026-09-01/2026-09-28",
    "rendered 2026-10-30T12:34:56Z",
    "rendered 2026-10-30T12:34:56.789Z",
    "rendered 2026-10-30T00:00:00+00:00",
    "rendered 2026-10-30T00:00:00-0700",
    "rendered 2026-10-30 12:34:56",
    "rendered 2026-10-30 12:34",
    f"duplicate of {UUID}, rendered 2026-10-30T12:00:00+00:00, hash {SHA256}, see run-007 (2026-08-04 to 2026-08-31)",
])
def test_run_ids_uuids_hashes_dates_and_timestamps_are_preserved(text):
    assert mask(text) == text


@pytest.mark.parametrize("text", [
    "approved after checking the keyword table",
    "checked 1,552 impressions and 6 clicks",
    "ticket 12345, order 98765432, count 1234567890",  # plain digit strings are not phone numbers
    "99.5% of $1,234.56 over 28 days",
    "version 3.14.7 build 20261030",
    "range 10 20 30 and 2026 2027",
    "a long-word-without-any-digits-" + "x" * 30,  # no digits: not an opaque token
    "1234567890123456789012345678901234567890",  # no letters: not an opaque token
])
def test_ordinary_text_and_numeric_values_are_preserved(text):
    assert mask(text) == text


# --- values that must be masked ----------------------------------------------------------------------------------------------

def test_email_addresses_are_masked():
    assert mask("contact a.person@example.test now") == "contact [email] now"
    assert mask("a@b.test, c.d+tag@sub.example.test") == "[email], [email]"


@pytest.mark.parametrize("text, expected", [
    ("call (281) 555-0100", "call [phone]"),
    ("call(281)555-0100", "call[phone]"),
    ("ref 281-555-0100", "ref [phone]"),
    ("281 555 0100", "[phone]"),
    ("281.555.0100", "[phone]"),
    ("+1 281 555 0100", "[phone]"),
    ("1-281-555-0100", "[phone]"),
    ("+44 20 7946 0958", "[phone]"),
    ("phone: 2815550100", "phone: [phone]"),
    ("call 2815550100 today", "call [phone] today"),
    ("fax 281 555 0100", "fax [phone]"),
    ("2026-10-30 (281) 555-0100", "2026-10-30 [phone]"),
])
def test_formatted_and_labelled_phone_numbers_are_masked(text, expected):
    assert mask(text) == expected


@pytest.mark.parametrize("text, expected", [
    ("ya29.A0ARrdaMfake_value-123", "[redacted]"),
    ("1//0gFakeRefresh-Value1", "[redacted]"),
    ("GOCSPX-fakefake123", "[redacted]"),
    ("AIza" + "A" * 30, "[redacted]"),
    ("eyJhbGci.eyJzdWIi.c2lnbmF0dXJl", "[redacted]"),
    ("Authorization: Bearer abc123DEF456ghi789", "Authorization: Bearer [redacted]"),
    ("password: hunter2xyz", "password: [redacted]"),
    ("client_secret=abcdef123456", "client_secret=[redacted]"),
    ("api key: abc123", "api key: [redacted]"),
    ("token=whatever", "token=[redacted]"),
    ("-----BEGIN PRIVATE KEY----- MIIEvQIBADANBgkq -----END PRIVATE KEY-----", "[redacted]"),
    (f"opaque {OPAQUE}", "opaque [redacted]"),
    ("secret " + "Ab1" * 12, "secret [redacted]"),
])
def test_secret_and_token_like_strings_are_masked(text, expected):
    assert mask(text) == expected


def test_a_secret_next_to_protected_values_is_still_masked():
    text = f"run-001 (2026-08-04 to 2026-08-31) emailed to a@b.test, call (281) 555-0100, id {UUID}, token=abc123xyz"
    assert mask(text) == (f"run-001 (2026-08-04 to 2026-08-31) emailed to [email], call [phone], id {UUID}, "
                          "token=[redacted]")


def test_a_hash_of_another_length_is_treated_as_an_opaque_token():
    assert mask("a " + OPAQUE) == "a [redacted]" and mask(f"a {SHA256}") == f"a {SHA256}"


# --- whitespace, empty values and robustness ---------------------------------------------------------------------------------

@pytest.mark.parametrize("value, expected", [
    ("  a   b \n c  ", "a b c"), ("   ", ""), ("", ""), ("\n\t ", ""), (None, None),
])
def test_whitespace_and_empty_details(value, expected):
    assert mask(value) == expected


def test_control_characters_cannot_forge_placeholders():
    assert mask("x\x000\x00y") == "x0y"
    assert mask("\x000\x00 2026-10-30") == "0 2026-10-30"
    assert mask(f"\x001\x00 {UUID} 2026-10-30") == f"1 {UUID} 2026-10-30"


@pytest.mark.parametrize("text", [
    f"run-001 (2026-08-04 to 2026-08-31) {UUID} {SHA256} a@b.test (281) 555-0100 ya29.A0ARrdaMfake_value-123 {OPAQUE}",
    "plain text", "", "   ",
])
def test_masking_is_idempotent(text):
    assert mask(mask(text)) == mask(text)


def test_non_text_values_are_converted_to_text():
    assert mask(12345) == "12345" and mask(2026) == "2026"


# --- add_event ---------------------------------------------------------------------------------------------------------------

@pytest.fixture
def db(tmp_path):
    return tmp_path / "history" / "seo_reports.sqlite"


@pytest.fixture
def store(db):
    with tss.make_store(db) as opened:
        opened.write(ts.build())
        yield opened


@pytest.mark.parametrize("kind", ["rendered", "reviewed", "delivered", "voided"])
def test_every_event_kind_preserves_run_ids_hashes_and_dates(store, kind):
    detail = f"run-001 (2026-08-04 to 2026-08-31) {UUID} {SHA256} 2026-10-30T12:00:00+00:00"
    event = store.add_event("run-001", kind, detail)
    assert event.kind == kind and event.detail == detail
    assert store.events("run-001")[-1].detail == detail


@pytest.mark.parametrize("kind", ["rendered", "reviewed", "delivered", "voided"])
def test_every_event_kind_masks_sensitive_text(store, kind):
    event = store.add_event("run-001", kind, "by a.person@example.test, call (281) 555-0100, token=abc123xyz")
    assert event.detail == "by [email], call [phone], token=[redacted]"
    assert store.events("run-001")[-1].detail == event.detail


def test_voided_events_still_need_a_reason(store):
    for reason in (None, "", "   ", "\n\t"):
        with pytest.raises(ValueError, match="reason"):
            store.add_event("run-001", "voided", reason)
    assert store.events("run-001") == () and store.get("run-001").voided is False
    event = store.add_event("run-001", "voided", "entered by mistake: run-009 (2026-08-04 to 2026-08-31)")
    assert event.detail == "entered by mistake: run-009 (2026-08-04 to 2026-08-31)"
    assert store.get("run-001").voided is True


def test_a_reason_made_only_of_a_secret_is_accepted_as_redacted(store):
    assert store.add_event("run-001", "voided", "ya29.A0ARrdaMfake_value-123").detail == "[redacted]"


def test_optional_details_keep_their_existing_behavior(store):
    assert store.add_event("run-001", "rendered").detail is None
    assert store.add_event("run-001", "reviewed", None).detail is None
    assert store.add_event("run-001", "delivered", "   ").detail == ""  # whitespace-only is stored as empty text


def test_event_kinds_and_run_checks_are_unchanged(store):
    with pytest.raises(ValueError, match="kind"):
        store.add_event("run-001", "edited", "x")
    with pytest.raises(ValueError, match="kind"):
        store.add_event("run-001", "superseded", "x")  # recorded by the store itself
    with pytest.raises(st.SnapshotStoreError, match="no snapshot"):
        store.add_event("missing", "reviewed", "x")
    assert store.events("run-001") == ()


def test_the_automatic_superseded_event_keeps_the_new_run_id_exactly(tmp_path):
    with st.SnapshotStore(tmp_path / "real" / "h.sqlite") as real:  # real UUID run IDs
        first = real.write(ts.build())
        second = real.write(ts.build(keywords=[ts.te.kw("changed", "", 3, "X")]))
        assert second.superseded == first.run_id and len(second.run_id) == 36
        assert [(e.kind, e.detail) for e in real.events(first.run_id)] == [("superseded", second.run_id)]
        note = real.add_event(first.run_id, "reviewed", f"superseded by {second.run_id} on 2026-10-30")
        assert note.detail == f"superseded by {second.run_id} on 2026-10-30"  # UUID and date survive
        assert real.verify().ok


def test_events_do_not_affect_hashes_the_chain_or_verification(store):
    before = store.get("run-001")
    store.write(ts.build(client_key="other-co"))
    chain_before = store.get("run-002").chain_hash
    for kind, detail in (("rendered", f"{UUID} {SHA256} 2026-10-30T12:00:00+00:00"), ("reviewed", "a@b.test"),
                         ("delivered", None), ("voided", "run-001 (2026-08-04 to 2026-08-31)")):
        store.add_event("run-001", kind, detail)
    after = store.get("run-001")
    assert (after.content_hash, after.chain_hash, after.prev_chain_hash, after.payload) == (
        before.content_hash, before.chain_hash, before.prev_chain_hash, before.payload)
    assert store.get("run-002").chain_hash == chain_before
    assert store.verify().ok and store.verify_run("run-001").ok


def test_events_remain_append_only(db, store):
    store.add_event("run-001", "reviewed", "ok")
    other = sqlite3.connect(db, isolation_level=None)
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        other.execute("UPDATE run_events SET detail = 'edited'")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        other.execute("DELETE FROM run_events")
    other.close()


# --- the payload sanitizer is unchanged -------------------------------------------------------------------------------------------

def test_the_snapshot_payload_sanitizer_is_unchanged():
    """These inputs are corrupted by the general sanitizer. Snapshot payload text, and so content hashes, depend on
    that behavior, so it is pinned here and was deliberately not touched by the event-detail fix."""
    assert snap.mask_text("baseline run-001 (2026-08-04 to 2026-08-31) was used") == (
        "baseline run-[phone] to 2026-08-31) was used")
    assert snap.mask_text(f"duplicate of {UUID}") == "duplicate of [redacted]"
    assert snap.mask_text("a@b.test 281 555 0100 " + "x" * 40) == "[email] [phone] [redacted]"
    assert snap.mask_text("reviewed on 2026-10-30") == "reviewed on 2026-10-30"


def test_only_add_event_uses_the_new_masker():
    source = (ROOT / "src" / "seo_report" / "snapshots" / "store.py").read_text(encoding="utf-8")
    assert source.count("mask_event_detail(") == 2  # the definition and the single call in add_event
    assert "snap.mask_text(" not in source  # the general sanitizer is no longer called from the store
    snapshot_source = (ROOT / "src" / "seo_report" / "snapshots" / "snapshot.py").read_text(encoding="utf-8")
    assert "mask_event_detail" not in snapshot_source
