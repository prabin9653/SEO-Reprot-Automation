"""Stable exit codes of the command-line interface (R8.6a).

The codes are part of the interface: scripts and Windows Task Scheduler rely on them, so a value is never reused or renumbered.
They are *derived from* the existing contract vocabulary (`contracts.ERROR_CATEGORIES`, reject codes, batch abort reasons); no new
error category or state name is introduced here.

  0  OK                    the command did what was asked (including "already complete")
  1  INTERNAL_ERROR        an unexpected exception (details are in the sanitized log, never on the console)
  2  USAGE                 invalid arguments or a missing mandatory flag (also what `argparse` uses)
  3  CONFIG_INVALID        configuration missing/invalid, or a request refused as invalid (unknown client, wrong period)
  4  PREFLIGHT_FAILED      the deployment preflight found a mandatory requirement missing
  5  AUTH_REQUIRED         Google credentials are missing/expired/revoked: a person must run `setup auth`
  6  SOURCE_UNAVAILABLE    a required Google source could not be read (temporary, denied, invalid, incompatible)
  7  REPORT_FAILED         collection assembled but rendering/report generation failed or timed out
  8  FINALIZATION_FAILED   finalization failed or conflicted
  9  AUDIT_FAILED          the mandatory audit gate did not pass (the report is never deliverable)
 10  LEDGER_UNAVAILABLE    the local ledger or its storage cannot be used (includes a full disk)
 11  LOCK_CONFLICT         another process holds the run/runner lock, or the ledger was busy
 12  OPERATOR_ACTION_REQUIRED  the run is blocked or the request needs a decision (acknowledge, cancel, supersede, retry flag)
 13  NOT_AVAILABLE         the command exists but its implementation arrives in a later sub-stage (nothing was done)
130  INTERRUPTED           Ctrl+C / termination request; locks were released, the ledger is consistent
"""

from enum import IntEnum


class Exit(IntEnum):
    OK = 0
    INTERNAL_ERROR = 1
    USAGE = 2
    CONFIG_INVALID = 3
    PREFLIGHT_FAILED = 4
    AUTH_REQUIRED = 5
    SOURCE_UNAVAILABLE = 6
    REPORT_FAILED = 7
    FINALIZATION_FAILED = 8
    AUDIT_FAILED = 9
    LEDGER_UNAVAILABLE = 10
    LOCK_CONFLICT = 11
    OPERATOR_ACTION_REQUIRED = 12
    NOT_AVAILABLE = 13
    INTERRUPTED = 130


# error category -> exit code, for failed/blocked/rejected responses (contracts.ERROR_CATEGORIES is the full vocabulary)
BY_CATEGORY = {
    "validation_failed": Exit.CONFIG_INVALID,
    "config_invalid": Exit.CONFIG_INVALID,
    "auth_required": Exit.AUTH_REQUIRED,
    "source_temporary": Exit.SOURCE_UNAVAILABLE,
    "source_denied": Exit.SOURCE_UNAVAILABLE,
    "source_incompatible": Exit.SOURCE_UNAVAILABLE,
    "source_invalid": Exit.SOURCE_UNAVAILABLE,
    "stale_bundle": Exit.SOURCE_UNAVAILABLE,
    "timeout": Exit.SOURCE_UNAVAILABLE,
    "render_failed": Exit.REPORT_FAILED,
    "layout_issues": Exit.OPERATOR_ACTION_REQUIRED,
    "finalization_failed": Exit.FINALIZATION_FAILED,
    "finalization_conflict": Exit.OPERATOR_ACTION_REQUIRED,
    "audit_failed": Exit.AUDIT_FAILED,
    "publish_failed": Exit.REPORT_FAILED,
    "delivery_failed": Exit.REPORT_FAILED,
    "lock_unavailable": Exit.LOCK_CONFLICT,
    "run_in_progress": Exit.LOCK_CONFLICT,
    "internal_error": Exit.INTERNAL_ERROR,
}

