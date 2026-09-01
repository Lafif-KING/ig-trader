"""Frozen, non-executing policy contestants for the Shadow Tournament."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from uuid import NAMESPACE_URL, uuid5

from src.ig_trader.shadow01.config import EXPECTED_SYMBOLS, ShadowTournamentConfig
from src.ig_trader.shadow01.models import (
    ContextState,
    CostContext,
    CostState,
    CrossAssetOpinion,
    Direction,
    FundamentalContext,
    FundamentalState,
    PolicyId,
    QualityAssessment,
    QualityState,
    ReversionOpinion,
    ShadowDecision,
    TechnicalOpinion,
    expected_factor_tags,
    require_utc,
)

_DIRECTIONAL_DIRECTIONS = frozenset((Direction.LONG, Direction.SHORT))
_POLICY_ENGINES = {
    PolicyId.P0_TECHNICAL_TREND_ONLY: "T1",
    PolicyId.P1_TECHNICAL_REVERSION_ONLY: "M1",
    PolicyId.P2_TREND_PLUS_CROSS_ASSET: "T1",
    PolicyId.P3_CONSERVATIVE_CONTEXT: "T1",
}


@dataclass(frozen=True)
class PolicyRecommendation:
    """A policy opinion before the append-only ShadowDecision is materialized."""

    policy_id: PolicyId
    direction: Direction
    technical_engine: str
    technical_score: float | None
    reason_codes: tuple[str, ...]

    def __post_init__(self) -> None:
        """Reject malformed candidate opinions before they reach the audit log."""

        if not isinstance(self.policy_id, PolicyId) or not isinstance(self.direction, Direction):
            raise ValueError("SHADOW01_POLICY_RECOMMENDATION_STATE_INVALID")
        if self.technical_engine != _POLICY_ENGINES[self.policy_id]:
            raise ValueError("SHADOW01_POLICY_ENGINE_CONTRACT_INVALID")
        if self.technical_score is not None and (
            isinstance(self.technical_score, bool)
            or not isinstance(self.technical_score, (int, float))
            or not math.isfinite(self.technical_score)
        ):
            raise ValueError("SHADOW01_POLICY_SCORE_INVALID")
        if self.direction in _DIRECTIONAL_DIRECTIONS and self.technical_score is None:
            raise ValueError("SHADOW01_DIRECTIONAL_POLICY_SCORE_REQUIRED")
        if (
            not isinstance(self.reason_codes, tuple)
            or not self.reason_codes
            or not all(isinstance(code, str) and code.strip() for code in self.reason_codes)
        ):
            raise ValueError("SHADOW01_POLICY_REASON_CODES_INVALID")
        normalized_codes = tuple(dict.fromkeys(self.reason_codes))
        object.__setattr__(self, "reason_codes", normalized_codes)


def evaluate_policies(
    config: ShadowTournamentConfig,
    *,
    trend: TechnicalOpinion,
    reversion: ReversionOpinion,
    cross_asset: CrossAssetOpinion,
    fundamental: FundamentalContext,
    quality: QualityAssessment,
    cost: CostContext,
) -> tuple[PolicyRecommendation, ...]:
    """Let all four contestants see the same causal timestamp and facts."""

    _validate_policy_inputs(
        config,
        trend=trend,
        reversion=reversion,
        cross_asset=cross_asset,
        fundamental=fundamental,
        quality=quality,
        cost=cost,
    )
    if quality.state is QualityState.BLOCKED:
        return _quality_blocked_recommendations(quality)
    p0_direction = _trend_direction(config, trend)
    p1_direction = _reversion_direction(reversion)
    return (
        PolicyRecommendation(
            PolicyId.P0_TECHNICAL_TREND_ONLY,
            p0_direction,
            "T1",
            trend.strength,
            _codes("P0_T1_ONLY", trend.reason_codes),
        ),
        PolicyRecommendation(
            PolicyId.P1_TECHNICAL_REVERSION_ONLY,
            p1_direction,
            "M1",
            reversion.normalized_return_5,
            _codes("P1_M1_ONLY", reversion.reason_codes),
        ),
        _p2(p0_direction, trend, cross_asset),
        _p3(p0_direction, trend, cross_asset, fundamental, quality, cost),
    )


def materialize_decisions(
    config: ShadowTournamentConfig,
    *,
    decision_timestamp_utc: datetime,
    instrument: str,
    epic: str,
    input_data_fingerprint: str,
    recommendations: tuple[PolicyRecommendation, ...],
    cross_asset: CrossAssetOpinion,
    fundamental: FundamentalContext,
    quality: QualityAssessment,
    cost: CostContext,
    created_at: datetime | None = None,
) -> tuple[ShadowDecision, ...]:
    """Build stable, broker-neutral decisions with no deal/order identifiers."""

    _validate_policy_inputs(
        config,
        cross_asset=cross_asset,
        fundamental=fundamental,
        quality=quality,
        cost=cost,
    )
    _require_frozen_scope(config, instrument, epic, input_data_fingerprint)
    timestamp = require_utc(decision_timestamp_utc)
    # A deterministic default is important for reproducible audit records. Runtime
    # callers that need a different recording time must provide it explicitly.
    created = require_utc(created_at) if created_at is not None else timestamp
    if len(recommendations) != len(PolicyId):
        raise ValueError("All four Shadow policies must receive the same snapshot")
    if not all(isinstance(item, PolicyRecommendation) for item in recommendations):
        raise ValueError("SHADOW01_POLICY_RECOMMENDATION_INVALID")
    for item in recommendations:
        item.__post_init__()
    by_policy = {item.policy_id: item for item in recommendations}
    if len(by_policy) != len(PolicyId) or set(by_policy) != set(PolicyId):
        raise ValueError("Shadow policy contestants are incomplete")
    canonical_recommendations = tuple(
        _enforce_blocking_invariants(
            by_policy[policy_id],
            cross_asset=cross_asset,
            fundamental=fundamental,
            quality=quality,
            cost=cost,
        )
        for policy_id in PolicyId
    )
    return tuple(
        ShadowDecision(
            decision_id=_decision_id(config, timestamp, instrument, item.policy_id),
            tournament_version=config.version,
            config_fingerprint=config.fingerprint,
            decision_timestamp_utc=timestamp,
            instrument=instrument,
            epic=epic,
            policy_id=item.policy_id,
            direction=item.direction,
            technical_engine=item.technical_engine,
            technical_score=item.technical_score,
            cross_asset_state=cross_asset.state,
            fundamental_context=fundamental.state,
            quality_state=quality.state,
            cost_state=cost.state,
            factor_tags=factor_tags(instrument, item.direction),
            reason_codes=item.reason_codes,
            input_data_fingerprint=input_data_fingerprint,
            created_at=created,
        )
        for item in canonical_recommendations
    )


def factor_tags(instrument: str, direction: Direction) -> tuple[str, ...]:
    """Expose correlated directional factors instead of counting opinions as independent."""

    return expected_factor_tags(instrument, direction)


def _p2(
    direction: Direction, trend: TechnicalOpinion, cross_asset: CrossAssetOpinion
) -> PolicyRecommendation:
    if direction not in _DIRECTIONAL_DIRECTIONS:
        return PolicyRecommendation(
            PolicyId.P2_TREND_PLUS_CROSS_ASSET,
            direction,
            "T1",
            trend.strength,
            _codes("P2_T1_NO_DIRECTION", trend.reason_codes),
        )
    if cross_asset.state not in {ContextState.SUPPORTIVE, ContextState.NEUTRAL}:
        return PolicyRecommendation(
            PolicyId.P2_TREND_PLUS_CROSS_ASSET,
            Direction.BLOCK,
            "T1",
            trend.strength,
            _codes(
                "P2_X1_NOT_SUPPORTIVE_OR_NEUTRAL",
                trend.reason_codes,
                cross_asset.reason_codes,
            ),
        )
    return PolicyRecommendation(
        PolicyId.P2_TREND_PLUS_CROSS_ASSET,
        direction,
        "T1",
        trend.strength,
        _codes("P2_X1_ALLOW", trend.reason_codes, cross_asset.reason_codes),
    )


def _p3(
    direction: Direction,
    trend: TechnicalOpinion,
    cross_asset: CrossAssetOpinion,
    fundamental: FundamentalContext,
    quality: QualityAssessment,
    cost: CostContext,
) -> PolicyRecommendation:
    blockers: list[str] = []
    if direction not in _DIRECTIONAL_DIRECTIONS:
        return PolicyRecommendation(
            PolicyId.P3_CONSERVATIVE_CONTEXT,
            direction,
            "T1",
            trend.strength,
            _codes("P3_T1_NO_DIRECTION", trend.reason_codes),
        )
    if quality.state is QualityState.BLOCKED:
        blockers.append("P3_Q1_BLOCKED")
    if cost.state is CostState.COST_HIGH:
        blockers.append("P3_C1_HIGH")
    if cross_asset.state is ContextState.OPPOSES:
        blockers.append("P3_X1_OPPOSES")
    if fundamental.state is FundamentalState.EVENT_RISK:
        blockers.append("P3_F1_EVENT_RISK")
    if blockers:
        return PolicyRecommendation(
            PolicyId.P3_CONSERVATIVE_CONTEXT,
            Direction.BLOCK,
            "T1",
            trend.strength,
            _codes(
                *blockers,
                trend.reason_codes,
                cross_asset.reason_codes,
                fundamental.reason_codes,
            ),
        )
    return PolicyRecommendation(
        PolicyId.P3_CONSERVATIVE_CONTEXT,
        direction,
        "T1",
        trend.strength,
        _codes(
            "P3_CONTEXT_ALLOW",
            trend.reason_codes,
            cross_asset.reason_codes,
            fundamental.reason_codes,
            cost.reason_codes,
        ),
    )


def _trend_direction(config: ShadowTournamentConfig, trend: TechnicalOpinion) -> Direction:
    minimum = config.payload["policies"]
    assert isinstance(minimum, dict)
    threshold = float(minimum["trend_minimum_strength"])
    if trend.direction not in _DIRECTIONAL_DIRECTIONS:
        return trend.direction
    if trend.strength is None or trend.strength < threshold:
        return Direction.FLAT
    return trend.direction


def _reversion_direction(reversion: ReversionOpinion) -> Direction:
    if reversion.direction not in _DIRECTIONAL_DIRECTIONS:
        return reversion.direction
    if reversion.normalized_return_5 is None:
        return Direction.FLAT
    return reversion.direction


def _decision_id(
    config: ShadowTournamentConfig, timestamp: datetime, instrument: str, policy: PolicyId
) -> str:
    identity = (
        f"shadow01:{config.version}:{config.fingerprint}:{timestamp.isoformat()}:"
        f"{instrument}:{policy.value}"
    )
    return uuid5(NAMESPACE_URL, identity).hex


def _codes(*values: object) -> tuple[str, ...]:
    result: list[str] = []
    for value in values:
        if isinstance(value, str):
            result.append(value)
        elif isinstance(value, tuple):
            result.extend(item for item in value if isinstance(item, str))
    return tuple(dict.fromkeys(result))


def _quality_blocked_recommendations(
    quality: QualityAssessment,
) -> tuple[PolicyRecommendation, ...]:
    """Apply Q1's all-policy circuit breaker in one canonical policy order."""

    return tuple(
        PolicyRecommendation(
            policy_id=policy_id,
            direction=Direction.BLOCK,
            technical_engine=_POLICY_ENGINES[policy_id],
            technical_score=None,
            reason_codes=_codes("Q1_BLOCKED", quality.reason_codes),
        )
        for policy_id in PolicyId
    )


