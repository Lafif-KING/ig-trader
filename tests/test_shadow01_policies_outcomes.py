from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from src.ig_trader.shadow01.config import ShadowTournamentConfig, load_config
from src.ig_trader.shadow01.models import (
    ContextState,
    CostContext,
    CostState,
    CrossAssetOpinion,
    DailyBar,
    Direction,
    FundamentalContext,
    FundamentalState,
    OutcomeLabel,
    PolicyId,
    QualityAssessment,
    QualityState,
    ShadowDecision,
)
from src.ig_trader.shadow01.outcomes import OutcomeResolutionInput, resolve_outcomes
from src.ig_trader.shadow01.policies import PolicyRecommendation, materialize_decisions

NOW = datetime(2026, 9, 2, 21, 10, tzinfo=UTC)


def _context(
    *,
    cross_asset: ContextState = ContextState.NEUTRAL,
    fundamental: FundamentalState = FundamentalState.NORMAL,
    quality: QualityState = QualityState.NORMAL,
    cost: CostState = CostState.COST_OK,
) -> tuple[CrossAssetOpinion, FundamentalContext, QualityAssessment, CostContext]:
    return (
        CrossAssetOpinion(cross_asset, 0.0, ("X1_TEST",), {}),
        FundamentalContext(
            fundamental,
            "NEUTRAL",
            "UNCHANGED",
            "NORMAL",
            0,
            fundamental is FundamentalState.EVENT_RISK,
            ("F1_TEST",),
        ),
        QualityAssessment(quality, 0, ("Q1_TEST",)),
        CostContext(cost, 0.01, 0.01, 1.0, "CASH", "DAILY", ("C1_TEST",)),
    )


def _injected_recommendations() -> tuple[PolicyRecommendation, ...]:
    """Deliberately reverse the caller order to prove canonical materialization."""

    return (
        PolicyRecommendation(PolicyId.P3_CONSERVATIVE_CONTEXT, Direction.LONG, "T1", 1.0, ("P3",)),
        PolicyRecommendation(
            PolicyId.P2_TREND_PLUS_CROSS_ASSET, Direction.LONG, "T1", 1.0, ("P2",)
        ),
        PolicyRecommendation(
            PolicyId.P1_TECHNICAL_REVERSION_ONLY, Direction.LONG, "M1", 1.0, ("P1",)
        ),
        PolicyRecommendation(PolicyId.P0_TECHNICAL_TREND_ONLY, Direction.LONG, "T1", 1.0, ("P0",)),
    )


def _materialize(
    config: ShadowTournamentConfig,
    recommendations: tuple[PolicyRecommendation, ...],
    *,
    cross_asset: CrossAssetOpinion,
    fundamental: FundamentalContext,
    quality: QualityAssessment,
    cost: CostContext,
) -> tuple[ShadowDecision, ...]:
    return materialize_decisions(
        config,
        decision_timestamp_utc=NOW,
        instrument="EURUSD",
        epic="TEST.EURUSD",
        input_data_fingerprint="immutable-input-fingerprint",
        recommendations=recommendations,
        cross_asset=cross_asset,
        fundamental=fundamental,
        quality=quality,
        cost=cost,
    )


def _decision() -> ShadowDecision:
    return ShadowDecision(
        decision_id="outcome-decision",
        tournament_version="SHADOW01-V1",
        config_fingerprint="f" * 64,
        decision_timestamp_utc=NOW,
        instrument="EURUSD",
        epic="TEST.EURUSD",
        policy_id=PolicyId.P0_TECHNICAL_TREND_ONLY,
        direction=Direction.LONG,
        technical_engine="T1",
        technical_score=1.0,
        cross_asset_state=ContextState.UNKNOWN,
        fundamental_context=FundamentalState.UNKNOWN,
        quality_state=QualityState.UNKNOWN,
        cost_state=CostState.COST_UNKNOWN,
        factor_tags=("EUR_LONG", "USD_SHORT"),
        reason_codes=("TEST",),
        input_data_fingerprint="input-fingerprint",
        created_at=NOW,
    )


