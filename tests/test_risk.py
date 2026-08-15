from datetime import datetime

import pytest

from src.ig_trader.models import Signal, SignalDirection
from src.ig_trader.portfolio import PortfolioManager
from src.ig_trader.risk import RiskEngine


def test_risk_blocks_when_no_budget():
    # Setup: Balance is $0
    pm = PortfolioManager(total_balance=0.0)
    risk = RiskEngine(pm)

    signal = Signal(
        epic="EURUSD",
        direction=SignalDirection.BUY,
        timestamp=datetime.now(),
        price=1.10,
        strategy_name="Scalper",
    )

    assert (
        risk.validate_signal(
            signal,
            open_positions_for_strategy=0,
            daily_loss_pct=0.0,
        )
        is False
    )


def test_risk_allows_when_budget_exists():
    # Setup: Balance is $1000
    pm = PortfolioManager(total_balance=1000.0)
    risk = RiskEngine(pm)

    signal = Signal(
        epic="EURUSD",
        direction=SignalDirection.BUY,
        timestamp=datetime.now(),
        price=1.10,
        strategy_name="Scalper",
    )

    assert (
        risk.validate_signal(
            signal,
            open_positions_for_strategy=0,
            daily_loss_pct=0.0,
        )
        is True
    )


def test_risk_requires_explicit_current_state() -> None:
    risk = RiskEngine(PortfolioManager(total_balance=1000.0))
    signal = Signal(
        epic="CS.D.EURGBP.MINI.IP",
        direction=SignalDirection.BUY,
        timestamp=datetime.now(),
        price=0.86,
        strategy_name="Scalper",
    )

    with pytest.raises(TypeError):
        risk.validate_signal(signal)
