"""Assembling one client's report in memory: base insights, snapshot, optional history and two separate views.

`assemble_report` runs these steps, in this order, and returns a `ReportBundle`:

  1. resolve the client's stable key from the client registry (an unknown client stops the assembly)
  2. generate the base `ReportInsights` with the existing insights engine
  3. build the snapshot in memory with the existing snapshot builder (it is never stored here)
  4. open the history through `history_access.open_history` (a read-only, finalized-only reader)
  5. when the history is available, run the existing Stage 6b `compare_with_history` through that reader
  6. when the history is missing, unavailable or fails in an expected way, carry on with the base report and
     record a safe, reviewer-only status
  7. start the history insights empty (history rules arrive in Stages 6c-4 and 6c-5)
  8. build `ClientContent` and `ReviewerReport` as two separate types
  9. return the bundle

Client and reviewer information live in different types. `ClientContent` has no field for run IDs, revisions,
hashes, paths, notes, diagnostics, excluded runs or comparison internals; the renderer of a later stage is meant
to accept a `ClientContent` and nothing else. Keyword rankings come from the sheet exactly as entered: history
never replaces a sheet value. A historical fallback for a blank Previous Ranking is only recorded as a pending
decision for the reviewer; no marker, footnote or historical wording is created here.

Nothing is persisted: no snapshot is stored, no event is written, no file or folder is created and no PDF is
rendered. The only I/O is what `open_history` and the Stage 6b comparison do, and both are read-only.
"""

import copy
from dataclasses import dataclass

from seo_report import rankings as rk
from seo_report.client_config import validate_client_key
from seo_report.history_access import ExcludedRun, open_history
from seo_report.insights.engine import ReportInsights, SectionInsights, generate_insights
from seo_report.insights.recommendations import Recommendation, RecommendationConfig
from seo_report.insights.rules import ReviewerNote, RuleOutput
from seo_report.output_layout import OutputLayoutError
from seo_report.periods import ReportPeriods
from seo_report.snapshots import eligibility as el
from seo_report.snapshots import store as st
from seo_report.snapshots.baselines import BaselineRef, HistoryConfig, SkippedRun
from seo_report.snapshots import comparison as cmp
from seo_report.snapshots.comparison import DimensionStatus, HistoricalComparison
from seo_report.snapshots.history import compare_with_history
from seo_report.snapshots.snapshot import Snapshot, build_snapshot

STATE_AVAILABLE = "available"
STATE_MISSING = "missing"
STATE_UNAVAILABLE = "unavailable"
STATE_SCHEMA_FAILURE = "schema_failure"
STATE_COMPARISON_UNAVAILABLE = "comparison_unavailable"
HISTORY_STATES = (STATE_AVAILABLE, STATE_MISSING, STATE_UNAVAILABLE, STATE_SCHEMA_FAILURE,
                  STATE_COMPARISON_UNAVAILABLE)

_COMPARISON_FAILED = "the history comparison could not be completed"
_OUTPUT_ROOT_UNUSABLE = "the report output location is not usable, so the history could not be checked"
_AVAILABLE = "the history was compared with the current report"


# --- what the client may see ------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class ClientIdentity:
    """The client details that may appear in the report. No property IDs, status or contact details."""

    name: str
    website: str
    target_country: str
    target_city: str


@dataclass(frozen=True)
class RankingRow:
    """One row of the keyword ranking table, exactly as the sheet has it."""

    keyword: str
    target_location: str
    target_url: str
    previous: rk.Ranking
    current: rk.Ranking
    movement: int | None  # `rankings.movement`: None unless both rankings are numeric


@dataclass(frozen=True)
class ClientContent:
    """Everything the client renderer may use, and nothing else.

    The historical collections are empty in this stage; later stages define their element types.
    """

    client: ClientIdentity
    periods: ReportPeriods
    sections: tuple[SectionInsights, ...]
    recommendations: tuple[Recommendation, ...]
    ranking_rows: tuple[RankingRow, ...]
    historical_statements: tuple = ()
    ranking_markers: tuple = ()
    ranking_footnote: str | None = None


