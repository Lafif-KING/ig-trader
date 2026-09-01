"""Lazy local-Demo stream construction for the isolated Shadow01 runner.

This module is intentionally a *factory*, rather than a SessionManager or
Lightstreamer wrapper created at import time.  Building a registry-bound
bridge is fully non-activating: it does not load settings, create an HTTP
client, authenticate, or create a streaming client.  The explicit ``status``
method may safely inspect settings only.  Only a later bridge ``connect``
action may activate a local Demo boundary.

The real path uses the existing ``SessionManagerReadOnlyTransport`` as the
sole REST boundary.  It can authenticate only through its reviewed exact
``POST /session`` allowance.  The stream side is deliberately implemented
here, rather than wrapping ``DemoPriceStream``: that older local worker does
not provide reviewed unsubscribe or receive-next-update lifecycle semantics.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable, Mapping
from contextlib import suppress
from decimal import Decimal, InvalidOperation
from math import isfinite
from numbers import Number
from threading import Condition
from typing import Any, Protocol
from urllib.parse import urlsplit

from .local_demo_read_only import (
    LocalDemoReadOnlyStatus,
    SessionManagerProtocol,
    SessionManagerReadOnlyTransport,
    Shadow01LocalDemoReadOnlyError,
    Shadow01LocalDemoReadOnlyFactory,
    ShadowStreamSessionMaterial,
)
from .registry import ShadowMarketRegistry
from .stream_bridge import (
    ShadowPriceUpdate,
    ShadowReadOnlyStreamBridge,
    ShadowStreamDisconnected,
)


class Shadow01LocalDemoReadOnlyStreamError(RuntimeError):
    """A local Demo stream condition could not be proven safely.

    Messages are stable reason codes only.  They never interpolate endpoint,
    account, credential, session, or server-error values.
    """


class _ReadOnlyPriceClient(Protocol):
    """The five stream lifecycle calls permitted beneath the Shadow bridge."""

    def connect(self) -> None: ...

    def subscribe_prices(self, epics: tuple[str, ...]) -> None: ...

    def receive_price_update(self, *, timeout_seconds: float) -> ShadowPriceUpdate | None: ...

    def unsubscribe_prices(self, epics: tuple[str, ...]) -> None: ...

    def disconnect(self) -> None: ...


SettingsProvider = Callable[[], object]
SessionFactory = Callable[[Callable[[str, str], None]], SessionManagerProtocol]
StreamClientFactory = Callable[[str, str, str, str], _ReadOnlyPriceClient]


class Shadow01LocalDemoReadOnlyStreamFactory:
    """Create a non-activating registry-bound local Demo stream bridge.

    The factory deliberately retains only injectable constructors.  It has no
    SessionManager, HTTP-client, token, endpoint, or Lightstreamer instance
    until the private transport's later ``connect`` call succeeds through the
    exact Demo-account guard.
    """

    _EXECUTION_AUTHORITY = "OFF"

    def __init__(
        self,
        *,
        settings_provider: SettingsProvider | None = None,
        session_factory: SessionFactory | None = None,
        stream_client_factory: StreamClientFactory | None = None,
    ) -> None:
        self._settings_provider = settings_provider or _load_settings_lazily
        self._session_factory = session_factory or _build_session_manager_lazily
        self._stream_client_factory = stream_client_factory or _build_lightstreamer_client_lazily

    @property
    def execution_authority(self) -> str:
        """Expose the permanent non-execution invariant for diagnostics."""

        return self._EXECUTION_AUTHORITY

    def status(self) -> LocalDemoReadOnlyStatus:
        """Return credential-safe readiness without activating any client."""

        try:
            settings = self._settings_provider()
        except Exception:
            return LocalDemoReadOnlyStatus(False, "SHADOW01_SETTINGS_UNAVAILABLE")
        return _status_for_settings(settings)

    def build(
        self,
        registry: ShadowMarketRegistry,
        *,
        session_material: ShadowStreamSessionMaterial,
        max_reconnect_attempts: int = 2,
    ) -> ShadowReadOnlyStreamBridge:
        """Build a bridge from the broker-owned authenticated REST session.

        The bridge still owns DQ-03 registry binding and its bounded recovery
        policy.  This factory cannot construct a SessionManager, authenticate,
        or log out the parent REST session.
        """

        if not isinstance(session_material, ShadowStreamSessionMaterial):
            raise Shadow01LocalDemoReadOnlyStreamError(
                "SHADOW01_STREAM_SESSION_MATERIAL_UNAVAILABLE"
            )
        transport = _SessionBoundLocalDemoReadOnlyStreamTransport(
            session_material=session_material,
            stream_client_factory=self._stream_client_factory,
        )
        return ShadowReadOnlyStreamBridge(
            registry,
            transport,
            max_reconnect_attempts=max_reconnect_attempts,
        )


class _LazyLocalDemoReadOnlyStreamTransport:
    """Retired pre-Gate-08 transport; construction is permanently blocked.

    The Gate-08 stream boundary is session-material-only.  Retaining this
    private name avoids a silent import break for old local tooling, while the
    immediate failure makes a second stream-owned REST session impossible.
    """

    _EXECUTION_AUTHORITY = "OFF"

    def __init__(
        self,
        *,
        settings_provider: SettingsProvider,
        session_factory: SessionFactory,
        stream_client_factory: StreamClientFactory,
    ) -> None:
        raise Shadow01LocalDemoReadOnlyStreamError("SHADOW01_STREAM_LEGACY_TRANSPORT_RETIRED")
        self._settings_provider = settings_provider
        self._expected_demo_account_id: str | None = None
        self._session_factory = session_factory
        self._stream_client_factory = stream_client_factory
        self._session_manager: SessionManagerProtocol | None = None
        self._session_transport: SessionManagerReadOnlyTransport | None = None
        self._stream_client: _ReadOnlyPriceClient | None = None
        self._connected = False
        self._subscribed_epics: tuple[str, ...] = ()

    def connect(self) -> None:
        """Authenticate lazily, verify identity, then create one stream client."""

        self._require_execution_authority_off()
        # A bridge reconnect follows a disconnect indication.  Dispose any
        # stale local client before creating a fresh authenticated session.
        self._best_effort_disconnect_and_clear()
        self._require_current_demo_configuration()
        try:
            session_manager, session_transport = self._construct_authenticated_session()
            self._session_manager = session_manager
            self._session_transport = session_transport
            self._require_session_identity()
            endpoint, account_id, cst, x_security_token = self._stream_parameters()
            stream_client = self._stream_client_factory(
                endpoint,
                account_id,
                cst,
                x_security_token,
            )
            _require_stream_client_contract(stream_client)
            self._stream_client = stream_client
            stream_client.connect()
            self._require_post_action_identity()
            self._connected = True
        except Shadow01LocalDemoReadOnlyStreamError:
            self._best_effort_disconnect_and_clear()
            raise
        except Exception:
            self._best_effort_disconnect_and_clear()
            raise ShadowStreamDisconnected("SHADOW01_STREAM_CONNECTION_UNAVAILABLE") from None

    def subscribe_prices(self, epics: tuple[str, ...]) -> None:
        """Subscribe only through a proven current Demo session."""

        self._require_pre_action_identity()
        client = self._require_connected_client()
        normalized = _normalize_epics(epics)
        if self._subscribed_epics:
            raise Shadow01LocalDemoReadOnlyStreamError(
                "SHADOW01_STREAM_SUBSCRIPTION_ALREADY_ACTIVE"
            )
        try:
            client.subscribe_prices(normalized)
            self._require_post_action_identity()
        except Shadow01LocalDemoReadOnlyStreamError:
            self._best_effort_disconnect_and_clear()
            raise
        except Exception:
            self._best_effort_disconnect_and_clear()
            raise ShadowStreamDisconnected("SHADOW01_STREAM_SUBSCRIPTION_UNAVAILABLE") from None
        self._subscribed_epics = normalized

    def receive_price_update(self, *, timeout_seconds: float = 0.0) -> ShadowPriceUpdate | None:
        """Return one subscribed price update after pre/post identity checks."""

        self._require_pre_action_identity()
        client = self._require_connected_client()
        try:
            update = client.receive_price_update(timeout_seconds=timeout_seconds)
            self._require_post_action_identity()
        except Shadow01LocalDemoReadOnlyStreamError:
            self._best_effort_disconnect_and_clear()
            raise
        except ShadowStreamDisconnected:
            self._best_effort_disconnect_and_clear()
            raise
        except Exception:
            self._best_effort_disconnect_and_clear()
            raise ShadowStreamDisconnected("SHADOW01_STREAM_RECEIVE_UNAVAILABLE") from None
        if update is None:
            return None
        if not isinstance(update, ShadowPriceUpdate) or update.epic not in self._subscribed_epics:
            self._best_effort_disconnect_and_clear()
            raise Shadow01LocalDemoReadOnlyStreamError(
                "SHADOW01_STREAM_UPDATE_SUBSCRIPTION_UNVERIFIED"
            )
        return update

    def unsubscribe_prices(self, epics: tuple[str, ...]) -> None:
        """Remove a current subscription only through a proven Demo session."""

        self._require_pre_action_identity()
        client = self._require_connected_client()
        normalized = _normalize_epics(epics)
        if not set(normalized).issubset(self._subscribed_epics):
            raise Shadow01LocalDemoReadOnlyStreamError("SHADOW01_STREAM_SUBSCRIPTION_UNVERIFIED")
        try:
            client.unsubscribe_prices(normalized)
            self._require_post_action_identity()
        except Shadow01LocalDemoReadOnlyStreamError:
            self._best_effort_disconnect_and_clear()
            raise
        except Exception:
            self._best_effort_disconnect_and_clear()
            raise ShadowStreamDisconnected("SHADOW01_STREAM_UNSUBSCRIPTION_UNAVAILABLE") from None
        removed = frozenset(normalized)
        self._subscribed_epics = tuple(
            epic for epic in self._subscribed_epics if epic not in removed
        )

    def disconnect(self) -> None:
        """Best-effort stream teardown, including when account identity is unknown."""

        self._require_execution_authority_off()
        if self._stream_client is None and self._session_transport is None:
            self._clear_local_state()
            return

        identity_unverified = False
        try:
            self._require_current_demo_configuration()
            self._require_session_identity()
        except Shadow01LocalDemoReadOnlyStreamError:
            identity_unverified = True

        client = self._stream_client
        session_transport = self._session_transport
        disconnect_failed = False
        try:
            if client is not None:
                client.disconnect()
        except Exception:
            disconnect_failed = True

        post_identity_unverified = False
        if not identity_unverified:
            try:
                self._require_current_demo_configuration()
                self._require_session_identity()
            except Shadow01LocalDemoReadOnlyStreamError:
                post_identity_unverified = True
        logout_failed = False
        try:
            if not identity_unverified and not post_identity_unverified:
                if session_transport is None:
                    logout_failed = True
                else:
                    try:
                        session_transport.logout()
                    except Exception:
                        logout_failed = True
        finally:
            self._clear_local_state()

        if identity_unverified or post_identity_unverified:
            raise Shadow01LocalDemoReadOnlyStreamError("SHADOW01_DEMO_ACCOUNT_IDENTITY_UNVERIFIED")
        if disconnect_failed or logout_failed:
            raise ShadowStreamDisconnected("SHADOW01_STREAM_DISCONNECT_UNAVAILABLE")

    def _construct_authenticated_session(
        self,
    ) -> tuple[SessionManagerProtocol, SessionManagerReadOnlyTransport]:
        holder: dict[str, SessionManagerReadOnlyTransport] = {}

        def observe_transport_request(method: str, endpoint: str) -> None:
            transport = holder.get("transport")
            if transport is None:
                raise Shadow01LocalDemoReadOnlyStreamError(
                    "SHADOW01_DEMO_SESSION_CONSTRUCTION_FAILED"
                )
            transport.observe_transport_request(method, endpoint)

        try:
            session_manager = self._session_factory(observe_transport_request)
            expected_demo_account_id = self._require_expected_demo_account_id()
            transport = SessionManagerReadOnlyTransport(
                session_manager,
                expected_demo_account_id=expected_demo_account_id,
            )
            holder["transport"] = transport
            # Retain the reviewed handles before authentication so a later
            # post-login identity failure can still invoke guarded cleanup.
            self._session_manager = session_manager
            self._session_transport = transport
            # This is the only authentication path.  The reviewed transport
            # admits exactly POST /session and observes the actual internal
            # SessionManager request before it reaches its HTTP client.
            transport.authorized_request("POST", "/session")
        except Shadow01LocalDemoReadOnlyError:
            raise Shadow01LocalDemoReadOnlyStreamError(
                "SHADOW01_DEMO_ACCOUNT_IDENTITY_UNVERIFIED"
            ) from None
        except Exception:
            raise Shadow01LocalDemoReadOnlyStreamError(
                "SHADOW01_DEMO_STREAM_AUTHENTICATION_FAILED"
            ) from None
        return session_manager, transport

    def _require_pre_action_identity(self) -> None:
        try:
            self._require_execution_authority_off()
            self._require_current_demo_configuration()
            self._require_session_identity()
        except Shadow01LocalDemoReadOnlyStreamError:
            self._best_effort_disconnect_and_clear()
            raise

    def _require_post_action_identity(self) -> None:
        try:
            self._require_execution_authority_off()
            self._require_current_demo_configuration()
            self._require_session_identity()
        except Shadow01LocalDemoReadOnlyStreamError:
            self._best_effort_disconnect_and_clear()
            raise

    def _require_execution_authority_off(self) -> None:
        if self._EXECUTION_AUTHORITY != "OFF":
            raise Shadow01LocalDemoReadOnlyStreamError("SHADOW01_EXECUTION_AUTHORITY_VIOLATION")

    def _require_current_demo_configuration(self) -> None:
        try:
            settings = self._settings_provider()
        except Exception:
            raise Shadow01LocalDemoReadOnlyStreamError("SHADOW01_SETTINGS_UNAVAILABLE") from None
        status = _status_for_settings(settings)
        configured_account_id = getattr(settings, "ig_expected_demo_account_id", None)
        if not status.ready or not _nonempty_string(configured_account_id):
            raise Shadow01LocalDemoReadOnlyStreamError(
                "SHADOW01_DEMO_STREAM_CONFIGURATION_UNVERIFIED"
            )
        if self._expected_demo_account_id is None:
            self._expected_demo_account_id = configured_account_id
        elif configured_account_id != self._expected_demo_account_id:
            raise Shadow01LocalDemoReadOnlyStreamError(
                "SHADOW01_DEMO_STREAM_CONFIGURATION_UNVERIFIED"
            )

    def _require_expected_demo_account_id(self) -> str:
        expected_demo_account_id = self._expected_demo_account_id
        if expected_demo_account_id is None:
            raise Shadow01LocalDemoReadOnlyStreamError(
                "SHADOW01_DEMO_STREAM_CONFIGURATION_UNVERIFIED"
            )
        return expected_demo_account_id

    def _require_session_identity(self) -> None:
        session_manager = self._session_manager
        expected_demo_account_id = self._expected_demo_account_id
        if session_manager is None:
            raise Shadow01LocalDemoReadOnlyStreamError("SHADOW01_DEMO_ACCOUNT_IDENTITY_UNVERIFIED")
        try:
            authenticated = session_manager.is_authenticated() is True
            account_id = session_manager.account_id
        except Exception:
            raise Shadow01LocalDemoReadOnlyStreamError(
                "SHADOW01_DEMO_ACCOUNT_IDENTITY_UNVERIFIED"
            ) from None
        if (
            expected_demo_account_id is None
            or not authenticated
            or account_id != expected_demo_account_id
        ):
            raise Shadow01LocalDemoReadOnlyStreamError("SHADOW01_DEMO_ACCOUNT_IDENTITY_UNVERIFIED")

    def _stream_parameters(self) -> tuple[str, str, str, str]:
        session_manager = self._session_manager
        expected_demo_account_id = self._expected_demo_account_id
        if session_manager is None:
            raise Shadow01LocalDemoReadOnlyStreamError("SHADOW01_DEMO_ACCOUNT_IDENTITY_UNVERIFIED")
        try:
            endpoint = session_manager.lightstreamer_endpoint  # type: ignore[attr-defined]
            account_id = session_manager.account_id
            cst = session_manager.cst  # type: ignore[attr-defined]
            x_security_token = session_manager.x_security_token  # type: ignore[attr-defined]
        except Exception:
            raise Shadow01LocalDemoReadOnlyStreamError(
                "SHADOW01_DEMO_STREAM_SESSION_UNAVAILABLE"
            ) from None
        if (
            not _is_valid_stream_endpoint(endpoint)
            or expected_demo_account_id is None
            or account_id != expected_demo_account_id
            or not _nonempty_string(cst)
            or not _nonempty_string(x_security_token)
        ):
            raise Shadow01LocalDemoReadOnlyStreamError("SHADOW01_DEMO_STREAM_SESSION_UNAVAILABLE")
        return endpoint, account_id, cst, x_security_token

    def _require_connected_client(self) -> _ReadOnlyPriceClient:
        if not self._connected or self._stream_client is None:
            raise Shadow01LocalDemoReadOnlyStreamError("SHADOW01_STREAM_NOT_CONNECTED")
        return self._stream_client

    def _best_effort_disconnect_and_clear(self) -> None:
        client = self._stream_client
        session_transport = self._session_transport
        identity_proven = self._identity_is_proven_for_cleanup()
        try:
            if client is not None:
                with suppress(Exception):
                    client.disconnect()
            if identity_proven and session_transport is not None:
                # The transport independently rechecks identity and never
                # authenticates during logout, so an intervening change also
                # fails closed before DELETE /session.
                with suppress(Exception):
                    session_transport.logout()
        finally:
            self._clear_local_state()

    def _identity_is_proven_for_cleanup(self) -> bool:
        """Return true only when guarded session cleanup may be attempted."""

        try:
            self._require_execution_authority_off()
            self._require_current_demo_configuration()
            self._require_session_identity()
        except Shadow01LocalDemoReadOnlyStreamError:
            return False
        return True

    def _clear_local_state(self) -> None:
        self._connected = False
        self._subscribed_epics = ()
        self._stream_client = None
        self._session_transport = None
        self._session_manager = None


class _SessionBoundLocalDemoReadOnlyStreamTransport:
    """Stream-only lifecycle using material owned by an existing REST session.

    This transport has no SessionManager or REST transport reference.  Its
    disconnect method can therefore never issue ``DELETE /session``; the
    surrounding read-only broker owns the single final REST logout.
    """

    _EXECUTION_AUTHORITY = "OFF"

    def __init__(
        self,
        *,
        session_material: ShadowStreamSessionMaterial,
        stream_client_factory: StreamClientFactory,
    ) -> None:
        self._session_material = session_material
        self._stream_client_factory = stream_client_factory
        self._stream_client: _ReadOnlyPriceClient | None = None
        self._connected = False
        self._subscribed_epics: tuple[str, ...] = ()

    def connect(self) -> None:
        """Create a stream client only from the pre-proven handoff material."""

        self._require_execution_authority_off()
        self._best_effort_stream_disconnect()
        material = self._session_material
        if not _is_valid_stream_endpoint(material.lightstreamer_endpoint):
            raise Shadow01LocalDemoReadOnlyStreamError("SHADOW01_DEMO_STREAM_SESSION_UNAVAILABLE")
        try:
            stream_client = self._stream_client_factory(
                material.lightstreamer_endpoint,
                material.account_identifier,
                material.cst,
                material.x_security_token,
            )
            _require_stream_client_contract(stream_client)
            self._stream_client = stream_client
            stream_client.connect()
            self._connected = True
        except Shadow01LocalDemoReadOnlyStreamError:
            self._best_effort_stream_disconnect()
            raise
        except Exception:
            self._best_effort_stream_disconnect()
            raise ShadowStreamDisconnected("SHADOW01_STREAM_CONNECTION_UNAVAILABLE") from None

    def subscribe_prices(self, epics: tuple[str, ...]) -> None:
        """Subscribe verified Price items without a REST operation."""

        self._require_execution_authority_off()
        client = self._require_connected_client()
        normalized = _normalize_epics(epics)
        if self._subscribed_epics:
            raise Shadow01LocalDemoReadOnlyStreamError(
                "SHADOW01_STREAM_SUBSCRIPTION_ALREADY_ACTIVE"
            )
        try:
            client.subscribe_prices(normalized)
            self._subscribed_epics = normalized
        except Shadow01LocalDemoReadOnlyStreamError:
            self._best_effort_stream_disconnect()
            raise
        except Exception:
            self._best_effort_stream_disconnect()
            raise ShadowStreamDisconnected("SHADOW01_STREAM_SUBSCRIPTION_UNAVAILABLE") from None

    def receive_price_update(self, *, timeout_seconds: float = 0.0) -> ShadowPriceUpdate | None:
        """Return one update after only the bridge's bounded wait interval."""

        self._require_execution_authority_off()
        client = self._require_connected_client()
        try:
            update = client.receive_price_update(timeout_seconds=timeout_seconds)
        except Shadow01LocalDemoReadOnlyStreamError:
            self._best_effort_stream_disconnect()
            raise
        except ShadowStreamDisconnected:
            self._best_effort_stream_disconnect()
            raise
        except Exception:
            self._best_effort_stream_disconnect()
            raise ShadowStreamDisconnected("SHADOW01_STREAM_RECEIVE_UNAVAILABLE") from None
        if update is None:
            return None
        if not isinstance(update, ShadowPriceUpdate) or update.epic not in self._subscribed_epics:
            self._best_effort_stream_disconnect()
            raise Shadow01LocalDemoReadOnlyStreamError(
                "SHADOW01_STREAM_UPDATE_SUBSCRIPTION_UNVERIFIED"
            )
        return update

    def unsubscribe_prices(self, epics: tuple[str, ...]) -> None:
        """Unsubscribe only active Price items without REST cleanup."""

        self._require_execution_authority_off()
        client = self._require_connected_client()
        normalized = _normalize_epics(epics)
        if not set(normalized).issubset(self._subscribed_epics):
            raise Shadow01LocalDemoReadOnlyStreamError("SHADOW01_STREAM_SUBSCRIPTION_UNVERIFIED")
        try:
            client.unsubscribe_prices(normalized)
        except Shadow01LocalDemoReadOnlyStreamError:
            self._best_effort_stream_disconnect()
            raise
        except Exception:
            self._best_effort_stream_disconnect()
            raise ShadowStreamDisconnected("SHADOW01_STREAM_UNSUBSCRIPTION_UNAVAILABLE") from None
        removed = frozenset(normalized)
        self._subscribed_epics = tuple(
            epic for epic in self._subscribed_epics if epic not in removed
        )

    def disconnect(self) -> None:
        """Tear down Lightstreamer only; parent REST logout remains external."""

        self._require_execution_authority_off()
        client = self._stream_client
        try:
            if client is not None:
                client.disconnect()
        except Exception:
            raise ShadowStreamDisconnected("SHADOW01_STREAM_DISCONNECT_UNAVAILABLE") from None
        finally:
            self._clear_stream_state()

    def subscription_diagnostic(self, epic: str) -> dict[str, bool | int | None]:
        """Delegate only safe callback facts from the active Price adapter."""

        if epic not in self._subscribed_epics or self._stream_client is None:
            return {
                "subscription_requested": False,
                "subscription_active": None,
                "listener_registered": False,
                "update_callback_count": 0,
            }
        reader = getattr(self._stream_client, "subscription_diagnostic", None)
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
        return (
            document
            if isinstance(document, dict)
            else {
                "subscription_requested": False,
                "subscription_active": None,
                "listener_registered": False,
                "update_callback_count": 0,
            }
        )

    def field_contract_diagnostic(self, epic: str) -> dict[str, object]:
        """Delegate one value-safe callback-shape record when available."""

        if epic not in self._subscribed_epics or self._stream_client is None:
            return _no_callback_contract()
        reader = getattr(self._stream_client, "field_contract_diagnostic", None)
        if not callable(reader):
            return _no_callback_contract()
        try:
            document = reader(epic)
        except Exception:
            return _no_callback_contract()
        return dict(document) if isinstance(document, Mapping) else _no_callback_contract()

    def invalid_reason_counts(self, epic: str) -> dict[str, int]:
        """Delegate the safe invalid-quote counter set when available."""

        if epic not in self._subscribed_epics or self._stream_client is None:
            return _zero_invalid_reason_counts()
        reader = getattr(self._stream_client, "invalid_reason_counts", None)
        if not callable(reader):
            return _zero_invalid_reason_counts()
        try:
            document = reader(epic)
        except Exception:
            return _zero_invalid_reason_counts()
        if not isinstance(document, Mapping):
            return _zero_invalid_reason_counts()
        return {
            name: value
            if isinstance(value := document.get(name), int)
            and not isinstance(value, bool)
            and value >= 0
            else 0
            for name in _INVALID_REASON_NAMES
        }

    def record_quote_validation(self, epic: str, reason_codes: tuple[str, ...]) -> None:
        """Pass canonical rejection reasons to the queue without stream values."""

        if epic not in self._subscribed_epics or self._stream_client is None:
            return
        writer = getattr(self._stream_client, "record_quote_validation", None)
        if not callable(writer):
            return
        try:
            writer(epic, reason_codes)
        except Exception:
            return

    def _require_execution_authority_off(self) -> None:
        if self._EXECUTION_AUTHORITY != "OFF":
            raise Shadow01LocalDemoReadOnlyStreamError("SHADOW01_EXECUTION_AUTHORITY_VIOLATION")

    def _require_connected_client(self) -> _ReadOnlyPriceClient:
        if not self._connected or self._stream_client is None:
            raise Shadow01LocalDemoReadOnlyStreamError("SHADOW01_STREAM_NOT_CONNECTED")
        return self._stream_client

    def _best_effort_stream_disconnect(self) -> None:
        client = self._stream_client
        try:
            if client is not None:
                with suppress(Exception):
                    client.disconnect()
        finally:
            self._clear_stream_state()

    def _clear_stream_state(self) -> None:
        self._connected = False
        self._subscribed_epics = ()
        self._stream_client = None


