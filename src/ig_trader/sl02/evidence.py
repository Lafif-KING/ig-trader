"""Read sanitized DQ-03 evidence without contacting IG or any broker."""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass
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


_REQUIRED_DOCUMENTS = ("instrument_registry.json", "history_validation.json")


@dataclass(frozen=True)
class EvidencePreflight:
    """Validated, sanitized DQ-03 handoff suitable for research only."""

    directory: Path
    expected_symbols: tuple[str, ...]
    loaded_documents: tuple[str, ...]
    verified_count: int
    broker_validated_count: int
    fingerprint_mismatches: tuple[str, ...]
    missing_cost_inputs: dict[str, tuple[str, ...]]
    errors: tuple[str, ...]
    evidence: dict[str, BrokerEvidence]

    @property
    def broker_ready(self) -> bool:
        return not self.errors and set(self.evidence) == set(self.expected_symbols)

    def document(self) -> dict[str, object]:
        return {
            "dq03_directory": str(self.directory),
            "loaded_documents": list(self.loaded_documents),
            "required_verified_count": len(self.expected_symbols),
            "loaded_verified_count": self.verified_count,
            "broker_validated_count": self.broker_validated_count,
            "fingerprint_mismatches": list(self.fingerprint_mismatches),
            "missing_cost_inputs": {
                symbol: list(reasons) for symbol, reasons in self.missing_cost_inputs.items()
            },
            "errors": list(self.errors),
            "status": "READY_FOR_RESEARCH_COST_MODEL" if self.broker_ready else "SL02_BROKER_EVIDENCE_REQUIRED",
            "execution_authority": "OFF",
        }


def write_preflight_report(path: Path, report: dict[str, object]) -> Path:
    """Persist the sanitized preflight report without copying DQ-03 evidence."""

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    return path


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
        result[symbol] = _broker_evidence(value, history_rows)
    return result


def preflight_dq03_evidence(directory: Path, *, expected_symbols: Iterable[str]) -> EvidencePreflight:
    """Fail closed unless the authoritative DQ-03 handoff proves every required fact."""

    expected = tuple(expected_symbols)
    documents = {name: _read_object(directory / name) for name in _REQUIRED_DOCUMENTS}
    loaded = tuple(name for name, value in documents.items() if value is not None)
    errors = [f"DQ03_DOCUMENT_UNREADABLE:{name}" for name, value in documents.items() if value is None]
    registry = documents.get("instrument_registry.json") or {}
    history = documents.get("history_validation.json") or {}
    registry_rows = registry.get("instruments")
    if not isinstance(registry_rows, list):
        registry_rows = []
        errors.append("DQ03_INSTRUMENT_REGISTRY_INVALID")
    history_rows = _history_rows(history.get("samples"))
    history_samples = history.get("samples")
    if not isinstance(history_samples, list):
        errors.append("DQ03_HISTORY_VALIDATION_INVALID")
        history_samples = []
    history_by_symbol = {
        sample.get("symbol"): sample
        for sample in history_samples
        if isinstance(sample, dict) and isinstance(sample.get("symbol"), str)
    }

    by_symbol: dict[str, dict[str, Any]] = {}
    for row in registry_rows:
        if isinstance(row, dict) and isinstance(row.get("canonical_symbol"), str):
            symbol = row["canonical_symbol"]
            if symbol in by_symbol:
                errors.append(f"DQ03_DUPLICATE_INSTRUMENT:{symbol}")
            by_symbol[symbol] = row

    verified = [row for row in registry_rows if isinstance(row, dict) and row.get("classification") == "VERIFIED"]
    evidence: dict[str, BrokerEvidence] = {}
    fingerprints: list[str] = []
    missing_cost_inputs: dict[str, tuple[str, ...]] = {}
    validated = 0
    for symbol in expected:
        row = by_symbol.get(symbol)
        if row is None:
            errors.append(f"DQ03_REQUIRED_INSTRUMENT_MISSING:{symbol}")
            continue
        row_errors, row_cost_inputs, row_mismatches = _validate_required_row(
            symbol, row, history_rows, history_by_symbol.get(symbol)
        )
        errors.extend(row_errors)
        fingerprints.extend(row_mismatches)
        if row_cost_inputs:
            missing_cost_inputs[symbol] = tuple(row_cost_inputs)
        if not row_errors:
            evidence[symbol] = _broker_evidence(row, history_rows)
            validated += 1
    extra_verified = sorted(
        str(row.get("canonical_symbol"))
        for row in verified
        if row.get("canonical_symbol") not in expected
    )
    if extra_verified:
        errors.append(f"DQ03_UNEXPECTED_VERIFIED_INSTRUMENTS:{','.join(extra_verified)}")
    if len(verified) != len(expected):
        errors.append(f"DQ03_VERIFIED_COUNT_MISMATCH:{len(verified)}")

    return EvidencePreflight(
        directory=directory,
        expected_symbols=expected,
        loaded_documents=loaded,
        verified_count=len(verified),
        broker_validated_count=validated,
        fingerprint_mismatches=tuple(sorted(fingerprints)),
        missing_cost_inputs=missing_cost_inputs,
        errors=tuple(sorted(set(errors))),
        evidence=evidence,
    )


