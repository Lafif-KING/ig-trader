"""The Main Conductor of the IG Trader platform."""

import structlog
from src.ig_trader.session import SessionManager
from src.ig_trader.market_data import MarketDataClient
from src.ig_trader.strategies.scalper import ScalperStrategy
from src.ig_trader.risk import RiskEngine
from src.ig_trader.portfolio import PortfolioManager

logger = structlog.get_logger(__name__)

class TradingBot:
    """Orchestrates the data, strategy, and risk components."""

    def __init__(self):
        self.session = SessionManager()
        self.market_data = MarketDataClient(self.session)
        self.portfolio = PortfolioManager(total_balance=0.0) # Will update after login
        self.risk = RiskEngine(self.portfolio)
        self.strategy = ScalperStrategy()

    def start(self):
        """Starts the bot workflow."""
        logger.info("bot_starting")
        
        # 1. Login
        if not self.session.login():
            logger.error("bot_start_failed_login")
            return

        # 2. Get real balance
        # (Placeholder for fetching balance from IG)
        self.portfolio.total_balance = 1000.0 
        logger.info("bot_ready", balance=self.portfolio.total_balance)

        # 3. Process a test signal
        # This is where we combine everything!
        logger.info("bot_running_cycle")
        
        # Fetch data -> Run Strategy -> Check Risk
        df = self.market_data.get_prices("CS.D.EURUSD.MINI.IP", "MINUTE", max_points=50)
        signal = self.strategy.generate_signal("EURUSD", df)
        
        if self.risk.validate_signal(signal):
            logger.info("TRADE_ALLOWED", epic=signal.epic, direction=signal.direction)
        else:
            logger.info("TRADE_BLOCKED_BY_RISK")

if __name__ == "__main__":
    bot = TradingBot()
    bot.start()
