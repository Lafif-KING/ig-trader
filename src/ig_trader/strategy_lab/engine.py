"""Deterministic candle-driven Strategy Lab backtesting and qualification."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from math import sqrt
from statistics import median

from src.ig_trader.strategy_lab.data import CanonicalDataset, LabCandle
from src.ig_trader.strategy_lab.models import InstrumentSpec
from src.ig_trader.strategy_lab.strategies import Direction, ResearchStrategy, StrategyDefinition


class QualificationStatus(StrEnum):
    DATA_NOT_AVAILABLE = "DATA_NOT_AVAILABLE"
    DATA_QUALITY_FAIL = "DATA_QUALITY_FAIL"
    COST_MODEL_INCOMPLETE = "COST_MODEL_INCOMPLETE"
    INSUFFICIENT_TRADES = "INSUFFICIENT_TRADES"
    LOW_SAMPLE_CONFIDENCE = "LOW_SAMPLE_CONFIDENCE"
    NEGATIVE_EXPECTANCY = "NEGATIVE_EXPECTANCY"
    OVERFIT_RISK = "OVERFIT_RISK"
    UNSTABLE_ACROSS_PERIODS = "UNSTABLE_ACROSS_PERIODS"
    RESEARCH_REJECTED = "RESEARCH_REJECTED"
    RESEARCH_WATCH = "RESEARCH_WATCH"
    CHALLENGER = "CHALLENGER"
    CHAMPION_CANDIDATE = "CHAMPION_CANDIDATE"
    READY_FOR_DEMO_QUALIFICATION = "READY_FOR_DEMO_QUALIFICATION"


@dataclass(frozen=True)
class FrictionModel:
    """Explicit, instrument-specific price-distance friction assumptions.

    Commission is a price-distance-equivalent supplied by the data/research
    configuration.  It is never inferred from a generic FX pip convention.
    """

    tick_size: Decimal | None
    typical_spread: Decimal | None
    slippage: Decimal | None
    commission_price_equivalent: Decimal | None
    minimum_stop_distance: Decimal | None
    minimum_size: Decimal | None
    allowed_utc_hours: frozenset[int] | None = None

    @property
    def complete(self) -> bool:
        positive = (self.tick_size, self.minimum_stop_distance, self.minimum_size)
        nonnegative = (
            self.typical_spread,
            self.slippage,
            self.commission_price_equivalent,
        )
        return (
            self.allowed_utc_hours is not None
            and bool(self.allowed_utc_hours)
            and all(0 <= hour <= 23 for hour in self.allowed_utc_hours)
            and all(value is not None and value > 0 for value in positive)
            and all(value is not None and value >= 0 for value in nonnegative)
        )

    def is_market_open(self, timestamp_utc: datetime) -> bool:
        """Use explicit configured UTC trading hours; unknown schedules fail closed."""

        return self.allowed_utc_hours is not None and timestamp_utc.hour in self.allowed_utc_hours

    @classmethod
    def from_instrument(cls, instrument: InstrumentSpec) -> FrictionModel:
        return cls(
            tick_size=instrument.pip_or_tick_size,
            typical_spread=(
                instrument.spread_statistics.median if instrument.spread_statistics else None
            ),
            slippage=None,
            commission_price_equivalent=None,
            minimum_stop_distance=instrument.minimum_stop_distance,
            minimum_size=instrument.minimum_deal_size,
            allowed_utc_hours=None,
        )


@dataclass(frozen=True)
class BacktestConfig:
    reward_to_risk: Decimal = Decimal("1.5")
    seed: int = 0

    def __post_init__(self) -> None:
        if self.reward_to_risk <= 0:
            raise ValueError("reward-to-risk must be positive")

    @property
    def fingerprint(self) -> str:
        return _fingerprint(asdict(self))


DEFAULT_BACKTEST_CONFIG = BacktestConfig()


@dataclass(frozen=True)
class Trade:
    instrument: str
    direction: Direction
    entry_timestamp_utc: datetime
    exit_timestamp_utc: datetime
    entry_price: Decimal
    exit_price: Decimal
    stop_distance: Decimal
    r_multiple: Decimal
    spread_cost: Decimal
    slippage_cost: Decimal
    commission_cost: Decimal
    exit_reason: str

    @property
    def duration_seconds(self) -> float:
        return (self.exit_timestamp_utc - self.entry_timestamp_utc).total_seconds()


@dataclass(frozen=True)
class PerformanceMetrics:
    trade_count: int
    wins: int
    losses: int
    win_rate: float | None
    net_pips_or_ticks: Decimal
    net_r: Decimal
    average_r: Decimal | None
    median_r: Decimal | None
    expectancy: Decimal | None
    profit_factor: Decimal | None
    maximum_drawdown_r: Decimal
    maximum_losing_streak: int
    average_trade_duration_seconds: float | None
    exposure_seconds: float
    spread_cost: Decimal
    slippage_cost: Decimal
    commission_cost: Decimal
    by_session: dict[str, Decimal]
    by_weekday: dict[str, Decimal]
    by_volatility_regime: dict[str, Decimal]
    by_month: dict[str, Decimal]


@dataclass(frozen=True)
class BacktestResult:
    strategy: StrategyDefinition
    dataset_fingerprint: str
    configuration_fingerprint: str
    trades: tuple[Trade, ...]
    metrics: PerformanceMetrics
    status: QualificationStatus
    status_reasons: tuple[str, ...]


class CandleBacktestEngine:
    """No-look-ahead engine using next-candle entries and conservative exits."""

    def run(
        self,
        dataset: CanonicalDataset,
        strategy: ResearchStrategy,
        friction: FrictionModel,
        config: BacktestConfig = DEFAULT_BACKTEST_CONFIG,
    ) -> BacktestResult:
        if dataset.has_quality_failure:
            return self._empty(
                dataset, strategy.definition, config, QualificationStatus.DATA_QUALITY_FAIL
            )
        if not friction.complete:
            return self._empty(
                dataset, strategy.definition, config, QualificationStatus.COST_MODEL_INCOMPLETE
            )
        assert friction.minimum_stop_distance is not None
        trades: list[Trade] = []
        index = 21
        candles = dataset.candles
        while index < len(candles) - 1:
            signal = strategy.signal(candles[: index + 1])
            if signal is None:
                index += 1
                continue
            if not friction.is_market_open(candles[index + 1].timestamp_utc):
                index += 1
                continue
            stop_distance = max(signal.stop_distance, friction.minimum_stop_distance)
            trade, exit_index = self._simulate_trade(
                candles,
                index + 1,
                signal.direction,
                stop_distance,
                config.reward_to_risk,
                friction,
            )
            trades.append(trade)
            index = exit_index + 1
        metrics = calculate_metrics(trades, friction.tick_size or Decimal("1"))
        return BacktestResult(
            strategy=strategy.definition,
            dataset_fingerprint=dataset.dataset_fingerprint,
            configuration_fingerprint=config.fingerprint,
            trades=tuple(trades),
            metrics=metrics,
            status=QualificationStatus.RESEARCH_WATCH,
            status_reasons=(),
        )

    def _empty(
        self,
        dataset: CanonicalDataset,
        strategy: StrategyDefinition,
        config: BacktestConfig,
        status: QualificationStatus,
    ) -> BacktestResult:
        return BacktestResult(
            strategy=strategy,
            dataset_fingerprint=dataset.dataset_fingerprint,
            configuration_fingerprint=config.fingerprint,
            trades=(),
            metrics=calculate_metrics((), Decimal("1")),
            status=status,
            status_reasons=(status.value,),
        )

    def _simulate_trade(
        self,
        candles: tuple[LabCandle, ...],
        entry_index: int,
        direction: Direction,
        stop_distance: Decimal,
        reward_to_risk: Decimal,
        friction: FrictionModel,
    ) -> tuple[Trade, int]:
        entry_candle = candles[entry_index]
        entry = entry_candle.open
        target_distance = stop_distance * reward_to_risk
        if direction is Direction.LONG:
            stop, target = entry - stop_distance, entry + target_distance
        else:
            stop, target = entry + stop_distance, entry - target_distance
        for index in range(entry_index, len(candles)):
            candle = candles[index]
            stop_hit = candle.low <= stop if direction is Direction.LONG else candle.high >= stop
            target_hit = (
                candle.high >= target if direction is Direction.LONG else candle.low <= target
            )
            # Same-candle ambiguity is deliberately pessimistic: the stop wins.
            if stop_hit:
                return self._trade(
                    entry_candle, candle, direction, entry, stop, stop_distance, friction, "STOP"
                ), index
            if target_hit:
                return self._trade(
                    entry_candle,
                    candle,
                    direction,
                    entry,
                    target,
                    stop_distance,
                    friction,
                    "TARGET",
                ), index
        final = candles[-1]
        return self._trade(
            entry_candle,
            final,
            direction,
            entry,
            final.close,
            stop_distance,
            friction,
            "END_OF_DATA",
        ), len(candles) - 1

    @staticmethod
    def _trade(
        entry_candle: LabCandle,
        exit_candle: LabCandle,
        direction: Direction,
        entry: Decimal,
        exit_price: Decimal,
        stop_distance: Decimal,
        friction: FrictionModel,
        reason: str,
    ) -> Trade:
        assert friction.typical_spread is not None
        assert friction.slippage is not None
        assert friction.commission_price_equivalent is not None
        price_move = exit_price - entry if direction is Direction.LONG else entry - exit_price
        gross_r = price_move / stop_distance
        spread_r = friction.typical_spread / stop_distance
        slippage_r = friction.slippage / stop_distance
        commission_r = friction.commission_price_equivalent / stop_distance
        return Trade(
            instrument=entry_candle.instrument,
            direction=direction,
            entry_timestamp_utc=entry_candle.timestamp_utc,
            exit_timestamp_utc=exit_candle.timestamp_utc,
            entry_price=entry,
            exit_price=exit_price,
            stop_distance=stop_distance,
            r_multiple=gross_r - spread_r - slippage_r - commission_r,
            spread_cost=spread_r,
            slippage_cost=slippage_r,
            commission_cost=commission_r,
            exit_reason=reason,
        )


def calculate_metrics(trades: Iterable[Trade], tick_size: Decimal) -> PerformanceMetrics:
    """Compute R-based metrics without assuming a generic instrument pip size."""

    items = tuple(trades)
    values = tuple(item.r_multiple for item in items)
    wins = sum(value > 0 for value in values)
    losses = sum(value <= 0 for value in values)
    net_r = sum(values, Decimal("0"))
    positive = sum((value for value in values if value > 0), Decimal("0"))
    negative = -sum((value for value in values if value < 0), Decimal("0"))
    equity = peak = Decimal("0")
    drawdown = Decimal("0")
    losing_streak = maximum_losing_streak = 0
    for value in values:
        equity += value
        peak = max(peak, equity)
        drawdown = max(drawdown, peak - equity)
        losing_streak = losing_streak + 1 if value <= 0 else 0
        maximum_losing_streak = max(maximum_losing_streak, losing_streak)
    net_ticks = sum(
        ((item.exit_price - item.entry_price) / tick_size)
        * (Decimal("1") if item.direction is Direction.LONG else Decimal("-1"))
        for item in items
    )
    return PerformanceMetrics(
        trade_count=len(items),
        wins=wins,
        losses=losses,
        win_rate=(wins / len(items)) if items else None,
        net_pips_or_ticks=net_ticks,
        net_r=net_r,
        average_r=(net_r / len(items)) if items else None,
        median_r=Decimal(str(median(values))) if values else None,
        expectancy=(net_r / len(items)) if items else None,
        profit_factor=(positive / negative)
        if negative > 0
        else (None if not positive else Decimal("Infinity")),
        maximum_drawdown_r=drawdown,
        maximum_losing_streak=maximum_losing_streak,
        average_trade_duration_seconds=(
            sum(item.duration_seconds for item in items) / len(items) if items else None
        ),
        exposure_seconds=sum(item.duration_seconds for item in items),
        spread_cost=sum((item.spread_cost for item in items), Decimal("0")),
        slippage_cost=sum((item.slippage_cost for item in items), Decimal("0")),
        commission_cost=sum((item.commission_cost for item in items), Decimal("0")),
        by_session=_aggregate(items, _session),
        by_weekday=_aggregate(items, lambda item: item.entry_timestamp_utc.strftime("%A")),
        by_volatility_regime=_volatility_regimes(items),
        by_month=_aggregate(items, lambda item: item.entry_timestamp_utc.strftime("%Y-%m")),
    )


def _aggregate(trades: tuple[Trade, ...], key) -> dict[str, Decimal]:
    result: dict[str, Decimal] = {}
    for trade in trades:
        label = key(trade)
        result[label] = result.get(label, Decimal("0")) + trade.r_multiple
    return result


def _session(trade: Trade) -> str:
    hour = trade.entry_timestamp_utc.hour
    if 0 <= hour < 7:
        return "ASIAN"
    if hour < 13:
        return "LONDON"
    if hour < 21:
        return "NEW_YORK"
    return "OFF_SESSION"


def _volatility_regimes(trades: tuple[Trade, ...]) -> dict[str, Decimal]:
    """Bucket realized trades by their observed stop-distance volatility proxy."""

    if not trades:
        return {"NOT_AVAILABLE": Decimal("0")}
    center = Decimal(str(median(item.stop_distance for item in trades)))
    result: dict[str, Decimal] = {}
    for trade in trades:
        if trade.stop_distance < center * Decimal("0.80"):
            label = "LOW"
        elif trade.stop_distance > center * Decimal("1.20"):
            label = "HIGH"
        else:
            label = "NORMAL"
        result[label] = result.get(label, Decimal("0")) + trade.r_multiple
    return result


@dataclass(frozen=True)
class SplitConfig:
    development_fraction: Decimal = Decimal("0.60")
    validation_fraction: Decimal = Decimal("0.20")
    release_untouched_test: bool = False

    def __post_init__(self) -> None:
        if self.development_fraction <= 0 or self.validation_fraction <= 0:
            raise ValueError("split fractions must be positive")
        if self.development_fraction + self.validation_fraction >= 1:
            raise ValueError("test split must remain non-empty")


DEFAULT_SPLIT_CONFIG = SplitConfig()


@dataclass(frozen=True)
class ChronologicalSplits:
    development: CanonicalDataset
    validation: CanonicalDataset
    untouched_test: CanonicalDataset
    test_released: bool


def chronological_splits(
    dataset: CanonicalDataset, config: SplitConfig = DEFAULT_SPLIT_CONFIG
) -> ChronologicalSplits:
    """Split chronologically; callers must opt in before evaluating final test data."""

    total = len(dataset.candles)
    development_end = int(total * config.development_fraction)
    validation_end = development_end + int(total * config.validation_fraction)
    if development_end < 1 or validation_end <= development_end or validation_end >= total:
        raise ValueError("dataset is too short for chronological train/validation/test splits")
    return ChronologicalSplits(
        _subset(dataset, dataset.candles[:development_end]),
        _subset(dataset, dataset.candles[development_end:validation_end]),
        _subset(dataset, dataset.candles[validation_end:]),
        config.release_untouched_test,
    )


@dataclass(frozen=True)
class WalkForwardWindow:
    development: CanonicalDataset
    validation: CanonicalDataset


def walk_forward_windows(
    dataset: CanonicalDataset, *, development_size: int, validation_size: int, step: int
) -> tuple[WalkForwardWindow, ...]:
    """Return chronological walk-forward windows without future leakage."""

    if min(development_size, validation_size, step) <= 0:
        raise ValueError("walk-forward sizes must be positive")
    windows = []
    start = 0
    while start + development_size + validation_size <= len(dataset.candles):
        development = dataset.candles[start : start + development_size]
        validation = dataset.candles[
            start + development_size : start + development_size + validation_size
        ]
        windows.append(
            WalkForwardWindow(_subset(dataset, development), _subset(dataset, validation))
        )
        start += step
    return tuple(windows)


@dataclass(frozen=True)
class ParameterEvaluation:
    parameters: tuple[tuple[str, str], ...]
    validation_expectancy: Decimal
    test_expectancy: Decimal
    trade_count: int


def detect_overfit(evaluations: Iterable[ParameterEvaluation]) -> bool:
    """Flag isolated peaks or a validation-to-test collapse, never optimise S0."""

    values = tuple(evaluations)
    if len(values) < 3:
        return False
    validation = sorted(item.validation_expectancy for item in values)
    best = validation[-1]
    neighbouring_median = Decimal(str(median(validation[:-1])))
    if best > 0 and neighbouring_median <= 0:
        return True
    return any(item.validation_expectancy > 0 and item.test_expectancy <= 0 for item in values)


@dataclass(frozen=True)
class QualificationThresholds:
    minimum_total_trades: int = 100
    minimum_oos_trades: int = 30
    maximum_drawdown_r: Decimal = Decimal("10")
    minimum_profit_factor: Decimal = Decimal("1.10")
    maximum_validation_to_test_degradation: Decimal = Decimal("0.50")


DEFAULT_QUALIFICATION_THRESHOLDS = QualificationThresholds()


def classify_result(
    result: BacktestResult,
    *,
    validation: PerformanceMetrics | None = None,
    out_of_sample: PerformanceMetrics | None = None,
    overfit_risk: bool = False,
    thresholds: QualificationThresholds = DEFAULT_QUALIFICATION_THRESHOLDS,
) -> QualificationStatus:
    """Conservative status ordering; profitable PnL alone never promotes a result."""

    if result.status in {
        QualificationStatus.DATA_QUALITY_FAIL,
        QualificationStatus.COST_MODEL_INCOMPLETE,
        QualificationStatus.DATA_NOT_AVAILABLE,
    }:
        return result.status
    metrics = result.metrics
    if metrics.trade_count < 30:
        return QualificationStatus.INSUFFICIENT_TRADES
    if overfit_risk:
        return QualificationStatus.OVERFIT_RISK
    if out_of_sample is None or out_of_sample.trade_count < thresholds.minimum_oos_trades:
        return QualificationStatus.LOW_SAMPLE_CONFIDENCE
    if out_of_sample.expectancy is None or out_of_sample.expectancy <= 0:
        return QualificationStatus.NEGATIVE_EXPECTANCY
    if validation is not None and validation.expectancy and validation.expectancy > 0:
        degradation = (validation.expectancy - out_of_sample.expectancy) / validation.expectancy
        if degradation > thresholds.maximum_validation_to_test_degradation:
            return QualificationStatus.UNSTABLE_ACROSS_PERIODS
    if metrics.trade_count < thresholds.minimum_total_trades:
        return QualificationStatus.LOW_SAMPLE_CONFIDENCE
    if (
        metrics.maximum_drawdown_r > thresholds.maximum_drawdown_r
        or metrics.profit_factor is None
        or metrics.profit_factor < thresholds.minimum_profit_factor
    ):
        return QualificationStatus.RESEARCH_REJECTED
    # This is evidence only. Human governance is required before any Demo work.
    return QualificationStatus.CHAMPION_CANDIDATE


@dataclass(frozen=True)
class StrategyVersion:
    strategy_id: str
    instrument: str
    family: str
    version: str
    parameters: tuple[tuple[str, str], ...]
    configuration_fingerprint: str
    parent_version: str | None
    change_reason: str
    development_dataset_fingerprint: str
    validation_result_fingerprint: str | None
    test_result_fingerprint: str | None
    status: QualificationStatus
    frozen_for_test: bool = False


class VersionRegistry:
    """Append-only in-memory version registry; duplicate identities are rejected."""

    def __init__(self) -> None:
        self._versions: dict[tuple[str, str, str], StrategyVersion] = {}

    def add(self, version: StrategyVersion) -> None:
        key = (version.instrument, version.strategy_id, version.version)
        if key in self._versions:
            raise ValueError("strategy version cannot be silently overwritten")
        self._versions[key] = version

    def values(self) -> tuple[StrategyVersion, ...]:
        return tuple(self._versions.values())


@dataclass(frozen=True)
class ChampionChallengerComparison:
    instrument: str
    champion_version: str | None
    challenger_version: str
    outcome: str
    reason: str


def compare_challenger(
    champion: BacktestResult | None, challenger: BacktestResult
) -> ChampionChallengerComparison:
    """A challenger only wins on stronger evidence, not a single higher PnL."""

    if champion is None:
        return ChampionChallengerComparison(
            challenger.trades[0].instrument if challenger.trades else "UNKNOWN",
            None,
            challenger.strategy.version,
            "CHALLENGER_RETAINED",
            "No frozen champion exists.",
        )
    better = (
        challenger.metrics.expectancy is not None
        and champion.metrics.expectancy is not None
        and challenger.metrics.expectancy > champion.metrics.expectancy
        and challenger.metrics.maximum_drawdown_r <= champion.metrics.maximum_drawdown_r
        and challenger.metrics.trade_count >= champion.metrics.trade_count
    )
    return ChampionChallengerComparison(
        champion.trades[0].instrument if champion.trades else "UNKNOWN",
        champion.strategy.version,
        challenger.strategy.version,
        "CHALLENGER_PROMOTED" if better else "CHALLENGER_RETAINED",
        "Stronger expectancy, drawdown, and sample evidence required for promotion.",
    )


@dataclass(frozen=True)
class PortfolioAnalysis:
    correlations: dict[str, float | None]
    currency_concentration: dict[str, int]
    asset_class_concentration: dict[str, int]
    simultaneous_loss_count: int
    diversification_score: float


def analyse_portfolio(
    named_returns: dict[str, tuple[Decimal, ...]],
    instruments: dict[str, InstrumentSpec],
) -> PortfolioAnalysis:
    """Assess correlations and shared currency/asset exposure before a portfolio claim."""

    correlations: dict[str, float | None] = {}
    names = tuple(named_returns)
    for position, left_name in enumerate(names):
        for right_name in names[position + 1 :]:
            correlations[f"{left_name}|{right_name}"] = _correlation(
                named_returns[left_name], named_returns[right_name]
            )
    currencies: dict[str, int] = {}
    asset_classes: dict[str, int] = {}
    for symbol, instrument in instruments.items():
        asset_classes[instrument.asset_class.value] = (
            asset_classes.get(instrument.asset_class.value, 0) + 1
        )
        if instrument.asset_class.value == "FX" and len(symbol) == 6:
            for currency in (symbol[:3], symbol[3:]):
                currencies[currency] = currencies.get(currency, 0) + 1
    shared_losses = 0
    for index in range(max((len(values) for values in named_returns.values()), default=0)):
        active_returns = [values[index] for values in named_returns.values() if index < len(values)]
        if len(active_returns) >= 2 and all(value < 0 for value in active_returns):
            shared_losses += 1
    average_abs_correlation = sum(
        abs(value) for value in correlations.values() if value is not None
    ) / max(1, sum(value is not None for value in correlations.values()))
    concentration_penalty = max(currencies.values(), default=0) + max(
        asset_classes.values(), default=0
    )
    score = max(
        0.0, min(100.0, 100.0 - 50.0 * average_abs_correlation - 4.0 * concentration_penalty)
    )
    return PortfolioAnalysis(correlations, currencies, asset_classes, shared_losses, score)


def _subset(dataset: CanonicalDataset, candles: tuple[LabCandle, ...]) -> CanonicalDataset:
    from src.ig_trader.strategy_lab.data import build_dataset

    return build_dataset(candles, source_documents=(dataset.dataset_fingerprint,))


def _correlation(left: tuple[Decimal, ...], right: tuple[Decimal, ...]) -> float | None:
    length = min(len(left), len(right))
    if length < 2:
        return None
    left_values = [float(value) for value in left[:length]]
    right_values = [float(value) for value in right[:length]]
    left_mean = sum(left_values) / length
    right_mean = sum(right_values) / length
    numerator = sum(
        (a - left_mean) * (b - right_mean) for a, b in zip(left_values, right_values, strict=True)
    )
    denominator = sqrt(sum((a - left_mean) ** 2 for a in left_values)) * sqrt(
        sum((b - right_mean) ** 2 for b in right_values)
    )
    return numerator / denominator if denominator else None


def _fingerprint(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()
