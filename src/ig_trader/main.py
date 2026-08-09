"""The Main Conductor of the IG Trader platform."""

import structlog

from src.ig_trader.execution import ExecutionEngine
from src.ig_trader.market_data import MarketDataClient
from src.ig_trader.portfolio import PortfolioManager
from src.ig_trader.risk import RiskEngine
from src.ig_trader.session import SessionManager
from src.ig_trader.strategies.scalper import ScalperStrategy

logger = structlog.get_logger(__name__)


class TradingBot:
    """Orchestrates the data, strategy, and risk components."""

    def __init__(self):
        self.session = SessionManager()
        self.market_data = MarketDataClient(self.session)
        self.portfolio = PortfolioManager(total_balance=0.0)
        self.risk = RiskEngine(self.portfolio)
        self.strategy = ScalperStrategy()
        self.execution = ExecutionEngine(self.session)

    def start(self) -> None:
        """Starts the bot workflow."""
        logger.info("bot_starting")

        if not self.session.login():
            logger.error("bot_start_failed_login")
            return

        self.portfolio.total_balance = 20000.0
        logger.info("bot_ready", balance=self.portfolio.total_balance)

        # ↓↓↓ ADD STATUS CHECK *HERE*, inside start(), before get_prices ↓↓↓
        epic = "CS.D.EURUSD.MINI.IP"

        status = self.market_data.get_market_status(epic)
        if status != "TRADEABLE":
            logger.info(
                "market_closed_or_untradeable",
                epic=epic,
                status=status,
            )
            return  # Exit this cycle without trading

        df = self.market_data.get_prices(epic, "MINUTE", max_points=50)
        signal = self.strategy.generate_signal(epic, df)
        logger.info("strategy_output", direction=signal.direction.value)

        if self.risk.validate_signal(signal):
            logger.info("TRADE_ALLOWED", epic=signal.epic, direction=signal.direction.value)
            lot_size = self.risk.calculate_lot_size(signal.strategy_name, 10)
            self.execution.execute_trade(signal, lot_size)
        else:
            logger.info("TRADE_BLOCKED_OR_WAITING")


if __name__ == "__main__":
    bot = TradingBot()
    bot.start()
