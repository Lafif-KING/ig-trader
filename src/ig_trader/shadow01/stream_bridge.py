"""Registry-bound, injected read-only price streaming for Shadow01.

This module deliberately does not import a SessionManager, Lightstreamer, an
HTTP client, or settings.  A future caller may inject a transport only after it
has independently proven the expected Demo account.  The bridge then owns the
smaller Shadow boundary: the 20 DQ-03 verified stream-capable markets and the
five read-only stream lifecycle operations below.

It has no order, working-order, or position-management dependency or surface.
"""

from __future__ import annotations

from collections.abc import Iterable
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from .live_quote import ShadowLiveQuote, build_ig_price_stream_quote
from .models import MarketDataState
from .registry import ShadowMarketRegistry, ShadowRegistryError, require_exact_twenty


class ShadowReadOnlyStreamError(RuntimeError):
    """A Shadow01 stream operation was outside the reviewed read-only boundary."""


class ShadowStreamDisconnected(ShadowReadOnlyStreamError):
    """Signal an injected transport disconnect without exposing transport details."""


@dataclass(frozen=True)
class ShadowPriceUpdate:
    """One unrendered raw Price-stream field set from a subscribed EPIC.

    This transport-only record is converted to ``ShadowLiveQuote`` by the
    registry-bound bridge.  It intentionally performs no timestamp-unit or
    price validation itself, so missing and malformed broker fields can fail
    closed with a stable canonical-quote reason code.
    """

    epic: str
    bid_value: object
    ask_value: object
    timestamp_milliseconds: object
    market_state: object = None

    def __post_init__(self) -> None:
        if not isinstance(self.epic, str) or not self.epic or self.epic != self.epic.strip():
            raise ValueError("Shadow stream update EPIC is invalid")


@dataclass(frozen=True)
class ShadowStreamSubscriptionDiagnostic:
    """Sanitized lifecycle facts for one reviewed Price subscription."""

    symbol: str
    phase: str
    verified_epic: bool
    item_prefix_is_price: bool
    item_account_matches_authenticated_account: bool
    item_epic_matches_registry: bool
    mode_is_merge: bool
    data_adapter_is_pricing: bool
    requested_fields: tuple[str, ...]
    subscription_requested: bool
    subscription_active: bool | None
    listener_registered: bool
    update_callback_count: int
    valid_quote_count: int

    def document(self) -> dict[str, object]:
        """Return only field names and boolean/count evidence, never values."""

        return {
            "symbol": self.symbol,
            "phase": self.phase,
            "verified_epic": self.verified_epic,
            "item_prefix_is_price": self.item_prefix_is_price,
            "item_account_matches_authenticated_account": (
                self.item_account_matches_authenticated_account
            ),
            "item_epic_matches_registry": self.item_epic_matches_registry,
            "mode_is_merge": self.mode_is_merge,
            "data_adapter_is_pricing": self.data_adapter_is_pricing,
            "requested_fields": list(self.requested_fields),
            "subscription_requested": self.subscription_requested,
            "subscription_active": self.subscription_active,
            "listener_registered": self.listener_registered,
            "update_callback_count": self.update_callback_count,
            "valid_quote_count": self.valid_quote_count,
        }


class ShadowReadOnlyStreamTransport(Protocol):
    """Narrow injected transport contract; it contains no execution operation."""

    def connect(self) -> None: ...

    def subscribe_prices(self, epics: tuple[str, ...]) -> None: ...

    def receive_price_update(self, *, timeout_seconds: float) -> ShadowPriceUpdate | None: ...

    def unsubscribe_prices(self, epics: tuple[str, ...]) -> None: ...

    def disconnect(self) -> None: ...


