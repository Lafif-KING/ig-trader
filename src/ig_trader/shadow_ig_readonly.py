"""Read-only IG stream contracts; tests drive these without an IG connection."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum

from src.ig_trader.shadow_execution import MarketQuote


class StreamHealth(StrEnum):
    DISCONNECTED = "DISCONNECTED"
    CONNECTING = "CONNECTING"
    CONNECTED = "CONNECTED"
    BACKOFF = "BACKOFF"


@dataclass(frozen=True)
class StreamState:
    health: StreamHealth
    subscriptions: tuple[str, ...]
    reconnect_after: datetime | None
    quote: MarketQuote | None


class ShadowReadonlyStream:
    """Tracks one MARKET subscription and bounded reconnect intent, never orders."""

    def __init__(self, epic: str, *, max_backoff: timedelta = timedelta(seconds=30)) -> None:
        self.item = f"MARKET:{epic}"
        self._max_backoff = max_backoff
        self._attempts = 0
        self._state = StreamState(StreamHealth.DISCONNECTED, (), None, None)

    @property
    def state(self) -> StreamState:
        return self._state

    def connected(self) -> StreamState:
        self._attempts = 0
        self._state = StreamState(StreamHealth.CONNECTED, (self.item,), None, self._state.quote)
        return self._state

    def disconnected(self, *, now: datetime) -> StreamState:
        self._attempts += 1
        seconds = min(2 ** (self._attempts - 1), int(self._max_backoff.total_seconds()))
        self._state = StreamState(
            StreamHealth.BACKOFF,
            (),
            now.astimezone(UTC) + timedelta(seconds=seconds),
            self._state.quote,
        )
        return self._state

    def update_quote(self, *, bid: float, offer: float, as_of: datetime) -> StreamState:
        if self._state.health is not StreamHealth.CONNECTED:
            raise RuntimeError("read-only stream is not connected")
        self._state = StreamState(
            StreamHealth.CONNECTED,
            (self.item,),
            None,
            MarketQuote(bid, offer, as_of),
        )
        return self._state

    def fresh_quote(self, *, now: datetime, max_age: timedelta) -> MarketQuote | None:
        quote = self._state.quote
        if self._state.health is not StreamHealth.CONNECTED or quote is None:
            return None
        try:
            quote.validate(now, max_age)
        except Exception:
            return None
        return quote
