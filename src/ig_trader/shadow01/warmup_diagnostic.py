"""Bounded, non-persisting completed-history readiness diagnostic for Shadow01.

The prospective runtime deliberately owns its own cache and persistence rules.
This module does neither: it requests a small, fixed number of historical
documents through an injected read-only broker and returns diagnostic facts
only.  It has no epoch, snapshot, decision, outcome, or execution dependency.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol

from src.ig_trader.shadow01.config import ShadowTournamentConfig
from src.ig_trader.shadow01.data import ShadowDataError, parse_completed_daily_bars
from src.ig_trader.shadow01.models import DailyBar, MarketDataState, MarketSpec, require_utc
from src.ig_trader.shadow01.registry import ShadowMarketRegistry

_DEFAULT_REPRESENTATIVES = ("EURUSD", "XAUUSD", "US500")
_MAX_WARMUP_REQUEST_BUDGET = len(_DEFAULT_REPRESENTATIVES)
_LIVE_READONLY_SMOKE_V2_REPRESENTATIVES = ("EURUSD", "USDJPY", "XAUUSD", "US500")
_LIVE_READONLY_SMOKE_V2_REQUEST_BUDGET = len(_LIVE_READONLY_SMOKE_V2_REPRESENTATIVES)


class ShadowWarmupDiagnosticError(ValueError):
    """The isolated warm-up diagnostic was configured outside its safe bounds."""


class WarmupDataQualityState(StrEnum):
    """Read-only readiness state; it is not a prospective Q1 decision."""

    READY = "READY"
    WARNING = "WARNING"
    BLOCKED = "BLOCKED"
    UNKNOWN = "UNKNOWN"


class WarmupDiagnosticBroker(Protocol):
    """The sole broker capability used by this bounded history diagnostic."""

    @property
    def execution_authority(self) -> str: ...

    def read_historical_prices(self, epic: str, resolution: str, points: int) -> object: ...


@dataclass(frozen=True)
class WarmupMarketReport:
    """One representative market's completed-history readiness facts."""

    symbol: str
    epic: str | None
    request_dispatched: bool
    bars_requested: int
    bars_received: int
    completed_sessions: int
    latest_completed_session_utc: datetime | None
    t1_history_ready: bool
    m1_history_ready: bool
    q1_history_ready: bool
    data_quality_result: WarmupDataQualityState
    reason_codes: tuple[str, ...]
    http_status_code: int | None = None
    upstream_error_code: str | None = None

    def document(self) -> dict[str, object]:
        """Return a credential-free report suitable for ignored diagnostic artifacts."""

        return {
            "symbol": self.symbol,
            "epic": self.epic,
            "request_dispatched": self.request_dispatched,
            "bars_requested": self.bars_requested,
            "bars_received": self.bars_received,
            "completed_sessions": self.completed_sessions,
            "latest_completed_session_utc": (
                self.latest_completed_session_utc.isoformat()
                if self.latest_completed_session_utc is not None
                else None
            ),
            "t1_history_ready": self.t1_history_ready,
            "m1_history_ready": self.m1_history_ready,
            "q1_history_ready": self.q1_history_ready,
            "data_quality_result": self.data_quality_result.value,
            "reason_codes": list(self.reason_codes),
            "http_status_code": self.http_status_code,
            "upstream_error_code": self.upstream_error_code,
        }


@dataclass(frozen=True)
class WarmupDiagnosticReport:
    """A fixed-budget read-only warm-up report with no persistence channel."""

    observed_at_utc: datetime
    execution_authority: str
    non_persisting: bool
    request_budget: int
    requests_used: int
    history_points_per_request: int
    t1_minimum_completed_sessions: int
    m1_full_calibration_minimum_sessions: int
    q1_minimum_completed_sessions: int
    overall_data_quality: WarmupDataQualityState
    markets: tuple[WarmupMarketReport, ...]

    def document(self) -> dict[str, object]:
        """Return artifact-ready facts without historical decisions or outcomes."""

        return {
            "diagnostic": "SHADOW01_WARMUP_READ_ONLY",
            "observed_at_utc": self.observed_at_utc.isoformat(),
            "execution_authority": self.execution_authority,
            "non_persisting": self.non_persisting,
            "request_budget": self.request_budget,
            "requests_used": self.requests_used,
            "history_points_per_request": self.history_points_per_request,
            "t1_minimum_completed_sessions": self.t1_minimum_completed_sessions,
            "m1_full_calibration_minimum_sessions": self.m1_full_calibration_minimum_sessions,
            "q1_minimum_completed_sessions": self.q1_minimum_completed_sessions,
            "overall_data_quality": self.overall_data_quality.value,
            "markets": [item.document() for item in self.markets],
        }


