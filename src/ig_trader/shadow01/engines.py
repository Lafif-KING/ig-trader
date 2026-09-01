"""Pure, causal observation engines for the Shadow Tournament.

The functions in this module accept only completed observations and explicit
context facts.  They do not acquire data, construct broker clients, or import
any order, position, Demo, or execution module.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from statistics import fmean

from src.ig_trader.shadow01.config import ShadowTournamentConfig
from src.ig_trader.shadow01.models import (
    ContextState,
    CostContext,
    CostState,
    CrossAssetOpinion,
    DailyBar,
    Direction,
    FundamentalContext,
    FundamentalState,
    MarketSnapshot,
    QualityAssessment,
    QualityState,
    ReversionOpinion,
    TechnicalOpinion,
    TechnicalState,
    require_utc,
)


class ShadowEngineError(ValueError):
    """Raised when a caller tries to calculate an engine from non-causal facts."""


"""The frozen T1 calculation used by ``compute_technical_state`` and T1."""

"""The causal, tie-stable M1 percentile calculation."""


@dataclass(frozen=True)
class CostInputs:
    """Explicit, read-only cost facts supplied by a market-data caller.

    ``spread`` and ``reference_price`` must refer to the same market snapshot.
    This module never fills either value from a provider or another instrument.
    """

    reference_price: float | None
    spread: float | None
    minimum_stop_distance: float | None = None
    product_type: str | None = None
    funding_metadata: str | None = None


@dataclass(frozen=True)
class CrossAssetInput:
    """One already-aligned, causally available X1 context fact.

    ``alignment_score`` is deliberately supplied by the caller rather than
    inferred here: -1 means materially opposed to the candidate directional
    opinion, +1 means materially supportive, and 0 is neutral.  Its
    ``available_at_utc`` is when the fact became usable by Shadow01.
    """

    name: str
    alignment_score: float | None
    available_at_utc: datetime | None
    source: str | None = None


@dataclass(frozen=True)
class FundamentalInputs:
    """Explicit, context-only F1 facts with no directional authority."""

    policy_state: str | None = None
    policy_trend: str | None = None
    data_quality: str | None = None
    event_risk: bool | None = None
    available_at_utc: datetime | None = None


def compute_technical_state(
    source: MarketSnapshot | Sequence[DailyBar],
    config: ShadowTournamentConfig,
    *,
    decision_timestamp_utc: datetime | None = None,
) -> TechnicalState:
    """Calculate T1's causal descriptive state from completed daily bars.

    A ``MarketSnapshot`` carries its own decision timestamp.  Callers passing
    a bare sequence must supply ``decision_timestamp_utc`` so the engine can
    reject an incomplete or future bar instead of silently consuming it.

    Returns are close-to-close simple returns.  ATR20 uses the final twenty
    completed true ranges.  Realized volatility is the unannualized population
    standard deviation of the final twenty (or sixty for T1) simple returns.
    ``trend_20`` and ``trend_60`` are the frozen normalized T1 values.
    """

    bars, _ = _causal_bars(source, decision_timestamp_utc)
    technical = _technical_rules(config)
    return_windows = technical.return_windows

    values = {window: _return_ending_at(bars, len(bars) - 1, window) for window in return_windows}
    atr20 = _atr_ending_at(bars, len(bars) - 1, technical.atr_window)
    atr20_over_price = atr20 / bars[-1].close if atr20 is not None else None
    volatility_20 = _realized_volatility_ending_at(bars, len(bars) - 1, technical.volatility_window)
    volatility_60 = _realized_volatility_ending_at(bars, len(bars) - 1, 60)
    normalized_20 = _normalize_return(values.get(20), volatility_20, technical.epsilon)
    normalized_60 = _normalize_return(values.get(60), volatility_60, technical.epsilon)
    trend_strength = _trend_strength(normalized_20, normalized_60, technical.trend_strength_cap)

    mean_60 = _close_mean(bars, 60)
    high_60 = _high_maximum(bars, 60)
    latest_close = bars[-1].close

    return TechnicalState(
        return_1=values.get(1),
        return_5=values.get(5),
        return_20=values.get(20),
        return_60=values.get(60),
        atr20_over_price=atr20_over_price,
        realized_volatility_20=volatility_20,
        trend_20=normalized_20,
        trend_60=normalized_60,
        trend_strength=trend_strength,
        distance_from_60_session_mean=(latest_close / mean_60 - 1.0) if mean_60 else None,
        drawdown_from_60_session_high=(latest_close / high_60 - 1.0) if high_60 else None,
    )


def evaluate_t1_trend(technical_state: TechnicalState) -> TechnicalOpinion:
    """Turn frozen normalized T1 state into an observation-only opinion."""

    normalized_20 = technical_state.trend_20
    normalized_60 = technical_state.trend_60
    if normalized_20 is None or normalized_60 is None:
        return TechnicalOpinion(
            direction=Direction.FLAT,
            strength=None,
            normalized_20=normalized_20,
            normalized_60=normalized_60,
            reason_codes=("T1_INSUFFICIENT_COMPLETED_HISTORY",),
        )
    if normalized_20 > 0.0 and normalized_60 > 0.0:
        return TechnicalOpinion(
            direction=Direction.LONG,
            strength=technical_state.trend_strength,
            normalized_20=normalized_20,
            normalized_60=normalized_60,
            reason_codes=("T1_ALIGNED_UPTREND",),
        )
    if normalized_20 < 0.0 and normalized_60 < 0.0:
        return TechnicalOpinion(
            direction=Direction.SHORT,
            strength=technical_state.trend_strength,
            normalized_20=normalized_20,
            normalized_60=normalized_60,
            reason_codes=("T1_ALIGNED_DOWNTREND",),
        )
    return TechnicalOpinion(
        direction=Direction.FLAT,
        strength=technical_state.trend_strength,
        normalized_20=normalized_20,
        normalized_60=normalized_60,
        reason_codes=("T1_SIGN_DISAGREEMENT_OR_ZERO",),
    )


def evaluate_m1_reversion(
    source: MarketSnapshot | Sequence[DailyBar],
    config: ShadowTournamentConfig,
    *,
    decision_timestamp_utc: datetime | None = None,
) -> ReversionOpinion:
    """Calculate M1's causal stretch/reversion observation.

    The current normalized five-session return is never included in its own
    percentile reference set.  Each reference value is calculated from a bar
    prefix ending before the current completed bar, so no future observation
    can influence the reported percentile.
    """

    bars, _ = _causal_bars(source, decision_timestamp_utc)
    technical = _technical_rules(config)
    reversion = _reversion_rules(config)
    current_index = len(bars) - 1
    current_value = _normalized_return_ending_at(
        bars,
        current_index,
        reversion.return_window,
        technical.volatility_window,
        technical.epsilon,
    )
    if current_value is None:
        return ReversionOpinion(
            direction=Direction.FLAT,
            percentile=None,
            normalized_return_5=None,
            reason_codes=("M1_INSUFFICIENT_COMPLETED_HISTORY",),
        )

    prior_values = [
        value
        for index in range(technical.volatility_window, current_index)
        if (
            value := _normalized_return_ending_at(
                bars,
                index,
                reversion.return_window,
                technical.volatility_window,
                technical.epsilon,
            )
        )
        is not None
    ]
    prior_values = prior_values[-reversion.percentile_lookback :]
    if not prior_values:
        return ReversionOpinion(
            direction=Direction.FLAT,
            percentile=None,
            normalized_return_5=current_value,
            reason_codes=("M1_INSUFFICIENT_CAUSAL_CALIBRATION",),
        )

    percentile = _midrank_percentile(current_value, prior_values)
    history_reason = (
        "M1_PERCENTILE_LOOKBACK_COMPLETE"
        if len(prior_values) == reversion.percentile_lookback
        else "M1_PERCENTILE_SHORT_HISTORY"
    )
    if percentile <= reversion.lower_percentile:
        return ReversionOpinion(
            direction=Direction.LONG,
            percentile=percentile,
            normalized_return_5=current_value,
            reason_codes=(history_reason, "M1_STRONG_DOWNSIDE_STRETCH"),
        )
    if percentile >= reversion.upper_percentile:
        return ReversionOpinion(
            direction=Direction.SHORT,
            percentile=percentile,
            normalized_return_5=current_value,
            reason_codes=(history_reason, "M1_STRONG_UPSIDE_STRETCH"),
        )
    return ReversionOpinion(
        direction=Direction.FLAT,
        percentile=percentile,
        normalized_return_5=current_value,
        reason_codes=(history_reason, "M1_NO_STRONG_STRETCH"),
    )


def assess_q1_quality(
    source: MarketSnapshot | Sequence[DailyBar],
    config: ShadowTournamentConfig,
    *,
    decision_timestamp_utc: datetime | None = None,
    provider_healthy: bool | None = None,
    stream_healthy: bool | None = None,
    session_complete: bool | None = None,
    feature_available: bool | None = None,
) -> QualityAssessment:
    """Assess explicit market-data health without assuming missing facts are good."""

    try:
        bars, decision_timestamp = _causal_bars(source, decision_timestamp_utc)
    except ShadowEngineError as error:
        return QualityAssessment(QualityState.BLOCKED, None, (str(error),))

    quality = _quality_rules(config)
    if len(bars) < quality.minimum_completed_observations:
        return QualityAssessment(
            QualityState.BLOCKED,
            None,
            ("Q1_INSUFFICIENT_COMPLETED_OBSERVATIONS",),
        )

    data_age_seconds = int((decision_timestamp - bars[-1].completed_at).total_seconds())
    failed: list[str] = []
    unknown: list[str] = []
    for name, value in (
        ("PROVIDER", provider_healthy),
        ("STREAM", stream_healthy),
        ("SESSION", session_complete),
        ("FEATURE", feature_available),
    ):
        if value is False:
            failed.append(f"Q1_{name}_UNHEALTHY")
        elif value is not True:
            unknown.append(f"Q1_{name}_HEALTH_UNKNOWN")
    if data_age_seconds > quality.maximum_price_age_seconds:
        failed.append("Q1_PRICE_STALE")
    if failed:
        return QualityAssessment(QualityState.BLOCKED, data_age_seconds, tuple(failed))
    if unknown:
        return QualityAssessment(QualityState.UNKNOWN, data_age_seconds, tuple(unknown))
    if data_age_seconds > quality.maximum_price_age_seconds // 2:
        return QualityAssessment(
            QualityState.WARNING,
            data_age_seconds,
            ("Q1_PRICE_AGE_WARNING",),
        )
    return QualityAssessment(QualityState.NORMAL, data_age_seconds, ("Q1_ALL_CHECKS_PASSED",))


def assess_c1_cost(
    inputs: CostInputs | None,
    technical_state: TechnicalState,
    config: ShadowTournamentConfig,
) -> CostContext:
    """Classify explicit broker-cost evidence without inventing a global model."""

    if inputs is None:
        return CostContext(
            state=CostState.COST_UNKNOWN,
            spread=None,
            spread_to_atr=None,
            minimum_stop_distance=None,
            product_type=None,
            funding_metadata=None,
            reason_codes=("C1_COST_INPUTS_UNAVAILABLE",),
        )

    product_type = _optional_text(inputs.product_type)
    funding_metadata = _optional_text(inputs.funding_metadata)
    minimum_stop_distance = (
        float(inputs.minimum_stop_distance)
        if _is_finite_nonnegative(inputs.minimum_stop_distance)
        else None
    )
    if inputs.minimum_stop_distance is not None and minimum_stop_distance is None:
        return CostContext(
            state=CostState.COST_UNKNOWN,
            spread=_finite_nonnegative_or_none(inputs.spread),
            spread_to_atr=None,
            minimum_stop_distance=None,
            product_type=product_type,
            funding_metadata=funding_metadata,
            reason_codes=("C1_MINIMUM_STOP_METADATA_INVALID",),
        )

    spread = _finite_nonnegative_or_none(inputs.spread)
    reference_price = _finite_positive_or_none(inputs.reference_price)
    atr20_over_price = _finite_positive_or_none(technical_state.atr20_over_price)
    if spread is None:
        return CostContext(
            CostState.COST_UNKNOWN,
            None,
            None,
            minimum_stop_distance,
            product_type,
            funding_metadata,
            ("C1_SPREAD_UNKNOWN",),
        )
    if reference_price is None:
        return CostContext(
            CostState.COST_UNKNOWN,
            spread,
            None,
            minimum_stop_distance,
            product_type,
            funding_metadata,
            ("C1_REFERENCE_PRICE_UNKNOWN",),
        )
    if atr20_over_price is None:
        return CostContext(
            CostState.COST_UNKNOWN,
            spread,
            None,
            minimum_stop_distance,
            product_type,
            funding_metadata,
            ("C1_ATR20_UNKNOWN",),
        )

    spread_to_atr = (spread / reference_price) / atr20_over_price
    if not math.isfinite(spread_to_atr):
        return CostContext(
            CostState.COST_UNKNOWN,
            spread,
            None,
            minimum_stop_distance,
            product_type,
            funding_metadata,
            ("C1_SPREAD_NORMALIZATION_INVALID",),
        )
    cost = _cost_rules(config)
    if spread_to_atr >= cost.high_spread_to_atr_fraction:
        return CostContext(
            CostState.COST_HIGH,
            spread,
            spread_to_atr,
            minimum_stop_distance,
            product_type,
            funding_metadata,
            ("C1_SPREAD_TO_ATR_HIGH",),
        )
    return CostContext(
        CostState.COST_OK,
        spread,
        spread_to_atr,
        minimum_stop_distance,
        product_type,
        funding_metadata,
        ("C1_SPREAD_TO_ATR_OK",),
    )


def evaluate_x1_context(
    inputs: Sequence[CrossAssetInput] | None,
    config: ShadowTournamentConfig,
    *,
    decision_timestamp_utc: datetime,
) -> CrossAssetOpinion:
    """Aggregate explicitly supplied, direction-aligned X1 context facts.

    The engine has no provider fallback and never derives an economic driver
    map on its own.  Missing, invalid, or not-yet-available inputs therefore
    produce ``UNKNOWN`` rather than a partial inferred context.
    """

    try:
        decision_timestamp = require_utc(decision_timestamp_utc)
    except ValueError:
        return CrossAssetOpinion(
            ContextState.UNKNOWN,
            None,
            ("X1_DECISION_TIMESTAMP_INVALID",),
            {"input_count": 0},
        )
    if not inputs:
        return CrossAssetOpinion(
            ContextState.UNKNOWN,
            None,
            ("X1_CONTEXT_INPUTS_UNAVAILABLE",),
            {"input_count": 0},
        )

    fields: dict[str, object] = {"input_count": len(inputs), "inputs": {}}
    rendered_inputs = fields["inputs"]
    assert isinstance(rendered_inputs, dict)
    scores: list[float] = []
    seen_names: set[str] = set()
    invalid_reasons: list[str] = []
    for item in inputs:
        if not isinstance(item, CrossAssetInput):
            invalid_reasons.append("X1_INPUT_CONTRACT_INVALID")
            continue
        name = item.name.strip() if isinstance(item.name, str) else ""
        if not name:
            invalid_reasons.append("X1_INPUT_NAME_INVALID")
            continue
        if name in seen_names:
            invalid_reasons.append("X1_INPUT_NAME_DUPLICATE")
            continue
        seen_names.add(name)
        rendered_inputs[name] = {
            "alignment_score": item.alignment_score,
            "available_at_utc": _render_timestamp(item.available_at_utc),
            "source": _optional_text(item.source),
        }
        score = _finite_score_or_none(item.alignment_score)
        if score is None:
            invalid_reasons.append("X1_ALIGNMENT_SCORE_UNKNOWN")
            continue
        try:
            available_at = require_utc(item.available_at_utc) if item.available_at_utc else None
        except ValueError:
            available_at = None
        if available_at is None:
            invalid_reasons.append("X1_INPUT_AVAILABILITY_UNKNOWN")
            continue
        if available_at > decision_timestamp:
            invalid_reasons.append("X1_INPUT_NOT_CAUSALLY_AVAILABLE")
            continue
        scores.append(score)
    if invalid_reasons:
        return CrossAssetOpinion(
            ContextState.UNKNOWN,
            None,
            tuple(_ordered_unique(invalid_reasons)),
            fields,
        )

    score = fmean(scores)
    context = _context_rules(config)
    if score <= -context.material_opposition_score:
        return CrossAssetOpinion(
            ContextState.OPPOSES,
            score,
            ("X1_MATERIAL_OPPOSITION",),
            fields,
        )
    if score >= context.material_opposition_score:
        return CrossAssetOpinion(
            ContextState.SUPPORTIVE,
            score,
            ("X1_MATERIAL_SUPPORT",),
            fields,
        )
    return CrossAssetOpinion(ContextState.NEUTRAL, score, ("X1_NEUTRAL_CONTEXT",), fields)


def build_f1_context(
    inputs: FundamentalInputs | None,
    *,
    decision_timestamp_utc: datetime,
) -> FundamentalContext:
    """Return only causal F1 context; this function never emits a direction."""

    if inputs is None:
        return FundamentalContext(
            FundamentalState.UNKNOWN,
            None,
            None,
            None,
            None,
            None,
            ("F1_CONTEXT_UNAVAILABLE", "FRED_CONTEXT_UNAVAILABLE"),
        )
    policy_state = _optional_text(inputs.policy_state)
    policy_trend = _optional_text(inputs.policy_trend)
    data_quality = _optional_text(inputs.data_quality)
    try:
        decision_timestamp = require_utc(decision_timestamp_utc)
        available_at = require_utc(inputs.available_at_utc) if inputs.available_at_utc else None
    except ValueError:
        decision_timestamp = None
        available_at = None
    if decision_timestamp is None or available_at is None:
        return FundamentalContext(
            FundamentalState.UNKNOWN,
            policy_state,
            policy_trend,
            data_quality,
            None,
            inputs.event_risk if isinstance(inputs.event_risk, bool) else None,
            ("F1_CONTEXT_AVAILABILITY_UNKNOWN",),
        )
    if available_at > decision_timestamp:
        return FundamentalContext(
            FundamentalState.UNKNOWN,
            policy_state,
            policy_trend,
            data_quality,
            None,
            inputs.event_risk if isinstance(inputs.event_risk, bool) else None,
            ("F1_CONTEXT_NOT_CAUSALLY_AVAILABLE",),
        )
    staleness_seconds = int((decision_timestamp - available_at).total_seconds())
    if inputs.event_risk is True:
        return FundamentalContext(
            FundamentalState.EVENT_RISK,
            policy_state,
            policy_trend,
            data_quality,
            staleness_seconds,
            True,
            ("F1_EVENT_RISK",),
        )
    if inputs.event_risk is not False:
        return FundamentalContext(
            FundamentalState.UNKNOWN,
            policy_state,
            policy_trend,
            data_quality,
            staleness_seconds,
            None,
            ("F1_EVENT_RISK_UNKNOWN",),
        )
    if data_quality is None or data_quality.upper() in {"UNKNOWN", "MISSING", "STALE"}:
        return FundamentalContext(
            FundamentalState.UNKNOWN,
            policy_state,
            policy_trend,
            data_quality,
            staleness_seconds,
            False,
            ("F1_DATA_QUALITY_UNKNOWN",),
        )
    return FundamentalContext(
        FundamentalState.NORMAL,
        policy_state,
        policy_trend,
        data_quality,
        staleness_seconds,
        False,
        ("F1_CONTEXT_AVAILABLE",),
    )


@dataclass(frozen=True)
class _TechnicalRules:
    return_windows: tuple[int, ...]
    volatility_window: int
    atr_window: int
    epsilon: float
    trend_strength_cap: float


@dataclass(frozen=True)
class _ReversionRules:
    return_window: int
    percentile_lookback: int
    lower_percentile: float
    upper_percentile: float


@dataclass(frozen=True)
class _QualityRules:
    maximum_price_age_seconds: int
    minimum_completed_observations: int


@dataclass(frozen=True)
class _CostRules:
    high_spread_to_atr_fraction: float


@dataclass(frozen=True)
class _ContextRules:
    material_opposition_score: float


def _causal_bars(
    source: MarketSnapshot | Sequence[DailyBar],
    decision_timestamp_utc: datetime | None,
) -> tuple[tuple[DailyBar, ...], datetime]:
    if isinstance(source, MarketSnapshot):
        decision_timestamp = source.decision_timestamp_utc
        if decision_timestamp_utc is not None:
            try:
                requested_timestamp = require_utc(decision_timestamp_utc)
            except ValueError as error:
                raise ShadowEngineError("SHADOW01_DECISION_TIMESTAMP_INVALID") from error
            if requested_timestamp != decision_timestamp:
                raise ShadowEngineError("SHADOW01_DECISION_TIMESTAMP_CONFLICT")
        bars = source.completed_bars
    else:
        if decision_timestamp_utc is None:
            raise ShadowEngineError("SHADOW01_DECISION_TIMESTAMP_REQUIRED")
        try:
            decision_timestamp = require_utc(decision_timestamp_utc)
        except ValueError as error:
            raise ShadowEngineError("SHADOW01_DECISION_TIMESTAMP_INVALID") from error
        try:
            bars = tuple(source)
        except TypeError as error:
            raise ShadowEngineError("SHADOW01_COMPLETED_BARS_INVALID") from error
    if not bars:
        raise ShadowEngineError("SHADOW01_COMPLETED_BARS_MISSING")

    previous: datetime | None = None
    for bar in bars:
        if not isinstance(bar, DailyBar):
            raise ShadowEngineError("SHADOW01_COMPLETED_BAR_CONTRACT_INVALID")
        if previous is not None and bar.completed_at <= previous:
            raise ShadowEngineError("SHADOW01_COMPLETED_BARS_NOT_STRICTLY_ORDERED")
        if bar.completed_at >= decision_timestamp:
            raise ShadowEngineError("SHADOW01_INCOMPLETE_OR_FUTURE_BAR")
        previous = bar.completed_at
    return bars, decision_timestamp


def _return_ending_at(bars: Sequence[DailyBar], end_index: int, window: int) -> float | None:
    if end_index < window:
        return None
    return bars[end_index].close / bars[end_index - window].close - 1.0


def _realized_volatility_ending_at(
    bars: Sequence[DailyBar], end_index: int, window: int
) -> float | None:
    if end_index < window:
        return None
    returns = [
        bars[index].close / bars[index - 1].close - 1.0
        for index in range(end_index - window + 1, end_index + 1)
    ]
    mean_return = fmean(returns)
    return math.sqrt(fmean((value - mean_return) ** 2 for value in returns))


def _atr_ending_at(bars: Sequence[DailyBar], end_index: int, window: int) -> float | None:
    if end_index < window:
        return None
    true_ranges = [
        max(
            bars[index].high - bars[index].low,
            abs(bars[index].high - bars[index - 1].close),
            abs(bars[index].low - bars[index - 1].close),
        )
        for index in range(end_index - window + 1, end_index + 1)
    ]
    return fmean(true_ranges)


def _normalized_return_ending_at(
    bars: Sequence[DailyBar],
    end_index: int,
    return_window: int,
    volatility_window: int,
    epsilon: float,
) -> float | None:
    return _normalize_return(
        _return_ending_at(bars, end_index, return_window),
        _realized_volatility_ending_at(bars, end_index, volatility_window),
        epsilon,
    )


def _normalize_return(
    return_value: float | None, volatility: float | None, epsilon: float
) -> float | None:
    if return_value is None or volatility is None:
        return None
    return return_value / max(volatility, epsilon)


def _trend_strength(
    normalized_20: float | None, normalized_60: float | None, cap: float
) -> float | None:
    if normalized_20 is None or normalized_60 is None:
        return None
    return min(cap, (abs(normalized_20) + abs(normalized_60)) / 2.0)


def _close_mean(bars: Sequence[DailyBar], window: int) -> float | None:
    return fmean(bar.close for bar in bars[-window:]) if len(bars) >= window else None


def _high_maximum(bars: Sequence[DailyBar], window: int) -> float | None:
    return max(bar.high for bar in bars[-window:]) if len(bars) >= window else None


def _midrank_percentile(current_value: float, prior_values: Sequence[float]) -> float:
    lower = sum(value < current_value for value in prior_values)
    equal = sum(value == current_value for value in prior_values)
    return (lower + 0.5 * equal) / len(prior_values)


def _technical_rules(config: ShadowTournamentConfig) -> _TechnicalRules:
    technical = _payload_mapping(config, "technical")
    windows = technical.get("return_windows")
    if not isinstance(windows, list) or tuple(windows) != (1, 5, 20, 60):
        raise ShadowEngineError("SHADOW01_TECHNICAL_RETURN_WINDOWS_INVALID")
    return _TechnicalRules(
        return_windows=(1, 5, 20, 60),
        volatility_window=_positive_int(technical.get("volatility_window"), "VOLATILITY_WINDOW"),
        atr_window=_positive_int(technical.get("atr_window"), "ATR_WINDOW"),
        epsilon=_positive_float(technical.get("epsilon"), "EPSILON"),
        trend_strength_cap=_positive_float(
            technical.get("trend_strength_cap"), "TREND_STRENGTH_CAP"
        ),
    )


def _reversion_rules(config: ShadowTournamentConfig) -> _ReversionRules:
    reversion = _payload_mapping(config, "reversion")
    lower = _finite_float(reversion.get("lower_percentile"))
    upper = _finite_float(reversion.get("upper_percentile"))
    if lower is None or upper is None or not 0.0 <= lower < upper <= 1.0:
        raise ShadowEngineError("SHADOW01_REVERSION_PERCENTILES_INVALID")
    return _ReversionRules(
        return_window=_positive_int(reversion.get("return_window"), "REVERSION_RETURN_WINDOW"),
        percentile_lookback=_positive_int(
            reversion.get("percentile_lookback"), "REVERSION_PERCENTILE_LOOKBACK"
        ),
        lower_percentile=lower,
        upper_percentile=upper,
    )


def _quality_rules(config: ShadowTournamentConfig) -> _QualityRules:
    quality = _payload_mapping(config, "quality")
    return _QualityRules(
        maximum_price_age_seconds=_positive_int(
            quality.get("maximum_price_age_seconds"), "MAXIMUM_PRICE_AGE_SECONDS"
        ),
        minimum_completed_observations=_positive_int(
            quality.get("minimum_completed_observations"), "MINIMUM_COMPLETED_OBSERVATIONS"
        ),
    )


def _cost_rules(config: ShadowTournamentConfig) -> _CostRules:
    cost = _payload_mapping(config, "cost")
    return _CostRules(
        high_spread_to_atr_fraction=_positive_float(
            cost.get("high_spread_to_atr_fraction"), "HIGH_SPREAD_TO_ATR_FRACTION"
        )
    )


def _context_rules(config: ShadowTournamentConfig) -> _ContextRules:
    context = _payload_mapping(config, "context")
    threshold = _positive_float(
        context.get("material_opposition_score"), "MATERIAL_OPPOSITION_SCORE"
    )
    if threshold > 1.0:
        raise ShadowEngineError("SHADOW01_MATERIAL_OPPOSITION_SCORE_INVALID")
    return _ContextRules(threshold)


def _payload_mapping(config: ShadowTournamentConfig, key: str) -> dict[str, object]:
    value = config.payload.get(key)
    if not isinstance(value, dict):
        raise ShadowEngineError(f"SHADOW01_{key.upper()}_CONFIG_INVALID")
    return value


def _positive_int(value: object, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ShadowEngineError(f"SHADOW01_{name}_INVALID")
    return value


def _positive_float(value: object, name: str) -> float:
    result = _finite_float(value)
    if result is None or result <= 0.0:
        raise ShadowEngineError(f"SHADOW01_{name}_INVALID")
    return result


def _finite_float(value: object) -> float | None:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    result = float(value)
    return result if math.isfinite(result) else None


def _finite_positive_or_none(value: object) -> float | None:
    result = _finite_float(value)
    return result if result is not None and result > 0.0 else None


def _finite_nonnegative_or_none(value: object) -> float | None:
    result = _finite_float(value)
    return result if result is not None and result >= 0.0 else None


def _is_finite_nonnegative(value: object) -> bool:
    return _finite_nonnegative_or_none(value) is not None


def _finite_score_or_none(value: object) -> float | None:
    result = _finite_float(value)
    return result if result is not None and -1.0 <= result <= 1.0 else None


def _optional_text(value: object) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _render_timestamp(value: object) -> str | None:
    try:
        return require_utc(value).isoformat() if isinstance(value, datetime) else None
    except ValueError:
        return None


def _ordered_unique(values: Sequence[str]) -> list[str]:
    return list(dict.fromkeys(values))