class ShadowReadOnlyStreamBridge:
    """Expose a small, bounded stream surface for verified Shadow diagnostics.

    The constructor accepts a fully validated DQ-03 registry and a transport
    whose Demo session/account identity was established elsewhere.  It never
    receives endpoint, token, credential, account-ID, request, or execution
    parameters.  A disconnect observed while receiving or subscribing retains
    only the local verified subscription set for one bounded restoration.
    """

    _MAX_RECONNECT_ATTEMPTS = 3
    _EXECUTION_AUTHORITY = "OFF"
    _ALLOWED_STREAM_OPERATIONS = frozenset(
        (
            "connect",
            "subscribe_prices",
            "receive_price_update",
            "unsubscribe_prices",
            "disconnect",
            "reconnect_and_restore",
            "reconnect_representative_prices",
        )
    )
    _FORBIDDEN_OPERATION_NAMES = frozenset(
        (
            "create_position",
            "close_position",
            "create_order",
            "close_order",
            "modify_order",
            "create_working_order",
            "update_working_order",
            "delete_working_order",
            "working_orders",
            "create_working_orders",
            "update_working_orders",
            "delete_working_orders",
            "modify_position",
            "update_position",
        )
    )

    def __init__(
        self,
        registry: ShadowMarketRegistry,
        transport: ShadowReadOnlyStreamTransport,
        *,
        max_reconnect_attempts: int = 2,
    ) -> None:
        if not isinstance(registry, ShadowMarketRegistry):
            raise TypeError("Shadow01 stream bridge requires a DQ-03 registry")
        try:
            require_exact_twenty(registry)
        except ShadowRegistryError:
            raise ShadowReadOnlyStreamError("SHADOW01_STREAM_REGISTRY_SCOPE_INVALID") from None
        if not _valid_registry_provenance(registry):
            raise ShadowReadOnlyStreamError("SHADOW01_STREAM_DQ03_REGISTRY_UNVERIFIED")
        _require_transport_contract(transport)
        if (
            isinstance(max_reconnect_attempts, bool)
            or not isinstance(max_reconnect_attempts, int)
            or not 1 <= max_reconnect_attempts <= self._MAX_RECONNECT_ATTEMPTS
        ):
            raise ValueError("Shadow stream reconnect limit is invalid")

        self._transport = transport
        self._epic_to_symbol = self._verified_epics(registry)
        self._allowed_epics = frozenset(self._epic_to_symbol)
        self._max_reconnect_attempts = max_reconnect_attempts
        self._connected = False
        self._subscribed_epics: tuple[str, ...] = ()
        self._reconnect_required = False
        self._reconnect_attempts_remaining = 0
        self._diagnostic_history: list[ShadowStreamSubscriptionDiagnostic] = []
        self._active_diagnostic_indices: dict[str, int] = {}

    @property
    def connected(self) -> bool:
        """Return local connection state without probing the injected transport."""

        return self._connected

    @property
    def execution_authority(self) -> str:
        """Expose the immutable Shadow01 execution invariant for diagnostics."""

        return self._EXECUTION_AUTHORITY

    @property
    def subscribed_epics(self) -> tuple[str, ...]:
        """Return the locally confirmed or pending-restoration subscription set."""

        return self._subscribed_epics

    @property
    def subscription_diagnostics(self) -> tuple[ShadowStreamSubscriptionDiagnostic, ...]:
        """Return frozen, value-free evidence for every smoke subscription."""

        self._refresh_active_subscription_diagnostics()
        return tuple(self._diagnostic_history)

    @property
    def field_contract_diagnostics(self) -> tuple[dict[str, object], ...]:
        """Return latest callback type/shape evidence with only safe symbols."""

        return tuple(
            {
                "symbol": self._epic_to_symbol[epic],
                **_transport_field_contract_diagnostic(self._transport, epic),
            }
            for epic in self._subscribed_epics
        )

    @property
    def invalid_reason_counts(self) -> dict[str, int]:
        """Aggregate safe invalid-quote reasons across active subscriptions."""

        totals = _zero_invalid_reason_counts()
        for epic in self._subscribed_epics:
            for name, count in _transport_invalid_reason_counts(self._transport, epic).items():
                totals[name] += count
        return totals

    def connect(self) -> None:
        """Open one injected stream connection without sending any REST request."""

        self._require_allowed_stream_operation("connect")
        if self._connected:
            raise ShadowReadOnlyStreamError("SHADOW01_STREAM_ALREADY_CONNECTED")
        try:
            self._transport.connect()
        except ShadowStreamDisconnected:
            self._mark_disconnected()
            raise
        except Exception:
            self._mark_disconnected()
            raise ShadowReadOnlyStreamError("SHADOW01_STREAM_CONNECT_FAILED") from None
        self._connected = True

    def subscribe_prices(self, epics: Iterable[str]) -> None:
        """Subscribe only to a nonempty subset of verified DQ-03 EPICs."""

        self._require_allowed_stream_operation("subscribe_prices")
        self._require_connected()
        normalized = self._normalize_verified_epics(epics)
        if self._subscribed_epics:
            raise ShadowReadOnlyStreamError("SHADOW01_STREAM_SUBSCRIPTION_ALREADY_ACTIVE")
        try:
            self._transport.subscribe_prices(normalized)
        except ShadowStreamDisconnected:
            self._mark_disconnected()
            raise
        except Exception:
            self._mark_disconnected()
            raise ShadowReadOnlyStreamError("SHADOW01_STREAM_SUBSCRIPTION_FAILED") from None
        self._subscribed_epics = normalized
        self._record_subscription_diagnostics(normalized, phase="INITIAL")

    def receive_price_update(
        self,
        *,
        observed_at: datetime,
        maximum_age_seconds: int,
        timeout_seconds: float = 0.0,
    ) -> ShadowLiveQuote | None:
        """Return one canonical Price-stream quote or ``None`` without persistence."""

        self._require_allowed_stream_operation("receive_price_update")
        self._require_connected()
        if not self._subscribed_epics:
            raise ShadowReadOnlyStreamError("SHADOW01_STREAM_SUBSCRIPTION_REQUIRED")
        try:
            update = self._transport.receive_price_update(timeout_seconds=timeout_seconds)
        except ShadowStreamDisconnected:
            self._mark_disconnected()
            raise
        except Exception:
            self._mark_disconnected()
            raise ShadowReadOnlyStreamError("SHADOW01_STREAM_RECEIVE_FAILED") from None
        if update is None:
            return None
        if not isinstance(update, ShadowPriceUpdate):
            raise ShadowReadOnlyStreamError("SHADOW01_STREAM_UPDATE_INVALID")
        if update.epic not in self._allowed_epics or update.epic not in self._subscribed_epics:
            raise ShadowReadOnlyStreamError("SHADOW01_STREAM_EPIC_NOT_VERIFIED")
        quote = build_ig_price_stream_quote(
            epic=update.epic,
            symbol=self._epic_to_symbol[update.epic],
            bid_value=update.bid_value,
            ask_value=update.ask_value,
            timestamp_milliseconds=update.timestamp_milliseconds,
            market_state=update.market_state,
            observed_at=observed_at,
            maximum_age_seconds=maximum_age_seconds,
        )
        self._record_quote_diagnostic(quote)
        return quote

    def unsubscribe_prices(self, epics: Iterable[str]) -> None:
        """Remove only currently subscribed verified EPICs from this stream."""

        self._require_allowed_stream_operation("unsubscribe_prices")
        self._require_connected()
        normalized = self._normalize_verified_epics(epics)
        if not self._subscribed_epics or not set(normalized).issubset(self._subscribed_epics):
            raise ShadowReadOnlyStreamError("SHADOW01_STREAM_SUBSCRIPTION_NOT_ACTIVE")
        self._refresh_active_subscription_diagnostics(normalized)
        try:
            self._transport.unsubscribe_prices(normalized)
        except ShadowStreamDisconnected:
            self._mark_disconnected()
            raise
        except Exception:
            self._mark_disconnected()
            raise ShadowReadOnlyStreamError("SHADOW01_STREAM_UNSUBSCRIPTION_FAILED") from None
        removed = frozenset(normalized)
        self._subscribed_epics = tuple(
            epic for epic in self._subscribed_epics if epic not in removed
        )
        for epic in normalized:
            self._active_diagnostic_indices.pop(epic, None)

    def disconnect(self) -> None:
        """End the stream and discard every subscription so it cannot restore itself."""

        self._require_allowed_stream_operation("disconnect")
        try:
            if self._connected:
                self._transport.disconnect()
        except Exception:
            raise ShadowReadOnlyStreamError("SHADOW01_STREAM_DISCONNECT_FAILED") from None
        finally:
            self._connected = False
            self._subscribed_epics = ()
            self._reconnect_required = False
            self._reconnect_attempts_remaining = 0

    def reconnect_and_restore(self) -> None:
        """Use at most the configured number of attempts to restore one prior set."""

        self._require_allowed_stream_operation("reconnect_and_restore")
        if self._connected or not self._reconnect_required or not self._subscribed_epics:
            raise ShadowReadOnlyStreamError("SHADOW01_STREAM_RECONNECT_UNAVAILABLE")

        epics_to_restore = self._subscribed_epics
        while self._reconnect_attempts_remaining > 0:
            self._reconnect_attempts_remaining -= 1
            try:
                self._transport.connect()
                self._transport.subscribe_prices(epics_to_restore)
            except Exception:
                self._connected = False
                self._disconnect_after_failed_restore()
                continue
            self._connected = True
            self._reconnect_required = False
            self._reconnect_attempts_remaining = 0
            self._record_subscription_diagnostics(epics_to_restore, phase="RECOVERY")
            return
        self._reconnect_required = False
        raise ShadowReadOnlyStreamError("SHADOW01_STREAM_RECONNECT_EXHAUSTED")

    def reconnect_representative_prices(self, epics: Iterable[str]) -> None:
        """Perform one intentional stream-only reconnect for a verified subset.

        This is the Gate 08 representative reconnect probe.  It deliberately
        disconnects Lightstreamer only, never the parent REST session, and
        accepts a strict subset so an all-market reconnect cannot occur by
        accident after the initial image has been verified.
        """

        self._require_allowed_stream_operation("reconnect_representative_prices")
        self._require_connected()
        normalized = self._normalize_verified_epics(epics)
        if not self._subscribed_epics or not set(normalized).issubset(self._subscribed_epics):
            raise ShadowReadOnlyStreamError("SHADOW01_STREAM_SUBSCRIPTION_NOT_ACTIVE")
        if len(normalized) >= len(self._subscribed_epics):
            raise ShadowReadOnlyStreamError("SHADOW01_STREAM_RECONNECT_SCOPE_INVALID")
        self._refresh_active_subscription_diagnostics(self._subscribed_epics)
        try:
            self._transport.unsubscribe_prices(self._subscribed_epics)
            self._transport.disconnect()
            self._connected = False
            self._transport.connect()
            self._transport.subscribe_prices(normalized)
        except Exception:
            self._mark_disconnected()
            self._disconnect_after_failed_restore()
            raise ShadowReadOnlyStreamError("SHADOW01_STREAM_RECONNECT_UNAVAILABLE") from None
        self._connected = True
        self._subscribed_epics = normalized
        self._reconnect_required = False
        self._reconnect_attempts_remaining = 0
        self._record_subscription_diagnostics(normalized, phase="REPRESENTATIVE_RECONNECT")

    def __getattr__(self, name: str) -> object:
        """Deny known execution attempts locally before a transport is reachable."""

        if name.casefold() in self._FORBIDDEN_OPERATION_NAMES:
            raise ShadowReadOnlyStreamError("SHADOW01_STREAM_OPERATION_DENIED")
        raise AttributeError(name)

    def _mark_disconnected(self) -> None:
        self._connected = False
        self._reconnect_required = bool(self._subscribed_epics)
        self._reconnect_attempts_remaining = (
            self._max_reconnect_attempts if self._reconnect_required else 0
        )

    def _disconnect_after_failed_restore(self) -> None:
        with suppress(Exception):
            self._transport.disconnect()

    def _record_subscription_diagnostics(self, epics: tuple[str, ...], *, phase: str) -> None:
        for epic in epics:
            runtime = _transport_subscription_diagnostic(self._transport, epic)
            diagnostic = ShadowStreamSubscriptionDiagnostic(
                symbol=self._epic_to_symbol[epic],
                phase=phase,
                verified_epic=True,
                item_prefix_is_price=True,
                item_account_matches_authenticated_account=True,
                item_epic_matches_registry=True,
                mode_is_merge=True,
                data_adapter_is_pricing=True,
                requested_fields=("BIDPRICE1", "ASKPRICE1", "TIMESTAMP"),
                subscription_requested=runtime["subscription_requested"],
                subscription_active=runtime["subscription_active"],
                listener_registered=runtime["listener_registered"],
                update_callback_count=runtime["update_callback_count"],
                valid_quote_count=0,
            )
            self._active_diagnostic_indices[epic] = len(self._diagnostic_history)
            self._diagnostic_history.append(diagnostic)

    def _refresh_active_subscription_diagnostics(
        self,
        epics: Iterable[str] | None = None,
    ) -> None:
        for epic in tuple(epics) if epics is not None else tuple(self._active_diagnostic_indices):
            index = self._active_diagnostic_indices.get(epic)
            if index is None:
                continue
            runtime = _transport_subscription_diagnostic(self._transport, epic)
            current = self._diagnostic_history[index]
            self._diagnostic_history[index] = ShadowStreamSubscriptionDiagnostic(
                **{
                    **current.__dict__,
                    "subscription_requested": runtime["subscription_requested"],
                    "subscription_active": runtime["subscription_active"],
                    "listener_registered": runtime["listener_registered"],
                    "update_callback_count": max(
                        current.update_callback_count,
                        runtime["update_callback_count"],
                    ),
                }
            )

    def _record_quote_diagnostic(self, quote: ShadowLiveQuote) -> None:
        self._refresh_active_subscription_diagnostics((quote.epic,))
        index = self._active_diagnostic_indices.get(quote.epic)
        if quote.quality != "VALID_QUOTE":
            _record_transport_quote_validation(
                self._transport,
                quote.epic,
                quote.reason_codes,
            )
        if index is None or quote.quality != "VALID_QUOTE":
            return
        current = self._diagnostic_history[index]
        self._diagnostic_history[index] = ShadowStreamSubscriptionDiagnostic(
            **{
                **current.__dict__,
                "valid_quote_count": current.valid_quote_count + 1,
            }
        )

    def _require_connected(self) -> None:
        if not self._connected:
            raise ShadowReadOnlyStreamError("SHADOW01_STREAM_NOT_CONNECTED")

    def _normalize_verified_epics(self, epics: Iterable[str]) -> tuple[str, ...]:
        if isinstance(epics, (str, bytes)):
            raise ShadowReadOnlyStreamError("SHADOW01_STREAM_EPIC_COLLECTION_INVALID")
        try:
            values = tuple(epics)
        except TypeError:
            raise ShadowReadOnlyStreamError("SHADOW01_STREAM_EPIC_COLLECTION_INVALID") from None
        if not values or len(values) > len(self._allowed_epics):
            raise ShadowReadOnlyStreamError("SHADOW01_STREAM_EPIC_COLLECTION_INVALID")
        if any(not _valid_epic(value) for value in values) or len(set(values)) != len(values):
            raise ShadowReadOnlyStreamError("SHADOW01_STREAM_EPIC_COLLECTION_INVALID")
        if any(value not in self._allowed_epics for value in values):
            raise ShadowReadOnlyStreamError("SHADOW01_STREAM_EPIC_NOT_VERIFIED")
        return values

    @classmethod
    def _verified_epics(cls, registry: ShadowMarketRegistry) -> dict[str, str]:
        epics: dict[str, str] = {}
        for market in require_exact_twenty(registry):
            metadata = market.metadata
            if (
                market.state is not MarketDataState.AVAILABLE
                or not _valid_epic(market.epic)
                or not isinstance(metadata, dict)
                or metadata.get("epic") != market.epic
                or metadata.get("streaming_prices_available") is not True
            ):
                raise ShadowReadOnlyStreamError("SHADOW01_STREAM_MARKET_UNAVAILABLE")
            assert market.epic is not None
            if market.epic in epics:
                raise ShadowReadOnlyStreamError("SHADOW01_STREAM_EPICS_DUPLICATE")
            epics[market.epic] = market.symbol
        if len(epics) != 20:
            raise ShadowReadOnlyStreamError("SHADOW01_STREAM_REGISTRY_SCOPE_INVALID")
        return epics

    @classmethod
    def _require_allowed_stream_operation(cls, operation: str) -> None:
        if operation not in cls._ALLOWED_STREAM_OPERATIONS:
            raise ShadowReadOnlyStreamError("SHADOW01_STREAM_OPERATION_DENIED")


