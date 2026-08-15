"""Canonical G3A candle normalization, quality analysis, and fingerprints."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from math import isfinite
from typing import Any

WORK_ORDER = "G3A-01"
SOURCE = "IG_REST_GET_PRICES_V3"
ENVIRONMENT = "DEMO"
CANONICAL_SCHEMA_VERSION = "g3a-canonical-candle/1.1.0"
MANIFEST_SCHEMA_VERSION = "g3a-dataset-manifest/1.0.0"
NORMALIZATION_VERSION = "g3a-normalizer/1.1.0"
SECOND_AGGREGATION_VERSION = "g3a-second-to-minute/1.0.0"
NATIVE_PROVENANCE = "IG_NATIVE_HISTORICAL"
DERIVED_SECOND_PROVENANCE = "DERIVED_FROM_IG_AUTHORITATIVE_1S"
MINIMUM_SCALPER_CANDLES = 60
STALE_SEQUENCE_MINIMUM = 5

RESOLUTION_MINUTES: Mapping[str, int] = {
    "HOUR": 60,
    "MINUTE_15": 15,
    "MINUTE_5": 5,
    "MINUTE": 1,
}
RESOLUTION_LABELS: Mapping[str, str] = {
    "HOUR": "1H",
    "MINUTE_15": "15M",
    "MINUTE_5": "5M",
    "MINUTE": "1M",
}
RESOLUTION_SECONDS: Mapping[str, int] = {
    **{key: minutes * 60 for key, minutes in RESOLUTION_MINUTES.items()},
    "SECOND": 1,
}


class FinalClassification(StrEnum):
    """The final classifications authorized by G3A-01."""

    PASS = "PASS"
    PARTIAL_DATA = "PARTIAL_DATA"
    API_ALLOWANCE_LIMIT = "API_ALLOWANCE_LIMIT"
    SCHEMA_GAP = "SCHEMA_GAP"
    TIMEZONE_GAP = "TIMEZONE_GAP"
    DATA_QUALITY_FAILURE = "DATA_QUALITY_FAILURE"
    INCONCLUSIVE = "INCONCLUSIVE"


class QualityStatus(StrEnum):
    QUALIFIED = "QUALIFIED"
    PARTIAL_DATA = "PARTIAL_DATA"
    API_ALLOWANCE_LIMIT = "API_ALLOWANCE_LIMIT"
    SCHEMA_GAP = "SCHEMA_GAP"
    TIMEZONE_GAP = "TIMEZONE_GAP"
    DATA_QUALITY_FAILURE = "DATA_QUALITY_FAILURE"
    INCONCLUSIVE = "INCONCLUSIVE"


class GapClassification(StrEnum):
    EXPECTED_WEEKEND_CLOSURE = "EXPECTED_WEEKEND_CLOSURE"
    EXPECTED_MARKET_SESSION_GAP = "EXPECTED_MARKET_SESSION_GAP"
    BROKER_MAINTENANCE = "BROKER_MAINTENANCE"
    ACTUAL_MISSING_DATA = "ACTUAL_MISSING_DATA"
    API_ALLOWANCE_LIMITATION = "API_ALLOWANCE_LIMITATION"


@dataclass(frozen=True)
class FrozenInstrument:
    symbol: str
    instrument_name: str
    epic: str


FROZEN_INSTRUMENTS = (
    FrozenInstrument("EURGBP", "EUR/GBP Mini", "CS.D.EURGBP.MINI.IP"),
    FrozenInstrument("EURUSD", "EUR/USD Mini", "CS.D.EURUSD.CEFM.IP"),
    FrozenInstrument("GBPUSD", "GBP/USD Mini", "CS.D.GBPUSD.MINI.IP"),
)


@dataclass(frozen=True)
class RawFileEvidence:
    relative_path: str
    sha256: str
    size_bytes: int


@dataclass(frozen=True)
class CanonicalCandle:
    epic: str
    resolution: str
    timestamp_utc: datetime
    bid_open: float
    bid_high: float
    bid_low: float
    bid_close: float
    offer_open: float
    offer_high: float
    offer_low: float
    offer_close: float
    last_traded_open: float | None
    last_traded_high: float | None
    last_traded_low: float | None
    last_traded_close: float | None
    volume: float | None
    source_timestamp: str | None
    source_timestamp_utc: str
    source_page: int
    source_index: int
    source_raw_file: str
    provenance: str = NATIVE_PROVENANCE
    aggregation_version: str | None = None
    source_component_count: int | None = None


@dataclass(frozen=True)
class NormalizationFindings:
    raw_candle_count: int
    rejected_candle_count: int
    invalid_ohlc_count: int
    crossed_bid_offer_count: int
    zero_or_negative_spread_count: int
    timezone_ambiguity_count: int
    boundary_mismatch_count: int
    outside_request_range_count: int

    def __add__(self, other: NormalizationFindings) -> NormalizationFindings:
        return NormalizationFindings(
            *(
                left + right
                for left, right in zip(asdict(self).values(), asdict(other).values(), strict=True)
            )
        )


EMPTY_FINDINGS = NormalizationFindings(0, 0, 0, 0, 0, 0, 0, 0)


@dataclass(frozen=True)
class GapFinding:
    after_utc: datetime
    before_utc: datetime
    missing_intervals: int
    classification: GapClassification


@dataclass(frozen=True)
class StaleSequence:
    start_utc: datetime
    end_utc: datetime
    candle_count: int


@dataclass(frozen=True)
class SeriesDataset:
    symbol: str
    instrument_name: str
    epic: str
    resolution: str
    requested_start_utc: datetime
    requested_end_utc: datetime
    candles: tuple[CanonicalCandle, ...]
    findings: NormalizationFindings
    source_files: tuple[RawFileEvidence, ...]
    source_metadata: tuple[dict[str, object], ...]
    allowance_remaining: int | None


@dataclass(frozen=True)
class SeriesQuality:
    status: QualityStatus
    actual_start_utc: datetime | None
    actual_end_utc: datetime | None
    candle_count: int
    expected_count: int
    missing_intervals: int
    duplicate_timestamps: int
    non_monotonic_timestamps: int
    invalid_ohlc: int
    crossed_bid_offer_anomalies: int
    zero_or_negative_spread_anomalies: int
    stale_sequences: tuple[StaleSequence, ...]
    large_market_gaps: tuple[GapFinding, ...]
    timezone_ambiguity: int
    detected_gaps: tuple[GapFinding, ...]
    reason_codes: tuple[str, ...]


@dataclass(frozen=True)
class EpicVerification:
    symbol: str
    configured_epic: str
    configured_instrument_name: str
    discovered_epic: str | None
    detail_epic: str | None
    detail_instrument_name: str | None
    verified: bool
    reason: str
    source_files: tuple[RawFileEvidence, ...]


def verify_epic(
    instrument: FrozenInstrument,
    search_payload: object,
    detail_payload: object,
    *,
    source_files: tuple[RawFileEvidence, ...] = (),
) -> EpicVerification:
    """Require the configured EPIC and name in both live IG responses."""

    discovered_epic = None
    if isinstance(search_payload, Mapping) and isinstance(search_payload.get("markets"), list):
        exact_matches = [
            item
            for item in search_payload["markets"]
            if isinstance(item, Mapping)
            and item.get("epic") == instrument.epic
            and isinstance(item.get("instrumentName"), str)
            and _same_name(str(item["instrumentName"]), instrument.instrument_name)
        ]
        if len(exact_matches) == 1:
            discovered_epic = instrument.epic

    detail_epic = detail_name = None
    if isinstance(detail_payload, Mapping) and isinstance(
        detail_payload.get("instrument"), Mapping
    ):
        detail = detail_payload["instrument"]
        if isinstance(detail.get("epic"), str):
            detail_epic = str(detail["epic"])
        if isinstance(detail.get("name"), str):
            detail_name = str(detail["name"])

    verified = (
        discovered_epic == instrument.epic
        and detail_epic == instrument.epic
        and detail_name is not None
        and _same_name(detail_name, instrument.instrument_name)
    )
    if discovered_epic != instrument.epic:
        reason = "SEARCH_EXACT_MATCH_NOT_FOUND"
    elif detail_epic != instrument.epic:
        reason = "DETAIL_EPIC_MISMATCH"
    elif detail_name is None or not _same_name(detail_name, instrument.instrument_name):
        reason = "DETAIL_INSTRUMENT_NAME_MISMATCH"
    else:
        reason = "VERIFIED"
    return EpicVerification(
        instrument.symbol,
        instrument.epic,
        instrument.instrument_name,
        discovered_epic,
        detail_epic,
        detail_name,
        verified,
        reason,
        source_files,
    )


def normalize_candle(
    value: object,
    *,
    epic: str,
    resolution: str,
    source_page: int,
    source_index: int,
    source_raw_file: str,
    requested_start_utc: datetime,
    requested_end_utc: datetime,
) -> tuple[CanonicalCandle | None, NormalizationFindings]:
    """Strictly map one broker candle and reject ambiguous or invalid evidence."""

    reasons: set[str] = set()
    if not isinstance(value, Mapping) or resolution not in RESOLUTION_SECONDS:
        return None, NormalizationFindings(1, 1, 1, 0, 0, 0, 0, 0)
    source_timestamp_utc = value.get("snapshotTimeUTC")
    timestamp = parse_snapshot_time_utc(source_timestamp_utc)
    if timestamp is None:
        reasons.add("TIMEZONE_AMBIGUITY")
    blocks = tuple(
        _price_block(value.get(name))
        for name in ("openPrice", "highPrice", "lowPrice", "closePrice")
    )
    if any(block is None for block in blocks):
        reasons.add("INVALID_OHLC")
    if timestamp is not None:
        interval_seconds = RESOLUTION_SECONDS[resolution]
        if timestamp.timestamp() % interval_seconds != 0:
            reasons.add("CANDLE_BOUNDARY_MISMATCH")
        if not requested_start_utc <= timestamp < requested_end_utc:
            reasons.add("CANDLE_OUTSIDE_REQUEST_RANGE")
    parsed_blocks = tuple(item for item in blocks if item is not None)
    if len(parsed_blocks) == 4:
        bid = tuple(item[0] for item in parsed_blocks)
        offer = tuple(item[1] for item in parsed_blocks)
        if not _valid_geometry(*bid) or not _valid_geometry(*offer):
            reasons.add("INVALID_OHLC")
        spreads = tuple(
            offer_value - bid_value for bid_value, offer_value in zip(bid, offer, strict=True)
        )
        if any(spread < 0 for spread in spreads):
            reasons.add("CROSSED_BID_OFFER")
            reasons.add("ZERO_OR_NEGATIVE_SPREAD")
        elif any(spread == 0 for spread in spreads):
            reasons.add("ZERO_OR_NEGATIVE_SPREAD")
    volume = _optional_nonnegative(value.get("lastTradedVolume"))
    if value.get("lastTradedVolume") is not None and volume is None:
        reasons.add("INVALID_OHLC")
    rejecting = {
        "TIMEZONE_AMBIGUITY",
        "CANDLE_BOUNDARY_MISMATCH",
        "CANDLE_OUTSIDE_REQUEST_RANGE",
        "INVALID_OHLC",
        "CROSSED_BID_OFFER",
    }
    rejected = bool(reasons & rejecting)
    findings = NormalizationFindings(
        raw_candle_count=1,
        rejected_candle_count=int(rejected),
        invalid_ohlc_count=int("INVALID_OHLC" in reasons),
        crossed_bid_offer_count=int("CROSSED_BID_OFFER" in reasons),
        zero_or_negative_spread_count=int("ZERO_OR_NEGATIVE_SPREAD" in reasons),
        timezone_ambiguity_count=int("TIMEZONE_AMBIGUITY" in reasons),
        boundary_mismatch_count=int("CANDLE_BOUNDARY_MISMATCH" in reasons),
        outside_request_range_count=int("CANDLE_OUTSIDE_REQUEST_RANGE" in reasons),
    )
    if rejected or timestamp is None or len(parsed_blocks) != 4:
        return None, findings
    open_price, high_price, low_price, close_price = parsed_blocks
    source_timestamp = value.get("snapshotTime")
    return (
        CanonicalCandle(
            epic=epic,
            resolution=resolution,
            timestamp_utc=timestamp,
            bid_open=open_price[0],
            bid_high=high_price[0],
            bid_low=low_price[0],
            bid_close=close_price[0],
            offer_open=open_price[1],
            offer_high=high_price[1],
            offer_low=low_price[1],
            offer_close=close_price[1],
            last_traded_open=open_price[2],
            last_traded_high=high_price[2],
            last_traded_low=low_price[2],
            last_traded_close=close_price[2],
            volume=volume,
            source_timestamp=(source_timestamp if isinstance(source_timestamp, str) else None),
            source_timestamp_utc=str(source_timestamp_utc),
            source_page=source_page,
            source_index=source_index,
            source_raw_file=source_raw_file,
        ),
        findings,
    )


def parse_snapshot_time_utc(value: object) -> datetime | None:
    """Parse only the broker's explicitly UTC timestamp field."""

    if not isinstance(value, str) or not value.strip():
        return None
    normalized = value.strip().replace("/", "-").replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    if parsed.utcoffset() is None:
        return None
    return parsed.astimezone(UTC)


