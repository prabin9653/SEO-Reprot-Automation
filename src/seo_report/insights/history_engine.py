"""Composing the Stage 6c-4 rule output into the historical result that later stages use.

`compose_history_insights` takes a `HistoryRuleResult` and returns a `HistoryInsights`. It selects and orders; it
never writes, rewords, recalculates or invents anything.

  client side      `ClientHistory` holds `ClientHistoryStatement` objects only: the candidates that survive the
                   composition rules, with their text and evidence exactly as the rules produced them.
  reviewer side    `ReviewerHistory` holds every reviewer finding, every note, every validation failure and a
                   `StatementAudit` for each client candidate (selected, or why it was left out).

The two sides are different types in different containers: a renderer is meant to receive `HistoryInsights.client`
and nothing else. The client statement type has no run ID, blocked reason, duplicate-check metadata, note or
diagnostic field.

Composition rules for client statements
  1. A candidate whose key also appears among the validation failures is never used (a blocked candidate is not
     brought back), and neither is one whose key equals a base insight key (a history statement must not shadow a
     base statement). The duplicate check against the base insights was already done in Stage 6c-4 and is not
     repeated here.
  2. Candidates are ordered by section (keywords, search_console, geography, ga4), then by ascending numeric
     priority (lower means greater priority), then by key alphabetically; any remaining tie is broken by the other
     fields so that the order never depends on the input order.
  3. Only the first candidate for a key is used; later ones with the same key are left out.
  4. At most `MAX_STATEMENTS_PER_SECTION` (2) statements are kept per section, applied after the ordering.

Reviewer findings are ordered by priority, then key (the Stage 6c-4 convention), notes by severity, source,
category and message, and validation failures by key and reason. Notes and validation failures are all kept,
including exact repeats; only reviewer findings collapse exact repeats. Nothing is dropped
because of the client limit.

The module is pure: no clock, files, database or network, and its inputs are not changed.
"""

from dataclasses import dataclass, replace

from seo_report.insights.history_rules import (
    MAX_STATEMENTS_PER_SECTION, RULES_VERSION, ClientHistoryCandidate, HistoryRuleResult, HistoryValidationFailure,
    ReviewerHistoryFinding)
from seo_report.insights.rules import SECTIONS, ReviewerNote, Tone

OMIT_SECTION_LIMIT = "section_limit"
OMIT_DUPLICATE_KEY = "duplicate_key"
OMIT_VALIDATION_FAILED = "validation_failed"
OMIT_BASE_KEY_COLLISION = "base_key_collision"
OMISSION_REASONS = (OMIT_SECTION_LIMIT, OMIT_DUPLICATE_KEY, OMIT_VALIDATION_FAILED, OMIT_BASE_KEY_COLLISION)


@dataclass(frozen=True)
class ClientHistoryStatement:
    """A historical statement for the client. Everything in it may be shown or traced to the shown text."""

    section: str
    key: str
    dimension: str
    text: str
    tone: Tone
    priority: int
    evidence: dict  # flat numbers, a copy of the candidate's evidence
    baseline_period_start: str
    baseline_period_end: str
    rules_version: str
    group: str


@dataclass(frozen=True)
class ClientHistory:
    statements: tuple[ClientHistoryStatement, ...] = ()


@dataclass(frozen=True)
class StatementAudit:
    """What happened to one client candidate. Reviewer-side only."""

    key: str
    section: str
    base_keys: tuple[str, ...]  # base insight keys covering the same fact (Stage 6c-4 duplicate guard)
    selected: bool
    omitted_reason: str | None = None  # one of OMISSION_REASONS when not selected


@dataclass(frozen=True)
class ReviewerHistory:
    findings: tuple[ReviewerHistoryFinding, ...] = ()
    notes: tuple[ReviewerNote, ...] = ()
    validation_failures: tuple[HistoryValidationFailure, ...] = ()
    statement_audit: tuple[StatementAudit, ...] = ()


@dataclass(frozen=True)
class HistoryInsights:
    client: ClientHistory = ClientHistory()
    reviewer: ReviewerHistory = ReviewerHistory()
    rules_version: str = RULES_VERSION

    # Read-only conveniences; each returns the collection of exactly one side.
    @property
    def client_statements(self) -> tuple[ClientHistoryStatement, ...]:
        return self.client.statements

    @property
    def reviewer_findings(self) -> tuple[ReviewerHistoryFinding, ...]:
        return self.reviewer.findings

    @property
    def notes(self) -> tuple[ReviewerNote, ...]:
        return self.reviewer.notes

    @property
    def validation_failures(self) -> tuple[HistoryValidationFailure, ...]:
        return self.reviewer.validation_failures


# --- ordering ---------------------------------------------------------------------------------------------------------------------

