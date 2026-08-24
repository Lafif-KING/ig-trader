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

from lightstreamer.client import (
    ClientListener,
    LightstreamerClient,
    Subscription,
    SubscriptionListener,
)

from src.ig_trader.http_client import build_system_ssl_context

_SYSTEM_TLS_CONFIGURED = False


class DemoStreamingError(RuntimeError):
    """A single-session Demo streaming invariant could not be proven."""


class StreamSessionTokens(Protocol):
    cst: str | None
    x_security_token: str | None
    account_id: str | None


@dataclass(frozen=True)
class DemoPriceQuote:
    epic: str
    bid: Decimal
    offer: Decimal
    observed_at: datetime


class DemoPriceStream:
    """Own exactly one client and bounded subscriptions for a local worker."""

    MAX_SUBSCRIPTIONS = 40
    PRICE_ITEM_PREFIX = "MARKET:"

    def __init__(
        self,
        *,
        endpoint: str,
        session: StreamSessionTokens,
        rest_demo_proven: bool,
        client_factory: Callable[[str, str | None], Any] = LightstreamerClient,
        subscription_factory: Callable[[str, list[str], list[str]], Any] = Subscription,
    ) -> None:
        if not rest_demo_proven:
            raise DemoStreamingError("Demo REST identity must be proven before streaming")
        parsed = urlsplit(endpoint)
        if parsed.scheme != "https" or not parsed.hostname or parsed.query or parsed.fragment:
            raise DemoStreamingError("Lightstreamer endpoint is invalid")
        if not session.account_id or not session.cst or not session.x_security_token:
            raise DemoStreamingError("Demo streaming session identity or tokens are unavailable")
        _configure_system_tls()
        # LightstreamerClient(serverAddress, adapterSet): the endpoint must be
        # configured as the server address before connect(), never as an adapter.
        self._client = client_factory(endpoint, None)
        self._client.connectionDetails.setUser(session.account_id)
        self._client.connectionDetails.setPassword(
            f"CST-{session.cst}|XST-{session.x_security_token}"
        )
        self._subscription_factory = subscription_factory
        self._quotes: dict[str, DemoPriceQuote] = {}
        self._subscribed_epics: frozenset[str] = frozenset()
        self._subscription_confirmed = False
        self._subscription_error: str | None = None
        self._connection_status = "NOT_CONNECTED"
        self._connection_error: str | None = None
        self._client.addListener(
            _ConnectionListener(self._record_connection_status, self._record_connection_error)
        )
        self._connected = False

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def subscribed_epics(self) -> frozenset[str]:
        return self._subscribed_epics

    @property
    def subscription_confirmed(self) -> bool:
        return self._subscription_confirmed

    @property
    def subscription_error(self) -> str | None:
        return self._subscription_error

    @property
    def connection_status(self) -> str:
        return self._connection_status

    @property
    def connection_error(self) -> str | None:
        return self._connection_error

    @property
    def connection_confirmed(self) -> bool:
        return self._connection_status.startswith("CONNECTED")

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
            "MERGE",
            [f"{self.PRICE_ITEM_PREFIX}{epic}" for epic in sorted(normalized)],
            ["BID", "OFFER"],
        )
        subscription.addListener(
            _PriceListener(
                self._quotes, self._confirm_subscription, self._record_subscription_error
            )
        )
        self._client.subscribe(subscription)
        self._subscribed_epics = normalized

    def _confirm_subscription(self) -> None:
        self._subscription_confirmed = True

    def _record_subscription_error(self, code: object) -> None:
        self._subscription_error = str(code)[:40]

    def _record_connection_status(self, status: object) -> None:
        self._connection_status = str(status)[:80]

    def _record_connection_error(self, code: object) -> None:
        self._connection_error = str(code)[:40]

    def quote(self, epic: str, *, maximum_age: timedelta) -> DemoPriceQuote | None:
        value = self._quotes.get(epic)
        if value is None or datetime.now(UTC) - value.observed_at > maximum_age:
            return None
        return value


class _PriceListener(SubscriptionListener):
    """Implement the complete library listener contract, not just price updates."""

    def __init__(
        self,
        quotes: dict[str, DemoPriceQuote],
        confirm_subscription: Callable[[], None],
        record_subscription_error: Callable[[object], None],
    ) -> None:
        self._quotes = quotes
        self._confirm_subscription = confirm_subscription
        self._record_subscription_error = record_subscription_error

    def onSubscription(self) -> None:  # noqa: N802 - Lightstreamer callback API
        self._confirm_subscription()

    def onSubscriptionError(self, code: object, _message: object) -> None:  # noqa: N802
        self._record_subscription_error(code)

    def onItemUpdate(self, update: Any) -> None:  # noqa: N802 - Lightstreamer callback API
        item_name = update.getItemName()
        if not isinstance(item_name, str) or not item_name.startswith(
            DemoPriceStream.PRICE_ITEM_PREFIX
        ):
            return
        try:
            bid = Decimal(str(update.getValue("BID")))
            offer = Decimal(str(update.getValue("OFFER")))
        except (InvalidOperation, ValueError):
            return
        if bid <= 0 or offer <= 0 or bid > offer:
            return
        epic = item_name.removeprefix(DemoPriceStream.PRICE_ITEM_PREFIX)
        self._quotes[epic] = DemoPriceQuote(epic, bid, offer, datetime.now(UTC))


class _ConnectionListener(ClientListener):
    """Capture lifecycle evidence without retaining a server message or credential."""

    def __init__(
        self,
        record_status: Callable[[object], None],
        record_error: Callable[[object], None],
    ) -> None:
        self._record_status = record_status
        self._record_error = record_error

    def onStatusChange(self, status: object) -> None:  # noqa: N802 - Lightstreamer callback API
        self._record_status(status)

    def onServerError(self, code: object, _message: object) -> None:  # noqa: N802
        self._record_error(code)


def _configure_system_tls() -> None:
    """Use the same hostname-verifying Windows trust source as authenticated REST."""

    global _SYSTEM_TLS_CONFIGURED
    if _SYSTEM_TLS_CONFIGURED:
        return
    try:
        LightstreamerClient.setTrustManagerFactory(build_system_ssl_context())
    except Exception as error:
        raise DemoStreamingError("Lightstreamer system TLS could not be configured") from error
    _SYSTEM_TLS_CONFIGURED = True