def _status_for_settings(settings: object) -> LocalDemoReadOnlyStatus:
    """Reuse the existing credential-safe local Demo readiness contract."""

    return Shadow01LocalDemoReadOnlyFactory(settings_provider=lambda: settings).status()


def _load_settings_lazily() -> object:
    """Import settings only for an explicit status or stream lifecycle action."""

    from src.ig_trader.config import settings

    return settings


def _build_session_manager_lazily(
    request_observer: Callable[[str, str], None],
) -> SessionManagerProtocol:
    """Import and construct SessionManager only during a stream ``connect``."""

    from src.ig_trader.session import SessionManager

    return SessionManager(request_observer=request_observer)


def _build_lightstreamer_client_lazily(
    endpoint: str,
    account_id: str,
    cst: str,
    x_security_token: str,
) -> _ReadOnlyPriceClient:
    """Create a direct queue-based Lightstreamer adapter after identity proof.

    This is intentionally not a ``DemoPriceStream`` adapter: direct handles
    are required to unsubscribe an individual EPIC and to provide a bounded,
    nonblocking receive-next-update operation.
    """

    from lightstreamer.client import (
        ClientListener,
        LightstreamerClient,
        Subscription,
        SubscriptionListener,
    )

    from src.ig_trader.http_client import build_system_ssl_context

    _configure_lightstreamer_tls(LightstreamerClient, build_system_ssl_context)
    try:
        client = LightstreamerClient(endpoint, None)
        client.connectionDetails.setUser(account_id)
        client.connectionDetails.setPassword(f"CST-{cst}|XST-{x_security_token}")
        _configure_http_streaming_transport(client)
    except Exception:
        raise Shadow01LocalDemoReadOnlyStreamError(
            "SHADOW01_DEMO_STREAM_CLIENT_CONSTRUCTION_FAILED"
        ) from None

    class _ConnectionListener(ClientListener):
        def __init__(self, adapter: _QueuedLightstreamerPriceClient) -> None:
            self._adapter = adapter

        def onStatusChange(self, status: object) -> None:  # noqa: N802 - library callback
            if str(status).upper().startswith("DISCONNECTED"):
                self._adapter.mark_disconnected()

        def onServerError(self, _code: object, _message: object) -> None:  # noqa: N802
            self._adapter.mark_disconnected()

    class _PriceListener(SubscriptionListener):
        def __init__(self, adapter: _QueuedLightstreamerPriceClient, epic: str) -> None:
            self._adapter = adapter
            self._epic = epic

        def onSubscription(self) -> None:  # noqa: N802 - library callback spelling
            self._adapter.mark_subscription_active(self._epic)

        def onSubscriptionError(self, _code: object, _message: object) -> None:  # noqa: N802
            self._adapter.mark_subscription_inactive(self._epic)

        def onItemUpdate(self, update: Any) -> None:  # noqa: N802 - library callback
            try:
                item_name = update.getItemName()
                bid = update.getValue("BIDPRICE1")
                ask = update.getValue("ASKPRICE1")
                timestamp = update.getValue("TIMESTAMP")
            except (AttributeError, TypeError, ValueError):
                return
            self._adapter.record_item_update(
                epic=self._epic,
                item_name=item_name,
                bid_value=bid,
                ask_value=ask,
                timestamp_milliseconds=timestamp,
                market_state=None,
                changed_field_names=_changed_field_names(update),
                is_snapshot=_snapshot_flag(update),
            )

    adapter = _QueuedLightstreamerPriceClient(
        client=client,
        account_id=account_id,
        subscription_factory=Subscription,
        listener_factory=lambda epic: _PriceListener(adapter, epic),
    )
    try:
        client.addListener(_ConnectionListener(adapter))
    except Exception:
        raise Shadow01LocalDemoReadOnlyStreamError(
            "SHADOW01_DEMO_STREAM_CLIENT_CONSTRUCTION_FAILED"
        ) from None
    return adapter


