"""Strict offline reader for local ``dukascopy-go`` OHLCV exports.

This module deliberately has no HTTP client, credential handling, or broker
transport.  It accepts only the documented CSV shape supplied for SL-04 and
records enough raw-file provenance to make each local research dataset
auditable without relabelling it as IG market data.
"""

from __future__ import annotations

import csv
import hashlib
import io
from collections import defaultdict
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from src.ig_trader.sl02.contracts import DatasetDepthStatus
from src.ig_trader.sl02.history import ExternalHistoryUnavailable
from src.ig_trader.sl03.history import ProviderProvenance, ResearchDataset
from src.ig_trader.strategy_lab.data import (
    TIMEFRAME_INTERVALS,
    CanonicalDataset,
    DataContractError,
    GapClassification,
    LabCandle,
    SourceQuality,
    build_dataset,
)
from src.ig_trader.strategy_lab.models import AssetClass, Timeframe

LOCAL_PROVIDER = "DUKASCOPY_PUBLIC_FEED"
ACQUISITION_TOOL = "dukascopy-go 0.2.0"
ACQUISITION_ENGINE = "jetta"
ACQUISITION_MODE = "LOCAL_CSV_EXPORT"
SOURCE_QUALITY = "EXTERNAL_UNVERIFIED"
MIDPOINT_TOLERANCE = Decimal("0.0000005")
EXPECTED_SCHEMA = (
    "timestamp",
    "mid_open",
    "mid_high",
    "mid_low",
    "mid_close",
    "spread",
    "volume",
    "bid_open",
    "bid_high",
    "bid_low",
    "bid_close",
    "ask_open",
    "ask_high",
    "ask_low",
    "ask_close",
)
M1_SYMBOLS = frozenset(
    {"EURUSD", "GBPUSD", "EURGBP", "USDJPY", "EURJPY", "GBPJPY", "AUDUSD", "USDCHF"}
)
H1_SYMBOLS = frozenset(
    {
        "EURUSD",
        "GBPUSD",
        "EURGBP",
        "USDJPY",
        "EURJPY",
        "GBPJPY",
        "AUDUSD",
        "NZDUSD",
        "USDCAD",
        "USDCHF",
        "EURCHF",
        "EURAUD",
        "GBPAUD",
        "AUDJPY",
        "CADJPY",
        "CHFJPY",
    }
)


@dataclass(frozen=True)
class LocalFileRecord:
    """Validated facts about one supplied raw CSV file."""

    path: Path
    symbol: str
    timeframe: Timeframe
    file_size: int
    raw_sha256: str
    row_count: int
    first_timestamp_utc: datetime
    last_timestamp_utc: datetime
    acquired_at_utc: datetime
    dataset: CanonicalDataset

    def document(self) -> dict[str, object]:
        return {
            "status": "ACCEPTED",
            "original_local_file_path": str(self.path),
            "instrument": self.symbol,
            "timeframe": self.timeframe.value,
            "file_size_bytes": self.file_size,
            "raw_file_sha256": self.raw_sha256,
            "row_count": self.row_count,
            "first_timestamp_utc": self.first_timestamp_utc,
            "last_timestamp_utc": self.last_timestamp_utc,
            "schema": list(EXPECTED_SCHEMA),
            "provider": LOCAL_PROVIDER,
            "acquisition_tool": ACQUISITION_TOOL,
            "engine": ACQUISITION_ENGINE,
            "acquisition_mode": ACQUISITION_MODE,
            "source_quality": SOURCE_QUALITY,
            "normalized_dataset_fingerprint": self.dataset.dataset_fingerprint,
        }