class ShadowWarmupDiagnostic:
    """Request at most three fixed, verified representative daily histories.

    The service takes neither a database nor a filesystem path.  Consequently
    it cannot cache raw history, write an epoch, or append a decision/outcome;
    the result is diagnostic-only and must never be treated as prospective
    tournament evidence.
    """

    def __init__(
        self,
        *,
        config: ShadowTournamentConfig,
        registry: ShadowMarketRegistry,
        broker: WarmupDiagnosticBroker,
        request_budget: int = _MAX_WARMUP_REQUEST_BUDGET,
        representative_symbols: tuple[str, ...] = _DEFAULT_REPRESENTATIVES,
    ) -> None:
        _require_verified_config(config)
        _require_authoritative_registry(registry)
        if not callable(getattr(broker, "read_historical_prices", None)):
            raise TypeError("Shadow01 warm-up diagnostic requires a history-read broker")
        if not _execution_authority_is_off(broker):
            raise ShadowWarmupDiagnosticError("SHADOW01_WARMUP_EXECUTION_AUTHORITY_INVALID")
        if (
            isinstance(request_budget, bool)
            or not isinstance(request_budget, int)
            or request_budget < 1
            or request_budget > _MAX_WARMUP_REQUEST_BUDGET
        ):
            raise ShadowWarmupDiagnosticError("SHADOW01_WARMUP_REQUEST_BUDGET_INVALID")
        if (
            not isinstance(representative_symbols, tuple)
            or not representative_symbols
            or len(representative_symbols) > _MAX_WARMUP_REQUEST_BUDGET
            or len(set(representative_symbols)) != len(representative_symbols)
            or any(not isinstance(symbol, str) or not symbol for symbol in representative_symbols)
        ):
            raise ShadowWarmupDiagnosticError("SHADOW01_WARMUP_REPRESENTATIVES_INVALID")
        self._config = config
        self._registry = registry
        self._broker = broker
        self._request_budget = request_budget
        self._representative_symbols = representative_symbols

    def run(self, *, observed_at_utc: datetime) -> WarmupDiagnosticReport:
        """Acquire only bounded completed history and return immediately.

        The timestamp is injected so callers can make the causal completed-bar
        boundary explicit.  The method does not wait for the 17:10 anchor,
        create a cache, or invoke any policy engine.
        """

        observed_at = _require_timestamp(observed_at_utc)
        requirements = _history_requirements(self._config)
        requests_used = 0
        reports: list[WarmupMarketReport] = []
        for symbol in self._representative_symbols:
            market = _market_or_none(self._registry, symbol)
            if market is None or market.state is not MarketDataState.AVAILABLE or not market.epic:
                reports.append(_unavailable_report(symbol, market))
                continue
            if requests_used >= self._request_budget:
                reports.append(_budget_exhausted_report(market))
                continue
            reports.append(self._read_market_history(market, observed_at, requirements))
            requests_used += 1
        result = tuple(reports)
        return WarmupDiagnosticReport(
            observed_at_utc=observed_at,
            execution_authority="OFF",
            non_persisting=True,
            request_budget=self._request_budget,
            requests_used=requests_used,
            history_points_per_request=requirements.target,
            t1_minimum_completed_sessions=requirements.t1,
            m1_full_calibration_minimum_sessions=requirements.m1,
            q1_minimum_completed_sessions=requirements.q1,
            overall_data_quality=_overall_quality(result),
            markets=result,
        )

    def _read_market_history(
        self,
        market: MarketSpec,
        observed_at: datetime,
        requirements: _HistoryRequirements,
    ) -> WarmupMarketReport:
        return _read_warmup_history(
            broker=self._broker,
            market=market,
            observed_at=observed_at,
            requirements=requirements,
        )


