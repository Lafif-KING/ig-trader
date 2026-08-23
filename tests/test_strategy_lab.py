"""Offline contract tests for the multi-instrument Strategy Lab."""

# ruff: noqa: E501

from __future__ import annotations

import ast
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from src.ig_trader.strategy_lab.artifacts import LeaderboardEntry, load_leaderboard, write_artifacts
from src.ig_trader.strategy_lab.data import (
    CanonicalDataset,
    DataContractError,
    GapClassification,
    LabCandle,
    LocalDatasetSource,
    SourceQuality,
    build_dataset,
)
from src.ig_trader.strategy_lab.engine import (
    BacktestConfig,
    CandleBacktestEngine,
    FrictionModel,
    ParameterEvaluation,
    QualificationStatus,
    SplitConfig,
    StrategyVersion,
    VersionRegistry,
    analyse_portfolio,
    chronological_splits,
    classify_result,
    compare_challenger,
    detect_overfit,
    walk_forward_windows,
)
from src.ig_trader.strategy_lab.models import (
    INITIAL_INSTRUMENT_REGISTRY,
    INITIAL_INSTRUMENTS,
    AssetClass,
    InstrumentSpec,
    StrategyFamily,
    Timeframe,
    is_timeframe_compatible,
    suitable_families,
)
from src.ig_trader.strategy_lab.runner import StrategyLabRunner
from src.ig_trader.strategy_lab.strategies import (
    Direction,
    ResearchSignal,
    StrategyDefinition,
    strategy_registry,
)


def _candle(index: int, *, price: Decimal | None = None) -> LabCandle:
    close = price if price is not None else Decimal("100") + Decimal(index) / Decimal("10")
    return LabCandle(
        instrument="EURUSD",
        timestamp_utc=datetime(2025, 1, 2, tzinfo=UTC) + timedelta(minutes=5 * index),
        timeframe=Timeframe.M5,
        open=close - Decimal("0.05"),
        high=close + Decimal("0.20"),
        low=close - Decimal("0.20"),
        close=close,
        spread=Decimal("0.02"),
        volume=Decimal("100"),
        source="UNIT_TEST_LOCAL_DATA",
        source_quality=SourceQuality.LOCAL_RESEARCH,
        gap_classification=GapClassification.NONE,
        synthetic=False,
    )


def _dataset(count: int = 160) -> CanonicalDataset:
    return build_dataset(tuple(_candle(index) for index in range(count)))


def _friction() -> FrictionModel:
    return FrictionModel(
        tick_size=Decimal("0.01"),
        typical_spread=Decimal("0.02"),
        slippage=Decimal("0.01"),
        commission_price_equivalent=Decimal("0.01"),
        minimum_stop_distance=Decimal("0.20"),
        minimum_size=Decimal("1"),
        allowed_utc_hours=frozenset(range(24)),
    )


class _AlwaysLong:
    definition = StrategyDefinition(
        "TEST", StrategyFamily.S2_BREAKOUT, "1.0.0", (AssetClass.FX,), (Timeframe.M5,)
    )

    def __init__(self) -> None:
        self.histories: list[tuple[LabCandle, ...]] = []

    def signal(self, history: tuple[LabCandle, ...]) -> ResearchSignal | None:
        self.histories.append(history)
        return ResearchSignal(Direction.LONG, Decimal("1"), "test") if len(history) == 22 else None


def test_initial_registry_has_full_research_universe_and_no_invented_epics() -> None:
    assert len(INITIAL_INSTRUMENTS) == 26
    assert {item.asset_class for item in INITIAL_INSTRUMENTS} == set(AssetClass)
    assert all(item.ig_epic is None for item in INITIAL_INSTRUMENTS)
    assert all(
        item.execution_status.value == "NOT_AN_EXECUTION_ALLOWLIST" for item in INITIAL_INSTRUMENTS
    )