@dataclass(frozen=True)
class ResamplingRecord:
    """Auditable complete-bucket aggregation facts."""

    symbol: str
    source_timeframe: Timeframe
    target_timeframe: Timeframe
    parent_dataset_fingerprint: str
    child_dataset_fingerprint: str
    expected_source_rows_per_bucket: int
    completed_bucket_count: int
    incomplete_bucket_count: int
    root_source_gap_count: int
    root_source_missing_intervals: int
    root_source_gaps: tuple[RootSourceGap, ...]
    derived_omissions: tuple[DerivedBucketOmission, ...]

    def document(self) -> dict[str, object]:
        return {
            "instrument": self.symbol,
            "source_timeframe": self.source_timeframe.value,
            "target_timeframe": self.target_timeframe.value,
            "parent_dataset_fingerprint": self.parent_dataset_fingerprint,
            "child_dataset_fingerprint": self.child_dataset_fingerprint,
            "aggregation_rule": "COMPLETE_UTC_BUCKETS_ONLY",
            "expected_source_rows_per_bucket": self.expected_source_rows_per_bucket,
            "completed_bucket_count": self.completed_bucket_count,
            "incomplete_bucket_count": self.incomplete_bucket_count,
            "root_source_gap_count": self.root_source_gap_count,
            "root_source_missing_intervals": self.root_source_missing_intervals,
            "root_source_gaps": [item.document() for item in self.root_source_gaps],
            "derived_omitted_bucket_count": self.incomplete_bucket_count,
            "derived_omitted_bucket_count_linked_to_source_gap": sum(
                bool(item.root_source_gap_ids) for item in self.derived_omissions
            ),
            "derived_omitted_bucket_count_unlinked": sum(
                not item.root_source_gap_ids for item in self.derived_omissions
            ),
            "derived_omissions": [item.document() for item in self.derived_omissions],
            "lineage_policy": (
                "A complete target bucket is emitted only when every required source row is "
                "present. A missing source row produces DERIVED_BUCKET_OMITTED linked to its "
                "root source-gap identifier; it is not a separate provider failure."
            ),
        }


@dataclass(frozen=True)
class RootSourceGap:
    """One original missing-data interval that can cause derived omissions."""

    identifier: str
    after_utc: datetime
    before_utc: datetime
    missing_intervals: int

    def document(self) -> dict[str, object]:
        return {
            "identifier": self.identifier,
            "classification": "SOURCE_GAP",
            "after_utc": self.after_utc,
            "before_utc": self.before_utc,
            "missing_intervals": self.missing_intervals,
        }


@dataclass(frozen=True)
class DerivedBucketOmission:
    """One omitted target bucket with links to its root source gap(s)."""

    bucket_start_utc: datetime
    missing_source_timestamps: tuple[datetime, ...]
    root_source_gap_ids: tuple[str, ...]

    def document(self) -> dict[str, object]:
        return {
            "classification": "DERIVED_BUCKET_OMITTED",
            "bucket_start_utc": self.bucket_start_utc,
            "missing_source_timestamps": list(self.missing_source_timestamps),
            "root_source_gap_ids": list(self.root_source_gap_ids),
        }