def run_shadow_warmup_diagnostic(
    *,
    config: ShadowTournamentConfig,
    registry: ShadowMarketRegistry,
    broker: WarmupDiagnosticBroker,
    observed_at_utc: datetime,
    request_budget: int = _MAX_WARMUP_REQUEST_BUDGET,
    representative_symbols: tuple[str, ...] = _DEFAULT_REPRESENTATIVES,
) -> WarmupDiagnosticReport:
    """Run the bounded non-persisting history readiness diagnostic."""

    return ShadowWarmupDiagnostic(
        config=config,
        registry=registry,
        broker=broker,
        request_budget=request_budget,
        representative_symbols=representative_symbols,
    ).run(observed_at_utc=observed_at_utc)


def run_shadow_live_readonly_smoke_warmup_v2(
    *,
    config: ShadowTournamentConfig,
    registry: ShadowMarketRegistry,
    broker: WarmupDiagnosticBroker,
    observed_at_utc: datetime,
) -> WarmupDiagnosticReport:
    """Run Gate V2's fixed four-market bounded read-only warm-up.

    This intentionally does not alter Gate-02's three-market public
    diagnostic contract.  The V2 smoke requires exactly one fixed FX, JPY,
    metal, and US-index representative set and has no caller-selectable
    symbols or request budget.
    """

    _require_verified_config(config)
    _require_authoritative_registry(registry)
    if not callable(getattr(broker, "read_historical_prices", None)):
        raise TypeError("Shadow01 warm-up diagnostic requires a history-read broker")
    if not _execution_authority_is_off(broker):
        raise ShadowWarmupDiagnosticError("SHADOW01_WARMUP_EXECUTION_AUTHORITY_INVALID")
    observed_at = _require_timestamp(observed_at_utc)
    requirements = _history_requirements(config)
    reports: list[WarmupMarketReport] = []
    requests_used = 0
    for symbol in _LIVE_READONLY_SMOKE_V2_REPRESENTATIVES:
        market = _market_or_none(registry, symbol)
        if market is None or market.state is not MarketDataState.AVAILABLE or not market.epic:
            reports.append(_unavailable_report(symbol, market))
            continue
        reports.append(
            _read_warmup_history(
                broker=broker,
                market=market,
                observed_at=observed_at,
                requirements=requirements,
            )
        )
        requests_used += 1
    result = tuple(reports)
    return WarmupDiagnosticReport(
        observed_at_utc=observed_at,
        execution_authority="OFF",
        non_persisting=True,
        request_budget=_LIVE_READONLY_SMOKE_V2_REQUEST_BUDGET,
        requests_used=requests_used,
        history_points_per_request=requirements.target,
        t1_minimum_completed_sessions=requirements.t1,
        m1_full_calibration_minimum_sessions=requirements.m1,
        q1_minimum_completed_sessions=requirements.q1,
        overall_data_quality=_overall_quality(result),
        markets=result,
    )


@dataclass(frozen=True)
class _HistoryRequirements:
    target: int
    t1: int
    m1: int
    q1: int


def _read_warmup_history(
    *,
    broker: WarmupDiagnosticBroker,
    market: MarketSpec,
    observed_at: datetime,
    requirements: _HistoryRequirements,
) -> WarmupMarketReport:
    """Read and classify one bounded history document without retaining it."""

    assert market.epic is not None
    try:
        raw = broker.read_historical_prices(market.epic, "DAY", requirements.target)
    except Exception:
        status_code, upstream_error_code = _response_diagnostic(broker)
        return _failure_report(
            market,
            requirements,
            "SHADOW01_WARMUP_HISTORY_UNAVAILABLE",
            http_status_code=status_code,
            upstream_error_code=upstream_error_code,
        )
    if not isinstance(raw, Mapping):
        return _failure_report(market, requirements, "SHADOW01_WARMUP_HISTORY_INVALID")
    rows = raw.get("prices")
    if not isinstance(rows, list):
        return _failure_report(market, requirements, "SHADOW01_WARMUP_HISTORY_INVALID")
    try:
        completed = parse_completed_daily_bars(raw, decision_timestamp_utc=observed_at)
    except ShadowDataError as error:
        return _failure_report(market, requirements, str(error), bars_received=len(rows))
    return _completed_report(market, requirements, len(rows), completed)


def _require_timestamp(value: datetime) -> datetime:
    try:
        return require_utc(value)
    except ValueError as error:
        raise ShadowWarmupDiagnosticError("SHADOW01_WARMUP_TIMESTAMP_INVALID") from error