def qualify_series(
    datasets: tuple[SeriesDataset, ...],
) -> dict[tuple[str, str], SeriesQuality]:
    """Calculate every required quality metric without filling missing candles."""

    missing_occurrence: Counter[tuple[str, datetime]] = Counter()
    for dataset in datasets:
        interval = timedelta(minutes=RESOLUTION_MINUTES[dataset.resolution])
        for gap in series_gaps(dataset, interval):
            for missing in missing_timestamps(gap, interval):
                missing_occurrence[(dataset.resolution, missing)] += 1

    result: dict[tuple[str, str], SeriesQuality] = {}
    for dataset in datasets:
        timestamps = [item.timestamp_utc for item in dataset.candles]
        duplicate_count = len(timestamps) - len(set(timestamps))
        non_monotonic = sum(
            current < previous
            for previous, current in zip(timestamps, timestamps[1:], strict=False)
        )
        interval = timedelta(minutes=RESOLUTION_MINUTES[dataset.resolution])
        gaps: list[GapFinding] = []
        for raw_gap in series_gaps(dataset, interval):
            absent = missing_timestamps(raw_gap, interval)
            classified = tuple(
                (
                    missing,
                    _gap_classification(
                        missing,
                        resolution=dataset.resolution,
                        missing_occurrence=missing_occurrence,
                    ),
                )
                for missing in absent
            )
            gaps.extend(_group_classified_gaps(classified, interval))

        stale = stale_sequences(dataset.candles)
        actual_missing = sum(
            item.missing_intervals
            for item in gaps
            if item.classification is GapClassification.ACTUAL_MISSING_DATA
        )
        reasons: list[str] = []
        findings = dataset.findings
        reason_conditions = (
            (findings.timezone_ambiguity_count, "TIMEZONE_AMBIGUITY"),
            (findings.rejected_candle_count, "INVALID_CANDLES_REJECTED"),
            (findings.invalid_ohlc_count, "INVALID_OHLC"),
            (findings.crossed_bid_offer_count, "CROSSED_BID_OFFER"),
            (findings.zero_or_negative_spread_count, "ZERO_OR_NEGATIVE_SPREAD"),
            (findings.boundary_mismatch_count, "CANDLE_BOUNDARY_MISMATCH"),
            (findings.outside_request_range_count, "CANDLE_OUTSIDE_REQUEST_RANGE"),
            (duplicate_count, "DUPLICATE_TIMESTAMPS"),
            (non_monotonic, "NON_MONOTONIC_TIMESTAMPS"),
            (actual_missing, "ACTUAL_MISSING_DATA"),
            (stale, "STALE_SEQUENCES_OBSERVED"),
        )
        reasons.extend(reason for condition, reason in reason_conditions if condition)
        if len(dataset.candles) < MINIMUM_SCALPER_CANDLES:
            reasons.append("SCALPER_WARMUP_SHORTFALL")

        if findings.timezone_ambiguity_count:
            status = QualityStatus.TIMEZONE_GAP
        elif (
            findings.rejected_candle_count
            or findings.zero_or_negative_spread_count
            or duplicate_count
            or non_monotonic
        ):
            status = QualityStatus.DATA_QUALITY_FAILURE
        elif actual_missing or len(dataset.candles) < MINIMUM_SCALPER_CANDLES:
            status = QualityStatus.PARTIAL_DATA
        else:
            status = QualityStatus.QUALIFIED
        expected_count = expected_market_count(
            dataset.requested_start_utc,
            dataset.requested_end_utc,
            interval,
        )
        result[(dataset.symbol, dataset.resolution)] = SeriesQuality(
            status=status,
            actual_start_utc=min(timestamps) if timestamps else None,
            actual_end_utc=max(timestamps) if timestamps else None,
            candle_count=len(timestamps),
            expected_count=expected_count,
            missing_intervals=sum(item.missing_intervals for item in gaps),
            duplicate_timestamps=duplicate_count,
            non_monotonic_timestamps=non_monotonic,
            invalid_ohlc=findings.invalid_ohlc_count,
            crossed_bid_offer_anomalies=findings.crossed_bid_offer_count,
            zero_or_negative_spread_anomalies=(findings.zero_or_negative_spread_count),
            stale_sequences=stale,
            large_market_gaps=tuple(item for item in gaps if item.missing_intervals >= 5),
            timezone_ambiguity=findings.timezone_ambiguity_count,
            detected_gaps=tuple(gaps),
            reason_codes=tuple(reasons),
        )
    return result


