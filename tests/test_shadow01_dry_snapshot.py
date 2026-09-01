"""Regression coverage for the strictly non-persisting Shadow01 dry snapshot."""

from __future__ import annotations

import ast
import inspect
import sqlite3
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

import src.ig_trader.shadow01.dry_snapshot as dry_snapshot_module
from src.ig_trader.shadow01.config import ShadowTournamentConfig, load_config
from src.ig_trader.shadow01.dry_snapshot import (
    DrySnapshotContext,
    DrySnapshotMarketInput,
    ShadowDrySnapshotError,
    ShadowDrySnapshotService,
    run_shadow_dry_snapshot,
)
from src.ig_trader.shadow01.engines import CostInputs, CrossAssetInput, FundamentalInputs
from src.ig_trader.shadow01.live_quote import build_ig_price_stream_quote
from src.ig_trader.shadow01.models import DailyBar, PolicyId, QualityState, fingerprint
from src.ig_trader.shadow01.read_only_broker import Shadow01ReadOnlyBroker
from src.ig_trader.shadow01.registry import ShadowMarketRegistry, load_verified_dq03_registry
from src.ig_trader.shadow01.storage import ShadowTournamentStore
from tests.shadow01_dq03_fixtures import write_verified_dq03_documents

NOW = datetime(2026, 9, 2, 21, 10, tzinfo=UTC)


