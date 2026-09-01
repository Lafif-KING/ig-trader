"""Lazy, local-only IG Demo transport construction for Shadow01.

This module deliberately has no module-level ``SessionManager`` import.  A
SessionManager creates an HTTP client in its constructor, so importing this
module or asking a factory for its status cannot construct a client,
authenticate, or make a network request.  Only :meth:`build` can do that.

The factory is intentionally narrow.  It validates an IG Demo-only endpoint,
requires non-empty credentials without ever returning or logging them, and
places a second method/path allowlist directly in front of the session
manager.  The resulting ``Shadow01ReadOnlyBroker`` remains observation-only
and always reports execution authority ``OFF``.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol
from urllib.parse import urlsplit

from .read_only_broker import Shadow01ReadOnlyBroker
from .schedule_contract import (
    sanitize_v2_schedule_response_contract,
    sanitize_v3_schedule_response_contract,
    v2_schedule_response_contract,
    v3_schedule_response_contract,
)


class Shadow01LocalDemoReadOnlyError(RuntimeError):
    """A local Demo read-only factory precondition or boundary was violated."""


@dataclass(frozen=True)
class ReadOnlyResponseDiagnostic:
    """Sanitized non-success IG response evidence with no body or secrets."""

    status_code: int
    upstream_error_code: str | None

    def document(self) -> dict[str, int | str | None]:
        return {
            "status_code": self.status_code,
            "upstream_error_code": self.upstream_error_code,
        }


class SessionManagerProtocol(Protocol):
    """The only SessionManager behaviour used by the Shadow01 boundary."""

    account_id: str | None

    def login(self) -> bool: ...

    def logout(self) -> bool: ...

    def is_authenticated(self) -> bool: ...

    def authorized_request(self, method: str, endpoint: str, **kwargs: Any) -> Any: ...


SessionFactory = Callable[[Callable[[str, str], None]], SessionManagerProtocol]
SettingsProvider = Callable[[], object]


@dataclass(frozen=True)
class LocalDemoReadOnlyStatus:
    """Credential-safe readiness state for a local Shadow01 Demo adapter."""

    ready: bool
    reason_code: str
    execution_authority: str = "OFF"
    demo_mode: bool = False
    demo_endpoint: bool = False
    local_operator: bool = False
    paper_trading: bool = False
    expected_demo_account_configured: bool = False
    credentials_present: bool = False

    def document(self) -> dict[str, bool | str]:
        """Return a credential-free document suitable for diagnostics."""

        return {
            "ready": self.ready,
            "reason_code": self.reason_code,
            "execution_authority": self.execution_authority,
            "demo_mode": self.demo_mode,
            "demo_endpoint": self.demo_endpoint,
            "local_operator": self.local_operator,
            "paper_trading": self.paper_trading,
            "expected_demo_account_configured": self.expected_demo_account_configured,
            "credentials_present": self.credentials_present,
        }


@dataclass(frozen=True)
class ShadowStreamSessionMaterial:
    """The immutable, non-renderable stream credentials of one proven session.

    The parent read-only REST transport creates this only after it has proven
    the expected active Demo account.  It is deliberately an internal handoff
    object: its values are hidden from ``repr`` and it has no serialization or
    logging method.  The stream owns neither the parent session nor its
    eventual REST logout.
    """

    account_identifier: str = field(repr=False)
    lightstreamer_endpoint: str = field(repr=False)
    cst: str = field(repr=False)
    x_security_token: str = field(repr=False)

    def __post_init__(self) -> None:
        if not all(
            _nonempty_string(value)
            for value in (
                self.account_identifier,
                self.lightstreamer_endpoint,
                self.cst,
                self.x_security_token,
            )
        ):
            raise ValueError("Shadow stream session material is incomplete")

    def presence_document(self) -> dict[str, bool]:
        """Return safe presence facts without revealing session material."""

        return {
            "account_identifier_present": True,
            "lightstreamer_endpoint_present": True,
            "cst_present": True,
            "x_security_token_present": True,
        }


class SessionManagerReadOnlyTransport:
    """Second exact REST allowlist immediately ahead of a SessionManager.

    ``SessionManager`` may perform its own ``POST session`` while authenticating
    an otherwise read-only request.  Its request observer is therefore wired
    to :meth:`observe_transport_request`, which validates that internal request
    before the underlying HTTP client can send it.
    """

    _MARKET_PATH = re.compile(r"/markets/[A-Za-z0-9._-]+\Z")
    _PRICE_PATH = re.compile(r"/prices/[A-Za-z0-9._-]+/[A-Za-z0-9._-]+/[1-9][0-9]*\Z")
    _UPSTREAM_ERROR_CODE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,159}\Z")
    _READ_VERSION_BY_ENDPOINT = {
        "/session": "1",
        "/accounts": "1",
    }

    def __init__(
        self,
        session_manager: SessionManagerProtocol,
        *,
        expected_demo_account_id: str,
        maximum_outbound_http_requests: int | None = None,
        maximum_authentication_requests: int | None = None,
        maximum_requests_per_route: int | None = None,
    ) -> None:
        if not callable(getattr(session_manager, "authorized_request", None)):
            raise TypeError("Shadow01 requires a SessionManager authorized-request method")
        if not callable(getattr(session_manager, "login", None)):
            raise TypeError("Shadow01 requires a SessionManager login method")
        if not callable(getattr(session_manager, "logout", None)):
            raise TypeError("Shadow01 requires a SessionManager logout method")
        if not callable(getattr(session_manager, "is_authenticated", None)):
            raise TypeError("Shadow01 requires a SessionManager authentication-state method")
        if not _is_exact_account_id(expected_demo_account_id):
            raise Shadow01LocalDemoReadOnlyError("SHADOW01_EXPECTED_DEMO_ACCOUNT_ID_REQUIRED")
        if not all(
            _is_optional_positive_request_limit(value)
            for value in (
                maximum_outbound_http_requests,
                maximum_authentication_requests,
                maximum_requests_per_route,
            )
        ):
            raise ValueError("Shadow01 observed request limit is invalid")
        self._session_manager = session_manager
        self._expected_demo_account_id = expected_demo_account_id
        self._maximum_outbound_http_requests = maximum_outbound_http_requests
        self._maximum_authentication_requests = maximum_authentication_requests
        self._maximum_requests_per_route = maximum_requests_per_route
        self._internal_authentication_observer: Callable[[], None] | None = None
        self._explicit_authentication_request_active = False
        self._explicit_logout_request_active = False
        self._logout_request_observed = False
        self._outbound_http_request_count = 0
        self._outbound_authentication_request_count = 0
        self._outbound_request_count_by_route: dict[tuple[str, str], int] = {}
        self._active_v3_schedule_endpoint: str | None = None
        self._active_v3_dispatch_version: str | None = None
        self._last_v3_schedule_response_contract: dict[str, object] | None = None
        self._active_v2_schedule_endpoint: str | None = None
        self._active_v2_dispatch_version: str | None = None
        self._last_v2_schedule_response_contract: dict[str, object] | None = None
        self._last_response_diagnostic: ReadOnlyResponseDiagnostic | None = None
        response_error_observer_setter = getattr(
            session_manager,
            "set_response_error_observer",
            None,
        )
        if callable(response_error_observer_setter):
            response_error_observer_setter(self._capture_response_error)
        request_version_observer_setter = getattr(
            session_manager,
            "set_request_version_observer",
            None,
        )
        if callable(request_version_observer_setter):
            request_version_observer_setter(self._capture_request_version)

    def set_internal_authentication_observer(self, observer: Callable[[], None]) -> None:
        """Register the broker's telemetry hook for implicit session POSTs."""

        if not callable(observer):
            raise TypeError("Shadow01 internal authentication observer must be callable")
        self._internal_authentication_observer = observer

    @property
    def outbound_http_request_count(self) -> int:
        """Return observed physical HTTP attempts, including a failed logout."""

        return self._outbound_http_request_count

    @property
    def outbound_authentication_request_count(self) -> int:
        """Return the observed physical Demo-session login count only."""

        return self._outbound_authentication_request_count

    def consume_v3_schedule_response_contract(self) -> dict[str, object] | None:
        """Return then clear the one approved V3 response-shape contract."""

        value = sanitize_v3_schedule_response_contract(self._last_v3_schedule_response_contract)
        self._last_v3_schedule_response_contract = None
        return value

    def consume_v2_schedule_response_contract(self) -> dict[str, object] | None:
        """Return then clear the one approved V2 response-shape contract."""

        value = sanitize_v2_schedule_response_contract(self._last_v2_schedule_response_contract)
        self._last_v2_schedule_response_contract = None
        return value

    def authorized_request(self, method: str, endpoint: str, **kwargs: Any) -> Any:
        """Pass only exact allowlisted requests to the session manager."""

        self._require_allowed(method, endpoint)
        api_version = kwargs.pop("api_version", None)
        if kwargs or api_version not in {None, "2", "3"}:
            raise Shadow01LocalDemoReadOnlyError("SHADOW01_READ_ONLY_REQUEST_ARGUMENTS_DENIED")
        if api_version in {"2", "3"} and self._MARKET_PATH.fullmatch(endpoint) is None:
            raise Shadow01LocalDemoReadOnlyError("SHADOW01_READ_ONLY_REQUEST_DENIED")
        if api_version == "3":
            self._active_v3_schedule_endpoint = endpoint
            self._active_v3_dispatch_version = None
            self._last_v3_schedule_response_contract = None
        if api_version == "2":
            self._active_v2_schedule_endpoint = endpoint
            self._active_v2_dispatch_version = None
            self._last_v2_schedule_response_contract = None
        if method == "POST":
            self._explicit_authentication_request_active = True
            try:
                self._require_authenticated_expected_account()
            finally:
                self._explicit_authentication_request_active = False
            return True
        self._require_authenticated_expected_account()
        self._last_response_diagnostic = None
        try:
            response = self._session_manager.authorized_request(
                method,
                endpoint,
                headers=self._fixed_read_headers(endpoint, api_version=api_version),
            )
        except Shadow01LocalDemoReadOnlyError:
            raise
        except Exception:
            raise Shadow01LocalDemoReadOnlyError(
                "SHADOW01_READ_ONLY_RESPONSE_UNAVAILABLE"
            ) from None
        self._require_expected_account_identity()
        response_status = _response_status(response)
        try:
            document = self._response_document(response)
        except Shadow01LocalDemoReadOnlyError:
            if api_version == "3":
                self._record_v3_schedule_response_contract(
                    response_status=response_status,
                    document=None,
                )
            if api_version == "2":
                self._record_v2_schedule_response_contract(
                    response_status=response_status,
                    document=None,
                )
            raise
        if api_version == "3":
            self._record_v3_schedule_response_contract(
                response_status=response_status,
                document=document,
            )
        if api_version == "2":
            self._record_v2_schedule_response_contract(
                response_status=response_status,
                document=document,
            )
        return document

    def logout(self) -> bool:
        """Close only the proven current Demo session through a dedicated path.

        This is intentionally not an ``authorized_request`` DELETE route.  It
        cannot authenticate, select an account, or accept caller arguments.
        The SessionManager observer must see the exact DELETE while this
        tightly-scoped flag is active, otherwise cleanup fails closed.
        """

        self._require_expected_account_identity()
        self._logout_request_observed = False
        self._last_response_diagnostic = None
        self._explicit_logout_request_active = True
        try:
            logged_out = self._session_manager.logout()
        except Exception:
            raise Shadow01LocalDemoReadOnlyError("SHADOW01_DEMO_SESSION_LOGOUT_FAILED") from None
        finally:
            self._explicit_logout_request_active = False
        if logged_out is not True or not self._logout_request_observed or self._is_authenticated():
            raise Shadow01LocalDemoReadOnlyError("SHADOW01_DEMO_SESSION_LOGOUT_FAILED")
        return True

    def latest_response_diagnostic(self) -> dict[str, int | str | None] | None:
        """Return only the last sanitized failed-response facts, if any."""

        diagnostic = self._last_response_diagnostic
        return diagnostic.document() if diagnostic is not None else None

    def stream_session_material(self) -> ShadowStreamSessionMaterial:
        """Return the current authenticated stream handoff without a REST call.

        This method never authenticates, refreshes, logs, or deletes a
        session.  A missing or changed parent session fails closed before any
        stream client can be constructed.
        """

        self._require_expected_account_identity()
        try:
            material = ShadowStreamSessionMaterial(
                account_identifier=self._session_manager.account_id,
                lightstreamer_endpoint=self._session_manager.lightstreamer_endpoint,  # type: ignore[attr-defined]
                cst=self._session_manager.cst,  # type: ignore[attr-defined]
                x_security_token=self._session_manager.x_security_token,  # type: ignore[attr-defined]
            )
        except (AttributeError, TypeError, ValueError):
            raise Shadow01LocalDemoReadOnlyError(
                "SHADOW01_STREAM_SESSION_MATERIAL_UNAVAILABLE"
            ) from None
        if material.account_identifier != self._expected_demo_account_id:
            raise Shadow01LocalDemoReadOnlyError("SHADOW01_DEMO_ACCOUNT_MISMATCH")
        return material

    def account_state_is_valid(self, document: object) -> bool:
        """Validate the configured account's read-only-observation state.

        The method returns a boolean only.  It never returns an account row or
        account identifier, and it re-proves the current authenticated account
        before comparing the already-read ``/accounts`` document in memory.
        """

        self._require_expected_account_identity()
        if not isinstance(document, Mapping):
            return False
        accounts = document.get("accounts")
        if not isinstance(accounts, list):
            return False
        configured: Mapping[str, object] | None = None
        for item in accounts:
            if not isinstance(item, Mapping):
                continue
            if item.get("accountId") == self._expected_demo_account_id:
                configured = item
        if configured is None or configured.get("preferred") is not True:
            return False
        status = configured.get("status")
        return isinstance(status, str) and status == "ENABLED"

    def _require_authenticated_expected_account(self) -> None:
        """Prove the active authenticated Demo account before every read.

        ``SessionManager.account_id`` is populated from the successful
        ``POST /session`` response's current-account identity.  A missing,
        stale, or mismatched identity blocks before a GET reaches
        ``SessionManager.authorized_request``.
        """

        if not self._is_authenticated():
            try:
                logged_in = self._session_manager.login()
            except Shadow01LocalDemoReadOnlyError:
                raise
            except Exception:
                raise Shadow01LocalDemoReadOnlyError(
                    "SHADOW01_DEMO_AUTHENTICATION_FAILED"
                ) from None
            if logged_in is not True:
                raise Shadow01LocalDemoReadOnlyError("SHADOW01_DEMO_AUTHENTICATION_FAILED")
            if not self._is_authenticated():
                raise Shadow01LocalDemoReadOnlyError("SHADOW01_DEMO_AUTHENTICATION_FAILED")
        self._require_expected_account_identity()

    def _is_authenticated(self) -> bool:
        """Read session authentication state without accepting an unknown value."""

        try:
            return self._session_manager.is_authenticated() is True
        except Exception:
            raise Shadow01LocalDemoReadOnlyError(
                "SHADOW01_DEMO_ACCOUNT_IDENTITY_UNVERIFIED"
            ) from None

    def _require_expected_account_identity(self) -> None:
        """Require an authenticated, exact current-account match without logging it."""

        if not self._is_authenticated():
            raise Shadow01LocalDemoReadOnlyError("SHADOW01_DEMO_ACCOUNT_IDENTITY_UNVERIFIED")
        try:
            authenticated_account_id = self._session_manager.account_id
        except Exception:
            raise Shadow01LocalDemoReadOnlyError(
                "SHADOW01_DEMO_ACCOUNT_IDENTITY_UNVERIFIED"
            ) from None
        if not _is_exact_account_id(authenticated_account_id):
            raise Shadow01LocalDemoReadOnlyError("SHADOW01_DEMO_ACCOUNT_IDENTITY_UNVERIFIED")
        if authenticated_account_id != self._expected_demo_account_id:
            raise Shadow01LocalDemoReadOnlyError("SHADOW01_DEMO_ACCOUNT_MISMATCH")

    def _response_document(self, response: Any) -> dict[str, object]:
        """Return only a successful JSON-object response to the Shadow runner."""

        try:
            status_code = response.status_code
        except Exception:
            raise Shadow01LocalDemoReadOnlyError(
                "SHADOW01_READ_ONLY_RESPONSE_STATUS_INVALID"
            ) from None
        if (
            isinstance(status_code, bool)
            or not isinstance(status_code, int)
            or not 200 <= status_code < 300
        ):
            self._capture_response_error(response)
            if status_code == 403:
                raise Shadow01LocalDemoReadOnlyError("SHADOW01_READ_ONLY_HTTP_403")
            raise Shadow01LocalDemoReadOnlyError("SHADOW01_READ_ONLY_RESPONSE_STATUS_INVALID")
        try:
            document = response.json()
        except Exception:
            raise Shadow01LocalDemoReadOnlyError(
                "SHADOW01_READ_ONLY_RESPONSE_JSON_INVALID"
            ) from None
        if not isinstance(document, Mapping):
            raise Shadow01LocalDemoReadOnlyError("SHADOW01_READ_ONLY_RESPONSE_DOCUMENT_INVALID")
        try:
            return dict(document)
        except Exception:
            raise Shadow01LocalDemoReadOnlyError(
                "SHADOW01_READ_ONLY_RESPONSE_DOCUMENT_INVALID"
            ) from None

    def _capture_response_error(self, response: Any) -> None:
        """Keep only a 4xx/5xx status and safe IG error code from one response."""

        try:
            status_code = response.status_code
        except Exception:
            return
        if (
            isinstance(status_code, bool)
            or not isinstance(status_code, int)
            or not 400 <= status_code < 600
        ):
            return
        upstream_error_code: str | None = None
        try:
            document = response.json()
        except Exception:
            document = None
        if isinstance(document, Mapping):
            candidate = document.get("errorCode")
            if isinstance(candidate, str) and self._UPSTREAM_ERROR_CODE.fullmatch(candidate):
                upstream_error_code = candidate
        self._last_response_diagnostic = ReadOnlyResponseDiagnostic(
            status_code=status_code,
            upstream_error_code=upstream_error_code,
        )

    def observe_transport_request(self, method: str, endpoint: str) -> None:
        """Fail closed before any SessionManager HTTP request reaches IG.

        The current SessionManager reports paths to its observer without a
        leading slash.  The observer normalizes that known representation only;
        it does not relax the exact route allowlist.
        """

        if not isinstance(method, str) or not isinstance(endpoint, str):
            raise Shadow01LocalDemoReadOnlyError("SHADOW01_READ_ONLY_REQUEST_DENIED")
        normalized_endpoint = endpoint if endpoint.startswith("/") else f"/{endpoint}"
        if method == "DELETE" and normalized_endpoint == "/session":
            if not self._explicit_logout_request_active:
                raise Shadow01LocalDemoReadOnlyError("SHADOW01_READ_ONLY_REQUEST_DENIED")
            self._logout_request_observed = True
            self._reserve_observed_http_request(method, normalized_endpoint)
            return
        self._require_allowed(method, normalized_endpoint)
        self._reserve_observed_http_request(method, normalized_endpoint)
        if (
            method == "POST"
            and normalized_endpoint == "/session"
            and not self._explicit_authentication_request_active
            and self._internal_authentication_observer is not None
        ):
            self._internal_authentication_observer()

    def _reserve_observed_http_request(self, method: str, endpoint: str) -> None:
        """Reserve one physical dispatch before the HTTP client can send it.

        Gate12 supplies all three limits.  They prevent retries and implicit
        reauthentication from turning a bounded authorization into extra Demo
        traffic.  Normal Shadow01 construction leaves the limits unset.
        """

        route = (method, endpoint)
        route_count = self._outbound_request_count_by_route.get(route, 0)
        if (
            method == "POST"
            and endpoint == "/session"
            and self._maximum_authentication_requests is not None
            and self._outbound_authentication_request_count >= self._maximum_authentication_requests
        ):
            raise Shadow01LocalDemoReadOnlyError(
                "SHADOW01_READ_ONLY_AUTHENTICATION_REQUEST_BUDGET_EXCEEDED"
            )
        if (
            self._maximum_requests_per_route is not None
            and route_count >= self._maximum_requests_per_route
        ):
            raise Shadow01LocalDemoReadOnlyError("SHADOW01_READ_ONLY_ROUTE_REQUEST_BUDGET_EXCEEDED")
        if (
            self._maximum_outbound_http_requests is not None
            and self._outbound_http_request_count >= self._maximum_outbound_http_requests
        ):
            raise Shadow01LocalDemoReadOnlyError(
                "SHADOW01_READ_ONLY_OUTBOUND_REQUEST_BUDGET_EXCEEDED"
            )
        self._outbound_request_count_by_route[route] = route_count + 1
        self._outbound_http_request_count += 1
        if method == "POST" and endpoint == "/session":
            self._outbound_authentication_request_count += 1

    def _capture_request_version(self, method: object, endpoint: object, version: object) -> None:
        """Capture only a final dispatched VERSION for an active schedule contract."""

        if not isinstance(method, str) or not isinstance(endpoint, str):
            return
        normalized_endpoint = endpoint if endpoint.startswith("/") else f"/{endpoint}"
        if method != "GET":
            return
        safe_version = version if version in {"1", "2", "3", "4"} else None
        if normalized_endpoint == self._active_v3_schedule_endpoint:
            self._active_v3_dispatch_version = safe_version
        if normalized_endpoint == self._active_v2_schedule_endpoint:
            self._active_v2_dispatch_version = safe_version

    def _record_v3_schedule_response_contract(
        self,
        *,
        response_status: int | None,
        document: object,
    ) -> None:
        """Retain only response shape and the HTTP-client dispatch version."""

        self._last_v3_schedule_response_contract = v3_schedule_response_contract(
            response_status=response_status,
            dispatched_version=self._active_v3_dispatch_version,
            document=document,
        )
        self._active_v3_schedule_endpoint = None
        self._active_v3_dispatch_version = None

    def _record_v2_schedule_response_contract(
        self,
        *,
        response_status: int | None,
        document: object,
    ) -> None:
        """Retain only V2 response shape and its final dispatch version."""

        self._last_v2_schedule_response_contract = v2_schedule_response_contract(
            response_status=response_status,
            dispatched_version=self._active_v2_dispatch_version,
            document=document,
        )
        self._active_v2_schedule_endpoint = None
        self._active_v2_dispatch_version = None

    @classmethod
    def _require_allowed(cls, method: object, endpoint: object) -> None:
        if not isinstance(method, str) or not isinstance(endpoint, str):
            raise Shadow01LocalDemoReadOnlyError("SHADOW01_READ_ONLY_REQUEST_DENIED")
        if method == "POST" and endpoint == "/session":
            return
        if method == "GET" and (
            endpoint in {"/session", "/accounts"}
            or cls._MARKET_PATH.fullmatch(endpoint) is not None
            or cls._PRICE_PATH.fullmatch(endpoint) is not None
        ):
            return
        raise Shadow01LocalDemoReadOnlyError("SHADOW01_READ_ONLY_REQUEST_DENIED")

    @classmethod
    def _fixed_read_headers(cls, endpoint: str, *, api_version: object = None) -> dict[str, str]:
        """Return the one reviewed IG API version for an exact read route."""

        if api_version in {"2", "3"}:
            if cls._MARKET_PATH.fullmatch(endpoint) is None:
                raise Shadow01LocalDemoReadOnlyError("SHADOW01_READ_ONLY_REQUEST_DENIED")
            version = api_version
        else:
            version = cls._READ_VERSION_BY_ENDPOINT.get(endpoint)
        if version is None:
            if cls._MARKET_PATH.fullmatch(endpoint) is not None:
                version = "4"
            elif cls._PRICE_PATH.fullmatch(endpoint) is not None:
                version = "2"
        if version is None:
            raise Shadow01LocalDemoReadOnlyError("SHADOW01_READ_ONLY_REQUEST_DENIED")
        return {
            "VERSION": version,
            "Accept": "application/json; charset=UTF-8",
        }


