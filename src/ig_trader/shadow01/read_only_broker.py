"""Strict, observation-only broker boundary for the Shadow01 runner.

The adapter deliberately accepts only a tiny REST read surface and a supplied
price-subscription factory.  It owns no account state, order state, or broker
action API.  Every request is validated before the injected transport can see
it.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from .schedule_contract import (
    sanitize_v2_schedule_response_contract,
    sanitize_v3_schedule_response_contract,
)


class ReadOnlyBrokerError(RuntimeError):
    """A Shadow01 broker operation was outside the permitted read boundary."""


class AuthorizedRequestTransport(Protocol):
    """Minimal dependency required by the read-only REST adapter."""

    def authorized_request(self, method: str, endpoint: str, **kwargs: Any) -> Any: ...


StreamSubscriptionFactory = Callable[[tuple[str, ...]], Any]


@dataclass(frozen=True)
class ReadOnlyBrokerRequestCounters:
    """Immutable per-adapter accounting that cannot imply broker authority."""

    authentication_request_count: int = 0
    session_logout_count: int = 0
    session_read_count: int = 0
    account_read_count: int = 0
    market_catalog_read_count: int = 0
    market_read_count: int = 0
    schedule_metadata_read_count: int = 0
    historical_price_read_count: int = 0
    streaming_subscription_count: int = 0
    streaming_reconnection_count: int = 0
    blocked_request_count: int = 0
    create_count: int = 0
    close_count: int = 0
    working_orders_count: int = 0
    demo_starts_count: int = 0

    def __post_init__(self) -> None:
        """Reject any counter that would suggest an execution action occurred."""

        if (
            self.create_count,
            self.close_count,
            self.working_orders_count,
            self.demo_starts_count,
        ) != (0, 0, 0, 0):
            raise ValueError("Shadow01 execution counters must remain zero")

    @classmethod
    def zero(cls) -> ReadOnlyBrokerRequestCounters:
        """Return the safe snapshot used before any broker is constructed."""

        return cls()

    def execution_safety_document(self) -> dict[str, int]:
        """Return the Gate-01 counters with their required report labels."""

        return {
            "create": self.create_count,
            "close": self.close_count,
            "working_orders": self.working_orders_count,
            "demo_starts": self.demo_starts_count,
        }

    @property
    def total_rest_request_count(self) -> int:
        """Return all REST calls that reached the injected transport."""

        return (
            self.authentication_request_count
            + self.session_logout_count
            + self.session_read_count
            + self.account_read_count
            + self.market_catalog_read_count
            + self.market_read_count
            + self.schedule_metadata_read_count
            + self.historical_price_read_count
        )

    def document(self) -> dict[str, object]:
        """Return a JSON-safe, explicitly non-authorizing counter document."""

        return {
            "execution_authority": "OFF",
            "authentication_request_count": self.authentication_request_count,
            "session_logout_count": self.session_logout_count,
            "session_read_count": self.session_read_count,
            "account_read_count": self.account_read_count,
            "market_catalog_read_count": self.market_catalog_read_count,
            "market_read_count": self.market_read_count,
            "schedule_metadata_read_count": self.schedule_metadata_read_count,
            "historical_price_read_count": self.historical_price_read_count,
            "streaming_subscription_count": self.streaming_subscription_count,
            "streaming_reconnection_count": self.streaming_reconnection_count,
            "blocked_request_count": self.blocked_request_count,
            "total_rest_request_count": self.total_rest_request_count,
            "execution_safety_counters": self.execution_safety_document(),
        }


class Shadow01ReadOnlyBroker:
    """Allowlisted broker reads and injected price subscriptions only."""

    _EXECUTION_AUTHORITY = "OFF"
    _PATH_SEGMENT = re.compile(r"[A-Za-z0-9._-]+\Z")
    _MARKET_PATH = re.compile(r"/markets/[A-Za-z0-9._-]+\Z")
    _PRICE_PATH = re.compile(r"/prices/[A-Za-z0-9._-]+/[A-Za-z0-9._-]+/[1-9][0-9]*\Z")

    def __init__(
        self,
        transport: AuthorizedRequestTransport,
        *,
        stream_subscription_factory: StreamSubscriptionFactory | None = None,
        stream_reconnection_factory: StreamSubscriptionFactory | None = None,
    ) -> None:
        if not callable(getattr(transport, "authorized_request", None)):
            raise TypeError("Shadow01 requires an authorized-request transport")
        if stream_subscription_factory is not None and not callable(stream_subscription_factory):
            raise TypeError("Shadow01 stream subscription factory must be callable")
        if stream_reconnection_factory is not None and not callable(stream_reconnection_factory):
            raise TypeError("Shadow01 stream reconnection factory must be callable")
        self._transport = transport
        self._stream_subscription_factory = stream_subscription_factory
        self._stream_reconnection_factory = stream_reconnection_factory
        self._authentication_request_count = 0
        self._session_logout_count = 0
        self._session_read_count = 0
        self._account_read_count = 0
        self._market_catalog_read_count = 0
        self._market_read_count = 0
        self._schedule_metadata_read_count = 0
        self._historical_price_read_count = 0
        self._streaming_subscription_count = 0
        self._streaming_reconnection_count = 0
        self._blocked_request_count = 0
        self._internal_authentication_request_count = 0
        internal_authentication_observer_setter = getattr(
            self._transport,
            "set_internal_authentication_observer",
            None,
        )
        if callable(internal_authentication_observer_setter):
            internal_authentication_observer_setter(self._record_internal_authentication_request)

    @property
    def execution_authority(self) -> str:
        """Expose the invariant explicitly for runner evidence and tests."""

        return self._EXECUTION_AUTHORITY

    @property
    def request_counters(self) -> ReadOnlyBrokerRequestCounters:
        """Return a stable snapshot, never the mutable internal counters."""

        return ReadOnlyBrokerRequestCounters(
            authentication_request_count=(
                self._authentication_request_count + self._internal_authentication_request_count
            ),
            session_logout_count=self._session_logout_count,
            session_read_count=self._session_read_count,
            account_read_count=self._account_read_count,
            market_catalog_read_count=self._market_catalog_read_count,
            market_read_count=self._market_read_count,
            schedule_metadata_read_count=self._schedule_metadata_read_count,
            historical_price_read_count=self._historical_price_read_count,
            streaming_subscription_count=self._streaming_subscription_count,
            streaming_reconnection_count=self._streaming_reconnection_count,
            blocked_request_count=self._blocked_request_count,
        )

    def request_counters_document(self) -> dict[str, object]:
        """Return the request-counter snapshot in artifact-ready form."""

        return self.request_counters.document()

    def authenticate(self, **kwargs: Any) -> Any:
        """Request the sole permitted non-GET endpoint: session authentication."""

        return self._request("POST", "/session", **kwargs)

    def logout(self) -> bool:
        """Use only a transport's dedicated proven-session cleanup operation.

        No generic DELETE route is exposed or attempted.  Missing cleanup
        support blocks locally rather than reaching the authorized-request
        transport with a broader method/path.
        """

        logout = getattr(self._transport, "logout", None)
        if not callable(logout):
            self._deny("Shadow01 session logout is unavailable")
        try:
            completed = logout()
        except Exception as error:
            if str(error) == "SHADOW01_DEMO_ACCOUNT_MISMATCH":
                raise ReadOnlyBrokerError("SHADOW01_DEMO_ACCOUNT_MISMATCH") from None
            raise ReadOnlyBrokerError("SHADOW01_DEMO_SESSION_LOGOUT_FAILED") from None
        if completed is not True:
            raise ReadOnlyBrokerError("SHADOW01_DEMO_SESSION_LOGOUT_FAILED")
        self._session_logout_count += 1
        return True

    def account_state_is_valid(self, document: object) -> bool:
        """Return only reviewed account-read validity, never account facts."""

        validator = getattr(self._transport, "account_state_is_valid", None)
        if not callable(validator):
            self._deny("Shadow01 account-state validation is unavailable")
        try:
            return validator(document) is True
        except Exception as error:
            if str(error) == "SHADOW01_DEMO_ACCOUNT_MISMATCH":
                raise ReadOnlyBrokerError("SHADOW01_DEMO_ACCOUNT_MISMATCH") from None
            return False

    def latest_response_diagnostic(self) -> dict[str, int | str | None] | None:
        """Return only validated status/error-code evidence from the read transport."""

        reader = getattr(self._transport, "latest_response_diagnostic", None)
        if not callable(reader):
            return None
        try:
            diagnostic = reader()
        except Exception:
            return None
        if not isinstance(diagnostic, Mapping):
            return None
        status_code = diagnostic.get("status_code")
        upstream_error_code = diagnostic.get("upstream_error_code")
        if (
            isinstance(status_code, bool)
            or not isinstance(status_code, int)
            or not 400 <= status_code < 600
        ):
            return None
        if upstream_error_code is not None and (
            not isinstance(upstream_error_code, str)
            or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,159}", upstream_error_code) is None
        ):
            return None
        return {
            "status_code": status_code,
            "upstream_error_code": upstream_error_code,
        }

    @property
    def outbound_http_request_count(self) -> int | None:
        """Expose an observer-backed physical-attempt count when available.

        It is deliberately separate from logical broker counters so a failed
        logout or a transport retry can never be mistaken for zero traffic.
        """

        value = getattr(self._transport, "outbound_http_request_count", None)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            return None
        return value

    def consume_v3_schedule_response_contract(self) -> dict[str, object] | None:
        """Return then clear only the approved V3 response-shape evidence."""

        reader = getattr(self._transport, "consume_v3_schedule_response_contract", None)
        if not callable(reader):
            return None
        try:
            return sanitize_v3_schedule_response_contract(reader())
        except Exception:
            return None

    def consume_v2_schedule_response_contract(self) -> dict[str, object] | None:
        """Return then clear only the approved V2 response-shape evidence."""

        reader = getattr(self._transport, "consume_v2_schedule_response_contract", None)
        if not callable(reader):
            return None
        try:
            return sanitize_v2_schedule_response_contract(reader())
        except Exception:
            return None

    def stream_session_material(self) -> object:
        """Obtain a non-renderable stream handoff from the current REST session.

        The broker delegates only to a dedicated transport capability.  It
        never exposes session headers itself and this call cannot create or
        delete a REST session.
        """

        provider = getattr(self._transport, "stream_session_material", None)
        if not callable(provider):
            self._deny("Shadow01 stream session material is unavailable")
        try:
            return provider()
        except Exception as error:
            if str(error) == "SHADOW01_DEMO_ACCOUNT_MISMATCH":
                raise ReadOnlyBrokerError("SHADOW01_DEMO_ACCOUNT_MISMATCH") from None
            raise ReadOnlyBrokerError("SHADOW01_STREAM_SESSION_MATERIAL_UNAVAILABLE") from None

    def read_session(self) -> Any:
        """Read the authenticated session document."""

        return self._request("GET", "/session")

    def read_account(self) -> Any:
        """Read account documents without inferring an active account."""

        return self._request("GET", "/accounts")

    def read_market_catalog(self) -> Any:
        """Read the allowlisted market-catalog route without query parameters."""

        return self._request("GET", "/markets")

    def read_market(self, epic: str) -> Any:
        """Read one explicitly supplied, path-safe EPIC."""

        normalized = self._path_segment(epic, "EPIC")
        return self._request("GET", f"/markets/{normalized}")

    def read_market_schedule_v3(self, epic: str) -> Any:
        """Issue the sole isolated V3 read, for opening-hours parsing only.

        This cannot become a general V3 market API: the caller is the schedule
        bridge and the route, version, method, and counter are fixed here.
        """

        normalized = self._path_segment(epic, "EPIC")
        document = self._request("GET", f"/markets/{normalized}", api_version="3")
        return _v3_schedule_only_document(document)

    def read_market_schedule_v2(self, epic: str) -> Any:
        """Issue the sole isolated V2 read, for schedule proof only."""

        normalized = self._path_segment(epic, "EPIC")
        document = self._request("GET", f"/markets/{normalized}", api_version="2")
        return _v3_schedule_only_document(document)

    def read_markets(self, epics: Iterable[str]) -> Mapping[str, Any]:
        """Read each requested EPIC independently through the exact market route."""

        normalized = self._epics(epics)
        return {epic: self.read_market(epic) for epic in normalized}

    def read_historical_prices(self, epic: str, resolution: str, points: int) -> Any:
        """Read a bounded, explicitly addressed historical-price document."""

        normalized_epic = self._path_segment(epic, "EPIC")
        normalized_resolution = self._path_segment(resolution, "resolution")
        if isinstance(points, bool) or not isinstance(points, int) or points < 1:
            self._deny("Shadow01 historical point count is invalid")
        return self._request(
            "GET",
            f"/prices/{normalized_epic}/{normalized_resolution}/{points}",
        )

    def subscribe_prices(self, epics: Iterable[str]) -> Any:
        """Delegate a validated immutable EPIC set to the injected stream factory."""

        normalized = self._epics(epics)
        if self._stream_subscription_factory is None:
            self._deny("Shadow01 stream subscription factory is unavailable")
        self._streaming_subscription_count += 1
        return self._stream_subscription_factory(normalized)

    def reconnect_prices(self, epics: Iterable[str]) -> Any:
        """Reconnect a validated EPIC set through an injected stream factory only."""

        normalized = self._epics(epics)
        factory = self._stream_reconnection_factory or self._stream_subscription_factory
        if factory is None:
            self._deny("Shadow01 stream reconnection factory is unavailable")
        self._streaming_reconnection_count += 1
        return factory(normalized)

    def _request(self, method: object, endpoint: object, **kwargs: Any) -> Any:
        """Validate method, exact path, and read arguments before transport use."""

        if not isinstance(method, str) or not isinstance(endpoint, str):
            self._deny("Shadow01 broker method or endpoint is invalid")
        if not self._allowed(method, endpoint):
            self._deny("Shadow01 broker request is not allowlisted")
        if method == "GET" and any(name in kwargs for name in ("params", "data", "json")):
            self._deny("Shadow01 GET request arguments are not allowlisted")
        if kwargs.get("api_version") not in {None, "2", "3"}:
            self._deny("Shadow01 GET API version is not allowlisted")
        if (
            kwargs.get("api_version") in {"2", "3"}
            and self._MARKET_PATH.fullmatch(endpoint) is None
        ):
            self._deny("Shadow01 schedule route is not allowlisted")

        self._record_transport_request(method, endpoint, api_version=kwargs.get("api_version"))
        return self._transport.authorized_request(method, endpoint, **kwargs)

    def _allowed(self, method: str, endpoint: str) -> bool:
        return (method == "POST" and endpoint == "/session") or (
            method == "GET"
            and (
                endpoint in {"/session", "/accounts", "/markets"}
                or self._MARKET_PATH.fullmatch(endpoint) is not None
                or self._PRICE_PATH.fullmatch(endpoint) is not None
            )
        )

    def _record_transport_request(
        self, method: str, endpoint: str, *, api_version: object = None
    ) -> None:
        if method == "POST":
            self._authentication_request_count += 1
        elif endpoint == "/session":
            self._session_read_count += 1
        elif endpoint == "/accounts":
            self._account_read_count += 1
        elif endpoint == "/markets":
            self._market_catalog_read_count += 1
        elif api_version in {"2", "3"}:
            self._schedule_metadata_read_count += 1
        elif endpoint.startswith("/markets/"):
            self._market_read_count += 1
        else:
            self._historical_price_read_count += 1

    def _record_internal_authentication_request(self) -> None:
        """Include an observer-reported implicit SessionManager login exactly once."""

        self._internal_authentication_request_count += 1

    def _epics(self, epics: Iterable[str]) -> tuple[str, ...]:
        if isinstance(epics, (str, bytes)):
            self._deny("Shadow01 EPIC collection is invalid")
        try:
            values = tuple(epics)
        except TypeError:
            self._deny("Shadow01 EPIC collection is invalid")
        if not values:
            self._deny("Shadow01 EPIC collection is empty")
        normalized = tuple(self._path_segment(value, "EPIC") for value in values)
        if len(set(normalized)) != len(normalized):
            self._deny("Shadow01 EPIC collection contains duplicates")
        return normalized

    def _path_segment(self, value: object, name: str) -> str:
        if (
            not isinstance(value, str)
            or not value
            or value != value.strip()
            or self._PATH_SEGMENT.fullmatch(value) is None
        ):
            self._deny(f"Shadow01 {name} is invalid")
        return value

    def _deny(self, reason: str) -> None:
        self._blocked_request_count += 1
        raise ReadOnlyBrokerError(reason)


ReadOnlyBroker = Shadow01ReadOnlyBroker


def _v3_schedule_only_document(document: object) -> dict[str, object]:
    """Drop every V3 field except the declared schedule contract immediately."""

    if not isinstance(document, Mapping):
        return {}
    instrument = document.get("instrument")
    if not isinstance(instrument, Mapping):
        return {"instrument": {}}
    opening_hours = instrument.get("openingHours")
    if not isinstance(opening_hours, Mapping):
        return {"instrument": {}}
    market_times = opening_hours.get("marketTimes")
    if not isinstance(market_times, list):
        return {"instrument": {"openingHours": {}}}
    retained: list[dict[str, object]] = []
    for item in market_times:
        if isinstance(item, Mapping):
            retained.append({"openTime": item.get("openTime"), "closeTime": item.get("closeTime")})
        else:
            retained.append({})
    return {"instrument": {"openingHours": {"marketTimes": retained}}}


ShadowReadOnlyBrokerAdapter = Shadow01ReadOnlyBroker


__all__ = (
    "AuthorizedRequestTransport",
    "ReadOnlyBroker",
    "ReadOnlyBrokerError",
    "ReadOnlyBrokerRequestCounters",
    "Shadow01ReadOnlyBroker",
    "ShadowReadOnlyBrokerAdapter",
    "StreamSubscriptionFactory",
)
