"""Conservative, per-instrument Demo qualification statistics.

The evaluator is evidence-only.  It cannot promote a research instrument into
the Demo execution registry and never classifies a strategy from profit alone.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal

from src.ig_trader.demo_operator_models import DemoResultClassification


@dataclass(frozen=True)
class DemoTradeOutcome:
    instrument: str
    strategy_id: str
    strategy_version: str
    realized_pnl: Decimal
    realized_r: Decimal
    spread_cost: Decimal | None
    slippage: Decimal | None
    holding_time: timedelta | None
    rejection_reason: str | None = None


@dataclass(frozen=True)
class DemoQualificationMetrics:
    trade_count: int
    wins: int
    losses: int
    win_rate: Decimal | None
    net_pnl: Decimal
    net_r: Decimal
    expectancy_r: Decimal | None
    profit_factor: Decimal | None
    max_drawdown_r: Decimal
    current_losing_streak: int
    average_holding_time: timedelta | None
    spread_cost: Decimal
    slippage: Decimal
    rejection_count: int
    classification: DemoResultClassification


def evaluate_demo_results(
    outcomes: tuple[DemoTradeOutcome, ...], *, minimum_trade_count: int = 20
) -> DemoQualificationMetrics:
    """Classify a single instrument/strategy series with sample and risk checks."""

    if minimum_trade_count <= 0:
        raise ValueError("minimum Demo trade count must be positive")
    if not outcomes:
        return _empty_metrics(DemoResultClassification.DEMO_NOT_STARTED)
    wins = sum(item.realized_r > 0 for item in outcomes)
    losses = sum(item.realized_r < 0 for item in outcomes)
    gross_win = sum((item.realized_r for item in outcomes if item.realized_r > 0), Decimal("0"))
    gross_loss = -sum((item.realized_r for item in outcomes if item.realized_r < 0), Decimal("0"))
    net_r = sum((item.realized_r for item in outcomes), Decimal("0"))
    net_pnl = sum((item.realized_pnl for item in outcomes), Decimal("0"))
    trade_count = len(outcomes)
    expectation = net_r / Decimal(trade_count)
    profit_factor = None if gross_loss == 0 else gross_win / gross_loss
    metrics = DemoQualificationMetrics(
        trade_count=trade_count,
        wins=wins,
        losses=losses,
        win_rate=Decimal(wins) / Decimal(trade_count),
        net_pnl=net_pnl,
        net_r=net_r,
        expectancy_r=expectation,
        profit_factor=profit_factor,
        max_drawdown_r=_max_drawdown(outcomes),
        current_losing_streak=_losing_streak(outcomes),
        average_holding_time=_average_holding_time(outcomes),
        spread_cost=sum((item.spread_cost or Decimal("0") for item in outcomes), Decimal("0")),
        slippage=sum((item.slippage or Decimal("0") for item in outcomes), Decimal("0")),
        rejection_count=sum(item.rejection_reason is not None for item in outcomes),
        classification=DemoResultClassification.DEMO_QUALIFYING,
    )
    return _with_classification(metrics, minimum_trade_count)


def _with_classification(
    metrics: DemoQualificationMetrics, minimum_trade_count: int
) -> DemoQualificationMetrics:
    if metrics.trade_count < minimum_trade_count:
        classification = DemoResultClassification.DEMO_LOW_SAMPLE
    elif metrics.expectancy_r is None or metrics.expectancy_r <= 0:
        classification = DemoResultClassification.DEMO_REJECTED
    elif (
        metrics.profit_factor is None
        or metrics.profit_factor < Decimal("1.1")
        or metrics.max_drawdown_r > Decimal("2")
        or metrics.current_losing_streak >= 5
    ):
        classification = DemoResultClassification.DEMO_WATCH
    else:
        classification = DemoResultClassification.DEMO_QUALIFIED
    return DemoQualificationMetrics(**{**metrics.__dict__, "classification": classification})


def _empty_metrics(classification: DemoResultClassification) -> DemoQualificationMetrics:
    return DemoQualificationMetrics(
        trade_count=0,
        wins=0,
        losses=0,
        win_rate=None,
        net_pnl=Decimal("0"),
        net_r=Decimal("0"),
        expectancy_r=None,
        profit_factor=None,
        max_drawdown_r=Decimal("0"),
        current_losing_streak=0,
        average_holding_time=None,
        spread_cost=Decimal("0"),
        slippage=Decimal("0"),
        rejection_count=0,
        classification=classification,
    )


def _max_drawdown(outcomes: tuple[DemoTradeOutcome, ...]) -> Decimal:
    equity = Decimal("0")
    peak = Decimal("0")
    maximum = Decimal("0")
    for outcome in outcomes:
        equity += outcome.realized_r
        peak = max(peak, equity)
        maximum = max(maximum, peak - equity)
    return maximum


def _losing_streak(outcomes: tuple[DemoTradeOutcome, ...]) -> int:
    count = 0
    for outcome in reversed(outcomes):
        if outcome.realized_r >= 0:
            break
        count += 1
    return count


def _average_holding_time(outcomes: tuple[DemoTradeOutcome, ...]) -> timedelta | None:
    values = tuple(item.holding_time for item in outcomes if item.holding_time is not None)
    if not values:
        return None
    return sum(values, timedelta()) / len(values)