def build_series_manifest(
    dataset: SeriesDataset,
    quality: SeriesQuality,
    *,
    retrieval_started_at: datetime,
    retrieval_completed_at: datetime,
    normalized_relative_path: str,
    normalized_sha256: str,
) -> dict[str, object]:
    """Build a deterministic, self-fingerprinted immutable manifest."""

    payload: dict[str, object] = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "canonical_schema_version": CANONICAL_SCHEMA_VERSION,
        "normalization_version": NORMALIZATION_VERSION,
        "work_order": WORK_ORDER,
        "source": SOURCE,
        "environment": ENVIRONMENT,
        "epic": dataset.epic,
        "instrument_name": dataset.instrument_name,
        "symbol": dataset.symbol,
        "resolution": dataset.resolution,
        "resolution_label": RESOLUTION_LABELS[dataset.resolution],
        "requested_start_utc": utc_text(dataset.requested_start_utc),
        "requested_end_utc": utc_text(dataset.requested_end_utc),
        "actual_start_utc": utc_text(quality.actual_start_utc),
        "actual_end_utc": utc_text(quality.actual_end_utc),
        "candle_count": quality.candle_count,
        "expected_count": quality.expected_count,
        "retrieval_started_at_utc": utc_text(retrieval_started_at),
        "retrieval_completed_at_utc": utc_text(retrieval_completed_at),
        "source_files": json_value(dataset.source_files),
        "source_metadata": json_value(dataset.source_metadata),
        "normalized_data": {
            "relative_path": normalized_relative_path,
            "sha256": normalized_sha256,
        },
        "detected_gaps": json_value(quality.detected_gaps),
        "quality": json_value(quality),
        "qualification_result": quality.status.value,
    }
    payload["manifest_fingerprint"] = fingerprint(payload)
    return payload