class _QueuedLightstreamerPriceClient:
    """Bounded Price queue with isolated per-EPIC MERGE field state."""

    # Price events may arrive on a listener thread faster than the diagnostic
    # polls them.  Never silently discard an event or permit unbounded local
    # retention: an overflow instead invalidates the stream and lets the
    # registry-bound bridge perform its bounded reconnect policy.
    _MAX_PENDING_UPDATES = 256
    _MAX_WAIT_SECONDS = 10.0

    def __init__(
        self,
        *,
        client: object,
        account_id: str,
        subscription_factory: Callable[[str, list[str], list[str]], object],
        listener_factory: Callable[[str], object],
    ) -> None:
        if not _nonempty_string(account_id):
            raise Shadow01LocalDemoReadOnlyStreamError(
                "SHADOW01_DEMO_STREAM_CLIENT_CONSTRUCTION_FAILED"
            )
        self._client = client
        self._account_id = account_id
        self._subscription_factory = subscription_factory
        self._listener_factory = listener_factory
        self._subscriptions: dict[str, object] = {}
        self._subscription_active: dict[str, bool | None] = {}
        self._listener_registered: set[str] = set()
        self._field_state: dict[str, dict[str, object]] = {}
        self._field_contract: dict[str, dict[str, object]] = {}
        self._invalid_reason_counts: dict[str, dict[str, int]] = {}
        self._callback_count: dict[str, int] = {}
        self._updates: deque[ShadowPriceUpdate] = deque()
        self._update_condition = Condition()
        self._update_buffer_overflowed = False
        self._connected = False
        self._disconnected = False

    def connect(self) -> None:
        with self._update_condition:
            self._updates.clear()
            self._subscription_active.clear()
            self._listener_registered.clear()
            self._field_state.clear()
            self._field_contract.clear()
            self._invalid_reason_counts.clear()
            self._callback_count.clear()
            self._update_buffer_overflowed = False
            self._connected = False
            self._disconnected = False
        try:
            self._client.connect()  # type: ignore[attr-defined]
        except Exception:
            raise ShadowStreamDisconnected("SHADOW01_STREAM_CONNECTION_UNAVAILABLE") from None
        with self._update_condition:
            if self._disconnected:
                raise ShadowStreamDisconnected("SHADOW01_STREAM_CONNECTION_UNAVAILABLE")
            self._connected = True

    def subscribe_prices(self, epics: tuple[str, ...]) -> None:
        if not self._connection_is_available():
            raise ShadowStreamDisconnected("SHADOW01_STREAM_CONNECTION_UNAVAILABLE")
        normalized = _normalize_epics(epics)
        if any(epic in self._subscriptions for epic in normalized):
            raise Shadow01LocalDemoReadOnlyStreamError(
                "SHADOW01_STREAM_SUBSCRIPTION_ALREADY_ACTIVE"
            )
        try:
            for epic in normalized:
                subscription = self._subscription_factory(
                    "MERGE",
                    [f"PRICE:{self._account_id}:{epic}"],
                    ["BIDPRICE1", "ASKPRICE1", "TIMESTAMP"],
                )
                data_adapter_setter = getattr(subscription, "setDataAdapter", None)
                if not callable(data_adapter_setter):
                    raise Shadow01LocalDemoReadOnlyStreamError(
                        "SHADOW01_STREAM_SUBSCRIPTION_CONTRACT_UNAVAILABLE"
                    )
                data_adapter_setter("Pricing")
                subscription.addListener(self._listener_factory(epic))  # type: ignore[attr-defined]
                # A MERGE initial image may be delivered synchronously from
                # subscribe().  Register first so its callback cannot vanish.
                with self._update_condition:
                    self._subscriptions[epic] = subscription
                    self._subscription_active[epic] = None
                    self._listener_registered.add(epic)
                    self._field_state[epic] = {}
                    self._field_contract[epic] = _no_callback_contract()
                    self._invalid_reason_counts[epic] = _zero_invalid_reason_counts()
                    self._callback_count[epic] = 0
                self._client.subscribe(subscription)  # type: ignore[attr-defined]
                if not self._connection_is_available():
                    raise ShadowStreamDisconnected("SHADOW01_STREAM_SUBSCRIPTION_UNAVAILABLE")
        except Shadow01LocalDemoReadOnlyStreamError:
            self._remove_subscriptions(normalized)
            raise
        except Exception:
            self._remove_subscriptions(normalized)
            raise ShadowStreamDisconnected("SHADOW01_STREAM_SUBSCRIPTION_UNAVAILABLE") from None

    def receive_price_update(self, *, timeout_seconds: float) -> ShadowPriceUpdate | None:
        """Wait once, for a fixed bounded interval, for the next queued update."""

        timeout = _bounded_wait_seconds(timeout_seconds)
        with self._update_condition:
            self._raise_if_unavailable()
            update = self._next_subscribed_update()
            if update is not None or timeout == 0:
                return update
            self._update_condition.wait(timeout)
            self._raise_if_unavailable()
            return self._next_subscribed_update()

    def unsubscribe_prices(self, epics: tuple[str, ...]) -> None:
        if not self._connection_is_available():
            raise ShadowStreamDisconnected("SHADOW01_STREAM_UNSUBSCRIPTION_UNAVAILABLE")
        normalized = _normalize_epics(epics)
        if any(epic not in self._subscriptions for epic in normalized):
            raise Shadow01LocalDemoReadOnlyStreamError("SHADOW01_STREAM_SUBSCRIPTION_UNVERIFIED")
        try:
            for epic in normalized:
                subscription = self._subscriptions[epic]
                self._client.unsubscribe(subscription)  # type: ignore[attr-defined]
                if not self._connection_is_available():
                    raise ShadowStreamDisconnected("SHADOW01_STREAM_UNSUBSCRIPTION_UNAVAILABLE")
        except Shadow01LocalDemoReadOnlyStreamError:
            raise
        except Exception:
            raise ShadowStreamDisconnected("SHADOW01_STREAM_UNSUBSCRIPTION_UNAVAILABLE") from None
        self._remove_subscriptions(normalized)

    def disconnect(self) -> None:
        try:
            self._client.disconnect()  # type: ignore[attr-defined]
        finally:
            self._subscriptions.clear()
            with self._update_condition:
                self._connected = False
                self._disconnected = True
                self._updates.clear()
                self._subscription_active.clear()
                self._listener_registered.clear()
                self._field_state.clear()
                self._field_contract.clear()
                self._invalid_reason_counts.clear()
                self._callback_count.clear()
                self._update_buffer_overflowed = False
                self._update_condition.notify_all()

    def record_item_update(
        self,
        *,
        epic: str,
        item_name: object,
        bid_value: object,
        ask_value: object,
        timestamp_milliseconds: object,
        market_state: object,
        changed_field_names: tuple[str, ...] = (),
        is_snapshot: bool | None = None,
    ) -> None:
        """Merge only same-EPIC partial MERGE fields and queue one raw update."""

        with self._update_condition:
            if epic not in self._subscriptions:
                return
            item_name_recognized = item_name == f"PRICE:{self._account_id}:{epic}"
            self._field_contract[epic] = _callback_contract_diagnostic(
                item_name_recognized=item_name_recognized,
                bid_value=bid_value,
                ask_value=ask_value,
                timestamp_milliseconds=timestamp_milliseconds,
                is_snapshot=is_snapshot,
                changed_field_names=changed_field_names,
            )
            if not item_name_recognized:
                self._invalid_reason_counts[epic]["item_resolution_failure"] += 1
                return
            state = self._field_state[epic]
            for name, value in (
                ("bid_value", bid_value),
                ("ask_value", ask_value),
                ("timestamp_milliseconds", timestamp_milliseconds),
                ("market_state", market_state),
            ):
                if value is not None:
                    state[name] = value
            self._callback_count[epic] += 1
            self._record_update_locked(
                ShadowPriceUpdate(
                    epic=epic,
                    bid_value=state.get("bid_value"),
                    ask_value=state.get("ask_value"),
                    timestamp_milliseconds=state.get("timestamp_milliseconds"),
                    market_state=state.get("market_state"),
                )
            )

    def record_update(self, update: ShadowPriceUpdate) -> None:
        """Record one already item-validated update for tests and adapters."""

        with self._update_condition:
            if update.epic not in self._subscriptions:
                return
            self._field_contract[update.epic] = _callback_contract_diagnostic(
                item_name_recognized=True,
                bid_value=update.bid_value,
                ask_value=update.ask_value,
                timestamp_milliseconds=update.timestamp_milliseconds,
                is_snapshot=None,
                changed_field_names=(),
            )
            self._callback_count[update.epic] += 1
            self._record_update_locked(update)

    def mark_subscription_active(self, epic: str) -> None:
        with self._update_condition:
            if epic in self._subscriptions:
                self._subscription_active[epic] = True

    def mark_subscription_inactive(self, epic: str) -> None:
        with self._update_condition:
            if epic in self._subscriptions:
                self._subscription_active[epic] = False
                self._update_condition.notify_all()

    def subscription_diagnostic(self, epic: str) -> dict[str, bool | int | None]:
        """Return only safe local subscription and callback lifecycle facts."""

        with self._update_condition:
            return {
                "subscription_requested": epic in self._subscriptions,
                "subscription_active": self._subscription_active.get(epic),
                "listener_registered": epic in self._listener_registered,
                "update_callback_count": self._callback_count.get(epic, 0),
            }

    def field_contract_diagnostic(self, epic: str) -> dict[str, object]:
        """Return only the latest value-free callback shape for one EPIC."""

        with self._update_condition:
            return dict(self._field_contract.get(epic, _no_callback_contract()))

    def invalid_reason_counts(self, epic: str) -> dict[str, int]:
        """Return the reviewed invalid-quote categories without source values."""

        with self._update_condition:
            return dict(self._invalid_reason_counts.get(epic, _zero_invalid_reason_counts()))

    def record_quote_validation(self, epic: str, reason_codes: tuple[str, ...]) -> None:
        """Classify one rejected canonical quote without retaining its values."""

        with self._update_condition:
            if epic not in self._subscriptions:
                return
            counts = self._invalid_reason_counts[epic]
            state = self._field_state[epic]
            reasons = frozenset(reason_codes)
            if "SHADOW01_LIVE_QUOTE_BID_UNAVAILABLE" in reasons:
                counts[_field_failure_category(state.get("bid_value"), "bid")] += 1
            if "SHADOW01_LIVE_QUOTE_ASK_UNAVAILABLE" in reasons:
                counts[_field_failure_category(state.get("ask_value"), "ask")] += 1
            if "SHADOW01_LIVE_QUOTE_STALE" in reasons:
                counts["stale_timestamp"] += 1
            if reasons & {
                "SHADOW01_STREAM_TIMESTAMP_MISSING",
                "SHADOW01_STREAM_TIMESTAMP_SCHEMA_UNSUPPORTED",
                "SHADOW01_STREAM_TIMESTAMP_INVALID",
            }:
                counts[_timestamp_failure_category(state.get("timestamp_milliseconds"))] += 1
            if "SHADOW01_LIVE_QUOTE_SPREAD_INVALID" in reasons:
                counts["invalid_ask"] += 1

    def mark_disconnected(self) -> None:
        """Record a listener-reported disconnect without retaining its message."""

        with self._update_condition:
            self._connected = False
            self._disconnected = True
            self._updates.clear()
            self._update_condition.notify_all()

    def _record_update_locked(self, update: ShadowPriceUpdate) -> None:
        if self._update_buffer_overflowed or not self._connected or self._disconnected:
            return
        if len(self._updates) >= self._MAX_PENDING_UPDATES:
            self._updates.clear()
            self._update_buffer_overflowed = True
            self._disconnected = True
            self._update_condition.notify_all()
            return
        self._updates.append(update)
        self._update_condition.notify()

    def _next_subscribed_update(self) -> ShadowPriceUpdate | None:
        while self._updates:
            update = self._updates.popleft()
            if update.epic in self._subscriptions:
                return update
        return None

    def _raise_if_unavailable(self) -> None:
        if self._update_buffer_overflowed:
            raise ShadowStreamDisconnected("SHADOW01_STREAM_UPDATE_BUFFER_OVERFLOW")
        if self._disconnected:
            raise ShadowStreamDisconnected("SHADOW01_STREAM_RECEIVE_UNAVAILABLE")

    def _remove_subscriptions(self, epics: tuple[str, ...]) -> None:
        removed = frozenset(epics)
        with self._update_condition:
            for epic in removed:
                self._subscriptions.pop(epic, None)
                self._subscription_active.pop(epic, None)
                self._listener_registered.discard(epic)
                self._field_state.pop(epic, None)
                self._field_contract.pop(epic, None)
                self._invalid_reason_counts.pop(epic, None)
                self._callback_count.pop(epic, None)
            self._updates = deque(update for update in self._updates if update.epic not in removed)
            self._update_condition.notify_all()

    def _connection_is_available(self) -> bool:
        """Read the listener-updated connection state without a stale race."""

        with self._update_condition:
            return self._connected and not self._disconnected


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
_TIMESTAMP_MILLISECONDS_THRESHOLD = 100_000_000_000


