"""Read-model contracts for the local DQ-02 Demo Operator.

The research universe and the Demo execution registry are intentionally
separate.  A research assignment explains what is being evaluated; it never
creates trading authority.
"""

# ruff: noqa: E501

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from src.ig_trader.strategy_lab.models import (
    INITIAL_INSTRUMENTS,
    AssetClass,
    InstrumentSpec,
    suitable_families,
)
from src.ig_trader.strategy_lab.strategies import strategy_registry


class InstrumentDiscoveryStatus(StrEnum):
    VERIFIED = "VERIFIED"
    NOT_FOUND = "NOT_FOUND"
    AMBIGUOUS = "AMBIGUOUS"
    UNTRADEABLE = "UNTRADEABLE"
    METADATA_INCOMPLETE = "METADATA_INCOMPLETE"


class DemoQualificationStatus(StrEnum):
    RESEARCH = "RESEARCH"
    DATA_PENDING = "DATA_PENDING"
    BACKTESTING = "BACKTESTING"
    REJECTED = "REJECTED"
    CHALLENGER = "CHALLENGER"
    CHAMPION_CANDIDATE = "CHAMPION_CANDIDATE"
    READY_FOR_DEMO = "READY_FOR_DEMO"
    DEMO_QUALIFYING = "DEMO_QUALIFYING"
    DEMO_QUALIFIED = "DEMO_QUALIFIED"


class DemoResultClassification(StrEnum):
    DEMO_NOT_STARTED = "DEMO_NOT_STARTED"
    DEMO_QUALIFYING = "DEMO_QUALIFYING"
    DEMO_LOW_SAMPLE = "DEMO_LOW_SAMPLE"
    DEMO_WATCH = "DEMO_WATCH"
    DEMO_REJECTED = "DEMO_REJECTED"
    DEMO_QUALIFIED = "DEMO_QUALIFIED"


@dataclass(frozen=True)
class StrategyDescription:
    strategy_id: str
    family: str
    version: str
    plain_english_name: str
    short_description: str
    detailed_description: str
    market_hypothesis: str
    entry_logic_summary: str
    exit_logic_summary: str
    stop_logic: str
    target_logic: str
    preferred_session: str
    preferred_timeframe: str
    preferred_regime: str
    known_weaknesses: str
    risk_considerations: str


@dataclass(frozen=True)
class ResearchInstrumentAssignment:
    symbol: str
    display_name: str
    asset_class: AssetClass
    strategy_id: str
    strategy_version: str
    timeframe: str
    qualification_status: DemoQualificationStatus
    why_assigned_to_instrument: str


@dataclass(frozen=True)
class DemoExecutionRegistration:
    """An explicit execution permit, never derived from the research universe."""

    symbol: str
    epic: str
    strategy_id: str
    strategy_version: str
    configuration_fingerprint: str


@dataclass(frozen=True)
class DemoResearchPortfolioPolicy:
    """Conservative DQ-02-only limits; Frozen V1 production limits remain intact."""

    max_concurrent_positions: int = 2
    max_positions_per_instrument: int = 1
    max_new_entries_per_cycle: int = 1
    max_trades_per_hour: int = 2
    max_currency_exposure: int = 1
    max_correlated_exposure: int = 1
    max_drawdown_r: float = 2.0


DEMO_RESEARCH_PORTFOLIO_POLICY: Final = DemoResearchPortfolioPolicy()