def _require_verified_config(config: ShadowTournamentConfig) -> None:
    if not isinstance(config, ShadowTournamentConfig):
        raise ShadowWarmupDiagnosticError("SHADOW01_WARMUP_CONFIG_UNVERIFIED")
    try:
        payload = config.payload
        valid_fingerprint = config.fingerprint_is_valid
    except Exception:
        raise ShadowWarmupDiagnosticError("SHADOW01_WARMUP_CONFIG_UNVERIFIED") from None
    if valid_fingerprint is not True or payload.get("execution_authority") != "OFF":
        raise ShadowWarmupDiagnosticError("SHADOW01_WARMUP_CONFIG_UNVERIFIED")


def _require_authoritative_registry(registry: ShadowMarketRegistry) -> None:
    if not isinstance(registry, ShadowMarketRegistry):
        raise ShadowWarmupDiagnosticError("SHADOW01_WARMUP_DQ03_REGISTRY_UNVERIFIED")
    source_fingerprint = registry.source_fingerprint
    if (
        len(registry.markets) != 20
        or registry.source_path is None
        or not isinstance(source_fingerprint, str)
        or len(source_fingerprint) != 64
        or any(character not in "0123456789abcdef" for character in source_fingerprint.lower())
    ):
        raise ShadowWarmupDiagnosticError("SHADOW01_WARMUP_DQ03_REGISTRY_UNVERIFIED")


def _execution_authority_is_off(broker: WarmupDiagnosticBroker) -> bool:
    try:
        return broker.execution_authority == "OFF"
    except Exception:
        return False


def _response_diagnostic(broker: WarmupDiagnosticBroker) -> tuple[int | None, str | None]:
    """Extract only pre-sanitized 4xx/5xx evidence from an optional transport hook."""

    reader = getattr(broker, "latest_response_diagnostic", None)
    if not callable(reader):
        return None, None
    try:
        diagnostic = reader()
    except Exception:
        return None, None
    if not isinstance(diagnostic, Mapping):
        return None, None
    status_code = diagnostic.get("status_code")
    upstream_error_code = diagnostic.get("upstream_error_code")
    if (
        isinstance(status_code, bool)
        or not isinstance(status_code, int)
        or not 400 <= status_code < 600
    ):
        return None, None
    if upstream_error_code is not None and (
        not isinstance(upstream_error_code, str)
        or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,159}", upstream_error_code) is None
    ):
        return None, None
    return status_code, upstream_error_code


def _history_requirements(config: ShadowTournamentConfig) -> _HistoryRequirements:
    payload = config.payload
    history = _mapping(payload.get("history"))
    technical = _mapping(payload.get("technical"))
    reversion = _mapping(payload.get("reversion"))
    quality = _mapping(payload.get("quality"))
    windows = technical.get("return_windows")
    if not isinstance(windows, list) or not all(_positive_int(value) for value in windows):
        raise ShadowWarmupDiagnosticError("SHADOW01_WARMUP_CONFIG_INVALID")
    volatility_window = _required_positive_int(technical.get("volatility_window"))
    atr_window = _required_positive_int(technical.get("atr_window"))
    reversion_window = _required_positive_int(reversion.get("return_window"))
    percentile_lookback = _required_positive_int(reversion.get("percentile_lookback"))
    return _HistoryRequirements(
        target=_required_positive_int(history.get("target_completed_observations")),
        t1=max((*windows, volatility_window, atr_window)) + 1,
        m1=max(volatility_window, reversion_window) + percentile_lookback + 1,
        q1=_required_positive_int(quality.get("minimum_completed_observations")),
    )


def _mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def _positive_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _required_positive_int(value: object) -> int:
    if not _positive_int(value):
        raise ShadowWarmupDiagnosticError("SHADOW01_WARMUP_CONFIG_INVALID")
    assert isinstance(value, int)
    return value


def _market_or_none(registry: ShadowMarketRegistry, symbol: str) -> MarketSpec | None:
    try:
        return registry.by_symbol(symbol)
    except KeyError:
        return None


def _unavailable_report(symbol: str, market: MarketSpec | None) -> WarmupMarketReport:
    return WarmupMarketReport(
        symbol=symbol,
        epic=market.epic if market is not None else None,
        request_dispatched=False,
        bars_requested=0,
        bars_received=0,
        completed_sessions=0,
        latest_completed_session_utc=None,
        t1_history_ready=False,
        m1_history_ready=False,
        q1_history_ready=False,
        data_quality_result=WarmupDataQualityState.UNKNOWN,
        reason_codes=("SHADOW01_WARMUP_DQ03_MARKET_UNAVAILABLE",),
    )