class Shadow01LocalDemoReadOnlyFactory:
    """Build a real SessionManager-backed broker only after explicit consent.

    ``status`` is deliberately non-activating: it reads only the supplied
    settings object and does not call the session factory.  ``build`` checks
    the same status, then creates one SessionManager with the transport
    observer already installed.
    """

    _EXECUTION_AUTHORITY = "OFF"
    _DEMO_HOST = "demo-api.ig.com"
    _DEMO_PATH = "/gateway/deal"
    _CREDENTIAL_FIELDS = ("ig_api_key", "ig_identifier", "ig_password")
    _EXPECTED_DEMO_ACCOUNT_ID_FIELD = "ig_expected_demo_account_id"

    def __init__(
        self,
        *,
        settings_provider: SettingsProvider | None = None,
        session_factory: SessionFactory | None = None,
        max_http_attempts: int = 3,
        maximum_outbound_http_requests: int | None = None,
        maximum_authentication_requests: int | None = None,
        maximum_requests_per_route: int | None = None,
    ) -> None:
        if (
            isinstance(max_http_attempts, bool)
            or not isinstance(max_http_attempts, int)
            or not 1 <= max_http_attempts <= 3
        ):
            raise ValueError("Shadow01 HTTP attempt bound is invalid")
        if not all(
            _is_optional_positive_request_limit(value)
            for value in (
                maximum_outbound_http_requests,
                maximum_authentication_requests,
                maximum_requests_per_route,
            )
        ):
            raise ValueError("Shadow01 observed request limit is invalid")
        self._settings_provider = settings_provider or _load_settings_lazily
        self._max_http_attempts = max_http_attempts
        self._maximum_outbound_http_requests = maximum_outbound_http_requests
        self._maximum_authentication_requests = maximum_authentication_requests
        self._maximum_requests_per_route = maximum_requests_per_route
        if session_factory is None:
            self._session_factory = lambda observer: _build_session_manager_lazily(
                observer,
                max_retries=max_http_attempts,
            )
        else:
            self._session_factory = session_factory

    @property
    def execution_authority(self) -> str:
        """State the invariant without providing a way to change it."""

        return self._EXECUTION_AUTHORITY

    @property
    def max_http_attempts(self) -> int:
        """Return the tested physical-attempt cap without exposing settings."""

        return self._max_http_attempts

    @property
    def maximum_outbound_http_requests(self) -> int | None:
        """Return an optional hard physical-request ceiling for bounded callers."""

        return self._maximum_outbound_http_requests

    @property
    def maximum_authentication_requests(self) -> int | None:
        """Return an optional hard Demo-login ceiling for bounded callers."""

        return self._maximum_authentication_requests

    @property
    def maximum_requests_per_route(self) -> int | None:
        """Return an optional hard per-route dispatch ceiling for bounded callers."""

        return self._maximum_requests_per_route

    def status(self) -> LocalDemoReadOnlyStatus:
        """Return non-activating readiness without exposing credential values."""

        try:
            settings = self._settings_provider()
        except Exception:
            return LocalDemoReadOnlyStatus(False, "SHADOW01_SETTINGS_UNAVAILABLE")
        return self._status_for(settings)

    def _status_for(self, settings: object) -> LocalDemoReadOnlyStatus:
        """Evaluate one supplied settings snapshot without constructing a client."""

        demo_mode = getattr(settings, "ig_demo", None) is True
        demo_endpoint = _is_exact_demo_endpoint(getattr(settings, "ig_base_url", None))
        local_operator = getattr(settings, "demo_operator_local", None) is True
        paper_trading = getattr(settings, "paper_trading", None) is True
        expected_demo_account_configured = _is_exact_account_id(
            getattr(settings, self._EXPECTED_DEMO_ACCOUNT_ID_FIELD, None)
        )
        credentials_present = all(
            _nonempty_string(getattr(settings, name, None)) for name in self._CREDENTIAL_FIELDS
        )
        reason_code = "SHADOW01_DEMO_READ_ONLY_READY"
        if not demo_mode:
            reason_code = "SHADOW01_DEMO_MODE_REQUIRED"
        elif not demo_endpoint:
            reason_code = "SHADOW01_DEMO_ENDPOINT_REQUIRED"
        elif not local_operator:
            reason_code = "SHADOW01_LOCAL_OPERATOR_REQUIRED"
        elif not paper_trading:
            reason_code = "SHADOW01_PAPER_TRADING_REQUIRED"
        elif not expected_demo_account_configured:
            reason_code = "SHADOW01_EXPECTED_DEMO_ACCOUNT_ID_REQUIRED"
        elif not credentials_present:
            reason_code = "SHADOW01_DEMO_CREDENTIALS_REQUIRED"
        return LocalDemoReadOnlyStatus(
            ready=reason_code == "SHADOW01_DEMO_READ_ONLY_READY",
            reason_code=reason_code,
            demo_mode=demo_mode,
            demo_endpoint=demo_endpoint,
            local_operator=local_operator,
            paper_trading=paper_trading,
            expected_demo_account_configured=expected_demo_account_configured,
            credentials_present=credentials_present,
        )

    def build(self) -> Shadow01ReadOnlyBroker:
        """Explicitly create an allowlisted SessionManager-backed broker.

        This is the only operation in this module that may construct an HTTP
        client.  It never authenticates by itself; authentication happens only
        when the resulting broker receives an allowed observation request.
        """

        try:
            settings = self._settings_provider()
        except Exception:
            raise Shadow01LocalDemoReadOnlyError("SHADOW01_SETTINGS_UNAVAILABLE") from None
        status = self._status_for(settings)
        if not status.ready:
            raise Shadow01LocalDemoReadOnlyError(status.reason_code)
        expected_demo_account_id = getattr(settings, self._EXPECTED_DEMO_ACCOUNT_ID_FIELD)
        try:
            transport_holder: dict[str, SessionManagerReadOnlyTransport] = {}

            def observe(method: str, endpoint: str) -> None:
                transport_holder["transport"].observe_transport_request(method, endpoint)

            session_manager = self._session_factory(observe)
            transport = SessionManagerReadOnlyTransport(
                session_manager,
                expected_demo_account_id=expected_demo_account_id,
                maximum_outbound_http_requests=self._maximum_outbound_http_requests,
                maximum_authentication_requests=self._maximum_authentication_requests,
                maximum_requests_per_route=self._maximum_requests_per_route,
            )
            transport_holder["transport"] = transport
        except Shadow01LocalDemoReadOnlyError:
            raise
        except Exception:
            raise Shadow01LocalDemoReadOnlyError(
                "SHADOW01_DEMO_SESSION_CONSTRUCTION_FAILED"
            ) from None
        return Shadow01ReadOnlyBroker(transport)


