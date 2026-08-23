"""Bounded, read-only broker history validation for verified DQ-03 contracts."""

# ruff: noqa: E501

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Protocol

from src.ig_trader.dq03.models import DataStatus, DQ03Resolution, DQ03Status, RequestCounters


class DQ03HistoryTransport(Protocol):
    """The one read-only prices endpoint required for a validation sample."""

    def get_historical_prices(
        self, epic: str, resolution: str, points: int
    ) -> Mapping[str, object]: ...


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
    ) -> None:
        if request_budget < 1 or point_budget < 1:
            raise ValueError("history budgets must be positive")
        self._transport = transport
        self._counters = counters
        self._request_budget = request_budget
        self._point_budget = point_budget
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
        timestamps = [_timestamp(row) for row in values if isinstance(row, Mapping)]
        timestamp_valid = len(timestamps) == len(values) and all(
            value is not None for value in timestamps
        )
        ohlc_valid = all(_valid_ohlc(row) for row in values if isinstance(row, Mapping)) and len(
            values
        ) == len(timestamps)
        spreads = sum(_spread_available(row) for row in values if isinstance(row, Mapping))
        status = (
            DataStatus.BROKER_VALIDATED
            if timestamp_valid and ohlc_valid
            else DataStatus.DATA_QUALITY_FAIL
        )
        sample = BrokerValidationSample(
            item.symbol,
            item.selected_epic,
            resolution,
            points,
            len(values),
            timestamp_valid,
            ohlc_valid,
            spreads,
            _fingerprint(_sample_document(values))
            if status is DataStatus.BROKER_VALIDATED
            else None,
            status,
            None
            if status is DataStatus.BROKER_VALIDATED
            else "Timestamp or OHLC contract failed validation.",
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


def _timestamp(row: Mapping[str, object]) -> datetime | None:
    value = row.get("snapshotTimeUTC") or row.get("snapshotTime")
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)
    except ValueError:
        return None


def _valid_ohlc(row: Mapping[str, object]) -> bool:
    values = [_mid(row.get(name)) for name in ("openPrice", "highPrice", "lowPrice", "closePrice")]
    if any(value is None for value in values):
        return False
    open_price, high, low, close = values
    assert open_price is not None and high is not None and low is not None and close is not None
    return low <= min(open_price, close) and high >= max(open_price, close) and high >= low


def _spread_available(row: Mapping[str, object]) -> bool:
    close = row.get("closePrice")
    if not isinstance(close, Mapping):
        return False
    bid, offer = _decimal(close.get("bid")), _decimal(close.get("offer"))
    return bid is not None and offer is not None and offer >= bid


def _mid(value: object) -> Decimal | None:
    if not isinstance(value, Mapping):
        return None
    last = _decimal(value.get("lastTraded"))
    if last is not None:
        return last
    bid, offer = _decimal(value.get("bid")), _decimal(value.get("offer"))
    return (bid + offer) / Decimal("2") if bid is not None and offer is not None else None


def _decimal(value: object) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _sample_document(values: list[object]) -> list[dict[str, object]]:
    return [
        {
            "timestamp": str(row.get("snapshotTimeUTC") or row.get("snapshotTime")),
            "open": str(_mid(row.get("openPrice"))),
            "high": str(_mid(row.get("highPrice"))),
            "low": str(_mid(row.get("lowPrice"))),
            "close": str(_mid(row.get("closePrice"))),
            "spread_available": _spread_available(row),
        }
        for row in values
        if isinstance(row, Mapping)
    ]


def _fingerprint(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
