"""Focused offline regression tests for the SL-02 research-only batch."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from src.ig_trader.sl02.contracts import (
    AcquiredDataset,
    BrokerEvidence,
    CostEvidence,
    DatasetDepthStatus,
)
from src.ig_trader.sl02.costs import (
    broker_minimum_stop_price_distance,
    friction_model,
    generate_research_cost_model,
    load_cost_evidence,
)
from src.ig_trader.sl02.evidence import (
    compare_with_broker_sample,
    load_dq03_evidence,
    preflight_dq03_evidence,
)
from src.ig_trader.sl02.runner import SL02BrokerEvidenceRequired, SL02Runner
from src.ig_trader.strategy_lab.data import (
    GapClassification,
    LabCandle,
    SourceQuality,
    build_dataset,
)
from src.ig_trader.strategy_lab.models import Timeframe
from src.ig_trader.strategy_lab.strategies import bounded_parameter_variants, strategy_registry


def _dataset(symbol: str, timeframe: Timeframe):
    interval = {
        Timeframe.H4: timedelta(hours=4),
        Timeframe.H1: timedelta(hours=1),
        Timeframe.M15: timedelta(minutes=15),
        Timeframe.M5: timedelta(minutes=5),
    }[timeframe]
    candles = []
    for number in range(100):
        close = Decimal("1.10") + Decimal(number) / Decimal("10000")
        candles.append(
            LabCandle(
                instrument=symbol,
                timestamp_utc=datetime(2026, 1, 5, tzinfo=UTC) + interval * number,
                timeframe=timeframe,
                open=close - Decimal("0.0001"),
                high=close + Decimal("0.0002"),
                low=close - Decimal("0.0002"),
                close=close,
                spread=None,
                volume=Decimal("1"),
                source="TEST_EXTERNAL",
                source_quality=SourceQuality.EXTERNAL_UNVERIFIED,
                gap_classification=GapClassification.NONE,
                synthetic=False,
            )
        )
    return build_dataset(candles)


class _FakeHistorySource:
    def load(self, symbol: str, timeframe: Timeframe) -> AcquiredDataset:
        dataset = _dataset(symbol, timeframe)
        return AcquiredDataset(
            dataset,
            "TEST_EXTERNAL_SOURCE",
            symbol,
            datetime(2026, 8, 24, tzinfo=UTC),
            "https://example.test/history",
            "a" * 64,
            True,
            DatasetDepthStatus.LOW_DATA_DEPTH,
        )


def test_bounded_challenger_grids_are_small_and_s0_is_immutable() -> None:
    registry = strategy_registry()
    assert bounded_parameter_variants(registry["S0"]) == (registry["S0"],)
    variants = bounded_parameter_variants(registry["S1"])
    assert len(variants) == 3
    assert all(item.definition.parent_version == "1.0.0" for item in variants)
    assert len({item.definition.configuration_fingerprint for item in variants}) == 3


def test_dq03_evidence_alignment_and_cost_model_require_matching_fingerprint(
    tmp_path: Path,
) -> None:
    timestamp = datetime(2026, 1, 5, tzinfo=UTC)
    fingerprint = "b" * 64
    (tmp_path / "instrument_registry.json").write_text(
        json.dumps(
            {
                "instruments": [
                    {
                        "canonical_symbol": "EURUSD",
                        "asset_class": "FX",
                        "classification": "VERIFIED",
                        "selected_epic": "CS.D.TEST.IP",
                        "metadata_fingerprint": fingerprint,
                        "broker_validation_fingerprint": "c" * 64,
                        "data_status": "BROKER_VALIDATED",
                        "cost_model_status": "BROKER_VALIDATED",
                        "metadata": {
                            "epic": "CS.D.TEST.IP",
                            "one_pip_means": "0.0001",
                            "minimum_deal_size": "1",
                            "minimum_stop_distance": "5",
                            "minimum_stop_distance_unit": "POINTS",
                            "decimal_places": 5,
                            "scaling_factor": 10000,
                            "spread": "0.0002",
                            "currency": "USD",
                            "expiry": "-",
                        },
                        "broker_validation": {
                            "status": "BROKER_VALIDATED",
                            "source_fingerprint": "d" * 64,
                            "observed_spread_rows": 1,
                            "rows": [
                                {
                                    "timestamp_utc": timestamp.isoformat(),
                                    "close_mid": "1.1000",
                                    "close_spread": "0.0002",
                                }
                            ],
                        },
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "history_validation.json").write_text(
        json.dumps(
            {
                "samples": [
                    {
                        "symbol": "EURUSD",
                        "epic": "CS.D.TEST.IP",
                        "status": "BROKER_VALIDATED",
                        "source_fingerprint": "d" * 64,
                        "observed_spread_rows": 1,
                        "rows": [
                            {
                                "timestamp_utc": timestamp.isoformat(),
                                "close_mid": "1.1000",
                                "close_spread": "0.0002",
                            }
                        ],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    preflight = preflight_dq03_evidence(tmp_path, expected_symbols=("EURUSD",))
    assert preflight.broker_ready
    generate_research_cost_model(
        preflight.evidence,
        expected_symbols=("EURUSD",),
        output_path=tmp_path / "cost.json",
    )
    evidence = load_dq03_evidence(tmp_path)["EURUSD"]
    assert evidence.minimum_stop_distance_value == Decimal("5")
    assert evidence.minimum_stop_distance_unit == "POINTS"
    assert evidence.decimal_places == 5
    assert evidence.scaling_factor == 10000
    assert broker_minimum_stop_price_distance(evidence) == Decimal("0.0005")
    matching = _dataset("EURUSD", Timeframe.H1)
    matching_candle = matching.candles[0]
    matching = build_dataset(
        (
            LabCandle(
                instrument="EURUSD",
                timestamp_utc=timestamp,
                timeframe=Timeframe.H1,
                open=Decimal("1.0999"),
                high=Decimal("1.1002"),
                low=Decimal("1.0998"),
                close=Decimal("1.1000"),
                spread=None,
                volume=matching_candle.volume,
                source="TEST_EXTERNAL",
                source_quality=SourceQuality.EXTERNAL_UNVERIFIED,
                gap_classification=GapClassification.NONE,
                synthetic=False,
            ),
        )
    )
    alignment = compare_with_broker_sample(matching, evidence)
    assert alignment.status.value == "ALIGNED_WITH_IG"
    assert friction_model(
        evidence,
        load_cost_evidence(tmp_path / "cost.json")["EURUSD"],
        stress_multiplier=Decimal("1"),
    ).complete


def _broker_distance_evidence(
    *,
    value: str,
    unit: str | None = "POINTS",
    pip: str | None = "0.0001",
    decimal_places: int | None = 5,
    scaling_factor: int | None = 10000,
) -> BrokerEvidence:
    return BrokerEvidence(
        symbol="TEST",
        epic="CS.D.TEST.IP",
        metadata_fingerprint="a" * 64,
        broker_validation_fingerprint="b" * 64,
        data_status="BROKER_VALIDATED",
        cost_model_status="BROKER_VALIDATED",
        pip_or_tick_size=Decimal(pip) if pip is not None else None,
        minimum_deal_size=Decimal("1"),
        minimum_stop_distance=Decimal(value),
        observed_spread=Decimal("0.0001"),
        currency="USD",
        minimum_stop_distance_value=Decimal(value),
        minimum_stop_distance_unit=unit,
        decimal_places=decimal_places,
        scaling_factor=scaling_factor,
    )


@pytest.mark.parametrize(
    ("symbol", "value", "pip", "decimal_places", "scaling_factor", "expected"),
    [
        ("EURUSD", "2", "0.0001", 5, 10000, "0.0002"),
        ("GBPUSD", "4", "0.0001", 5, 10000, "0.0004"),
        ("EURGBP", "2", "0.0001", 5, 10000, "0.0002"),
        ("USDJPY", "2", "0.01", 3, 100, "0.02"),
        ("EURJPY", "4", "0.01", 3, 100, "0.04"),
        ("XAUUSD", "1", "1", 2, 1, "1"),
        ("XAGUSD", "4", "1", 3, 100, "4"),
        ("US500", "1", "1", 2, 1, "1"),
        ("USTECH100", "4", "1", 1, 1, "4"),
    ],
)
def test_ig_points_rules_normalize_to_raw_market_price_distance(
    symbol: str,
    value: str,
    pip: str,
    decimal_places: int,
    scaling_factor: int,
    expected: str,
) -> None:
    """Sanitized DQ-03 representative contracts retain their native IG values."""

    evidence = _broker_distance_evidence(
        value=value,
        pip=pip,
        decimal_places=decimal_places,
        scaling_factor=scaling_factor,
    )
    evidence = BrokerEvidence(**{**evidence.__dict__, "symbol": symbol})

    assert broker_minimum_stop_price_distance(evidence) == Decimal(expected)


def test_percentage_rule_requires_explicit_price_and_static_model_fails_closed() -> None:
    evidence = _broker_distance_evidence(value="2", unit="PERCENTAGE")
    cost = CostEvidence(
        symbol="TEST",
        metadata_fingerprint="a" * 64,
        base_spread=Decimal("0.0001"),
        slippage=Decimal("0.0001"),
        commission_price_equivalent=Decimal("0"),
        allowed_utc_hours=frozenset(range(24)),
        evidence_basis="Synthetic unit-regression evidence only.",
    )

    assert broker_minimum_stop_price_distance(evidence) is None
    assert broker_minimum_stop_price_distance(
        evidence, reference_price=Decimal("1.20000")
    ) == Decimal("0.02400")
    assert friction_model(evidence, cost, stress_multiplier=Decimal("1")) is None


@pytest.mark.parametrize(
    "evidence",
    [
        _broker_distance_evidence(value="2", unit="TICKS"),
        _broker_distance_evidence(value="2", pip=None),
        _broker_distance_evidence(value="2", scaling_factor=None),
        _broker_distance_evidence(value="2", decimal_places=None),
    ],
)
def test_unprovable_broker_stop_conversion_fails_closed(evidence: BrokerEvidence) -> None:
    assert broker_minimum_stop_price_distance(evidence) is None


def test_sl02_runner_writes_full_artifact_set_and_blocks_missing_cost_evidence(
    tmp_path: Path,
) -> None:
    runner = SL02Runner(
        artifact_directory=tmp_path / "artifacts",
        cache_directory=tmp_path / "cache",
        dq03_directory=tmp_path / "dq03-missing",
        cost_evidence_path=tmp_path / "cost-missing.json",
        history_source=_FakeHistorySource(),
    )
    with pytest.raises(SL02BrokerEvidenceRequired) as error:
        runner.run()
    report = json.loads(error.value.report_path.read_text(encoding="utf-8"))
    assert report["status"] == "SL02_BROKER_EVIDENCE_REQUIRED"
    assert report["loaded_verified_count"] == 0
    assert not (tmp_path / "artifacts" / "sl02_results.json").exists()
