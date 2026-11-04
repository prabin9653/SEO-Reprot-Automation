"""R8.3: report assembly, historical comparison and insights through the service (unchanged engine underneath)."""
import dataclasses
import datetime as dt
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from seo_report import report_service
from seo_report.client_config import ClientEntry, ClientRegistry, UnknownClientError
from seo_report.insights import engine
from seo_report.snapshots.baselines import HistoryConfig
from test_history_access import env  # noqa: F401
from test_pipeline import ClientRun, default_collect, make_settings, world  # noqa: F401
from test_report_run import CURRENT, KEYWORDS, REGISTRY, with_history

TODAY = dt.date(2026, 11, 2)


def client_run(w, key="example-co"):
    return w.services.clients.clients[key]


def assemble(w, key="example-co", **kw):
    run = client_run(w, key)
    return report_service.assemble(run, default_collect(run, TODAY), registry=w.registry, settings=w.settings, **kw)


def tree(folder):
    return {str(p): p.read_bytes() for p in Path(folder).rglob("*") if p.is_file()} if Path(folder).exists() else None


def test_the_bundle_covers_exactly_the_clients_explicit_period(world):
    w = world()
    assembled = assemble(w)
    snapshot = assembled.bundle.snapshot
    assert (snapshot.client_key, snapshot.period_start, snapshot.period_end) == (
        "example-co", CURRENT.current.start.isoformat(), CURRENT.current.end.isoformat())
    periods = assembled.bundle.client_content.periods
    assert (periods.current, periods.previous) == (CURRENT.current, CURRENT.previous)
    assert assembled.history_config == HistoryConfig()
    assert assembled.bundle.historical_comparison is None  # no history database exists
    assert assembled.history_insights.client_statements == ()


def test_the_base_insights_are_exactly_what_the_existing_engine_returns(world):
    w = world()
    run = client_run(w)
    sources = default_collect(run, TODAY)
    assembled = assemble(w)
    direct = engine.generate_insights(gsc=sources.gsc, ga4=sources.ga4, keywords=tuple(run.client.keywords),
                                      target_country=run.client.target_country or None,
                                      recommendation_config=engine.RecommendationConfig())
    assert assembled.bundle.base_insights == direct
    assert len(assembled.annotations.as_record()) == len(assembled.bundle.base_insights.recommendations)  # one each


def test_assembly_is_deterministic_and_the_render_identity_inputs_are_stable(world):
    w = world()
    first, second = assemble(w), assemble(w)
    assert first.bundle.snapshot.content_hash == second.bundle.snapshot.content_hash
    assert first.bundle.client_content == second.bundle.client_content
    assert first.history_insights == second.history_insights


def test_nothing_is_written_by_assembly(world):
    w = world()
    assemble(w)
    assert not w.settings.output_root.exists() and not w.settings.history_db_path.exists()
    assert not w.settings.work_root.exists() and not w.settings.ledger_db_path.parent.parent.joinpath("history").exists()


def test_a_finalized_earlier_period_gives_a_historical_comparison(env, world, tmp_path):
    with_history(env)
    w = world()
    settings = dataclasses.replace(w.settings, history_db_path=env.db, output_root=env.out)
    run = client_run(w)
    assembled = report_service.assemble(run, default_collect(run, TODAY), registry=w.registry, settings=settings)
    assert assembled.bundle.historical_comparison is not None
    assert assembled.bundle.reviewer_report.history_status.state == "available"
    assert any(b for b in assembled.bundle.reviewer_report.history.baselines)
    before = tree(env.tmp)
    report_service.assemble(run, default_collect(run, TODAY), registry=w.registry, settings=settings)
    assert tree(env.tmp) == before  # the history is only read


def test_an_unregistered_client_stops_the_assembly(world):
    w = world()
    run = client_run(w)
    with pytest.raises(UnknownClientError):
        report_service.assemble(run, default_collect(run, TODAY), registry=ClientRegistry([ClientEntry("Other Co", "other-co")]),
                                settings=w.settings)


def test_a_custom_history_configuration_is_passed_through(world):
    w = world()
    config = HistoryConfig(lookback_periods=2, max_gap_days=40, list_limit=7)
    assert assemble(w, history_config=config).history_config == config


def test_missing_data_is_not_invented(world):
    w = world()
    run = client_run(w)
    sources = default_collect(run, TODAY)
    empty = dataclasses.replace(sources.gsc, current=dataclasses.replace(
        sources.gsc.current, totals=dataclasses.replace(sources.gsc.current.totals, clicks=0, impressions=0, ctr=None,
                                                         position=None), daily=[], countries=[], queries=[], pages=[]))
    assembled = report_service.assemble(run, dataclasses.replace(sources, gsc=empty), registry=w.registry,
                                        settings=w.settings)
    snapshot = assembled.bundle.snapshot
    assert snapshot.content_hash != assemble(w).bundle.snapshot.content_hash
    metrics = assembled.bundle.base_insights.metrics.gsc
    assert (metrics.clicks.current, metrics.ctr.current, metrics.position.current) == (None, None, None)  # absent, not zero