def replay_sufficiency(
    datasets: Sequence[SeriesDataset],
    qualities: Mapping[tuple[str, str], SeriesQuality],
) -> dict[str, object]:
    """Prove structural readiness for the existing 60-candle Scalper contract."""

    minute_sets = [
        {item.timestamp_utc for item in dataset.candles}
        for dataset in datasets
        if dataset.resolution == "MINUTE"
    ]
    common = set.intersection(*minute_sets) if len(minute_sets) == 3 else set()
    expected_keys = {
        (instrument.symbol, resolution)
        for instrument in FROZEN_INSTRUMENTS
        for resolution in RESOLUTION_MINUTES
    }
    all_qualified = set(qualities) == expected_keys and all(
        quality.status is QualityStatus.QUALIFIED for quality in qualities.values()
    )
    enough = all(quality.candle_count >= MINIMUM_SCALPER_CANDLES for quality in qualities.values())
    ready = all_qualified and enough and len(common) >= MINIMUM_SCALPER_CANDLES
    reasons = []
    if not all_qualified:
        reasons.append("ALL_TWELVE_SERIES_NOT_QUALIFIED")
    if not enough:
        reasons.append("TIMEFRAME_WARMUP_SHORTFALL")
    if len(common) < MINIMUM_SCALPER_CANDLES:
        reasons.append("COMMON_MINUTE_WARMUP_SHORTFALL")
    return {
        "strategy": "Scalper",
        "optimization_run": False,
        "required_resolutions": list(RESOLUTION_MINUTES),
        "minimum_candles_per_resolution": MINIMUM_SCALPER_CANDLES,
        "common_minute_candle_count": len(common),
        "common_minute_start_utc": utc_text(min(common) if common else None),
        "common_minute_end_utc": utc_text(max(common) if common else None),
        "ready": ready,
        "reason_codes": reasons,
    }