def strategy_descriptions() -> dict[str, StrategyDescription]:
    """Return human-readable, non-promotional descriptions for every S0–S7 family."""

    versions = {name: item.definition.version for name, item in strategy_registry().items()}
    documents = {
        "S0": (
            "Frozen RSI/ADX reference",
            "A frozen comparison baseline, not a strategy being optimized.",
            "Uses the historical RSI/ADX reference logic exactly as preserved for comparison.",
            "A fixed baseline helps measure whether a research candidate adds evidence.",
            "Acts only when the reference RSI/ADX condition is present on completed candles.",
            "Uses its existing reviewed exit representation.",
            "The frozen baseline stop is preserved rather than tuned.",
            "The frozen baseline target is preserved rather than tuned.",
            "Original reference session.",
            "1M and 5M.",
            "Reference-only.",
            "Small historical samples and changing market behavior can make a reference misleading.",
            "It is a benchmark and does not grant execution authority.",
        ),
        "S1": (
            "Trend momentum",
            "Looks for price movement that is continuing in one direction.",
            "Compares short and longer price structure, then waits for a completed candle to support the direction.",
            "Persistent directional movement can continue after a confirmed pullback or continuation signal.",
            "Requires a directional structure and a candle that closes in the same direction.",
            "Leaves through the reviewed stop/target or strategy exit evidence.",
            "Uses a volatility-derived protective distance.",
            "Uses the recorded research reward objective, never an invented fixed pip target.",
            "London and New York overlap when liquidity is observable.",
            "4H, 1H, or 15M.",
            "Sustained directional movement.",
            "Sideways markets can create repeated false continuations.",
            "Volatility expansion can make stops and exposure larger.",
        ),
        "S2": (
            "Range breakout",
            "Looks for a completed close beyond a recent trading range.",
            "Tracks recent high and low boundaries and treats a completed close beyond one as a possible breakout.",
            "A meaningful range break can signal a new directional phase.",
            "Requires a completed close beyond the recent range, never an intrabar guess.",
            "Leaves through the reviewed stop/target or evidence-based exit.",
            "Uses a volatility-aware distance outside normal range noise.",
            "Uses the documented research target multiple.",
            "Liquid session opens.",
            "1H, 15M, or 5M.",
            "Compression followed by expansion.",
            "False breakouts are common around news and thin liquidity.",
            "Spreads and slippage can materially reduce apparent edge at breakout points.",
        ),
        "S3": (
            "Mean reversion",
            "Looks for unusually stretched prices that may return toward a recent average.",
            "Measures a completed close against a volatility-normalized recent average and only considers a return-to-mean setup.",
            "Range-bound markets can revisit a statistically normal price after a temporary stretch.",
            "Requires a completed close materially away from the recent average.",
            "Exits at the reviewed target or when the protective stop proves the range thesis wrong.",
            "Uses a protective distance sized from observed volatility.",
            "Uses a documented return-to-mean objective.",
            "Quieter liquid periods.",
            "1H, 15M, or 5M.",
            "Non-trending, contained price behavior.",
            "A strong trend can keep moving instead of reverting.",
            "Must be disabled when trend or volatility evidence is strong.",
        ),
        "S4": (
            "Session sweep",
            "Looks for a brief break of a recent range that closes back inside it.",
            "Detects a completed candle that sweeps a recent high or low and then closes back through that boundary.",
            "Liquidity around session transitions can create failed range breaks before reversal.",
            "Requires both a sweep and a completed close back inside the observed range.",
            "Exits using the reviewed invalidation and return objective.",
            "Protects beyond the swept extreme using validated market distance rules.",
            "Targets the recorded range-return objective.",
            "London and New York transitions.",
            "1H, 15M, or 5M.",
            "Range behavior around session liquidity changes.",
            "News releases can turn a sweep into a genuine breakout.",
            "Requires current spread and market-status checks.",
        ),
        "S5": (
            "Volatility regime",
            "Participates only when recent movement is meaningfully different from normal movement.",
            "Compares recent and normal volatility, then follows the direction of a completed expansion candle.",
            "Volatility expansions can reveal a changed market regime with directional opportunity.",
            "Requires recent volatility to exceed the strategy's normal-volatility threshold.",
            "Uses reviewed exits that respond to the same observed regime.",
            "Uses volatility-derived stop distance and never a generic pip rule.",
            "Uses the recorded research target relationship.",
            "High-liquidity periods.",
            "4H, 1H, or 15M.",
            "Volatility expansion.",
            "Short-lived spikes can reverse quickly after entry.",
            "Higher volatility can increase loss size and correlated exposure.",
        ),
        "S6": (
            "Price structure",
            "Looks for a clear break of a recent swing after a large completed move.",
            "Compares a completed candle with recent swing highs and lows and requires material displacement.",
            "A decisive structure break can indicate that one side of the market has taken control.",
            "Requires both a swing break and a displacement-sized completed candle.",
            "Uses the reviewed structural invalidation or target evidence.",
            "Places protection beyond the invalidated structure using broker-valid distances.",
            "Uses the recorded research target relationship.",
            "Liquid trend sessions.",
            "1H, 15M, or 5M.",
            "Directional structural transitions.",
            "Choppy markets can create many weak structure breaks.",
            "Execution requires validated market metadata and current liquidity.",
        ),
        "S7": (
            "Multi-timeframe trend",
            "Combines a broader directional context with a shorter-term trigger.",
            "Compares broad and recent completed price averages, then requires the current completed candle to agree.",
            "Alignment between broader context and short-term trigger may reduce countertrend entries.",
            "Requires both context and trigger to agree on a completed candle.",
            "Uses reviewed exit evidence rather than a claimed guaranteed outcome.",
            "Uses a volatility-derived protective distance.",
            "Uses the recorded research target relationship.",
            "Liquid overlap periods.",
            "4H, 1H, or 15M.",
            "Persistent directional behavior across observed windows.",
            "Conflicting timeframes can delay or invalidate entries.",
            "Correlated instruments can concentrate the same directional risk.",
        ),
    }
    return {
        key: StrategyDescription(
            strategy_id=key,
            family=key,
            version=versions[key],
            plain_english_name=values[0],
            short_description=values[1],
            detailed_description=values[2],
            market_hypothesis=values[3],
            entry_logic_summary=values[4],
            exit_logic_summary=values[5],
            stop_logic=values[6],
            target_logic=values[7],
            preferred_session=values[8],
            preferred_timeframe=values[9],
            preferred_regime=values[10],
            known_weaknesses=values[11],
            risk_considerations=values[12],
        )
        for key, values in documents.items()
    }