# `validation_failed` rejections are split: invalid input/configuration versus "this needs a person's decision"
_CONFIG_REJECTS = frozenset({"client_unknown", "client_invalid", "period_differs_from_workbook", "action_not_supported",
                             "publish_not_supported", "period_invalid", "row_invalid", "scratch_dir_invalid"})

# batch abort reasons (contracts.ABORT_REASONS)
BY_ABORT_REASON = {
    "auth_required": Exit.AUTH_REQUIRED,
    "ledger_unavailable": Exit.LEDGER_UNAVAILABLE,
    "disk_full": Exit.LEDGER_UNAVAILABLE,
    "batch_deadline": Exit.OPERATOR_ACTION_REQUIRED,
    "lock_unavailable": Exit.LOCK_CONFLICT,
}

# ledger error codes (ledger.LedgerError.code) that are not storage failures
_LEDGER_LOCK_CODES = frozenset({"lock_held", "lock_lost", "stale_state", "ledger_busy"})


def exit_code_for_response(response) -> Exit:
    """The exit code for one `RunResponse` (or its dict form): success, or the category of its error."""
    data = response if isinstance(response, dict) else response.to_dict()
    outcome, error = data["outcome"], data.get("error")
    if error is None:
        if outcome in ("succeeded", "already_complete"):
            return Exit.OK
        if outcome == "blocked" or data["state"] == "blocked":
            return Exit.OPERATOR_ACTION_REQUIRED
        return Exit.OK if outcome == "in_progress" else Exit.INTERNAL_ERROR
    category, code = error["category"], error["code"]
    if outcome == "rejected" and category == "validation_failed":
        return Exit.CONFIG_INVALID if code in _CONFIG_REJECTS else Exit.OPERATOR_ACTION_REQUIRED
    if code == "disk_full":
        return Exit.LEDGER_UNAVAILABLE
    return BY_CATEGORY.get(category, Exit.INTERNAL_ERROR)


def exit_code_for_batch(outcome) -> Exit:
    """Batch rule: an abort decides; otherwise the most serious client result (lowest-numbered operator-visible failure first)."""
    if outcome.aborted:
        return BY_ABORT_REASON.get(outcome.abort_reason, Exit.INTERNAL_ERROR)
    worst = Exit.OK
    for response in outcome.results:
        code = exit_code_for_response(response)
        if code != Exit.OK and (worst == Exit.OK or code < worst):
            worst = code
    return worst


def exit_code_for_ledger_error(error) -> Exit:
    """A `LedgerError` that reached the CLI: lock/stale/busy are conflicts, anything else is an unusable ledger."""
    return Exit.LOCK_CONFLICT if getattr(error, "code", "") in _LEDGER_LOCK_CODES else Exit.LEDGER_UNAVAILABLE


def describe(code: Exit) -> str:
    """One operator-friendly line per exit code (printed by `--help` and used in messages)."""
    return {
        Exit.OK: "completed",
        Exit.INTERNAL_ERROR: "unexpected internal error (see the log)",
        Exit.USAGE: "invalid arguments",
        Exit.CONFIG_INVALID: "configuration or request is invalid",
        Exit.PREFLIGHT_FAILED: "deployment preflight failed",
        Exit.AUTH_REQUIRED: "Google sign-in is required: run `setup auth`",
        Exit.SOURCE_UNAVAILABLE: "a required Google data source is unavailable",
        Exit.REPORT_FAILED: "report generation failed",
        Exit.FINALIZATION_FAILED: "finalization failed",
        Exit.AUDIT_FAILED: "the audit did not pass; the report is not deliverable",
        Exit.LEDGER_UNAVAILABLE: "the local ledger or storage cannot be used",
        Exit.LOCK_CONFLICT: "another process is working on this; try again later",
        Exit.OPERATOR_ACTION_REQUIRED: "an operator decision is required",
        Exit.NOT_AVAILABLE: "this command is not available in this build yet",
        Exit.INTERRUPTED: "interrupted",
    }[code]
