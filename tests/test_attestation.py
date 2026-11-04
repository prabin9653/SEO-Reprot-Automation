"""R8.7c: the live-verification attestation and its place in the production gate.

Everything here is SYNTHETIC. No Google call, sign-in or browser is possible (sockets and the sign-in flow are forbidden), no real attestation exists or is
created: every attestation is written into a temporary `%LOCALAPPDATA%` and is protected either by a fake protector or, in two tests, by the real Windows
DPAPI over synthetic data. The fakes (protector, identity, ACL reader) are Python-level test seams that no command-line argument or environment variable can reach.
"""
import datetime as dt
import hashlib
import hmac
import json
import os
import socket
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from seo_report import attestation as att
from seo_report import auth, cli, client_loader
from seo_report import preflight as pf
from seo_report import runner_config as rc
from seo_report.cli_exit import Exit
from test_pipeline import default_collect, make_client
from test_r86d_report_status_recovery import PERIOD, PROBES_OK, RUN, NoGoogle, home, loaded, tree  # noqa: F401  (home is a fixture)
from test_render_artifacts import StubRenderer

UTC = dt.timezone.utc
NOW = dt.datetime.now(UTC).replace(microsecond=0)
GUID, SID = "11111111-2222-3333-4444-555555555555", "S-1-5-21-1111-2222-3333-1001"
SAFE_SDDL = f"O:{SID}D:PAI(A;OICI;FA;;;{SID})(A;OICI;FA;;;SY)"
requires_windows = pytest.mark.skipif(os.name != "nt", reason="Windows DPAPI")


def IDENT():
    return GUID, SID


def ACL(path):
    return SAFE_SDDL


class FakeProtector:
    """A MAC instead of DPAPI: any change to the bytes, or a different entropy (= a different machine/account binding), makes `unprotect` fail."""

    def __init__(self, bind_entropy=True):
        self.bind = bind_entropy

    def _mac(self, data, entropy):
        return hmac.new(hashlib.sha256(entropy if self.bind else b"").digest(), data, "sha256").digest()

    def protect(self, data, entropy):
        return self._mac(data, entropy) + data

    def unprotect(self, blob, entropy):
        mac, data = blob[:32], blob[32:]
        if len(blob) < 32 or not hmac.compare_digest(mac, self._mac(data, entropy)):
            raise att.AttestationError("attestation_unprotect_failed")
        return data


SEAMS = dict(protector=FakeProtector(), identity=IDENT, acl_reader=ACL)
GATE_SEAMS = {"attestation_protector": SEAMS["protector"], "attestation_identity": IDENT, "attestation_acl_reader": ACL}


