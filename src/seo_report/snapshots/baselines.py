"""Choosing the earlier report to compare against, separately for every dimension.

Pure functions. Reading and verifying stored runs is done by `history.py`; this module only decides
which runs are candidates and which candidate is the baseline for each dimension.

Candidate rules:
  * the run must belong to the same client and not be voided;
  * only the highest non-voided revision of each period counts (superseded revisions are skipped);
  * the same period as the current report, overlapping periods and later periods are excluded;
  * only the nearest `lookback_periods` earlier periods are considered, and a period that ends more
    than `max_gap_days` before the current one starts is excluded.

Each dimension then takes the nearest candidate that is eligible for it, so different dimensions can
use different baselines. Whenever an older report than the nearest one is used, a reviewer note says so.
"""

import datetime as dt
import re
from dataclasses import dataclass
from enum import Enum

from seo_report import metrics as mt
from seo_report.insights.rules import ReviewerNote
from seo_report.snapshots import eligibility as el

_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")


class BaselineKind(Enum):
    PREVIOUS_REPORT = "previous_report"  # year over year is a future, additive kind


@dataclass(frozen=True)
class HistoryConfig:
    lookback_periods: int = 3
    max_gap_days: int = 35
    list_limit: int = 10
    thresholds: mt.Thresholds = mt.Thresholds()

    def __post_init__(self):
        if self.lookback_periods < 1 or self.max_gap_days < 0 or self.list_limit < 1:
            raise ValueError("lookback_periods and list_limit must be at least 1 and max_gap_days at least 0")


@dataclass(frozen=True)
class SkippedRun:
    run_id: str
    revision: int
    period_start: str
    period_end: str
    reason: str


@dataclass(frozen=True)
class CandidatePlan:
    ordered: tuple  # run infos, nearest earlier period first
    skipped: tuple[SkippedRun, ...]


@dataclass(frozen=True)
class LoadedRun:
    """A planned candidate whose payload has been read and verified."""

    run_id: str
    revision: int
    period_start: str
    period_end: str
    created_at: str
    rank: int  # 1 = the nearest earlier period among the planned candidates
    payload: dict


@dataclass(frozen=True)
class BaselineRef:
    run_id: str
    revision: int
    period_start: str
    period_end: str
    created_at: str
    rank: int
    gap_days: int  # days between the baseline's end and the current period's start (0 = adjacent)
    same_length: bool  # counts are only compared between periods of equal length
    kind: BaselineKind = BaselineKind.PREVIOUS_REPORT


@dataclass(frozen=True)
class BaselineSelection:
    baselines: dict  # dimension -> BaselineRef | None
    payloads: dict  # run_id -> payload of every candidate that was loaded
    assessments: dict  # run_id -> {dimension: DimensionEligibility}
    rejections: dict  # dimension -> ((run_id, reason), ...) for nearer candidates that could not be used
    skipped: tuple[SkippedRun, ...]
    notes: tuple[ReviewerNote, ...]


def _date(text: str) -> dt.date:
    return dt.date.fromisoformat(text)


def note(severity: str, category: str, message: str) -> ReviewerNote:
    """A reviewer note from the history stage.

    Notes are built only from fixed phrases, run IDs, dates and dimension names, never from free text, so
    only an email-address safety net is applied. (The general masker would mistake a run ID followed by a
    date for a phone number.)
    """
    return ReviewerNote(severity, "history", category, _EMAIL.sub("[email]", message))


def plan_candidates(*, current_start: str, current_end: str, run_infos, config: HistoryConfig) -> CandidatePlan:
    """Filter and order the stored runs of one client. `run_infos` may include voided runs."""
    skipped = []

    def skip(info, reason):
        skipped.append(SkippedRun(info.run_id, info.revision, info.period_start, info.period_end, reason))

    latest = {}
    for info in sorted(run_infos, key=lambda i: (i.period_start, i.period_end, i.revision)):
        if info.voided:
            skip(info, "voided")
            continue
        key = (info.period_start, info.period_end)
        if key in latest:
            skip(latest[key], f"superseded by revision {info.revision}")
        latest[key] = info

    earlier = []
    for (start, end), info in latest.items():
        if (start, end) == (current_start, current_end):
            skip(info, "same period as the current report")
        elif start <= current_end and end >= current_start:
            skip(info, "overlaps the current period")
        elif start > current_end:
            skip(info, "later than the current period")
        else:
            earlier.append(info)

    earlier.sort(key=lambda i: (i.period_end, i.period_start), reverse=True)
    for info in earlier[config.lookback_periods:]:
        skip(info, f"outside the lookback of {config.lookback_periods} earlier periods")
    ordered = []
    for info in earlier[:config.lookback_periods]:
        gap = (_date(current_start) - _date(info.period_end)).days - 1
        if gap > config.max_gap_days:
            skip(info, f"a gap of {gap} days exceeds the {config.max_gap_days}-day limit")
        else:
            ordered.append(info)
    skipped.sort(key=lambda s: (s.period_start, s.period_end, s.revision, s.run_id))
    return CandidatePlan(tuple(ordered), tuple(skipped))