def test_suitability_matrix_is_limited_hypotheses_not_bruteforce() -> None:
    assert StrategyFamily.S3_MEAN_REVERSION in suitable_families(
        INITIAL_INSTRUMENT_REGISTRY["EURGBP"]
    )
    assert StrategyFamily.S5_VOLATILITY_REGIME in suitable_families(
        INITIAL_INSTRUMENT_REGISTRY["GBPJPY"]
    )
    assert StrategyFamily.S2_BREAKOUT in suitable_families(INITIAL_INSTRUMENT_REGISTRY["XAUUSD"])
    assert StrategyFamily.S0_FROZEN_RSI_ADX not in suitable_families(
        INITIAL_INSTRUMENT_REGISTRY["EURUSD"]
    )


def test_timeframe_matrix_covers_required_timeframes_and_rejects_invalid_pairing() -> None:
    assert {item for item in Timeframe} == {
        Timeframe.H4,
        Timeframe.H1,
        Timeframe.M15,
        Timeframe.M5,
        Timeframe.M1,
    }
    assert is_timeframe_compatible(StrategyFamily.S2_BREAKOUT, AssetClass.FX, Timeframe.M5)
    assert not is_timeframe_compatible(
        StrategyFamily.S3_MEAN_REVERSION, AssetClass.METAL, Timeframe.H4
    )


def test_registry_rejects_non_positive_verified_market_metadata() -> None:
    with pytest.raises(ValueError, match="positive"):
        InstrumentSpec("TEST", AssetClass.FX, "Test", pip_or_tick_size=Decimal("0"))


def test_data_contract_rejects_invalid_ohlc_and_duplicate_timestamps() -> None:
    with pytest.raises(DataContractError, match="invalid OHLC"):
        replace(_candle(0), low=Decimal("101"))
    with pytest.raises(DataContractError, match="duplicate"):
        build_dataset((_candle(0), _candle(0)))


def test_data_contract_normalizes_utc_and_fingerprints_source_and_dataset() -> None:
    dataset = _dataset()
    assert dataset.candles[0].timestamp_utc.tzinfo is UTC
    assert len(dataset.source_fingerprint) == len(dataset.dataset_fingerprint) == 64


def test_gaps_are_explicit_and_missing_data_is_quality_failure() -> None:
    dataset = build_dataset((_candle(0), _candle(2)))
    assert dataset.gaps[0].classification is GapClassification.MISSING_DATA
    assert dataset.has_quality_failure


def test_weekend_gap_is_recorded_without_hiding_it() -> None:
    friday = replace(_candle(0), timestamp_utc=datetime(2025, 1, 3, 23, 55, tzinfo=UTC))
    monday = replace(_candle(1), timestamp_utc=datetime(2025, 1, 6, 0, 0, tzinfo=UTC))
    dataset = build_dataset((friday, monday))
    assert dataset.gaps[0].classification is GapClassification.WEEKEND_OR_SESSION


def test_local_source_loads_explicitly_synthetic_fixture() -> None:
    fixture = Path(__file__).parents[1] / "fixtures" / "strategy_lab" / "eurusd_5m.csv"
    dataset = LocalDatasetSource().load(fixture)
    assert dataset.candles and all(candle.synthetic for candle in dataset.candles)
    assert {item.source_quality for item in dataset.candles} == {SourceQuality.SYNTHETIC_TEST_ONLY}


def test_strategy_registry_contains_s0_to_s7_and_s0_is_frozen_baseline() -> None:
    registry = strategy_registry()
    assert set(registry) == {f"S{number}" for number in range(8)}
    assert registry["S0"].definition.baseline_only
    assert "optimization is prohibited" in registry["S0"].definition.change_reason


def test_strategy_signals_depend_only_on_past_and_closed_history() -> None:
    strategy = _AlwaysLong()
    result = CandleBacktestEngine().run(_dataset(), strategy, _friction())
    assert result.trades
    assert strategy.histories[0][-1].timestamp_utc < result.trades[0].entry_timestamp_utc