# --- what only the reviewer sees ------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class SnapshotIdentity:
    """The in-memory snapshot. `run_id` and `revision` exist only after the snapshot is stored (a later stage)."""

    client_key: str
    period_start: str
    period_end: str
    content_hash: str
    quality: str
    run_id: str | None = None
    revision: int | None = None


@dataclass(frozen=True)
class HistoryStatus:
    state: str  # one of HISTORY_STATES
    detail: str  # a fixed phrase
    probe_status: str | None = None  # the probe's status, when the probe ran
    problems: tuple[str, ...] = ()  # schema problems: table, column and trigger names only


@dataclass(frozen=True)
class HistoryReview:
    """Internals of the Stage 6b comparison, for the reviewer."""

    limits: dict
    dimension_status: tuple[DimensionStatus, ...]  # one per dimension, in `eligibility.DIMENSIONS` order
    baselines: tuple[tuple[str, BaselineRef], ...]  # (dimension, baseline) for dimensions that have one
    skipped_runs: tuple[SkippedRun, ...]
    diagnostics: dict
    notes: tuple[ReviewerNote, ...]


@dataclass(frozen=True)
class PendingFallback:
    """A possible historical stand-in for a blank sheet Previous Ranking. Only a pending decision: it is not
    used, not displayed and does not change any sheet value."""

    keyword: str
    target_location: str
    current_position: int  # the sheet's Current Ranking
    baseline_position: int  # the baseline report's Current Ranking
    movement: int
    baseline_run_id: str
    baseline_revision: int
    baseline_period_start: str
    baseline_period_end: str


@dataclass(frozen=True)
class RankingCrosscheck:
    """Reviewer-only comparison of the sheet's rankings with the history (Stage 6b `RankingComparison.counts`).

    The counts are None when no ranking comparison was possible. `fallback_used` is 0 in this stage: pending
    fallbacks are only recorded.
    """

    fallback_used: int = 0
    sheet_previous_mismatch: int | None = None
    new_keywords: int | None = None
    no_longer_tracked: int | None = None
    not_comparable: int | None = None
    mismatched_keywords: tuple[tuple[str, str], ...] = ()  # (keyword, target location), as far as 6b lists them
    pending_fallbacks: tuple[PendingFallback, ...] = ()


@dataclass(frozen=True)
class ReviewerReport:
    client_key: str
    snapshot: SnapshotIdentity
    history_status: HistoryStatus
    history: HistoryReview | None
    ranking_crosscheck: RankingCrosscheck
    excluded_unfinalized: tuple[ExcludedRun, ...]
    base_notes: tuple[ReviewerNote, ...]
    validation_failures: tuple[tuple[str, str], ...]
    diagnostics: dict
    notes: tuple[ReviewerNote, ...]  # notes from this stage and from the history comparison


@dataclass(frozen=True)
class ReportBundle:
    base_insights: ReportInsights  # exactly what the insights engine returned
    snapshot: Snapshot  # in memory only
    historical_comparison: HistoricalComparison | None
    history_insights: RuleOutput  # empty until Stages 6c-4 and 6c-5
    client_content: ClientContent
    reviewer_report: ReviewerReport


# --- assembly --------------------------------------------------------------------------------------------------------------

def assemble_report(*, client, periods: ReportPeriods, gsc, ga4, registry, history_db_path=None, output_root=None,
                    environ=None, allow_onedrive_output: bool = False,
                    recommendation_config: RecommendationConfig | None = None,
                    history_config: HistoryConfig = HistoryConfig(), thresholds=None) -> ReportBundle:
    """Assemble one client's report in memory. See the module docstring for the steps.

    `client` is a `clients.Client` (with its keyword rows), `registry` a `client_config.ClientRegistry`, `gsc` and
    `ga4` the fetched data or None. Errors in the client lookup, the insight engine or the snapshot builder
    propagate: no partial report is returned. Expected history problems never do.
    """
    client_key = validate_client_key(registry.key_for(client.name))  # 1. unknown client: UnknownClientError

    keywords = tuple(client.keywords)
    config = recommendation_config if recommendation_config is not None else RecommendationConfig()
    options = {} if thresholds is None else {"thresholds": thresholds}
    base = generate_insights(gsc=gsc, ga4=ga4, keywords=keywords, target_country=client.target_country or None,
                             recommendation_config=config, **options)  # 2.
    snapshot = build_snapshot(client_key=client_key, client=client, periods=periods, gsc=gsc, ga4=ga4,
                              keywords=keywords, insights=base, recommendation_config=config)  # 3.

    comparison, status, excluded, baseline_payloads = _compare_with_history(  # 4. to 6.
        snapshot, client_key, keywords, history_db_path, output_root, environ, allow_onedrive_output, history_config)

    history_insights = RuleOutput()  # 7.
    reviewer = _reviewer_report(client_key, snapshot, base, comparison, status, excluded, keywords,
                                baseline_payloads)  # 8.
    content = _client_content(client, periods, base, keywords)
    return ReportBundle(base, snapshot, comparison, history_insights, content, reviewer)  # 9.


