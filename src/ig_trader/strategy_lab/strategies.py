"""Deterministic, rule-based candidate strategy families for research only."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from decimal import Decimal
from enum import StrEnum
from statistics import mean, pstdev
from typing import Protocol

from src.ig_trader.strategy_lab.data import LabCandle
from src.ig_trader.strategy_lab.models import AssetClass, StrategyFamily, Timeframe


class Direction(StrEnum):
    LONG = "LONG"
    SHORT = "SHORT"


@dataclass(frozen=True)
class ResearchSignal:
    """A strategy decision made at the close of one observed candle."""

    direction: Direction
    stop_distance: Decimal
    rationale: str

    def __post_init__(self) -> None:
        if self.stop_distance <= 0:
            raise ValueError("strategy stop distance must be positive")


class ResearchStrategy(Protocol):
    """A no-look-ahead strategy interface.

    ``history`` ends at the decision candle.  The engine only accepts the
    resulting position on the following candle, so implementations never see
    entry or exit data while deciding.
    """

    definition: StrategyDefinition

    def signal(self, history: tuple[LabCandle, ...]) -> ResearchSignal | None: ...


@dataclass(frozen=True)
class StrategyDefinition:
    strategy_id: str
    family: StrategyFamily
    version: str
    compatible_assets: tuple[AssetClass, ...]
    compatible_timeframes: tuple[Timeframe, ...]
    parameters: tuple[tuple[str, str], ...] = ()
    parent_version: str | None = None
    change_reason: str = "Initial deterministic research candidate."
    baseline_only: bool = False

    @property
    def configuration_fingerprint(self) -> str:
        document = asdict(self)
        return hashlib.sha256(
            json.dumps(document, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
        ).hexdigest()


@dataclass(frozen=True)
class RuleStrategy:
    definition: StrategyDefinition
    _rule: str = field(repr=False)

    def signal(self, history: tuple[LabCandle, ...]) -> ResearchSignal | None:
        """Evaluate only the supplied closed-candle history."""

        if len(history) < 22:
            return None
        closes = [float(candle.close) for candle in history]
        atr = _atr(history[-15:])
        if atr <= 0:
            return None
        latest = history[-1]
        previous = history[:-1]
        if self._rule == "frozen_rsi_adx_reference":
            rsi = _rsi(closes[-8:])
            trend = abs(closes[-1] - closes[-8]) / atr
            if rsi < 30 and trend >= 1:
                return _signal(Direction.LONG, atr, "Frozen V1 baseline RSI/ADX reference")
            if rsi > 70 and trend >= 1:
                return _signal(Direction.SHORT, atr, "Frozen V1 baseline RSI/ADX reference")
        elif self._rule == "trend":
            short, long = mean(closes[-8:]), mean(closes[-21:])
            if short > long and latest.close > latest.open:
                return _signal(Direction.LONG, atr, "EMA-structure trend proxy")
            if short < long and latest.close < latest.open:
                return _signal(Direction.SHORT, atr, "EMA-structure trend proxy")
        elif self._rule == "breakout":
            upper = max(float(candle.high) for candle in previous[-20:])
            lower = min(float(candle.low) for candle in previous[-20:])
            if float(latest.close) > upper:
                return _signal(Direction.LONG, atr, "Donchian-style breakout")
            if float(latest.close) < lower:
                return _signal(Direction.SHORT, atr, "Donchian-style breakout")
        elif self._rule == "mean_reversion":
            baseline = mean(closes[-21:-1])
            deviation = pstdev(closes[-21:-1])
            if deviation > 0 and closes[-1] < baseline - 1.5 * deviation:
                return _signal(Direction.LONG, atr, "volatility-normalized return-to-mean")
            if deviation > 0 and closes[-1] > baseline + 1.5 * deviation:
                return _signal(Direction.SHORT, atr, "volatility-normalized return-to-mean")
        elif self._rule == "session_sweep":
            range_high = max(float(candle.high) for candle in previous[-12:])
            range_low = min(float(candle.low) for candle in previous[-12:])
            if float(latest.low) < range_low and float(latest.close) > range_low:
                return _signal(Direction.LONG, atr, "range liquidity sweep reversal")
            if float(latest.high) > range_high and float(latest.close) < range_high:
                return _signal(Direction.SHORT, atr, "range liquidity sweep reversal")
        elif self._rule == "volatility_regime":
            recent = _atr(history[-6:])
            normal = _atr(history[-21:-1])
            if normal > 0 and recent > normal * 1.25:
                return _signal(
                    Direction.LONG if latest.close > latest.open else Direction.SHORT,
                    atr,
                    "high-volatility expansion",
                )
        elif self._rule == "structure":
            swing_high = max(float(candle.high) for candle in previous[-10:])
            swing_low = min(float(candle.low) for candle in previous[-10:])
            displacement = abs(float(latest.close) - float(latest.open)) >= atr
            if displacement and float(latest.close) > swing_high:
                return _signal(Direction.LONG, atr, "rule-based break of structure")
            if displacement and float(latest.close) < swing_low:
                return _signal(Direction.SHORT, atr, "rule-based break of structure")
        elif self._rule == "multi_timeframe":
            context = mean(closes[-21:])
            trigger = mean(closes[-5:])
            if trigger > context and latest.close > latest.open:
                return _signal(Direction.LONG, atr, "higher-context trend plus trigger")
            if trigger < context and latest.close < latest.open:
                return _signal(Direction.SHORT, atr, "higher-context trend plus trigger")
        return None


def _signal(direction: Direction, atr: float, rationale: str) -> ResearchSignal:
    return ResearchSignal(direction, Decimal(str(atr * 1.5)), rationale)


def _atr(candles: tuple[LabCandle, ...] | list[LabCandle]) -> float:
    if len(candles) < 2:
        return 0.0
    values = []
    for previous, current in zip(candles, candles[1:], strict=False):
        values.append(
            max(
                float(current.high - current.low),
                abs(float(current.high - previous.close)),
                abs(float(current.low - previous.close)),
            )
        )
    return mean(values) if values else 0.0


def _rsi(closes: list[float]) -> float:
    changes = [right - left for left, right in zip(closes, closes[1:], strict=False)]
    gains = [max(0.0, item) for item in changes]
    losses = [max(0.0, -item) for item in changes]
    average_gain = mean(gains) if gains else 0.0
    average_loss = mean(losses) if losses else 0.0
    if average_loss == 0:
        return 100.0 if average_gain else 50.0
    return 100.0 - (100.0 / (1.0 + average_gain / average_loss))


def strategy_registry() -> dict[str, RuleStrategy]:
    """The candidate families deliberately stay deterministic and versioned."""

    all_assets = (AssetClass.FX, AssetClass.METAL, AssetClass.INDEX, AssetClass.ENERGY)
    definitions = (
        ("S0", StrategyFamily.S0_FROZEN_RSI_ADX, "frozen_rsi_adx_reference", True),
        ("S1", StrategyFamily.S1_TREND_MOMENTUM, "trend", False),
        ("S2", StrategyFamily.S2_BREAKOUT, "breakout", False),
        ("S3", StrategyFamily.S3_MEAN_REVERSION, "mean_reversion", False),
        ("S4", StrategyFamily.S4_SESSION_SWEEP, "session_sweep", False),
        ("S5", StrategyFamily.S5_VOLATILITY_REGIME, "volatility_regime", False),
        ("S6", StrategyFamily.S6_PRICE_STRUCTURE, "structure", False),
        ("S7", StrategyFamily.S7_MULTI_TIMEFRAME_TREND, "multi_timeframe", False),
    )
    return {
        strategy_id: RuleStrategy(
            StrategyDefinition(
                strategy_id=strategy_id,
                family=family,
                version="frozen-v1-reference" if baseline else "1.0.0",
                compatible_assets=(AssetClass.FX,) if baseline else all_assets,
                compatible_timeframes=(Timeframe.M5, Timeframe.M1)
                if baseline
                else tuple(Timeframe),
                parameters=(("rule", rule),),
                change_reason=(
                    "Frozen V1 benchmark only; parameter optimization is prohibited."
                    if baseline
                    else "Initial deterministic candidate."
                ),
                baseline_only=baseline,
            ),
            rule,
        )
        for strategy_id, family, rule, baseline in definitions
    }