def _bar(session: int, close: float = 100.0) -> DailyBar:
    return DailyBar(
        completed_at=NOW + timedelta(days=session),
        open=close,
        high=close + 1.0,
        low=close - 1.0,
        close=close,
    )


def test_materialization_is_canonical_deterministic_and_quality_fails_closed() -> None:
    config = load_config()
    cross_asset, fundamental, quality, cost = _context(quality=QualityState.BLOCKED)

    first = _materialize(
        config,
        _injected_recommendations(),
        cross_asset=cross_asset,
        fundamental=fundamental,
        quality=quality,
        cost=cost,
    )
    second = _materialize(
        config,
        _injected_recommendations(),
        cross_asset=cross_asset,
        fundamental=fundamental,
        quality=quality,
        cost=cost,
    )

    assert tuple(item.policy_id for item in first) == tuple(PolicyId)
    assert first == second
    assert {item.created_at for item in first} == {NOW}
    assert {item.direction for item in first} == {Direction.BLOCK}
    assert all(item.factor_tags == () for item in first)
    assert all("Q1_BLOCKED" in item.reason_codes for item in first)


def test_materialization_cannot_bypass_p2_or_p3_contextual_blocks() -> None:
    config = load_config()
    cross_asset, fundamental, quality, cost = _context(
        cross_asset=ContextState.UNKNOWN,
        fundamental=FundamentalState.EVENT_RISK,
        cost=CostState.COST_HIGH,
    )

    decisions = _materialize(
        config,
        _injected_recommendations(),
        cross_asset=cross_asset,
        fundamental=fundamental,
        quality=quality,
        cost=cost,
    )
    by_policy = {item.policy_id: item for item in decisions}

    assert by_policy[PolicyId.P0_TECHNICAL_TREND_ONLY].direction is Direction.LONG
    assert by_policy[PolicyId.P1_TECHNICAL_REVERSION_ONLY].direction is Direction.LONG
    assert by_policy[PolicyId.P2_TREND_PLUS_CROSS_ASSET].direction is Direction.BLOCK
    assert by_policy[PolicyId.P3_CONSERVATIVE_CONTEXT].direction is Direction.BLOCK
    assert (
        "P2_X1_NOT_SUPPORTIVE_OR_NEUTRAL"
        in by_policy[PolicyId.P2_TREND_PLUS_CROSS_ASSET].reason_codes
    )
    assert {"P3_C1_HIGH", "P3_F1_EVENT_RISK"} <= set(
        by_policy[PolicyId.P3_CONSERVATIVE_CONTEXT].reason_codes
    )


def test_direct_decision_construction_cannot_bypass_p2_context_gate() -> None:
    with pytest.raises(ValueError, match="SHADOW01_DIRECTIONAL_P2_CONTEXT_BLOCKED"):
        ShadowDecision(
            decision_id="unsafe-p2",
            tournament_version="SHADOW01-V1",
            config_fingerprint="f" * 64,
            decision_timestamp_utc=NOW,
            instrument="EURUSD",
            epic="TEST.EURUSD",
            policy_id=PolicyId.P2_TREND_PLUS_CROSS_ASSET,
            direction=Direction.LONG,
            technical_engine="T1",
            technical_score=1.0,
            cross_asset_state=ContextState.UNKNOWN,
            fundamental_context=FundamentalState.NORMAL,
            quality_state=QualityState.NORMAL,
            cost_state=CostState.COST_OK,
            factor_tags=("EUR_LONG", "USD_SHORT"),
            reason_codes=("UNSAFE",),
            input_data_fingerprint="input-fingerprint",
            created_at=NOW,
        )