class PoisonTransport:
    """Any broker use is a test failure: supplied facts are the only input surface."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def authorized_request(self, method: str, endpoint: str, **_: object) -> object:
        self.calls.append((method, endpoint))
        raise AssertionError("dry snapshot must not call the broker")


def _registry(tmp_path: Path, config: ShadowTournamentConfig) -> ShadowMarketRegistry:
    dq03 = tmp_path / "dq03"
    dq03.mkdir()
    write_verified_dq03_documents(dq03, config)
    return load_verified_dq03_registry(config, dq03 / "instrument_registry.json")


def _bars() -> tuple[DailyBar, ...]:
    start = NOW - timedelta(minutes=10, days=299)
    return tuple(
        DailyBar(
            completed_at=start + timedelta(days=index),
            open=100.0 + index,
            high=100.5 + index,
            low=99.5 + index,
            close=100.0 + index,
        )
        for index in range(300)
    )


def _market_input(*, epic: str = "TEST.EURUSD") -> DrySnapshotMarketInput:
    return DrySnapshotMarketInput(
        instrument="EURUSD",
        epic=epic,
        completed_bars=_bars(),
        input_data_fingerprint=fingerprint({"fixture": "dry-snapshot-eurusd"}),
        cost_inputs=CostInputs(
            reference_price=399.0,
            spread=0.02,
            minimum_stop_distance=1.0,
            product_type="CASH",
            funding_metadata="DFB",
        ),
        cross_asset_inputs=(
            CrossAssetInput(
                name="SANITIZED_CONTEXT",
                alignment_score=0.0,
                available_at_utc=NOW - timedelta(seconds=1),
                source="test-only",
            ),
        ),
        fundamental_inputs=FundamentalInputs(
            data_quality="NORMAL",
            event_risk=False,
            available_at_utc=NOW - timedelta(seconds=1),
        ),
    )


def _context(*, epic: str = "TEST.EURUSD") -> DrySnapshotContext:
    return DrySnapshotContext(
        observed_at_utc=NOW,
        markets=(_market_input(epic=epic),),
        provider_healthy=True,
        stream_healthy=True,
        session_complete=True,
    )


def _live_quote():
    return build_ig_price_stream_quote(
        epic="TEST.EURUSD",
        symbol="EURUSD",
        bid_value="1.0000",
        ask_value="1.0002",
        timestamp_milliseconds=int((NOW - timedelta(seconds=30)).timestamp() * 1000),
        market_state="DEAL",
        observed_at=NOW,
        maximum_age_seconds=60,
    )


def _database_counts(path: Path) -> tuple[int, int, int]:
    with sqlite3.connect(path) as connection:
        epoch_count = connection.execute(
            "SELECT COUNT(*) FROM tournament_runs WHERE epoch_utc IS NOT NULL"
        ).fetchone()[0]
        decision_count = connection.execute("SELECT COUNT(*) FROM shadow_decisions").fetchone()[0]
        outcome_count = connection.execute("SELECT COUNT(*) FROM outcome_labels").fetchone()[0]
    return epoch_count, decision_count, outcome_count


def test_dry_snapshot_runs_all_engines_and_policies_without_broker_or_database_writes(
    tmp_path: Path,
) -> None:
    config = load_config()
    registry = _registry(tmp_path, config)
    transport = PoisonTransport()
    broker = Shadow01ReadOnlyBroker(transport)
    store = ShadowTournamentStore(tmp_path / "shadow.sqlite3")
    store.initialize()
    before_epoch = store.epoch(config)
    before_counts = _database_counts(store.path)

    result = run_shadow_dry_snapshot(
        config=config,
        registry=registry,
        broker=broker,
        context=_context(),
    )

    market = result.markets[0]
    assert result.status == "DRY_RUN_NON_PROSPECTIVE"
    assert result.dry_run_non_prospective is True
    assert result.execution_authority == "OFF"
    assert result.epoch_created is False
    assert result.prospective_decisions_created == 0
    assert result.outcomes_created == 0
    assert result.demo_robot_starts == 0
    assert market.technical_state.return_60 is not None
    assert market.trend.reason_codes
    assert market.reversion.reason_codes
    assert market.cross_asset.reason_codes
    assert market.fundamental.reason_codes
    assert market.quality.reason_codes
    assert market.cost.reason_codes
    assert tuple(item.recommendation.policy_id for item in market.policies) == tuple(PolicyId)
    assert transport.calls == []
    assert result.broker_counters_before == result.broker_counters_after
    assert result.broker_counters_after.total_rest_request_count == 0
    assert broker.execution_authority == "OFF"
    assert store.epoch(config) is before_epoch is None
    assert _database_counts(store.path) == before_counts == (0, 0, 0)


def test_completed_clock_context_does_not_leave_q1_blocked_solely_for_clock(
    tmp_path: Path,
) -> None:
    config = load_config()
    registry = _registry(tmp_path, config)
    broker = Shadow01ReadOnlyBroker(PoisonTransport())

    result = run_shadow_dry_snapshot(
        config=config,
        registry=registry,
        broker=broker,
        context=_context(),
    )

    quality = result.markets[0].quality
    assert quality.state is not QualityState.BLOCKED
    assert not any(code.startswith("Q1_SESSION_") for code in quality.reason_codes)
    assert result.status == "DRY_RUN_NON_PROSPECTIVE"
    assert result.epoch_created is False
    assert result.prospective_decisions_created == 0


def test_c1_uses_the_same_valid_stream_tier_one_quote_without_broker_activity(
    tmp_path: Path,
) -> None:
    config = load_config()
    registry = _registry(tmp_path, config)
    transport = PoisonTransport()
    broker = Shadow01ReadOnlyBroker(transport)
    market = replace(_market_input(), live_quote=_live_quote())
    context = DrySnapshotContext(
        observed_at_utc=NOW,
        markets=(market,),
        provider_healthy=True,
        stream_healthy=True,
        session_complete=True,
    )

    result = run_shadow_dry_snapshot(
        config=config,
        registry=registry,
        broker=broker,
        context=context,
    )

    assert result.markets[0].cost.spread == pytest.approx(0.0002)
    assert transport.calls == []
    assert result.broker_counters_before == result.broker_counters_after


def test_dry_snapshot_uses_one_explicit_timestamp_for_each_engine_and_policy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = load_config()
    registry = _registry(tmp_path, config)
    broker = Shadow01ReadOnlyBroker(PoisonTransport())
    timestamps: list[datetime] = []
    names = (
        "assess_q1_quality",
        "compute_technical_state",
        "evaluate_m1_reversion",
        "evaluate_x1_context",
        "build_f1_context",
    )

    for name in names:
        original = getattr(dry_snapshot_module, name)

        def record(*args: object, _original: object = original, **kwargs: object) -> object:
            timestamp = kwargs.get("decision_timestamp_utc")
            assert isinstance(timestamp, datetime)
            timestamps.append(timestamp)
            return _original(*args, **kwargs)  # type: ignore[operator]

        monkeypatch.setattr(dry_snapshot_module, name, record)

    service = ShadowDrySnapshotService(config=config, registry=registry, broker=broker)
    result = service.run(_context())

    assert timestamps == [NOW] * len(names)
    assert result.observed_at_utc == NOW
    assert {market.diagnostic_timestamp_utc for market in result.markets} == {NOW}
    assert {
        policy.diagnostic_timestamp_utc for market in result.markets for policy in market.policies
    } == {NOW}


def test_dry_snapshot_omitted_health_facts_remain_unknown_and_are_not_invented(
    tmp_path: Path,
) -> None:
    config = load_config()
    registry = _registry(tmp_path, config)
    broker = Shadow01ReadOnlyBroker(PoisonTransport())
    context = DrySnapshotContext(
        observed_at_utc=NOW,
        markets=(_market_input(),),
        stream_healthy=True,
    )

    result = run_shadow_dry_snapshot(
        config=config,
        registry=registry,
        broker=broker,
        context=context,
    )

    assert result.markets[0].quality.state is QualityState.UNKNOWN
    assert "Q1_PROVIDER_HEALTH_UNKNOWN" in result.markets[0].quality.reason_codes
    assert "Q1_SESSION_HEALTH_UNKNOWN" in result.markets[0].quality.reason_codes
    assert broker.request_counters.total_rest_request_count == 0


def test_dry_snapshot_rejects_an_unverified_epic_before_any_broker_activity(tmp_path: Path) -> None:
    config = load_config()
    registry = _registry(tmp_path, config)
    transport = PoisonTransport()
    broker = Shadow01ReadOnlyBroker(transport)

    with pytest.raises(ShadowDrySnapshotError, match="SHADOW01_DRY_SNAPSHOT_UNVERIFIED_EPIC"):
        run_shadow_dry_snapshot(
            config=config,
            registry=registry,
            broker=broker,
            context=_context(epic="TEST.UNVERIFIED"),
        )

    assert transport.calls == []
    assert broker.request_counters.total_rest_request_count == 0
    assert broker.execution_authority == "OFF"


def test_dry_snapshot_requires_an_authoritative_dq03_source_before_broker_activity(
    tmp_path: Path,
) -> None:
    config = load_config()
    registry = _registry(tmp_path, config)
    transport = PoisonTransport()
    broker = Shadow01ReadOnlyBroker(transport)

    with pytest.raises(
        ShadowDrySnapshotError,
        match="SHADOW01_DRY_SNAPSHOT_DQ03_PROVENANCE_REQUIRED",
    ):
        run_shadow_dry_snapshot(
            config=config,
            registry=replace(registry, source_path=None),
            broker=broker,
            context=_context(),
        )

    assert transport.calls == []
    assert broker.request_counters.total_rest_request_count == 0


def test_dry_snapshot_module_has_no_storage_runtime_or_execution_surface() -> None:
    source_path = Path(inspect.getsourcefile(dry_snapshot_module) or "")
    source = source_path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    imports = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module is not None
    }
    imports.update(
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    )

    assert not any(
        name.startswith(
            (
                "src.ig_trader.shadow01.runtime",
                "src.ig_trader.shadow01.storage",
                "src.ig_trader.shadow01.outcomes",
                "src.ig_trader.demo",
                "src.ig_trader.execution",
                "src.ig_trader.shadow_execution",
            )
        )
        for name in imports
    )
    assert "materialize_decisions" not in source
    assert "ShadowDecision" not in source
    assert ShadowDrySnapshotService.execution_authority == "OFF"
