"""IG Demo v2 REST and Lightstreamer read-only authentication diagnostic.

This module is deliberately independent of the production SessionManager and
execution adapter. Every REST request is checked against a small allow-list
before it can reach the network.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
import re
import ssl
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import parse_qs, urlparse

import httpx
from lightstreamer.client import LightstreamerClient, Subscription

from src.ig_trader.http_client import build_system_ssl_context

DEMO_REST_HOST = "demo-api.ig.com"
DEMO_REST_BASE_URL = f"https://{DEMO_REST_HOST}/gateway/deal"
DEFAULT_EPIC = "CS.D.EURGBP.MINI.IP"
DEFAULT_OUTPUT = Path(".runtime/evidence/g1-auth-diagnostic.json")
ACCOUNT_ENV_NAMES = (
    "IG_ACCOUNT_ID",
    "IG_ACCOUNT_NUMBER",
    "IG_SERVICE_ACC_NUMBER",
)
SECRET_ENV_NAMES = (
    "IG_API_KEY",
    "IG_IDENTIFIER",
    "IG_PASSWORD",
    *ACCOUNT_ENV_NAMES,
)
PREFLIGHT_ENV_NAMES = (
    "IG_DEMO",
    "IG_BASE_URL",
    "PAPER_TRADING",
)
ORDER_PATH_PREFIXES = ("/positions", "/workingorders")
ORDER_METHODS = {"POST", "PUT", "DELETE"}
EPIC_PATTERN = re.compile(r"[A-Za-z0-9._-]+")
FINAL_CLASSIFICATIONS = {
    "PASS",
    "CODE_DEFECT",
    "CONFIGURATION_DEFECT",
    "ACCOUNT_RESTRICTION",
    "API_KEY_RESTRICTION",
    "IG_PLATFORM_FAILURE",
    "STREAMING_HANDSHAKE_FAILURE",
    "NETWORK_FAILURE",
    "INCONCLUSIVE",
}
_LIGHTSTREAMER_TRUST_LOCK = threading.Lock()
_lightstreamer_trust_configured = False


class Classification(StrEnum):
    """Permitted final work-order classifications."""

    PASS = "PASS"
    CODE_DEFECT = "CODE_DEFECT"
    CONFIGURATION_DEFECT = "CONFIGURATION_DEFECT"
    ACCOUNT_RESTRICTION = "ACCOUNT_RESTRICTION"
    API_KEY_RESTRICTION = "API_KEY_RESTRICTION"
    IG_PLATFORM_FAILURE = "IG_PLATFORM_FAILURE"
    STREAMING_HANDSHAKE_FAILURE = "STREAMING_HANDSHAKE_FAILURE"
    NETWORK_FAILURE = "NETWORK_FAILURE"
    INCONCLUSIVE = "INCONCLUSIVE"


class DiagnosticError(RuntimeError):
    """A sanitized, classified diagnostic failure."""

    def __init__(
        self,
        classification: Classification,
        reason: str,
        *,
        http_status: int | None = None,
        error_code: str | None = None,
    ) -> None:
        super().__init__(reason)
        self.classification = classification
        self.reason = reason
        self.http_status = http_status
        self.error_code = error_code


class EndpointBlockedError(DiagnosticError):
    """Raised before a non-allow-listed endpoint can reach the network."""

    def __init__(self, method: str, path: str) -> None:
        super().__init__(
            Classification.CODE_DEFECT,
            f"ENDPOINT_BLOCKED:{method.upper()}:{sanitize_path(path)}",
        )


@dataclass(frozen=True)
class DiagnosticConfig:
    """Validated runtime configuration with secret fields excluded from repr."""

    environment: str
    session_version: int
    epic: str
    output: Path
    base_url: str
    paper_trading: bool
    api_key: str = field(repr=False)
    identifier: str = field(repr=False)
    password: str = field(repr=False)
    account_id: str = field(repr=False)
    connect_timeout_seconds: float = 15.0
    quote_timeout_seconds: float = 20.0
    disconnect_timeout_seconds: float = 10.0
    maximum_quote_age_seconds: float = 5.0
    max_request_attempts: int = 2
    max_reauth_attempts: int = 2

    def secret_values(self) -> tuple[str, ...]:
        """Return sensitive values strictly for the in-memory leak scan."""

        return tuple(
            value
            for value in (
                self.api_key,
                self.identifier,
                self.password,
                self.account_id,
            )
            if value
        )


@dataclass(frozen=True)
class RestResult:
    """Parsed REST response with sensitive headers omitted."""

    status_code: int
    payload: Mapping[str, Any] | None
    headers: httpx.Headers
    error_code: str | None


class RequestingClient(Protocol):
    """The small HTTP client surface used by the guarded transport."""

    def request(self, method: str, url: str, **kwargs: Any) -> httpx.Response: ...

    def close(self) -> None: ...


def utc_now() -> datetime:
    """Return an aware UTC time."""

    return datetime.now(UTC)


def utc_text(value: datetime | None = None) -> str:
    """Format an aware UTC timestamp."""

    return (value or utc_now()).astimezone(UTC).isoformat().replace("+00:00", "Z")


def sanitize_path(path: str) -> str:
    """Return only the path component, never query values or credentials."""

    parsed = urlparse(path)
    return parsed.path or "/"


def _safe_error_code(payload: object) -> str | None:
    if not isinstance(payload, Mapping):
        return None
    value = payload.get("errorCode")
    return value if isinstance(value, str) and value else None


def _request_identifier(headers: httpx.Headers) -> str | None:
    for name in ("X-IG-REQUEST-ID", "X-REQUEST-ID", "X-CORRELATION-ID"):
        value = headers.get(name)
        if value:
            return value
    return None


def _account_fingerprint(account_id: str, key: bytes) -> str:
    digest = hashlib.sha256(key + account_id.encode("utf-8")).hexdigest()[:12]
    return f"account-sha256:{digest}"


@lru_cache(maxsize=1)
def _application_version() -> str:
    try:
        return importlib.metadata.version("ig-trader")
    except importlib.metadata.PackageNotFoundError:
        return "UNKNOWN"


@lru_cache(maxsize=1)
def _git_commit_sha() -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return "UNKNOWN"
    return result.stdout.strip() or "UNKNOWN"


def _parse_bool(value: str | None, *, default: bool) -> bool:
    if value is None or not value.strip():
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise DiagnosticError(Classification.CONFIGURATION_DEFECT, "BOOLEAN_CONFIG_INVALID")


def _dotenv_keys(path: Path) -> set[str]:
    """Read key names only; values for non-selected keys are never loaded."""

    if not path.exists():
        return set()
    keys: set[str] = set()
    for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key = line.split("=", 1)[0].strip()
        if key.lower().startswith("export "):
            key = key[7:].strip()
        if key:
            keys.add(key)
    return keys


def _selected_dotenv_values(path: Path, names: set[str]) -> dict[str, str]:
    """Load only explicitly selected keys from a simple dotenv file."""

    if not path.exists():
        return {}
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        raw_key, raw_value = line.split("=", 1)
        key = raw_key.strip()
        if key.lower().startswith("export "):
            key = key[7:].strip()
        if key not in names:
            continue
        value = raw_value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        if "${" in value:
            raise DiagnosticError(
                Classification.CONFIGURATION_DEFECT,
                f"DOTENV_INTERPOLATION_UNSUPPORTED:{key}",
            )
        values[key] = value
    return values


def _configured_value(name: str, dotenv_values: Mapping[str, str]) -> str:
    return os.environ.get(name, dotenv_values.get(name, "")).strip()


def load_config(args: argparse.Namespace, *, dotenv_path: Path = Path(".env")) -> DiagnosticConfig:
    """Validate Demo-only state before loading credential values."""

    environment = str(args.environment).strip().lower()
    if environment != "demo":
        raise DiagnosticError(Classification.CONFIGURATION_DEFECT, "LIVE_ENVIRONMENT_REJECTED")
    if int(args.session_version) != 2:
        raise DiagnosticError(Classification.CONFIGURATION_DEFECT, "SESSION_VERSION_MUST_BE_2")
    if str(args.epic) != DEFAULT_EPIC:
        raise DiagnosticError(Classification.CONFIGURATION_DEFECT, "CONFIGURED_EPIC_REQUIRED")

    dotenv_keys = _dotenv_keys(dotenv_path)
    live_keys = sorted(key for key in dotenv_keys if key.upper().startswith("IG_LIVE"))
    live_keys.extend(key for key in os.environ if key.upper().startswith("IG_LIVE"))
    if live_keys:
        raise DiagnosticError(
            Classification.CONFIGURATION_DEFECT,
            "LIVE_CREDENTIAL_CONFIGURATION_PRESENT",
        )

    preflight = _selected_dotenv_values(dotenv_path, set(PREFLIGHT_ENV_NAMES))
    base_url = _configured_value("IG_BASE_URL", preflight) or DEMO_REST_BASE_URL
    parsed = urlparse(base_url)
    if parsed.scheme != "https" or parsed.hostname != DEMO_REST_HOST:
        raise DiagnosticError(Classification.CONFIGURATION_DEFECT, "DEMO_HOSTNAME_REQUIRED")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise DiagnosticError(Classification.CONFIGURATION_DEFECT, "REST_BASE_URL_INVALID")
    if parsed.path.rstrip("/") not in {"", "/gateway/deal"}:
        raise DiagnosticError(Classification.CONFIGURATION_DEFECT, "REST_BASE_PATH_INVALID")
    base_url = DEMO_REST_BASE_URL

    if not _parse_bool(_configured_value("IG_DEMO", preflight), default=True):
        raise DiagnosticError(Classification.CONFIGURATION_DEFECT, "IG_DEMO_MUST_BE_TRUE")
    paper_trading = _parse_bool(
        _configured_value("PAPER_TRADING", preflight),
        default=True,
    )
    if not paper_trading:
        raise DiagnosticError(Classification.CONFIGURATION_DEFECT, "PAPER_TRADING_MUST_BE_TRUE")

    secrets = _selected_dotenv_values(dotenv_path, set(SECRET_ENV_NAMES))
    account_values = {
        name: _configured_value(name, secrets)
        for name in ACCOUNT_ENV_NAMES
        if _configured_value(name, secrets)
    }
    if len(set(account_values.values())) > 1:
        raise DiagnosticError(Classification.CONFIGURATION_DEFECT, "ACCOUNT_CONFIG_AMBIGUOUS")
    account_id = next(iter(account_values.values()), "")
    required = {
        "IG_API_KEY": _configured_value("IG_API_KEY", secrets),
        "IG_IDENTIFIER": _configured_value("IG_IDENTIFIER", secrets),
        "IG_PASSWORD": _configured_value("IG_PASSWORD", secrets),
        "IG_ACCOUNT_ID": account_id,
    }
    missing = sorted(name for name, value in required.items() if not value)
    if missing:
        raise DiagnosticError(
            Classification.CONFIGURATION_DEFECT,
            "MISSING_REQUIRED_CONFIG:" + ",".join(missing),
        )

    return DiagnosticConfig(
        environment=environment,
        session_version=int(args.session_version),
        epic=str(args.epic),
        output=Path(args.output),
        base_url=base_url,
        paper_trading=paper_trading,
        api_key=required["IG_API_KEY"],
        identifier=required["IG_IDENTIFIER"],
        password=required["IG_PASSWORD"],
        account_id=account_id,
        connect_timeout_seconds=float(args.connect_timeout),
        quote_timeout_seconds=float(args.quote_timeout),
        disconnect_timeout_seconds=float(args.disconnect_timeout),
        maximum_quote_age_seconds=float(args.maximum_quote_age),
    )


def endpoint_is_allowed(method: str, path: str) -> bool:
    """Return whether the request is an explicitly permitted operation."""

    method = method.upper()
    parsed = urlparse(path)
    clean_path = parsed.path
    query = parse_qs(parsed.query, keep_blank_values=True)
    if method == "POST" and clean_path == "/session" and not query:
        return True
    if method == "DELETE" and clean_path == "/session" and not query:
        return True
    if method == "GET" and clean_path in {"/session", "/accounts"} and not query:
        return True
    if method == "GET" and clean_path.startswith("/markets/") and not query:
        epic = clean_path.removeprefix("/markets/")
        return bool(EPIC_PATTERN.fullmatch(epic))
    if method == "GET" and clean_path == "/markets":
        return set(query) == {"searchTerm"} and len(query["searchTerm"]) == 1
    return False


def endpoint_version_is_allowed(method: str, path: str, version: str) -> bool:
    """Bind each allow-listed operation to its declared IG API version."""

    clean_path = urlparse(path).path
    key = (method.upper(), clean_path)
    exact_versions = {
        ("POST", "/session"): "2",
        ("DELETE", "/session"): "1",
        ("GET", "/session"): "1",
        ("GET", "/accounts"): "1",
        ("GET", "/markets"): "1",
    }
    if key in exact_versions:
        return version == exact_versions[key]
    if method.upper() == "GET" and clean_path.startswith("/markets/"):
        return version == "3"
    return False


class SafeRestClient:
    """Endpoint-allowlisted IG REST client with sanitized request evidence."""

    def __init__(
        self,
        config: DiagnosticConfig,
        request_history: list[dict[str, Any]],
        *,
        client: RequestingClient | None = None,
        clock: Callable[[], datetime] = utc_now,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.config = config
        self.request_history = request_history
        self.clock = clock
        self.monotonic = monotonic
        self.client = client or httpx.Client(
            timeout=config.connect_timeout_seconds,
            verify=build_system_ssl_context(),
            headers={"X-IG-API-KEY": config.api_key},
        )
        self.order_endpoint_call_count = 0
        self.blocked_endpoint_attempt_count = 0

    def close(self) -> None:
        self.client.close()

    def request_json(
        self,
        method: str,
        path: str,
        *,
        version: str,
        headers: Mapping[str, str] | None = None,
        body: Mapping[str, Any] | None = None,
        attempts: int | None = None,
    ) -> RestResult:
        method = method.upper()
        if not endpoint_is_allowed(method, path) or not endpoint_version_is_allowed(
            method,
            path,
            version,
        ):
            self.blocked_endpoint_attempt_count += 1
            raise EndpointBlockedError(method, path)
        if method in ORDER_METHODS and sanitize_path(path).startswith(ORDER_PATH_PREFIXES):
            self.order_endpoint_call_count += 1

        merged_headers = {"VERSION": version, "Accept": "application/json; charset=UTF-8"}
        if headers:
            merged_headers.update(headers)
        max_attempts = attempts or self.config.max_request_attempts
        for attempt in range(1, max_attempts + 1):
            started = self.monotonic()
            timestamp = utc_text(self.clock())
            try:
                response = self.client.request(
                    method,
                    self.config.base_url + path,
                    headers=merged_headers,
                    json=body,
                )
            except (httpx.TimeoutException, httpx.ConnectError):
                duration_ms = round((self.monotonic() - started) * 1000, 3)
                self.request_history.append(
                    {
                        "timestamp_utc": timestamp,
                        "method": method,
                        "path": sanitize_path(path),
                        "session_api_version": version,
                        "http_status": None,
                        "ig_request_identifier": None,
                        "ig_errorCode": None,
                        "duration_ms": duration_ms,
                        "retry_number": attempt - 1,
                        "transport_error": "TIMEOUT_OR_CONNECT_ERROR",
                    }
                )
                if attempt == max_attempts:
                    raise DiagnosticError(
                        Classification.NETWORK_FAILURE,
                        "REST_NETWORK_RETRIES_EXHAUSTED",
                    ) from None
                continue

            duration_ms = round((self.monotonic() - started) * 1000, 3)
            payload: Mapping[str, Any] | None
            if response.status_code == 204 or not response.content:
                payload = None
            else:
                try:
                    decoded = response.json()
                except (ValueError, json.JSONDecodeError):
                    self.request_history.append(
                        {
                            "timestamp_utc": timestamp,
                            "method": method,
                            "path": sanitize_path(path),
                            "session_api_version": version,
                            "http_status": response.status_code,
                            "ig_request_identifier": _request_identifier(response.headers),
                            "ig_errorCode": None,
                            "duration_ms": duration_ms,
                            "retry_number": attempt - 1,
                            "transport_error": None,
                        }
                    )
                    raise DiagnosticError(
                        Classification.IG_PLATFORM_FAILURE,
                        "MALFORMED_JSON_RESPONSE",
                        http_status=response.status_code,
                    ) from None
                if not isinstance(decoded, Mapping):
                    self.request_history.append(
                        {
                            "timestamp_utc": timestamp,
                            "method": method,
                            "path": sanitize_path(path),
                            "session_api_version": version,
                            "http_status": response.status_code,
                            "ig_request_identifier": _request_identifier(response.headers),
                            "ig_errorCode": None,
                            "duration_ms": duration_ms,
                            "retry_number": attempt - 1,
                            "transport_error": None,
                        }
                    )
                    raise DiagnosticError(
                        Classification.IG_PLATFORM_FAILURE,
                        "MALFORMED_RESPONSE_SHAPE",
                        http_status=response.status_code,
                    )
                payload = decoded
            error_code = _safe_error_code(payload)
            self.request_history.append(
                {
                    "timestamp_utc": timestamp,
                    "method": method,
                    "path": sanitize_path(path),
                    "session_api_version": version,
                    "http_status": response.status_code,
                    "ig_request_identifier": _request_identifier(response.headers),
                    "ig_errorCode": error_code,
                    "duration_ms": duration_ms,
                    "retry_number": attempt - 1,
                    "transport_error": None,
                }
            )
            return RestResult(response.status_code, payload, response.headers, error_code)

        raise DiagnosticError(Classification.CODE_DEFECT, "REQUEST_LOOP_EXHAUSTED")


def classify_http_failure(status: int, error_code: str | None) -> Classification:
    """Classify an IG error without discarding its exact code or status."""

    normalized = (error_code or "").lower()
    if "api-key" in normalized or "apikey" in normalized:
        return Classification.API_KEY_RESTRICTION
    if "preferred.account.not.set" in normalized:
        return Classification.CONFIGURATION_DEFECT
    if any(
        term in normalized
        for term in (
            "kyc",
            "agreement",
            "appropriateness",
            "preferred.account.disabled",
            "account-suspended",
            "account-not-yet-activated",
            "all-accounts-",
            "client-suspended",
            "account-access-denied",
            "not-a-client-account",
            "stockbroking-not-supported",
        )
    ):
        return Classification.ACCOUNT_RESTRICTION
    if any(
        term in normalized
        for term in ("invalid-details", "invalid-credentials", "encryption.required")
    ):
        return Classification.CONFIGURATION_DEFECT
    if status == 401:
        return Classification.CONFIGURATION_DEFECT
    if status == 403:
        return Classification.ACCOUNT_RESTRICTION
    if status >= 500:
        return Classification.IG_PLATFORM_FAILURE
    return Classification.INCONCLUSIVE


def require_success(result: RestResult, reason: str) -> Mapping[str, Any]:
    """Return a response payload or raise a classified exact-status failure."""

    if 200 <= result.status_code < 300 and result.payload is not None:
        return result.payload
    raise DiagnosticError(
        classify_http_failure(result.status_code, result.error_code),
        reason,
        http_status=result.status_code,
        error_code=result.error_code,
    )


def build_stream_password(cst: str, xst: str) -> str:
    """Build IG's required Lightstreamer password and reject OAuth tokens."""

    for value in (cst, xst):
        normalized = value.strip().lower()
        if not normalized or normalized.startswith("bearer ") or "oauth" in normalized:
            raise DiagnosticError(
                Classification.STREAMING_HANDSHAKE_FAILURE,
                "LIGHTSTREAMER_REQUIRES_CST_XST",
            )
    return f"CST-{cst}|XST-{xst}"


