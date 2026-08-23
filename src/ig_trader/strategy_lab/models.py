"""Immutable contracts and the initial broker-neutral Strategy Lab registry.

Nothing in this module is an execution allowlist.  In particular, an ``epic``
is deliberately absent until a separate read-only discovery process verifies
it.  The registry is research metadata, not dealing metadata.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Final


class AssetClass(StrEnum):
    FX = "FX"
    METAL = "METAL"
    INDEX = "INDEX"
    ENERGY = "ENERGY"


class Timeframe(StrEnum):
    H4 = "4H"
    H1 = "1H"
    M15 = "15M"
    M5 = "5M"
    M1 = "1M"


class StrategyFamily(StrEnum):
    S0_FROZEN_RSI_ADX = "S0"
    S1_TREND_MOMENTUM = "S1"
    S2_BREAKOUT = "S2"
    S3_MEAN_REVERSION = "S3"
    S4_SESSION_SWEEP = "S4"
    S5_VOLATILITY_REGIME = "S5"
    S6_PRICE_STRUCTURE = "S6"
    S7_MULTI_TIMEFRAME_TREND = "S7"


class DataAvailability(StrEnum):
    UNKNOWN = "UNKNOWN"
    NOT_AVAILABLE = "NOT_AVAILABLE"
    AVAILABLE = "AVAILABLE"
    PARTIAL = "PARTIAL"


class ResearchStatus(StrEnum):
    HYPOTHESIS = "HYPOTHESIS"
    DATA_PENDING = "DATA_PENDING"
    RESEARCH_ONLY = "RESEARCH_ONLY"


class ExecutionStatus(StrEnum):
    RESEARCH_ONLY = "RESEARCH_ONLY"
    NOT_AN_EXECUTION_ALLOWLIST = "NOT_AN_EXECUTION_ALLOWLIST"


@dataclass(frozen=True)
class SpreadStatistics:
    """Observed research-data spread statistics; absent data stays ``None``."""

    median: Decimal | None = None
    percentile_95: Decimal | None = None
    sample_count: int = 0
    source_fingerprint: str | None = None


@dataclass(frozen=True)
class InstrumentSpec:
    """Canonical instrument identity with no invented broker values."""

    symbol: str
    asset_class: AssetClass
    display_name: str
    ig_epic: str | None = None
    expiry: str | None = None
    currency: str | None = None
    pip_or_tick_size: Decimal | None = None
    decimal_places: int | None = None
    minimum_deal_size: Decimal | None = None
    minimum_stop_distance: Decimal | None = None
    market_hours: str | None = None
    spread_statistics: SpreadStatistics | None = None
    data_availability: DataAvailability = DataAvailability.UNKNOWN
    research_status: ResearchStatus = ResearchStatus.HYPOTHESIS
    execution_status: ExecutionStatus = ExecutionStatus.NOT_AN_EXECUTION_ALLOWLIST

    def __post_init__(self) -> None:
        if not self.symbol.isascii() or not self.symbol.isupper() or not self.symbol.isalnum():
            raise ValueError("canonical symbol must be uppercase ASCII alphanumeric text")
        if not self.display_name.strip():
            raise ValueError("display name is required")
        if self.ig_epic is not None and not self.ig_epic.strip():
            raise ValueError("an EPIC must be verified text or remain unknown")
        for value in (self.pip_or_tick_size, self.minimum_deal_size, self.minimum_stop_distance):
            if value is not None and value <= 0:
                raise ValueError("numeric market metadata must be positive when verified")


@dataclass(frozen=True)
class SuitabilityHypothesis:
    """A testable hypothesis, not a claim about expected profitability."""

    instrument: str
    family: StrategyFamily
    rationale: str


ALL_TIMEFRAMES: Final[tuple[Timeframe, ...]] = (
    Timeframe.H4,
    Timeframe.H1,
    Timeframe.M15,
    Timeframe.M5,
    Timeframe.M1,
)


def _instrument(symbol: str, asset_class: AssetClass, display_name: str) -> InstrumentSpec:
    """Create a research-only registry item with all broker metadata unknown."""

    return InstrumentSpec(symbol=symbol, asset_class=asset_class, display_name=display_name)


INITIAL_INSTRUMENTS: Final[tuple[InstrumentSpec, ...]] = (
    _instrument("EURUSD", AssetClass.FX, "EUR/USD"),
    _instrument("GBPUSD", AssetClass.FX, "GBP/USD"),
    _instrument("EURGBP", AssetClass.FX, "EUR/GBP"),
    _instrument("USDJPY", AssetClass.FX, "USD/JPY"),
    _instrument("EURJPY", AssetClass.FX, "EUR/JPY"),
    _instrument("GBPJPY", AssetClass.FX, "GBP/JPY"),
    _instrument("AUDUSD", AssetClass.FX, "AUD/USD"),
    _instrument("NZDUSD", AssetClass.FX, "NZD/USD"),
    _instrument("USDCAD", AssetClass.FX, "USD/CAD"),
    _instrument("USDCHF", AssetClass.FX, "USD/CHF"),
    _instrument("EURCHF", AssetClass.FX, "EUR/CHF"),
    _instrument("EURAUD", AssetClass.FX, "EUR/AUD"),
    _instrument("GBPAUD", AssetClass.FX, "GBP/AUD"),
    _instrument("AUDJPY", AssetClass.FX, "AUD/JPY"),
    _instrument("CADJPY", AssetClass.FX, "CAD/JPY"),
    _instrument("CHFJPY", AssetClass.FX, "CHF/JPY"),
    _instrument("XAUUSD", AssetClass.METAL, "Gold / XAUUSD"),
    _instrument("XAGUSD", AssetClass.METAL, "Silver / XAGUSD"),
    _instrument("GER40", AssetClass.INDEX, "Germany 40"),
    _instrument("UK100", AssetClass.INDEX, "FTSE 100"),
    _instrument("US500", AssetClass.INDEX, "US 500"),
    _instrument("USTECH100", AssetClass.INDEX, "US Tech 100"),
    _instrument("US30", AssetClass.INDEX, "Wall Street"),
    _instrument("FRA40", AssetClass.INDEX, "France 40"),
    _instrument("USCRUDE", AssetClass.ENERGY, "US Crude"),
    _instrument("BRENT", AssetClass.ENERGY, "Brent"),
)

INITIAL_INSTRUMENT_REGISTRY: Final[dict[str, InstrumentSpec]] = {
    item.symbol: item for item in INITIAL_INSTRUMENTS
}


_FX_DEFAULT = (
    StrategyFamily.S1_TREND_MOMENTUM,
    StrategyFamily.S2_BREAKOUT,
    StrategyFamily.S3_MEAN_REVERSION,
    StrategyFamily.S4_SESSION_SWEEP,
    StrategyFamily.S7_MULTI_TIMEFRAME_TREND,
)
_VOLATILE_JPY = (
    StrategyFamily.S1_TREND_MOMENTUM,
    StrategyFamily.S2_BREAKOUT,
    StrategyFamily.S4_SESSION_SWEEP,
    StrategyFamily.S5_VOLATILITY_REGIME,
    StrategyFamily.S7_MULTI_TIMEFRAME_TREND,
)
_METAL = (
    StrategyFamily.S1_TREND_MOMENTUM,
    StrategyFamily.S2_BREAKOUT,
    StrategyFamily.S5_VOLATILITY_REGIME,
    StrategyFamily.S6_PRICE_STRUCTURE,
    StrategyFamily.S7_MULTI_TIMEFRAME_TREND,
)
_INDEX = (
    StrategyFamily.S1_TREND_MOMENTUM,
    StrategyFamily.S2_BREAKOUT,
    StrategyFamily.S3_MEAN_REVERSION,
    StrategyFamily.S4_SESSION_SWEEP,
    StrategyFamily.S5_VOLATILITY_REGIME,
    StrategyFamily.S7_MULTI_TIMEFRAME_TREND,
)


def suitable_families(instrument: InstrumentSpec) -> tuple[StrategyFamily, ...]:
    """Return limited initial hypotheses instead of every possible combination."""

    if instrument.symbol == "EURGBP":
        return (
            StrategyFamily.S1_TREND_MOMENTUM,
            StrategyFamily.S2_BREAKOUT,
            StrategyFamily.S3_MEAN_REVERSION,
            StrategyFamily.S4_SESSION_SWEEP,
        )
    if instrument.symbol in {"GBPJPY", "EURJPY", "AUDJPY", "CADJPY", "CHFJPY"}:
        return _VOLATILE_JPY
    if instrument.asset_class is AssetClass.FX:
        return _FX_DEFAULT
    if instrument.asset_class is AssetClass.METAL:
        return _METAL
    if instrument.asset_class is AssetClass.INDEX:
        return _INDEX
    return (
        StrategyFamily.S1_TREND_MOMENTUM,
        StrategyFamily.S2_BREAKOUT,
        StrategyFamily.S5_VOLATILITY_REGIME,
        StrategyFamily.S6_PRICE_STRUCTURE,
    )


def suitability_matrix() -> tuple[SuitabilityHypothesis, ...]:
    """Materialize the initial, explicitly non-promotional hypotheses."""

    return tuple(
        SuitabilityHypothesis(
            instrument=item.symbol,
            family=family,
            rationale="Initial behavior hypothesis; comparative evidence may reject it.",
        )
        for item in INITIAL_INSTRUMENTS
        for family in suitable_families(item)
    )


TIMEFRAME_COMPATIBILITY: Final[dict[StrategyFamily, dict[AssetClass, tuple[Timeframe, ...]]]] = {
    StrategyFamily.S0_FROZEN_RSI_ADX: {
        AssetClass.FX: (Timeframe.M5, Timeframe.M1),
    },
    StrategyFamily.S1_TREND_MOMENTUM: {
        AssetClass.FX: (Timeframe.H4, Timeframe.H1, Timeframe.M15),
        AssetClass.METAL: (Timeframe.H4, Timeframe.H1, Timeframe.M15),
        AssetClass.INDEX: (Timeframe.H1, Timeframe.M15, Timeframe.M5),
        AssetClass.ENERGY: (Timeframe.H4, Timeframe.H1, Timeframe.M15),
    },
    StrategyFamily.S2_BREAKOUT: {
        AssetClass.FX: (Timeframe.H1, Timeframe.M15, Timeframe.M5),
        AssetClass.METAL: (Timeframe.H1, Timeframe.M15, Timeframe.M5),
        AssetClass.INDEX: (Timeframe.M15, Timeframe.M5, Timeframe.M1),
        AssetClass.ENERGY: (Timeframe.H1, Timeframe.M15, Timeframe.M5),
    },
    StrategyFamily.S3_MEAN_REVERSION: {
        AssetClass.FX: (Timeframe.H1, Timeframe.M15, Timeframe.M5),
        AssetClass.INDEX: (Timeframe.M15, Timeframe.M5, Timeframe.M1),
    },
    StrategyFamily.S4_SESSION_SWEEP: {
        AssetClass.FX: (Timeframe.H1, Timeframe.M15, Timeframe.M5),
        AssetClass.INDEX: (Timeframe.M15, Timeframe.M5, Timeframe.M1),
    },
    StrategyFamily.S5_VOLATILITY_REGIME: {
        AssetClass.FX: (Timeframe.H4, Timeframe.H1, Timeframe.M15),
        AssetClass.METAL: (Timeframe.H4, Timeframe.H1, Timeframe.M15),
        AssetClass.INDEX: (Timeframe.H1, Timeframe.M15, Timeframe.M5),
        AssetClass.ENERGY: (Timeframe.H4, Timeframe.H1, Timeframe.M15),
    },
    StrategyFamily.S6_PRICE_STRUCTURE: {
        AssetClass.FX: (Timeframe.H1, Timeframe.M15, Timeframe.M5),
        AssetClass.METAL: (Timeframe.H1, Timeframe.M15, Timeframe.M5),
        AssetClass.ENERGY: (Timeframe.H1, Timeframe.M15, Timeframe.M5),
    },
    StrategyFamily.S7_MULTI_TIMEFRAME_TREND: {
        AssetClass.FX: (Timeframe.H4, Timeframe.H1, Timeframe.M15),
        AssetClass.METAL: (Timeframe.H4, Timeframe.H1, Timeframe.M15),
        AssetClass.INDEX: (Timeframe.H1, Timeframe.M15, Timeframe.M5),
        AssetClass.ENERGY: (Timeframe.H4, Timeframe.H1, Timeframe.M15),
    },
}


def is_timeframe_compatible(
    family: StrategyFamily, asset_class: AssetClass, timeframe: Timeframe
) -> bool:
    """Check the reviewed research matrix before scheduling a combination."""

    return timeframe in TIMEFRAME_COMPATIBILITY.get(family, {}).get(asset_class, ())
