"""Strict offline data contracts and adapters for Strategy Lab research."""

from __future__ import annotations

import csv
import hashlib
import json
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from src.ig_trader.strategy_lab.models import Timeframe


class DataContractError(ValueError):
    """Raised when local research data cannot be safely evaluated."""


class SourceQuality(StrEnum):
    IG_VERIFIED = "IG_VERIFIED"
    EXTERNAL_UNVERIFIED = "EXTERNAL_UNVERIFIED"
    LOCAL_RESEARCH = "LOCAL_RESEARCH"
    SYNTHETIC_TEST_ONLY = "SYNTHETIC_TEST_ONLY"


class GapClassification(StrEnum):
    NONE = "NONE"
    WEEKEND_OR_SESSION = "WEEKEND_OR_SESSION"
    MISSING_DATA = "MISSING_DATA"
    SOURCE_REPORTED = "SOURCE_REPORTED"


TIMEFRAME_INTERVALS: dict[Timeframe, timedelta] = {
    Timeframe.H4: timedelta(hours=4),
    Timeframe.H1: timedelta(hours=1),
    Timeframe.M15: timedelta(minutes=15),
    Timeframe.M5: timedelta(minutes=5),
    Timeframe.M1: timedelta(minutes=1),
}


@dataclass(frozen=True)
class LabCandle:
    """Canonical OHLCV/spread row, normalized to UTC and explicitly sourced."""

    instrument: str
    timestamp_utc: datetime
    timeframe: Timeframe
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    spread: Decimal | None
    volume: Decimal | None
    source: str
    source_quality: SourceQuality
    gap_classification: GapClassification
    synthetic: bool

    def __post_init__(self) -> None:
        if self.timestamp_utc.tzinfo is None or self.timestamp_utc.utcoffset() != timedelta(0):
            raise DataContractError("timestamps must be timezone-aware UTC")
        if not self.instrument or not self.instrument.isupper():
            raise DataContractError("candle instrument must be a canonical uppercase symbol")
        if not self.source.strip():
            raise DataContractError("candle source is required")
        if self.low > min(self.open, self.close) or self.high < max(self.open, self.close):
            raise DataContractError("invalid OHLC geometry")
        if self.high < self.low:
            raise DataContractError("high cannot be below low")
        if self.spread is not None and self.spread < 0:
            raise DataContractError("spread cannot be negative")
        if self.volume is not None and self.volume < 0:
            raise DataContractError("volume cannot be negative")


@dataclass(frozen=True)
class DatasetGap:
    after_utc: datetime
    before_utc: datetime
    missing_intervals: int
    classification: GapClassification


@dataclass(frozen=True)
class CanonicalDataset:
    """A validated single-instrument, single-timeframe research dataset."""

    instrument: str
    timeframe: Timeframe
    candles: tuple[LabCandle, ...]
    source_fingerprint: str
    dataset_fingerprint: str
    gaps: tuple[DatasetGap, ...]

    def __post_init__(self) -> None:
        if not self.candles:
            raise DataContractError("dataset must contain at least one candle")
        timestamps = tuple(candle.timestamp_utc for candle in self.candles)
        if timestamps != tuple(sorted(timestamps)):
            raise DataContractError("timestamps must be chronological")
        if len(timestamps) != len(set(timestamps)):
            raise DataContractError("duplicate timestamps are not allowed")
        if any(
            candle.instrument != self.instrument or candle.timeframe is not self.timeframe
            for candle in self.candles
        ):
            raise DataContractError("dataset identity does not match its candles")
        if len(self.source_fingerprint) != 64 or len(self.dataset_fingerprint) != 64:
            raise DataContractError("fingerprints must be SHA-256 hex digests")

    @property
    def has_quality_failure(self) -> bool:
        return any(gap.classification is GapClassification.MISSING_DATA for gap in self.gaps)


def build_dataset(
    candles: Iterable[LabCandle], *, source_documents: Iterable[bytes | str] = ()
) -> CanonicalDataset:
    """Validate rows, record gaps, and create stable source/dataset fingerprints."""

    values = tuple(candles)
    if not values:
        raise DataContractError("dataset must not be empty")
    instrument = values[0].instrument
    timeframe = values[0].timeframe
    source_payload = b"\n".join(
        item if isinstance(item, bytes) else item.encode("utf-8") for item in source_documents
    )
    if not source_payload:
        source_payload = _canonical_json([_candle_document(item) for item in values]).encode(
            "utf-8"
        )
    source_fingerprint = hashlib.sha256(source_payload).hexdigest()
    sorted_values = tuple(sorted(values, key=lambda item: item.timestamp_utc))
    if len({item.timestamp_utc for item in sorted_values}) != len(sorted_values):
        raise DataContractError("duplicate timestamps are not allowed")
    gaps = detect_gaps(sorted_values)
    dataset_document = {
        "schema": "strategy-lab-canonical-ohlcv/1.0",
        "source_fingerprint": source_fingerprint,
        "candles": [_candle_document(item) for item in sorted_values],
        "gaps": [
            {
                "after_utc": item.after_utc.isoformat(),
                "before_utc": item.before_utc.isoformat(),
                "missing_intervals": item.missing_intervals,
                "classification": item.classification.value,
            }
            for item in gaps
        ],
    }
    return CanonicalDataset(
        instrument=instrument,
        timeframe=timeframe,
        candles=sorted_values,
        source_fingerprint=source_fingerprint,
        dataset_fingerprint=hashlib.sha256(
            _canonical_json(dataset_document).encode("utf-8")
        ).hexdigest(),
        gaps=gaps,
    )


