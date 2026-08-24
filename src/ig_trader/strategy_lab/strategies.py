"""Deterministic, rule-based candidate strategy families for research only."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field, replace
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

        parameters = dict(self.definition.parameters)
        if len(history) < _minimum_history(self._rule, parameters):
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
            short_period = _integer_parameter(parameters, "fast", 8)
            long_period = _integer_parameter(parameters, "slow", 21)
            short, long = mean(closes[-short_period:]), mean(closes[-long_period:])
            if short > long and latest.close > latest.open:
                return _signal(Direction.LONG, atr, "EMA-structure trend proxy")
            if short < long and latest.close < latest.open:
                return _signal(Direction.SHORT, atr, "EMA-structure trend proxy")
        elif self._rule == "breakout":
            lookback = _integer_parameter(parameters, "lookback", 20)
            upper = max(float(candle.high) for candle in previous[-lookback:])
            lower = min(float(candle.low) for candle in previous[-lookback:])
            if float(latest.close) > upper:
                return _signal(Direction.LONG, atr, "Donchian-style breakout")
            if float(latest.close) < lower:
                return _signal(Direction.SHORT, atr, "Donchian-style breakout")
        elif self._rule == "mean_reversion":
            lookback = _integer_parameter(parameters, "lookback", 20)
            threshold = _float_parameter(parameters, "deviation", 1.5)
            baseline = mean(closes[-(lookback + 1) : -1])
            deviation = pstdev(closes[-(lookback + 1) : -1])
            if deviation > 0 and closes[-1] < baseline - threshold * deviation:
                return _signal(Direction.LONG, atr, "volatility-normalized return-to-mean")
            if deviation > 0 and closes[-1] > baseline + threshold * deviation:
                return _signal(Direction.SHORT, atr, "volatility-normalized return-to-mean")
        elif self._rule == "session_sweep":
            lookback = _integer_parameter(parameters, "lookback", 12)
            range_high = max(float(candle.high) for candle in previous[-lookback:])
            range_low = min(float(candle.low) for candle in previous[-lookback:])
            if float(latest.low) < range_low and float(latest.close) > range_low:
                return _signal(Direction.LONG, atr, "range liquidity sweep reversal")
            if float(latest.high) > range_high and float(latest.close) < range_high:
                return _signal(Direction.SHORT, atr, "range liquidity sweep reversal")
        elif self._rule == "volatility_regime":
            recent = _atr(history[-6:])
            normal_period = _integer_parameter(parameters, "normal_period", 20)
            expansion = _float_parameter(parameters, "expansion", 1.25)
            normal = _atr(history[-(normal_period + 1) : -1])
            if normal > 0 and recent > normal * expansion:
                return _signal(
                    Direction.LONG if latest.close > latest.open else Direction.SHORT,
                    atr,
                    "high-volatility expansion",
                )
        elif self._rule == "structure":
            lookback = _integer_parameter(parameters, "lookback", 10)
            displacement_multiple = _float_parameter(parameters, "displacement", 1.0)
            swing_high = max(float(candle.high) for candle in previous[-lookback:])
            swing_low = min(float(candle.low) for candle in previous[-lookback:])
            displacement = (
                abs(float(latest.close) - float(latest.open)) >= atr * displacement_multiple
            )
            if displacement and float(latest.close) > swing_high:
                return _signal(Direction.LONG, atr, "rule-based break of structure")
            if displacement and float(latest.close) < swing_low:
                return _signal(Direction.SHORT, atr, "rule-based break of structure")
        elif self._rule == "multi_timeframe":
            context_period = _integer_parameter(parameters, "context", 21)
            trigger_period = _integer_parameter(parameters, "trigger", 5)
            context = mean(closes[-context_period:])
            trigger = mean(closes[-trigger_period:])
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


def _integer_parameter(parameters: dict[str, str], name: str, default: int) -> int:
    try:
        value = int(parameters.get(name, str(default)))
    except ValueError:
        return default
    return value if value > 0 else default


def _float_parameter(parameters: dict[str, str], name: str, default: float) -> float:
    try:
        value = float(parameters.get(name, str(default)))
    except ValueError:
        return default
    return value if value > 0 else default


def _minimum_history(rule: str, parameters: dict[str, str]) -> int:
    """Prevent a bounded challenger variation from using a shortened first window."""

    requirements = {
        "trend": _integer_parameter(parameters, "slow", 21),
        "breakout": _integer_parameter(parameters, "lookback", 20) + 1,
        "mean_reversion": _integer_parameter(parameters, "lookback", 20) + 1,
        "session_sweep": _integer_parameter(parameters, "lookback", 12) + 1,
        "volatility_regime": _integer_parameter(parameters, "normal_period", 20) + 1,
        "structure": _integer_parameter(parameters, "lookback", 10) + 1,
        "multi_timeframe": _integer_parameter(parameters, "context", 21),
    }
    return max(22, requirements.get(rule, 22))


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


_BOUNDED_CHALLENGER_PARAMETERS: dict[str, tuple[tuple[tuple[str, str], ...], ...]] = {
    "S1": (
        (("fast", "8"), ("slow", "21")),
        (("fast", "10"), ("slow", "30")),
        (("fast", "13"), ("slow", "34")),
    ),
    "S2": ((("lookback", "15"),), (("lookback", "20"),), (("lookback", "30"),)),
    "S3": (
        (("lookback", "20"), ("deviation", "1.25")),
        (("lookback", "20"), ("deviation", "1.50")),
        (("lookback", "30"), ("deviation", "1.75")),
    ),
    "S4": ((("lookback", "8"),), (("lookback", "12"),), (("lookback", "16"),)),
    "S5": (
        (("normal_period", "20"), ("expansion", "1.15")),
        (("normal_period", "20"), ("expansion", "1.25")),
        (("normal_period", "30"), ("expansion", "1.35")),
    ),
    "S6": (
        (("lookback", "8"), ("displacement", "0.8")),
        (("lookback", "10"), ("displacement", "1.0")),
        (("lookback", "14"), ("displacement", "1.2")),
    ),
    "S7": (
        (("context", "21"), ("trigger", "5")),
        (("context", "30"), ("trigger", "8")),
        (("context", "34"), ("trigger", "10")),
    ),
}


def bounded_parameter_variants(strategy: RuleStrategy) -> tuple[RuleStrategy, ...]:
    """Return the small, reviewed SL-02 grids; S0 is intentionally unchanged."""

    if strategy.definition.baseline_only:
        return (strategy,)
    variants = _BOUNDED_CHALLENGER_PARAMETERS.get(strategy.definition.strategy_id)
    if variants is None:
        raise ValueError(f"no bounded parameter grid for {strategy.definition.strategy_id}")
    base_parameters = dict(strategy.definition.parameters)
    results: list[RuleStrategy] = []
    for number, values in enumerate(variants, start=1):
        merged = {**base_parameters, **dict(values)}
        definition = replace(
            strategy.definition,
            version=f"{strategy.definition.version}-sl02-p{number}",
            parameters=tuple(sorted(merged.items())),
            parent_version=strategy.definition.version,
            change_reason=(
                "SL-02 bounded challenger grid; selected only from chronological development "
                "and validation evidence."
            ),
        )
        results.append(RuleStrategy(definition, strategy._rule))
    return tuple(results)