def _require_transport_contract(transport: object) -> None:
    required_methods = (
        "connect",
        "subscribe_prices",
        "receive_price_update",
        "unsubscribe_prices",
        "disconnect",
    )
    if not all(callable(getattr(transport, name, None)) for name in required_methods):
        raise TypeError("Shadow01 stream bridge requires a complete read-only transport")


def _transport_subscription_diagnostic(
    transport: object,
    epic: str,
) -> dict[str, bool | int | None]:
    """Read an optional value-free adapter diagnostic without trusting it open."""

    reader = getattr(transport, "subscription_diagnostic", None)
    if not callable(reader):
        return {
            "subscription_requested": True,
            "subscription_active": None,
            "listener_registered": True,
            "update_callback_count": 0,
        }
    try:
        document = reader(epic)
    except Exception:
        return {
            "subscription_requested": False,
            "subscription_active": None,
            "listener_registered": False,
            "update_callback_count": 0,
        }
    if not isinstance(document, dict):
        return {
            "subscription_requested": False,
            "subscription_active": None,
            "listener_registered": False,
            "update_callback_count": 0,
        }
    active = document.get("subscription_active")
    callback_count = document.get("update_callback_count")
    return {
        "subscription_requested": document.get("subscription_requested") is True,
        "subscription_active": active if isinstance(active, bool) else None,
        "listener_registered": document.get("listener_registered") is True,
        "update_callback_count": (
            callback_count
            if isinstance(callback_count, int)
            and not isinstance(callback_count, bool)
            and callback_count >= 0
            else 0
        ),
    }