def _load_settings_lazily() -> object:
    """Import settings only when status/build is explicitly requested."""

    from src.ig_trader.config import settings

    return settings


def _build_session_manager_lazily(
    request_observer: Callable[[str, str], None],
    *,
    max_retries: int = 3,
) -> SessionManagerProtocol:
    """Instantiate the real SessionManager only inside ``build``."""

    from src.ig_trader.session import SessionManager

    return SessionManager(request_observer=request_observer, max_retries=max_retries)


def _response_status(response: object) -> int | None:
    """Return a status integer only; malformed response objects reveal nothing."""

    try:
        value = response.status_code
    except Exception:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or not 100 <= value <= 599:
        return None
    return value


def _is_optional_positive_request_limit(value: object) -> bool:
    return value is None or (isinstance(value, int) and not isinstance(value, bool) and value >= 1)


def _is_exact_demo_endpoint(value: object) -> bool:
    """Accept only the configured HTTPS IG Demo gateway URL."""

    if not _nonempty_string(value):
        return False
    parsed = urlsplit(value)
    try:
        port = parsed.port
    except ValueError:
        return False
    return (
        parsed.scheme == "https"
        and parsed.hostname == Shadow01LocalDemoReadOnlyFactory._DEMO_HOST
        and port in {None, 443}
        and parsed.username is None
        and parsed.password is None
        and parsed.path.rstrip("/") == Shadow01LocalDemoReadOnlyFactory._DEMO_PATH
        and not parsed.query
        and not parsed.fragment
    )


def _nonempty_string(value: object) -> bool:
    """Check presence only; never return, log, or interpolate a secret."""

    return isinstance(value, str) and bool(value.strip())


def _is_exact_account_id(value: object) -> bool:
    """Require a configured or authenticated account ID without whitespace repair."""

    return _nonempty_string(value) and value == value.strip()


__all__ = (
    "LocalDemoReadOnlyStatus",
    "ReadOnlyResponseDiagnostic",
    "SessionFactory",
    "SessionManagerProtocol",
    "SessionManagerReadOnlyTransport",
    "ShadowStreamSessionMaterial",
    "Shadow01LocalDemoReadOnlyError",
    "Shadow01LocalDemoReadOnlyFactory",
)
