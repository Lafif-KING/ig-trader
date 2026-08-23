"""One controlled IG Demo Lightstreamer price session for the local worker.

This is intentionally outside the dashboard process.  It accepts a streaming
endpoint only after the REST Demo endpoint and expected Demo account have been
verified by the controller.  New entries must treat a stale quote as a veto.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Protocol
from urllib.parse import urlsplit

from lightstreamer.client import LightstreamerClient, Subscription


class DemoStreamingError(RuntimeError):
    """A single-session Demo streaming invariant could not be proven."""


class StreamSessionTokens(Protocol):
    cst: str | None
    x_security_token: str | None


@dataclass(frozen=True)
class DemoPriceQuote:
    epic: str
    bid: Decimal
    offer: Decimal
    observed_at: datetime


class DemoPriceStream:
    """Own exactly one client and bounded subscriptions for a local worker."""

    MAX_SUBSCRIPTIONS = 40

    def __init__(
        self,
        *,
        endpoint: str,
        api_key: str,
        session: StreamSessionTokens,
        rest_demo_proven: bool,
        client_factory: Callable[[str | None, str], Any] = LightstreamerClient,
        subscription_factory: Callable[[str, list[str], list[str]], Any] = Subscription,
    ) -> None:
        if not rest_demo_proven:
            raise DemoStreamingError("Demo REST identity must be proven before streaming")
        parsed = urlsplit(endpoint)
        if parsed.scheme != "https" or not parsed.hostname or parsed.query or parsed.fragment:
            raise DemoStreamingError("Lightstreamer endpoint is invalid")
        if not isinstance(api_key, str) or not api_key:
            raise DemoStreamingError("Demo streaming API key is unavailable")
        if not session.cst or not session.x_security_token:
            raise DemoStreamingError("Demo streaming session tokens are unavailable")
        self._client = client_factory(None, endpoint)
        self._client.connectionDetails.setUser(api_key)
        self._client.connectionDetails.setPassword(
            f"CST-{session.cst}|XST-{session.x_security_token}"
        )
        self._subscription_factory = subscription_factory
        self._quotes: dict[str, DemoPriceQuote] = {}
        self._subscribed_epics: frozenset[str] = frozenset()
        self._connected = False

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def subscribed_epics(self) -> frozenset[str]:
        return self._subscribed_epics

    def connect(self) -> None:
        self._client.connect()
        self._connected = True

    def disconnect(self) -> None:
        self._client.disconnect()
        self._connected = False

    def subscribe_prices(self, epics: Iterable[str]) -> None:
        if not self._connected:
            raise DemoStreamingError("Lightstreamer is not connected")
        normalized = frozenset(item for item in epics if isinstance(item, str) and item)
        if not normalized or len(normalized) > self.MAX_SUBSCRIPTIONS:
            raise DemoStreamingError("Demo streaming subscription count is invalid")
        if self._subscribed_epics:
            raise DemoStreamingError("a Demo worker must not create another price subscription set")
        subscription = self._subscription_factory(
            "MERGE", [f"L1:{epic}" for epic in sorted(normalized)], ["BID", "OFFER"]
        )
        subscription.addListener(_PriceListener(self._quotes))
        self._client.subscribe(subscription)
        self._subscribed_epics = normalized

    def quote(self, epic: str, *, maximum_age: timedelta) -> DemoPriceQuote | None:
        value = self._quotes.get(epic)
        if value is None or datetime.now(UTC) - value.observed_at > maximum_age:
            return None
        return value


class _PriceListener:
    def __init__(self, quotes: dict[str, DemoPriceQuote]) -> None:
        self._quotes = quotes

    def onItemUpdate(self, update: Any) -> None:  # noqa: N802 - Lightstreamer callback API
        item_name = update.getItemName()
        if not isinstance(item_name, str) or not item_name.startswith("L1:"):
            return
        try:
            bid = Decimal(str(update.getValue("BID")))
            offer = Decimal(str(update.getValue("OFFER")))
        except (InvalidOperation, ValueError):
            return
        if bid <= 0 or offer <= 0 or bid > offer:
            return
        self._quotes[item_name.removeprefix("L1:")] = DemoPriceQuote(
            item_name.removeprefix("L1:"), bid, offer, datetime.now(UTC)
        )
