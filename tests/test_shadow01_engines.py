from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from src.ig_trader.shadow01.config import load_config
from src.ig_trader.shadow01.engines import (
    CostInputs,
    CrossAssetInput,
    FundamentalInputs,
    ShadowEngineError,
    assess_c1_cost,
    assess_q1_quality,
    build_f1_context,
    compute_technical_state,
    evaluate_m1_reversion,
    evaluate_t1_trend,
    evaluate_x1_context,
)
from src.ig_trader.shadow01.models import (
    ContextState,
    CostState,
    DailyBar,
    Direction,
    FundamentalState,
    QualityState,
    TechnicalState,
)

NOW = datetime(2026, 9, 2, 17, 10, tzinfo=UTC)


def _bars(closes: list[float]) -> tuple[DailyBar, ...]:
    start = NOW - timedelta(minutes=10, days=len(closes) - 1)
    return tuple(
        DailyBar(
            completed_at=start + timedelta(days=index),
            open=close,
            high=close + 1.0,
            low=max(0.01, close - 1.0),
            close=close,
        )
        for index, close in enumerate(closes)
    )


def _technical_state(*, atr20_over_price: float | None = 0.01) -> TechnicalState:
    return TechnicalState(
        return_1=None,
        return_5=None,
        return_20=None,
        return_60=None,
        atr20_over_price=atr20_over_price,
        realized_volatility_20=None,
        trend_20=None,
        trend_60=None,
        trend_strength=None,
        distance_from_60_session_mean=None,
        drawdown_from_60_session_high=None,
    )


def test_technical_state_and_t1_use_completed_bars_only() -> None:
    config = load_config()
    bars = _bars([100.0 + index for index in range(70)])

    state = compute_technical_state(bars, config, decision_timestamp_utc=NOW)
    opinion = evaluate_t1_trend(state)

    assert state.return_1 == pytest.approx(169.0 / 168.0 - 1.0)
    assert state.return_5 == pytest.approx(169.0 / 164.0 - 1.0)
    assert state.return_20 == pytest.approx(169.0 / 149.0 - 1.0)
    assert state.return_60 == pytest.approx(169.0 / 109.0 - 1.0)
    assert state.atr20_over_price == pytest.approx(2.0 / 169.0)
    assert state.realized_volatility_20 is not None
    assert state.trend_20 is not None and state.trend_60 is not None
    assert state.trend_strength is not None and 0.0 <= state.trend_strength <= 3.0
    assert state.distance_from_60_session_mean is not None
    assert state.drawdown_from_60_session_high is not None
    assert opinion.direction is Direction.LONG

    future = DailyBar(
        completed_at=NOW + timedelta(days=1),
        open=171.0,
        high=172.0,
        low=170.0,
        close=171.0,
    )
    with pytest.raises(ShadowEngineError, match="SHADOW01_INCOMPLETE_OR_FUTURE_BAR"):
        compute_technical_state((*bars, future), config, decision_timestamp_utc=NOW)


def test_m1_percentile_excludes_the_current_completed_observation() -> None:
    config = load_config()
    closes = [100.0 + 0.1 * index for index in range(300)]
    closes[-1] = closes[-2] * 0.8

    opinion = evaluate_m1_reversion(_bars(closes), config, decision_timestamp_utc=NOW)

    assert opinion.direction is Direction.LONG
    assert opinion.percentile == 0.0
    assert opinion.normalized_return_5 is not None and opinion.normalized_return_5 < 0.0
    assert "M1_PERCENTILE_LOOKBACK_COMPLETE" in opinion.reason_codes
    assert "M1_STRONG_DOWNSIDE_STRETCH" in opinion.reason_codes


