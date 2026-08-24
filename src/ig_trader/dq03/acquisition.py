"""Bounded, read-only broker history validation for verified DQ-03 contracts."""

# ruff: noqa: E501

from __future__ import annotations

import hashlib
import json
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Protocol

from src.ig_trader.dq03.models import DataStatus, DQ03Resolution, DQ03Status, RequestCounters


class DQ03HistoryTransport(Protocol):
    """The one read-only prices endpoint required for a validation sample."""

    def get_historical_prices(
        self, epic: str, resolution: str, points: int
    ) -> Mapping[str, object]: ...


@dataclass(frozen=True)
class _Timestamp:
    value: datetime
    source_field: str
    parser: str


@dataclass(frozen=True)
class _Price:
    mid: Decimal
    bid: Decimal | None
    ask_or_offer: Decimal | None
    side_field: str | None
    source: str


@dataclass(frozen=True)
class BrokerValidationRow:
    """A normalized, sanitized candle row retained as broker-validation evidence."""

    timestamp_utc: datetime
    timestamp_source_field: str
    timestamp_parser: str
    open_mid: Decimal
    high_mid: Decimal
    low_mid: Decimal
    close_mid: Decimal
    close_bid: Decimal | None
    close_ask_or_offer: Decimal | None
    close_side_field: str | None
    close_spread: Decimal | None
    open_source: str
    high_source: str
    low_source: str
    close_source: str

    def document(self) -> dict[str, object]:
        return {
            "timestamp_utc": self.timestamp_utc.astimezone(UTC).isoformat(),
            "timestamp_source_field": self.timestamp_source_field,
            "timestamp_parser": self.timestamp_parser,
            "open_mid": str(self.open_mid),
            "high_mid": str(self.high_mid),
            "low_mid": str(self.low_mid),
            "close_mid": str(self.close_mid),
            "close_bid": str(self.close_bid) if self.close_bid is not None else None,
            "close_ask_or_offer": (
                str(self.close_ask_or_offer) if self.close_ask_or_offer is not None else None
            ),
            "close_side_field": self.close_side_field,
            "close_spread": str(self.close_spread) if self.close_spread is not None else None,
            "open_source": self.open_source,
            "high_source": self.high_source,
            "low_source": self.low_source,
            "close_source": self.close_source,
        }


@dataclass(frozen=True)
class BrokerValidationSample:
    symbol: str
    epic: str
    resolution: str
    requested_points: int
    returned_points: int
    timestamp_shape_valid: bool
    ohlc_shape_valid: bool
    observed_spread_rows: int
    source_fingerprint: str | None
    status: DataStatus
    reason: str | None = None
    normalized_rows: tuple[BrokerValidationRow, ...] = ()
    first_timestamp_utc: str | None = None
    last_timestamp_utc: str | None = None
    duplicate_timestamp_count: int = 0
    invalid_row_count: int = 0
    timestamps_monotonic: bool = False
    resolution_ordering_valid: bool = False
    timestamp_parser_evidence: tuple[tuple[str, int], ...] = ()

    def document(self) -> dict[str, object]:
        return {
            "symbol": self.symbol,
            "epic": self.epic,
            "resolution": self.resolution,
            "requested_points": self.requested_points,
            "returned_points": self.returned_points,
            "timestamp_shape_valid": self.timestamp_shape_valid,
            "ohlc_shape_valid": self.ohlc_shape_valid,
            "observed_spread_rows": self.observed_spread_rows,
            "source_fingerprint": self.source_fingerprint,
            "status": self.status.value,
            "reason": self.reason,
            "rows": [row.document() for row in self.normalized_rows],
            "row_count": len(self.normalized_rows),
            "first_timestamp_utc": self.first_timestamp_utc,
            "last_timestamp_utc": self.last_timestamp_utc,
            "duplicate_timestamp_count": self.duplicate_timestamp_count,
            "invalid_row_count": self.invalid_row_count,
            "timestamps_monotonic": self.timestamps_monotonic,
            "resolution_ordering_valid": self.resolution_ordering_valid,
            "timestamp_parser_evidence": dict(self.timestamp_parser_evidence),
            "timestamp_policy": (
                "snapshotTimeUTC is authoritative and naive values in that explicitly UTC field "
                "are normalized as UTC; snapshotTime is documented broker-local time and is "
                "converted only with an explicit offset or the broker-declared session timezoneOffset."
            ),
        }