def overall_classification(
    qualities: Iterable[SeriesQuality], replay: Mapping[str, object]
) -> FinalClassification:
    statuses = {item.status for item in qualities}
    ordered = (
        (QualityStatus.API_ALLOWANCE_LIMIT, FinalClassification.API_ALLOWANCE_LIMIT),
        (QualityStatus.SCHEMA_GAP, FinalClassification.SCHEMA_GAP),
        (QualityStatus.TIMEZONE_GAP, FinalClassification.TIMEZONE_GAP),
        (QualityStatus.DATA_QUALITY_FAILURE, FinalClassification.DATA_QUALITY_FAILURE),
    )
    for status, classification in ordered:
        if status in statuses:
            return classification
    if QualityStatus.PARTIAL_DATA in statuses or replay.get("ready") is not True:
        return FinalClassification.PARTIAL_DATA
    if statuses == {QualityStatus.QUALIFIED}:
        return FinalClassification.PASS
    return FinalClassification.INCONCLUSIVE


def canonical_candle_document(value: CanonicalCandle) -> dict[str, object]:
    document = asdict(value)
    document["timestamp_utc"] = utc_text(value.timestamp_utc)
    document["schema_version"] = CANONICAL_SCHEMA_VERSION
    return document


def canonical_candle_from_document(value: object) -> CanonicalCandle:
    """Load a v1 canonical row while supplying explicit provenance for legacy rows."""

    if not isinstance(value, Mapping):
        raise ValueError("canonical candle must be an object")
    timestamp = parse_snapshot_time_utc(value.get("timestamp_utc"))
    if timestamp is None:
        raise ValueError("canonical timestamp is invalid")
    numeric_fields = (
        "bid_open",
        "bid_high",
        "bid_low",
        "bid_close",
        "offer_open",
        "offer_high",
        "offer_low",
        "offer_close",
    )
    parsed_numeric: dict[str, float] = {}
    for field_name in numeric_fields:
        parsed = _positive(value.get(field_name))
        if parsed is None:
            raise ValueError(f"canonical {field_name} is invalid")
        parsed_numeric[field_name] = parsed
    optional_last = {}
    for field_name in (
        "last_traded_open",
        "last_traded_high",
        "last_traded_low",
        "last_traded_close",
    ):
        parsed = _optional_positive(value.get(field_name))
        if value.get(field_name) is not None and parsed is None:
            raise ValueError(f"canonical {field_name} is invalid")
        optional_last[field_name] = parsed
    epic = value.get("epic")
    resolution = value.get("resolution")
    source_raw_file = value.get("source_raw_file")
    source_timestamp_utc = value.get("source_timestamp_utc")
    if (
        not isinstance(epic, str)
        or not epic
        or not isinstance(resolution, str)
        or resolution not in RESOLUTION_SECONDS
        or not isinstance(source_raw_file, str)
        or not source_raw_file
        or not isinstance(source_timestamp_utc, str)
        or not source_timestamp_utc
    ):
        raise ValueError("canonical identity or source is invalid")
    source_page = value.get("source_page")
    source_index = value.get("source_index")
    if (
        isinstance(source_page, bool)
        or not isinstance(source_page, int)
        or isinstance(source_index, bool)
        or not isinstance(source_index, int)
    ):
        raise ValueError("canonical source location is invalid")
    volume = _optional_nonnegative(value.get("volume"))
    if value.get("volume") is not None and volume is None:
        raise ValueError("canonical volume is invalid")
    source_component_count = value.get("source_component_count")
    if source_component_count is not None and (
        isinstance(source_component_count, bool)
        or not isinstance(source_component_count, int)
        or source_component_count < 1
    ):
        raise ValueError("canonical source component count is invalid")
    provenance = value.get("provenance", NATIVE_PROVENANCE)
    aggregation_version = value.get("aggregation_version")
    if not isinstance(provenance, str) or not provenance:
        raise ValueError("canonical provenance is invalid")
    if aggregation_version is not None and not isinstance(aggregation_version, str):
        raise ValueError("canonical aggregation version is invalid")
    source_timestamp = value.get("source_timestamp")
    if source_timestamp is not None and not isinstance(source_timestamp, str):
        raise ValueError("canonical source timestamp is invalid")
    return CanonicalCandle(
        epic=epic,
        resolution=str(resolution),
        timestamp_utc=timestamp,
        **parsed_numeric,
        **optional_last,
        volume=volume,
        source_timestamp=source_timestamp,
        source_timestamp_utc=source_timestamp_utc,
        source_page=source_page,
        source_index=source_index,
        source_raw_file=source_raw_file,
        provenance=provenance,
        aggregation_version=aggregation_version,
        source_component_count=source_component_count,
    )


