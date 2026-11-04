"""Historical comparison against a stored history (read-only).

The only module of the comparison stage that touches the store. It reads candidate runs through a
`SnapshotReader` (or a `SnapshotStore`, which has the same read methods), verifies every candidate
before using it, and hands the loaded payloads to the pure selection and comparison code. It never
writes, and it never uses a run that failed verification.
"""

from seo_report.snapshots import baselines as bl
from seo_report.snapshots import comparison as cmp
from seo_report.snapshots import eligibility as el
from seo_report.snapshots.baselines import HistoryConfig, LoadedRun, SkippedRun


def compare_with_history(current_payload: dict, reader, config: HistoryConfig = HistoryConfig()) -> cmp.HistoricalComparison:
    """Compare a snapshot payload (built or stored) with the client's earlier reports.

    The current run's own period (including any earlier revisions of it) is never a baseline.
    """
    if current_payload.get("format") not in el.SUPPORTED_FORMATS:
        raise ValueError("the current snapshot format is not supported")
    client_key = current_payload["client"]["client_key"]
    start = current_payload["periods"]["current"]["start"]
    end = current_payload["periods"]["current"]["end"]

    plan = bl.plan_candidates(current_start=start, current_end=end,
                              run_infos=reader.list_runs(client_key, include_voided=True), config=config)
    loaded, skipped = [], list(plan.skipped)
    for rank, info in enumerate(plan.ordered, start=1):
        report = reader.verify_run(info.run_id)
        if not report.ok:
            skipped.append(SkippedRun(info.run_id, info.revision, info.period_start, info.period_end,
                                      f"failed verification ({len(report.problems)} problem(s)); not used"))
            continue
        stored = reader.get(info.run_id)
        if stored.payload.get("format") not in el.SUPPORTED_FORMATS:
            skipped.append(SkippedRun(info.run_id, info.revision, info.period_start, info.period_end,
                                      "unsupported snapshot format"))
            continue
        loaded.append(LoadedRun(info.run_id, info.revision, info.period_start, info.period_end, stored.created_at,
                                rank, stored.payload))
    skipped.sort(key=lambda s: (s.period_start, s.period_end, s.revision, s.run_id))
    selection = bl.select_baselines(current_payload, loaded, config, skipped)
    return cmp.compare(current_payload, selection, config)
