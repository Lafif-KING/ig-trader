"""One bounded, value-safe Gate 13 IG Demo V2 declared-hours probe."""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from src.ig_trader.shadow01.config import DEFAULT_CONFIG_PATH, ShadowTournamentConfig, load_config
from src.ig_trader.shadow01.live_contract_probe import _build_broker, _require_frozen_config
from src.ig_trader.shadow01.local_demo_read_only import (
    LocalDemoReadOnlyStatus,
    Shadow01LocalDemoReadOnlyFactory,
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

_AUTHORIZATION = "SHADOW01_GATE13_V2_DECLARED_HOURS_PROBE"
_EURUSD = "EURUSD"
_ALLOWANCE_CODE = "error.public-api.exceeded-api-key-allowance"
_SAFE_REASON = re.compile(r"SHADOW01_[A-Z0-9_]+\Z")
DEFAULT_DQ03_REGISTRY_PATH = (
    DEFAULT_CONFIG_PATH.parent / "artifacts" / "dq03" / "instrument_registry.json"
)


@dataclass(frozen=True)
class Gate13V2ScheduleProbeResult:
    """Only shape, dispatch, cleanup, and counter facts from the V2 probe."""

    status: str
    preflight_passed: bool
    auth_result: str
    v2_contract: dict[str, object] | None
    rest_logout_result: str
    cleanup_passed: bool
    observed_physical_ig_calls: int | None
    counters: ReadOnlyBrokerRequestCounters = field(
        default_factory=ReadOnlyBrokerRequestCounters.zero
    )
    error_code: str | None = None

    def document(self) -> dict[str, object]:
        """Return the Gate 13 response shape without values, credentials, or bodies."""

        contract = self.v2_contract
        return {
            "status": self.status,
            "v2_probe": {
                "method": "GET",
                "route_category": "market-details",
                "requested_version": "2",
                "dispatched_version": (
                    contract.get("http_client_dispatch_VERSION") if contract else None
                ),
                "http_status": contract.get("response_status") if contract else None,
                "top_level_key_names": contract.get("top_level_key_names") if contract else [],
                "instrument": contract.get("instrument") if contract else None,
                "openingHours": _opening_hours_document(contract),
                "marketTimes": _market_times_document(contract),
                "openTime": _field_document(contract, "openTime"),
                "closeTime": _field_document(contract, "closeTime"),
            },
            "ig_calls": {
                "maximum": 3,
                "observed_physical": self.observed_physical_ig_calls,
                "auth": self.counters.authentication_request_count,
                "account": self.counters.account_read_count,
                "v2_market_details": self.counters.schedule_metadata_read_count,
                "v3_market_details": 0,
                "history": self.counters.historical_price_read_count,
                "stream": 0,
                "logout": self.counters.session_logout_count,
            },
            "logout": {"status": self.rest_logout_result, "passed": self.cleanup_passed},
            "allowance_affected": self.status == "SHADOW01_BLOCKED_IG_API_ALLOWANCE",
            "execution": {
                **self.counters.execution_safety_document(),
                "position_updates": 0,
                "execution_authority": "OFF",
                "demo_starts": 0,
                "live_actions": 0,
                "azure_actions": 0,
            },
            "tournament_epoch_created": False,
            "prospective_decisions": 0,
            "outcomes": 0,
            "error_code": self.error_code,
        }


class Gate13V2ScheduleProbe:
    """Perform only one Demo login, one V2 EURUSD read, and one logout."""

    def __init__(
        self,
        *,
        authorization: str,
        rest_factory: object | None = None,
        config_loader: Callable[[], ShadowTournamentConfig] = load_config,
        registry_loader: Callable[[ShadowTournamentConfig, Path], ShadowMarketRegistry] = (
            load_verified_dq03_registry
        ),
        registry_path: Path = DEFAULT_DQ03_REGISTRY_PATH,
    ) -> None:
        self._authorization = authorization
        self._rest_factory = rest_factory or Shadow01LocalDemoReadOnlyFactory(
            max_http_attempts=1,
            maximum_outbound_http_requests=3,
            maximum_authentication_requests=1,
            maximum_requests_per_route=1,
        )
        self._config_loader = config_loader
        self._registry_loader = registry_loader
        self._registry_path = registry_path

    def run(self) -> Gate13V2ScheduleProbeResult:
        broker: Shadow01ReadOnlyBroker | None = None
        authenticated = False
        preflight_passed = False
        auth_result = "NOT_RUN"
        contract: dict[str, object] | None = None
        logout = "NOT_REQUIRED"
        error_code: str | None = None
        try:
            if self._authorization != _AUTHORIZATION:
                raise _ProbeBlocked("SHADOW01_BLOCKED_GATE13_AUTHORIZATION_REQUIRED")
            _require_single_attempt_limited_factory(self._rest_factory)
            config = self._config_loader()
            _require_frozen_config(config)
            registry = self._registry_loader(config, self._registry_path)
            eurusd_epic = _eurusd_epic(registry)
            _require_ready_factory(self._rest_factory)
            broker = _build_broker(self._rest_factory)
            if broker.execution_authority != "OFF":
                raise _ProbeBlocked("SHADOW01_BLOCKED_EXECUTION_AUTHORITY_VIOLATION")
            preflight_passed = True
            broker.authenticate()
            authenticated = True
            auth_result = "PASS"
            try:
                broker.read_market_schedule_v2(eurusd_epic)
            except Exception:
                contract = broker.consume_v2_schedule_response_contract()
                if _allowance_affected(broker):
                    raise _ProbeBlocked("SHADOW01_BLOCKED_IG_API_ALLOWANCE") from None
                raise _ProbeBlocked("SHADOW01_BLOCKED_V2_SCHEDULE_PROBE_READ_FAILED") from None
            contract = broker.consume_v2_schedule_response_contract()
            error_code = _classify_contract(contract)
        except _ProbeBlocked as error:
            error_code = error.reason_code
        except Exception as error:
            if broker is not None and _allowance_affected(broker):
                error_code = "SHADOW01_BLOCKED_IG_API_ALLOWANCE"
            else:
                error_code = _safe_reason(error, "SHADOW01_BLOCKED_V2_SCHEDULE_PROBE_FAILED")
        finally:
            if broker is not None and authenticated:
                try:
                    logout = "PASS" if broker.logout() is True else "FAILED"
                except Exception:
                    logout = "FAILED"
                    error_code = error_code or "SHADOW01_BLOCKED_V2_SCHEDULE_PROBE_LOGOUT_FAILED"

        counters = (
            broker.request_counters if broker is not None else ReadOnlyBrokerRequestCounters.zero()
        )
        observed = broker.outbound_http_request_count if broker is not None else 0
        if observed is None:
            error_code = error_code or "SHADOW01_BLOCKED_V2_SCHEDULE_PROBE_CALL_COUNT_UNVERIFIED"
        elif observed > 3:
            error_code = error_code or "SHADOW01_BLOCKED_V2_SCHEDULE_PROBE_CALL_BUDGET_EXCEEDED"
        cleanup_passed = not authenticated or logout == "PASS"
        if not cleanup_passed:
            error_code = error_code or "SHADOW01_BLOCKED_V2_SCHEDULE_PROBE_LOGOUT_FAILED"
        status = error_code or "SHADOW01_BLOCKED_V2_SCHEDULE_PROBE_UNCLASSIFIED"
        return Gate13V2ScheduleProbeResult(
            status=status,
            preflight_passed=preflight_passed,
            auth_result=auth_result,
            v2_contract=contract,
            rest_logout_result=logout,
            cleanup_passed=cleanup_passed,
            observed_physical_ig_calls=observed,
            counters=counters,
            error_code=error_code,
        )


def run_gate13_v2_schedule_probe(*, authorization: str) -> Gate13V2ScheduleProbeResult:
    """Run the default V2 probe only with its explicit Gate 13 authorization."""

    return Gate13V2ScheduleProbe(authorization=authorization).run()


class _ProbeBlocked(RuntimeError):
    def __init__(self, reason_code: str) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code


def _require_single_attempt_limited_factory(factory: object) -> None:
    if getattr(factory, "max_http_attempts", None) != 1:
        raise _ProbeBlocked("SHADOW01_BLOCKED_V2_SCHEDULE_PROBE_SINGLE_ATTEMPT_REQUIRED")
    if isinstance(factory, Shadow01LocalDemoReadOnlyFactory) and (
        factory.maximum_outbound_http_requests != 3
        or factory.maximum_authentication_requests != 1
        or factory.maximum_requests_per_route != 1
    ):
        raise _ProbeBlocked("SHADOW01_BLOCKED_V2_SCHEDULE_PROBE_HARD_LIMIT_REQUIRED")


def _eurusd_epic(registry: object) -> str:
    if not isinstance(registry, ShadowMarketRegistry):
        raise _ProbeBlocked("SHADOW01_BLOCKED_DQ03_REGISTRY_UNVERIFIED")
    try:
        require_exact_twenty(registry)
        epic = registry.by_symbol(_EURUSD).epic
    except Exception:
        epic = None
    if registry.verified_count != 20 or not isinstance(epic, str) or not epic:
        raise _ProbeBlocked("SHADOW01_BLOCKED_DQ03_REGISTRY_UNVERIFIED")
    return epic


def _require_ready_factory(factory: object) -> None:
    status = getattr(factory, "status", None)
    if not callable(status):
        raise _ProbeBlocked("SHADOW01_BLOCKED_DEMO_READ_ONLY_STATUS_UNAVAILABLE")
    try:
        value = status()
    except Exception:
        raise _ProbeBlocked("SHADOW01_BLOCKED_DEMO_READ_ONLY_STATUS_UNAVAILABLE") from None
    if not isinstance(value, LocalDemoReadOnlyStatus) or not value.ready:
        reason = (
            value.reason_code
            if isinstance(value, LocalDemoReadOnlyStatus)
            else "STATUS_UNAVAILABLE"
        )
        raise _ProbeBlocked(f"SHADOW01_BLOCKED_{reason.removeprefix('SHADOW01_')}")
    if value.execution_authority != "OFF":
        raise _ProbeBlocked("SHADOW01_BLOCKED_EXECUTION_AUTHORITY_VIOLATION")


def _classify_contract(contract: Mapping[str, object] | None) -> str:
    if not isinstance(contract, Mapping):
        return "SHADOW01_BLOCKED_V2_SCHEDULE_PROBE_CONTRACT_UNAVAILABLE"
    if contract.get("http_client_dispatch_VERSION") != "2":
        return "SHADOW01_BLOCKED_V2_SCHEDULE_PROBE_HEADER_UNPROVEN"
    if contract.get("response_status") != 200:
        return "SHADOW01_BLOCKED_V2_SCHEDULE_PROBE_RESPONSE_UNAVAILABLE"
    opening = contract.get("openingHours")
    if (
        isinstance(opening, Mapping)
        and opening.get("present") is True
        and opening.get("type") == "null"
    ):
        return "SHADOW01_V2_DECLARED_HOURS_NULL"
    if _usable_hours(contract):
        return "SHADOW01_V2_DECLARED_HOURS_AVAILABLE"
    return "SHADOW01_V2_DECLARED_HOURS_UNUSABLE"


def _usable_hours(contract: Mapping[str, object]) -> bool:
    opening = contract.get("openingHours")
    market_times = contract.get("marketTimes")
    open_time = contract.get("openTime")
    close_time = contract.get("closeTime")
    return (
        isinstance(opening, Mapping)
        and opening.get("present") is True
        and opening.get("type") == "object"
        and isinstance(market_times, Mapping)
        and market_times.get("present") is True
        and market_times.get("type") == "array"
        and isinstance(market_times.get("count"), int)
        and market_times["count"] > 0
        and isinstance(open_time, Mapping)
        and open_time.get("present") is True
        and open_time.get("type") == "string"
        and isinstance(close_time, Mapping)
        and close_time.get("present") is True
        and close_time.get("type") == "string"
    )


def _opening_hours_document(contract: Mapping[str, object] | None) -> object:
    if not isinstance(contract, Mapping):
        return None
    opening_hours = contract.get("openingHours")
    if not isinstance(opening_hours, Mapping):
        return None
    return {
        "present": opening_hours.get("present"),
        "type": opening_hours.get("type"),
        "is_null": opening_hours.get("type") == "null",
    }


def _market_times_document(contract: Mapping[str, object] | None) -> object:
    return contract.get("marketTimes") if isinstance(contract, Mapping) else None


def _field_document(contract: Mapping[str, object] | None, name: str) -> object:
    return contract.get(name) if isinstance(contract, Mapping) else None


def _allowance_affected(broker: Shadow01ReadOnlyBroker) -> bool:
    diagnostic = broker.latest_response_diagnostic()
    return (
        isinstance(diagnostic, Mapping)
        and diagnostic.get("status_code") == 403
        and diagnostic.get("upstream_error_code") == _ALLOWANCE_CODE
    )


def _safe_reason(error: BaseException, fallback: str) -> str:
    candidate = str(error)
    return candidate if _SAFE_REASON.fullmatch(candidate) is not None else fallback


__all__ = ("Gate13V2ScheduleProbe", "Gate13V2ScheduleProbeResult", "run_gate13_v2_schedule_probe")