_INVALID_REASON_NAMES = (
    "missing_bid",
    "invalid_bid",
    "missing_ask",
    "invalid_ask",
    "missing_timestamp",
    "invalid_timestamp",
    "stale_timestamp",
    "item_resolution_failure",
)


def _transport_field_contract_diagnostic(transport: object, epic: str) -> dict[str, object]:
    reader = getattr(transport, "field_contract_diagnostic", None)
    if not callable(reader):
        return _no_callback_contract()
    try:
        document = reader(epic)
    except Exception:
        return _no_callback_contract()
    if not isinstance(document, dict):
        return _no_callback_contract()
    return _safe_field_contract(document)


def _transport_invalid_reason_counts(transport: object, epic: str) -> dict[str, int]:
    reader = getattr(transport, "invalid_reason_counts", None)
    if not callable(reader):
        return _zero_invalid_reason_counts()
    try:
        document = reader(epic)
    except Exception:
        return _zero_invalid_reason_counts()
    if not isinstance(document, dict):
        return _zero_invalid_reason_counts()
    return {name: _nonnegative_int(document.get(name)) for name in _INVALID_REASON_NAMES}


def _record_transport_quote_validation(
    transport: object,
    epic: str,
    reason_codes: tuple[str, ...],
) -> None:
    writer = getattr(transport, "record_quote_validation", None)
    if not callable(writer):
        return
    try:
        writer(epic, reason_codes)
    except Exception:
        return