class _PayloadRecorder:
    """Hands Stage 6b's four reader calls straight through and keeps the payloads that `get` returned.

    Stage 6b has already verified each of those runs (`verify_run`) before it calls `get`, so the recorded
    payloads are the exact, verified baseline data it compared against. No extra read is made.
    """

    def __init__(self, reader):
        self._reader = reader
        self.payloads = {}

    def list_runs(self, client_key=None, *, include_voided=False):
        return self._reader.list_runs(client_key, include_voided=include_voided)

    def verify_run(self, run_id):
        return self._reader.verify_run(run_id)

    def get(self, run_id):
        run = self._reader.get(run_id)
        self.payloads[run_id] = run.payload
        return run

    def events(self, run_id):
        return self._reader.events(run_id)


def _compare_with_history(snapshot, client_key, keywords, db_path, output_root, environ, allow_onedrive_output,
                          history_config):
    try:
        access = open_history(db_path, output_root, environ=environ, allow_onedrive_output=allow_onedrive_output)
    except OutputLayoutError:
        return None, HistoryStatus(STATE_UNAVAILABLE, _OUTPUT_ROOT_UNUSABLE), (), {}
    try:
        probe = access.probe
        if access.reader is None:
            state = {"missing": STATE_MISSING, "schema_failure": STATE_SCHEMA_FAILURE}.get(probe.status,
                                                                                            STATE_UNAVAILABLE)
            return None, HistoryStatus(state, probe.detail, probe.status, probe.problems), (), {}
        recorder = _PayloadRecorder(access.reader)
        try:
            excluded = access.reader.excluded_unfinalized(client_key)
            comparison = compare_with_history(snapshot.payload, recorder, history_config)
        except st.SnapshotStoreError:  # includes HistoryAccessError: an expected failure to read the history
            return None, HistoryStatus(STATE_COMPARISON_UNAVAILABLE, _COMPARISON_FAILED, probe.status), (), {}
        return comparison, HistoryStatus(STATE_AVAILABLE, _AVAILABLE, probe.status), excluded, recorder.payloads
    finally:
        access.close()


def _client_content(client, periods, base: ReportInsights, keywords) -> ClientContent:
    identity = ClientIdentity(client.name, client.website, client.target_country, client.target_city)
    rows = tuple(RankingRow(k.keyword, k.target_location, k.target_url, k.previous, k.current,
                            rk.movement(k.previous, k.current)) for k in keywords)
    return ClientContent(identity, periods, tuple(base.sections), tuple(base.recommendations), rows)


def _reviewer_report(client_key, snapshot, base, comparison, status, excluded, keywords,
                     baseline_payloads) -> ReviewerReport:
    identity = SnapshotIdentity(client_key, snapshot.period_start, snapshot.period_end, snapshot.content_hash,
                                snapshot.quality)
    notes = []
    if status.state != STATE_AVAILABLE:
        notes.append(ReviewerNote("info", "history", "limitation",
                                  f"No history comparison was made: {status.detail}."))
    review = _history_review(comparison) if comparison is not None else None
    if comparison is not None:
        notes += comparison.notes
    return ReviewerReport(
        client_key=client_key, snapshot=identity, history_status=status, history=review,
        ranking_crosscheck=_ranking_crosscheck(comparison, snapshot, keywords, baseline_payloads),
        excluded_unfinalized=tuple(excluded), base_notes=tuple(base.notes),
        validation_failures=tuple(base.validation_failures), diagnostics=copy.deepcopy(base.diagnostics),
        notes=tuple(notes))


