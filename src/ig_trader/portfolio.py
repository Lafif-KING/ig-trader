"""Portfolio Manager to handle budget allocation between strategies."""

from src.ig_trader.models import StrategyConfig


class PortfolioManager:
    """Manages budget and configuration for multiple strategies."""

    def __init__(self, total_balance: float):
        """
        Initialize with the account balance and set the 30/70 split.

        Args:
            total_balance: The current account balance from IG.
        """
        self.total_balance = total_balance

        # Here is your custom 30/70 split!
        self.allocations = {
            "Scalper": StrategyConfig(
                name="Scalper",
                enabled=True,
                budget_percentage=0.30,  # 30% for quick trades
                max_positions=5,
                timeframe="MINUTE",
            ),
            "Intraday": StrategyConfig(
                name="Intraday",
                enabled=True,
                budget_percentage=0.70,  # 70% for daily trends
                max_positions=2,
                timeframe="HOUR",
            ),
        }

    def get_strategy_config(self, strategy_name: str) -> StrategyConfig:
        """Get the configuration for a specific strategy."""
        return self.allocations.get(strategy_name)

    def get_budget_for_strategy(self, strategy_name: str) -> float:
        """Calculate the cash amount allowed for a strategy."""
        config = self.get_strategy_config(strategy_name)
        if config:
            return self.total_balance * config.budget_percentage
        return 0.0