_SECTION_ORDER = {section: index for index, section in enumerate(SECTIONS)}


def _candidate_order(candidate: ClientHistoryCandidate) -> tuple:
    """A total order: section, priority, key, then every other field, so input order never matters."""
    if candidate.section not in _SECTION_ORDER:
        raise ValueError(f"unknown section {candidate.section!r}")
    return (_SECTION_ORDER[candidate.section], candidate.priority, candidate.key, candidate.text, candidate.dimension,
            candidate.baseline_period_start, candidate.baseline_period_end, tuple(sorted(candidate.evidence.items())),
            candidate.tone.value, candidate.rules_version, candidate.group, candidate.base_keys)


def _finding_order(finding: ReviewerHistoryFinding) -> tuple:
    return (finding.priority, finding.key, finding.kind, finding.message, finding.section, finding.dimension or "",
            finding.blocked_reason or "", finding.baseline_period_start or "", finding.baseline_period_end or "",
            finding.baseline_run_id or "", tuple(sorted(finding.evidence.items())))


def _note_order(note: ReviewerNote) -> tuple:
    return (note.severity, note.source, note.category, note.message)


def _failure_order(failure: HistoryValidationFailure) -> tuple:
    return (failure.key, failure.reason)


def _unique(items) -> list:
    kept = []
    for item in items:
        if item not in kept:
            kept.append(item)
    return kept


def _require(items, kind, name: str) -> tuple:
    items = tuple(items)
    for item in items:
        if not isinstance(item, kind):
            raise TypeError(f"{name} must contain {kind.__name__} objects, got {type(item).__name__}")
    return items


def _base_keys(base_insights) -> set:
    sections = getattr(base_insights, "sections", None)
    if sections is not None:
        return {insight.key for section in sections for insight in section.insights}
    return {insight.key for insight in (base_insights or ())}


# --- composition ----------------------------------------------------------------------------------------------------------------------

def compose_history_insights(*, rule_output, base_insights=()) -> HistoryInsights:
    """Compose a `HistoryRuleResult` (or None) into a `HistoryInsights`. Pure and deterministic.

    `base_insights` (a `ReportInsights` or an iterable of insights) is only read for its keys, so that a historical
    statement can never reuse the key of a base statement. It is not changed.
    """
    if rule_output is None:
        return HistoryInsights()
    if not isinstance(rule_output, HistoryRuleResult):
        raise TypeError(f"rule_output must be a HistoryRuleResult, got {type(rule_output).__name__}")
    candidates = _require(rule_output.client_candidates, ClientHistoryCandidate, "client_candidates")
    findings = _require(rule_output.reviewer_findings, ReviewerHistoryFinding, "reviewer_findings")
    notes = _require(rule_output.notes, ReviewerNote, "notes")
    failures = _require(rule_output.validation_failures, HistoryValidationFailure, "validation_failures")

    statements, audit = _select_statements(candidates, {f.key for f in failures}, _base_keys(base_insights))
    reviewer = ReviewerHistory(
        findings=tuple(replace(f, evidence=dict(f.evidence))
                       for f in _unique(sorted(findings, key=_finding_order))),
        notes=tuple(sorted(notes, key=_note_order)),  # every note is kept, exact repeats included
        validation_failures=tuple(sorted(failures, key=_failure_order)),  # every failure is kept, repeats included
        statement_audit=tuple(audit))
    return HistoryInsights(ClientHistory(tuple(statements)), reviewer, rule_output.rules_version)


def _select_statements(candidates, failed_keys: set, base_keys: set):
    statements, audit = [], []
    used_keys, per_section = set(), {}
    for candidate in sorted(candidates, key=_candidate_order):
        reason = None
        if candidate.key in failed_keys:
            reason = OMIT_VALIDATION_FAILED
        elif candidate.key in base_keys:
            reason = OMIT_BASE_KEY_COLLISION
        elif candidate.key in used_keys:
            reason = OMIT_DUPLICATE_KEY
        elif per_section.get(candidate.section, 0) >= MAX_STATEMENTS_PER_SECTION:
            reason = OMIT_SECTION_LIMIT
        if reason is None:
            used_keys.add(candidate.key)
            per_section[candidate.section] = per_section.get(candidate.section, 0) + 1
            statements.append(ClientHistoryStatement(
                section=candidate.section, key=candidate.key, dimension=candidate.dimension, text=candidate.text,
                tone=candidate.tone, priority=candidate.priority, evidence=dict(candidate.evidence),
                baseline_period_start=candidate.baseline_period_start,
                baseline_period_end=candidate.baseline_period_end, rules_version=candidate.rules_version,
                group=candidate.group))
        audit.append(StatementAudit(candidate.key, candidate.section, tuple(candidate.base_keys), reason is None,
                                    reason))
    return statements, audit