def _budget_exhausted_report(market: MarketSpec) -> WarmupMarketReport:
    return WarmupMarketReport(
        symbol=market.symbol,
        epic=market.epic,
        request_dispatched=False,
        bars_requested=0,
        bars_received=0,
        completed_sessions=0,
        latest_completed_session_utc=None,
        t1_history_ready=False,
        m1_history_ready=False,
        q1_history_ready=False,
        data_quality_result=WarmupDataQualityState.UNKNOWN,
        reason_codes=("SHADOW01_WARMUP_REQUEST_BUDGET_EXHAUSTED",),
    )


def _failure_report(
    market: MarketSpec,
    requirements: _HistoryRequirements,
    reason: str,
    *,
    bars_received: int = 0,
    http_status_code: int | None = None,
    upstream_error_code: str | None = None,
) -> WarmupMarketReport:
    return WarmupMarketReport(
        symbol=market.symbol,
        epic=market.epic,
        request_dispatched=True,
        bars_requested=requirements.target,
        bars_received=bars_received,
        completed_sessions=0,
        latest_completed_session_utc=None,
        t1_history_ready=False,
        m1_history_ready=False,
        q1_history_ready=False,
        data_quality_result=WarmupDataQualityState.UNKNOWN,
        reason_codes=(reason,),
        http_status_code=http_status_code,
        upstream_error_code=upstream_error_code,
    )


def _completed_report(
    market: MarketSpec,
    requirements: _HistoryRequirements,
    received: int,
    completed: tuple[DailyBar, ...],
) -> WarmupMarketReport:
    completed_count = len(completed)
    t1_ready = completed_count >= requirements.t1
    m1_ready = completed_count >= requirements.m1
    q1_ready = completed_count >= requirements.q1
    reasons: list[str] = []
    if completed_count < requirements.t1:
        reasons.append("SHADOW01_WARMUP_T1_HISTORY_INSUFFICIENT")
    if completed_count < requirements.m1:
        reasons.append("SHADOW01_WARMUP_M1_FULL_CALIBRATION_INSUFFICIENT")
    if completed_count < requirements.q1:
        reasons.append("SHADOW01_WARMUP_Q1_HISTORY_INSUFFICIENT")
    if received != completed_count:
        reasons.append("SHADOW01_WARMUP_INCOMPLETE_ROWS_EXCLUDED")
    if t1_ready and m1_ready and q1_ready:
        state = WarmupDataQualityState.READY
        reasons.append("SHADOW01_WARMUP_HISTORY_READY")
    elif t1_ready or q1_ready:
        state = WarmupDataQualityState.WARNING
    else:
        state = WarmupDataQualityState.BLOCKED
    return WarmupMarketReport(
        symbol=market.symbol,
        epic=market.epic,
        request_dispatched=True,
        bars_requested=requirements.target,
        bars_received=received,
        completed_sessions=completed_count,
        latest_completed_session_utc=completed[-1].completed_at if completed else None,
        t1_history_ready=t1_ready,
        m1_history_ready=m1_ready,
        q1_history_ready=q1_ready,
        data_quality_result=state,
        reason_codes=tuple(reasons),
    )


def _overall_quality(values: tuple[WarmupMarketReport, ...]) -> WarmupDataQualityState:
    states = {item.data_quality_result for item in values}
    if WarmupDataQualityState.UNKNOWN in states:
        return WarmupDataQualityState.UNKNOWN
    if WarmupDataQualityState.BLOCKED in states:
        return WarmupDataQualityState.BLOCKED
    if WarmupDataQualityState.WARNING in states:
        return WarmupDataQualityState.WARNING
    return WarmupDataQualityState.READY


__all__ = (
    "ShadowWarmupDiagnostic",
    "ShadowWarmupDiagnosticError",
    "WarmupDataQualityState",
    "WarmupDiagnosticBroker",
    "WarmupDiagnosticReport",
    "WarmupMarketReport",
    "run_shadow_live_readonly_smoke_warmup_v2",
    "run_shadow_warmup_diagnostic",
)