class LocalDukascopyGoCsvSource:
    """Read and validate only the supplied local public-feed CSV files."""

    def __init__(self, root_directory: Path) -> None:
        self.root_directory = root_directory
        self._prepared = False
        self._records: dict[tuple[str, Timeframe], LocalFileRecord] = {}
        self._errors: dict[tuple[str, Timeframe], str] = {}
        self._file_documents: list[dict[str, object]] = []
        self._resampled: dict[tuple[str, Timeframe], tuple[CanonicalDataset, ResamplingRecord]] = {}

    def prepare(self) -> None:
        """Validate every CSV in both supplied local directories exactly once."""

        if self._prepared:
            return
        self._prepared = True
        self._discover_directory("m1_90d", Timeframe.M1, M1_SYMBOLS)
        self._discover_directory("h1_2y", Timeframe.H1, H1_SYMBOLS)

    def available(self, symbol: str, timeframe: Timeframe) -> bool:
        self.prepare()
        source_timeframe = _source_timeframe(timeframe)
        return (symbol, source_timeframe) in self._records

    def load(self, symbol: str, timeframe: Timeframe, asset_class: AssetClass) -> ResearchDataset:
        if asset_class is not AssetClass.FX:
            raise ExternalHistoryUnavailable(f"LOCAL_DUKASCOPY_CSV_UNSUPPORTED_ASSET:{symbol}")
        self.prepare()
        source_timeframe = _source_timeframe(timeframe)
        key = (symbol, source_timeframe)
        record = self._records.get(key)
        if record is None:
            error = self._errors.get(key)
            if error is not None:
                raise ExternalHistoryUnavailable(error)
            raise ExternalHistoryUnavailable(
                f"LOCAL_DUKASCOPY_CSV_NOT_AVAILABLE:{symbol}:{source_timeframe.value}"
            )
        if timeframe is source_timeframe:
            dataset = record.dataset
            parent = None
        else:
            dataset, resampling = self._resample(record.dataset, timeframe)
            parent = resampling.parent_dataset_fingerprint
        return ResearchDataset(
            dataset=dataset,
            provenance=ProviderProvenance(
                provider=LOCAL_PROVIDER,
                provider_symbol=symbol,
                acquisition_timestamp_utc=record.acquired_at_utc,
                source_url=str(record.path),
                raw_source_fingerprint=record.raw_sha256,
                normalized_fingerprint=dataset.dataset_fingerprint,
                license_source_note=(
                    "Dukascopy public historical feed exported locally with "
                    "dukascopy-go 0.2.0 (jetta); external unverified research data, not IG candles."
                ),
                parent_dataset_fingerprint=parent,
            ),
            depth_status=_depth_status(dataset),
            cached=True,
        )

    def import_validation_document(self) -> dict[str, object]:
        self.prepare()
        accepted = [item for item in self._file_documents if item["status"] == "ACCEPTED"]
        rejected = [item for item in self._file_documents if item["status"] != "ACCEPTED"]
        return {
            "schema_version": "strategy-lab-sl04-local-import-validation/1.0",
            "provider": LOCAL_PROVIDER,
            "acquisition_tool": ACQUISITION_TOOL,
            "engine": ACQUISITION_ENGINE,
            "acquisition_mode": ACQUISITION_MODE,
            "expected_schema": list(EXPECTED_SCHEMA),
            "midpoint_tolerance": MIDPOINT_TOLERANCE,
            "files_discovered": len(self._file_documents),
            "files_accepted": len(accepted),
            "files_rejected": len(rejected),
            "raw_rows_imported": sum(int(item.get("row_count", 0)) for item in accepted),
            "raw_validation_counts": {
                "duplicate_timestamps": 0,
                "out_of_order_timestamps": 0,
                "invalid_ohlc": 0,
                "invalid_bid_ask_geometry": 0,
                "midpoint_mismatches": 0,
                "negative_spread": 0,
                "negative_volume": 0,
                "invalid_utc_timestamps": 0,
                "rejected_files": len(rejected),
            },
            "files": self._file_documents,
            "policy": (
                "A malformed row or file is never rewritten. Its affected local dataset is "
                "unavailable to the priority source and the reason remains recorded."
            ),
            "execution_authority": "OFF",
        }

    def resampling_document(self) -> dict[str, object]:
        return {
            "schema_version": "strategy-lab-sl04-resampling/1.0",
            "records": [
                item.document()
                for _, item in sorted((key, value[1]) for key, value in self._resampled.items())
            ],
            "execution_authority": "OFF",
        }

    def local_source_manifest(self) -> dict[str, object]:
        return {
            "schema_version": "strategy-lab-sl04-local-source-manifest/1.0",
            "provider": LOCAL_PROVIDER,
            "acquisition_tool": ACQUISITION_TOOL,
            "engine": ACQUISITION_ENGINE,
            "acquisition_mode": ACQUISITION_MODE,
            "source_quality": SOURCE_QUALITY,
            "source_root": str(self.root_directory),
            "files": self.import_validation_document()["files"],
            "execution_authority": "OFF",
        }

    def _discover_directory(
        self, name: str, timeframe: Timeframe, expected_symbols: frozenset[str]
    ) -> None:
        directory = self.root_directory / name
        if not directory.is_dir():
            return
        for path in sorted(directory.glob("*.csv")):
            symbol = path.stem.upper()
            key = (symbol, timeframe)
            try:
                if symbol not in expected_symbols:
                    raise DataContractError(f"LOCAL_DUKASCOPY_CSV_UNEXPECTED_INSTRUMENT:{symbol}")
                record = self._read_file(path, symbol, timeframe)
            except (DataContractError, OSError) as error:
                self._errors[key] = str(error)
                self._file_documents.append(_rejected_document(path, symbol, timeframe, str(error)))
            else:
                self._records[key] = record
                self._file_documents.append(record.document())

    def _read_file(self, path: Path, symbol: str, timeframe: Timeframe) -> LocalFileRecord:
        raw = path.read_bytes()
        raw_sha256 = hashlib.sha256(raw).hexdigest()
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as error:
            raise DataContractError("LOCAL_DUKASCOPY_CSV_UTF8_REQUIRED") from error
        try:
            reader = csv.DictReader(io.StringIO(text, newline=""))
        except csv.Error as error:
            raise DataContractError("LOCAL_DUKASCOPY_CSV_PARSE_ERROR") from error
        if tuple(reader.fieldnames or ()) != EXPECTED_SCHEMA:
            raise DataContractError("LOCAL_DUKASCOPY_CSV_SCHEMA_MISMATCH")
        candles: list[LabCandle] = []
        previous: datetime | None = None
        try:
            for row_number, row in enumerate(reader, start=2):
                candle = _parse_row(row, symbol, timeframe, row_number)
                if previous is not None:
                    if candle.timestamp_utc == previous:
                        raise DataContractError("LOCAL_DUKASCOPY_CSV_DUPLICATE_TIMESTAMP")
                    if candle.timestamp_utc < previous:
                        raise DataContractError("LOCAL_DUKASCOPY_CSV_OUT_OF_ORDER_TIMESTAMP")
                previous = candle.timestamp_utc
                candles.append(candle)
        except csv.Error as error:
            raise DataContractError("LOCAL_DUKASCOPY_CSV_PARSE_ERROR") from error
        if not candles:
            raise DataContractError("LOCAL_DUKASCOPY_CSV_EMPTY")
        dataset = build_dataset(candles, source_documents=(raw,))
        stat = path.stat()
        return LocalFileRecord(
            path=path.resolve(),
            symbol=symbol,
            timeframe=timeframe,
            file_size=stat.st_size,
            raw_sha256=raw_sha256,
            row_count=len(candles),
            first_timestamp_utc=candles[0].timestamp_utc,
            last_timestamp_utc=candles[-1].timestamp_utc,
            acquired_at_utc=datetime.fromtimestamp(stat.st_mtime, tz=UTC),
            dataset=dataset,
        )

    def _resample(
        self, dataset: CanonicalDataset, target: Timeframe
    ) -> tuple[CanonicalDataset, ResamplingRecord]:
        key = (dataset.instrument, target)
        cached = self._resampled.get(key)
        if cached is not None:
            return cached
        source_interval = TIMEFRAME_INTERVALS[dataset.timeframe]
        target_interval = TIMEFRAME_INTERVALS[target]
        if target_interval <= source_interval or target_interval % source_interval:
            raise DataContractError("SL04_LOCAL_RESAMPLING_REQUIRES_LARGER_INTEGRAL_TIMEFRAME")
        expected = int(target_interval / source_interval)
        grouped: dict[datetime, list[LabCandle]] = defaultdict(list)
        for candle in dataset.candles:
            seconds = int(candle.timestamp_utc.timestamp())
            bucket_seconds = int(target_interval.total_seconds())
            bucket = datetime.fromtimestamp(seconds - seconds % bucket_seconds, tz=UTC)
            grouped[bucket].append(candle)
        root_gaps = tuple(
            gap for gap in dataset.gaps if gap.classification is GapClassification.MISSING_DATA
        )
        root_gap_ids = {gap: f"SOURCE_GAP_{number:05d}" for number, gap in enumerate(root_gaps, 1)}
        candidate_buckets = set(grouped)
        for gap in root_gaps:
            first_missing = gap.after_utc + source_interval
            last_missing = gap.before_utc - source_interval
            point = _bucket_start(first_missing, target_interval)
            while point <= last_missing:
                candidate_buckets.add(point)
                point += target_interval
        complete: list[LabCandle] = []
        omissions: list[DerivedBucketOmission] = []
        for bucket in sorted(candidate_buckets):
            members = grouped.get(bucket, [])
            ordered = tuple(sorted(members, key=lambda item: item.timestamp_utc))
            expected_timestamps = tuple(
                bucket + source_interval * index for index in range(expected)
            )
            if (
                len(ordered) != expected
                or tuple(item.timestamp_utc for item in ordered) != expected_timestamps
            ):
                present = {item.timestamp_utc for item in ordered}
                missing = tuple(item for item in expected_timestamps if item not in present)
                sources = tuple(
                    root_gap_ids[gap]
                    for gap in root_gaps
                    if any(gap.after_utc < point < gap.before_utc for point in missing)
                )
                omissions.append(DerivedBucketOmission(bucket, missing, sources))
                continue
            complete.append(
                LabCandle(
                    instrument=dataset.instrument,
                    timestamp_utc=bucket,
                    timeframe=target,
                    open=ordered[0].open,
                    high=max(item.high for item in ordered),
                    low=min(item.low for item in ordered),
                    close=ordered[-1].close,
                    spread=ordered[-1].spread,
                    volume=(
                        sum(
                            (item.volume for item in ordered if item.volume is not None),
                            Decimal("0"),
                        )
                        if all(item.volume is not None for item in ordered)
                        else None
                    ),
                    source=LOCAL_PROVIDER,
                    source_quality=SourceQuality.EXTERNAL_UNVERIFIED,
                    gap_classification=GapClassification.NONE,
                    synthetic=False,
                )
            )
        if not complete:
            raise DataContractError("SL04_LOCAL_RESAMPLING_NO_COMPLETE_BUCKETS")
        child = build_dataset(
            complete,
            source_documents=(
                dataset.source_fingerprint,
                dataset.dataset_fingerprint,
                f"sl04-local-complete-resample/{dataset.timeframe.value}-to-{target.value}",
            ),
        )
        result = (
            child,
            ResamplingRecord(
                symbol=dataset.instrument,
                source_timeframe=dataset.timeframe,
                target_timeframe=target,
                parent_dataset_fingerprint=dataset.dataset_fingerprint,
                child_dataset_fingerprint=child.dataset_fingerprint,
                expected_source_rows_per_bucket=expected,
                completed_bucket_count=len(complete),
                incomplete_bucket_count=len(omissions),
                root_source_gap_count=len(root_gaps),
                root_source_missing_intervals=sum(gap.missing_intervals for gap in root_gaps),
                root_source_gaps=tuple(
                    RootSourceGap(
                        root_gap_ids[gap],
                        gap.after_utc,
                        gap.before_utc,
                        gap.missing_intervals,
                    )
                    for gap in root_gaps
                ),
                derived_omissions=tuple(omissions),
            ),
        )
        self._resampled[key] = result
        return result