def test_backtest_is_deterministic_and_configuration_fingerprinted() -> None:
    engine = CandleBacktestEngine()
    first = engine.run(_dataset(), _AlwaysLong(), _friction(), BacktestConfig(seed=7))
    second = engine.run(_dataset(), _AlwaysLong(), _friction(), BacktestConfig(seed=7))
    assert first.trades == second.trades
    assert first.configuration_fingerprint == second.configuration_fingerprint


def test_backtest_requires_complete_instrument_specific_cost_model() -> None:
    result = CandleBacktestEngine().run(
        _dataset(), _AlwaysLong(), replace(_friction(), tick_size=None)
    )
    assert result.status is QualificationStatus.COST_MODEL_INCOMPLETE
    assert not result.trades


def test_backtest_does_not_enter_outside_explicit_market_hours() -> None:
    friction = replace(_friction(), allowed_utc_hours=frozenset({7}))
    result = CandleBacktestEngine().run(_dataset(), _AlwaysLong(), friction)
    assert not result.trades


def test_same_candle_stop_and_target_ambiguity_fails_conservatively() -> None:
    candles = list(_dataset(30).candles)
    candles[22] = replace(candles[22], high=Decimal("104"), low=Decimal("100"))
    result = CandleBacktestEngine().run(build_dataset(tuple(candles)), _AlwaysLong(), _friction())
    assert result.trades[0].exit_reason == "STOP"
    assert result.trades[0].r_multiple < Decimal("-1")


def test_metrics_include_risk_cost_and_distribution_fields() -> None:
    result = CandleBacktestEngine().run(_dataset(), _AlwaysLong(), _friction())
    metrics = result.metrics
    assert metrics.trade_count == 1
    assert metrics.spread_cost > 0 and metrics.slippage_cost > 0 and metrics.commission_cost > 0
    assert metrics.by_session and metrics.by_weekday and metrics.by_month


def test_chronological_splits_keep_final_test_untouched_until_explicit_release() -> None:
    splits = chronological_splits(_dataset(), SplitConfig(release_untouched_test=False))
    assert not splits.test_released
    assert splits.development.candles[-1].timestamp_utc < splits.validation.candles[0].timestamp_utc
    assert (
        splits.validation.candles[-1].timestamp_utc < splits.untouched_test.candles[0].timestamp_utc
    )


def test_walk_forward_windows_are_chronological() -> None:
    windows = walk_forward_windows(_dataset(), development_size=60, validation_size=20, step=20)
    assert len(windows) == 5
    assert all(
        window.development.candles[-1].timestamp_utc < window.validation.candles[0].timestamp_utc
        for window in windows
    )


@pytest.mark.parametrize(
    ("trades", "expected"),
    ((0, QualificationStatus.INSUFFICIENT_TRADES), (30, QualificationStatus.LOW_SAMPLE_CONFIDENCE)),
)
def test_low_sample_classifications_are_conservative(
    trades: int, expected: QualificationStatus
) -> None:
    result = CandleBacktestEngine().run(_dataset(), _AlwaysLong(), _friction())
    metrics = replace(result.metrics, trade_count=trades)
    assert classify_result(replace(result, metrics=metrics)) is expected


def test_overfit_detection_flags_isolated_peak_and_test_collapse() -> None:
    evaluations = (
        ParameterEvaluation((("period", "10"),), Decimal("-0.1"), Decimal("-0.1"), 40),
        ParameterEvaluation((("period", "11"),), Decimal("2"), Decimal("-1"), 40),
        ParameterEvaluation((("period", "12"),), Decimal("0"), Decimal("0"), 40),
    )
    assert detect_overfit(evaluations)


def test_version_registry_is_append_only() -> None:
    version = StrategyVersion(
        "S2",
        "EURUSD",
        "S2",
        "1.0.0",
        (),
        "a" * 64,
        None,
        "initial",
        "b" * 64,
        None,
        None,
        QualificationStatus.RESEARCH_WATCH,
    )
    registry = VersionRegistry()
    registry.add(version)
    with pytest.raises(ValueError, match="silently overwritten"):
        registry.add(version)