@pytest.fixture(autouse=True)
def nothing_leaves_the_machine(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("a network connection was attempted")
    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    import google_auth_oauthlib.flow as flow
    import webbrowser
    monkeypatch.setattr(flow.InstalledAppFlow, "run_local_server", lambda *a, **k: (_ for _ in ()).throw(AssertionError("sign-in flow started")))
    monkeypatch.setattr(webbrowser, "open", lambda *a, **k: (_ for _ in ()).throw(AssertionError("a browser was opened")))


def checks(at=None, **overrides):
    at = at or NOW - dt.timedelta(minutes=1)
    items = {"oauth_refresh": "ok", "sheets_read": "ok", "search_console_read": "ok", "ga4_read": "ok", **overrides}
    return [att.CheckResult(service, outcome, at) for service, outcome in items.items()]


def client_evidence(name="Alpha Co", **overrides):
    client = make_client(name, **overrides)
    return att.ClientEvidence(name.lower().replace(" ", "-"), client.gsc_property, client.ga4_property_id)


def evidence(settings, *, factory="from_live_checks", at=None, clients=None, workbook=None, **check_overrides):
    return getattr(att.VerificationEvidence, factory)(checks=checks(at, **check_overrides), clients=clients if clients is not None else [client_evidence()],
                                                     workbook_id=workbook if workbook is not None else settings.reporting_spreadsheet_id)


def issue(home, settings=None, *, now=NOW, ev=None, **seams):
    settings = settings or home.settings
    return att.issue_attestation(ev or evidence(settings), settings, environ=home.env, now=now, acl_applier=lambda *a: None, **{**SEAMS, **seams})


def check(home, settings=None, *, now=NOW, **seams):
    return att.evaluate(settings or home.settings, home.env, now=now, **{**SEAMS, **seams})


def stored(home):
    return att.store_path(home.env)


def rewrite(home, mutate, protector=None):
    """Decrypt the stored record with the fake protector, change the payload, protect it again (a hand-made record)."""
    protector, path = protector or SEAMS["protector"], stored(home)
    entropy = att._entropy(att.binding_of(GUID, SID))
    payload = json.loads(protector.unprotect(path.read_bytes()[len(att.FILE_MAGIC) + 1:], entropy))
    mutate(payload)
    path.write_bytes(att.FILE_MAGIC + bytes([att.FILE_FORMAT]) + protector.protect(json.dumps(payload).encode(), entropy))


def in_project_settings(home):
    """Same settings but with no OAuth locations configured: the credentials fall back to the project folder (`credentials_not_relocated`)."""
    import yaml
    data = json.loads(json.dumps(home.data))
    data["paths"] = {k: v for k, v in data["paths"].items() if not k.startswith("oauth_")}
    path = home.tmp / "runner_in_project.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return rc.load_settings(path, environ=home.env)


def gate_inputs(home, settings=None, **extra):
    return pf.Inputs(settings=settings or home.settings, environ=home.env, app_version="8", probes={**PROBES_OK, **GATE_SEAMS}, clock=lambda: NOW,
                     logging_status={"active": True, "code": None}, launch_browser=False, **extra)


def blockers(inp):
    allowed, code, items = pf.production_gate(inp)
    return allowed, sorted(b.code for b in items)


# === validity ======================================================================================================================

def test_a_valid_attestation_is_accepted(home):
    verdict = issue(home)
    assert verdict.valid and verdict.code == "attestation_valid" and verdict.status == "valid"
    assert verdict.issued_at == att._stamp(NOW) and verdict.expires_at == att._stamp(NOW + dt.timedelta(days=7)) and verdict.age_seconds == 0
    assert verdict.clients == (("alpha-co", att.fingerprint("https://example.test/"), att.fingerprint("123456789")),)
    assert check(home).valid and check(home, now=NOW + dt.timedelta(days=7) - dt.timedelta(seconds=1)).valid


def test_the_schema_and_the_evidence_of_the_stored_record(home):
    issue(home)
    payload = {}
    rewrite(home, payload.update)  # captures the decrypted payload unchanged
    assert payload["schema"] == "jasper.live-verification-attestation" and payload["version"] == 1 and payload["origin"] == "live"
    assert sorted(c["service"] for c in payload["checks"]) == sorted(att.REQUIRED_CHECKS) and all(c["outcome"] in ("ok", "not_needed") for c in payload["checks"])
    assert att._parse_stamp(payload["expires_at"]) - att._parse_stamp(payload["issued_at"]) == dt.timedelta(days=7) == att.VALIDITY
    assert payload["binding"] == att.binding_of(GUID, SID) and len(payload["binding"]) == 32
    text = json.dumps(payload)
    for secret in (GUID, SID, "https://example.test/", "123456789", home.settings.reporting_spreadsheet_id, str(home.creds)):
        assert secret not in text, "raw identifiers must not be stored"
    assert payload["clients"] == [{"client_key": "alpha-co", "gsc16": att.fingerprint("https://example.test/"), "ga416": att.fingerprint("123456789")}]


def test_the_token_refresh_may_be_not_needed_but_every_other_check_must_be_ok(home):
    assert issue(home, ev=evidence(home.settings, oauth_refresh="not_needed")).valid
    with pytest.raises(att.AttestationError) as error:
        issue(home, ev=evidence(home.settings, ga4_read="not_needed"))
    assert error.value.code == "attestation_incomplete_evidence"


@requires_windows
def test_real_dpapi_identity_acl_and_storage_work_together_on_synthetic_data(home):
    verdict = att.issue_attestation(evidence(home.settings), home.settings, environ=home.env, now=NOW)  # real DPAPI, real identity, real private ACL
    assert verdict.valid
    path = stored(home)
    assert path.read_bytes().startswith(att.FILE_MAGIC) and b"alpha-co" not in path.read_bytes() and b"live" not in path.read_bytes()  # really encrypted
    assert att.evaluate(home.settings, home.env, now=NOW).valid
    raw = bytearray(path.read_bytes())
    raw[-5] ^= 0x01
    path.write_bytes(bytes(raw))
    assert att.evaluate(home.settings, home.env, now=NOW).code == "attestation_unprotect_failed"  # DPAPI detects the change


@requires_windows
def test_dpapi_round_trip_tamper_and_entropy(home):
    protector = att.DpapiProtector()
    blob = protector.protect(b"synthetic payload", b"entropy-a")
    assert protector.unprotect(blob, b"entropy-a") == b"synthetic payload" and b"synthetic" not in blob
    for bad in (blob[:-1] + bytes([blob[-1] ^ 1]), blob[:20]):
        with pytest.raises(att.AttestationError) as error:
            protector.unprotect(bad, b"entropy-a")
        assert error.value.code == "attestation_unprotect_failed"
    with pytest.raises(att.AttestationError):
        protector.unprotect(blob, b"entropy-b")


@requires_windows
def test_the_real_acl_helpers_make_a_folder_private_and_detect_a_broad_grant(tmp_path):
    guid, sid = att.windows_identity()
    assert guid and sid.startswith("S-1-5-") and att.binding_of(guid, sid) == att.binding_of(guid, sid) and guid not in att.binding_of(guid, sid)
    folder = tmp_path / "private"
    folder.mkdir()
    att.apply_private_acl(folder, sid)
    assert not att.unsafe_acl(att.read_sddl(folder), sid) and "PAI" in att.read_sddl(folder) or "P" in att.read_sddl(folder).split("D:")[1][:3]
    assert att.unsafe_acl(att.read_sddl(folder).replace(f";;;{sid})", ";;;WD)"), sid)  # the same list with Everyone instead of the user


# === rejection =====================================================================================================================

def test_a_missing_attestation_is_missing_and_creates_nothing(home):
    before = tree(home.tmp)
    verdict = check(home)
    assert (verdict.status, verdict.code, verdict.valid) == ("missing", "attestation_missing", False)
    assert tree(home.tmp) == before and not (home.local / "JasperSEOReporting").exists()


def test_an_expired_attestation_is_rejected_at_the_exact_expiry(home):
    issue(home)
    for moment in (NOW + dt.timedelta(days=7), NOW + dt.timedelta(days=30)):
        assert check(home, now=moment).code == "attestation_expired"


def test_a_future_dated_attestation_is_rejected(home):
    issue(home, now=NOW + dt.timedelta(hours=1), ev=evidence(home.settings, at=NOW + dt.timedelta(minutes=59)))
    assert check(home, now=NOW).code == "attestation_future"
    issue(home, now=NOW + dt.timedelta(minutes=4), ev=evidence(home.settings, at=NOW + dt.timedelta(minutes=3)))
    assert check(home, now=NOW).valid  # within the five-minute tolerance


def test_a_clock_that_moved_backwards_is_not_trusted(home):
    issue(home)
    assert check(home, now=NOW - dt.timedelta(hours=1)).code == "attestation_clock_suspect"  # the file is newer than "now"
    path = stored(home)
    os.utime(path, (NOW.timestamp() + 7200, NOW.timestamp() + 7200))  # touching the file forward does not help
    assert check(home).code == "attestation_clock_suspect"
    os.utime(path, (NOW.timestamp() - 86400, NOW.timestamp() - 86400))  # nor does touching it backwards extend anything
    assert check(home, now=NOW + dt.timedelta(days=8)).code == "attestation_expired"


def test_corrupted_truncated_or_foreign_files_are_rejected(home):
    issue(home)
    path, good = stored(home), stored(home).read_bytes()
    for offset in (len(att.FILE_MAGIC) + 1, len(att.FILE_MAGIC) + 40, len(good) // 2, len(good) - 1):  # tampering anywhere in the protected data
        raw = bytearray(good)
        raw[offset] ^= 0x40
        path.write_bytes(bytes(raw))
        assert check(home).code == "attestation_unprotect_failed", offset
    path.write_bytes(good[:-3])
    assert check(home).code == "attestation_unprotect_failed"
    path.write_bytes(b"JASPERATT")
    assert check(home).code == "attestation_corrupt"
    path.write_bytes(b"x" * 100)
    assert check(home).code == "attestation_corrupt"
    path.write_bytes(good[:len(att.FILE_MAGIC)] + bytes([9]) + good[len(att.FILE_MAGIC) + 1:])
    assert check(home).code == "attestation_unsupported_version"
    path.write_bytes(b"A" * (att.MAX_FILE_BYTES + 1))
    assert check(home).code == "attestation_corrupt"
    path.write_bytes(good)
    assert check(home).valid
    rewrite(home, lambda p: p.pop("clients"))
    assert check(home).code == "attestation_incomplete_evidence"
    rewrite(home, lambda p: p.update(clients=[{"client_key": "alpha-co", "gsc16": "nothex", "ga416": "x"}]))
    assert check(home).code == "attestation_corrupt"


def test_a_payload_that_is_not_json_or_not_an_object_is_corrupt(home):
    issue(home)
    path, entropy = stored(home), att._entropy(att.binding_of(GUID, SID))
    for plain in (b"\xff\xfe not json", b"[1, 2]", b"\"text\""):
        path.write_bytes(att.FILE_MAGIC + bytes([1]) + SEAMS["protector"].protect(plain, entropy))
        assert check(home).code == "attestation_corrupt"


def test_the_wrong_machine_or_account_is_rejected(home):
    issue(home)
    assert check(home, identity=lambda: ("99999999-0000-0000-0000-000000000000", SID)).code == "attestation_unprotect_failed"  # other machine
    acl9 = lambda p: SAFE_SDDL.replace(SID, "S-1-5-21-9-9-9-1002")  # noqa: E731  (the other account sees its own, private ACL)
    assert check(home, identity=lambda: (GUID, "S-1-5-21-9-9-9-1002"), acl_reader=acl9).code == "attestation_unprotect_failed"  # other account
    assert check(home, identity=lambda: (GUID, "S-1-5-21-9-9-9-1002")).code == "attestation_permissions_unsafe"  # and the file is not private to it
    lenient = FakeProtector(bind_entropy=False)  # even if the protection layer let it through, the stored binding still refuses it
    issue(home, protector=lenient)
    assert check(home, protector=lenient, identity=lambda: ("99999999-0000-0000-0000-000000000000", SID)).code == "attestation_binding_mismatch"
    assert check(home, protector=lenient, identity=lambda: (GUID, "S-1-5-21-9-9-9-1002"), acl_reader=acl9).code == "attestation_binding_mismatch"


def test_an_unavailable_identity_or_protection_fails_closed(home):
    issue(home)

    def broken():
        raise att.AttestationError("attestation_identity_unavailable")
    assert check(home, identity=broken).code == "attestation_identity_unavailable"
    assert check(home, identity=lambda: (GUID, "not-a-sid")).code != "attestation_valid"

    class NoDpapi:
        def unprotect(self, blob, entropy):
            raise att.AttestationError("attestation_protection_unavailable")
    assert check(home, protector=NoDpapi()).code == "attestation_protection_unavailable"
    assert check(home, identity=lambda: (_ for _ in ()).throw(RuntimeError("boom"))).code == "attestation_check_failed"


def test_an_unsupported_schema_version_is_rejected(home):
    issue(home)
    rewrite(home, lambda p: p.update(version=2))
    assert check(home).code == "attestation_unsupported_version"
    rewrite(home, lambda p: p.update(version=1, schema="jasper.something-else"))
    assert check(home).code == "attestation_unsupported_version"


def test_synthetic_or_unlabelled_origin_is_rejected(home):
    issue(home)
    for origin in ("synthetic", "", None, "LIVE"):
        rewrite(home, lambda p, o=origin: p.update(origin=o))
        assert check(home).code == "attestation_not_live", origin
    with pytest.raises(att.AttestationError) as error:
        issue(home, ev=evidence(home.settings, factory="synthetic"))
    assert error.value.code == "attestation_not_live"
    with pytest.raises(att.AttestationError) as error:
        issue(home, ev={"origin": "live"})
    assert error.value.code == "attestation_not_live"


def test_evidence_must_cover_every_required_service_freshly_and_consistently(home):
    settings = home.settings
    cases = {
        "missing service": [c for c in checks() if c.service != "search_console_read"],
        "failed check": checks(ga4_read="failed"),
        "stale": checks(NOW - dt.timedelta(hours=2)),
        "future": checks(NOW + dt.timedelta(hours=1)),
        "spread out": [att.CheckResult(c.service, c.outcome, NOW - dt.timedelta(minutes=(50 if i == 0 else 1))) for i, c in enumerate(checks())],
        "naive time": [att.CheckResult("oauth_refresh", "ok", dt.datetime(2026, 1, 1))] + checks()[1:]}
    for label, items in cases.items():
        with pytest.raises(att.AttestationError) as error:
            issue(home, ev=att.VerificationEvidence.from_live_checks(checks=items, clients=[client_evidence()], workbook_id=settings.reporting_spreadsheet_id))
        assert error.value.code == "attestation_incomplete_evidence", label
    for label, kwargs in {"no clients": dict(clients=[]), "duplicate client": dict(clients=[client_evidence(), client_evidence()]),
                          "empty property": dict(clients=[att.ClientEvidence("alpha-co", "", "1")]),
                          "bad key": dict(clients=[att.ClientEvidence("Bad Key", "a", "1")]), "no workbook": dict(workbook=" ")}.items():
        with pytest.raises(att.AttestationError) as error:
            issue(home, ev=evidence(settings, **kwargs))
        assert error.value.code == "attestation_incomplete_evidence", label
    with pytest.raises(att.AttestationError) as error:
        issue(home, ev=att.VerificationEvidence.from_live_checks(checks=checks(), clients=[client_evidence()], workbook_id=settings.reporting_spreadsheet_id,
                                                                 scopes=["https://www.googleapis.com/auth/drive"]))
    assert error.value.code == "attestation_scopes_changed"
    assert not stored(home).exists()  # nothing was written for any refused evidence


def test_a_stored_record_without_complete_evidence_is_rejected(home):
    issue(home)
    rewrite(home, lambda p: p.update(checks=[c for c in p["checks"] if c["service"] != "ga4_read"]))
    assert check(home).code == "attestation_incomplete_evidence"
    rewrite(home, lambda p: p.update(checks=[dict(c, outcome="failed") for c in p["checks"]]))
    assert check(home).code == "attestation_incomplete_evidence"
    rewrite(home, lambda p: p.update(checks="none"))
    assert check(home).code == "attestation_incomplete_evidence"


def test_an_altered_validity_period_is_rejected(home):
    issue(home)
    rewrite(home, lambda p: p.update(expires_at=att._stamp(NOW + dt.timedelta(days=30))))
    assert check(home).code == "attestation_validity_invalid"
    rewrite(home, lambda p: p.update(expires_at="2026-13-45T00:00:00+00:00"))
    assert check(home).code == "attestation_corrupt"


def test_a_changed_workbook_credential_location_or_scope_set_invalidates_the_attestation(home):
    issue(home)
    import yaml
    other = json.loads(json.dumps(home.data))
    other["workbooks"]["reporting_spreadsheet_id"] = "1ZzYyXxWwVvUuTtSsRrQqPpOoNnMmLlKkJjIiHh"
    path = home.tmp / "other_workbook.yaml"
    path.write_text(yaml.safe_dump(other), encoding="utf-8")
    assert check(home, rc.load_settings(path, environ=home.env)).code == "attestation_workbook_changed"
    moved = json.loads(json.dumps(home.data))
    moved["paths"]["oauth_cache_file"] = str(home.creds / "elsewhere.json")
    path = home.tmp / "other_cache.yaml"
    path.write_text(yaml.safe_dump(moved), encoding="utf-8")
    assert check(home, rc.load_settings(path, environ=home.env)).code == "attestation_credentials_changed"
    assert check(home, in_project_settings(home)).code == "attestation_credentials_changed"
    assert check(home).valid
    rewrite(home, lambda p: p.update(scopes16="0" * 16))
    assert check(home).code == "attestation_scopes_changed"
    with pytest.raises(att.AttestationError) as error:
        issue(home, ev=evidence(home.settings, workbook="1ZzYyXxWwVvUuTtSsRrQqPpOoNnMmLlKkJjIiHh"))
    assert error.value.code == "attestation_workbook_changed"


def test_client_and_property_coverage(home):
    verdict = issue(home)
    ok = att.client_coverage_code
    assert ok(verdict, "alpha-co", "https://example.test/", "123456789") is None
    assert ok(verdict, "beta-co", "https://example.test/", "123456789") == "attestation_client_not_covered"
    assert ok(verdict, "alpha-co", "https://other.test/", "123456789") == "attestation_property_changed"
    assert ok(verdict, "alpha-co", "https://example.test/", "999") == "attestation_property_changed"
    assert ok(att.Verdict("missing", "attestation_missing"), "alpha-co", "a", "1") == "attestation_missing"
    assert ok(att.Verdict("invalid", "attestation_expired"), "alpha-co", "a", "1") == "attestation_expired"


# === storage and permissions =======================================================================================================

def test_unsafe_storage_locations_are_refused(home, tmp_path):
    for environ, label in (({}, "no LOCALAPPDATA"), ({"LOCALAPPDATA": str(ROOT / "work")}, "inside the project"),
                           ({"LOCALAPPDATA": str(tmp_path / "OneDrive" / "local")}, "inside OneDrive"),
                           ({"LOCALAPPDATA": "relative\\path"}, "relative"), ({"LOCALAPPDATA": "\\\\server\\share\\x"}, "network share")):
        with pytest.raises(att.AttestationError) as error:
            att.store_path(environ)
        assert error.value.code == "attestation_location_unsafe", label
        assert att.evaluate(home.settings, environ, now=NOW, **SEAMS).code == "attestation_location_unsafe", label
        with pytest.raises(att.AttestationError):
            att.issue_attestation(evidence(home.settings), home.settings, environ=environ, now=NOW, acl_applier=lambda *a: None, **SEAMS)
    assert not (ROOT / "work").exists() and not (tmp_path / "OneDrive").exists()
    assert "attestation" in str(stored(home)).lower() and str(home.local) in str(stored(home))  # the normal location is under LOCALAPPDATA


def test_the_attestation_path_is_never_in_the_project_or_onedrive():
    # LOCALAPPDATA is the only input; the file name and folder are fixed in code
    assert att.FOLDER_NAME == "attestation" and att.FILE_NAME == "live_verification.attest"
    source = (ROOT / "src" / "seo_report" / "attestation.py").read_text(encoding="utf-8")
    assert "PROJECT_ROOT" in source and "in_cloud_sync_folder" in source and "Path.home" not in source


def test_inaccessible_storage_fails_closed(home, monkeypatch):
    issue(home)
    path = stored(home)
    real_lstat = os.lstat

    def denied(target, *a, **k):
        if os.path.normcase(str(target)) == os.path.normcase(str(path)):
            raise PermissionError("denied")
        return real_lstat(target, *a, **k)
    monkeypatch.setattr(os, "lstat", denied)
    assert check(home).code == "attestation_inaccessible"
    monkeypatch.undo()
    real_open = open

    def unreadable(target, mode="r", *a, **k):
        if os.path.normcase(str(target)) == os.path.normcase(str(path)) and "b" in mode:
            raise PermissionError("denied")
        return real_open(target, mode, *a, **k)
    monkeypatch.setattr("builtins.open", unreadable)
    assert check(home).code == "attestation_inaccessible"


def test_a_directory_or_other_non_regular_file_in_place_of_the_record_is_refused(home):
    path = att.store_path(home.env)
    path.mkdir(parents=True)
    assert check(home).code == "attestation_location_unsafe"


def test_file_and_folder_permissions_must_be_private(home):
    issue(home)
    broad = [f"O:{SID}D:(A;;FA;;;WD)", f"O:{SID}D:PAI(A;;FA;;;{SID})(A;;FR;;;BU)", f"O:{SID}D:(A;;FA;;;AU)", f"O:{SID}D:(A;;FA;;;S-1-5-21-5-5-5-1100)",
             f"O:{SID}", "garbage", "O:S-1-5-21-7-7-7-1000D:(A;;FA;;;" + SID + ")"]
    for sddl in broad:
        assert check(home, acl_reader=lambda p, s=sddl: s).code == "attestation_permissions_unsafe", sddl
    assert check(home, acl_reader=lambda p: SAFE_SDDL.replace("SY", "BA")).valid  # administrators and SYSTEM are accepted
    only_folder = lambda p: (SAFE_SDDL if Path(p).name == att.FILE_NAME else f"O:{SID}D:(A;;FA;;;WD)")  # noqa: E731
    assert check(home, acl_reader=only_folder).code == "attestation_permissions_unsafe"  # the folder is judged as well as the file
    assert check(home, acl_reader=lambda p: f"O:{SID}D:(D;;FA;;;WD)(A;;FA;;;{SID})").valid  # a deny entry never makes it unsafe


# === ACL parser: fail-closed regression (R8.7c.1) ================================================================================

def sddl(*aces, owner=SID, flags="PAI"):
    return f"O:{owner}D:{flags}" + "".join(aces)


MINE = f"(A;OICI;FA;;;{SID})"


@pytest.mark.parametrize("ace", [
    "(XA;;FA;;;WD;(Member_of{SID(WD)}))",  # conditional grant to Everyone
    f"(XA;;FA;;;{SID};(Member_of{{SID(BA)}}))",  # a conditional grant even to a permitted trustee is not understood
    "(XD;;FA;;;WD;(Member_of{SID(WD)}))", "(ZA;;FA;;;WD;(Member_of{SID(WD)}))", f"(ZA;;FA;;;{SID})",  # unknown / callback types
    f"(AU;SA;FA;;;{SID})", f"(AL;;FA;;;{SID})", f"(ML;;NW;;;S-1-16-4096)", f"(RA;;;;;WD;(\"Project\",TS,0,\"Windows\"))", f"(SP;;;;;{SID})",
    f"(Q;;FA;;;{SID})", f"(;;FA;;;{SID})"])
def test_conditional_unknown_and_foreign_ace_types_are_unsafe(ace):
    assert att.unsafe_acl(sddl(MINE, ace), SID), ace  # unsafe even when a perfectly good entry is also present
    assert att.unsafe_acl(sddl(ace, MINE), SID), ace


@pytest.mark.parametrize("text", [
    sddl(MINE + "(A;;FA;;;SY"),  # unbalanced parenthesis
    sddl(MINE + ")(A;;FA;;;SY)"), sddl("(A;;FA;;SY)"), sddl("(A;;FA;;;;SY)"),  # five and seven fields
    sddl("(A;;FA;;;)"), sddl("(A;;FA;;; )"),  # no trustee
    sddl(MINE + "junk"), sddl("junk" + MINE), sddl(MINE + "((A;;FA;;;WD))"),  # stray text or nested entries
    sddl(MINE, flags="NO_ACCESS_CONTROL"), sddl(MINE, flags="X"),  # unknown DACL flags
    f"D:PAI{MINE}", f"O:{SID}", f"O:{SID}G:{SID}", f"junk O:{SID}D:{MINE}", f"O:{SID}D:{MINE}D:{MINE}",  # missing owner/DACL, leading text, repeated section
    f"O:D:PAI{MINE}", f"O:WDD:PAI{MINE}", sddl(MINE, owner="S-1-5-21-5-5-5-1100"), "", "garbage", None, 123, b"O:"])
def test_malformed_or_incomplete_sddl_is_never_safe(text):
    assert att.unsafe_acl(text, SID), text


@pytest.mark.parametrize("text", [
    sddl(MINE, "(A;OICI;FA;;;SY)"),  # exactly what apply_private_acl produces
    sddl(MINE, "(A;OICI;FA;;;SY)", flags="P"), sddl(MINE, flags="AI"), sddl(MINE, flags="PAIAR"), sddl(MINE, flags=""),
    sddl(f"(A;ID;FA;;;{SID})", "(A;ID;FA;;;SY)"), sddl("(A;;FA;;;BA)", "(A;;FA;;;S-1-5-18)", "(A;;FA;;;S-1-5-32-544)"),
    sddl("(A;;FA;;;CO)", "(A;;FR;;;OW)"), sddl(f"(A;;FA;;;{SID.lower()})"),  # case does not matter
    sddl(MINE, "(D;;FA;;;WD)", "(OD;;FA;;;BU)"),  # deny entries only remove access
    sddl("(OA;;CC;bf967aba-0de6-11d0-a285-00aa003049e2;;" + SID + ")"),  # an object-allow entry for a permitted trustee
    sddl(owner="BA"), sddl(MINE, owner="SY"), sddl(MINE, owner="BA"), sddl(MINE, owner="S-1-5-32-544"),
    sddl(flags="P"),  # an empty DACL grants nobody anything
    sddl(MINE) + "S:AI(ML;;NW;;;LW)"])  # a SACL (audit/label) after the DACL is not part of access
def test_known_safe_acls_are_still_accepted(text):
    assert not att.unsafe_acl(text, SID), text


@pytest.mark.parametrize("trustee", ["WD", "BU", "AU", "IU", "NU", "S-1-1-0", "S-1-5-11", "S-1-5-32-545", "S-1-5-21-5-5-5-1100", "LG", "AC", ""])
def test_known_unsafe_acls_are_still_rejected(trustee):
    assert att.unsafe_acl(sddl(MINE, f"(A;;FA;;;{trustee})"), SID), trustee
    assert att.unsafe_acl(sddl(f"(OA;;CC;bf967aba-0de6-11d0-a285-00aa003049e2;;{trustee})", MINE), SID), trustee
    assert att.unsafe_acl(sddl(f"(A;;FR;;;{trustee})"), SID), trustee  # read-only access for a stranger is also refused


def test_a_missing_dacl_and_an_unsafe_owner_are_still_rejected():
    assert att.unsafe_acl(f"O:{SID}", SID) and att.unsafe_acl(f"O:{SID}G:{SID}S:AI", SID) and att.unsafe_acl("D:(A;;FA;;;WD)", SID)
    assert att.unsafe_acl(sddl(MINE, owner="WD"), SID) and att.unsafe_acl(sddl(MINE, owner="BU"), SID)


def test_an_unsafe_acl_makes_evaluation_and_the_production_gate_fail_closed(home):
    issue(home)
    for bad in (sddl(MINE, "(XA;;FA;;;WD;(Member_of{SID(WD)}))"), sddl(MINE, "(ZA;;FA;;;WD)"), sddl(MINE + "(A;;FA;;SY)"), f"O:{SID}"):
        assert check(home, acl_reader=lambda p, s=bad: s).code == "attestation_permissions_unsafe", bad
        inp = gate_inputs(home)
        inp.probes["attestation_acl_reader"] = lambda p, s=bad: s
        allowed, codes = blockers(inp)
        assert not allowed and codes == ["attestation_permissions_unsafe", "live_google_not_verified"], bad
    folder_only = lambda p: SAFE_SDDL if Path(p).name == att.FILE_NAME else sddl(MINE, "(XA;;FA;;;WD;(Member_of{SID(WD)}))")  # noqa: E731
    assert check(home, acl_reader=folder_only).code == "attestation_permissions_unsafe"  # the folder's list is parsed with the same strictness
    assert check(home, acl_reader=ACL).valid  # the genuine private list still passes
    assert blockers(gate_inputs(home)) == (True, [])


@requires_windows
def test_the_real_private_acl_still_passes_the_stricter_parser(tmp_path):
    guid, sid = att.windows_identity()
    folder = tmp_path / "private"
    folder.mkdir()
    att.apply_private_acl(folder, sid)
    (folder / "f.bin").write_bytes(b"x")
    assert not att.unsafe_acl(att.read_sddl(folder), sid) and not att.unsafe_acl(att.read_sddl(folder / "f.bin"), sid)


# === production gate ===============================================================================================================

def test_without_an_attestation_the_gate_blocks_with_the_same_single_code_as_before(home):
    assert blockers(gate_inputs(home)) == (False, ["live_google_not_verified"])
    assert pf.GATE_REQUIREMENTS == ("live_google_not_verified", "credentials_not_relocated")


def test_a_valid_attestation_clears_only_live_google_not_verified(home, monkeypatch):
    # With no OAuth locations configured the credentials fall back to the project folder. A synthetic project folder under the temporary directory
    # replaces the real one (the fallback constants are read at call time), so the result never depends on files in the real project.
    project = home.tmp / "synthetic_project"
    (project / "credentials").mkdir(parents=True)
    token, client_file = project / "token.json", project / "credentials" / "client_secret.json"
    token.write_bytes(b"synthetic token placeholder")
    client_file.write_bytes(b"synthetic client placeholder")
    monkeypatch.setattr(auth, "PROJECT_ROOT", project)
    monkeypatch.setattr(auth, "TOKEN_PATH", token)
    monkeypatch.setattr(auth, "CLIENT_SECRET_PATH", client_file)
    inside = in_project_settings(home)
    issue(home, inside)
    allowed, codes = blockers(gate_inputs(home, inside))
    assert not allowed and codes == ["credentials_not_relocated"]  # the credentials requirement is independent
    token.unlink()  # a missing sign-in is a blocker of its own, and it is reported next to (not instead of) the relocation requirement
    assert blockers(gate_inputs(home, inside)) == (False, ["auth_token_missing", "credentials_not_relocated"])
    issue(home)  # credentials configured outside the project: both requirements are cleared and nothing else was blocking
    assert blockers(gate_inputs(home)) == (True, [])


def test_a_valid_attestation_does_not_hide_other_blockers(home):
    issue(home)
    (home.tmp / "gone.ttf").write_bytes(b"")
    inp = gate_inputs(home)
    inp.probes["chromium_folders"] = lambda env: []
    allowed, codes = blockers(inp)
    assert not allowed and "chromium_missing" in codes and "live_google_not_verified" not in codes


def test_an_invalid_attestation_blocks_with_a_specific_sanitized_code(home):
    issue(home)
    inp = gate_inputs(home)
    inp.clock = lambda: NOW + dt.timedelta(days=8)
    allowed, codes = blockers(inp)
    assert not allowed and codes == ["attestation_expired", "live_google_not_verified"]
    allowed, items = pf.production_gate(inp)[0], pf.production_gate(inp)[2]
    messages = " ".join(b.message + " " + b.action for b in items)
    assert GUID not in messages and SID not in messages and str(home.tmp) not in messages and "expired" in messages


def test_every_failure_class_blocks_the_gate(home):
    issue(home)
    cases = {"attestation_unprotect_failed": dict(clock=lambda: NOW, protector=FakeProtector(), identity=lambda: ("x" * 8, SID)),
             "attestation_permissions_unsafe": dict(acl=lambda p: f"O:{SID}D:(A;;FA;;;WD)")}
    for code, spec in cases.items():
        inp = gate_inputs(home)
        if "identity" in spec:
            inp.probes["attestation_identity"] = spec["identity"]
        if "acl" in spec:
            inp.probes["attestation_acl_reader"] = spec["acl"]
        allowed, codes = blockers(inp)
        assert not allowed and code in codes and "live_google_not_verified" in codes, code
    stored(home).write_bytes(b"junk" * 10)
    assert blockers(gate_inputs(home)) == (False, ["attestation_corrupt", "live_google_not_verified"])


def test_evaluating_the_gate_is_read_only_and_makes_no_network_call(home, monkeypatch):
    issue(home)
    before = tree(home.tmp)
    stamp = os.stat(stored(home)).st_mtime_ns
    for _ in range(3):
        assert blockers(gate_inputs(home)) == (True, [])
    pf.run_preflight(gate_inputs(home))
    assert tree(home.tmp) == before and os.stat(stored(home)).st_mtime_ns == stamp
    assert check(home).expires_at == att._stamp(NOW + dt.timedelta(days=7))  # checking never extends the expiry
    empty = home.tmp / "empty_home"
    missing_inputs = gate_inputs(home)
    missing_inputs.environ = {"LOCALAPPDATA": str(empty)}
    pf.production_gate(missing_inputs)
    assert not empty.exists()  # a gate evaluation never creates the folder either


def test_the_gate_never_calls_the_issuing_functions(home, monkeypatch):
    monkeypatch.setattr(att, "issue_attestation", lambda *a, **k: (_ for _ in ()).throw(AssertionError("an attestation was issued")))
    monkeypatch.setattr(att.VerificationEvidence, "from_live_checks", classmethod(lambda *a, **k: (_ for _ in ()).throw(AssertionError("evidence made"))))
    pf.production_gate(gate_inputs(home))
    pf.run_preflight(gate_inputs(home))


def test_the_ordinary_configuration_cannot_change_the_expiry_or_the_requirement(home):
    import yaml
    for extra in ({"attestation": {"validity_days": 365}}, {"execution": {"attestation_validity_days": 30}}, {"paths": {"attestation_file": "C:\\x\\a.attest"}},
                  {"workbooks": {"live_verified": True}}, {"live_google_verified": True}):
        data = json.loads(json.dumps(home.data))
        for key, value in extra.items():
            data.setdefault(key, {})
            data[key].update(value) if isinstance(value, dict) else data.update({key: value})
        path = home.tmp / "bad.yaml"
        path.write_text(yaml.safe_dump(data), encoding="utf-8")
        with pytest.raises(rc.RunnerConfigError):
            rc.load_settings(path, environ=home.env)
    assert att.VALIDITY == dt.timedelta(days=7)
    assert not hasattr(home.settings, "attestation_validity_days") and not any("attest" in f for f in vars(home.settings))


# === the command line ==============================================================================================================

def run_cli(home, monkeypatch, argv, *, probes=None, result=None, options=None):
    if result is not None:
        monkeypatch.setattr(client_loader, "load_from_workbook", lambda *a, **k: result)
    services = {"renderer": StubRenderer(), "collect": default_collect, "sleep": lambda s: None, **(options or {})}
    code = cli.main(["--json"] + argv + ["--config", str(home.path)], environ=home.env, gate=None, services_options=services,
                    preflight_probes={**PROBES_OK, **GATE_SEAMS, **(probes or {})}, clock=lambda: dt.datetime.now(UTC), client_factory=NoGoogle())
    return code


def test_a_report_run_passes_the_real_gate_only_with_a_valid_attestation_that_covers_the_client(home, monkeypatch, capsys):
    result = loaded("Alpha Co")
    assert run_cli(home, monkeypatch, RUN, result=result) == Exit.PREFLIGHT_FAILED  # no attestation
    assert json.loads(capsys.readouterr().out)["details"]["blockers"] == ["live_google_not_verified"] and not home.ledger_path.exists()
    issue(home, now=dt.datetime.now(UTC).replace(microsecond=0), ev=evidence(home.settings, at=dt.datetime.now(UTC) - dt.timedelta(minutes=1)))
    assert run_cli(home, monkeypatch, RUN, result=result) == Exit.OK
    data = json.loads(capsys.readouterr().out)
    assert data["state"] == "audited" and data["outcome"] == "succeeded"  # the whole pipeline ran, with the real gate


def test_the_second_stage_blocks_an_uncovered_client_or_a_changed_property_before_anything_starts(home, monkeypatch, capsys):
    moment = dt.datetime.now(UTC).replace(microsecond=0)
    issue(home, now=moment, ev=evidence(home.settings, at=moment - dt.timedelta(minutes=1), clients=[client_evidence("Beta Co")]))
    assert run_cli(home, monkeypatch, RUN, result=loaded("Alpha Co")) == Exit.PREFLIGHT_FAILED
    data = json.loads(capsys.readouterr().out)
    assert data["details"]["blockers"] == ["attestation_client_not_covered"] and not home.ledger_path.exists()
    issue(home, now=moment, ev=evidence(home.settings, at=moment - dt.timedelta(minutes=1)))
    changed = loaded("Alpha Co")
    changed.clients["alpha-co"].client.__dict__  # the Client is a frozen dataclass: build a changed copy instead
    import dataclasses
    changed.clients["alpha-co"] = dataclasses.replace(changed.clients["alpha-co"], client=make_client("Alpha Co", ga4_property_id="987654321"))
    assert run_cli(home, monkeypatch, RUN, result=changed) == Exit.PREFLIGHT_FAILED
    assert json.loads(capsys.readouterr().out)["details"]["blockers"] == ["attestation_property_changed"] and not home.ledger_path.exists()
    batch = ["report", "batch", "--period", PERIOD, "--production"]
    assert run_cli(home, monkeypatch, batch, result=changed) == Exit.PREFLIGHT_FAILED
    assert json.loads(capsys.readouterr().out)["details"]["blockers"] == ["attestation_property_changed"] and not home.ledger_path.exists()


def test_a_synthetic_or_expired_record_cannot_pass_the_real_gate_through_the_cli(home, monkeypatch, capsys):
    moment = dt.datetime.now(UTC).replace(microsecond=0)
    issue(home, now=moment, ev=evidence(home.settings, at=moment - dt.timedelta(minutes=1)))
    rewrite(home, lambda p: p.update(origin="synthetic"))
    assert run_cli(home, monkeypatch, RUN, result=loaded("Alpha Co")) == Exit.PREFLIGHT_FAILED
    assert json.loads(capsys.readouterr().out)["details"]["blockers"] == ["attestation_not_live", "live_google_not_verified"] and not home.ledger_path.exists()
    code = run_cli(home, monkeypatch, ["recovery", "cancel", "--run", f"alpha-co/{PERIOD}", "--operator", "jw", "--reason", "no_longer_needed", "--production"])
    assert code == Exit.PREFLIGHT_FAILED and not home.ledger_path.exists()


def test_there_is_no_command_flag_or_environment_variable_that_creates_or_accepts_an_attestation(home, capsys):
    for extra in (["--attested"], ["--attestation", "valid"], ["--verified"], ["--force"], ["--skip-gate"]):
        assert cli.main(RUN + extra, environ=home.env) == Exit.USAGE
    cli.main(["--help"], environ=home.env)
    help_text = capsys.readouterr().out
    assert "attest" not in help_text.lower()
    for sub in (["report", "run"], ["report", "batch"], ["recovery", "cancel"], ["doctor"], ["system-info"], ["setup", "auth"], ["clients", "validate"]):
        cli.main(sub + ["--help"], environ=home.env)
        assert "attest" not in capsys.readouterr().out.lower(), sub
    env = {**home.env, "JASPER_ATTESTATION": "valid", "JASPER_LIVE_VERIFIED": "1", "LIVE_GOOGLE_VERIFIED": "true", "JASPER_SKIP_GATE": "1"}
    code = cli.main(["--json", *RUN, "--config", str(home.path)], environ=env, preflight_probes=PROBES_OK, client_factory=NoGoogle())
    assert code == Exit.PREFLIGHT_FAILED and not home.ledger_path.exists() and not stored(home).exists()
    parser = cli.build_parser()
    options = {opt for action in parser._actions for opt in action.option_strings}
    assert not any("attest" in opt or "verif" in opt for opt in options)


def test_only_the_allowed_modules_use_the_issuing_functions():
    """Tripwire: in `src/` only the attestation module itself and the live-verification workflow may create evidence or issue an attestation, and the
    workflow only through the evidence interface and the issue function (never the permission helper). Any other module is an offender."""
    allowed = {"attestation.py": {"issue_attestation", "from_live_checks", "VerificationEvidence", "apply_private_acl"},
               "live_verification.py": {"issue_attestation", "from_live_checks", "VerificationEvidence"}}
    names = ("issue_attestation", "from_live_checks", "VerificationEvidence", "apply_private_acl")
    offenders = sorted(f"{path.name}:{name}" for path in (ROOT / "src").rglob("*.py")
                       for name in names if name in path.read_text(encoding="utf-8") and name not in allowed.get(path.name, set()))
    assert offenders == []
    workflow = (ROOT / "src" / "seo_report" / "live_verification.py").read_text(encoding="utf-8")
    code = workflow.split('"""', 2)[2]  # the module docstring mentions the names; only the code counts
    assert code.count("attestation.issue_attestation(") == 1 and code.count("from_live_checks(") == 1  # exactly one issuing call site
    assert ".synthetic(" not in code and "os.environ" not in code and "open(" not in code and "json" not in code  # no evidence from files, variables or JSON


def test_doctor_and_system_info_report_the_state_without_leaking_it(home, monkeypatch, capsys):
    def doctor(command):
        assert cli.main(["--json", command, "--config", str(home.path)] + (["--skip-browser-launch"] if command == "doctor" else []), environ=home.env,
                        preflight_probes={**PROBES_OK, **GATE_SEAMS}, clock=lambda: dt.datetime.now(UTC)) == 0
        return json.loads(capsys.readouterr().out)
    report = doctor("doctor")
    # the credentials of this fixture are configured outside the project and OneDrive, so only the live verification and the unconnected services remain
    assert report["outstanding"] == ["live_google_not_verified", "make_not_connected", "drive_not_connected", "email_not_connected"]
    assert report["production_ready"] is False
    assert {f["code"] for f in report["findings"]} >= {"live_google_not_verified"} and "live_google_attested" not in {f["code"] for f in report["findings"]}
    info = doctor("system-info")
    assert info["live_google_verified"] is False and info["verification_attestation"]["status"] == "missing"
    moment = dt.datetime.now(UTC).replace(microsecond=0)
    issue(home, now=moment, ev=evidence(home.settings, at=moment - dt.timedelta(minutes=1)))
    report = doctor("doctor")
    codes = {f["code"] for f in report["findings"]}
    assert "live_google_attested" in codes and "live_google_not_verified" not in codes
    # R8.7d.2: with a valid attestation AND relocated credentials only the unconnected services remain (this used to assert the static, stale
    # "credentials_not_relocated"; the relocation is now reported with the same condition the production gate applies)
    assert report["outstanding"] == ["make_not_connected", "drive_not_connected", "email_not_connected"] and report["production_ready"] is False
    info = doctor("system-info")
    assert info["runner_readiness"]["outstanding"] == report["outstanding"]  # doctor and system-info agree
    assert info["live_google_verified"] is True and info["verification_attestation"]["status"] == "valid" and info["verification_attestation"]["age_hours"] < 1
    text = json.dumps(report) + json.dumps(info)
    for secret in (GUID, SID, "https://example.test/", "123456789", home.settings.reporting_spreadsheet_id, str(home.tmp), "checks", "nonce", "binding"):
        assert secret not in text, secret


def test_the_outstanding_list_reflects_attestation_and_relocation_independently(home):
    inside = in_project_settings(home)
    issue(home, inside)
    findings = pf.run_preflight(gate_inputs(home, inside))
    # valid attestation, credentials still in the project: only the live verification leaves the list
    assert pf.outstanding_for(findings, inside) == ["credentials_not_relocated", "make_not_connected", "drive_not_connected", "email_not_connected"]
    issue(home)
    findings = pf.run_preflight(gate_inputs(home))
    assert pf.outstanding_for(findings, home.settings) == ["make_not_connected", "drive_not_connected", "email_not_connected"]
    assert pf.outstanding_for(findings) == ["credentials_not_relocated", "make_not_connected", "drive_not_connected", "email_not_connected"]  # no settings: never assumed
    stored(home).unlink()
    findings = pf.run_preflight(gate_inputs(home))
    assert pf.outstanding_for(findings, home.settings) == ["live_google_not_verified", "make_not_connected", "drive_not_connected", "email_not_connected"]
    assert pf.build_report(findings, app_version="8", settings=home.settings)["production_ready"] is False


@pytest.mark.parametrize("client_where,token_where", [("project", "safe"), ("safe", "project"), ("onedrive", "safe"), ("safe", "onedrive"), ("onedrive", "project")])
def test_a_valid_attestation_never_hides_a_credential_file_in_the_project_or_onedrive(home, client_where, token_where):
    import yaml
    places = {"safe": home.creds, "project": ROOT / "credentials_matrix_unused", "onedrive": home.tmp / "OneDrive" / "creds"}
    data = json.loads(json.dumps(home.data))
    data["paths"]["oauth_client_file"] = str(places[client_where] / "client.json")
    data["paths"]["oauth_cache_file"] = str(places[token_where] / "login.json")
    path = home.tmp / "matrix.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    settings = rc.load_settings(path, environ=home.env)
    issue(home, settings)  # synthetic evidence, a temporary store: the attestation itself is valid for exactly these paths
    findings = pf.run_preflight(gate_inputs(home, settings))
    assert pf.outstanding_for(findings, settings) == ["credentials_not_relocated", "make_not_connected", "drive_not_connected", "email_not_connected"]
    allowed, codes = blockers(gate_inputs(home, settings))
    # (a credential file that does not exist at its synthetic location adds its own `auth_token_missing` blocker; irrelevant to this check)
    assert not allowed and "credentials_not_relocated" in codes and "live_google_not_verified" not in codes


def test_doctor_shows_a_specific_warning_for_an_invalid_attestation_and_keeps_its_exit_code(home, capsys):
    moment = dt.datetime.now(UTC).replace(microsecond=0)
    issue(home, now=moment, ev=evidence(home.settings, at=moment - dt.timedelta(minutes=1)))
    stored(home).write_bytes(b"JASPERATT\x01broken")
    code = cli.main(["--json", "doctor", "--skip-browser-launch", "--config", str(home.path)], environ=home.env, preflight_probes={**PROBES_OK, **GATE_SEAMS},
                    clock=lambda: dt.datetime.now(UTC))
    report = json.loads(capsys.readouterr().out)
    finding = next(f for f in report["findings"] if f["component"] == "attestation")
    assert code == 0 and finding["status"] == "warning" and finding["code"] == "attestation_unprotect_failed" and "live_google_not_verified" in report["outstanding"]


# === security ======================================================================================================================

def test_nothing_is_logged_while_issuing_or_evaluating(home, caplog):
    import logging
    caplog.set_level(logging.DEBUG)
    issue(home)
    check(home)
    check(home, now=NOW + dt.timedelta(days=9))
    pf.production_gate(gate_inputs(home))
    assert [r for r in caplog.records if "attest" in r.name.lower() or "attest" in r.getMessage().lower() or GUID in r.getMessage()] == []
    assert not any(GUID in r.getMessage() or SID in r.getMessage() for r in caplog.records)


def test_error_codes_and_messages_are_fixed_and_sanitized():
    for code, message in att.CODES.items():
        assert code.startswith("attestation_") and code == code.lower() and message and "\\" not in message and "@" not in message
    verdict = att.Verdict("valid", "attestation_valid", "2026-11-01T00:00:00+00:00", "2026-11-08T00:00:00+00:00", 5, (("alpha-co", "a" * 16, "b" * 16),))
    assert verdict.public() == {"status": "valid", "code": "attestation_valid", "issued_at": "2026-11-01T00:00:00+00:00", "expires_at": "2026-11-08T00:00:00+00:00",
                                "age_seconds": 5, "clients": ["alpha-co"]}


def test_an_interrupted_write_fails_safely_and_keeps_the_previous_attestation(home, monkeypatch):
    issue(home)
    before = stored(home).read_bytes()
    folder = stored(home).parent
    for target in ("os.replace", "os.fsync"):
        monkeypatch.setattr(target, lambda *a, **k: (_ for _ in ()).throw(OSError("simulated interruption")))
        with pytest.raises(OSError):
            issue(home, now=NOW + dt.timedelta(minutes=1), ev=evidence(home.settings, at=NOW))
        monkeypatch.undo()
        assert stored(home).read_bytes() == before and check(home).valid
        assert [p.name for p in folder.iterdir()] == [att.FILE_NAME]  # no partial or temporary file is left behind
    # a crash with no previous record leaves nothing that could be mistaken for one
    stored(home).unlink()
    monkeypatch.setattr("os.replace", lambda *a, **k: (_ for _ in ()).throw(OSError("simulated interruption")))
    with pytest.raises(OSError):
        issue(home)
    monkeypatch.undo()
    assert check(home).status == "missing" and list(folder.iterdir()) == []


def test_reverification_produces_a_new_record_with_a_fresh_validity_period(home):
    first = issue(home)
    later = NOW + dt.timedelta(days=6)
    second = issue(home, now=later, ev=evidence(home.settings, at=later - dt.timedelta(minutes=1)))
    assert second.issued_at != first.issued_at and second.expires_at == att._stamp(later + att.VALIDITY)
    assert check(home, now=NOW + dt.timedelta(days=8)).valid  # valid only because it was issued again, not because the old one was extended


def test_synthetic_evidence_cannot_reach_a_production_path(home):
    synthetic = evidence(home.settings, factory="synthetic")
    assert synthetic.origin == "synthetic"
    with pytest.raises(att.AttestationError):
        att.issue_attestation(synthetic, home.settings, environ=home.env, now=NOW, acl_applier=lambda *a: None, **SEAMS)
    assert not stored(home).exists()
    assert "synthetic" not in (ROOT / "src" / "seo_report" / "preflight.py").read_text(encoding="utf-8")  # the gate never reads an origin other than "live"