def _enforce_blocking_invariants(
    recommendation: PolicyRecommendation,
    *,
    cross_asset: CrossAssetOpinion,
    fundamental: FundamentalContext,
    quality: QualityAssessment,
    cost: CostContext,
) -> PolicyRecommendation:
    """Fail closed if an injected recommendation tries to bypass its policy gate.

    ``evaluate_policies`` is the normal source of recommendations, but the
    materialization boundary is deliberately defensive as well.  This prevents
    a caller from directly constructing a directional P2/P3 record while its
    own recorded context says that the policy must block.
    """

    if recommendation.direction not in _DIRECTIONAL_DIRECTIONS:
        return recommendation
    blockers = _blocker_codes(
        recommendation.policy_id,
        cross_asset=cross_asset,
        fundamental=fundamental,
        quality=quality,
        cost=cost,
    )
    if not blockers:
        return recommendation
    return PolicyRecommendation(
        policy_id=recommendation.policy_id,
        direction=Direction.BLOCK,
        technical_engine=recommendation.technical_engine,
        technical_score=recommendation.technical_score,
        reason_codes=_codes(
            *blockers,
            recommendation.reason_codes,
            quality.reason_codes,
            cost.reason_codes,
            cross_asset.reason_codes,
            fundamental.reason_codes,
        ),
    )


