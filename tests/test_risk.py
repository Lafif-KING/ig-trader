from datetime import datetime

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

    assert risk.validate_signal(signal) is False


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

    assert risk.validate_signal(signal) is True
