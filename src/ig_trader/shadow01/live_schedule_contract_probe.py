"""One small, no-retry Gate 12 IG Demo V3 schedule-contract probe.

The probe is intentionally narrower than the normal Shadow01 smoke.  It has
no account read, V4 metadata read, history read, stream lifecycle, store,
epoch, decision, or execution path.  It exists only to determine whether the
actual V3 response shape supplies the declared-hours contract.
"""

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
from src.ig_trader.shadow01.schedule_contract import documented_v2_schedule_contract

_AUTHORIZATION = "SHADOW01_GATE12_SCHEDULE_CONTRACT_PROBE"
_EURUSD = "EURUSD"
_XAUUSD = "XAUUSD"
_SAFE_REASON = re.compile(r"SHADOW01_[A-Z0-9_]+\Z")
_ALLOWANCE_CODE = "error.public-api.exceeded-api-key-allowance"
DEFAULT_DQ03_REGISTRY_PATH = (
    DEFAULT_CONFIG_PATH.parent / "artifacts" / "dq03" / "instrument_registry.json"
)


@dataclass(frozen=True)
class Gate12ScheduleContractProbeResult:
    """Sanitized runtime evidence from no more than four physical IG calls."""

    status: str
    preflight_passed: bool
    auth_result: str
    eurusd_result: str
    eurusd_contract: dict[str, object] | None
    xauusd_result: str = "NOT_REQUESTED"
    xauusd_contract: dict[str, object] | None = None
    rest_logout_result: str = "NOT_REQUIRED"
    rest_logout_http_status: int | None = None
    rest_logout_upstream_error_code: str | None = None
    cleanup_passed: bool = False
    observed_physical_ig_calls: int | None = None
    maximum_physical_ig_calls: int = 3
    counters: ReadOnlyBrokerRequestCounters = field(
        default_factory=ReadOnlyBrokerRequestCounters.zero
    )
    error_code: str | None = None

    def document(self) -> dict[str, object]:
        """Return only values that are safe to print or hand to a human gate."""

        return {
            "status": self.status,
            "scope": {
                "authorization": _AUTHORIZATION,
                "representative": _EURUSD,
                "optional_comparison": _XAUUSD,
                "account_reads": 0,
                "v4_market_reads": 0,
                "history_reads": 0,
                "stream_connections": 0,
                "rest_live_price_reads": 0,
                "v2_live_market_reads": 0,
                "execution_authority": "OFF",
                "tournament_epoch_created": False,
                "prospective_decisions": 0,
                "outcomes": 0,
            },
            "auth": self.auth_result,
            "schedule_contract": {
                _EURUSD: {"result": self.eurusd_result, "response": self.eurusd_contract},
                _XAUUSD: {"result": self.xauusd_result, "response": self.xauusd_contract},
            },
            "offline_v2_contract_reference": (
                documented_v2_schedule_contract()
                if self.error_code == "SHADOW01_GATE12_V3_HOURS_HUMAN_GATE_REQUIRED"
                else None
            ),
            "ig_call_budget": {
                "maximum_physical": self.maximum_physical_ig_calls,
                "observed_physical": self.observed_physical_ig_calls,
                "logical": {
                    "auth": self.counters.authentication_request_count,
                    "account": self.counters.account_read_count,
                    "v4_market": self.counters.market_read_count,
                    "v3_schedule": self.counters.schedule_metadata_read_count,
                    "history": self.counters.historical_price_read_count,
                    "successful_logout": self.counters.session_logout_count,
                },
            },
            "cleanup": {
                "rest_logout": self.rest_logout_result,
                "rest_logout_http_status": self.rest_logout_http_status,
                "rest_logout_upstream_error_code": self.rest_logout_upstream_error_code,
                "passed": self.cleanup_passed,
            },
            "execution": {
                **self.counters.execution_safety_document(),
                "execution_authority": "OFF",
                "live_actions": 0,
                "azure_actions": 0,
            },
            "error_code": self.error_code,
        }