class ConnectionListener:
    """Thread-safe Lightstreamer connection status recorder."""

    def __init__(
        self,
        history: list[dict[str, str]],
        *,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self.history = history
        self.clock = clock
        self.connected = threading.Event()
        self.disconnected = threading.Event()
        self.server_error: tuple[int | None, str | None] | None = None
        self._lock = threading.Lock()

    def onListenStart(self, _client: object) -> None:  # noqa: N802
        return None

    def onListenEnd(self, _client: object) -> None:  # noqa: N802
        return None

    def onPropertyChange(self, _property_name: str) -> None:  # noqa: N802
        return None

    def onStatusChange(self, status: str) -> None:  # noqa: N802
        with self._lock:
            self.history.append({"timestamp_utc": utc_text(self.clock()), "status": status})
        if status.startswith("CONNECTED"):
            self.connected.set()
        if status == "DISCONNECTED":
            self.disconnected.set()

    def onServerError(self, error_code: int, error_message: str) -> None:  # noqa: N802
        self.server_error = (error_code, error_message)


class QuoteListener:
    """Record only quote presence and arrival time, never the price values."""

    def __init__(self, *, clock: Callable[[], datetime] = utc_now) -> None:
        self.clock = clock
        self.subscribed = threading.Event()
        self.quote_received = threading.Event()
        self.subscription_error_received = threading.Event()
        self.subscription_error_code: int | None = None
        self.first_quote_at: datetime | None = None
        self.bid_present = False
        self.offer_present = False

    def onSubscription(self) -> None:  # noqa: N802
        self.subscribed.set()

    def onSubscriptionError(self, code: int, _message: str) -> None:  # noqa: N802
        # The numeric protocol code is safe evidence. Deliberately discard the
        # server message because adapter implementations may include identifiers.
        self.subscription_error_code = int(code)
        self.subscription_error_received.set()

    def onItemUpdate(self, update: Any) -> None:  # noqa: N802
        bid = update.getValue("BID")
        offer = update.getValue("OFFER")
        self.bid_present = bid is not None and str(bid).strip() != ""
        self.offer_present = offer is not None and str(offer).strip() != ""
        if self.bid_present and self.offer_present and self.first_quote_at is None:
            self.first_quote_at = self.clock()
            self.quote_received.set()

    def onClearSnapshot(self, _item_name: str, _item_pos: int) -> None:  # noqa: N802
        return None

    def onCommandSecondLevelItemLostUpdates(  # noqa: N802
        self,
        _lost_updates: int,
        _key: str,
    ) -> None:
        return None

    def onCommandSecondLevelSubscriptionError(  # noqa: N802
        self,
        _code: int,
        _message: str,
        _key: str,
    ) -> None:
        return None

    def onEndOfSnapshot(self, _item_name: str, _item_pos: int) -> None:  # noqa: N802
        return None

    def onItemLostUpdates(  # noqa: N802
        self,
        _item_name: str,
        _item_pos: int,
        _lost_updates: int,
    ) -> None:
        return None

    def onListenEnd(self, _subscription: object) -> None:  # noqa: N802
        return None

    def onListenStart(self, _subscription: object) -> None:  # noqa: N802
        return None

    def onRealMaxFrequency(self, _frequency: str | None) -> None:  # noqa: N802
        return None

    def onUnsubscription(self) -> None:  # noqa: N802
        return None


LightstreamerFactory = Callable[[str, str | None], Any]
SubscriptionFactory = Callable[[str, list[str], list[str]], Any]


def lightstreamer_subscription_error_category(code: int | None) -> str | None:
    """Map a numeric Lightstreamer code without retaining its server message."""

    if code is None:
        return None
    if code <= 0:
        return "METADATA_ADAPTER_REJECTED"
    standard_categories = {
        15: "COMMAND_KEY_FIELD_MISSING",
        16: "COMMAND_FIELD_MISSING",
        17: "DATA_ADAPTER_INVALID",
        21: "GROUP_INVALID",
        22: "GROUP_INVALID_FOR_SCHEMA",
        23: "SCHEMA_INVALID",
        24: "MODE_NOT_ALLOWED",
        25: "SELECTOR_INVALID",
        26: "UNFILTERED_DISPATCH_NOT_ALLOWED",
        27: "UNFILTERED_DISPATCH_NOT_SUPPORTED",
        28: "UNFILTERED_DISPATCH_LICENSE_RESTRICTED",
        29: "RAW_MODE_LICENSE_RESTRICTED",
        30: "SUBSCRIPTIONS_LICENSE_RESTRICTED",
        61: "SERVER_RESPONSE_PARSE_ERROR",
        66: "METADATA_ADAPTER_AUTHORIZATION_EXCEPTION",
        68: "SERVER_INTERNAL_ERROR",
    }
    return standard_categories.get(code, "SERVER_REJECTED")


def create_system_trust_lightstreamer_client(
    endpoint: str,
    adapter_set: str | None,
) -> Any:
    """Create a Lightstreamer client using verified Windows system trust."""

    global _lightstreamer_trust_configured
    with _LIGHTSTREAMER_TRUST_LOCK:
        if not _lightstreamer_trust_configured:
            context = build_system_ssl_context()
            if not context.check_hostname or context.verify_mode != ssl.CERT_REQUIRED:
                raise DiagnosticError(
                    Classification.CODE_DEFECT,
                    "LIGHTSTREAMER_TLS_VERIFICATION_NOT_ENFORCED",
                )
            LightstreamerClient.setTrustManagerFactory(context)
            _lightstreamer_trust_configured = True
    return LightstreamerClient(endpoint, adapter_set)


class StreamingProbe:
    """One-connection, one-price-subscription Lightstreamer probe."""

    def __init__(
        self,
        *,
        endpoint: str,
        account_id: str,
        cst: str,
        xst: str,
        epic: str,
        status_history: list[dict[str, str]],
        client_factory: LightstreamerFactory = create_system_trust_lightstreamer_client,
        subscription_factory: SubscriptionFactory = Subscription,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        parsed = urlparse(endpoint)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise DiagnosticError(
                Classification.STREAMING_HANDSHAKE_FAILURE,
                "LIGHTSTREAMER_ENDPOINT_INVALID",
            )
        if not account_id.strip():
            raise DiagnosticError(
                Classification.STREAMING_HANDSHAKE_FAILURE,
                "LIGHTSTREAMER_ACCOUNT_MISSING",
            )
        self.endpoint_hostname = parsed.hostname
        self.clock = clock
        self.client = client_factory(endpoint, None)
        self.connection_listener = ConnectionListener(status_history, clock=clock)
        self.quote_listener = QuoteListener(clock=clock)
        self.client.connectionDetails.setUser(account_id)
        self.client.connectionDetails.setPassword(build_stream_password(cst, xst))
        self.client.addListener(self.connection_listener)
        self.subscription = subscription_factory(
            "MERGE",
            [f"L1:{epic}"],
            ["UPDATE_TIME", "BID", "OFFER"],
        )
        self.subscription.addListener(self.quote_listener)
        self._subscribed = False

    def connect_and_wait(self, *, connect_timeout: float, quote_timeout: float) -> None:
        self.client.connect()
        if not self.connection_listener.connected.wait(connect_timeout):
            raise DiagnosticError(
                Classification.NETWORK_FAILURE,
                "LIGHTSTREAMER_CONNECTION_TIMEOUT",
            )
        self.client.subscribe(self.subscription)
        self._subscribed = True
        if not self.quote_listener.quote_received.wait(quote_timeout):
            if self.quote_listener.subscription_error_received.is_set():
                reason = "LIGHTSTREAMER_SUBSCRIPTION_REJECTED"
            else:
                reason = "LIGHTSTREAMER_QUOTE_TIMEOUT"
            raise DiagnosticError(Classification.STREAMING_HANDSHAKE_FAILURE, reason)

    def quote_age_seconds(self) -> float | None:
        if self.quote_listener.first_quote_at is None:
            return None
        return max(0.0, (self.clock() - self.quote_listener.first_quote_at).total_seconds())

    def require_fresh_quote(self, maximum_age_seconds: float) -> float:
        age = self.quote_age_seconds()
        if age is None or age > maximum_age_seconds:
            raise DiagnosticError(
                Classification.STREAMING_HANDSHAKE_FAILURE,
                "STALE_PRICE_STREAM",
            )
        return age

    def disconnect_and_wait(self, timeout: float) -> None:
        self.client.disconnect()
        if not self.connection_listener.disconnected.wait(timeout):
            raise DiagnosticError(
                Classification.STREAMING_HANDSHAKE_FAILURE,
                "LIGHTSTREAMER_DISCONNECT_TIMEOUT",
            )


@dataclass
class SessionTokens:
    """In-memory v2 credentials never emitted to evidence."""

    cst: str = field(repr=False)
    xst: str = field(repr=False)
    active_account_id: str = field(repr=False)
    lightstreamer_endpoint: str

    def auth_headers(self) -> dict[str, str]:
        return {"CST": self.cst, "X-SECURITY-TOKEN": self.xst}


def _default_evidence(config: DiagnosticConfig) -> dict[str, Any]:
    return {
        "schema_version": "g1-auth-diagnostic-v1",
        "diagnostic_timestamp_utc": utc_text(),
        "git_commit_sha": _git_commit_sha(),
        "application_version": _application_version(),
        "python_version": sys.version.split()[0],
        "environment": "DEMO",
        "sanitized_base_hostname": DEMO_REST_HOST,
        "session_version": config.session_version,
        "epic": config.epic,
        "preflight": {
            "environment_demo": True,
            "demo_hostname_accepted": True,
            "live_hostname_absent": True,
            "required_configuration_present": True,
            "logging_redaction_enabled": True,
            "paper_trading_true": config.paper_trading,
        },
        "http_status_history": [],
        "ig_errorCode_history": [],
        "account": {
            "active_account_id_masked": None,
            "configured_account_id_masked": None,
            "account_match": False,
            "demo_account_confirmed": False,
            "configured_account_exists": False,
            "preferred_account_set": False,
            "preferred_account_enabled": False,
            "account_type": None,
            "dealing_status": None,
        },
        "tokens": {"cst_present": False, "x_security_token_present": False},
        "transport_security": {
            "rest_tls_trust": "WINDOWS_SYSTEM_TRUST",
            "lightstreamer_tls_trust": "WINDOWS_SYSTEM_TRUST",
            "certificate_verification": "CERT_REQUIRED",
            "hostname_verification": True,
        },
        "lightstreamer": {
            "endpoint_hostname": None,
            "subscription_status": "NOT_ATTEMPTED",
            "subscription_error_code": None,
            "subscription_error_category": None,
            "first_quote_timestamp_utc": None,
            "bid_present": False,
            "offer_present": False,
            "quote_age_seconds": None,
            "connection_status_history": [],
            "forced_reconnect_result": "NOT_ATTEMPTED",
            "active_connection_high_watermark": 0,
            "failure_cleanup": "NOT_REQUIRED",
        },
        "fault_injection_coverage": {
            "invalid_token": "AUTOMATED_TEST",
            "rest_401": "AUTOMATED_TEST",
            "connection_timeout": "AUTOMATED_TEST",
            "malformed_response": "AUTOMATED_TEST",
            "lightstreamer_disconnect": "LIVE_FORCED_OR_AUTOMATED_TEST",
        },
        "bounded_reauthentication": {"limit": config.max_reauth_attempts, "count": 0},
        "session_cleanup": "NOT_REQUIRED",
        "secret_scan_result": "NOT_RUN",
        "order_endpoint_call_count": 0,
        "blocked_endpoint_attempt_count": 0,
        "final_classification": Classification.INCONCLUSIVE.value,
        "remaining_blocker": None,
    }


class DiagnosticRunner:
    """Orchestrate the independent read-only REST and streaming proof."""

    def __init__(
        self,
        config: DiagnosticConfig,
        *,
        rest_client: SafeRestClient | None = None,
        stream_factory: Callable[..., StreamingProbe] = StreamingProbe,
        fingerprint_key: bytes | None = None,
    ) -> None:
        self.config = config
        self.evidence = _default_evidence(config)
        if rest_client is None:
            self.rest = SafeRestClient(
                config,
                self.evidence["http_status_history"],
            )
        else:
            self.rest = rest_client
            self.evidence["http_status_history"] = rest_client.request_history
        self.stream_factory = stream_factory
        self.fingerprint_key = fingerprint_key or os.urandom(32)
        self.tokens: SessionTokens | None = None
        self.reauth_count = 0

    def _authenticate(self, *, recovery: bool) -> SessionTokens:
        if recovery:
            if self.reauth_count >= self.config.max_reauth_attempts:
                raise DiagnosticError(
                    Classification.CONFIGURATION_DEFECT,
                    "REAUTHENTICATION_LIMIT_EXHAUSTED",
                )
            self.reauth_count += 1
            self.evidence["bounded_reauthentication"]["count"] = self.reauth_count
        result = self.rest.request_json(
            "POST",
            "/session",
            version="2",
            body={
                "identifier": self.config.identifier,
                "password": self.config.password,
                "encryptedPassword": False,
            },
        )
        payload = require_success(result, "SESSION_CREATION_FAILED")
        cst = result.headers.get("CST", "").strip()
        xst = result.headers.get("X-SECURITY-TOKEN", "").strip()
        self.evidence["tokens"] = {
            "cst_present": bool(cst),
            "x_security_token_present": bool(xst),
        }
        if not cst:
            raise DiagnosticError(
                Classification.STREAMING_HANDSHAKE_FAILURE,
                "CST_MISSING",
            )
        if not xst:
            raise DiagnosticError(
                Classification.STREAMING_HANDSHAKE_FAILURE,
                "X_SECURITY_TOKEN_MISSING",
            )
        active_account_id = payload.get("currentAccountId") or payload.get("accountId")
        endpoint = payload.get("lightstreamerEndpoint")
        if not isinstance(active_account_id, str) or not active_account_id.strip():
            raise DiagnosticError(Classification.IG_PLATFORM_FAILURE, "ACTIVE_ACCOUNT_MISSING")
        if not isinstance(endpoint, str) or not endpoint.strip():
            raise DiagnosticError(
                Classification.STREAMING_HANDSHAKE_FAILURE,
                "LIGHTSTREAMER_ENDPOINT_MISSING",
            )
        account = self.evidence["account"]
        account["active_account_id_masked"] = _account_fingerprint(
            active_account_id,
            self.fingerprint_key,
        )
        account["configured_account_id_masked"] = _account_fingerprint(
            self.config.account_id,
            self.fingerprint_key,
        )
        account["account_match"] = active_account_id == self.config.account_id
        account["demo_account_confirmed"] = (
            str(payload.get("reroutingEnvironment", "DEMO")).upper() != "LIVE"
            and payload.get("hasActiveDemoAccounts") is not False
        )
        if not account["demo_account_confirmed"]:
            raise DiagnosticError(
                Classification.ACCOUNT_RESTRICTION,
                "DEMO_ACCOUNT_NOT_CONFIRMED",
            )
        if not account["account_match"]:
            raise DiagnosticError(Classification.CONFIGURATION_DEFECT, "ACCOUNT_MISMATCH")
        return SessionTokens(cst, xst, active_account_id, endpoint)

    def _authorized_get(self, path: str, *, version: str) -> Mapping[str, Any]:
        if self.tokens is None:
            raise DiagnosticError(Classification.CODE_DEFECT, "SESSION_TOKENS_UNAVAILABLE")
        result = self.rest.request_json(
            "GET",
            path,
            version=version,
            headers=self.tokens.auth_headers(),
        )
        if result.status_code == 401:
            self.tokens = self._authenticate(recovery=True)
            result = self.rest.request_json(
                "GET",
                path,
                version=version,
                headers=self.tokens.auth_headers(),
            )
        return require_success(result, f"READ_ONLY_REQUEST_FAILED:{sanitize_path(path)}")

    def _prove_accounts(self) -> None:
        if self.tokens is None:
            raise DiagnosticError(Classification.CODE_DEFECT, "SESSION_TOKENS_UNAVAILABLE")
        session = self._authorized_get("/session", version="1")
        if session.get("accountId") != self.tokens.active_account_id:
            raise DiagnosticError(Classification.CONFIGURATION_DEFECT, "ACTIVE_ACCOUNT_CHANGED")
        accounts_payload = self._authorized_get("/accounts", version="1")
        accounts = accounts_payload.get("accounts")
        if not isinstance(accounts, list):
            raise DiagnosticError(Classification.IG_PLATFORM_FAILURE, "ACCOUNTS_RESPONSE_INVALID")
        configured = next(
            (
                item
                for item in accounts
                if isinstance(item, Mapping) and item.get("accountId") == self.config.account_id
            ),
            None,
        )
        account_evidence = self.evidence["account"]
        account_evidence["configured_account_exists"] = configured is not None
        if configured is None:
            raise DiagnosticError(
                Classification.CONFIGURATION_DEFECT,
                "CONFIGURED_ACCOUNT_NOT_FOUND",
            )
        preferred = [
            item for item in accounts if isinstance(item, Mapping) and item.get("preferred") is True
        ]
        account_evidence["preferred_account_set"] = bool(preferred)
        if not preferred:
            raise DiagnosticError(Classification.CONFIGURATION_DEFECT, "PREFERRED_ACCOUNT_NOT_SET")
        if configured.get("preferred") is not True:
            raise DiagnosticError(
                Classification.CONFIGURATION_DEFECT,
                "CONFIGURED_ACCOUNT_NOT_PREFERRED",
            )
        status = configured.get("status")
        dealing_enabled = configured.get("dealingEnabled")
        enabled = dealing_enabled is not False and str(status).upper() not in {
            "DISABLED",
            "SUSPENDED",
            "CLOSED",
        }
        account_evidence["preferred_account_enabled"] = enabled
        account_evidence["account_type"] = configured.get("accountType")
        account_evidence["dealing_status"] = status if status is not None else dealing_enabled
        if not enabled:
            raise DiagnosticError(Classification.ACCOUNT_RESTRICTION, "PREFERRED_ACCOUNT_DISABLED")
        self._authorized_get(f"/markets/{self.config.epic}", version="3")

    def _logout(self) -> None:
        if self.tokens is None:
            return
        result = self.rest.request_json(
            "DELETE",
            "/session",
            version="1",
            headers=self.tokens.auth_headers(),
            attempts=1,
        )
        if result.status_code not in {200, 204}:
            raise DiagnosticError(
                classify_http_failure(result.status_code, result.error_code),
                "SESSION_LOGOUT_FAILED",
                http_status=result.status_code,
                error_code=result.error_code,
            )
        self.tokens = None
        self.evidence["session_cleanup"] = "LOGGED_OUT"

    def _probe_stream(self) -> StreamingProbe:
        if self.tokens is None:
            raise DiagnosticError(Classification.CODE_DEFECT, "SESSION_TOKENS_UNAVAILABLE")
        statuses = self.evidence["lightstreamer"]["connection_status_history"]
        probe = self.stream_factory(
            endpoint=self.tokens.lightstreamer_endpoint,
            account_id=self.tokens.active_account_id,
            cst=self.tokens.cst,
            xst=self.tokens.xst,
            epic=self.config.epic,
            status_history=statuses,
        )
        stream = self.evidence["lightstreamer"]
        stream["endpoint_hostname"] = probe.endpoint_hostname
        stream["active_connection_high_watermark"] = max(
            stream["active_connection_high_watermark"],
            1,
        )
        try:
            probe.connect_and_wait(
                connect_timeout=self.config.connect_timeout_seconds,
                quote_timeout=self.config.quote_timeout_seconds,
            )
        except DiagnosticError:
            listener = probe.quote_listener
            if listener.subscription_error_received.is_set():
                stream["subscription_status"] = "REJECTED"
                stream["subscription_error_code"] = listener.subscription_error_code
                stream["subscription_error_category"] = lightstreamer_subscription_error_category(
                    listener.subscription_error_code,
                )
            elif listener.subscribed.is_set():
                stream["subscription_status"] = "SUBSCRIBED_NO_QUOTE"
            elif probe._subscribed:
                stream["subscription_status"] = "REQUESTED_NO_ACK"
            try:
                if probe.connection_listener.connected.is_set():
                    probe.disconnect_and_wait(self.config.disconnect_timeout_seconds)
                else:
                    probe.client.disconnect()
                stream["failure_cleanup"] = "DISCONNECTED"
            except Exception:
                stream["failure_cleanup"] = "FAILED_SANITIZED"
            raise
        age = probe.require_fresh_quote(self.config.maximum_quote_age_seconds)
        listener = probe.quote_listener
        stream["subscription_status"] = "SUBSCRIBED_QUOTE_RECEIVED"
        stream["first_quote_timestamp_utc"] = utc_text(listener.first_quote_at)
        stream["bid_present"] = listener.bid_present
        stream["offer_present"] = listener.offer_present
        stream["quote_age_seconds"] = round(age, 6)
        return probe

    def _populate_histories(self) -> None:
        self.evidence["ig_errorCode_history"] = [
            {
                "timestamp_utc": item["timestamp_utc"],
                "http_status": item["http_status"],
                "ig_errorCode": item["ig_errorCode"],
            }
            for item in self.evidence["http_status_history"]
        ]

    def run(self) -> dict[str, Any]:
        first_probe: StreamingProbe | None = None
        second_probe: StreamingProbe | None = None
        try:
            self.tokens = self._authenticate(recovery=False)
            self._prove_accounts()
            first_probe = self._probe_stream()
            first_probe.disconnect_and_wait(self.config.disconnect_timeout_seconds)
            first_probe = None
            self._logout()

            self.tokens = self._authenticate(recovery=True)
            self._prove_accounts()
            second_probe = self._probe_stream()
            self.evidence["lightstreamer"]["forced_reconnect_result"] = (
                "REAUTHENTICATED_AND_SUBSCRIPTION_RESTORED"
            )
            second_probe.disconnect_and_wait(self.config.disconnect_timeout_seconds)
            second_probe = None
            self._logout()
            self.evidence["final_classification"] = Classification.PASS.value
        except DiagnosticError as exc:
            self.evidence["final_classification"] = exc.classification.value
            self.evidence["remaining_blocker"] = exc.reason
            if exc.http_status is not None:
                self.evidence["failure_http_status"] = exc.http_status
            if exc.error_code is not None:
                self.evidence["failure_ig_errorCode"] = exc.error_code
        except Exception:
            self.evidence["final_classification"] = Classification.CODE_DEFECT.value
            self.evidence["remaining_blocker"] = "UNEXPECTED_DIAGNOSTIC_EXCEPTION"
        finally:
            for probe in (first_probe, second_probe):
                if probe is not None:
                    probe.client.disconnect()
            if self.tokens is not None:
                try:
                    self._logout()
                except DiagnosticError:
                    self.evidence["session_cleanup"] = "FAILED_SANITIZED"
            self.rest.close()
            self.evidence["order_endpoint_call_count"] = self.rest.order_endpoint_call_count
            self.evidence["blocked_endpoint_attempt_count"] = (
                self.rest.blocked_endpoint_attempt_count
            )
            self._populate_histories()
        return self.evidence


def _secret_scan(document: Mapping[str, Any], secrets: tuple[str, ...]) -> str:
    serialized = json.dumps(document, ensure_ascii=True, sort_keys=True)
    if any(secret and secret in serialized for secret in secrets):
        return "FAIL_SECRET_VALUE_PRESENT"
    forbidden_labels = (
        '"password":',
        '"api_key":',
        '"cst":',
        '"xst":',
        '"access_token":',
        '"refresh_token":',
    )
    if any(label in serialized.lower() for label in forbidden_labels):
        return "FAIL_SECRET_FIELD_PRESENT"
    return "PASS"


def _markdown_report(document: Mapping[str, Any]) -> str:
    account = document["account"]
    tokens = document["tokens"]
    transport = document["transport_security"]
    stream = document["lightstreamer"]
    lines = [
        "# G1-01 IG Demo read-only authentication diagnostic",
        "",
        f"- Diagnostic timestamp UTC: {document['diagnostic_timestamp_utc']}",
        f"- Git commit SHA: `{document['git_commit_sha']}`",
        f"- Application version: `{document['application_version']}`",
        f"- Python version: `{document['python_version']}`",
        f"- Environment: `{document['environment']}`",
        f"- Base hostname: `{document['sanitized_base_hostname']}`",
        f"- Session version: `{document['session_version']}`",
        f"- EPIC: `{document['epic']}`",
        f"- Configured account: `{account['configured_account_id_masked']}`",
        f"- Active account: `{account['active_account_id_masked']}`",
        f"- Account match: `{account['account_match']}`",
        f"- Demo account confirmed: `{account['demo_account_confirmed']}`",
        f"- CST present: `{tokens['cst_present']}`",
        f"- X-SECURITY-TOKEN present: `{tokens['x_security_token_present']}`",
        f"- Lightstreamer TLS trust: `{transport['lightstreamer_tls_trust']}`",
        f"- Certificate verification: `{transport['certificate_verification']}`",
        f"- Hostname verification: `{transport['hostname_verification']}`",
        f"- Lightstreamer status: `{stream['subscription_status']}`",
        f"- First quote UTC: `{stream['first_quote_timestamp_utc']}`",
        f"- Forced reconnect: `{stream['forced_reconnect_result']}`",
        f"- Session cleanup: `{document['session_cleanup']}`",
        f"- Secret scan: `{document['secret_scan_result']}`",
        f"- Order endpoint call count: `{document['order_endpoint_call_count']}`",
        f"- Final classification: `{document['final_classification']}`",
        f"- Remaining blocker: `{document['remaining_blocker']}`",
        "",
        "## Sanitized HTTP history",
        "",
        "```json",
        json.dumps(document["http_status_history"], indent=2, sort_keys=True),
        "```",
        "",
        "## Lightstreamer status history",
        "",
        "```json",
        json.dumps(stream["connection_status_history"], indent=2, sort_keys=True),
        "```",
        "",
    ]
    return "\n".join(lines)


def write_reports(
    document: dict[str, Any],
    output: Path,
    *,
    secrets: tuple[str, ...],
) -> tuple[Path, Path]:
    """Leak-scan and write paired sanitized JSON and Markdown reports."""

    document["secret_scan_result"] = _secret_scan(document, secrets)
    if document["secret_scan_result"] != "PASS":
        document["final_classification"] = Classification.CODE_DEFECT.value
        document["remaining_blocker"] = "SECRET_SCAN_FAILED_REPORT_NOT_WRITTEN"
        raise DiagnosticError(Classification.CODE_DEFECT, "SECRET_SCAN_FAILED")
    output = output.resolve()
    markdown = output.with_suffix(".md")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(document, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    markdown.write_text(_markdown_report(document), encoding="utf-8")
    return output, markdown


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="IG Demo v2 read-only REST and Lightstreamer diagnostic",
    )
    parser.add_argument("--environment", required=True, choices=("demo",))
    parser.add_argument("--session-version", required=True, type=int, choices=(2,))
    parser.add_argument("--epic", default=DEFAULT_EPIC)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--connect-timeout", type=float, default=15.0)
    parser.add_argument("--quote-timeout", type=float, default=20.0)
    parser.add_argument("--disconnect-timeout", type=float, default=10.0)
    parser.add_argument("--maximum-quote-age", type=float, default=5.0)
    return parser


def _configuration_failure_document(
    args: argparse.Namespace,
    error: DiagnosticError,
) -> dict[str, Any]:
    hostname_failure_reasons = {
        "LIVE_CREDENTIAL_CONFIGURATION_PRESENT",
        "DEMO_HOSTNAME_REQUIRED",
        "REST_BASE_URL_INVALID",
        "REST_BASE_PATH_INVALID",
    }
    demo_hostname_accepted = error.reason not in hostname_failure_reasons
    return {
        "schema_version": "g1-auth-diagnostic-v1",
        "diagnostic_timestamp_utc": utc_text(),
        "git_commit_sha": _git_commit_sha(),
        "application_version": _application_version(),
        "python_version": sys.version.split()[0],
        "environment": str(args.environment).upper(),
        "sanitized_base_hostname": DEMO_REST_HOST if demo_hostname_accepted else None,
        "session_version": int(args.session_version),
        "epic": str(args.epic),
        "preflight": {
            "environment_demo": str(args.environment).lower() == "demo",
            "demo_hostname_accepted": demo_hostname_accepted,
            "live_hostname_absent": demo_hostname_accepted,
            "required_configuration_present": not error.reason.startswith(
                "MISSING_REQUIRED_CONFIG"
            ),
            "logging_redaction_enabled": True,
            "paper_trading_true": error.reason != "PAPER_TRADING_MUST_BE_TRUE",
        },
        "http_status_history": [],
        "ig_errorCode_history": [],
        "account": {
            "active_account_id_masked": None,
            "configured_account_id_masked": None,
            "account_match": False,
            "demo_account_confirmed": False,
            "configured_account_exists": False,
            "preferred_account_set": False,
            "preferred_account_enabled": False,
            "account_type": None,
            "dealing_status": None,
        },
        "tokens": {"cst_present": False, "x_security_token_present": False},
        "transport_security": {
            "rest_tls_trust": "WINDOWS_SYSTEM_TRUST",
            "lightstreamer_tls_trust": "WINDOWS_SYSTEM_TRUST",
            "certificate_verification": "CERT_REQUIRED",
            "hostname_verification": True,
        },
        "lightstreamer": {
            "endpoint_hostname": None,
            "subscription_status": "NOT_ATTEMPTED",
            "subscription_error_code": None,
            "subscription_error_category": None,
            "first_quote_timestamp_utc": None,
            "bid_present": False,
            "offer_present": False,
            "quote_age_seconds": None,
            "connection_status_history": [],
            "forced_reconnect_result": "NOT_ATTEMPTED",
            "active_connection_high_watermark": 0,
            "failure_cleanup": "NOT_REQUIRED",
        },
        "fault_injection_coverage": {
            "invalid_token": "AUTOMATED_TEST",
            "rest_401": "AUTOMATED_TEST",
            "connection_timeout": "AUTOMATED_TEST",
            "malformed_response": "AUTOMATED_TEST",
            "lightstreamer_disconnect": "AUTOMATED_TEST",
        },
        "bounded_reauthentication": {"limit": 2, "count": 0},
        "session_cleanup": "NOT_REQUIRED",
        "secret_scan_result": "NOT_RUN",
        "order_endpoint_call_count": 0,
        "blocked_endpoint_attempt_count": 0,
        "final_classification": error.classification.value,
        "remaining_blocker": error.reason,
    }


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if any(
        value <= 0
        for value in (
            args.connect_timeout,
            args.quote_timeout,
            args.disconnect_timeout,
            args.maximum_quote_age,
        )
    ):
        parser.error("timeouts and maximum quote age must be positive")
    try:
        config = load_config(args)
    except DiagnosticError as exc:
        document = _configuration_failure_document(args, exc)
        try:
            json_path, markdown_path = write_reports(document, args.output, secrets=())
        except DiagnosticError:
            return 2
        print(f"classification={document['final_classification']}")
        print(f"json_report={json_path}")
        print(f"markdown_report={markdown_path}")
        return 1

    document = DiagnosticRunner(config).run()
    try:
        json_path, markdown_path = write_reports(
            document,
            config.output,
            secrets=config.secret_values(),
        )
    except DiagnosticError:
        print("classification=CODE_DEFECT")
        print("report_write=BLOCKED_BY_SECRET_SCAN")
        return 2
    if document["final_classification"] not in FINAL_CLASSIFICATIONS:
        print("classification=CODE_DEFECT")
        return 2
    print(f"classification={document['final_classification']}")
    print(f"json_report={json_path}")
    print(f"markdown_report={markdown_path}")
    return 0 if document["final_classification"] == Classification.PASS.value else 1


if __name__ == "__main__":
    raise SystemExit(main())
