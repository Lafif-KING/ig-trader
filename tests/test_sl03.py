"""Focused SL-03 regression tests; all data is synthetic and offline."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from dashboard.sources.strategy_lab import load_strategy_lab_snapshot
from src.ig_trader.sl03.artifacts import REQUIRED_ARTIFACTS, write_sl03_artifacts
from src.ig_trader.sl03.history import MultiSourceHistory, resample_complete
from src.ig_trader.sl03.quality import EXPECTED_WEEKEND, UNEXPLAINED, audit_dataset
from src.ig_trader.sl03.runner import _bootstrap, _simulate
from src.ig_trader.sl03.strategies import sl03_challenger_variants
from src.ig_trader.strategy_lab.data import (
    GapClassification,
    LabCandle,
    SourceQuality,
    build_dataset,
)
from src.ig_trader.strategy_lab.engine import CandleBacktestEngine, FrictionModel
from src.ig_trader.strategy_lab.models import AssetClass, Timeframe


def _dataset(
    *,
    start: datetime = datetime(2026, 1, 5, tzinfo=UTC),
    count: int = 80,
    timeframe: Timeframe = Timeframe.M1,
    missing: int | None = None,
):
    interval = {
        Timeframe.M1: timedelta(minutes=1),
        Timeframe.M5: timedelta(minutes=5),
        Timeframe.H1: timedelta(hours=1),
    }[timeframe]
    candles = []
    for number in range(count):
        if number == missing:
            continue
        close = Decimal("1.1000") + Decimal(number) / Decimal("100000")
        candles.append(
            LabCandle(
                instrument="EURUSD",
                timestamp_utc=start + interval * number,
                timeframe=timeframe,
                open=close - Decimal("0.00001"),
                high=close + Decimal("0.00002"),
                low=close - Decimal("0.00002"),
                close=close,
                spread=None,
                volume=Decimal("1"),
                source="SYNTHETIC_TEST_ONLY",
                source_quality=SourceQuality.SYNTHETIC_TEST_ONLY,
                gap_classification=GapClassification.NONE,
                synthetic=True,
            )
        )
    return build_dataset(candles, source_documents=("sl03-test",))


def test_gap_audit_recovers_only_weekend_and_keeps_weekday_missing_fail_closed() -> None:
    source = _dataset(
        start=datetime(2026, 1, 2, 21, tzinfo=UTC),
        count=52,
        timeframe=Timeframe.H1,
    )
    weekend = build_dataset((source.candles[0], source.candles[-1]), source_documents=("weekend",))
    audited_weekend = audit_dataset(weekend, AssetClass.FX)
    assert audited_weekend.gaps[0]["classification"] == EXPECTED_WEEKEND
    assert not audited_weekend.dataset.has_quality_failure

    weekday = _dataset(missing=20)
    audited_weekday = audit_dataset(weekday, AssetClass.FX)
    assert audited_weekday.gaps[0]["classification"] == UNEXPLAINED
    assert audited_weekday.dataset.has_quality_failure


def test_complete_resample_omits_incomplete_bucket_and_preserves_parent_provenance() -> None:
    minute = _dataset(count=12, missing=6)
    resampled = resample_complete(minute, Timeframe.M5)
    assert len(resampled.candles) == 1
    assert resampled.candles[0].timestamp_utc == minute.candles[0].timestamp_utc
    assert resampled.source_fingerprint != minute.source_fingerprint


def test_multisource_prefers_structured_deep_cache_and_records_parent_provenance(
    tmp_path: Path,
) -> None:
    rows = [
        {
            "timestamp_utc": (
                datetime(2026, 1, 5, tzinfo=UTC) + timedelta(minutes=index)
            ).isoformat(),
            "open": "1.1000",
            "high": "1.1002",
            "low": "1.0998",
            "close": "1.1001",
            "volume": "1",
        }
        for index in range(5)
    ]
    (tmp_path / "eurusd_1m_dukascopy.json").write_text(
        json.dumps(
            {
                "acquired_at_utc": datetime(2026, 1, 6, tzinfo=UTC).isoformat(),
                "provider_symbol": "EUR/USD",
                "candles": rows,
            }
        ),
        encoding="utf-8",
    )
    acquired = MultiSourceHistory(dukascopy_cache=tmp_path, yahoo_cache=tmp_path).load(
        "EURUSD", Timeframe.M5, AssetClass.FX
    )
    assert acquired.provenance.provider == "DUKASCOPY_STRUCTURED_HISTORY"
    assert acquired.provenance.parent_dataset_fingerprint is not None
    assert len(acquired.dataset.candles) == 1


def test_sl03_challengers_are_versioned_and_s0_is_exactly_frozen() -> None:
    s0 = sl03_challenger_variants("S0")
    challengers = sl03_challenger_variants("S1")
    assert len(s0) == 1
    assert s0[0].definition.baseline_only
    assert len(challengers) == 2
    assert all(item.definition.version.startswith("1.1.0-sl03-s1-") for item in challengers)
    assert all(item.definition.parent_version == "1.0.0" for item in challengers)


def test_signal_funnel_rejects_subminimum_stops_without_resizing() -> None:
    dataset = _dataset(timeframe=Timeframe.M5)
    strategy = sl03_challenger_variants("S1")[0]
    friction = FrictionModel(
        tick_size=Decimal("0.0001"),
        typical_spread=Decimal("0.0001"),
        slippage=Decimal("0.0001"),
        commission_price_equivalent=Decimal("0"),
        minimum_stop_distance=Decimal("1"),
        minimum_size=Decimal("1"),
        allowed_utc_hours=frozenset(range(24)),
    )
    simulation = _simulate(dataset, strategy, friction, CandleBacktestEngine())
    assert simulation.funnel.raw_strategy_signals > 0
    assert simulation.funnel.signals_rejected_by_cost_or_minimum_stop > 0
    assert simulation.funnel.entries_taken == 0


def test_bootstrap_is_deterministic_for_insufficient_trade_evidence() -> None:
    dataset = _dataset(timeframe=Timeframe.M5)
    strategy = sl03_challenger_variants("S1")[0]
    friction = FrictionModel(
        tick_size=Decimal("0.0001"),
        typical_spread=Decimal("0.0001"),
        slippage=Decimal("0.0001"),
        commission_price_equivalent=Decimal("0"),
        minimum_stop_distance=Decimal("1"),
        minimum_size=Decimal("1"),
        allowed_utc_hours=frozenset(range(24)),
    )
    result = _simulate(dataset, strategy, friction, CandleBacktestEngine()).result
    assert _bootstrap(result, dataset.dataset_fingerprint) == _bootstrap(
        result, dataset.dataset_fingerprint
    )


def test_full_ignored_artifact_set_is_off_only(tmp_path: Path) -> None:
    paths = write_sl03_artifacts(tmp_path, {name: {} for name in REQUIRED_ARTIFACTS})
    assert set(paths) == REQUIRED_ARTIFACTS
    assert all(
        '"execution_authority": "OFF"' in path.read_text(encoding="utf-8")
        for path in paths.values()
    )


def test_dashboard_prefers_sl03_leaderboard_and_exposes_signal_fields(tmp_path: Path) -> None:
    (tmp_path / "sl03_leaderboard.json").write_text(
        json.dumps(
            {
                "entries": [
                    {
                        "instrument": "EURUSD",
                        "asset_class": "FX",
                        "strategy": "S1",
                        "strategy_version": "1.1.0-sl03-s1-1",
                        "timeframe": "1H",
                        "trade_count": 12,
                        "oos_trade_count": 4,
                        "raw_strategy_signals": 30,
                        "classification": "LOW_SAMPLE_CONFIDENCE",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    snapshot = load_strategy_lab_snapshot(tmp_path)
    assert snapshot.available
    assert snapshot.entries[0]["raw_signals"] == 30
    assert snapshot.entries[0]["oos_trades"] == 4