def _source_timeframe(timeframe: Timeframe) -> Timeframe:
    if timeframe in {Timeframe.M1, Timeframe.M5, Timeframe.M15}:
        return Timeframe.M1
    if timeframe in {Timeframe.H1, Timeframe.H4}:
        return Timeframe.H1
    raise DataContractError(f"SL04_LOCAL_TIMEFRAME_UNSUPPORTED:{timeframe.value}")


def _bucket_start(timestamp: datetime, interval) -> datetime:
    seconds = int(timestamp.timestamp())
    bucket_seconds = int(interval.total_seconds())
    return datetime.fromtimestamp(seconds - seconds % bucket_seconds, tz=UTC)


def _parse_row(
    row: dict[str | None, str | None], symbol: str, timeframe: Timeframe, row_number: int
) -> LabCandle:
    try:
        timestamp = _utc_timestamp(_required(row, "timestamp"))
        values = {name: _decimal(_required(row, name)) for name in EXPECTED_SCHEMA[1:]}
        _validate_ohlc(values, "mid")
        _validate_ohlc(values, "bid")
        _validate_ohlc(values, "ask")
        if values["spread"] < 0:
            raise DataContractError("LOCAL_DUKASCOPY_CSV_NEGATIVE_SPREAD")
        if values["volume"] < 0:
            raise DataContractError("LOCAL_DUKASCOPY_CSV_NEGATIVE_VOLUME")
        for field in ("open", "high", "low", "close"):
            midpoint = (values[f"bid_{field}"] + values[f"ask_{field}"]) / Decimal("2")
            if abs(values[f"mid_{field}"] - midpoint) > MIDPOINT_TOLERANCE:
                raise DataContractError(f"LOCAL_DUKASCOPY_CSV_MIDPOINT_MISMATCH:{field}")
        return LabCandle(
            instrument=symbol,
            timestamp_utc=timestamp,
            timeframe=timeframe,
            open=values["mid_open"],
            high=values["mid_high"],
            low=values["mid_low"],
            close=values["mid_close"],
            spread=values["spread"],
            volume=values["volume"],
            source=LOCAL_PROVIDER,
            source_quality=SourceQuality.EXTERNAL_UNVERIFIED,
            gap_classification=GapClassification.NONE,
            synthetic=False,
        )
    except DataContractError:
        raise
    except (InvalidOperation, KeyError, TypeError, ValueError) as error:
        raise DataContractError(f"LOCAL_DUKASCOPY_CSV_INVALID_ROW:{row_number}") from error