class Gate12ScheduleContractProbe:
    """Run a value-safe V3 shape check under an exact physical-call envelope."""

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
        compare_xau_if_eurusd_hours_absent: bool = False,
    ) -> None:
        if not isinstance(compare_xau_if_eurusd_hours_absent, bool):
            raise TypeError("Gate12 XAUUSD comparison switch must be boolean")
        self._authorization = authorization
        self._maximum_physical_ig_calls = 4 if compare_xau_if_eurusd_hours_absent else 3
        self._rest_factory = rest_factory or Shadow01LocalDemoReadOnlyFactory(
            max_http_attempts=1,
            maximum_outbound_http_requests=self._maximum_physical_ig_calls,
            maximum_authentication_requests=1,
            maximum_requests_per_route=1,
        )
        self._config_loader = config_loader
        self._registry_loader = registry_loader
        self._registry_path = registry_path
        self._compare_xau_if_eurusd_hours_absent = compare_xau_if_eurusd_hours_absent

    def run(self) -> Gate12ScheduleContractProbeResult:
        """Run one EURUSD V3 read and, only when explicitly enabled, one XAUUSD read."""

        broker: Shadow01ReadOnlyBroker | None = None
        authenticated = False
        preflight_passed = False
        auth_result = "NOT_RUN"
        eurusd_result = "NOT_RUN"
        eurusd_contract: dict[str, object] | None = None
        xauusd_result = "NOT_REQUESTED"
        xauusd_contract: dict[str, object] | None = None
        rest_logout_result = "NOT_REQUIRED"
        rest_logout_http_status: int | None = None
        rest_logout_upstream_error_code: str | None = None
        error_code: str | None = None

        try:
            if self._authorization != _AUTHORIZATION:
                raise _ProbeBlocked("SHADOW01_BLOCKED_GATE12_AUTHORIZATION_REQUIRED")
            _require_single_attempt_factory(self._rest_factory)
            _require_gate12_hard_request_limits(
                self._rest_factory,
                maximum_physical_ig_calls=self._maximum_physical_ig_calls,
            )
            config = self._config_loader()
            _require_frozen_config(config)
            registry = self._registry_loader(config, self._registry_path)
            _require_registry(registry)
            eurusd_epic = _required_epic(registry, _EURUSD)
            xauusd_epic = _required_epic(registry, _XAUUSD)
            _require_ready_read_only_factory(self._rest_factory)
            broker = _build_broker(self._rest_factory)
            if broker.execution_authority != "OFF":
                raise _ProbeBlocked("SHADOW01_BLOCKED_EXECUTION_AUTHORITY_VIOLATION")
            preflight_passed = True

            broker.authenticate()
            authenticated = True
            auth_result = "PASS"
            eurusd_result, eurusd_contract = _read_schedule_contract(broker, eurusd_epic)
            if eurusd_result != "PASS":
                if _allowance_affected(broker):
                    raise _ProbeBlocked("SHADOW01_BLOCKED_IG_API_ALLOWANCE")
                raise _ProbeBlocked("SHADOW01_BLOCKED_GATE12_V3_READ_FAILED")
            if (
                self._compare_xau_if_eurusd_hours_absent
                and eurusd_result == "PASS"
                and _hours_shape_absent(eurusd_contract)
            ):
                xauusd_result, xauusd_contract = _read_schedule_contract(broker, xauusd_epic)
                if xauusd_result != "PASS":
                    if _allowance_affected(broker):
                        raise _ProbeBlocked("SHADOW01_BLOCKED_IG_API_ALLOWANCE")
                    raise _ProbeBlocked("SHADOW01_BLOCKED_GATE12_V3_READ_FAILED")
        except _ProbeBlocked as error:
            error_code = error.reason_code
        except Exception as error:
            if broker is not None and _allowance_affected(broker):
                error_code = "SHADOW01_BLOCKED_IG_API_ALLOWANCE"
            else:
                error_code = _safe_reason(error, "SHADOW01_BLOCKED_GATE12_CONTRACT_PROBE_FAILED")
        finally:
            if broker is not None and authenticated:
                try:
                    rest_logout_result = "PASS" if broker.logout() is True else "FAILED"
                except Exception:
                    diagnostic = broker.latest_response_diagnostic()
                    rest_logout_http_status = _diagnostic_status(diagnostic)
                    rest_logout_upstream_error_code = _diagnostic_error_code(diagnostic)
                    if _allowance_affected(broker):
                        rest_logout_result = "FAILED_ALLOWANCE_AFFECTED"
                        error_code = "SHADOW01_BLOCKED_IG_API_ALLOWANCE"
                    else:
                        rest_logout_result = "FAILED"

        counters = (
            broker.request_counters if broker is not None else ReadOnlyBrokerRequestCounters.zero()
        )
        observed_calls = broker.outbound_http_request_count if broker is not None else 0
        maximum_calls = self._maximum_physical_ig_calls
        if observed_calls is None:
            error_code = error_code or "SHADOW01_BLOCKED_GATE12_IG_CALL_COUNT_UNVERIFIED"
        elif observed_calls > maximum_calls:
            error_code = error_code or "SHADOW01_BLOCKED_GATE12_IG_CALL_BUDGET_EXCEEDED"
        cleanup_passed = not authenticated or rest_logout_result == "PASS"
        if not cleanup_passed and error_code is None:
            error_code = "SHADOW01_BLOCKED_GATE12_CLEANUP_FAILED"
        if error_code is None:
            error_code = _shape_outcome(eurusd_contract)
        status = (
            "SHADOW01_GATE12_SCHEDULE_CONTRACT_PROBE_PASS" if error_code is None else error_code
        )
        return Gate12ScheduleContractProbeResult(
            status=status,
            preflight_passed=preflight_passed,
            auth_result=auth_result,
            eurusd_result=eurusd_result,
            eurusd_contract=eurusd_contract,
            xauusd_result=xauusd_result,
            xauusd_contract=xauusd_contract,
            rest_logout_result=rest_logout_result,
            rest_logout_http_status=rest_logout_http_status,
            rest_logout_upstream_error_code=rest_logout_upstream_error_code,
            cleanup_passed=cleanup_passed,
            observed_physical_ig_calls=observed_calls,
            maximum_physical_ig_calls=maximum_calls,
            counters=counters,
            error_code=error_code,
        )