def _no_callback_contract() -> dict[str, object]:
    return {
        "callback_observed": False,
        "item_name_recognized": False,
        "BIDPRICE1": _price_field_contract(None),
        "ASKPRICE1": _price_field_contract(None),
        "TIMESTAMP": _timestamp_field_contract(None),
        "is_snapshot": None,
        "changed_field_names": [],
    }


def _callback_contract_diagnostic(
    *,
    item_name_recognized: bool,
    bid_value: object,
    ask_value: object,
    timestamp_milliseconds: object,
    is_snapshot: bool | None,
    changed_field_names: tuple[str, ...],
) -> dict[str, object]:
    """Describe a Price callback without serialising its source values."""

    return {
        "callback_observed": True,
        "item_name_recognized": item_name_recognized,
        "BIDPRICE1": _price_field_contract(bid_value),
        "ASKPRICE1": _price_field_contract(ask_value),
        "TIMESTAMP": _timestamp_field_contract(timestamp_milliseconds),
        "is_snapshot": is_snapshot,
        "changed_field_names": list(changed_field_names),
    }


def _price_field_contract(value: object) -> dict[str, object]:
    parsed = _positive_decimal(value)
    return {
        "present": value is not None,
        "runtime_type": type(value).__name__ if value is not None else None,
        "is_none": value is None,
        "is_numeric_string": isinstance(value, str) and parsed is not None,
        "is_numeric_object": _numeric_object(value),
        "parse_success": parsed is not None,
    }