def _required(row: dict[str | None, str | None], name: str) -> str:
    value = row.get(name)
    if value is None or not value.strip():
        raise DataContractError(f"LOCAL_DUKASCOPY_CSV_REQUIRED_VALUE_MISSING:{name}")
    return value


def _decimal(value: str) -> Decimal:
    result = Decimal(value)
    if not result.is_finite():
        raise DataContractError("LOCAL_DUKASCOPY_CSV_NONFINITE_NUMBER")
    return result


def _utc_timestamp(value: str) -> datetime:
    if not value.endswith("Z"):
        raise DataContractError("LOCAL_DUKASCOPY_CSV_TIMESTAMP_NOT_UTC_Z")
    timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if timestamp.tzinfo is None or timestamp.utcoffset() != UTC.utcoffset(timestamp):
        raise DataContractError("LOCAL_DUKASCOPY_CSV_TIMESTAMP_NOT_UTC")
    return timestamp.astimezone(UTC)


def _validate_ohlc(values: dict[str, Decimal], prefix: str) -> None:
    opening, high, low, close = (
        values[f"{prefix}_{name}"] for name in ("open", "high", "low", "close")
    )
    if high < low or low > min(opening, close) or high < max(opening, close):
        raise DataContractError(f"LOCAL_DUKASCOPY_CSV_INVALID_{prefix.upper()}_OHLC")


