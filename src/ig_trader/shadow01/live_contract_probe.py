"""One bounded, non-persisting Gate 09 IG Demo contract probe.

This probe exists solely to capture value-free runtime field shapes before a
parser admits a new broker representation.  It deliberately does not open a
Shadow store, cache a response, create an epoch, construct a decision, or
reach any execution surface.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from src.ig_trader.shadow01.config import DEFAULT_CONFIG_PATH, ShadowTournamentConfig, load_config
from src.ig_trader.shadow01.data import history_row_contract_diagnostic
from src.ig_trader.shadow01.local_demo_read_only import (
    LocalDemoReadOnlyStatus,
    Shadow01LocalDemoReadOnlyFactory,
)
from src.ig_trader.shadow01.local_demo_stream_transport import (
    Shadow01LocalDemoReadOnlyStreamFactory,
)
from src.ig_trader.shadow01.read_only_broker import (
    ReadOnlyBrokerRequestCounters,
    Shadow01ReadOnlyBroker,
)
from src.ig_trader.shadow01.registry import (
    ShadowMarketRegistry,
    load_verified_dq03_registry,
    require_exact_twenty,
)
from src.ig_trader.shadow01.stream_bridge import ShadowReadOnlyStreamBridge

_AUTHORIZATION = "SHADOW01_GATE09_LIVE_CONTRACT_PROBE"
_SYMBOL = "EURUSD"
_HISTORY_POINTS = 5
_STREAM_WAIT_SECONDS = 5.0
_SAFE_REASON = re.compile(r"SHADOW01_[A-Z0-9_]+\Z")
DEFAULT_DQ03_REGISTRY_PATH = (
    DEFAULT_CONFIG_PATH.parent / "artifacts" / "dq03" / "instrument_registry.json"
)


@dataclass(frozen=True)
class Gate09LiveContractProbeResult:
    """Sanitized facts from at most one Gate 09 field-type proof probe."""

    status: str
    preflight_passed: bool
    auth_result: str
    history_result: str
    history_row_contract: dict[str, object] | None
    stream_result: str
    stream_callback_contracts: tuple[dict[str, object], ...] = ()
    stream_invalid_reason_counts: tuple[tuple[str, int], ...] = ()
    stream_quote_quality: str | None = None
    stream_unsubscribe_result: str = "NOT_REQUIRED"
    stream_disconnect_result: str = "NOT_REQUIRED"
    rest_logout_result: str = "NOT_REQUIRED"
    cleanup_passed: bool = False
    counters: ReadOnlyBrokerRequestCounters = field(
        default_factory=ReadOnlyBrokerRequestCounters.zero
    )
    error_code: str | None = None

    def document(self) -> dict[str, object]:
        """Return only non-secret facts; source values never leave this object."""

        return {
            "status": self.status,
            "scope": {
                "symbol": _SYMBOL,
                "history_resolution": "DAY",
                "history_points": _HISTORY_POINTS,
                "stream_wait_seconds": _STREAM_WAIT_SECONDS,
                "execution_authority": "OFF",
                "tournament_epoch_created": False,
                "prospective_decisions": 0,
                "outcomes": 0,
            },
            "preflight_passed": self.preflight_passed,
            "auth": self.auth_result,
            "history": {
                "result": self.history_result,
                "row_contract": self.history_row_contract,
            },
            "stream": {
                "result": self.stream_result,
                "callback_contracts": list(self.stream_callback_contracts),
                "invalid_reason_counts": dict(self.stream_invalid_reason_counts),
                "quote_quality": self.stream_quote_quality,
            },
            "cleanup": {
                "unsubscribe": self.stream_unsubscribe_result,
                "disconnect": self.stream_disconnect_result,
                "rest_logout": self.rest_logout_result,
                "passed": self.cleanup_passed,
            },
            "ig_counters": {
                "auth": self.counters.authentication_request_count,
                "account_reads": self.counters.account_read_count,
                "market_metadata_reads": self.counters.market_read_count,
                "historical_reads": self.counters.historical_price_read_count,
                "stream_subscriptions": 1 if self.stream_result != "NOT_RUN" else 0,
                "session_logouts": self.counters.session_logout_count,
            },
            "execution": {
                **self.counters.execution_safety_document(),
                "execution_authority": "OFF",
                "live_actions": 0,
                "azure_actions": 0,
            },
            "error_code": self.error_code,
        }


class Gate09LiveContractProbe:
    """Perform the single authorized live contract proof with guaranteed cleanup."""

    def __init__(
        self,
        *,
        authorization: str,
        rest_factory: object | None = None,
        stream_factory: object | None = None,
        config_loader: Callable[[], ShadowTournamentConfig] = load_config,
        registry_loader: Callable[[ShadowTournamentConfig, Path], ShadowMarketRegistry] = (
            load_verified_dq03_registry
        ),
        registry_path: Path = DEFAULT_DQ03_REGISTRY_PATH,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._authorization = authorization
        self._rest_factory = rest_factory or Shadow01LocalDemoReadOnlyFactory()
        self._stream_factory = stream_factory or Shadow01LocalDemoReadOnlyStreamFactory()
        self._config_loader = config_loader
        self._registry_loader = registry_loader
        self._registry_path = registry_path
        self._now = now

    def run(self) -> Gate09LiveContractProbeResult:
        """Run one bounded proof; never persist payloads or start Shadow runtime."""

        broker: Shadow01ReadOnlyBroker | None = None
        bridge: ShadowReadOnlyStreamBridge | None = None
        authenticated = False
        preflight_passed = False
        auth_result = "NOT_RUN"
        history_result = "NOT_RUN"
        history_row: dict[str, object] | None = None
        stream_result = "NOT_RUN"
        stream_contracts: tuple[dict[str, object], ...] = ()
        stream_counts: tuple[tuple[str, int], ...] = ()
        quote_quality: str | None = None
        unsubscribe_result = "NOT_REQUIRED"
        disconnect_result = "NOT_REQUIRED"
        logout_result = "NOT_REQUIRED"
        error_code: str | None = None

        try:
            if self._authorization != _AUTHORIZATION:
                raise _ProbeBlocked("SHADOW01_BLOCKED_GATE09_AUTHORIZATION_REQUIRED")
            config = self._config_loader()
            _require_frozen_config(config)
            registry = self._registry_loader(config, self._registry_path)
            _require_registry(registry)
            market = registry.by_symbol(_SYMBOL)
            if not isinstance(market.epic, str) or not market.epic:
                raise _ProbeBlocked("SHADOW01_BLOCKED_GATE09_EURUSD_IDENTITY_UNVERIFIED")
            epic = market.epic
            _require_ready_read_only_factory(self._rest_factory)
            _require_ready_read_only_factory(self._stream_factory)
            broker = _build_broker(self._rest_factory)
            if broker.execution_authority != "OFF":
                raise _ProbeBlocked("SHADOW01_BLOCKED_EXECUTION_AUTHORITY_VIOLATION")
            preflight_passed = True

            broker.authenticate()
            authenticated = True
            auth_result = "PASS"
            history_document = broker.read_historical_prices(epic, "DAY", _HISTORY_POINTS)
            history_row = _first_history_row_contract(history_document)
            history_result = "PASS"
            stream_material = broker.stream_session_material()
            bridge = _build_bridge(self._stream_factory, registry, stream_material)
            if bridge.execution_authority != "OFF" or bridge.connected or bridge.subscribed_epics:
                raise _ProbeBlocked("SHADOW01_BLOCKED_READ_ONLY_BOUNDARY_UNVERIFIED")
            bridge.connect()
            bridge.subscribe_prices((epic,))
            quote = bridge.receive_price_update(
                observed_at=_require_utc(self._now()),
                maximum_age_seconds=_maximum_quote_age_seconds(config),
                timeout_seconds=_STREAM_WAIT_SECONDS,
            )
            stream_contracts = bridge.field_contract_diagnostics
            stream_counts = tuple(sorted(bridge.invalid_reason_counts.items()))
            if quote is None:
                stream_result = "NO_CALLBACK"
            else:
                quote_quality = quote.quality
                stream_result = "PASS"
        except _ProbeBlocked as error:
            error_code = error.reason_code
        except Exception as error:
            error_code = _safe_reason(error, "SHADOW01_BLOCKED_GATE09_CONTRACT_PROBE_FAILED")
        finally:
            if bridge is not None:
                if bridge.connected and bridge.subscribed_epics:
                    try:
                        bridge.unsubscribe_prices(bridge.subscribed_epics)
                        unsubscribe_result = "PASS"
                    except Exception:
                        unsubscribe_result = "FAILED"
                if bridge.connected:
                    try:
                        bridge.disconnect()
                        disconnect_result = "PASS"
                    except Exception:
                        disconnect_result = "FAILED"
            if broker is not None and authenticated:
                try:
                    logout_result = "PASS" if broker.logout() is True else "FAILED"
                except Exception:
                    logout_result = "FAILED"

        counters = (
            broker.request_counters if broker is not None else ReadOnlyBrokerRequestCounters.zero()
        )
        cleanup_passed = (
            bridge is None or (unsubscribe_result == "PASS" and disconnect_result == "PASS")
        ) and (not authenticated or logout_result == "PASS")
        if not cleanup_passed and error_code is None:
            error_code = "SHADOW01_BLOCKED_GATE09_CLEANUP_FAILED"
        if error_code is None and stream_result == "NO_CALLBACK":
            error_code = "SHADOW01_BLOCKED_GATE09_STREAM_CALLBACK_UNAVAILABLE"
        status = "SHADOW01_GATE09_LIVE_CONTRACT_PROBE_PASS" if error_code is None else error_code
        return Gate09LiveContractProbeResult(
            status=status,
            preflight_passed=preflight_passed,
            auth_result=auth_result,
            history_result=history_result,
            history_row_contract=history_row,
            stream_result=stream_result,
            stream_callback_contracts=stream_contracts,
            stream_invalid_reason_counts=stream_counts,
            stream_quote_quality=quote_quality,
            stream_unsubscribe_result=unsubscribe_result,
            stream_disconnect_result=disconnect_result,
            rest_logout_result=logout_result,
            cleanup_passed=cleanup_passed,
            counters=counters,
            error_code=error_code,
        )


class _ProbeBlocked(RuntimeError):
    def __init__(self, reason_code: str) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code


def _require_frozen_config(config: object) -> None:
    if (
        not isinstance(config, ShadowTournamentConfig)
        or not config.fingerprint_is_valid
        or config.payload.get("execution_authority") != "OFF"
    ):
        raise _ProbeBlocked("SHADOW01_BLOCKED_FROZEN_CONFIG_UNVERIFIED")


def _require_registry(registry: object) -> None:
    if not isinstance(registry, ShadowMarketRegistry):
        raise _ProbeBlocked("SHADOW01_BLOCKED_DQ03_REGISTRY_UNVERIFIED")
    try:
        markets = require_exact_twenty(registry)
    except Exception:
        raise _ProbeBlocked("SHADOW01_BLOCKED_DQ03_REGISTRY_UNVERIFIED") from None
    if (
        registry.verified_count != 20
        or registry.unavailable_count != 0
        or any(market.epic is None for market in markets)
    ):
        raise _ProbeBlocked("SHADOW01_BLOCKED_DQ03_REGISTRY_UNVERIFIED")


def _require_ready_read_only_factory(factory: object) -> None:
    status = getattr(factory, "status", None)
    if not callable(status):
        raise _ProbeBlocked("SHADOW01_BLOCKED_DEMO_READ_ONLY_STATUS_UNAVAILABLE")
    try:
        value = status()
    except Exception:
        raise _ProbeBlocked("SHADOW01_BLOCKED_DEMO_READ_ONLY_STATUS_UNAVAILABLE") from None
    if not isinstance(value, LocalDemoReadOnlyStatus):
        raise _ProbeBlocked("SHADOW01_BLOCKED_DEMO_READ_ONLY_STATUS_UNAVAILABLE")
    if not value.ready:
        raise _ProbeBlocked(_blocked_reason(value.reason_code))
    if value.execution_authority != "OFF":
        raise _ProbeBlocked("SHADOW01_BLOCKED_EXECUTION_AUTHORITY_VIOLATION")


def _build_broker(factory: object) -> Shadow01ReadOnlyBroker:
    build = getattr(factory, "build", None)
    if not callable(build):
        raise _ProbeBlocked("SHADOW01_BLOCKED_READ_ONLY_FACTORY_UNAVAILABLE")
    try:
        broker = build()
    except Exception as error:
        raise _ProbeBlocked(
            _blocked_reason(_safe_reason(error, "SHADOW01_READ_ONLY_BUILD_FAILED"))
        ) from None
    if not isinstance(broker, Shadow01ReadOnlyBroker):
        raise _ProbeBlocked("SHADOW01_BLOCKED_READ_ONLY_BROKER_UNVERIFIED")
    return broker


def _build_bridge(
    factory: object,
    registry: ShadowMarketRegistry,
    session_material: object,
) -> ShadowReadOnlyStreamBridge:
    build = getattr(factory, "build", None)
    if not callable(build):
        raise _ProbeBlocked("SHADOW01_BLOCKED_STREAM_FACTORY_UNAVAILABLE")
    try:
        bridge = build(registry, session_material=session_material, max_reconnect_attempts=1)
    except Exception as error:
        raise _ProbeBlocked(
            _blocked_reason(_safe_reason(error, "SHADOW01_STREAM_CONSTRUCTION_UNAVAILABLE"))
        ) from None
    if not isinstance(bridge, ShadowReadOnlyStreamBridge):
        raise _ProbeBlocked("SHADOW01_BLOCKED_STREAM_BRIDGE_UNVERIFIED")
    return bridge


def _first_history_row_contract(document: object) -> dict[str, object]:
    if not isinstance(document, Mapping):
        raise _ProbeBlocked("SHADOW01_BLOCKED_GATE09_HISTORY_RESPONSE_INVALID")
    rows = document.get("prices")
    if not isinstance(rows, list) or not rows:
        raise _ProbeBlocked("SHADOW01_BLOCKED_GATE09_HISTORY_ROW_UNAVAILABLE")
    for row in rows:
        if isinstance(row, Mapping):
            return history_row_contract_diagnostic(row)
    raise _ProbeBlocked("SHADOW01_BLOCKED_GATE09_HISTORY_ROW_UNAVAILABLE")


def _maximum_quote_age_seconds(config: ShadowTournamentConfig) -> int:
    quality = config.payload.get("quality")
    value = quality.get("maximum_price_age_seconds") if isinstance(quality, Mapping) else None
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise _ProbeBlocked("SHADOW01_BLOCKED_QUOTE_FRESHNESS_CONFIG_INVALID")
    return value


def _require_utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise _ProbeBlocked("SHADOW01_BLOCKED_DIAGNOSTIC_CLOCK_INVALID")
    return value.astimezone(UTC)


def _safe_reason(error: BaseException, fallback: str) -> str:
    candidate = str(error)
    return candidate if _SAFE_REASON.fullmatch(candidate) is not None else fallback


def _blocked_reason(reason: str) -> str:
    if reason.startswith("SHADOW01_BLOCKED_"):
        return reason
    suffix = reason.removeprefix("SHADOW01_")
    return f"SHADOW01_BLOCKED_{suffix}"


__all__ = (
    "Gate09LiveContractProbe",
    "Gate09LiveContractProbeResult",
)