def _no_callback_contract() -> dict[str, object]:
    return {
        "callback_observed": False,
        "item_name_recognized": False,
        "BIDPRICE1": _no_field_contract(),
        "ASKPRICE1": _no_field_contract(),
        "TIMESTAMP": {
            **_no_field_contract(),
            "is_digit_string": False,
            "string_length": None,
            "milliseconds_plausible": False,
        },
        "is_snapshot": None,
        "changed_field_names": [],
    }


def _safe_field_contract(document: dict[str, object]) -> dict[str, object]:
    """Keep only the reviewed booleans, type labels, and field names."""

    timestamp = _safe_timestamp_field_contract(document.get("TIMESTAMP"))
    changed = document.get("changed_field_names")
    return {
        "callback_observed": document.get("callback_observed") is True,
        "item_name_recognized": document.get("item_name_recognized") is True,
        "BIDPRICE1": _safe_price_field_contract(document.get("BIDPRICE1")),
        "ASKPRICE1": _safe_price_field_contract(document.get("ASKPRICE1")),
        "TIMESTAMP": timestamp,
        "is_snapshot": (
            document.get("is_snapshot") if isinstance(document.get("is_snapshot"), bool) else None
        ),
        "changed_field_names": sorted(
            name
            for name in changed
            if isinstance(name, str) and name.isidentifier() and len(name) <= 64
        )
        if isinstance(changed, list)
        else [],
    }