def _depth_status(dataset: CanonicalDataset) -> DatasetDepthStatus:
    minimum_days = {
        Timeframe.H4: 365,
        Timeframe.H1: 180,
        Timeframe.M15: 90,
        Timeframe.M5: 60,
        Timeframe.M1: 60,
    }[dataset.timeframe]
    duration = dataset.candles[-1].timestamp_utc - dataset.candles[0].timestamp_utc
    return (
        DatasetDepthStatus.SUFFICIENT
        if duration.days >= minimum_days
        else DatasetDepthStatus.LOW_DATA_DEPTH
    )


def _rejected_document(
    path: Path, symbol: str, timeframe: Timeframe, reason: str
) -> dict[str, object]:
    raw = b""
    with suppress(OSError):
        raw = path.read_bytes()
    return {
        "status": "REJECTED",
        "original_local_file_path": str(path.resolve()),
        "instrument": symbol,
        "timeframe": timeframe.value,
        "file_size_bytes": len(raw),
        "raw_file_sha256": hashlib.sha256(raw).hexdigest() if raw else None,
        "schema": list(EXPECTED_SCHEMA),
        "provider": LOCAL_PROVIDER,
        "acquisition_tool": ACQUISITION_TOOL,
        "engine": ACQUISITION_ENGINE,
        "acquisition_mode": ACQUISITION_MODE,
        "source_quality": SOURCE_QUALITY,
        "reason": reason,
    }
