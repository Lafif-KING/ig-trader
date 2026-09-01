"""Narrow, value-safe V3 market-schedule evidence for Shadow01 clock checks."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import time

_AVAILABLE = "DECLARED_HOURS_AVAILABLE"
_UNAVAILABLE = "DECLARED_HOURS_UNAVAILABLE"
_AMBIGUOUS = "DECLARED_HOURS_AMBIGUOUS"
_INVALID = "DECLARED_HOURS_INVALID"


@dataclass(frozen=True)
class ShadowMarketScheduleEvidence:
    """Only V3 opening-hours facts for one already V4-identified market.

    ``market_times`` is internal parsing state used by the clock classifier.
    The public document deliberately emits counts and types, not raw hours,
    broker bodies, prices, credentials, or account information.
    """

    symbol: str
    epic: str
    source_version: int
    opening_hours_available: bool
    market_times_available: bool
    open_time_type: str | None
    close_time_type: str | None
    hours_state: str
    market_times: tuple[tuple[time, time], ...] = ()

    def target_anchor_in_declared_operational_window(self, anchor_time: time) -> bool | None:
        """Return only a schedule-window truth value for the frozen local clock."""

        if self.hours_state != _AVAILABLE:
            return None
        return any(_contains_clock(interval, anchor_time) for interval in self.market_times)

    def document(self) -> dict[str, object]:
        """Return a diagnostics-safe schedule contract without raw market data."""

        return {
            "symbol": self.symbol,
            "epic_present": bool(self.epic),
            "source_version": self.source_version,
            "opening_hours_available": self.opening_hours_available,
            "market_times_available": self.market_times_available,
            "market_times_count": len(self.market_times),
            "open_time_type": self.open_time_type,
            "close_time_type": self.close_time_type,
            "hours_state": self.hours_state,
        }


def parse_v3_market_schedule(
    *, symbol: str, epic: str, document: object
) -> ShadowMarketScheduleEvidence:
    """Parse only ``instrument.openingHours.marketTimes`` from a V3 document."""

    base = _evidence(symbol=symbol, epic=epic)
    if not isinstance(document, Mapping):
        return base
    instrument = document.get("instrument")
    if not isinstance(instrument, Mapping):
        return base
    opening_hours = instrument.get("openingHours")
    if not isinstance(opening_hours, Mapping):
        return _evidence(symbol=symbol, epic=epic, opening_hours_available=False)
    market_times = opening_hours.get("marketTimes")
    if not isinstance(market_times, list) or not market_times:
        return _evidence(
            symbol=symbol,
            epic=epic,
            opening_hours_available=True,
            market_times_available=False,
        )
    parsed: list[tuple[time, time]] = []
    open_type: str | None = None
    close_type: str | None = None
    for value in market_times:
        if not isinstance(value, Mapping):
            return _evidence(
                symbol=symbol,
                epic=epic,
                opening_hours_available=True,
                market_times_available=True,
                open_time_type=open_type,
                close_time_type=close_type,
                hours_state=_INVALID,
            )
        open_value, close_value = value.get("openTime"), value.get("closeTime")
        open_type = _merge_type(open_type, _type_name(open_value))
        close_type = _merge_type(close_type, _type_name(close_value))
        opening, closing = _clock_time(open_value), _clock_time(close_value)
        if opening is None or closing is None:
            return _evidence(
                symbol=symbol,
                epic=epic,
                opening_hours_available=True,
                market_times_available=True,
                open_time_type=open_type,
                close_time_type=close_type,
                hours_state=_INVALID,
            )
        if opening == closing:
            return _evidence(
                symbol=symbol,
                epic=epic,
                opening_hours_available=True,
                market_times_available=True,
                open_time_type=open_type,
                close_time_type=close_type,
                hours_state=_AMBIGUOUS,
            )
        parsed.append((opening, closing))
    return _evidence(
        symbol=symbol,
        epic=epic,
        opening_hours_available=True,
        market_times_available=True,
        open_time_type=open_type,
        close_time_type=close_type,
        hours_state=_AVAILABLE,
        market_times=tuple(parsed),
    )


def _evidence(
    *,
    symbol: str,
    epic: str,
    opening_hours_available: bool = False,
    market_times_available: bool = False,
    open_time_type: str | None = None,
    close_time_type: str | None = None,
    hours_state: str = _UNAVAILABLE,
    market_times: tuple[tuple[time, time], ...] = (),
) -> ShadowMarketScheduleEvidence:
    return ShadowMarketScheduleEvidence(
        symbol=symbol,
        epic=epic,
        source_version=3,
        opening_hours_available=opening_hours_available,
        market_times_available=market_times_available,
        open_time_type=open_time_type,
        close_time_type=close_time_type,
        hours_state=hours_state,
        market_times=market_times,
    )


def _clock_time(value: object) -> time | None:
    """Accept only the documented V3 ``HH:MM`` or ``HH:MM:SS`` strings."""

    if not isinstance(value, str):
        return None
    parts = value.strip().split(":")
    if len(parts) not in {2, 3} or not all(part.isdigit() for part in parts):
        return None
    try:
        return time(int(parts[0]), int(parts[1]), int(parts[2]) if len(parts) == 3 else 0)
    except ValueError:
        return None


def _contains_clock(interval: tuple[time, time], anchor: time) -> bool:
    opening, closing = interval
    if opening < closing:
        return opening <= anchor < closing
    return anchor >= opening or anchor < closing


def _type_name(value: object) -> str:
    return type(value).__name__


def _merge_type(current: str | None, candidate: str) -> str:
    return candidate if current is None or current == candidate else "MIXED"


__all__ = ("ShadowMarketScheduleEvidence", "parse_v3_market_schedule")