class _ProbeBlocked(RuntimeError):
    def __init__(self, reason_code: str) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code


def run_gate12_schedule_contract_probe(
    *,
    authorization: str,
    compare_xau_if_eurusd_hours_absent: bool = False,
) -> Gate12ScheduleContractProbeResult:
    """Run the default local Gate12 probe only with its explicit authorization."""

    return Gate12ScheduleContractProbe(
        authorization=authorization,
        compare_xau_if_eurusd_hours_absent=compare_xau_if_eurusd_hours_absent,
    ).run()


def _require_single_attempt_factory(factory: object) -> None:
    """Reject a known retrying real factory before it can construct an HTTP client."""

    attempts = getattr(factory, "max_http_attempts", None)
    if type(attempts) is not int or attempts != 1:
        raise _ProbeBlocked("SHADOW01_BLOCKED_GATE12_SINGLE_ATTEMPT_TRANSPORT_REQUIRED")


def _require_gate12_hard_request_limits(
    factory: object,
    *,
    maximum_physical_ig_calls: int,
) -> None:
    """Require the real Demo factory to reject excess traffic before dispatch."""

    if not isinstance(factory, Shadow01LocalDemoReadOnlyFactory):
        return
    if (
        factory.maximum_outbound_http_requests != maximum_physical_ig_calls
        or factory.maximum_authentication_requests != 1
        or factory.maximum_requests_per_route != 1
    ):
        raise _ProbeBlocked("SHADOW01_BLOCKED_GATE12_HARD_REQUEST_LIMIT_REQUIRED")


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


