"""Canonical broker-neutral Frozen V1 strategy and portfolio policy."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from math import floor, isfinite

from src.ig_trader.offline_paper.domain import AccountSnapshot, RiskDecision, TradeCandidate

G2_HISTORICAL_FIXTURE_INSTRUMENTS = (
    ("EURGBP", "CS.D.EURGBP.MINI.IP", "EUR", "GBP"),
    ("EURUSD", "CS.D.EURUSD.MINI.IP", "EUR", "USD"),
    ("GBPUSD", "CS.D.GBPUSD.MINI.IP", "GBP", "USD"),
)

FROZEN_V1_PRODUCTION_INSTRUMENTS = (
    ("EURGBP", "CS.D.EURGBP.MINI.IP", "EUR", "GBP"),
    ("EURUSD", "CS.D.EURUSD.CEFM.IP", "EUR", "USD"),
    ("GBPUSD", "CS.D.GBPUSD.MINI.IP", "GBP", "USD"),
)


@dataclass(frozen=True)
class FrozenV1Config:
    rsi_period: int = 7
    confidence_threshold: float = 0.70
    adx_threshold: float = 20.0
    warmup_candles: int = 60
    stop_atr_multiplier: float = 2.0
    reward_to_risk: float = 1.5
    maximum_stop_pips: float = 12.0
    maximum_spread_pips: float = 1.2
    maximum_spread_to_target_ratio: float = 0.15
    maximum_total_positions: int = 1
    maximum_positions_per_instrument: int = 1
    maximum_executions_per_cycle: int = 1
    scalper_budget_fraction: float = 0.30
    scalper_risk_fraction: float = 0.005
    maximum_daily_loss_fraction: float = 0.05

    def __post_init__(self) -> None:
        if asdict(self) != asdict(FrozenV1Config.__new_defaults__()):
            raise ValueError("frozen V1 configuration cannot be changed")

    @classmethod
    def __new_defaults__(cls) -> FrozenV1Config:
        value = object.__new__(cls)
        for name, field in cls.__dataclass_fields__.items():
            object.__setattr__(value, name, field.default)
        return value

    @property
    def configuration_hash(self) -> str:
        """The immutable historical G2 OFFLINE_PAPER identity."""

        return self._configuration_hash(
            execution_mode="OFFLINE_PAPER",
            instruments=G2_HISTORICAL_FIXTURE_INSTRUMENTS,
        )

    @property
    def shadow_configuration_hash(self) -> str:
        """The production Shadow identity, distinct from historical G2."""

        return self._configuration_hash(
            execution_mode="SHADOW_DEMO",
            instruments=FROZEN_V1_PRODUCTION_INSTRUMENTS,
        )

    def _configuration_hash(
        self,
        *,
        execution_mode: str,
        instruments: tuple[tuple[str, str, str, str], ...],
    ) -> str:
        document = {
            "parameters": asdict(self),
            "instruments": instruments,
            "strategy": "Scalper:rsi-adx-v1",
            "execution_mode": execution_mode,
            "ai_trading_authority": False,
            "strategy_optimization": False,
            "advanced_management": False,
            "autonomous_intraday_authority": False,
        }
        return hashlib.sha256(_encode(document).encode()).hexdigest()


class PortfolioRisk:
    """Absolute-veto Frozen V1 portfolio policy with explicit current state."""

    def __init__(self, config: FrozenV1Config) -> None:
        self.config = config

    def evaluate(
        self,
        candidate: TradeCandidate,
        *,
        account: object,
        executions_in_cycle: int,
        stop_pips: float,
    ) -> RiskDecision:
        if not isinstance(account, AccountSnapshot) or not account.state_known:
            return _risk_block("ACCOUNT_STATE_UNKNOWN")
        if account.captured_at != candidate.quote.timestamp:
            return _risk_block("ACCOUNT_STATE_STALE")
        daily_loss = account.daily_loss_pct
        if daily_loss is None:
            return _risk_block("DAILY_RISK_UNKNOWN")
        if daily_loss <= -self.config.maximum_daily_loss_fraction:
            return _risk_block("DAILY_LOSS_LIMIT")
        if executions_in_cycle < 0:
            return _risk_block("CYCLE_EXECUTION_STATE_UNKNOWN")
        if executions_in_cycle >= self.config.maximum_executions_per_cycle:
            return _risk_block("CYCLE_EXECUTION_LIMIT")
        if len(account.positions) >= self.config.maximum_total_positions:
            return _risk_block("TOTAL_POSITION_LIMIT")
        same_epic = sum(position.epic == candidate.signal.epic for position in account.positions)
        if same_epic >= self.config.maximum_positions_per_instrument:
            return _risk_block("INSTRUMENT_POSITION_LIMIT")
        if not isfinite(stop_pips) or stop_pips <= 0:
            return _risk_block("STOP_STATE_UNKNOWN")
        monetary_risk = (
            account.balance
            * self.config.scalper_budget_fraction
            * self.config.scalper_risk_fraction
        )
        raw_size = monetary_risk / (stop_pips * candidate.quote.pip_value_account_currency)
        size = floor(raw_size * 100.0) / 100.0
        if not isfinite(size) or size < candidate.quote.minimum_size:
            return _risk_block("POSITION_SIZE_BELOW_MINIMUM")
        return RiskDecision(
            True,
            "ALLOWED",
            account.balance,
            daily_loss,
            len(account.positions),
            len(account.positions) + 1,
            executions_in_cycle,
            monetary_risk,
            size,
            stop_pips,
            stop_pips * self.config.reward_to_risk,
        )


def _risk_block(code: str) -> RiskDecision:
    return RiskDecision(False, code, None, None, None, None, None, None, None, None, None)


def _encode(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