def test_champion_challenger_requires_better_evidence_not_net_pnl() -> None:
    champion = CandleBacktestEngine().run(_dataset(), _AlwaysLong(), _friction())
    challenger = replace(
        champion,
        strategy=replace(champion.strategy, version="1.1.0"),
        metrics=replace(champion.metrics, trade_count=0),
    )
    assert compare_challenger(champion, challenger).outcome == "CHALLENGER_RETAINED"


def test_portfolio_analysis_reports_correlation_and_shared_fx_concentration() -> None:
    analysis = analyse_portfolio(
        {"EURUSD-S1": (Decimal("1"), Decimal("-1")), "GBPUSD-S1": (Decimal("2"), Decimal("-2"))},
        {
            "EURUSD": INITIAL_INSTRUMENT_REGISTRY["EURUSD"],
            "GBPUSD": INITIAL_INSTRUMENT_REGISTRY["GBPUSD"],
        },
    )
    assert analysis.correlations and analysis.currency_concentration["USD"] == 2
    assert 0 <= analysis.diversification_score <= 100


def test_artifact_generation_writes_required_machine_readable_evidence(tmp_path: Path) -> None:
    result = CandleBacktestEngine().run(_dataset(), _AlwaysLong(), _friction())
    entry = LeaderboardEntry.from_result(
        result, instrument="EURUSD", asset_class=AssetClass.FX, timeframe=Timeframe.M5
    )
    paths = write_artifacts(tmp_path, (entry,), data_quality=({"quality_status": "PASS"},))
    assert {
        "run_manifest.json",
        "leaderboard.csv",
        "leaderboard.json",
        "instrument_summary.json",
        "strategy_summary.json",
        "rejections.json",
        "champion_challenger.json",
        "data_quality.json",
    } == set(paths)
    assert load_leaderboard(paths["leaderboard.json"])[0]["instrument"] == "EURUSD"


def test_runner_and_cli_work_offline_and_never_promote_unknown_cost_data(
    tmp_path: Path, capsys
) -> None:
    runner = StrategyLabRunner()
    run = runner.run_one("EURUSD", "S2", Timeframe.M5)
    assert run.entries[0].status is QualificationStatus.COST_MODEL_INCOMPLETE
    paths = runner.write(run, tmp_path)
    assert paths["leaderboard.json"].exists()
    from src.ig_trader.strategy_lab.__main__ import main

    assert main(["list-instruments"]) == 0
    assert "EURUSD" in capsys.readouterr().out


def test_batch_records_data_unavailable_without_contacting_any_source(tmp_path: Path) -> None:
    run = StrategyLabRunner(fixture_directory=tmp_path).batch_initial()
    assert run.entries and all(
        item.status is QualificationStatus.DATA_NOT_AVAILABLE for item in run.entries
    )


def test_package_has_no_network_or_broker_mutation_imports() -> None:
    root = Path(__file__).parents[1] / "src" / "ig_trader" / "strategy_lab"
    prohibited = {
        "httpx",
        "requests",
        "socket",
        "session",
        "execution",
        "demo_execution",
        "lightstreamer",
    }
    for path in root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        modules = {
            node.module.split(".")[-1]
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module
        }
        modules.update(
            alias.name.split(".")[0]
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        )
        assert not modules.intersection(prohibited), path
        methods = {
            node.func.attr.casefold()
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        }
        assert not methods.intersection(
            {"post", "put", "patch", "delete", "create_position", "close_position"}
        ), path


def test_sample_fixture_is_not_claimed_as_ig_broker_data() -> None:
    fixture = Path(__file__).parents[1] / "fixtures" / "strategy_lab" / "eurusd_5m.csv"
    text = fixture.read_text(encoding="utf-8")
    assert "SYNTHETIC_TEST_ONLY" in text
    assert "IG_VERIFIED" not in text
    assert json.loads(json.dumps({"fixture": "safe"}))["fixture"] == "safe"
