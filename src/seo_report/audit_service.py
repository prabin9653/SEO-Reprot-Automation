"""The verification that gates delivery (R8.3).

`verify_finalization` is read-only. It passes only when all of these hold:

  1. `finalization_audit.audit_period(...)` is `consistent` with `verification_complete` and the finalized-only reader
     lists the run;
  2. the audit reports the run's reviewer record as `finalized_valid`;
  3. the reviewer record is a schema version 2 record whose run ID, PDF SHA-256 and size, and render-input hash and
     format are exactly the ledger's expectation;
  4. no problem or limit finding exists for the period (information findings such as superseded or duplicate copies
     and valid attempt records are tolerated). An unarchived draft after finalization is a problem.

A period with a leftover partial file (for example after an interrupted write) therefore does not pass: the engine never
deletes such files, so an operator must look at it. The verdict carries fixed reason codes only.
"""

import json
from dataclasses import dataclass
from pathlib import Path

from seo_report import finalization_audit as fa
from seo_report import history_access as ha
from seo_report import output_layout as layout
from seo_report import record_writer as rw
from seo_report import render_input_hash as rh

TOLERATED = {fa.FINALIZED_VALID, fa.SUPERSEDED_PDF, fa.DUPLICATE_PDF, fa.ATTEMPT_VALID}


@dataclass(frozen=True)
class AuditVerdict:
    ok: bool
    outcome: str
    finalized_valid: int
    verification_complete: bool
    reader_lists_run: bool
    reasons: tuple  # fixed codes; empty when ok

    def as_summary(self) -> dict:
        return {"outcome": self.outcome, "finalized_valid": self.finalized_valid,
                "verification_complete": self.verification_complete}


def verify_finalization(*, settings, client_key: str, period_start: str, period_end: str, run_id: str,
                        pdf_sha256: str, pdf_bytes: int, record_name: str, render_input_sha256: str,
                        environ=None, ops=None, allow_onedrive_output: bool = False) -> AuditVerdict:
    report = fa.audit_period(output_root=settings.output_root, history_db_path=settings.history_db_path,
                             client_key=client_key, period_start=period_start, period_end=period_end,
                             environ=environ, allow_onedrive_output=allow_onedrive_output, ops=ops)
    reasons = []
    if report.outcome != fa.OUTCOME_CONSISTENT:
        reasons.append("audit_" + report.outcome)
    if not report.verification_complete:
        reasons.append("verification_incomplete")
    for finding in report.findings:
        if finding.category not in TOLERATED:
            reasons.append("finding_" + finding.category)
    valid_names = {f.name for f in report.by_category(fa.FINALIZED_VALID)}
    if record_name not in valid_names:
        reasons.append("record_not_finalized_valid")

    listed = False
    try:
        access = ha.open_history(settings.history_db_path, settings.output_root, environ=environ,
                                 allow_onedrive_output=allow_onedrive_output)
        try:
            listed = access.reader is not None and run_id in {i.run_id for i in access.reader.list_runs(client_key)}
        finally:
            access.close()
    except Exception:
        listed = False
    if not listed:
        reasons.append("reader_does_not_list_run")

    record = _read_record(settings, client_key, period_start, period_end, record_name)
    if record is None:
        reasons.append("record_unreadable")
    else:
        fin = record["finalization"]
        if record["schema_version"] != 2:
            reasons.append("record_not_version_2")
        elif (fin["render_input_sha256"], fin["render_input_format"]) != (
                render_input_sha256, {"name": rh.FORMAT_NAME, "version": rh.FORMAT_VERSION}):
            reasons.append("record_render_identity_differs")
        if record["snapshot"]["run_id"] != run_id or (fin["pdf_sha256"], fin["pdf_bytes"]) != (pdf_sha256, pdf_bytes):
            reasons.append("record_identity_differs")
    reasons = tuple(dict.fromkeys(reasons))
    return AuditVerdict(not reasons, report.outcome, report.finalized_valid, report.verification_complete, listed,
                        reasons)


def _read_record(settings, client_key, period_start, period_end, record_name):
    try:
        path = Path(layout.reviewer_dir(settings.output_root, client_key, period_start, period_end)) / record_name
        return rw.parse_record_bytes(path.read_bytes())
    except (OSError, layout.OutputLayoutError):
        return None