def _validate_required_row(
    symbol: str,
    row: dict[str, Any],
    history_rows: dict[str, tuple[BrokerValidationPoint, ...]],
    history_sample: dict[str, Any] | None,
) -> tuple[list[str], list[str], list[str]]:
    errors: list[str] = []
    cost_inputs: list[str] = []
    mismatches: list[str] = []
    metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
    validation = row.get("broker_validation") if isinstance(row.get("broker_validation"), dict) else {}
    if row.get("classification") != "VERIFIED":
        errors.append(f"DQ03_INSTRUMENT_NOT_VERIFIED:{symbol}")
    if row.get("data_status") != "BROKER_VALIDATED":
        errors.append(f"DQ03_BROKER_VALIDATION_REQUIRED:{symbol}")
    if not _valid_fingerprint(row.get("metadata_fingerprint")):
        errors.append(f"DQ03_METADATA_FINGERPRINT_INVALID:{symbol}")
    if not _valid_fingerprint(row.get("broker_validation_fingerprint")):
        errors.append(f"DQ03_BROKER_VALIDATION_FINGERPRINT_INVALID:{symbol}")
    epic = _text(row.get("selected_epic"))
    if epic is None or epic != _text(metadata.get("epic")):
        errors.append(f"DQ03_EPIC_MAPPING_INVALID:{symbol}")
    if validation.get("status") != "BROKER_VALIDATED":
        errors.append(f"DQ03_EMBEDDED_BROKER_VALIDATION_INVALID:{symbol}")
    if history_sample is None or history_sample.get("status") != "BROKER_VALIDATED":
        errors.append(f"DQ03_HISTORY_BROKER_VALIDATION_REQUIRED:{symbol}")
    elif epic != _text(history_sample.get("epic")):
        errors.append(f"DQ03_HISTORY_EPIC_MAPPING_INVALID:{symbol}")
    elif _text(validation.get("source_fingerprint")) != _text(history_sample.get("source_fingerprint")):
        mismatch = f"DQ03_BROKER_HISTORY_FINGERPRINT_MISMATCH:{symbol}"
        errors.append(mismatch)
        mismatches.append(mismatch)

    pip_or_tick = _decimal(metadata.get("one_pip_means"))
    minimum_size = _decimal(metadata.get("minimum_deal_size"))
    minimum_stop = _decimal(metadata.get("minimum_stop_distance"))
    if pip_or_tick is None or pip_or_tick <= 0:
        cost_inputs.append("PIP_OR_TICK_SIZE_MISSING")
    if minimum_size is None or minimum_size <= 0:
        cost_inputs.append("MINIMUM_DEAL_SIZE_MISSING")
    if minimum_stop is None or minimum_stop < 0:
        cost_inputs.append("MINIMUM_STOP_DISTANCE_MISSING")
    currency = _text(metadata.get("currency"))
    if currency is None or len(currency) != 3:
        cost_inputs.append("CURRENCY_MISSING")
    points = history_rows.get(symbol, ())
    if not points or not any(point.spread is not None and point.spread > 0 for point in points):
        cost_inputs.append("OBSERVED_SPREAD_ROWS_MISSING")
    if _nonnegative_int(validation.get("observed_spread_rows")) <= 0:
        errors.append(f"DQ03_EMBEDDED_OBSERVED_SPREAD_REQUIRED:{symbol}")
    if history_sample is not None and _nonnegative_int(history_sample.get("observed_spread_rows")) <= 0:
        errors.append(f"DQ03_HISTORY_OBSERVED_SPREAD_REQUIRED:{symbol}")
    return errors, cost_inputs, mismatches


def _broker_evidence(
    value: dict[str, Any], history_rows: dict[str, tuple[BrokerValidationPoint, ...]]
) -> BrokerEvidence:
    symbol = str(value["canonical_symbol"])
    metadata = value.get("metadata") if isinstance(value.get("metadata"), dict) else {}
    embedded = value.get("broker_validation")
    rows = _points(embedded.get("rows") if isinstance(embedded, dict) else ())
    points = rows or history_rows.get(symbol, ())
    return BrokerEvidence(
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
        points=points,
        observed_spreads=tuple(point.spread for point in points if point.spread is not None),
        asset_class=_text(value.get("asset_class")),
        expiry=_text(metadata.get("expiry")),
    )


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


def _valid_fingerprint(value: object) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(
        char in "0123456789abcdef" for char in value.lower()
    )


def _nonnegative_int(value: object) -> int:
    try:
        return int(value) if value is not None else 0
    except (TypeError, ValueError):
        return 0


def _text(value: object) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None