def _history_review(comparison: HistoricalComparison) -> HistoryReview:
    statuses = tuple(comparison.status[dimension] for dimension in el.DIMENSIONS)
    return HistoryReview(
        limits=dict(comparison.limits), dimension_status=statuses,
        baselines=tuple((s.dimension, s.baseline) for s in statuses if s.baseline is not None),
        skipped_runs=tuple(comparison.skipped), diagnostics=copy.deepcopy(comparison.diagnostics),
        notes=tuple(comparison.notes))


def _ranking_crosscheck(comparison: HistoricalComparison | None, snapshot, keywords,
                        baseline_payloads) -> RankingCrosscheck:
    if comparison is None:
        return RankingCrosscheck()
    ranking = comparison.rankings
    counts = ranking.counts
    listed = (*ranking.moved, *ranking.not_comparable)  # Stage 6b's own lists, capped at its list limit
    mismatched = tuple((k.keyword, k.target_location or "") for k in listed if k.sheet_previous_matches is False)
    return RankingCrosscheck(
        fallback_used=0, sheet_previous_mismatch=counts.get("sheet_previous_mismatch"),
        new_keywords=counts.get("new_keyword"), no_longer_tracked=counts.get("no_longer_tracked"),
        not_comparable=counts.get("not_comparable"), mismatched_keywords=mismatched,
        pending_fallbacks=_pending_fallbacks(comparison, snapshot, keywords, baseline_payloads))


def _keyword_index(rows) -> tuple[dict, set]:
    """Stage 6b's keyword matching (`comparison._compare_rankings`): key = normalized keyword and location,
    a key that occurs twice in one list is ambiguous and is never matched."""
    seen, ambiguous = {}, set()
    for row in rows:
        key = (cmp._norm(row["keyword"]), cmp._norm(row["target_location"]))
        if key in seen:
            ambiguous.add(key)
        seen.setdefault(key, row)
    return seen, ambiguous


def _pending_fallbacks(comparison: HistoricalComparison, snapshot, keywords,
                       baseline_payloads) -> tuple[PendingFallback, ...]:
    """Every sheet keyword whose Previous Ranking is blank and that has a numeric ranking now and in the baseline.

    The complete keyword lists are used, not Stage 6b's capped changed-keyword lists, so keywords with no
    movement (movement 0) are included. The current rows are the snapshot's sheet rows and the baseline rows
    are the payload Stage 6b itself loaded and verified for the rankings baseline. A keyword is skipped when its
    key is duplicated on either side, is new, or when either ranking is Above 10, missing or otherwise not a
    position. Nothing is invented: positions and movement come straight from those rows.
    """
    status = comparison.status[el.RANKINGS]
    payload = baseline_payloads.get(status.baseline.run_id) if status.eligible and status.baseline else None
    if payload is None:
        return ()
    current_rows = snapshot.payload["rankings"]["keywords"]
    baseline_rows = payload["rankings"]["keywords"]
    current, current_ambiguous = _keyword_index(current_rows)
    baseline, baseline_ambiguous = _keyword_index(baseline_rows)
    found = []
    for row in current_rows:
        key = (cmp._norm(row["keyword"]), cmp._norm(row["target_location"]))
        if key in current_ambiguous or key in baseline_ambiguous or key not in baseline:
            continue
        if row["previous"]["kind"] != "missing":
            continue  # the sheet has a Previous Ranking: it stays the source of truth
        now, then = row["current"], baseline[key]["current"]
        if now["kind"] != "position" or then["kind"] != "position":
            continue
        source = keywords[row["ordinal"]]
        found.append(PendingFallback(
            keyword=source.keyword, target_location=source.target_location, current_position=now["position"],
            baseline_position=then["position"], movement=then["position"] - now["position"],
            baseline_run_id=status.baseline.run_id, baseline_revision=status.baseline.revision,
            baseline_period_start=status.baseline.period_start, baseline_period_end=status.baseline.period_end))
    return tuple(found)