def detect_gaps(candles: tuple[LabCandle, ...]) -> tuple[DatasetGap, ...]:
    """Record every interval jump; weekends require an explicit classification."""

    if len(candles) < 2:
        return ()
    interval = TIMEFRAME_INTERVALS[candles[0].timeframe]
    gaps: list[DatasetGap] = []
    for before, after in zip(candles, candles[1:], strict=False):
        difference = after.timestamp_utc - before.timestamp_utc
        if difference <= interval:
            continue
        missing = max(1, int(difference / interval) - 1)
        classification = (
            GapClassification.WEEKEND_OR_SESSION
            if before.timestamp_utc.weekday() == 4 and after.timestamp_utc.weekday() == 0
            else GapClassification.MISSING_DATA
        )
        if after.gap_classification is not GapClassification.NONE:
            classification = after.gap_classification
        gaps.append(DatasetGap(before.timestamp_utc, after.timestamp_utc, missing, classification))
    return tuple(gaps)


class LocalDatasetSource:
    """CSV-only local adapter. It makes no network or broker calls."""

    REQUIRED_COLUMNS = frozenset(
        {
            "instrument",
            "timestamp_utc",
            "timeframe",
            "open",
            "high",
            "low",
            "close",
            "spread",
            "volume",
            "source",
            "source_quality",
            "gap_classification",
            "synthetic",
        }
    )

    def load(self, path: Path) -> CanonicalDataset:
        try:
            content = path.read_bytes()
            rows = tuple(csv.DictReader(content.decode("utf-8").splitlines()))
        except (OSError, UnicodeDecodeError, csv.Error) as error:
            raise DataContractError("unable to read local Strategy Lab CSV") from error
        if not rows or not self.REQUIRED_COLUMNS.issubset(rows[0]):
            raise DataContractError("local dataset does not implement the canonical CSV contract")
        return build_dataset((self._parse_row(row) for row in rows), source_documents=(content,))

    @staticmethod
    def _parse_row(row: Mapping[str, str]) -> LabCandle:
        try:
            timestamp = datetime.fromisoformat(row["timestamp_utc"].replace("Z", "+00:00"))
            timestamp = timestamp.astimezone(UTC)
            return LabCandle(
                instrument=row["instrument"],
                timestamp_utc=timestamp,
                timeframe=Timeframe(row["timeframe"]),
                open=Decimal(row["open"]),
                high=Decimal(row["high"]),
                low=Decimal(row["low"]),
                close=Decimal(row["close"]),
                spread=_optional_decimal(row["spread"]),
                volume=_optional_decimal(row["volume"]),
                source=row["source"],
                source_quality=SourceQuality(row["source_quality"]),
                gap_classification=GapClassification(row["gap_classification"]),
                synthetic=_parse_bool(row["synthetic"]),
            )
        except (KeyError, InvalidOperation, ValueError) as error:
            raise DataContractError("local dataset row is invalid") from error


class IGHistoricalSource(Protocol):
    """Read-only future adapter boundary; no HTTP implementation exists in SL-01."""

    def load_verified_history(
        self, instrument: str, timeframe: Timeframe, start_utc: datetime, end_utc: datetime
    ) -> CanonicalDataset: ...


class ExternalResearchSource(Protocol):
    """Optional adapter boundary whose rows must remain EXTERNAL_UNVERIFIED."""

    def load_external_history(
        self, instrument: str, timeframe: Timeframe, start_utc: datetime, end_utc: datetime
    ) -> CanonicalDataset: ...


def _parse_bool(value: str) -> bool:
    if value.casefold() == "true":
        return True
    if value.casefold() == "false":
        return False
    raise ValueError("synthetic must be true or false")


def _optional_decimal(value: str) -> Decimal | None:
    return None if not value.strip() else Decimal(value)


def _candle_document(candle: LabCandle) -> dict[str, object]:
    document = asdict(candle)
    return {
        key: value.isoformat() if isinstance(value, datetime) else str(value)
        for key, value in document.items()
    }


def _canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, default=str)