class DQ03HistoryAcquirer:
    """Quota-aware validation sampler; it never downloads broad history by default."""

    def __init__(
        self,
        transport: DQ03HistoryTransport,
        counters: RequestCounters,
        *,
        request_budget: int = 30,
        point_budget: int = 650,
        snapshot_time_utc_offset_hours: int | None = None,
    ) -> None:
        if request_budget < 1 or point_budget < 1:
            raise ValueError("history budgets must be positive")
        if (
            snapshot_time_utc_offset_hours is not None
            and not -14 <= snapshot_time_utc_offset_hours <= 14
        ):
            raise ValueError("IG session timezone offset must be between -14 and 14 hours")
        self._transport = transport
        self._counters = counters
        self._request_budget = request_budget
        self._point_budget = point_budget
        self._snapshot_time_utc_offset_hours = snapshot_time_utc_offset_hours
        self._cache: dict[tuple[str, str, int], BrokerValidationSample] = {}

    def validate_verified(
        self,
        resolutions: tuple[DQ03Resolution, ...],
        *,
        resolution: str = "MINUTE_5",
        points: int = 20,
    ) -> tuple[tuple[DQ03Resolution, ...], tuple[BrokerValidationSample, ...]]:
        """Acquire one economical sample per verified symbol within a fixed quota."""

        if not 2 <= points <= 200:
            raise ValueError("DQ-03 validation sample must be between 2 and 200 points")
        updated: list[DQ03Resolution] = []
        samples: list[BrokerValidationSample] = []
        for item in resolutions:
            if item.classification is not DQ03Status.VERIFIED or not item.selected_epic:
                updated.append(item)
                continue
            sample = self._sample(item, resolution=resolution, points=points)
            samples.append(sample)
            status = (
                DataStatus.BROKER_VALIDATED
                if sample.status is DataStatus.BROKER_VALIDATED
                else DataStatus.DATA_QUALITY_FAIL
            )
            updated.append(item.with_broker_validation(status, sample.source_fingerprint))
        return tuple(updated), tuple(samples)

    def _sample(
        self, item: DQ03Resolution, *, resolution: str, points: int
    ) -> BrokerValidationSample:
        assert item.selected_epic is not None
        key = (item.selected_epic, resolution, points)
        if key in self._cache:
            return self._cache[key]
        if self._counters.history_request_count >= self._request_budget:
            return _failed_sample(
                item, resolution, points, "DQ-03 history request budget exhausted"
            )
        if self._counters.history_points_consumed + points > self._point_budget:
            return _failed_sample(item, resolution, points, "DQ-03 history point budget exhausted")
        try:
            document = self._transport.get_historical_prices(item.selected_epic, resolution, points)
        except RuntimeError as error:
            return _failed_sample(item, resolution, points, str(error)[:180])
        self._counters.history_request_count += 1
        values = document.get("prices")
        if not isinstance(values, list) or not values:
            return _failed_sample(
                item, resolution, points, "IG history response contains no prices"
            )
        self._counters.history_points_consumed += len(values)

        rows: list[BrokerValidationRow] = []
        invalid_timestamp_rows = 0
        invalid_ohlc_rows = 0
        invalid_rows = 0
        for value in values:
            row, failures = _normalize_row(
                value,
                snapshot_time_utc_offset_hours=self._snapshot_time_utc_offset_hours,
            )
            if "timestamp" in failures:
                invalid_timestamp_rows += 1
            if "ohlc" in failures:
                invalid_ohlc_rows += 1
            if failures:
                invalid_rows += 1
            if row is not None:
                rows.append(row)

        timestamps = [row.timestamp_utc for row in rows]
        duplicate_count = len(timestamps) - len(set(timestamps))
        monotonic = len(rows) == len(values) and all(
            earlier < later for earlier, later in zip(timestamps, timestamps[1:], strict=False)
        )
        resolution_ordering = monotonic and _resolution_ordering_valid(timestamps, resolution)
        timestamp_valid = invalid_timestamp_rows == 0
        ohlc_valid = invalid_ohlc_rows == 0
        failures: list[str] = []
        if len(values) != points:
            failures.append(f"returned {len(values)} points; expected {points}")
        if invalid_timestamp_rows:
            failures.append(f"{invalid_timestamp_rows} row(s) have an unnormalizable timestamp")
        if invalid_ohlc_rows:
            failures.append(f"{invalid_ohlc_rows} row(s) have invalid positive OHLC data")
        if duplicate_count:
            failures.append(f"{duplicate_count} duplicate timestamp(s)")
        if not monotonic:
            failures.append("timestamps are not strictly increasing")
        if monotonic and not resolution_ordering:
            failures.append(f"timestamps do not follow {resolution} ordering")
        status = DataStatus.BROKER_VALIDATED if not failures else DataStatus.DATA_QUALITY_FAIL
        fingerprint = (
            _fingerprint(
                {
                    "symbol": item.symbol,
                    "epic": item.selected_epic,
                    "resolution": resolution,
                    "rows": [row.document() for row in rows],
                }
            )
            if status is DataStatus.BROKER_VALIDATED
            else None
        )
        parser_counts = Counter(row.timestamp_parser for row in rows)
        sample = BrokerValidationSample(
            item.symbol,
            item.selected_epic,
            resolution,
            points,
            len(values),
            timestamp_valid,
            ohlc_valid,
            sum(row.close_spread is not None for row in rows),
            fingerprint,
            status,
            "; ".join(failures) if failures else None,
            tuple(rows),
            _document_timestamp(rows[0].timestamp_utc) if rows else None,
            _document_timestamp(rows[-1].timestamp_utc) if rows else None,
            duplicate_count,
            invalid_rows,
            monotonic,
            resolution_ordering,
            tuple(sorted(parser_counts.items())),
        )
        self._cache[key] = sample
        return sample