def test_direct_decision_construction_cannot_forge_factor_exposure_tags() -> None:
    with pytest.raises(ValueError, match="factor tags do not match"):
        ShadowDecision(
            decision_id="forged-tags",
            tournament_version="SHADOW01-V1",
            config_fingerprint="f" * 64,
            decision_timestamp_utc=NOW,
            instrument="EURUSD",
            epic="TEST.EURUSD",
            policy_id=PolicyId.P0_TECHNICAL_TREND_ONLY,
            direction=Direction.LONG,
            technical_engine="T1",
            technical_score=1.0,
            cross_asset_state=ContextState.NEUTRAL,
            fundamental_context=FundamentalState.NORMAL,
            quality_state=QualityState.NORMAL,
            cost_state=CostState.COST_OK,
            factor_tags=("USD_LONG", "EUR_SHORT"),
            reason_codes=("FORGED",),
            input_data_fingerprint="input-fingerprint",
            created_at=NOW,
        )


def test_outcomes_use_exact_completed_session_horizons() -> None:
    bars = tuple(_bar(session, 100.0 + session) for session in range(1, 21))

    outcomes = resolve_outcomes(
        OutcomeResolutionInput(
            _decision(),
            entry_price=100.0,
            atr20_over_price=0.01,
            future_completed_bars=bars,
        )
    )

    assert tuple(item.horizon_sessions for item in outcomes) == (1, 3, 5, 10, 20)
    assert tuple(item.outcome_timestamp_utc for item in outcomes) == tuple(
        bars[index].completed_at for index in (0, 2, 4, 9, 19)
    )
    assert {item.quality for item in outcomes} == {QualityState.NORMAL}
    assert all(item.cost_adjusted_result is None for item in outcomes)


@pytest.mark.parametrize(
    ("entry_price", "atr20_over_price", "bars", "error"),
    (
        (float("nan"), None, (), "SHADOW01_OUTCOME_ENTRY_PRICE_INVALID"),
        (True, None, (), "SHADOW01_OUTCOME_ENTRY_PRICE_INVALID"),
        (100.0, float("inf"), (), "SHADOW01_OUTCOME_ATR_INVALID"),
        (100.0, None, (_bar(2), _bar(1)), "SHADOW01_OUTCOME_SESSIONS_MUST_BE_STRICTLY_ORDERED"),
        (100.0, None, (_bar(0),), "SHADOW01_OUTCOME_NON_FUTURE_SESSION"),
    ),
)
def test_outcomes_reject_invalid_prices_and_non_future_sessions(
    entry_price: float,
    atr20_over_price: float | None,
    bars: tuple[DailyBar, ...],
    error: str,
) -> None:
    with pytest.raises(ValueError, match=error):
        OutcomeResolutionInput(
            _decision(),
            entry_price=entry_price,
            atr20_over_price=atr20_over_price,
            future_completed_bars=bars,
        )


def test_outcomes_revalidate_bypassed_future_bar_prices() -> None:
    invalid = _bar(1)
    object.__setattr__(invalid, "close", float("nan"))

    with pytest.raises(ValueError, match="SHADOW01_OUTCOME_FUTURE_PRICE_INVALID"):
        OutcomeResolutionInput(
            _decision(),
            entry_price=100.0,
            atr20_over_price=None,
            future_completed_bars=(invalid,),
        )


def test_outcomes_revalidate_bars_again_when_a_frozen_input_is_bypassed() -> None:
    value = OutcomeResolutionInput(
        _decision(),
        entry_price=100.0,
        atr20_over_price=None,
        future_completed_bars=(_bar(1),),
    )
    object.__setattr__(value.future_completed_bars[0], "close", float("nan"))

    with pytest.raises(ValueError, match="SHADOW01_OUTCOME_FUTURE_PRICE_INVALID"):
        resolve_outcomes(value)


def test_outcome_model_rejects_a_resolved_label_with_an_invalid_future_price() -> None:
    with pytest.raises(ValueError, match="Shadow outcome future price is invalid"):
        OutcomeLabel(
            decision_id="outcome-decision",
            horizon_sessions=1,
            reference_entry_price=100.0,
            future_price=0.0,
            raw_directional_return=0.0,
            atr_normalized_return=None,
            cost_adjusted_result=None,
            outcome_timestamp_utc=NOW + timedelta(days=1),
            quality=QualityState.NORMAL,
            blocked_reason=None,
        )