def _required_epic(registry: ShadowMarketRegistry, symbol: str) -> str:
    try:
        epic = registry.by_symbol(symbol).epic
    except Exception:
        epic = None
    if not isinstance(epic, str) or not epic:
        raise _ProbeBlocked(f"SHADOW01_BLOCKED_GATE12_{symbol}_IDENTITY_UNVERIFIED")
    return epic


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


def _read_schedule_contract(
    broker: Shadow01ReadOnlyBroker,
    epic: str,
) -> tuple[str, dict[str, object] | None]:
    try:
        broker.read_market_schedule_v3(epic)
    except Exception:
        return "FAILED", broker.consume_v3_schedule_response_contract()
    contract = broker.consume_v3_schedule_response_contract()
    if contract is None:
        raise _ProbeBlocked("SHADOW01_BLOCKED_GATE12_V3_CONTRACT_UNAVAILABLE")
    return "PASS", contract


def _shape_outcome(contract: dict[str, object] | None) -> str | None:
    if contract is None:
        return "SHADOW01_BLOCKED_GATE12_V3_CONTRACT_UNAVAILABLE"
    if contract.get("http_client_dispatch_VERSION") != "3":
        return "SHADOW01_BLOCKED_GATE12_V3_HEADER_UNPROVEN"
    if contract.get("response_status") != 200:
        return "SHADOW01_BLOCKED_GATE12_V3_RESPONSE_UNAVAILABLE"
    if _hours_shape_compatible(contract):
        return None
    return "SHADOW01_GATE12_V3_HOURS_HUMAN_GATE_REQUIRED"


def _hours_shape_absent(contract: Mapping[str, object] | None) -> bool:
    if not isinstance(contract, Mapping):
        return False
    opening_hours = contract.get("openingHours")
    return isinstance(opening_hours, Mapping) and opening_hours.get("present") is False


def _hours_shape_compatible(contract: Mapping[str, object]) -> bool:
    opening_hours = contract.get("openingHours")
    market_times = contract.get("marketTimes")
    open_time = contract.get("openTime")
    close_time = contract.get("closeTime")
    return (
        isinstance(opening_hours, Mapping)
        and opening_hours.get("present") is True
        and opening_hours.get("type") == "object"
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


def _allowance_affected(broker: Shadow01ReadOnlyBroker) -> bool:
    diagnostic = broker.latest_response_diagnostic()
    return (
        isinstance(diagnostic, Mapping)
        and diagnostic.get("status_code") == 403
        and diagnostic.get("upstream_error_code") == _ALLOWANCE_CODE
    )


def _diagnostic_status(diagnostic: object) -> int | None:
    if not isinstance(diagnostic, Mapping):
        return None
    value = diagnostic.get("status_code")
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _diagnostic_error_code(diagnostic: object) -> str | None:
    if not isinstance(diagnostic, Mapping):
        return None
    value = diagnostic.get("upstream_error_code")
    return _ALLOWANCE_CODE if value == _ALLOWANCE_CODE else None


def _safe_reason(error: BaseException, fallback: str) -> str:
    candidate = str(error)
    return candidate if _SAFE_REASON.fullmatch(candidate) is not None else fallback


def _blocked_reason(reason: str) -> str:
    if reason.startswith("SHADOW01_BLOCKED_"):
        return reason
    suffix = reason.removeprefix("SHADOW01_")
    return f"SHADOW01_BLOCKED_{suffix}"


__all__ = (
    "Gate12ScheduleContractProbe",
    "Gate12ScheduleContractProbeResult",
    "run_gate12_schedule_contract_probe",
)
