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

    def start(self):
        """Starts the bot workflow."""
        logger.info("bot_starting")

        # 1. Login
        if not self.session.login():
            logger.error("bot_start_failed_login")
            return

        # 2. Get Account Balance (Using your Demo balance from the login response)
        self.portfolio.total_balance = 20000.0  # Placeholder: you have ~23k in your demo
        logger.info("bot_ready", balance=self.portfolio.total_balance)

        # 3. RUN ONE CYCLE
        # A. Fetch Data
        epic = "CS.D.EURUSD.MINI.IP"
        df = self.market_data.get_prices(epic, "MINUTE", max_points=50)

        # B. Generate Signal from Strategy
        signal = self.strategy.generate_signal(epic, df)
        logger.info("strategy_output", direction=signal.direction.value)

        # C. Validate with Risk Engine
        if self.risk.validate_signal(signal):
            logger.info("TRADE_ALLOWED", epic=signal.epic, direction=signal.direction.value)

            # D. Execute Trade
            lot_size = self.risk.calculate_lot_size(signal.strategy_name, 10)
            self.execution.execute_trade(signal, lot_size)
        else:
            logger.info("TRADE_BLOCKED_OR_WAITING", reason="Signal is WAIT or Risk denied")


if __name__ == "__main__":
    bot = TradingBot()
    bot.start()
