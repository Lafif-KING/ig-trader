"""Non-persisting, no-wait verification of the frozen Shadow01 session clock.

This module is deliberately separate from :mod:`runtime`: it has no store,
no epoch API, and no policy/decision imports.  It can only inspect bounded
read-only market metadata and completed daily history supplied by its caller.
The diagnostic is evidence for a human clock decision, never permission to
change the frozen V1 clock.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, time
from enum import StrEnum
from typing import Protocol

from src.ig_trader.shadow01.clock import (
    ShadowClockError,
    decision_anchor_for_date,
    decision_session_key,
    fx_anchor_follows_completed_session,
    new_york_local_date,
    require_decision_anchor,
)
from src.ig_trader.shadow01.config import ShadowTournamentConfig
from src.ig_trader.shadow01.data import ShadowDataError, parse_completed_daily_bars
from src.ig_trader.shadow01.live_quote import ShadowLiveQuote
from src.ig_trader.shadow01.models import DailyBar, MarketDataState, MarketSpec, require_utc
from src.ig_trader.shadow01.registry import ShadowMarketRegistry

_CLOCK_TIME = time(17, 10)
_CLOCK_TIMEZONE = "America/New_York"
_CLOCK_HISTORY_POINTS = 5
_MAX_CLOCK_REQUEST_BUDGET = 8
_PASS_REQUIREMENTS = (
    "PREVIOUS_REQUIRED_SESSION_COMPLETED",
    "COMPLETED_HISTORY_ONLY",
    "DETERMINISTIC_SESSION_KEY",
    "FRESH_CANONICAL_IG_PRICE_STREAM_QUOTE",
    "RESTART_IDEMPOTENCY_KEY_DETERMINISTIC",
    "MISSED_RUN_NEVER_RETROSPECTIVELY_BACKFILLED",
)


class ShadowClockDiagnosticError(ValueError):
    """The isolated clock diagnostic was configured outside its read-only bounds."""


class ClockDiagnosticState(StrEnum):
    """Conservative suitability states required for the human clock review."""

    PASS = "PASS"
    WARNING = "WARNING"
    UNSUITABLE = "UNSUITABLE"
    UNKNOWN = "UNKNOWN"


class ClockDiagnosticBroker(Protocol):
    """The entire broker surface available to the no-wait clock diagnostic."""

    @property
    def execution_authority(self) -> str: ...

    def read_market(self, epic: str) -> object: ...

    def read_historical_prices(self, epic: str, resolution: str, points: int) -> object: ...


@dataclass(frozen=True)
class ClockDiagnosticAssessment:
    """Value-safe evidence for one non-substitutable 17:10 NY contract."""

    asset_class: str
    symbol: str
    epic: str | None
    state: ClockDiagnosticState
    configured_clock: str
    market_status: str | None
    opening_hours_state: str
    completed_session_count: int
    latest_completed_session_utc: datetime | None
    proposed_clock_for_human_review: str | None
    reason_codes: tuple[str, ...]
    market_metadata_available: bool = False
    trading_hours_metadata_state: str = "NOT_OBSERVED"
    streaming_price_available: bool = False
    latest_completed_day_history_available: bool = False
    latest_completed_session_timestamp_parseable: bool = False
    current_incomplete_day_row_distinguishable: bool = False
    target_anchor_computable: bool = False
    target_anchor_after_completed_session_boundary: bool = False
    read_only_observation_operational: bool = False
    dst_conversion_valid: bool = False
    duplicate_session_key_deterministic: bool = False
    restart_idempotency_key_deterministic: bool = False
    history_rows_received: int = 0
    schedule_source_version: int | None = None
    opening_hours_available: bool = False
    market_times_count: int = 0
    target_anchor_in_declared_operational_window: bool | None = None

    def document(self) -> dict[str, object]:
        """Return a JSON-safe, explicitly non-prospective assessment."""

        return {
            "asset_class": self.asset_class,
            "symbol": self.symbol,
            "epic_present": self.epic is not None,
            "state": self.state.value,
            "configured_clock": self.configured_clock,
            "market_status": self.market_status,
            "opening_hours_state": self.opening_hours_state,
            "completed_session_count": self.completed_session_count,
            "history_rows_received": self.history_rows_received,
            "proposed_clock_for_human_review": self.proposed_clock_for_human_review,
            "evidence": {
                "market_metadata_available": self.market_metadata_available,
                "trading_hours_metadata_state": self.trading_hours_metadata_state,
                "streaming_price_available": self.streaming_price_available,
                "latest_completed_day_history_available": (
                    self.latest_completed_day_history_available
                ),
                "latest_completed_session_timestamp_parseable": (
                    self.latest_completed_session_timestamp_parseable
                ),
                "current_incomplete_day_row_distinguishable": (
                    self.current_incomplete_day_row_distinguishable
                ),
                "target_anchor_computable": self.target_anchor_computable,
                "target_anchor_after_completed_session_boundary": (
                    self.target_anchor_after_completed_session_boundary
                ),
                "read_only_observation_operational": self.read_only_observation_operational,
                "dst_conversion_valid": self.dst_conversion_valid,
                "duplicate_session_key_deterministic": self.duplicate_session_key_deterministic,
                "restart_idempotency_key_deterministic": (
                    self.restart_idempotency_key_deterministic
                ),
                "schedule_source_version": self.schedule_source_version,
                "opening_hours_available": self.opening_hours_available,
                "market_times_count": self.market_times_count,
                "target_anchor_in_declared_operational_window": (
                    self.target_anchor_in_declared_operational_window
                ),
            },
            "reason_codes": list(self.reason_codes),
        }


@dataclass(frozen=True)
class ClockDiagnosticReport:
    """A bounded, read-only report that has no persistence capability."""

    observed_at_utc: datetime
    execution_authority: str
    non_persisting: bool
    request_budget: int
    requests_used: int
    overall_state: ClockDiagnosticState
    assessments: tuple[ClockDiagnosticAssessment, ...]

    def document(self) -> dict[str, object]:
        """Return an artifact-ready report without credentials or mutable state."""

        return {
            "diagnostic": "SHADOW01_SESSION_CLOCK_READ_ONLY",
            "observed_at_utc_present": self.observed_at_utc is not None,
            "execution_authority": self.execution_authority,
            "non_persisting": self.non_persisting,
            "request_budget": self.request_budget,
            "requests_used": self.requests_used,
            "overall_state": self.overall_state.value,
            "pass_requirements": list(_PASS_REQUIREMENTS),
            "assessments": [item.document() for item in self.assessments],
        }


@dataclass(frozen=True)
class _HistoryEvidence:
    """Completed-bar evidence plus proof that the current DAY row is visible."""

    rows_received: int
    completed: tuple[DailyBar, ...]
    latest_timestamp_parseable: bool
    current_incomplete_row_distinguishable: bool
    reason_code: str | None


class ShadowSessionClockDiagnostic:
    """Inspect four fixed DQ-03 representatives without waiting or writing.

    The class intentionally receives no database, cache directory, callback,
    or sleep function.  Its only dependency is the narrow read-only protocol
    above, which prevents this diagnostic from creating epochs, snapshots,
    decisions, outcomes, or broker actions by construction.
    """

    _REPRESENTATIVES = (
        ("FX", "EURUSD"),
        ("METAL", "XAUUSD"),
        ("US500", "US500"),
        ("USTECH100", "USTECH100"),
    )

    def __init__(
        self,
        *,
        config: ShadowTournamentConfig,
        registry: ShadowMarketRegistry,
        broker: ClockDiagnosticBroker,
        live_quotes: Mapping[str, ShadowLiveQuote] | None = None,
        request_budget: int = _MAX_CLOCK_REQUEST_BUDGET,
    ) -> None:
        _require_verified_config(config)
        _require_authoritative_registry(registry)
        if not callable(getattr(broker, "read_market", None)):
            raise TypeError("Shadow01 clock diagnostic requires a market-read broker")
        if not callable(getattr(broker, "read_historical_prices", None)):
            raise TypeError("Shadow01 clock diagnostic requires a history-read broker")
        if not _execution_authority_is_off(broker):
            raise ShadowClockDiagnosticError("SHADOW01_CLOCK_EXECUTION_AUTHORITY_INVALID")
        if (
            isinstance(request_budget, bool)
            or not isinstance(request_budget, int)
            or request_budget < 1
            or request_budget > _MAX_CLOCK_REQUEST_BUDGET
        ):
            raise ShadowClockDiagnosticError("SHADOW01_CLOCK_REQUEST_BUDGET_INVALID")
        self._config = config
        self._registry = registry
        self._broker = broker
        self._live_quotes = dict(live_quotes or {})
        self._request_budget = request_budget

    def verify(self, *, observed_at_utc: datetime) -> ClockDiagnosticReport:
        """Return immediately with only current read-only diagnostic evidence.

        ``observed_at_utc`` is a caller-supplied diagnostic timestamp; the
        method deliberately does not wait for, schedule, or synthesize an
        anchor.  It never treats an unavailable fact as a good fact.
        """

        observed_at = _require_timestamp(observed_at_utc)
        configured_clock = _configured_clock(self._config)
        requests_used = 0
        assessments: list[ClockDiagnosticAssessment] = []
        for asset_class, symbol in self._REPRESENTATIVES:
            market = _market_or_none(self._registry, symbol)
            if market is None or market.state is not MarketDataState.AVAILABLE or not market.epic:
                assessments.append(
                    _unavailable_assessment(asset_class, symbol, market, configured_clock)
                )
                continue
            if configured_clock is None:
                assessments.append(_invalid_config_assessment(asset_class, market))
                continue
            assessment, used = self._assess_market(
                asset_class,
                market,
                observed_at,
                configured_clock,
                self._live_quotes,
                self._request_budget - requests_used,
            )
            requests_used += used
            assessments.append(assessment)
        result = tuple(assessments)
        return ClockDiagnosticReport(
            observed_at_utc=observed_at,
            execution_authority="OFF",
            non_persisting=True,
            request_budget=self._request_budget,
            requests_used=requests_used,
            overall_state=_overall_state(result),
            assessments=result,
        )

    def _assess_market(
        self,
        asset_class: str,
        market: MarketSpec,
        observed_at: datetime,
        configured_clock: str,
        live_quotes: Mapping[str, ShadowLiveQuote],
        remaining_budget: int,
    ) -> tuple[ClockDiagnosticAssessment, int]:
        """Use exactly one metadata and one small completed-history read."""

        return _assess_market_contract(
            config=self._config,
            broker=self._broker,
            asset_class=asset_class,
            market=market,
            observed_at=observed_at,
            configured_clock=configured_clock,
            live_quotes=live_quotes,
            remaining_budget=remaining_budget,
        )


def _assess_market_contract(
    *,
    config: ShadowTournamentConfig,
    broker: ClockDiagnosticBroker,
    asset_class: str,
    market: MarketSpec,
    observed_at: datetime,
    configured_clock: str,
    live_quotes: Mapping[str, ShadowLiveQuote],
    remaining_budget: int,
) -> tuple[ClockDiagnosticAssessment, int]:
    """Classify declared clock evidence without using it as a live-price proxy.

    Declared hours can establish only whether the frozen candidate warrants
    human review.  Completed-history ordering proves the session boundary;
    stream availability and Q1 remain separately fail-closed.  For FX the
    explicit 17:00 New York boundary is authoritative; the other
    representatives are checked from their own completed-history response.
    """

    assert market.epic is not None
    return _assess_market_without_schedule_probe(
        config=config,
        broker=broker,
        asset_class=asset_class,
        market=market,
        observed_at=observed_at,
        configured_clock=configured_clock,
        live_quotes=live_quotes,
        remaining_budget=remaining_budget,
    )


def _contract_assessment(
    *,
    asset_class: str,
    market: MarketSpec,
    state: ClockDiagnosticState,
    configured_clock: str,
    market_status: str | None = None,
    opening_hours_state: str = "NOT_OBSERVED",
    completed_session_count: int = 0,
    latest_completed_session_utc: datetime | None = None,
    proposed_clock_for_human_review: str | None = None,
    reason_codes: tuple[str, ...] = (),
    **evidence: object,
) -> ClockDiagnosticAssessment:
    return ClockDiagnosticAssessment(
        asset_class=asset_class,
        symbol=market.symbol,
        epic=market.epic,
        state=state,
        configured_clock=configured_clock,
        market_status=market_status,
        opening_hours_state=opening_hours_state,
        completed_session_count=completed_session_count,
        latest_completed_session_utc=latest_completed_session_utc,
        proposed_clock_for_human_review=proposed_clock_for_human_review,
        reason_codes=reason_codes,
        **evidence,
    )


def _contract_evidence(
    *,
    metadata_available: bool,
    trading_hours_state: str,
    stream_available: bool,
    history: _HistoryEvidence,
    target_anchor: datetime | None,
    anchor_after_boundary: bool,
    market_status: str | None,
    operational: bool,
    dst_valid: bool,
    deterministic_keys: bool,
) -> dict[str, object]:
    return {
        "market_metadata_available": metadata_available,
        "trading_hours_metadata_state": trading_hours_state,
        "streaming_price_available": stream_available,
        "latest_completed_day_history_available": bool(history.completed),
        "latest_completed_session_timestamp_parseable": history.latest_timestamp_parseable,
        "current_incomplete_day_row_distinguishable": (
            history.current_incomplete_row_distinguishable
        ),
        "target_anchor_computable": target_anchor is not None,
        "target_anchor_after_completed_session_boundary": anchor_after_boundary,
        "read_only_observation_operational": operational,
        "dst_conversion_valid": dst_valid,
        "duplicate_session_key_deterministic": deterministic_keys,
        "restart_idempotency_key_deterministic": deterministic_keys,
        "history_rows_received": history.rows_received,
    }


def _assess_market_without_schedule_probe(
    *,
    config: ShadowTournamentConfig,
    broker: ClockDiagnosticBroker,
    asset_class: str,
    market: MarketSpec,
    observed_at: datetime,
    configured_clock: str,
    live_quotes: Mapping[str, ShadowLiveQuote],
    remaining_budget: int,
) -> tuple[ClockDiagnosticAssessment, int]:
    """Use causal history and canonical stream quotes, never a schedule probe."""

    assert market.epic is not None
    if remaining_budget < 2:
        return (
            _contract_assessment(
                asset_class=asset_class,
                market=market,
                state=ClockDiagnosticState.UNKNOWN,
                configured_clock=configured_clock,
                opening_hours_state="NOT_READ_REQUEST_BUDGET_EXHAUSTED",
                reason_codes=("SHADOW01_CLOCK_REQUEST_BUDGET_EXHAUSTED",),
            ),
            0,
        )
    metadata, metadata_reason = _read_metadata(broker, market.epic)
    target_anchor, dst_valid = _target_anchor(config, observed_at)
    history = _read_clock_history(broker, market.epic, target_anchor or observed_at)
    metadata_available = metadata is not None
    identity_valid = metadata is not None and _metadata_identity_matches(metadata, market.epic)
    stream_available = _fresh_canonical_stream_quote(
        live_quotes,
        market=market,
        observed_at=observed_at,
        config=config,
    )
    completed = history.completed
    latest = completed[-1].completed_at if completed else None
    market_status = _market_status(metadata)
    anchor_after_boundary = _anchor_after_completed_boundary(
        asset_class=asset_class,
        config=config,
        observed_at=observed_at,
        target_anchor=target_anchor,
        latest_completed=latest,
    )
    deterministic_keys = _deterministic_keys(config, market.symbol, target_anchor)
    declared_hours_state = _declared_hours_state(metadata)
    operational = bool(
        metadata_available
        and identity_valid
        and stream_available
        and completed
        and market_status in {"TRADEABLE", "OPEN"}
    )
    evidence = _contract_evidence(
        metadata_available=metadata_available,
        trading_hours_state=declared_hours_state,
        stream_available=stream_available,
        history=history,
        target_anchor=target_anchor,
        anchor_after_boundary=anchor_after_boundary,
        market_status=market_status,
        operational=operational,
        dst_valid=dst_valid,
        deterministic_keys=deterministic_keys,
    )
    evidence.update(
        schedule_source_version=None,
        opening_hours_available=declared_hours_state == "DECLARED_HOURS_AVAILABLE",
        market_times_count=0,
        target_anchor_in_declared_operational_window=None,
    )
    reasons = _clock_reason_codes(
        metadata_reason=metadata_reason,
        history_reason=history.reason_code,
        identity_valid=identity_valid,
        stream_available=stream_available,
        history=history,
        target_anchor=target_anchor,
        anchor_after_boundary=anchor_after_boundary,
        market_status=market_status,
        operational=operational,
        dst_valid=dst_valid,
        deterministic_keys=deterministic_keys,
        declared_hours_state=declared_hours_state,
    )
    passes = (
        metadata_available
        and identity_valid
        and stream_available
        and bool(completed)
        and history.latest_timestamp_parseable
        and history.current_incomplete_row_distinguishable
        and target_anchor is not None
        and anchor_after_boundary
        and market_status in {"TRADEABLE", "OPEN"}
        and operational
        and dst_valid
        and deterministic_keys
    )
    return (
        _contract_assessment(
            asset_class=asset_class,
            market=market,
            state=ClockDiagnosticState.PASS if passes else ClockDiagnosticState.UNKNOWN,
            configured_clock=configured_clock,
            market_status=market_status,
            opening_hours_state=declared_hours_state,
            completed_session_count=len(completed),
            latest_completed_session_utc=latest,
            reason_codes=reasons,
            **evidence,
        ),
        2,
    )


def _clock_reason_codes(
    *,
    metadata_reason: str | None,
    history_reason: str | None,
    identity_valid: bool,
    stream_available: bool,
    history: _HistoryEvidence,
    target_anchor: datetime | None,
    anchor_after_boundary: bool,
    market_status: str | None,
    operational: bool,
    dst_valid: bool,
    deterministic_keys: bool,
    declared_hours_state: str,
) -> tuple[str, ...]:
    values: list[str | None] = [metadata_reason, history_reason]
    if not identity_valid:
        values.append("SHADOW01_CLOCK_MARKET_IDENTITY_UNVERIFIED")
    if not stream_available:
        values.append("SHADOW01_CLOCK_STREAMING_PRICE_AVAILABILITY_UNPROVEN")
    if not history.completed:
        values.append("SHADOW01_CLOCK_LATEST_COMPLETED_HISTORY_UNAVAILABLE")
    if not history.latest_timestamp_parseable:
        values.append("SHADOW01_CLOCK_COMPLETED_HISTORY_TIMESTAMP_UNPARSEABLE")
    if not history.current_incomplete_row_distinguishable:
        values.append("SHADOW01_CLOCK_INCOMPLETE_DAY_ROW_NOT_DISTINGUISHABLE")
    if declared_hours_state == "DECLARED_HOURS_NOT_PROVIDED":
        values.append("SHADOW01_DECLARED_HOURS_NOT_PROVIDED")
    elif declared_hours_state == "DECLARED_HOURS_ADVISORY_UNUSABLE":
        values.append("SHADOW01_DECLARED_HOURS_ADVISORY_UNUSABLE")
    if target_anchor is None:
        values.append("SHADOW01_CLOCK_TARGET_ANCHOR_UNCOMPUTABLE")
    if not anchor_after_boundary:
        values.append("SHADOW01_CLOCK_ANCHOR_AFTER_COMPLETED_BOUNDARY_UNPROVEN")
    if market_status not in {"TRADEABLE", "OPEN"}:
        values.append("SHADOW01_CLOCK_V4_MARKET_STATUS_UNAVAILABLE")
    if not operational:
        values.append("SHADOW01_CLOCK_READ_ONLY_OBSERVATION_UNPROVEN")
    if not dst_valid:
        values.append("SHADOW01_CLOCK_DST_CONVERSION_UNPROVEN")
    if not deterministic_keys:
        values.append("SHADOW01_CLOCK_IDEMPOTENCY_KEY_UNPROVEN")
    if not values:
        values.append("SHADOW01_CLOCK_ANCHOR_OPERATIONALLY_SUPPORTED")
    return tuple(value for value in values if value is not None)


def verify_shadow_session_clock(
    *,
    config: ShadowTournamentConfig,
    registry: ShadowMarketRegistry,
    broker: ClockDiagnosticBroker,
    observed_at_utc: datetime,
    live_quotes: Mapping[str, ShadowLiveQuote] | None = None,
    request_budget: int = _MAX_CLOCK_REQUEST_BUDGET,
) -> ClockDiagnosticReport:
    """Run the isolated no-wait clock diagnostic through a bounded read surface."""

    return ShadowSessionClockDiagnostic(
        config=config,
        registry=registry,
        broker=broker,
        live_quotes=live_quotes,
        request_budget=request_budget,
    ).verify(observed_at_utc=observed_at_utc)


@dataclass(frozen=True)
class _OpeningHoursEvidence:
    state: str
    opening_time: time | None
    timezone: str | None


def _require_timestamp(value: datetime) -> datetime:
    try:
        return require_utc(value)
    except ValueError as error:
        raise ShadowClockDiagnosticError("SHADOW01_CLOCK_DIAGNOSTIC_TIMESTAMP_INVALID") from error


def _require_verified_config(config: ShadowTournamentConfig) -> None:
    if not isinstance(config, ShadowTournamentConfig):
        raise ShadowClockDiagnosticError("SHADOW01_CLOCK_CONFIG_UNVERIFIED")
    try:
        payload = config.payload
        valid_fingerprint = config.fingerprint_is_valid
    except Exception:
        raise ShadowClockDiagnosticError("SHADOW01_CLOCK_CONFIG_UNVERIFIED") from None
    if valid_fingerprint is not True or payload.get("execution_authority") != "OFF":
        raise ShadowClockDiagnosticError("SHADOW01_CLOCK_CONFIG_UNVERIFIED")


def _require_authoritative_registry(registry: ShadowMarketRegistry) -> None:
    if not isinstance(registry, ShadowMarketRegistry):
        raise ShadowClockDiagnosticError("SHADOW01_CLOCK_DQ03_REGISTRY_UNVERIFIED")
    source_fingerprint = registry.source_fingerprint
    if (
        len(registry.markets) != 20
        or registry.source_path is None
        or not isinstance(source_fingerprint, str)
        or len(source_fingerprint) != 64
        or any(character not in "0123456789abcdef" for character in source_fingerprint.lower())
    ):
        raise ShadowClockDiagnosticError("SHADOW01_CLOCK_DQ03_REGISTRY_UNVERIFIED")


def _configured_clock(config: ShadowTournamentConfig) -> str | None:
    clock = config.decision_clock
    if clock.get("timezone") != _CLOCK_TIMEZONE or clock.get("local_time") != "17:10":
        return None
    return "17:10 America/New_York"


def _market_or_none(registry: ShadowMarketRegistry, symbol: str) -> MarketSpec | None:
    try:
        return registry.by_symbol(symbol)
    except KeyError:
        return None


def _unavailable_assessment(
    asset_class: str,
    symbol: str,
    market: MarketSpec | None,
    configured_clock: str | None,
) -> ClockDiagnosticAssessment:
    return ClockDiagnosticAssessment(
        asset_class=asset_class,
        symbol=symbol,
        epic=market.epic if market is not None else None,
        state=ClockDiagnosticState.UNKNOWN,
        configured_clock=configured_clock or "UNVERIFIED_CONFIGURED_CLOCK",
        market_status=None,
        opening_hours_state="NOT_READ_DQ03_MARKET_UNAVAILABLE",
        completed_session_count=0,
        latest_completed_session_utc=None,
        proposed_clock_for_human_review=None,
        reason_codes=("SHADOW01_CLOCK_DQ03_MARKET_UNAVAILABLE",),
    )


def _invalid_config_assessment(asset_class: str, market: MarketSpec) -> ClockDiagnosticAssessment:
    return ClockDiagnosticAssessment(
        asset_class=asset_class,
        symbol=market.symbol,
        epic=market.epic,
        state=ClockDiagnosticState.UNKNOWN,
        configured_clock="UNVERIFIED_CONFIGURED_CLOCK",
        market_status=None,
        opening_hours_state="NOT_READ_CONFIG_INVALID",
        completed_session_count=0,
        latest_completed_session_utc=None,
        proposed_clock_for_human_review=None,
        reason_codes=("SHADOW01_SESSION_CLOCK_HUMAN_GATE_REQUIRED",),
    )


def _read_metadata(
    broker: ClockDiagnosticBroker, epic: str
) -> tuple[Mapping[str, object] | None, str | None]:
    try:
        value = broker.read_market(epic)
    except Exception:
        return None, "SHADOW01_CLOCK_METADATA_UNAVAILABLE"
    if not isinstance(value, Mapping):
        return None, "SHADOW01_CLOCK_METADATA_INVALID"
    return value, None


def _read_clock_history(
    broker: ClockDiagnosticBroker, epic: str, target_anchor: datetime
) -> _HistoryEvidence:
    """Read a bounded DAY sample and prove why its current row is excluded."""

    try:
        value = broker.read_historical_prices(epic, "DAY", _CLOCK_HISTORY_POINTS)
    except Exception:
        return _HistoryEvidence(0, (), False, False, "SHADOW01_CLOCK_HISTORY_UNAVAILABLE")
    if not isinstance(value, Mapping):
        return _HistoryEvidence(0, (), False, False, "SHADOW01_CLOCK_HISTORY_INVALID")
    rows = value.get("prices")
    if not isinstance(rows, list):
        return _HistoryEvidence(0, (), False, False, "SHADOW01_CLOCK_HISTORY_INVALID")
    current_row_distinguishable = _has_current_daily_row(rows, target_anchor)
    try:
        completed = parse_completed_daily_bars(value, decision_timestamp_utc=target_anchor)
    except ShadowDataError as error:
        return _HistoryEvidence(len(rows), (), False, current_row_distinguishable, str(error))
    return _HistoryEvidence(
        len(rows), completed, bool(completed), current_row_distinguishable, None
    )


def _has_current_daily_row(rows: list[object], anchor: datetime) -> bool:
    """Require an explicit current-day row; malformed rows never count as proof."""

    for row in rows:
        timestamp = _clock_history_timestamp(row)
        if timestamp is None:
            continue
        if timestamp >= anchor:
            return True
        if timestamp.date() == anchor.date() and timestamp.time() == time():
            return True
    return False


def _clock_history_timestamp(row: object) -> datetime | None:
    if not isinstance(row, Mapping):
        return None
    value = row.get("snapshotTimeUTC")
    if isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)
    value = row.get("snapshotTime")
    if not isinstance(value, str):
        return None
    try:
        return datetime.strptime(value, "%Y/%m/%d %H:%M:%S").replace(tzinfo=UTC)
    except ValueError:
        return None


def _market_status(metadata: Mapping[str, object] | None) -> str | None:
    for block in _metadata_blocks(metadata):
        value = block.get("marketStatus")
        if isinstance(value, str) and value.strip():
            return value.strip().upper()
    return None


def _streaming_price_available(metadata: Mapping[str, object] | None) -> bool:
    """Use only the explicit IG metadata capability, never a schedule inference."""

    return any(
        block.get("streamingPricesAvailable") is True for block in _metadata_blocks(metadata)
    )


def _fresh_canonical_stream_quote(
    live_quotes: Mapping[str, ShadowLiveQuote],
    *,
    market: MarketSpec,
    observed_at: datetime,
    config: ShadowTournamentConfig,
) -> bool:
    """Accept only the actual fresh IG Price-stream quote for this observation."""

    if market.epic is None:
        return False
    quote = live_quotes.get(market.epic)
    if (
        not isinstance(quote, ShadowLiveQuote)
        or quote.epic != market.epic
        or quote.symbol != market.symbol
        or quote.source != "IG_PRICE_STREAM"
        or quote.quality != "VALID_QUOTE"
        or quote.timestamp_utc is None
        or quote.quote_age_seconds is None
    ):
        return False
    quality = config.payload.get("quality")
    maximum_age = quality.get("maximum_price_age_seconds") if isinstance(quality, Mapping) else None
    if isinstance(maximum_age, bool) or not isinstance(maximum_age, int) or maximum_age < 1:
        return False
    try:
        quote_timestamp = require_utc(quote.timestamp_utc)
    except ValueError:
        return False
    age_seconds = (observed_at - quote_timestamp).total_seconds()
    return 0.0 <= age_seconds <= maximum_age and quote.quote_age_seconds <= maximum_age


def _declared_hours_state(metadata: Mapping[str, object] | None) -> str:
    """Classify optional declared-hours evidence without making another call."""

    for block in _metadata_blocks(metadata):
        if "openingHours" not in block or block.get("openingHours") is None:
            continue
        opening_hours = block.get("openingHours")
        if isinstance(opening_hours, Mapping) and isinstance(
            opening_hours.get("marketTimes"), list
        ):
            return "DECLARED_HOURS_AVAILABLE"
        return "DECLARED_HOURS_ADVISORY_UNUSABLE"
    return "DECLARED_HOURS_NOT_PROVIDED"


def _trading_hours_metadata_state(metadata: Mapping[str, object] | None) -> str:
    """Record schedule availability without treating it as CFD availability."""

    for block in _metadata_blocks(metadata):
        opening_hours = block.get("openingHours")
        if isinstance(opening_hours, Mapping):
            return "PRESENT" if isinstance(opening_hours.get("marketTimes"), list) else "INVALID"
    return "UNAVAILABLE"


def _target_anchor(
    config: ShadowTournamentConfig, observed_at: datetime
) -> tuple[datetime | None, bool]:
    try:
        anchor = decision_anchor_for_date(config, new_york_local_date(observed_at))
        return anchor, require_decision_anchor(config, anchor) == anchor
    except ShadowClockError:
        return None, False


def _anchor_after_completed_boundary(
    *,
    asset_class: str,
    config: ShadowTournamentConfig,
    observed_at: datetime,
    target_anchor: datetime | None,
    latest_completed: datetime | None,
) -> bool:
    if target_anchor is None:
        return False
    if asset_class == "FX":
        return fx_anchor_follows_completed_session(config, new_york_local_date(observed_at))
    return latest_completed is not None and latest_completed < target_anchor


def _deterministic_keys(
    config: ShadowTournamentConfig, symbol: str, target_anchor: datetime | None
) -> bool:
    if target_anchor is None:
        return False
    try:
        first = decision_session_key(
            config, instrument=symbol, decision_timestamp_utc=target_anchor
        )
        second = decision_session_key(
            config, instrument=symbol, decision_timestamp_utc=target_anchor
        )
    except ShadowClockError:
        return False
    return first == second


def _execution_authority_is_off(broker: ClockDiagnosticBroker) -> bool:
    try:
        return broker.execution_authority == "OFF"
    except Exception:
        return False


def _latest_completed_session_is_fresh(
    config: ShadowTournamentConfig,
    latest: datetime | None,
    observed_at: datetime,
) -> bool:
    """Require recent completed-session evidence before declaring a live PASS."""

    if latest is None:
        return False
    quality = config.payload.get("quality")
    if not isinstance(quality, Mapping):
        return False
    maximum_age = quality.get("maximum_price_age_seconds")
    if isinstance(maximum_age, bool) or not isinstance(maximum_age, int) or maximum_age < 1:
        return False
    age_seconds = (observed_at - latest).total_seconds()
    return 0 <= age_seconds <= maximum_age


def _metadata_identity_matches(metadata: Mapping[str, object], expected_epic: str) -> bool:
    """Require an explicit matching broker identity for the fixed representative."""

    matched = False
    for block in _metadata_blocks(metadata):
        if "epic" not in block:
            continue
        value = block.get("epic")
        if not isinstance(value, str) or value.strip() != expected_epic:
            return False
        matched = True
    return matched


def _metadata_blocks(metadata: Mapping[str, object] | None) -> tuple[Mapping[str, object], ...]:
    if metadata is None:
        return ()
    blocks: list[Mapping[str, object]] = [metadata]
    for key in ("instrument", "snapshot"):
        value = metadata.get(key)
        if isinstance(value, Mapping):
            blocks.append(value)
    return tuple(blocks)


def _opening_hours_evidence(metadata: Mapping[str, object] | None) -> _OpeningHoursEvidence:
    if metadata is None:
        return _OpeningHoursEvidence("METADATA_UNAVAILABLE", None, None)
    for block in _metadata_blocks(metadata):
        opening_hours = block.get("openingHours")
        if not isinstance(opening_hours, Mapping):
            continue
        timezone = _opening_hours_timezone(opening_hours, block)
        market_times = opening_hours.get("marketTimes")
        if not isinstance(market_times, list) or timezone != _CLOCK_TIMEZONE:
            return _OpeningHoursEvidence("DECLARED_HOURS_TIMEZONE_UNVERIFIED", None, timezone)
        intervals = _opening_intervals(market_times)
        if not intervals:
            return _OpeningHoursEvidence("DECLARED_HOURS_INVALID", None, timezone)
        if any(opening_time == closing_time for opening_time, closing_time in intervals):
            # IG's compact equal-time representation can mean a full-day
            # session or a zero-width closure.  It cannot establish that the
            # frozen anchor is inside or outside a session without an explicit
            # contract, so never turn that ambiguity into an UNSUITABLE claim.
            return _OpeningHoursEvidence("DECLARED_HOURS_AMBIGUOUS", None, timezone)
        if any(_contains_clock(interval, _CLOCK_TIME) for interval in intervals):
            return _OpeningHoursEvidence("ANCHOR_WITHIN_DECLARED_HOURS", None, timezone)
        first_open = min(interval[0] for interval in intervals)
        return _OpeningHoursEvidence("ANCHOR_OUTSIDE_DECLARED_HOURS", first_open, timezone)
    return _OpeningHoursEvidence("DECLARED_HOURS_UNAVAILABLE", None, None)


def _opening_hours_timezone(
    opening_hours: Mapping[str, object], block: Mapping[str, object]
) -> str | None:
    for value in (opening_hours.get("timezone"), block.get("timezone")):
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _opening_intervals(values: list[object]) -> tuple[tuple[time, time], ...]:
    intervals: list[tuple[time, time]] = []
    for value in values:
        if not isinstance(value, Mapping):
            continue
        opening_time = _parse_clock_time(value.get("openTime"))
        closing_time = _parse_clock_time(value.get("closeTime"))
        if opening_time is not None and closing_time is not None:
            intervals.append((opening_time, closing_time))
    return tuple(intervals)


def _parse_clock_time(value: object) -> time | None:
    if not isinstance(value, str):
        return None
    parts = value.strip().split(":")
    if len(parts) not in {2, 3} or not all(part.isdigit() for part in parts):
        return None
    hour, minute = int(parts[0]), int(parts[1])
    second = int(parts[2]) if len(parts) == 3 else 0
    try:
        return time(hour, minute, second)
    except ValueError:
        return None


def _contains_clock(interval: tuple[time, time], anchor: time) -> bool:
    opening_time, closing_time = interval
    if opening_time == closing_time:
        return False
    if opening_time < closing_time:
        return opening_time <= anchor < closing_time
    return anchor >= opening_time or anchor < closing_time


def _proposed_clock(evidence: _OpeningHoursEvidence) -> str | None:
    if evidence.opening_time is None or evidence.timezone != _CLOCK_TIMEZONE:
        return None
    opening_minutes = evidence.opening_time.hour * 60 + evidence.opening_time.minute
    proposed_minutes = (opening_minutes + 10) % (24 * 60)
    proposed = time(proposed_minutes // 60, proposed_minutes % 60)
    return f"{proposed.strftime('%H:%M')} {evidence.timezone} (HUMAN_REVIEW_REQUIRED)"


def _without_none(values: list[str | None]) -> tuple[str, ...]:
    return tuple(value for value in values if value is not None)


def _overall_state(values: tuple[ClockDiagnosticAssessment, ...]) -> ClockDiagnosticState:
    states = {item.state for item in values}
    if ClockDiagnosticState.UNSUITABLE in states:
        return ClockDiagnosticState.UNSUITABLE
    if ClockDiagnosticState.UNKNOWN in states:
        return ClockDiagnosticState.UNKNOWN
    if ClockDiagnosticState.WARNING in states:
        return ClockDiagnosticState.WARNING
    return ClockDiagnosticState.PASS


__all__ = (
    "ClockDiagnosticAssessment",
    "ClockDiagnosticBroker",
    "ClockDiagnosticReport",
    "ClockDiagnosticState",
    "ShadowClockDiagnosticError",
    "ShadowSessionClockDiagnostic",
    "verify_shadow_session_clock",
)