def aggregate_second_candles(
    candles: Sequence[CanonicalCandle],
    minute_start_utc: datetime,
    *,
    epic: str,
    source_raw_file: str,
    require_every_second: bool = True,
) -> CanonicalCandle | None:
    """Aggregate authoritative broker second-bars without synthesizing a component."""

    minute_start = minute_start_utc.astimezone(UTC)
    if minute_start.second or minute_start.microsecond:
        raise ValueError("minute_start_utc must be minute aligned")
    ordered = sorted(
        (
            candle
            for candle in candles
            if minute_start <= candle.timestamp_utc < minute_start + timedelta(minutes=1)
        ),
        key=lambda candle: candle.timestamp_utc,
    )
    expected = tuple(minute_start + timedelta(seconds=index) for index in range(60))
    if (
        not ordered
        or len({candle.timestamp_utc for candle in ordered}) != len(ordered)
        or any(candle.resolution != "SECOND" or candle.epic != epic for candle in ordered)
        or (
            require_every_second
            and (
                len(ordered) != 60 or tuple(candle.timestamp_utc for candle in ordered) != expected
            )
        )
    ):
        return None
    last_fields = (
        "last_traded_open",
        "last_traded_high",
        "last_traded_low",
        "last_traded_close",
    )
    have_last_traded = all(
        getattr(candle, field_name) is not None for candle in ordered for field_name in last_fields
    )
    return CanonicalCandle(
        epic=epic,
        resolution="MINUTE",
        timestamp_utc=minute_start,
        bid_open=ordered[0].bid_open,
        bid_high=max(candle.bid_high for candle in ordered),
        bid_low=min(candle.bid_low for candle in ordered),
        bid_close=ordered[-1].bid_close,
        offer_open=ordered[0].offer_open,
        offer_high=max(candle.offer_high for candle in ordered),
        offer_low=min(candle.offer_low for candle in ordered),
        offer_close=ordered[-1].offer_close,
        last_traded_open=(ordered[0].last_traded_open if have_last_traded else None),
        last_traded_high=(
            max(float(candle.last_traded_high) for candle in ordered) if have_last_traded else None
        ),
        last_traded_low=(
            min(float(candle.last_traded_low) for candle in ordered) if have_last_traded else None
        ),
        last_traded_close=(ordered[-1].last_traded_close if have_last_traded else None),
        volume=(
            sum(float(candle.volume) for candle in ordered)
            if all(candle.volume is not None for candle in ordered)
            else None
        ),
        source_timestamp=None,
        source_timestamp_utc=utc_text(minute_start) or "",
        source_page=0,
        source_index=0,
        source_raw_file=source_raw_file,
        provenance=DERIVED_SECOND_PROVENANCE,
        aggregation_version=SECOND_AGGREGATION_VERSION,
        source_component_count=len(ordered),
    )