def _timestamp_field_contract(value: object) -> dict[str, object]:
    parsed = _timestamp_milliseconds(value)
    return {
        "present": value is not None,
        "runtime_type": type(value).__name__ if value is not None else None,
        "is_none": value is None,
        "is_digit_string": isinstance(value, str) and value.isascii() and value.isdigit(),
        "is_numeric_object": _numeric_object(value),
        "string_length": len(value) if isinstance(value, str) else None,
        "parse_success": parsed is not None,
        "milliseconds_plausible": (
            parsed is not None and parsed >= _TIMESTAMP_MILLISECONDS_THRESHOLD
        ),
    }


def _changed_field_names(update: object) -> tuple[str, ...]:
    """Read only Lightstreamer field names, never the changed values."""

    getter = getattr(update, "getChangedFields", None)
    if not callable(getter):
        return ()
    try:
        fields = getter()
    except Exception:
        return ()
    if not isinstance(fields, Mapping):
        return ()
    return tuple(
        sorted(
            name
            for name in fields
            if isinstance(name, str) and name.isidentifier() and len(name) <= 64
        )
    )


def _snapshot_flag(update: object) -> bool | None:
    checker = getattr(update, "isSnapshot", None)
    if not callable(checker):
        return None
    try:
        value = checker()
    except Exception:
        return None
    return value if isinstance(value, bool) else None