def _failed_sample(
    item: DQ03Resolution, resolution: str, points: int, reason: str
) -> BrokerValidationSample:
    assert item.selected_epic is not None
    return BrokerValidationSample(
        item.symbol,
        item.selected_epic,
        resolution,
        points,
        0,
        False,
        False,
        0,
        None,
        DataStatus.DATA_QUALITY_FAIL,
        reason,
    )


def _normalize_row(
    value: object, *, snapshot_time_utc_offset_hours: int | None
) -> tuple[BrokerValidationRow | None, tuple[str, ...]]:
    if not isinstance(value, Mapping):
        return None, ("timestamp", "ohlc")
    timestamp = _timestamp(value, snapshot_time_utc_offset_hours=snapshot_time_utc_offset_hours)
    blocks = tuple(
        _price(value.get(name)) for name in ("openPrice", "highPrice", "lowPrice", "closePrice")
    )
    failures: list[str] = []
    if timestamp is None:
        failures.append("timestamp")
    if any(block is None for block in blocks):
        failures.append("ohlc")
    if timestamp is None or len(blocks) != 4 or any(block is None for block in blocks):
        return None, tuple(failures)
    open_price, high_price, low_price, close_price = blocks
    assert (
        open_price is not None
        and high_price is not None
        and low_price is not None
        and close_price is not None
    )
    if not _valid_geometry(open_price.mid, high_price.mid, low_price.mid, close_price.mid):
        return None, tuple((*failures, "ohlc"))
    close_spread = (
        close_price.ask_or_offer - close_price.bid
        if close_price.bid is not None and close_price.ask_or_offer is not None
        else None
    )
    return (
        BrokerValidationRow(
            timestamp.value,
            timestamp.source_field,
            timestamp.parser,
            open_price.mid,
            high_price.mid,
            low_price.mid,
            close_price.mid,
            close_price.bid,
            close_price.ask_or_offer,
            close_price.side_field,
            close_spread,
            open_price.source,
            high_price.source,
            low_price.source,
            close_price.source,
        ),
        tuple(failures),
    )