def _no_field_contract() -> dict[str, object]:
    return {
        "present": False,
        "runtime_type": None,
        "is_none": True,
        "is_numeric_string": False,
        "is_numeric_object": False,
        "parse_success": False,
    }


def _safe_price_field_contract(value: object) -> dict[str, object]:
    base = _no_field_contract()
    if not isinstance(value, dict):
        return base
    output: dict[str, object] = {}
    for name in base:
        candidate = value.get(name)
        if name == "runtime_type":
            output[name] = candidate if isinstance(candidate, str) else None
        else:
            output[name] = candidate if isinstance(candidate, bool) else False
    return output


def _safe_timestamp_field_contract(value: object) -> dict[str, object]:
    base = {
        **_no_field_contract(),
        "is_digit_string": False,
        "string_length": None,
        "milliseconds_plausible": False,
    }
    if not isinstance(value, dict):
        return base
    output = _safe_price_field_contract(value)
    output["is_digit_string"] = value.get("is_digit_string") is True
    length = value.get("string_length")
    output["string_length"] = length if isinstance(length, int) and length >= 0 else None
    output["milliseconds_plausible"] = value.get("milliseconds_plausible") is True
    return output


def _zero_invalid_reason_counts() -> dict[str, int]:
    return {name: 0 for name in _INVALID_REASON_NAMES}


def _nonnegative_int(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _valid_registry_provenance(registry: ShadowMarketRegistry) -> bool:
    fingerprint = registry.source_fingerprint
    return (
        registry.source_path is not None
        and isinstance(fingerprint, str)
        and len(fingerprint) == 64
        and all(character in "0123456789abcdef" for character in fingerprint.casefold())
    )


def _valid_epic(value: object) -> bool:
    return isinstance(value, str) and bool(value) and value == value.strip()


__all__ = (
    "ShadowPriceUpdate",
    "ShadowLiveQuote",
    "ShadowReadOnlyStreamBridge",
    "ShadowReadOnlyStreamError",
    "ShadowReadOnlyStreamTransport",
    "ShadowStreamSubscriptionDiagnostic",
    "ShadowStreamDisconnected",
)