def test_q1_fails_closed_for_stale_or_unknown_market_data() -> None:
    config = load_config()
    bars = _bars([100.0 + index for index in range(61)])

    normal = assess_q1_quality(
        bars,
        config,
        decision_timestamp_utc=NOW,
        provider_healthy=True,
        stream_healthy=True,
        session_complete=True,
        feature_available=True,
    )
    unknown = assess_q1_quality(
        bars,
        config,
        decision_timestamp_utc=NOW,
        provider_healthy=None,
        stream_healthy=True,
        session_complete=True,
        feature_available=True,
    )
    stale = assess_q1_quality(
        bars,
        config,
        decision_timestamp_utc=NOW + timedelta(hours=3),
        provider_healthy=True,
        stream_healthy=True,
        session_complete=True,
        feature_available=True,
    )

    assert normal.state is QualityState.NORMAL
    assert unknown.state is QualityState.UNKNOWN
    assert stale.state is QualityState.BLOCKED
    assert "Q1_PRICE_STALE" in stale.reason_codes


def test_c1_records_explicit_cost_facts_or_unknown_without_a_fallback() -> None:
    config = load_config()
    high = assess_c1_cost(
        CostInputs(
            reference_price=100.0,
            spread=0.2,
            minimum_stop_distance=1.0,
            product_type="CASH",
            funding_metadata="DAILY",
        ),
        _technical_state(),
        config,
    )
    unknown = assess_c1_cost(None, _technical_state(), config)

    assert high.state is CostState.COST_HIGH
    assert high.spread_to_atr == pytest.approx(0.2)
    assert high.minimum_stop_distance == 1.0
    assert unknown.state is CostState.COST_UNKNOWN
    assert unknown.reason_codes == ("C1_COST_INPUTS_UNAVAILABLE",)


def test_x1_requires_explicit_causally_available_context() -> None:
    config = load_config()
    opposed = evaluate_x1_context(
        (
            CrossAssetInput(
                name="US_2Y",
                alignment_score=-0.8,
                available_at_utc=NOW - timedelta(minutes=1),
                source="sanitized-provider",
            ),
        ),
        config,
        decision_timestamp_utc=NOW,
    )
    future = evaluate_x1_context(
        (
            CrossAssetInput(
                name="US_2Y",
                alignment_score=-0.8,
                available_at_utc=NOW + timedelta(seconds=1),
            ),
        ),
        config,
        decision_timestamp_utc=NOW,
    )
    unavailable = evaluate_x1_context(None, config, decision_timestamp_utc=NOW)

    assert opposed.state is ContextState.OPPOSES
    assert opposed.score == pytest.approx(-0.8)
    assert future.state is ContextState.UNKNOWN
    assert "X1_INPUT_NOT_CAUSALLY_AVAILABLE" in future.reason_codes
    assert unavailable.state is ContextState.UNKNOWN


def test_f1_is_context_only_and_rejects_future_context() -> None:
    unavailable = build_f1_context(None, decision_timestamp_utc=NOW)
    event_risk = build_f1_context(
        FundamentalInputs(
            policy_state="RESTRICTIVE",
            policy_trend="UNCHANGED",
            data_quality="NORMAL",
            event_risk=True,
            available_at_utc=NOW - timedelta(hours=1),
        ),
        decision_timestamp_utc=NOW,
    )
    future = build_f1_context(
        FundamentalInputs(
            data_quality="NORMAL",
            event_risk=False,
            available_at_utc=NOW + timedelta(seconds=1),
        ),
        decision_timestamp_utc=NOW,
    )

    assert unavailable.state is FundamentalState.UNKNOWN
    assert "FRED_CONTEXT_UNAVAILABLE" in unavailable.reason_codes
    assert event_risk.state is FundamentalState.EVENT_RISK
    assert event_risk.event_risk is True
    assert not hasattr(event_risk, "direction")
    assert future.state is FundamentalState.UNKNOWN
    assert "F1_CONTEXT_NOT_CAUSALLY_AVAILABLE" in future.reason_codes


def test_engine_module_has_no_execution_or_future_label_imports() -> None:
    source = (Path(__file__).resolve().parents[1] / "src/ig_trader/shadow01/engines.py").read_text(
        encoding="utf-8"
    )

    assert "shadow_execution" not in source
    assert "from src.ig_trader.execution" not in source
    assert "from src.ig_trader.demo" not in source
    assert "OutcomeLabel" not in source