def select_baselines(current: dict, candidates, config: HistoryConfig, skipped=()) -> BaselineSelection:
    """Pick, for each dimension, the nearest loaded candidate that is eligible for it."""
    candidates = sorted(candidates, key=lambda c: c.rank)
    current_assessment = el.assess(current)
    assessments = {c.run_id: el.assess(c.payload) for c in candidates}
    compatibility = {c.run_id: el.compatibility(current, c.payload) for c in candidates}
    current_start, current_end = current["periods"]["current"]["start"], current["periods"]["current"]["end"]
    current_days = (_date(current_end) - _date(current_start)).days
    loaded_periods = {(c.period_start, c.period_end) for c in candidates}

    refs, rejections, notes = {}, {}, []
    if not candidates:
        notes.append(note("info", "suppressed", "No earlier report is available as a baseline for this client."))
    for dimension in el.DIMENSIONS:
        rejected, chosen = [], None
        if not current_assessment[dimension].eligible:
            refs[dimension], rejections[dimension] = None, ()
            continue
        for candidate in candidates:
            reasons = list(assessments[candidate.run_id][dimension].reasons) + list(
                compatibility[candidate.run_id][dimension])
            if reasons:
                rejected.append((candidate.run_id, reasons[0]))
            else:
                chosen = candidate
                break
        rejections[dimension] = tuple(rejected)
        if chosen is None:
            refs[dimension] = None
            if candidates:
                notes.append(note("info", "suppressed", f"{dimension}: no eligible baseline was found among the "
                                   f"{len(candidates)} earlier report(s) considered."))
            continue
        days = (_date(chosen.period_end) - _date(chosen.period_start)).days
        refs[dimension] = BaselineRef(
            run_id=chosen.run_id, revision=chosen.revision, period_start=chosen.period_start,
            period_end=chosen.period_end, created_at=chosen.created_at, rank=chosen.rank,
            gap_days=(_date(current_start) - _date(chosen.period_end)).days - 1, same_length=days == current_days)
        # Nearer reports that were excluded for quality reasons before ranking (voided, damaged, unsupported).
        nearer_excluded = [s for s in skipped
                           if (s.period_start, s.period_end) not in loaded_periods
                           and chosen.period_end < s.period_end < current_start
                           and (s.reason == "voided" or s.reason.startswith("failed verification")
                                or s.reason == "unsupported snapshot format")]
        if rejected or nearer_excluded:
            why = "; ".join([f"run {rid}: {reason}" for rid, reason in rejected]
                            + [f"run {s.run_id} ({s.period_start} to {s.period_end}): {s.reason}"
                               for s in nearer_excluded])
            notes.append(note("info", "data_quality",
                               f"{dimension}: the baseline is run {chosen.run_id} ({chosen.period_start} to "
                               f"{chosen.period_end}), an older report than the nearest earlier one ({why})."))

    for run_id in dict.fromkeys(ref.run_id for ref in refs.values() if ref is not None):
        ref = next(r for r in refs.values() if r is not None and r.run_id == run_id)
        if ref.gap_days > 0:
            notes.append(note("info", "data_quality", f"Baseline run {run_id} ends {ref.gap_days} day(s) before the "
                                                       "current period starts."))
        if not ref.same_length:
            notes.append(note("warning", "data_quality", f"Baseline run {run_id} covers a different number of days; "
                                                          "counts were not compared, only rates and positions."))
    return BaselineSelection(refs, {c.run_id: c.payload for c in candidates}, assessments, rejections,
                             tuple(skipped), tuple(notes))