def compare_bid_offer_fields(
    reconstructed: CanonicalCandle,
    native: CanonicalCandle,
    *,
    decimal_places: int,
) -> tuple[dict[str, object], ...]:
    """Compare every replay price field at broker-reported decimal precision."""

    if not 0 <= decimal_places <= 12:
        raise ValueError("decimal_places is invalid")
    quantum = Decimal(1).scaleb(-decimal_places)
    fields = (
        "bid_open",
        "bid_high",
        "bid_low",
        "bid_close",
        "offer_open",
        "offer_high",
        "offer_low",
        "offer_close",
    )
    result = []
    for field_name in fields:
        reconstructed_value = Decimal(str(getattr(reconstructed, field_name)))
        native_value = Decimal(str(getattr(native, field_name)))
        result.append(
            {
                "field": field_name,
                "reconstructed": format(reconstructed_value, "f"),
                "native": format(native_value, "f"),
                "delta": format(reconstructed_value - native_value, "f"),
                "decimal_places": decimal_places,
                "match": (reconstructed_value.quantize(quantum) == native_value.quantize(quantum)),
            }
        )
    return tuple(result)


def canonical_json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def pretty_json_bytes(value: object) -> bytes:
    return (
        json.dumps(value, allow_nan=False, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")


def fingerprint(value: object) -> str:
    return sha256_bytes(canonical_json_bytes(value))


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def json_value(value: object) -> Any:
    if isinstance(value, datetime):
        return utc_text(value)
    if isinstance(value, StrEnum):
        return value.value
    if isinstance(value, tuple):
        return [json_value(item) for item in value]
    if isinstance(value, list):
        return [json_value(item) for item in value]
    if isinstance(value, Mapping):
        return {str(key): json_value(item) for key, item in value.items()}
    if hasattr(value, "__dataclass_fields__"):
        return {key: json_value(item) for key, item in asdict(value).items()}
    return value


def raw_gaps(
    candles: Sequence[CanonicalCandle], interval: timedelta
) -> tuple[tuple[datetime, datetime], ...]:
    ordered = sorted({item.timestamp_utc for item in candles})
    return tuple(
        (previous, current)
        for previous, current in zip(ordered, ordered[1:], strict=False)
        if current - previous > interval
    )


def series_gaps(
    dataset: SeriesDataset, interval: timedelta
) -> tuple[tuple[datetime, datetime], ...]:
    """Return leading, internal, and trailing gaps in the requested range."""

    ordered = sorted({item.timestamp_utc for item in dataset.candles})
    if not ordered:
        return ((dataset.requested_start_utc - interval, dataset.requested_end_utc),)
    gaps: list[tuple[datetime, datetime]] = []
    if ordered[0] > dataset.requested_start_utc:
        gaps.append((dataset.requested_start_utc - interval, ordered[0]))
    gaps.extend(raw_gaps(dataset.candles, interval))
    if ordered[-1] + interval < dataset.requested_end_utc:
        gaps.append((ordered[-1], dataset.requested_end_utc))
    return tuple(gaps)


def missing_timestamps(gap: tuple[datetime, datetime], interval: timedelta) -> tuple[datetime, ...]:
    values: list[datetime] = []
    current = gap[0] + interval
    while current < gap[1]:
        values.append(current)
        current += interval
    return tuple(values)


def _gap_classification(
    missing: datetime,
    *,
    resolution: str,
    missing_occurrence: Counter[tuple[str, datetime]],
) -> GapClassification:
    if inside_weekend_closure(missing):
        return GapClassification.EXPECTED_WEEKEND_CLOSURE
    if missing_occurrence[(resolution, missing)] == len(FROZEN_INSTRUMENTS):
        return GapClassification.EXPECTED_MARKET_SESSION_GAP
    return GapClassification.ACTUAL_MISSING_DATA


def _group_classified_gaps(
    classified: Sequence[tuple[datetime, GapClassification]],
    interval: timedelta,
) -> tuple[GapFinding, ...]:
    """Split a raw gap wherever its evidence-based classification changes."""

    if not classified:
        return ()
    result: list[GapFinding] = []
    segment_start = classified[0][0]
    segment_end = segment_start
    segment_classification = classified[0][1]
    for timestamp, classification in classified[1:]:
        if classification is segment_classification and timestamp == segment_end + interval:
            segment_end = timestamp
            continue
        result.append(
            GapFinding(
                segment_start - interval,
                segment_end + interval,
                int((segment_end - segment_start) / interval) + 1,
                segment_classification,
            )
        )
        segment_start = segment_end = timestamp
        segment_classification = classification
    result.append(
        GapFinding(
            segment_start - interval,
            segment_end + interval,
            int((segment_end - segment_start) / interval) + 1,
            segment_classification,
        )
    )
    return tuple(result)


def inside_weekend_closure(value: datetime) -> bool:
    """Conservative UTC FX closure rule; the DST edge uses session consensus."""

    utc = value.astimezone(UTC)
    return (
        utc.weekday() == 5
        or (utc.weekday() == 4 and utc.hour >= 22)
        or (utc.weekday() == 6 and utc.hour < 21)
    )


def expected_market_count(start: datetime, end: datetime, interval: timedelta) -> int:
    count = 0
    current = start
    while current < end:
        if not inside_weekend_closure(current):
            count += 1
        current += interval
    return count


def stale_sequences(candles: Sequence[CanonicalCandle]) -> tuple[StaleSequence, ...]:
    if not candles:
        return ()
    ordered = sorted(candles, key=lambda item: item.timestamp_utc)
    result: list[StaleSequence] = []
    start = 0
    for index in range(1, len(ordered) + 1):
        same = index < len(ordered) and _quote_key(ordered[index]) == _quote_key(ordered[index - 1])
        if same:
            continue
        length = index - start
        if length >= STALE_SEQUENCE_MINIMUM:
            result.append(
                StaleSequence(
                    ordered[start].timestamp_utc,
                    ordered[index - 1].timestamp_utc,
                    length,
                )
            )
        start = index
    return tuple(result)


def utc_text(value: datetime | None) -> str | None:
    return value.astimezone(UTC).isoformat() if value is not None else None


def _price_block(value: object) -> tuple[float, float, float | None] | None:
    if not isinstance(value, Mapping):
        return None
    bid = _positive(value.get("bid"))
    offer = _positive(value.get("ask"))
    last_traded = _optional_positive(value.get("lastTraded"))
    if value.get("lastTraded") is not None and last_traded is None:
        return None
    return (bid, offer, last_traded) if bid is not None and offer is not None else None


def _positive(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    parsed = float(value)
    return parsed if isfinite(parsed) and parsed > 0 else None


def _optional_positive(value: object) -> float | None:
    return None if value is None else _positive(value)


def _optional_nonnegative(value: object) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    parsed = float(value)
    return parsed if isfinite(parsed) and parsed >= 0 else None


def _valid_geometry(open_: float, high: float, low: float, close: float) -> bool:
    return low <= min(open_, close) <= max(open_, close) <= high


def _same_name(left: str, right: str) -> bool:
    def normalized(value: str) -> str:
        return "".join(character for character in value.casefold() if character.isalnum())

    return normalized(left) == normalized(right)


def _quote_key(candle: CanonicalCandle) -> tuple[float, ...]:
    return (
        candle.bid_open,
        candle.bid_high,
        candle.bid_low,
        candle.bid_close,
        candle.offer_open,
        candle.offer_high,
        candle.offer_low,
        candle.offer_close,
    )
