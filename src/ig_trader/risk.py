"""Risk Management Engine to protect capital."""

from __future__ import annotations

from dataclasses import dataclass

import structlog

from src.ig_trader.models import Signal, SignalDirection
from src.ig_trader.portfolio import PortfolioManager

logger = structlog.get_logger(__name__)


@dataclass
class StrategyRiskConfig:
    """Per-strategy risk configuration."""

    name: str
    risk_per_trade_pct: float  # % of strategy budget
    max_positions: int
    stop_loss_pips: int
    take_profit_pips: int


class RiskEngine:
    """The safety guard that validates every trade and sizes positions."""

    def __init__(
        self,
        portfolio_mgr: PortfolioManager,
        max_daily_loss_pct: float = 0.05,
    ) -> None:
        """
        Args:
            portfolio_mgr: The boss who knows the budget split (30/70).
            max_daily_loss_pct: Max account loss per day (e.g. 5%).
        """
        self.portfolio_mgr = portfolio_mgr
        self.max_daily_loss_pct = max_daily_loss_pct

        # Per-strategy configs (you can tune these later)
        self.strategy_risk: dict[str, StrategyRiskConfig] = {
            "Scalper": StrategyRiskConfig(
                name="Scalper",
                risk_per_trade_pct=0.005,  # 0.5% of scalper budget
                max_positions=5,
                stop_loss_pips=12,
                take_profit_pips=7,
            ),
            "Intraday": StrategyRiskConfig(
                name="Intraday",
                risk_per_trade_pct=0.01,  # 1% of intraday budget
                max_positions=2,
                stop_loss_pips=40,
                take_profit_pips=80,
            ),
        }

    def validate_signal(
        self,
        signal: Signal,
        open_positions_for_strategy: int = 0,
        daily_loss_pct: float = 0.0,
    ) -> bool:
        """
        Checks if a signal is safe to trade.

        Args:
            signal: Proposed trade signal.
            open_positions_for_strategy: Current open positions for this strategy.
            daily_loss_pct: Current % loss for today (negative if losing).

        Returns:
            True if allowed, False if blocked.
        """
        if signal.direction == SignalDirection.WAIT:
            logger.info(
                "risk_block_wait_signal",
                strategy=signal.strategy_name,
                epic=signal.epic,
            )
            return False

        cfg = self.strategy_risk.get(signal.strategy_name)
        if cfg is None:
            logger.warning(
                "risk_block_unknown_strategy",
                strategy=signal.strategy_name,
            )
            return False

        # Check daily loss vs max allowed
        if daily_loss_pct <= -self.max_daily_loss_pct:
            logger.warning(
                "risk_block_max_daily_loss",
                strategy=signal.strategy_name,
                daily_loss_pct=daily_loss_pct,
                max_daily_loss_pct=self.max_daily_loss_pct,
            )
            return False

        # Check max concurrent positions for this strategy
        if open_positions_for_strategy >= cfg.max_positions:
            logger.warning(
                "risk_block_max_positions",
                strategy=signal.strategy_name,
                open_positions=open_positions_for_strategy,
                max_positions=cfg.max_positions,
            )
            return False

        # Check there is some budget for this strategy
        strategy_budget = self.portfolio_mgr.get_budget_for_strategy(signal.strategy_name)
        if strategy_budget <= 0:
            logger.warning(
                "risk_block_no_budget",
                strategy=signal.strategy_name,
            )
            return False

        logger.info(
            "risk_validation_passed",
            strategy=signal.strategy_name,
            epic=signal.epic,
            budget=strategy_budget,
        )
        return True

    def calculate_lot_size(
        self,
        strategy_name: str,
        price: float,
    ) -> float:
        """
        Calculate lot size for a trade.

        Uses per-strategy risk_per_trade_pct and stop_loss_pips.

        Args:
            strategy_name: "Scalper" or "Intraday".
            price: Current price (used later for more precise pip value).

        Returns:
            Lot size (rounded to 2 decimals).
        """
        cfg = self.strategy_risk.get(strategy_name)
        if cfg is None:
            logger.warning("risk_no_config_for_strategy", strategy=strategy_name)
            return 0.0

        budget = self.portfolio_mgr.get_budget_for_strategy(strategy_name)
        if budget <= 0:
            logger.warning("risk_no_budget_for_strategy", strategy=strategy_name)
            return 0.0

        # Risked money on this trade:
        risk_amount = budget * cfg.risk_per_trade_pct

        # Simplified FX pip value: assume 10 EUR per pip per standard lot on majors
        # (This is a simplification; we can refine by pair/base currency later.)
        pip_value_per_standard_lot = 10.0

        # Lot size = Money at risk / (SL pips * pip_value_per_lot)
        if cfg.stop_loss_pips <= 0:
            logger.warning("risk_invalid_sl_pips", strategy=strategy_name)
            return 0.0

        raw_lot = risk_amount / (cfg.stop_loss_pips * pip_value_per_standard_lot)

        # Clamp to a reasonable range
        lot_size = max(0.1, min(round(raw_lot, 2), 10.0))

        logger.info(
            "risk_lot_size_calculated",
            strategy=strategy_name,
            budget=budget,
            risk_amount=risk_amount,
            stop_loss_pips=cfg.stop_loss_pips,
            lot_size=lot_size,
        )
        return lot_size

    def get_sl_tp_pips(self, strategy_name: str) -> tuple[int, int]:
        """Return (SL pips, TP pips) for a strategy."""
        cfg = self.strategy_risk.get(strategy_name)
        if cfg is None:
            return (0, 0)
        return (cfg.stop_loss_pips, cfg.take_profit_pips)
