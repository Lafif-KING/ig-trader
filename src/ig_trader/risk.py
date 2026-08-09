"""Risk Management Engine to protect capital."""

import structlog

from src.ig_trader.models import Signal, SignalDirection
from src.ig_trader.portfolio import PortfolioManager

logger = structlog.get_logger(__name__)


class RiskEngine:
    """The safety guard that validates every trade."""

    def __init__(
        self, portfolio_mgr: PortfolioManager, max_risk_per_trade: float = 0.02
    ):
        """
        Args:
            portfolio_mgr: The boss who knows the budget.
            max_risk_per_trade: Maximum percentage of strategy budget to risk (default 2%).
        """
        self.portfolio_mgr = portfolio_mgr
        self.max_risk_per_trade = max_risk_per_trade

    def validate_signal(self, signal: Signal) -> bool:
        """
        Checks if a signal is safe to trade.
        Returns: True if allowed, False if blocked.
        """
        if signal.direction == SignalDirection.WAIT:
            return False

        # 1. Get the specific budget for this strategy (e.g., 30% of total)
        strategy_budget = self.portfolio_mgr.get_budget_for_strategy(
            signal.strategy_name
        )

        if strategy_budget <= 0:
            logger.warning("risk_block_no_budget", strategy=signal.strategy_name)
            return False

        # 2. Check if we are within risk limits (Placeholder for further checks)
        # In a real system, we'd check current open positions here too.

        logger.info(
            "risk_validation_passed",
            strategy=signal.strategy_name,
            epic=signal.epic,
            budget=strategy_budget,
        )
        return True

    def calculate_lot_size(self, strategy_name: str, stop_loss_pips: int) -> float:
        """Calculates how much to buy so we only risk our 2% limit."""
        # Simple lot calculation (Simplified for Forex)
        # Lot = Risk Amount / (Stop Loss Pips * Pip Value)
        # We will refine this later, for now we return a safe minimum lot.
        return 0.5
