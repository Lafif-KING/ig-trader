"""Canonical, non-persisting IG Price-stream quote contract for Shadow01."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from math import isfinite

_SOURCE = "IG_PRICE_STREAM"
_QUALITY_VALID = "VALID_QUOTE"
_QUALITY_UNAVAILABLE = "UNAVAILABLE"
_QUALITY_STALE = "STALE"
_MILLISECONDS_THRESHOLD = 100_000_000_000
_MODERN_LOWER_BOUND = datetime(2000, 1, 1, tzinfo=UTC)
_FUTURE_SKEW_SECONDS = 300
_REASON_CODE = re.compile(r"SHADOW01_[A-Z0-9_]+\Z")


@dataclass(frozen=True)
class ShadowLiveQuote:
    """One canonical Shadow01 live quote sourced only from IG Price streaming.

    Values are retained only for a fresh, fully valid quote and are never
    included in safety diagnostics.  Invalid or stale updates retain the
    stable reason code rather than a partial price or source timestamp.
    """

    epic: str
    symbol: str
    bid: Decimal | None = field(repr=False)
    ask: Decimal | None = field(repr=False)
    timestamp_utc: datetime | None = field(repr=False)
    market_state: str | None
    source: str
    quality: str
    reason_codes: tuple[str, ...] = ()
    quote_age_seconds: float | None = None

    def __post_init__(self) -> None:
        if not _identifier(self.epic) or not _identifier(self.symbol):
            raise ValueError("Shadow live quote identity is invalid")
        if self.source != _SOURCE:
            raise ValueError("Shadow live quote source is invalid")
        if self.quality not in {_QUALITY_VALID, _QUALITY_UNAVAILABLE, _QUALITY_STALE}:
            raise ValueError("Shadow live quote quality is invalid")
        if self.market_state is not None and not _identifier(self.market_state):
            raise ValueError("Shadow live quote market state is invalid")
        if not isinstance(self.reason_codes, tuple) or not all(
            isinstance(code, str) and _REASON_CODE.fullmatch(code) is not None
            for code in self.reason_codes
        ):
            raise ValueError("Shadow live quote reasons are invalid")
        if self.quality == _QUALITY_VALID:
            if (
                not _valid_price(self.bid)
                or not _valid_price(self.ask)
                or self.ask < self.bid
                or not _valid_timestamp(self.timestamp_utc)
                or not _valid_age(self.quote_age_seconds)
                or self.reason_codes
            ):
                raise ValueError("Shadow live quote values are invalid")
            object.__setattr__(self, "timestamp_utc", self.timestamp_utc.astimezone(UTC))
            return
        if (
            self.bid is not None
            or self.ask is not None
            or self.timestamp_utc is not None
            or self.quote_age_seconds is not None
            or not self.reason_codes
        ):
            raise ValueError("Unavailable Shadow live quote retains data values")


def build_ig_price_stream_quote(
    *,
    epic: str,
    symbol: str,
    bid_value: object,
    ask_value: object,
    timestamp_milliseconds: object,
    market_state: object,
    observed_at: datetime,
    maximum_age_seconds: int,
) -> ShadowLiveQuote:
    """Validate IG ``BIDPRICE1``/``ASKPRICE1``/``TIMESTAMP`` as one quote.

    The Price subscription timestamp contract is deliberately separate from
    REST V4's ``updateTimestampUTC`` parser: IG Price streaming supplies UTC
    milliseconds only.
    """

    normalized_state = _market_state(market_state)
    bid = _positive_decimal(bid_value)
    if bid is None:
        return _unavailable(epic, symbol, normalized_state, "SHADOW01_LIVE_QUOTE_BID_UNAVAILABLE")
    ask = _positive_decimal(ask_value)
    if ask is None:
        return _unavailable(epic, symbol, normalized_state, "SHADOW01_LIVE_QUOTE_ASK_UNAVAILABLE")
    if ask < bid:
        return _unavailable(epic, symbol, normalized_state, "SHADOW01_LIVE_QUOTE_SPREAD_INVALID")

    timestamp_status, timestamp, age_seconds = _stream_timestamp(
        timestamp_milliseconds,
        observed_at,
        maximum_age_seconds,
    )
    if timestamp_status == "FRESH" and timestamp is not None and age_seconds is not None:
        return ShadowLiveQuote(
            epic=epic,
            symbol=symbol,
            bid=bid,
            ask=ask,
            timestamp_utc=timestamp,
            market_state=normalized_state,
            source=_SOURCE,
            quality=_QUALITY_VALID,
            quote_age_seconds=age_seconds,
        )
    if timestamp_status == "STALE":
        return _unavailable(epic, symbol, normalized_state, "SHADOW01_LIVE_QUOTE_STALE", stale=True)
    return _unavailable(epic, symbol, normalized_state, _timestamp_reason(timestamp_status))


def stream_timestamp_status(
    value: object,
    observed_at: datetime,
    maximum_age_seconds: int,
) -> str:
    """Return a value-safe classification for the IG Price-stream timestamp."""

    return _stream_timestamp(value, observed_at, maximum_age_seconds)[0]


def _stream_timestamp(
    value: object,
    observed_at: datetime,
    maximum_age_seconds: int,
) -> tuple[str, datetime | None, float | None]:
    if not _valid_timestamp(observed_at) or (
        isinstance(maximum_age_seconds, bool)
        or not isinstance(maximum_age_seconds, int)
        or maximum_age_seconds < 1
    ):
        return "INVALID", None, None
    if value is None:
        return "MISSING", None, None
    if isinstance(value, bool):
        return "SCHEMA_UNSUPPORTED", None, None
    if isinstance(value, str):
        # Gate 09's one bounded Demo callback proof established this exact
        # Lightstreamer representation: ASCII decimal milliseconds only.
        if not value.isascii() or not value.isdigit():
            return "SCHEMA_UNSUPPORTED", None, None
    elif not isinstance(value, (int, float)):
        return "SCHEMA_UNSUPPORTED", None, None
    try:
        milliseconds = float(value)
    except (OverflowError, TypeError, ValueError):
        return "INVALID", None, None
    if not isfinite(milliseconds) or milliseconds <= 0:
        return "INVALID", None, None
    if milliseconds < _MILLISECONDS_THRESHOLD:
        return "SCHEMA_UNSUPPORTED", None, None
    try:
        timestamp = datetime.fromtimestamp(milliseconds / 1000, UTC)
    except (OverflowError, OSError, ValueError):
        return "INVALID", None, None
    if timestamp < _MODERN_LOWER_BOUND:
        return "INVALID", None, None
    age_seconds = (observed_at.astimezone(UTC) - timestamp).total_seconds()
    if age_seconds < -_FUTURE_SKEW_SECONDS:
        return "INVALID", None, None
    if age_seconds > maximum_age_seconds:
        return "STALE", None, None
    return "FRESH", timestamp, max(0.0, age_seconds)


def _timestamp_reason(status: str) -> str:
    if status == "MISSING":
        return "SHADOW01_STREAM_TIMESTAMP_MISSING"
    if status == "SCHEMA_UNSUPPORTED":
        return "SHADOW01_STREAM_TIMESTAMP_SCHEMA_UNSUPPORTED"
    return "SHADOW01_STREAM_TIMESTAMP_INVALID"


def _unavailable(
    epic: str,
    symbol: str,
    market_state: str | None,
    reason_code: str,
    *,
    stale: bool = False,
) -> ShadowLiveQuote:
    return ShadowLiveQuote(
        epic=epic,
        symbol=symbol,
        bid=None,
        ask=None,
        timestamp_utc=None,
        market_state=market_state,
        source=_SOURCE,
        quality=_QUALITY_STALE if stale else _QUALITY_UNAVAILABLE,
        reason_codes=(reason_code,),
    )


def _positive_decimal(value: object) -> Decimal | None:
    if isinstance(value, bool) or not isinstance(value, (Decimal, float, int, str)):
        return None
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return number if _valid_price(number) else None


def _valid_price(value: object) -> bool:
    return isinstance(value, Decimal) and value.is_finite() and value > 0


def _valid_timestamp(value: object) -> bool:
    return (
        isinstance(value, datetime) and value.tzinfo is not None and value.utcoffset() is not None
    )


def _valid_age(value: object) -> bool:
    return isinstance(value, float) and isfinite(value) and value >= 0


def _identifier(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip()) and value == value.strip()


def _market_state(value: object) -> str | None:
    return value.strip().upper() if isinstance(value, str) and value.strip() else None


__all__ = (
    "ShadowLiveQuote",
    "build_ig_price_stream_quote",
    "stream_timestamp_status",
)