def _zero_invalid_reason_counts() -> dict[str, int]:
    return {name: 0 for name in _INVALID_REASON_NAMES}


def _field_failure_category(value: object, field: str) -> str:
    return f"missing_{field}" if value is None else f"invalid_{field}"


def _timestamp_failure_category(value: object) -> str:
    return "missing_timestamp" if value is None else "invalid_timestamp"


def _positive_decimal(value: object) -> Decimal | None:
    if isinstance(value, bool) or not isinstance(value, (Decimal, Number, str)):
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return parsed if parsed.is_finite() and parsed > 0 else None


def _timestamp_milliseconds(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, str):
        if not value.isascii() or not value.isdigit():
            return None
    elif not _numeric_object(value):
        return None
    try:
        parsed = float(value)
    except (OverflowError, TypeError, ValueError):
        return None
    return parsed if isfinite(parsed) and parsed > 0 else None


def _numeric_object(value: object) -> bool:
    if isinstance(value, (bool, str)) or not isinstance(value, (Decimal, Number)):
        return False
    try:
        return isfinite(float(value))
    except (OverflowError, TypeError, ValueError):
        return False


_LIGHTSTREAMER_TLS_CONFIGURED = False


def _configure_lightstreamer_tls(
    lightstreamer_client: object,
    ssl_context_factory: Callable[[], object],
) -> None:
    """Configure hostname-verifying system trust before the first client exists."""

    global _LIGHTSTREAMER_TLS_CONFIGURED
    if _LIGHTSTREAMER_TLS_CONFIGURED:
        return
    try:
        lightstreamer_client.setTrustManagerFactory(ssl_context_factory())  # type: ignore[attr-defined]
    except Exception:
        raise Shadow01LocalDemoReadOnlyStreamError(
            "SHADOW01_DEMO_STREAM_TLS_CONFIGURATION_FAILED"
        ) from None
    _LIGHTSTREAMER_TLS_CONFIGURED = True


