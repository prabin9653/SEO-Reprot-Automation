"""Report assembly, historical comparison and insights for one client (R8.3).

Connects existing, unchanged pieces in the order the tests have always used (`tests/test_reviewer_record.pipeline`):

  report_run.assemble_report           in-memory bundle: base insights, snapshot, optional history comparison
  history_rules.evaluate_history_rules the historical rules (pure)
  history_engine.compose_history_insights  the client statements and reviewer findings (pure)
  history_recommendations.annotate_recommendations  reviewer-only annotations (pure)

The reporting period is exactly the client's explicit 28-day period (`ClientRun.periods`). Nothing is stored; the history
database and the output root are only read (through `assemble_report`'s finalized-only reader). Missing metrics are never
invented: absent data stays absent and the existing notices describe it.
"""

from dataclasses import dataclass

from seo_report import report_run
from seo_report.insights import history_engine as he
from seo_report.insights import history_recommendations as hrec
from seo_report.insights import history_rules as hr
from seo_report.snapshots.baselines import HistoryConfig


@dataclass(frozen=True)
class AssembledReport:
    bundle: report_run.ReportBundle
    history_insights: he.HistoryInsights
    annotations: hrec.RecommendationHistoryAnnotations
    history_config: HistoryConfig


def assemble(client_run, sources, *, registry, settings, history_config: HistoryConfig | None = None,
             environ=None) -> AssembledReport:
    """Assemble `client_run` from collected `sources` (a `SourceResult` whose sources are both available)."""
    config = history_config if history_config is not None else HistoryConfig()
    bundle = report_run.assemble_report(
        client=client_run.client, periods=client_run.periods, gsc=sources.gsc, ga4=sources.ga4, registry=registry,
        history_db_path=settings.history_db_path, output_root=settings.output_root, environ=environ,
        history_config=config)
    rules = hr.evaluate_history_rules(
        comparison=bundle.historical_comparison, metrics=bundle.base_insights.metrics,
        base_insights=bundle.base_insights, crosscheck=bundle.reviewer_report.ranking_crosscheck,
        target_country=client_run.client.target_country or None)
    composed = he.compose_history_insights(rule_output=rules, base_insights=bundle.base_insights)
    annotations = hrec.annotate_recommendations(
        recommendations=bundle.base_insights.recommendations, comparison=bundle.historical_comparison,
        history_insights=composed)
    return AssembledReport(bundle, composed, annotations, config)
