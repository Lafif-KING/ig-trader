"""Gate 12 final clock schedule verification under a seven-call ceiling.

This runner deliberately verifies only the four V3 declared schedules in a
new authenticated Demo session.  It consumes an explicit, sanitized V11
evidence bundle for facts already proven by the larger smoke; it never claims
to revalidate V4 metadata, history, or PRICE streaming in this small run.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path

from src.ig_trader.shadow01.clock import ANCHOR_TIME
from src.ig_trader.shadow01.config import DEFAULT_CONFIG_PATH, ShadowTournamentConfig, load_config
from src.ig_trader.shadow01.live_contract_probe import _build_broker, _require_frozen_config
from src.ig_trader.shadow01.live_schedule_contract_probe import (
    _allowance_affected,
    _require_gate12_hard_request_limits,
    _require_ready_read_only_factory,
    _require_single_attempt_factory,
)
from src.ig_trader.shadow01.local_demo_read_only import Shadow01LocalDemoReadOnlyFactory
from src.ig_trader.shadow01.read_only_broker import (
    ReadOnlyBrokerRequestCounters,
    Shadow01ReadOnlyBroker,
)
from src.ig_trader.shadow01.registry import (
    ShadowMarketRegistry,
    load_verified_dq03_registry,
    require_exact_twenty,
)
from src.ig_trader.shadow01.schedule_metadata import parse_v3_market_schedule

_AUTHORIZATION = "SHADOW01_BOUNDED_FINAL_CLOCK_SMOKE_V12"
_REPRESENTATIVES = ("EURUSD", "XAUUSD", "US500", "USTECH100")
_RECONNECT_REPRESENTATIVES = ("EURUSD", "USDJPY", "XAUUSD", "US500")
_SAFE_REASON = re.compile(r"SHADOW01_[A-Z0-9_]+\Z")
DEFAULT_DQ03_REGISTRY_PATH = (
    DEFAULT_CONFIG_PATH.parent / "artifacts" / "dq03" / "instrument_registry.json"
)


class BoundedFinalClockBudgetError(RuntimeError):
    """The dedicated seven-call final-clock envelope was exceeded locally."""


@dataclass(frozen=True)
class BoundedFinalClockRequestBudget:
    """The isolated V12 envelope: auth + account + four schedules + logout."""

    maximum_auth: int = 1
    maximum_account: int = 1
    maximum_schedule: int = 4
    maximum_logout: int = 1
    used_auth: int = 0
    used_account: int = 0
    used_schedule: int = 0
    used_logout: int = 0

    def __post_init__(self) -> None:
        values = (
            self.maximum_auth,
            self.maximum_account,
            self.maximum_schedule,
            self.maximum_logout,
            self.used_auth,
            self.used_account,
            self.used_schedule,
            self.used_logout,
        )
        if any(
            isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in values
        ):
            raise ValueError("Bounded final-clock request budget is invalid")
        if (
            self.maximum_auth,
            self.maximum_account,
            self.maximum_schedule,
            self.maximum_logout,
        ) != (1, 1, 4, 1):
            raise ValueError("Bounded final-clock request budget must remain seven calls")
        if (
            self.used_auth > self.maximum_auth
            or self.used_account > self.maximum_account
            or self.used_schedule > self.maximum_schedule
            or self.used_logout > self.maximum_logout
        ):
            raise ValueError("Bounded final-clock request budget exceeds its maximum")

    @property
    def maximum_requests(self) -> int:
        return (
            self.maximum_auth + self.maximum_account + self.maximum_schedule + self.maximum_logout
        )

    @property
    def reserved_requests(self) -> int:
        return self.used_auth + self.used_account + self.used_schedule + self.used_logout

    def reserve(self, category: str) -> BoundedFinalClockRequestBudget:
        if category not in {"auth", "account", "schedule", "logout"}:
            raise BoundedFinalClockBudgetError("SHADOW01_BOUNDED_CLOCK_BUDGET_INVALID")
        used_name = f"used_{category}"
        maximum_name = f"maximum_{category}"
        if getattr(self, used_name) >= getattr(self, maximum_name):
            raise BoundedFinalClockBudgetError("SHADOW01_BOUNDED_CLOCK_BUDGET_EXCEEDED")
        return replace(self, **{used_name: getattr(self, used_name) + 1})

    def document(self) -> dict[str, object]:
        return {
            "maximum_requests": self.maximum_requests,
            "reserved_requests": self.reserved_requests,
            "maximum": {
                "auth": self.maximum_auth,
                "account": self.maximum_account,
                "v4_market": 0,
                "v3_schedule": self.maximum_schedule,
                "history": 0,
                "logout": self.maximum_logout,
            },
            "reserved": {
                "auth": self.used_auth,
                "account": self.used_account,
                "v4_market": 0,
                "v3_schedule": self.used_schedule,
                "history": 0,
                "logout": self.used_logout,
            },
        }


@dataclass(frozen=True)
class PriorV11ClockEvidence:
    """Explicit operator-reviewed, sanitized prerequisites for the V12 run.

    This contains no broker payload, timestamp, price, account identifier, or
    token. It is an explicit operator attestation rather than independently
    verified proof, and is labelled as prior evidence so V12 cannot overstate
    its scope.
    """

    source_label: str
    operator_attested: bool
    configuration_fingerprint_verified: bool
    registry_fingerprint_verified: bool
    v4_identity_status_verified_count: int
    history_ready_symbols: tuple[str, ...]
    stream_valid_quote_count: int
    stream_reconnect_symbols: tuple[str, ...]
    execution_authority: str
    tournament_epoch_created: bool

    def is_complete(self) -> bool:
        return (
            self.source_label == "SHADOW01_LIVE_READONLY_SMOKE_V11"
            and self.operator_attested is True
            and self.configuration_fingerprint_verified is True
            and self.registry_fingerprint_verified is True
            and self.v4_identity_status_verified_count == 20
            and set(self.history_ready_symbols) == set(_REPRESENTATIVES)
            and self.stream_valid_quote_count == 20
            and set(self.stream_reconnect_symbols) == set(_RECONNECT_REPRESENTATIVES)
            and self.execution_authority == "OFF"
            and self.tournament_epoch_created is False
        )

    def document(self) -> dict[str, object]:
        return {
            "source_label": self.source_label,
            "evidence_class": "OPERATOR_ATTESTED_SANITIZED_PRIOR_FACTS",
            "accepted": self.is_complete(),
            "freshly_revalidated_in_v12": False,
            "operator_attested": self.operator_attested,
            "configuration_fingerprint_verified": self.configuration_fingerprint_verified,
            "registry_fingerprint_verified": self.registry_fingerprint_verified,
            "v4_identity_status_verified_count": self.v4_identity_status_verified_count,
            "history_ready_symbols": sorted(set(self.history_ready_symbols)),
            "stream_valid_quote_count": self.stream_valid_quote_count,
            "stream_reconnect_symbols": sorted(set(self.stream_reconnect_symbols)),
            "execution_authority": self.execution_authority,
            "tournament_epoch_created": self.tournament_epoch_created,
        }


@dataclass(frozen=True)
class BoundedFinalClockSmokeResult:
    """Value-safe result of the seven-call V12 final-clock schedule check."""

    status: str
    preflight_passed: bool
    auth_result: str
    account_result: str
    prior_v11_evidence: PriorV11ClockEvidence
    schedules: tuple[dict[str, object], ...]
    v3_schedule_contracts: tuple[dict[str, object], ...]
    request_budget: BoundedFinalClockRequestBudget
    rest_logout_result: str
    rest_logout_http_status: int | None
    rest_logout_upstream_error_code: str | None
    cleanup_passed: bool
    observed_physical_ig_calls: int | None
    counters: ReadOnlyBrokerRequestCounters = field(
        default_factory=ReadOnlyBrokerRequestCounters.zero
    )
    error_code: str | None = None

    def document(self) -> dict[str, object]:
        return {
            "status": self.status,
            "scope": {
                "authorization": _AUTHORIZATION,
                "representatives": list(_REPRESENTATIVES),
                "fresh_v4_market_reads": 0,
                "fresh_history_reads": 0,
                "fresh_stream_connections": 0,
                "rest_live_price_reads": 0,
                "execution_authority": "OFF",
                "tournament_epoch_created": False,
                "prospective_decisions": 0,
                "outcomes": 0,
            },
            "prior_v11_evidence": self.prior_v11_evidence.document(),
            "auth": self.auth_result,
            "account": self.account_result,
            "schedules": [dict(item) for item in self.schedules],
            "v3_schedule_contracts": [dict(item) for item in self.v3_schedule_contracts],
            "rest_budget": self.request_budget.document(),
            "physical_ig_calls": {
                "maximum": self.request_budget.maximum_requests,
                "observed": self.observed_physical_ig_calls,
                "single_attempt_transport": True,
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


class BoundedFinalClockSmokeV12:
    """Verify only fresh declared schedule evidence while reusing labeled V11 facts."""

    def __init__(
        self,
        *,
        authorization: str,
        prior_v11_evidence: PriorV11ClockEvidence,
        rest_factory: object | None = None,
        config_loader: Callable[[], ShadowTournamentConfig] = load_config,
        registry_loader: Callable[[ShadowTournamentConfig, Path], ShadowMarketRegistry] = (
            load_verified_dq03_registry
        ),
        registry_path: Path = DEFAULT_DQ03_REGISTRY_PATH,
        request_budget: BoundedFinalClockRequestBudget | None = None,
    ) -> None:
        if not isinstance(prior_v11_evidence, PriorV11ClockEvidence):
            raise TypeError("V12 requires a sanitized prior V11 evidence bundle")
        if request_budget is not None and not isinstance(
            request_budget,
            BoundedFinalClockRequestBudget,
        ):
            raise TypeError("V12 requires a bounded final-clock request budget")
        if request_budget is not None and request_budget.reserved_requests != 0:
            raise ValueError("V12 request budget must be unused")
        self._authorization = authorization
        self._prior_v11_evidence = prior_v11_evidence
        self._rest_factory = rest_factory or Shadow01LocalDemoReadOnlyFactory(
            max_http_attempts=1,
            maximum_outbound_http_requests=7,
            maximum_authentication_requests=1,
            maximum_requests_per_route=1,
        )
        self._config_loader = config_loader
        self._registry_loader = registry_loader
        self._registry_path = registry_path
        self._request_budget = request_budget or BoundedFinalClockRequestBudget()

    def run(self) -> BoundedFinalClockSmokeResult:
        """Execute no more than auth, account, four V3 reads, and final logout."""

        broker: Shadow01ReadOnlyBroker | None = None
        budget = self._request_budget
        authenticated = False
        preflight_passed = False
        auth_result = "NOT_RUN"
        account_result = "NOT_RUN"
        schedules: list[dict[str, object]] = []
        v3_schedule_contracts: list[dict[str, object]] = []
        logout_result = "NOT_REQUIRED"
        logout_status: int | None = None
        logout_error_code: str | None = None
        error_code: str | None = None

        try:
            if self._authorization != _AUTHORIZATION:
                raise _SmokeBlocked("SHADOW01_BLOCKED_BOUNDED_CLOCK_AUTHORIZATION_REQUIRED")
            if not self._prior_v11_evidence.is_complete():
                raise _SmokeBlocked("SHADOW01_BLOCKED_BOUNDED_CLOCK_PRIOR_EVIDENCE_REQUIRED")
            _require_single_attempt_factory(self._rest_factory)
            _require_gate12_hard_request_limits(
                self._rest_factory,
                maximum_physical_ig_calls=7,
            )
            config = self._config_loader()
            _require_frozen_config(config)
            registry = self._registry_loader(config, self._registry_path)
            _require_registry(registry)
            _require_ready_read_only_factory(self._rest_factory)
            broker = _build_broker(self._rest_factory)
            if broker.execution_authority != "OFF":
                raise _SmokeBlocked("SHADOW01_BLOCKED_EXECUTION_AUTHORITY_VIOLATION")
            preflight_passed = True

            budget = budget.reserve("auth")
            broker.authenticate()
            authenticated = True
            auth_result = "PASS"
            budget = budget.reserve("account")
            account_document = broker.read_account()
            account_result = "PASS" if broker.account_state_is_valid(account_document) else "FAILED"
            del account_document
            if account_result != "PASS":
                raise _SmokeBlocked("SHADOW01_BLOCKED_DEMO_ACCOUNT_STATE_INVALID")
            for symbol in _REPRESENTATIVES:
                market = registry.by_symbol(symbol)
                if not isinstance(market.epic, str) or not market.epic:
                    raise _SmokeBlocked("SHADOW01_BLOCKED_DQ03_LIVE_IDENTITY_INCOMPLETE")
                budget = budget.reserve("schedule")
                try:
                    document = broker.read_market_schedule_v3(market.epic)
                except Exception:
                    contract = broker.consume_v3_schedule_response_contract()
                    if contract is not None:
                        v3_schedule_contracts.append(contract)
                    if _allowance_affected(broker):
                        raise _SmokeBlocked("SHADOW01_BLOCKED_IG_API_ALLOWANCE") from None
                    raise _SmokeBlocked(
                        "SHADOW01_BLOCKED_BOUNDED_CLOCK_SCHEDULE_UNAVAILABLE"
                    ) from None
                contract = broker.consume_v3_schedule_response_contract()
                if not _v3_schedule_contract_proves_success(contract):
                    raise _SmokeBlocked("SHADOW01_BLOCKED_GATE12_V3_HEADER_UNPROVEN")
                assert contract is not None
                v3_schedule_contracts.append(contract)
                evidence = parse_v3_market_schedule(
                    symbol=symbol,
                    epic=market.epic,
                    document=document,
                )
                schedules.append(evidence.document())
                if evidence.hours_state != "DECLARED_HOURS_AVAILABLE":
                    raise _SmokeBlocked("SHADOW01_GATE12_V3_HOURS_HUMAN_GATE_REQUIRED")
                if evidence.target_anchor_in_declared_operational_window(ANCHOR_TIME) is not True:
                    raise _SmokeBlocked("SHADOW01_GATE12_CLOCK_WINDOW_HUMAN_GATE_REQUIRED")
        except (_SmokeBlocked, BoundedFinalClockBudgetError) as error:
            error_code = getattr(error, "reason_code", str(error))
        except Exception as error:
            if broker is not None and _allowance_affected(broker):
                error_code = "SHADOW01_BLOCKED_IG_API_ALLOWANCE"
            else:
                error_code = _safe_reason(error, "SHADOW01_BLOCKED_BOUNDED_CLOCK_UNAVAILABLE")
        finally:
            if broker is not None and authenticated:
                try:
                    budget = budget.reserve("logout")
                    logout_result = "PASS" if broker.logout() is True else "FAILED"
                except Exception:
                    diagnostic = broker.latest_response_diagnostic()
                    logout_status = _diagnostic_status(diagnostic)
                    logout_error_code = _diagnostic_error_code(diagnostic)
                    if _allowance_affected(broker):
                        logout_result = "FAILED_ALLOWANCE_AFFECTED"
                        error_code = "SHADOW01_BLOCKED_IG_API_ALLOWANCE"
                    else:
                        logout_result = "FAILED"

        counters = (
            broker.request_counters if broker is not None else ReadOnlyBrokerRequestCounters.zero()
        )
        observed_calls = broker.outbound_http_request_count if broker is not None else 0
        if observed_calls is None:
            error_code = error_code or "SHADOW01_BLOCKED_BOUNDED_CLOCK_IG_CALL_COUNT_UNVERIFIED"
        elif observed_calls > budget.maximum_requests:
            error_code = error_code or "SHADOW01_BLOCKED_BOUNDED_CLOCK_IG_CALL_BUDGET_EXCEEDED"
        cleanup_passed = not authenticated or logout_result == "PASS"
        if not cleanup_passed and error_code is None:
            error_code = "SHADOW01_BLOCKED_BOUNDED_CLOCK_CLEANUP_FAILED"
        status = "SHADOW01_BOUNDED_FINAL_CLOCK_SMOKE_V12_PASS" if error_code is None else error_code
        return BoundedFinalClockSmokeResult(
            status=status,
            preflight_passed=preflight_passed,
            auth_result=auth_result,
            account_result=account_result,
            prior_v11_evidence=self._prior_v11_evidence,
            schedules=tuple(schedules),
            v3_schedule_contracts=tuple(v3_schedule_contracts),
            request_budget=budget,
            rest_logout_result=logout_result,
            rest_logout_http_status=logout_status,
            rest_logout_upstream_error_code=logout_error_code,
            cleanup_passed=cleanup_passed,
            observed_physical_ig_calls=observed_calls,
            counters=counters,
            error_code=error_code,
        )


class _SmokeBlocked(RuntimeError):
    def __init__(self, reason_code: str) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code


def _require_registry(registry: object) -> None:
    if not isinstance(registry, ShadowMarketRegistry):
        raise _SmokeBlocked("SHADOW01_BLOCKED_DQ03_REGISTRY_UNVERIFIED")
    try:
        markets = require_exact_twenty(registry)
    except Exception:
        raise _SmokeBlocked("SHADOW01_BLOCKED_DQ03_REGISTRY_UNVERIFIED") from None
    if (
        registry.verified_count != 20
        or registry.unavailable_count != 0
        or any(market.epic is None for market in markets)
    ):
        raise _SmokeBlocked("SHADOW01_BLOCKED_DQ03_REGISTRY_UNVERIFIED")


def _diagnostic_status(diagnostic: object) -> int | None:
    if not isinstance(diagnostic, Mapping):
        return None
    value = diagnostic.get("status_code")
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _diagnostic_error_code(diagnostic: object) -> str | None:
    if not isinstance(diagnostic, Mapping):
        return None
    value = diagnostic.get("upstream_error_code")
    return (
        "error.public-api.exceeded-api-key-allowance"
        if value == "error.public-api.exceeded-api-key-allowance"
        else None
    )


def _v3_schedule_contract_proves_success(contract: object) -> bool:
    return (
        isinstance(contract, Mapping)
        and contract.get("response_status") == 200
        and contract.get("http_client_dispatch_VERSION") == "3"
    )


def _safe_reason(error: BaseException, fallback: str) -> str:
    candidate = str(error)
    return candidate if _SAFE_REASON.fullmatch(candidate) is not None else fallback


__all__ = (
    "BoundedFinalClockBudgetError",
    "BoundedFinalClockRequestBudget",
    "BoundedFinalClockSmokeResult",
    "BoundedFinalClockSmokeV12",
    "PriorV11ClockEvidence",
)