def _configure_http_streaming_transport(client: object) -> None:
    """Avoid the installed client's defective WebSocket-dispose callback path."""

    try:
        setter = client.connectionOptions.setForcedTransport  # type: ignore[attr-defined]
    except Exception:
        raise Shadow01LocalDemoReadOnlyStreamError(
            "SHADOW01_DEMO_STREAM_TRANSPORT_CONFIGURATION_FAILED"
        ) from None
    if not callable(setter):
        raise Shadow01LocalDemoReadOnlyStreamError(
            "SHADOW01_DEMO_STREAM_TRANSPORT_CONFIGURATION_FAILED"
        )
    try:
        setter("HTTP-STREAMING")
    except Exception:
        raise Shadow01LocalDemoReadOnlyStreamError(
            "SHADOW01_DEMO_STREAM_TRANSPORT_CONFIGURATION_FAILED"
        ) from None


def _require_stream_client_contract(value: object) -> None:
    required_methods = (
        "connect",
        "subscribe_prices",
        "receive_price_update",
        "unsubscribe_prices",
        "disconnect",
    )
    if not all(callable(getattr(value, name, None)) for name in required_methods):
        raise Shadow01LocalDemoReadOnlyStreamError(
            "SHADOW01_DEMO_STREAM_CLIENT_CONSTRUCTION_FAILED"
        )