def _blocker_codes(
    policy_id: PolicyId,
    *,
    cross_asset: CrossAssetOpinion,
    fundamental: FundamentalContext,
    quality: QualityAssessment,
    cost: CostContext,
) -> tuple[str, ...]:
    """Return only gates that the frozen policy is documented to consume."""

    blockers: list[str] = []
    if quality.state is QualityState.BLOCKED:
        blockers.append("Q1_BLOCKED")
    if policy_id is PolicyId.P2_TREND_PLUS_CROSS_ASSET and cross_asset.state not in {
        ContextState.SUPPORTIVE,
        ContextState.NEUTRAL,
    }:
        blockers.append("P2_X1_NOT_SUPPORTIVE_OR_NEUTRAL")
    if policy_id is PolicyId.P3_CONSERVATIVE_CONTEXT:
        if cost.state is CostState.COST_HIGH:
            blockers.append("P3_C1_HIGH")
        if cross_asset.state is ContextState.OPPOSES:
            blockers.append("P3_X1_OPPOSES")
        if fundamental.state is FundamentalState.EVENT_RISK:
            blockers.append("P3_F1_EVENT_RISK")
    return tuple(blockers)


def _validate_policy_inputs(
    config: ShadowTournamentConfig,
    *,
    cross_asset: CrossAssetOpinion,
    fundamental: FundamentalContext,
    quality: QualityAssessment,
    cost: CostContext,
    trend: TechnicalOpinion | None = None,
    reversion: ReversionOpinion | None = None,
) -> None:
    """Keep malformed state values from falling through state comparisons."""

    if not isinstance(config, ShadowTournamentConfig):
        raise ValueError("SHADOW01_POLICY_CONFIG_INVALID")
    payload = config.payload
    if not config.fingerprint_is_valid or payload.get("execution_authority") != "OFF":
        raise ValueError("SHADOW01_POLICY_CONFIG_UNSAFE")
    if trend is not None:
        _validate_opinion(
            trend,
            expected_type=TechnicalOpinion,
            direction_error="SHADOW01_T1_POLICY_STATE_INVALID",
            score=trend.strength,
        )
    if reversion is not None:
        _validate_opinion(
            reversion,
            expected_type=ReversionOpinion,
            direction_error="SHADOW01_M1_POLICY_STATE_INVALID",
            score=reversion.normalized_return_5,
        )
    if not isinstance(cross_asset, CrossAssetOpinion) or not isinstance(
        cross_asset.state, ContextState
    ):
        raise ValueError("SHADOW01_X1_POLICY_STATE_INVALID")
    if not isinstance(fundamental, FundamentalContext) or not isinstance(
        fundamental.state, FundamentalState
    ):
        raise ValueError("SHADOW01_F1_POLICY_STATE_INVALID")
    if not isinstance(quality, QualityAssessment) or not isinstance(quality.state, QualityState):
        raise ValueError("SHADOW01_Q1_POLICY_STATE_INVALID")
    if not isinstance(cost, CostContext) or not isinstance(cost.state, CostState):
        raise ValueError("SHADOW01_C1_POLICY_STATE_INVALID")