def _timestamp(
    row: Mapping[str, object], *, snapshot_time_utc_offset_hours: int | None
) -> _Timestamp | None:
    """Normalize only explicit UTC or broker-declared local-time evidence."""

    value = row.get("snapshotTimeUTC")
    if isinstance(value, str) and value.strip():
        parsed = _parse_utc_field(value)
        return _Timestamp(parsed[0], "snapshotTimeUTC", parsed[1]) if parsed is not None else None
    value = row.get("snapshotTime")
    if not isinstance(value, str) or not value.strip():
        return None
    parsed = _parse_local_timestamp(value, snapshot_time_utc_offset_hours)
    if parsed is None:
        return None
    return _Timestamp(parsed[0], "snapshotTime", parsed[1])


def _parse_utc_field(value: str) -> tuple[datetime, str] | None:
    """The IG `snapshotTimeUTC` field permits naive text because its field is UTC."""

    text = value.strip()
    formats = (
        ("%Y-%m-%dT%H:%M:%SZ", "snapshotTimeUTC_iso8601_z"),
        ("%Y-%m-%dT%H:%M:%S", "snapshotTimeUTC_iso8601_naive_utc"),
        ("%Y/%m/%d %H:%M:%S", "snapshotTimeUTC_slash_naive_utc"),
        ("%Y-%m-%d %H:%M:%S", "snapshotTimeUTC_space_naive_utc"),
    )
    for layout, parser in formats:
        try:
            return datetime.strptime(text, layout).replace(tzinfo=UTC), parser
        except ValueError:
            continue
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC), "snapshotTimeUTC_iso8601_naive_utc"
    if parsed.utcoffset() is None:
        return None
    return parsed.astimezone(UTC), "snapshotTimeUTC_iso8601_explicit_offset"


def _parse_local_timestamp(
    value: str, session_timezone_offset_hours: int | None
) -> tuple[datetime, str] | None:
    """Normalize IG local time only from explicit or same-session broker offset evidence."""

    text = value.strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        parsed = None
    if parsed is not None and parsed.tzinfo is not None and parsed.utcoffset() is not None:
        return parsed.astimezone(UTC), "snapshotTime_iso8601_explicit_offset"
    if session_timezone_offset_hours is None:
        return None
    for layout, parser in (
        ("%Y/%m/%d %H:%M:%S", "snapshotTime_slash_session_offset"),
        ("%Y-%m-%d %H:%M:%S", "snapshotTime_space_session_offset"),
        ("%Y-%m-%dT%H:%M:%S", "snapshotTime_iso8601_session_offset"),
    ):
        try:
            local = datetime.strptime(text, layout)
        except ValueError:
            continue
        utc = local.replace(tzinfo=UTC) - timedelta(hours=session_timezone_offset_hours)
        return utc, f"{parser}_hours_{session_timezone_offset_hours:+d}"
    return None


def _price(value: object) -> _Price | None:
    if not isinstance(value, Mapping):
        return None
    bid = _positive_decimal(value.get("bid"))
    for side_field in ("ask", "offer"):
        side = _positive_decimal(value.get(side_field))
        if bid is not None and side is not None and side >= bid:
            return _Price((bid + side) / Decimal("2"), bid, side, side_field, f"bid_{side_field}")
    last = _positive_decimal(value.get("lastTraded"))
    if last is not None:
        return _Price(last, None, None, None, "lastTraded_fallback")
    return None


def _valid_geometry(open_price: Decimal, high: Decimal, low: Decimal, close: Decimal) -> bool:
    return (
        all(value > 0 for value in (open_price, high, low, close))
        and low <= open_price <= high
        and low <= close <= high
        and high >= low
    )


def _resolution_ordering_valid(timestamps: list[datetime], resolution: str) -> bool:
    """Require 5-minute alignment without demanding continuity across closed sessions."""

    if resolution != "MINUTE_5":
        return True
    step = timedelta(minutes=5)
    return all(
        (later - earlier) % step == timedelta()
        for earlier, later in zip(timestamps, timestamps[1:], strict=False)
    )


def _positive_decimal(value: object) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return parsed if parsed > 0 else None


def _document_timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


def _fingerprint(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