def _normalize_epics(epics: object) -> tuple[str, ...]:
    if isinstance(epics, (str, bytes)):
        raise Shadow01LocalDemoReadOnlyStreamError("SHADOW01_STREAM_EPIC_COLLECTION_INVALID")
    try:
        values = tuple(epics)  # type: ignore[arg-type]
    except TypeError:
        raise Shadow01LocalDemoReadOnlyStreamError(
            "SHADOW01_STREAM_EPIC_COLLECTION_INVALID"
        ) from None
    if (
        not values
        or len(set(values)) != len(values)
        or any(not isinstance(epic, str) or not epic or epic != epic.strip() for epic in values)
    ):
        raise Shadow01LocalDemoReadOnlyStreamError("SHADOW01_STREAM_EPIC_COLLECTION_INVALID")
    return values


def _bounded_wait_seconds(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise Shadow01LocalDemoReadOnlyStreamError("SHADOW01_STREAM_WAIT_INVALID")
    seconds = float(value)
    if not 0 <= seconds <= _QueuedLightstreamerPriceClient._MAX_WAIT_SECONDS:
        raise Shadow01LocalDemoReadOnlyStreamError("SHADOW01_STREAM_WAIT_INVALID")
    return seconds


def _is_valid_stream_endpoint(value: object) -> bool:
    if not _nonempty_string(value):
        return False
    parsed = urlsplit(value)
    try:
        port = parsed.port
    except ValueError:
        return False
    return (
        parsed.scheme == "https"
        and parsed.hostname is not None
        and parsed.username is None
        and parsed.password is None
        and port in {None, 443}
        and not parsed.query
        and not parsed.fragment
    )


def _nonempty_string(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


__all__ = (
    "SessionFactory",
    "SettingsProvider",
    "Shadow01LocalDemoReadOnlyStreamError",
    "Shadow01LocalDemoReadOnlyStreamFactory",
    "StreamClientFactory",
)
