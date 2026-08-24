"""Read sanitized DQ-03 evidence without contacting IG or any broker."""

from __future__ import annotations

import json
from collections.abc import Iterable
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from statistics import median
from typing import Any

from src.ig_trader.sl02.contracts import (
    AlignmentResult,
    AlignmentStatus,
    BrokerEvidence,
    BrokerValidationPoint,
)
from src.ig_trader.strategy_lab.data import CanonicalDataset


def load_dq03_evidence(directory: Path) -> dict[str, BrokerEvidence]:
    """Load local DQ-03 facts only; absent or malformed evidence remains absent."""

    registry = _read_object(directory / "instrument_registry.json")
    history = _read_object(directory / "history_validation.json")
    history_rows = _history_rows(history.get("samples") if history else ())
    instruments = registry.get("instruments") if registry else ()
    if not isinstance(instruments, list):
        return {}
    result: dict[str, BrokerEvidence] = {}
    for value in instruments:
        if not isinstance(value, dict):
            continue
        symbol = value.get("canonical_symbol")
        if not isinstance(symbol, str) or not symbol.isupper():
            continue
        metadata = value.get("metadata") if isinstance(value.get("metadata"), dict) else {}
        embedded = value.get("broker_validation")
        rows = _points(embedded.get("rows") if isinstance(embedded, dict) else ())
        result[symbol] = BrokerEvidence(
            symbol=symbol,
            epic=_text(value.get("selected_epic")),
            metadata_fingerprint=_text(value.get("metadata_fingerprint")),
            broker_validation_fingerprint=_text(value.get("broker_validation_fingerprint")),
            data_status=_text(value.get("data_status")),
            cost_model_status=_text(value.get("cost_model_status")),
            pip_or_tick_size=_decimal(metadata.get("one_pip_means")),
            minimum_deal_size=_decimal(metadata.get("minimum_deal_size")),
            minimum_stop_distance=_decimal(metadata.get("minimum_stop_distance")),
            observed_spread=_decimal(metadata.get("spread")),
            currency=_text(metadata.get("currency")),
            points=rows or history_rows.get(symbol, ()),
        )
    return result


def compare_with_broker_sample(
    dataset: CanonicalDataset, evidence: BrokerEvidence | None
) -> AlignmentResult:
    """Measure only actual timestamp overlaps; no missing IG candle is constructed."""

    if evidence is None or not evidence.points:
        return AlignmentResult(
            AlignmentStatus.NO_OVERLAP_AVAILABLE,
            0,
            None,
            None,
            None,
            None,
            "No normalized DQ-03 broker-validation rows were supplied in this clean worktree.",
        )
    external = {item.timestamp_utc: item for item in dataset.candles}
    pairs = tuple(
        (item, external[item.timestamp_utc]) for item in evidence.points if item.timestamp_utc in external
    )
    if not pairs:
        return AlignmentResult(
            AlignmentStatus.NO_OVERLAP_AVAILABLE,
            0,
            Decimal("0"),
            None,
            None,
            Decimal("1"),
            "DQ-03 rows and external candles have no identical UTC candle timestamps.",
        )
    differences = tuple(abs(point.close_mid - candle.close) for point, candle in pairs)
    average_difference = sum(differences, Decimal("0")) / len(differences)
    spread_pairs = tuple(
        abs(point.spread - candle.spread)
        for point, candle in pairs
        if point.spread is not None and candle.spread is not None
    )
    average_spread_difference = (
        sum(spread_pairs, Decimal("0")) / len(spread_pairs) if spread_pairs else None
    )
    alignment_rate = Decimal(len(pairs)) / Decimal(len(evidence.points))
    price_scale = Decimal(str(median(abs(point.close_mid) for point, _ in pairs)))
    relative_difference = average_difference / price_scale if price_scale else Decimal("Infinity")
    exact_tolerance = (evidence.pip_or_tick_size or Decimal("0")) * Decimal("5")
    status = (
        AlignmentStatus.ALIGNED_WITH_IG
        if exact_tolerance > 0 and average_difference <= exact_tolerance
        else (
            AlignmentStatus.ACCEPTABLE_SOURCE_DIFFERENCE
            if relative_difference <= Decimal("0.005")
            else AlignmentStatus.MATERIAL_SOURCE_DIVERGENCE
        )
    )
    return AlignmentResult(
        status,
        len(pairs),
        alignment_rate,
        average_difference,
        average_spread_difference,
        Decimal("1") - alignment_rate,
        "Compared only normalized DQ-03 rows at exactly matching UTC timestamps.",
    )


def _history_rows(value: object) -> dict[str, tuple[BrokerValidationPoint, ...]]:
    if not isinstance(value, list):
        return {}
    result: dict[str, tuple[BrokerValidationPoint, ...]] = {}
    for sample in value:
        if not isinstance(sample, dict) or sample.get("status") != "BROKER_VALIDATED":
            continue
        symbol = sample.get("symbol")
        if isinstance(symbol, str):
            result[symbol] = _points(sample.get("rows"))
    return result


def _points(value: object) -> tuple[BrokerValidationPoint, ...]:
    if not isinstance(value, Iterable) or isinstance(value, str | bytes | dict):
        return ()
    points: list[BrokerValidationPoint] = []
    for row in value:
        if not isinstance(row, dict):
            continue
        timestamp = _timestamp(row.get("timestamp_utc"))
        close = _decimal(row.get("close_mid"))
        if timestamp is not None and close is not None:
            points.append(BrokerValidationPoint(timestamp, close, _decimal(row.get("close_spread"))))
    return tuple(points)


def _read_object(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _timestamp(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)
    except ValueError:
        return None


def _decimal(value: object) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _text(value: object) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None