def research_assignments() -> tuple[ResearchInstrumentAssignment, ...]:
    """Give all 26 research instruments a visible primary hypothesis only."""

    descriptions = strategy_descriptions()
    return tuple(_assignment(spec, descriptions) for spec in INITIAL_INSTRUMENTS)


def classify_discovery(
    instrument: InstrumentSpec, candidates: tuple[Mapping[str, object], ...]
) -> InstrumentDiscoveryStatus:
    """Refuse to select an EPIC unless a single cash/DFB candidate is clear."""

    if not candidates:
        return InstrumentDiscoveryStatus.NOT_FOUND
    valid = tuple(
        candidate
        for candidate in candidates
        if isinstance(candidate.get("epic"), str)
        and candidate["epic"].strip()
        and str(candidate.get("expiry") or "").upper() in {"DFB", "-"}
    )
    if len(valid) != 1:
        return InstrumentDiscoveryStatus.AMBIGUOUS
    candidate = valid[0]
    if candidate.get("market_status") not in {"TRADEABLE", "EDITS_ONLY"}:
        return InstrumentDiscoveryStatus.UNTRADEABLE
    required = ("epic", "name", "type", "expiry")
    if any(not candidate.get(item) for item in required):
        return InstrumentDiscoveryStatus.METADATA_INCOMPLETE
    del instrument
    return InstrumentDiscoveryStatus.VERIFIED


def approved_demo_execution_registry() -> tuple[DemoExecutionRegistration, ...]:
    """DQ-02 starts with no execution permits; research visibility is not authority."""

    return ()


def _assignment(
    instrument: InstrumentSpec, descriptions: Mapping[str, StrategyDescription]
) -> ResearchInstrumentAssignment:
    strategy_id = _primary_strategy(instrument)
    description = descriptions[strategy_id]
    reason = _assignment_reason(instrument, description)
    return ResearchInstrumentAssignment(
        symbol=instrument.symbol,
        display_name=instrument.display_name,
        asset_class=instrument.asset_class,
        strategy_id=strategy_id,
        strategy_version=description.version,
        timeframe=description.preferred_timeframe,
        qualification_status=DemoQualificationStatus.RESEARCH,
        why_assigned_to_instrument=reason,
    )


def _primary_strategy(instrument: InstrumentSpec) -> str:
    if instrument.symbol == "EURGBP":
        return "S3"
    if instrument.symbol.endswith("JPY"):
        return "S5"
    if instrument.asset_class is AssetClass.METAL:
        return "S2"
    if instrument.asset_class is AssetClass.INDEX:
        return "S4"
    if instrument.asset_class is AssetClass.ENERGY:
        return "S1"
    return suitable_families(instrument)[0].value


def _assignment_reason(instrument: InstrumentSpec, description: StrategyDescription) -> str:
    if instrument.symbol == "EURGBP":
        return (
            "EUR/GBP is being evaluated for range behavior. This mean-reversion candidate only "
            "acts on statistically stretched completed prices and is not a claim of profitability."
        )
    return (
        f"{instrument.display_name} is assigned to the {description.plain_english_name} research "
        "hypothesis because its asset class and available research timeframes fit the reviewed "
        "suitability matrix. The assignment is research-only, not a Demo trading permit."
    )
