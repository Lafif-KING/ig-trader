"""Later outcome labels, intentionally separate from causal feature building."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from src.ig_trader.shadow01.models import (
    DailyBar,
    Direction,
    OutcomeLabel,
    QualityState,
    ShadowDecision,
    is_finite_positive,
    require_utc,
)

OUTCOME_HORIZONS = (1, 3, 5, 10, 20)


@dataclass(frozen=True)
class OutcomeResolutionInput:
    """A decision's known entry facts and subsequent completed market sessions."""

    decision: ShadowDecision
    entry_price: float
    atr20_over_price: float | None
    future_completed_bars: tuple[DailyBar, ...]

    def __post_init__(self) -> None:
        _validate_resolution_input(self)


def _validate_resolution_input(value: OutcomeResolutionInput) -> None:
    """Validate again at resolution time, not only at frozen-dataclass construction."""

    try:
        bars = tuple(value.future_completed_bars)
    except TypeError as error:
        raise ValueError("SHADOW01_OUTCOME_SESSION_INVALID") from error
    if not isinstance(value.decision, ShadowDecision) or value.decision.direction not in {
        Direction.LONG,
        Direction.SHORT,
    }:
        raise ValueError("SHADOW01_OUTCOME_REQUIRES_DIRECTIONAL_DECISION")
    value.decision.__post_init__()
    if not is_finite_positive(value.entry_price):
        raise ValueError("SHADOW01_OUTCOME_ENTRY_PRICE_INVALID")
    if value.atr20_over_price is not None and not is_finite_positive(value.atr20_over_price):
        raise ValueError("SHADOW01_OUTCOME_ATR_INVALID")

    decision_timestamp = require_utc(value.decision.decision_timestamp_utc)
    if not all(isinstance(bar, DailyBar) for bar in bars):
        raise ValueError("SHADOW01_OUTCOME_SESSION_INVALID")
    timestamps = tuple(require_utc(bar.completed_at) for bar in bars)
    if tuple(sorted(timestamps)) != timestamps or len(set(timestamps)) != len(timestamps):
        raise ValueError("SHADOW01_OUTCOME_SESSIONS_MUST_BE_STRICTLY_ORDERED")
    for bar in bars:
        _validate_completed_bar(bar, decision_timestamp)
    object.__setattr__(value, "future_completed_bars", bars)


def resolve_outcomes(value: OutcomeResolutionInput) -> tuple[OutcomeLabel, ...]:
    """Resolve fixed horizons only from completed sessions after a recorded decision."""

    if not isinstance(value, OutcomeResolutionInput):
        raise TypeError("SHADOW01_OUTCOME_RESOLUTION_INPUT_INVALID")
    _validate_resolution_input(value)
    return tuple(
        _outcome(value, value.future_completed_bars, horizon) for horizon in OUTCOME_HORIZONS
    )


def _outcome(
    value: OutcomeResolutionInput, bars: tuple[DailyBar, ...], horizon: int
) -> OutcomeLabel:
    if len(bars) < horizon:
        return OutcomeLabel(
            decision_id=value.decision.decision_id,
            horizon_sessions=horizon,
            reference_entry_price=value.entry_price,
            future_price=None,
            raw_directional_return=None,
            atr_normalized_return=None,
            cost_adjusted_result=None,
            outcome_timestamp_utc=None,
            quality=QualityState.BLOCKED,
            blocked_reason="OUTCOME_COMPLETED_SESSION_UNAVAILABLE",
        )
    bar = bars[horizon - 1]
    raw = _directional_return(value.decision.direction, value.entry_price, bar.close)
    atr_normalized = raw / value.atr20_over_price if value.atr20_over_price else None
    return OutcomeLabel(
        decision_id=value.decision.decision_id,
        horizon_sessions=horizon,
        reference_entry_price=value.entry_price,
        future_price=bar.close,
        raw_directional_return=raw,
        atr_normalized_return=atr_normalized,
        cost_adjusted_result=None,
        outcome_timestamp_utc=bar.completed_at.astimezone(UTC),
        quality=QualityState.NORMAL,
        blocked_reason=None,
    )


def _directional_return(direction: Direction, entry: float, future: float) -> float:
    if not is_finite_positive(entry) or not is_finite_positive(future):
        raise ValueError("SHADOW01_OUTCOME_PRICE_INVALID")
    if direction is Direction.LONG:
        return future / entry - 1.0
    if direction is Direction.SHORT:
        return entry / future - 1.0
    raise ValueError("SHADOW01_OUTCOME_DIRECTION_INVALID")


def _validate_completed_bar(bar: DailyBar, decision_timestamp: datetime) -> None:
    """Recheck persisted-looking bars: frozen dataclasses can still be bypassed in Python."""

    completed_at = require_utc(bar.completed_at)
    if completed_at <= decision_timestamp:
        raise ValueError("SHADOW01_OUTCOME_NON_FUTURE_SESSION")
    values = (bar.open, bar.high, bar.low, bar.close)
    if not all(is_finite_positive(item) for item in values):
        raise ValueError("SHADOW01_OUTCOME_FUTURE_PRICE_INVALID")
    if not bar.low <= bar.open <= bar.high or not bar.low <= bar.close <= bar.high:
        raise ValueError("SHADOW01_OUTCOME_FUTURE_BAR_GEOMETRY_INVALID")
    if bar.bid is not None and not is_finite_positive(bar.bid):
        raise ValueError("SHADOW01_OUTCOME_FUTURE_BID_INVALID")
    if bar.offer is not None and not is_finite_positive(bar.offer):
        raise ValueError("SHADOW01_OUTCOME_FUTURE_OFFER_INVALID")
    if bar.bid is not None and bar.offer is not None and bar.offer < bar.bid:
        raise ValueError("SHADOW01_OUTCOME_FUTURE_SPREAD_INVALID")