def _require_frozen_scope(
    config: ShadowTournamentConfig,
    instrument: str,
    epic: str,
    input_data_fingerprint: str,
) -> None:
    """Refuse to create a correlation record outside the locked 20-market scope."""

    frozen_symbols = tuple(item["symbol"] for item in config.universe)
    if (
        frozen_symbols != EXPECTED_SYMBOLS
        or not isinstance(instrument, str)
        or instrument not in frozen_symbols
    ):
        raise ValueError("SHADOW01_POLICY_INSTRUMENT_OUTSIDE_FROZEN_SCOPE")
    if not isinstance(epic, str) or not epic.strip():
        raise ValueError("SHADOW01_POLICY_EPIC_INVALID")
    if not isinstance(input_data_fingerprint, str) or not input_data_fingerprint.strip():
        raise ValueError("SHADOW01_POLICY_INPUT_FINGERPRINT_INVALID")


def _validate_opinion(
    opinion: TechnicalOpinion | ReversionOpinion,
    *,
    expected_type: type[TechnicalOpinion] | type[ReversionOpinion],
    direction_error: str,
    score: float | None,
) -> None:
    if not isinstance(opinion, expected_type) or not isinstance(opinion.direction, Direction):
        raise ValueError(direction_error)
    if score is not None and (
        isinstance(score, bool) or not isinstance(score, (int, float)) or not math.isfinite(score)
    ):
        raise ValueError(direction_error)
    if not isinstance(opinion.reason_codes, tuple) or not all(
        isinstance(code, str) and code.strip() for code in opinion.reason_codes
    ):
        raise ValueError(direction_error)
