"""SL-03 cache-first deep-data, funnel, and robust-qualification research."""

from __future__ import annotations

import hashlib
import random
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime
from decimal import Decimal
from math import sqrt
from pathlib import Path
from statistics import median
from time import monotonic

from src.ig_trader.sl02.contracts import AlignmentResult, BrokerEvidence, CostEvidence
from src.ig_trader.sl02.costs import (
    friction_model,
    generate_research_cost_model,
    load_cost_evidence,
)
from src.ig_trader.sl02.evidence import compare_with_broker_sample, preflight_dq03_evidence
from src.ig_trader.sl02.history import ExternalHistoryUnavailable
from src.ig_trader.sl02.runner import SL02_VERIFIED_SYMBOLS
from src.ig_trader.sl03.artifacts import write_sl03_artifacts
from src.ig_trader.sl03.history import MultiSourceHistory, ResearchDataset, ResearchHistorySource
from src.ig_trader.sl03.quality import AuditedDataset, audit_dataset
from src.ig_trader.sl03.strategies import sl03_challenger_variants, sl03_hypothesis
from src.ig_trader.strategy_lab.data import CanonicalDataset, DataContractError
from src.ig_trader.strategy_lab.engine import (
    DEFAULT_BACKTEST_CONFIG,
    BacktestResult,
    CandleBacktestEngine,
    FrictionModel,
    PerformanceMetrics,
    QualificationStatus,
    analyse_portfolio,
    calculate_metrics,
    chronological_splits,
    classify_result,
    walk_forward_windows,
)
from src.ig_trader.strategy_lab.models import (
    INITIAL_INSTRUMENT_REGISTRY,
    InstrumentSpec,
    Timeframe,
    is_timeframe_compatible,
    suitable_families,
)
from src.ig_trader.strategy_lab.strategies import RuleStrategy

ROOT = Path(__file__).resolve().parents[3]
DEFAULT_ARTIFACT_DIRECTORY = ROOT / "artifacts" / "strategy_lab"
DEFAULT_DQ03_DIRECTORY = ROOT / "artifacts" / "dq03"
DEFAULT_DUKASCOPY_CACHE_DIRECTORY = DEFAULT_ARTIFACT_DIRECTORY / "cache" / "dukascopy"
DEFAULT_YAHOO_CACHE_DIRECTORY = DEFAULT_ARTIFACT_DIRECTORY / "cache" / "sl02"

PRIORITY_SYMBOLS = frozenset(
    {
        "EURUSD",
        "GBPUSD",
        "EURGBP",
        "USDJPY",
        "EURJPY",
        "GBPJPY",
        "AUDUSD",
        "USDCHF",
        "XAUUSD",
        "XAGUSD",
        "US500",
        "USTECH100",
    }
)


@dataclass(frozen=True)
class SignalFunnel:
    candles_evaluated: int
    raw_strategy_signals: int
    signals_rejected_by_regime_filter: int
    signals_rejected_by_session_filter: int
    signals_rejected_by_cost_or_minimum_stop: int
    signals_while_trade_open: int
    entries_taken: int
    completed_trades: int
    oos_trades: int
    signals_rejected_by_minimum_stop: int = 0
    signals_rejected_by_cost_or_spread: int = 0
    minimum_stop_rejection_diagnostics: dict[str, Decimal | int | None] = field(
        default_factory=dict
    )


@dataclass(frozen=True)
class Simulation:
    result: BacktestResult
    funnel: SignalFunnel


@dataclass(frozen=True)
class SL03Run:
    artifact_paths: dict[str, Path]
    combinations_scheduled: int
    combinations_simulated: int
    parameter_sets_evaluated: int
    dataset_count: int
    runtime_seconds: float


class SL03BrokerEvidenceRequired(RuntimeError):
    """Fail before all data work if the supplied DQ-03 evidence is incomplete."""


class SL03Runner:
    """Offline research conductor.  It has no IG transport or execution capability."""

    def __init__(
        self,
        *,
        artifact_directory: Path = DEFAULT_ARTIFACT_DIRECTORY,
        dq03_directory: Path = DEFAULT_DQ03_DIRECTORY,
        yahoo_cache_directory: Path = DEFAULT_YAHOO_CACHE_DIRECTORY,
        dukascopy_cache_directory: Path = DEFAULT_DUKASCOPY_CACHE_DIRECTORY,
        history_source: ResearchHistorySource | None = None,
    ) -> None:
        self.artifact_directory = artifact_directory
        self.dq03_directory = dq03_directory
        self.history_source = history_source or MultiSourceHistory(
            dukascopy_cache=dukascopy_cache_directory,
            yahoo_cache=yahoo_cache_directory,
        )
        self.engine = CandleBacktestEngine()

    def run(self) -> SL03Run:
        started = monotonic()
        preflight = preflight_dq03_evidence(
            self.dq03_directory, expected_symbols=SL02_VERIFIED_SYMBOLS
        )
        if not preflight.broker_ready:
            raise SL03BrokerEvidenceRequired("SL03_BROKER_EVIDENCE_REQUIRED")
        cost_path = self.artifact_directory / "sl03_cost_model.json"
        generate_research_cost_model(
            preflight.evidence,
            expected_symbols=SL02_VERIFIED_SYMBOLS,
            output_path=cost_path,
        )
        costs = load_cost_evidence(cost_path)
        specifications = {
            symbol: INITIAL_INSTRUMENT_REGISTRY[symbol] for symbol in SL02_VERIFIED_SYMBOLS
        }
        datasets, acquisition_errors, gap_rows = self._acquire(specifications)
        manifest = self._manifest(
            specifications,
            datasets,
            acquisition_errors,
            preflight.evidence,
        )
        planned = self._planned_combinations(specifications)
        entries: list[dict[str, object]] = []
        funnels: list[dict[str, object]] = []
        walk_forward: list[dict[str, object]] = []
        stress_tests: list[dict[str, object]] = []
        robustness: list[dict[str, object]] = []
        simulated = parameter_sets = 0
        returns: dict[str, tuple[Decimal, ...]] = {}
        for symbol, strategy_id, timeframe in planned:
            specification = specifications[symbol]
            acquired = datasets.get((symbol, timeframe))
            broker = preflight.evidence.get(symbol)
            alignment = (
                compare_with_broker_sample(acquired.dataset, broker)
                if acquired is not None
                else None
            )
            blocked = _blocked_reason(
                acquired,
                acquisition_errors.get((symbol, timeframe)),
                broker,
                costs.get(symbol),
                alignment,
            )
            if blocked is not None:
                entry = _blocked_entry(
                    specification,
                    strategy_id,
                    timeframe,
                    acquired,
                    broker,
                    alignment,
                    blocked,
                )
                entries.append(entry)
                funnels.append(_blocked_funnel(entry))
                walk_forward.append(_blocked_auxiliary(entry, "walk_forward"))
                stress_tests.append(_blocked_auxiliary(entry, "stress"))
                robustness.append(_blocked_auxiliary(entry, "robustness"))
                continue
            assert acquired is not None and broker is not None
            assert costs.get(symbol) is not None
            evaluation = self._evaluate(
                specification,
                strategy_id,
                timeframe,
                acquired,
                broker,
                costs[symbol],
                alignment,
            )
            entries.append(evaluation.entry)
            funnels.append(evaluation.funnel)
            walk_forward.append(evaluation.walk_forward)
            stress_tests.append(evaluation.stress)
            robustness.append(evaluation.robustness)
            simulated += 1
            parameter_sets += int(evaluation.entry["parameter_sets_evaluated"])
            if evaluation.returns:
                returns[_entry_key(evaluation.entry)] = evaluation.returns
        _rank(entries)
        demo_ready = [
            entry
            for entry in entries
            if entry["classification"] == QualificationStatus.READY_FOR_DEMO_QUALIFICATION.value
        ]
        watchlist = _watchlist(entries)
        portfolio = _portfolio(entries, returns, specifications)
        documents = {
            "sl03_data_quality_audit.json": _quality_document(gap_rows),
            "sl03_dataset_manifest.json": {
                "datasets": manifest,
                "source_policy": (
                    "External-provider candles are never labelled as IG candles. "
                    "SL-03 uses cache-first source selection and records unavailable deep data."
                ),
            },
            "sl03_signal_funnel.json": {"evaluations": funnels},
            "sl03_results.json": {
                "run_started_at_utc": datetime.now(UTC),
                "combinations_scheduled": len(planned),
                "combinations_simulated": simulated,
                "parameter_sets_evaluated": parameter_sets,
                "walk_forward_evaluations": sum(
                    item.get("window_count", 0) for item in walk_forward
                ),
                "failure_counts": _failure_counts(entries),
                "results": entries,
                "safety": _safety(),
            },
            "sl03_leaderboard.json": {"entries": _ranked(entries)},
            "sl03_walk_forward.json": {"evaluations": walk_forward},
            "sl03_stress_tests.json": {"evaluations": stress_tests},
            "sl03_robustness.json": {"evaluations": robustness},
            "sl03_portfolio.json": portfolio,
            "sl03_demo_watchlist.json": {
                "ready_for_demo_qualification": demo_ready,
                "demo_research_watchlist": watchlist,
                "activation_required": "Separate reviewed DQ-04 activation only.",
            },
            "sl03_demo_candidate_registry.json": {
                "execution_authority": "OFF",
                "activation_required": "Separate reviewed DQ-04 activation only.",
                "registrations": demo_ready,
                "reason": (
                    "Research evidence cannot activate a Demo robot or an execution registry."
                ),
            },
        }
        paths = write_sl03_artifacts(self.artifact_directory, documents)
        return SL03Run(
            artifact_paths=paths,
            combinations_scheduled=len(planned),
            combinations_simulated=simulated,
            parameter_sets_evaluated=parameter_sets,
            dataset_count=len(datasets),
            runtime_seconds=monotonic() - started,
        )

    def _acquire(
        self, specifications: dict[str, InstrumentSpec]
    ) -> tuple[
        dict[tuple[str, Timeframe], ResearchDataset],
        dict[tuple[str, Timeframe], str],
        list[dict[str, object]],
    ]:
        datasets: dict[tuple[str, Timeframe], ResearchDataset] = {}
        errors: dict[tuple[str, Timeframe], str] = {}
        gap_rows: list[dict[str, object]] = []
        for symbol, specification in specifications.items():
            for timeframe in (Timeframe.H4, Timeframe.H1, Timeframe.M15, Timeframe.M5):
                key = (symbol, timeframe)
                try:
                    acquired = self.history_source.load(
                        symbol, timeframe, specification.asset_class
                    )
                    audited = audit_dataset(acquired.dataset, specification.asset_class)
                    datasets[key] = replace(
                        acquired,
                        dataset=audited.dataset,
                        provenance=replace(
                            acquired.provenance,
                            normalized_fingerprint=audited.dataset.dataset_fingerprint,
                            parent_dataset_fingerprint=audited.parent_dataset_fingerprint,
                        ),
                    )
                    gap_rows.extend(_gap_rows(symbol, timeframe, audited))
                except (DataContractError, ExternalHistoryUnavailable) as error:
                    errors[key] = str(error)
        return datasets, errors, gap_rows

    def _planned_combinations(
        self, specifications: dict[str, InstrumentSpec]
    ) -> tuple[tuple[str, str, Timeframe], ...]:
        planned: list[tuple[str, str, Timeframe]] = []
        for symbol, specification in specifications.items():
            family_ids = tuple(family.value for family in suitable_families(specification))
            for timeframe in (Timeframe.H4, Timeframe.H1):
                for strategy_id in family_ids:
                    if is_timeframe_compatible(
                        strategy_id_to_family(strategy_id), specification.asset_class, timeframe
                    ):
                        planned.append((symbol, strategy_id, timeframe))
            if symbol in PRIORITY_SYMBOLS:
                for timeframe in (Timeframe.M15, Timeframe.M5):
                    for strategy_id in family_ids:
                        if is_timeframe_compatible(
                            strategy_id_to_family(strategy_id), specification.asset_class, timeframe
                        ):
                            planned.append((symbol, strategy_id, timeframe))
                if specification.asset_class.value == "FX":
                    planned.append((symbol, "S0", Timeframe.M5))
        return tuple(planned)

    def _manifest(
        self,
        specifications: dict[str, InstrumentSpec],
        datasets: dict[tuple[str, Timeframe], ResearchDataset],
        errors: dict[tuple[str, Timeframe], str],
        evidence: dict[str, BrokerEvidence],
    ) -> list[dict[str, object]]:
        rows: list[dict[str, object]] = []
        for symbol, specification in specifications.items():
            for timeframe in (Timeframe.H4, Timeframe.H1, Timeframe.M15, Timeframe.M5):
                acquired = datasets.get((symbol, timeframe))
                broker = evidence.get(symbol)
                if acquired is None:
                    rows.append(
                        {
                            "instrument": symbol,
                            "timeframe": timeframe.value,
                            "ig_epic": broker.epic if broker else None,
                            "status": "DATA_NOT_AVAILABLE",
                            "reason": errors.get((symbol, timeframe), "Dataset unavailable."),
                        }
                    )
                    continue
                alignment = compare_with_broker_sample(acquired.dataset, broker)
                rows.append(
                    {
                        "instrument": symbol,
                        "display_name": specification.display_name,
                        "ig_epic": broker.epic if broker else None,
                        "timeframe": timeframe.value,
                        "provider": acquired.provenance.provider,
                        "provider_symbol": acquired.provenance.provider_symbol,
                        "acquisition_timestamp_utc": acquired.provenance.acquisition_timestamp_utc,
                        "source_url": acquired.provenance.source_url,
                        "license_source_note": acquired.provenance.license_source_note,
                        "raw_source_fingerprint": acquired.provenance.raw_source_fingerprint,
                        "normalized_fingerprint": acquired.provenance.normalized_fingerprint,
                        "parent_dataset_fingerprint": (
                            acquired.provenance.parent_dataset_fingerprint
                        ),
                        "date_range": {
                            "first_utc": acquired.dataset.candles[0].timestamp_utc,
                            "last_utc": acquired.dataset.candles[-1].timestamp_utc,
                        },
                        "candle_count": len(acquired.dataset.candles),
                        "gap_count": len(acquired.dataset.gaps),
                        "unexplained_gap_count": sum(
                            gap.classification.value == "UNEXPLAINED_MISSING_DATA"
                            for gap in acquired.dataset.gaps
                        ),
                        "depth_status": acquired.depth_status.value,
                        "ig_alignment": asdict(alignment),
                    }
                )
        return rows

    def _evaluate(
        self,
        specification: InstrumentSpec,
        strategy_id: str,
        timeframe: Timeframe,
        acquired: ResearchDataset,
        broker: BrokerEvidence,
        cost: CostEvidence,
        alignment: AlignmentResult | None,
    ) -> _Evaluation:
        friction = friction_model(broker, cost, stress_multiplier=Decimal("1"))
        assert friction is not None
        variants = sl03_challenger_variants(strategy_id)
        splits = chronological_splits(acquired.dataset)
        development = {
            item.definition.configuration_fingerprint: _simulate(
                splits.development, item, friction, self.engine
            )
            for item in variants
        }
        validation = {
            item.definition.configuration_fingerprint: _simulate(
                splits.validation, item, friction, self.engine
            )
            for item in variants
        }
        selection = _select(variants, development, validation)
        selected = selection.strategy
        test = _simulate(splits.untouched_test, selected, friction, self.engine)
        walk_document, walk_metrics = _walk_forward(
            acquired.dataset,
            variants,
            friction,
            self.engine,
            run=test.result.metrics.trade_count >= 30,
        )
        base_classification = classify_result(
            test.result,
            validation=validation[selected.definition.configuration_fingerprint].result.metrics,
            out_of_sample=walk_metrics,
            overfit_risk=selection.isolated_peak,
        )
        stress_document, stress_passed = _stress(
            splits.untouched_test,
            selected,
            broker,
            cost,
            self.engine,
            severe=base_classification
            in {
                QualificationStatus.CHAMPION_CANDIDATE,
                QualificationStatus.LOW_SAMPLE_CONFIDENCE,
            },
        )
        classification = _final_classification(
            base_classification,
            test.result.metrics,
            walk_metrics,
            stress_passed,
        )
        bootstrap = _bootstrap(test.result, acquired.dataset.dataset_fingerprint)
        entry = _entry(
            specification,
            selected,
            timeframe,
            acquired,
            broker,
            alignment,
            test.result.metrics,
            development[selected.definition.configuration_fingerprint].result.metrics,
            validation[selected.definition.configuration_fingerprint].result.metrics,
            walk_metrics,
            classification,
            len(variants),
            selection,
            stress_document,
            bootstrap,
        )
        entry.update(
            {
                "raw_strategy_signals": test.funnel.raw_strategy_signals,
                "signals_rejected_by_session_filter": (
                    test.funnel.signals_rejected_by_session_filter
                ),
                "signals_rejected_by_cost_or_minimum_stop": (
                    test.funnel.signals_rejected_by_cost_or_minimum_stop
                ),
                "signals_rejected_by_minimum_stop": (test.funnel.signals_rejected_by_minimum_stop),
                "signals_rejected_by_cost_or_spread": (
                    test.funnel.signals_rejected_by_cost_or_spread
                ),
                "minimum_stop_rejection_diagnostics": (
                    test.funnel.minimum_stop_rejection_diagnostics
                ),
                "signals_while_trade_open": test.funnel.signals_while_trade_open,
                "entries_taken": test.funnel.entries_taken,
            }
        )
        return _Evaluation(
            entry=entry,
            funnel={
                "instrument": specification.symbol,
                "strategy": selected.definition.strategy_id,
                "strategy_version": selected.definition.version,
                "timeframe": timeframe.value,
                **asdict(test.funnel),
                "regime_filter_note": (
                    "0: current deterministic rules embed regime logic; no extra filter was added."
                ),
                "cost_rule_note": (
                    "Signals below the normalized DQ-03 minimum stop price distance are "
                    "rejected, not resized. No separate spread/cost eligibility rule exists."
                ),
            },
            walk_forward={
                "instrument": specification.symbol,
                "strategy": selected.definition.strategy_id,
                "strategy_version": selected.definition.version,
                "timeframe": timeframe.value,
                "classification": classification.value,
                **walk_document,
            },
            stress={
                "instrument": specification.symbol,
                "strategy": selected.definition.strategy_id,
                "strategy_version": selected.definition.version,
                "timeframe": timeframe.value,
                "classification": classification.value,
                **stress_document,
            },
            robustness={
                "instrument": specification.symbol,
                "strategy": selected.definition.strategy_id,
                "strategy_version": selected.definition.version,
                "timeframe": timeframe.value,
                "selection": asdict(selection),
                "bootstrap": bootstrap,
            },
            returns=tuple(trade.r_multiple for trade in test.result.trades),
        )


@dataclass(frozen=True)
class _Selection:
    strategy: RuleStrategy
    robustness_score: Decimal
    positive_neighbour_count: int
    isolated_peak: bool
    rationale: str


@dataclass(frozen=True)
class _Evaluation:
    entry: dict[str, object]
    funnel: dict[str, object]
    walk_forward: dict[str, object]
    stress: dict[str, object]
    robustness: dict[str, object]
    returns: tuple[Decimal, ...]


def _simulate(
    dataset: CanonicalDataset,
    strategy: RuleStrategy,
    friction: FrictionModel,
    engine: CandleBacktestEngine,
) -> Simulation:
    if dataset.has_quality_failure or not friction.complete:
        result = engine.run(dataset, strategy, friction)
        return Simulation(result, SignalFunnel(0, 0, 0, 0, 0, 0, 0, 0, 0))
    assert friction.minimum_stop_distance is not None
    candles = dataset.candles
    trades = []
    raw = rejected_session = rejected_minimum_stop = open_signals = entries = 0
    rejected_stop_distances: list[Decimal] = []
    next_available = 21
    for index in range(21, len(candles) - 1):
        signal = strategy.signal(candles[: index + 1])
        if signal is None:
            continue
        raw += 1
        if index < next_available:
            open_signals += 1
            continue
        if not friction.is_market_open(candles[index + 1].timestamp_utc):
            rejected_session += 1
            continue
        if signal.stop_distance < friction.minimum_stop_distance:
            rejected_minimum_stop += 1
            rejected_stop_distances.append(signal.stop_distance)
            continue
        trade, exit_index = engine._simulate_trade(
            candles,
            index + 1,
            signal.direction,
            signal.stop_distance,
            DEFAULT_BACKTEST_CONFIG.reward_to_risk,
            friction,
        )
        trades.append(trade)
        entries += 1
        next_available = exit_index + 1
    metrics = calculate_metrics(trades, friction.tick_size or Decimal("1"))
    result = BacktestResult(
        strategy=strategy.definition,
        dataset_fingerprint=dataset.dataset_fingerprint,
        configuration_fingerprint=DEFAULT_BACKTEST_CONFIG.fingerprint,
        trades=tuple(trades),
        metrics=metrics,
        status=QualificationStatus.RESEARCH_WATCH,
        status_reasons=(),
    )
    return Simulation(
        result,
        SignalFunnel(
            candles_evaluated=max(0, len(candles) - 22),
            raw_strategy_signals=raw,
            signals_rejected_by_regime_filter=0,
            signals_rejected_by_session_filter=rejected_session,
            signals_rejected_by_cost_or_minimum_stop=rejected_minimum_stop,
            signals_while_trade_open=open_signals,
            entries_taken=entries,
            completed_trades=len(trades),
            oos_trades=len(trades),
            signals_rejected_by_minimum_stop=rejected_minimum_stop,
            signals_rejected_by_cost_or_spread=0,
            minimum_stop_rejection_diagnostics=_minimum_stop_diagnostics(
                rejected_stop_distances, friction.minimum_stop_distance
            ),
        ),
    )


def _minimum_stop_diagnostics(
    rejected_stop_distances: list[Decimal], broker_minimum_stop_price: Decimal
) -> dict[str, Decimal | int | None]:
    """Aggregate raw-price stop diagnostics for one instrument/strategy/timeframe."""

    if not rejected_stop_distances:
        return {
            "rejection_count": 0,
            "broker_minimum_stop_price": broker_minimum_stop_price,
            "strategy_stop_distance_price_min": None,
            "strategy_stop_distance_price_max": None,
            "ratio_strategy_stop_to_minimum_min": None,
            "ratio_strategy_stop_to_minimum_max": None,
            "ratio_strategy_stop_to_minimum_median": None,
        }
    ratios = sorted(distance / broker_minimum_stop_price for distance in rejected_stop_distances)
    return {
        "rejection_count": len(rejected_stop_distances),
        "broker_minimum_stop_price": broker_minimum_stop_price,
        "strategy_stop_distance_price_min": min(rejected_stop_distances),
        "strategy_stop_distance_price_max": max(rejected_stop_distances),
        "ratio_strategy_stop_to_minimum_min": ratios[0],
        "ratio_strategy_stop_to_minimum_max": ratios[-1],
        "ratio_strategy_stop_to_minimum_median": Decimal(str(median(ratios))),
    }


def _select(
    variants: tuple[RuleStrategy, ...],
    development: dict[str, Simulation],
    validation: dict[str, Simulation],
) -> _Selection:
    scored: list[tuple[Decimal, RuleStrategy, int]] = []
    for item in variants:
        fingerprint = item.definition.configuration_fingerprint
        development_metrics = development[fingerprint].result.metrics
        validation_metrics = validation[fingerprint].result.metrics
        positive_neighbours = sum(
            _expectancy(validation[other.definition.configuration_fingerprint].result.metrics) > 0
            for other in variants
            if other is not item
        )
        score = (
            (Decimal("2") if _expectancy(validation_metrics) > 0 else Decimal("0"))
            + (Decimal("1") if _expectancy(development_metrics) > 0 else Decimal("0"))
            + Decimal(min(validation_metrics.trade_count, 100)) / Decimal("100")
            + Decimal(positive_neighbours) / Decimal(max(1, len(variants) - 1))
        )
        scored.append((score, item, positive_neighbours))
    score, strategy, neighbours = max(
        scored,
        key=lambda item: (
            item[0],
            _expectancy(validation[item[1].definition.configuration_fingerprint].result.metrics),
            validation[item[1].definition.configuration_fingerprint].result.metrics.trade_count,
        ),
    )
    isolated = (
        _expectancy(validation[strategy.definition.configuration_fingerprint].result.metrics) > 0
        and neighbours == 0
    )
    return _Selection(
        strategy=strategy,
        robustness_score=score,
        positive_neighbour_count=neighbours,
        isolated_peak=isolated,
        rationale=(
            "Selected on chronological development/validation robustness score: positive "
            "development and validation evidence, neighbour support, and trade count. "
            "Untouched test was excluded."
        ),
    )


def _walk_forward(
    dataset: CanonicalDataset,
    variants: tuple[RuleStrategy, ...],
    friction: FrictionModel,
    engine: CandleBacktestEngine,
    *,
    run: bool,
) -> tuple[dict[str, object], PerformanceMetrics | None]:
    if not run:
        return {
            "window_count": 0,
            "reason": "NOT_RUN_BELOW_30_UNTOUCHED_TEST_TRADES",
            "windows": [],
        }, None
    total = len(dataset.candles)
    windows = walk_forward_windows(
        dataset,
        development_size=max(60, int(total * 0.40)),
        validation_size=max(30, int(total * 0.20)),
        step=max(30, int(total * 0.20)),
    )[:3]
    rows: list[dict[str, object]] = []
    trades = []
    for number, window in enumerate(windows, start=1):
        development = {
            item.definition.configuration_fingerprint: _simulate(
                window.development, item, friction, engine
            )
            for item in variants
        }
        selected = max(
            variants,
            key=lambda item: _expectancy(
                development[item.definition.configuration_fingerprint].result.metrics
            ),
        )
        validation = _simulate(window.validation, selected, friction, engine)
        trades.extend(validation.result.trades)
        rows.append(
            {
                "window": number,
                "development_trade_count": development[
                    selected.definition.configuration_fingerprint
                ].result.metrics.trade_count,
                "validation_trade_count": validation.result.metrics.trade_count,
                "parameter_fingerprint": selected.definition.configuration_fingerprint,
                "expectancy": validation.result.metrics.expectancy,
                "development_first_utc": window.development.candles[0].timestamp_utc,
                "validation_last_utc": window.validation.candles[-1].timestamp_utc,
            }
        )
    metrics = calculate_metrics(trades, friction.tick_size or Decimal("1")) if trades else None
    return {"window_count": len(rows), "windows": rows, "aggregate_oos": _metrics(metrics)}, metrics


def _stress(
    dataset: CanonicalDataset,
    strategy: RuleStrategy,
    broker: BrokerEvidence,
    cost: CostEvidence,
    engine: CandleBacktestEngine,
    *,
    severe: bool,
) -> tuple[dict[str, object], bool]:
    multipliers = [Decimal("1"), Decimal("1.25"), Decimal("1.50")]
    if severe:
        multipliers.append(Decimal("2"))
    rows: list[dict[str, object]] = []
    passes = True
    for multiplier in multipliers:
        friction = friction_model(broker, cost, stress_multiplier=multiplier)
        assert friction is not None
        simulation = _simulate(dataset, strategy, friction, engine)
        metric = simulation.result.metrics
        passed = metric.expectancy is not None and metric.expectancy > 0
        if multiplier <= Decimal("1.50"):
            passes = passes and passed
        rows.append(
            {
                "cost_multiplier": multiplier,
                "trade_count": metric.trade_count,
                "expectancy": metric.expectancy,
                "profit_factor": metric.profit_factor,
                "max_drawdown_r": metric.maximum_drawdown_r,
                "passed": passed,
            }
        )
    return {"passed_base_25_50": passes, "scenarios": rows}, passes


def _bootstrap(result: BacktestResult, dataset_fingerprint: str) -> dict[str, object]:
    returns = tuple(trade.r_multiple for trade in result.trades)
    if len(returns) < 30:
        return {"status": "NOT_RUN_INSUFFICIENT_TRADES", "trade_count": len(returns)}
    seed = int(
        hashlib.sha256(
            f"{dataset_fingerprint}:{result.strategy.configuration_fingerprint}".encode()
        ).hexdigest()[:16],
        16,
    )
    rng = random.Random(seed)
    block = max(2, int(sqrt(len(returns))))
    means: list[Decimal] = []
    for _ in range(500):
        sample: list[Decimal] = []
        while len(sample) < len(returns):
            start = rng.randrange(len(returns))
            sample.extend(returns[start : start + block])
            if len(sample) < len(returns):
                sample.extend(returns[: max(0, start + block - len(returns))])
        means.append(sum(sample[: len(returns)], Decimal("0")) / len(returns))
    ordered = sorted(means)
    return {
        "status": "BLOCK_BOOTSTRAP_500",
        "block_size": block,
        "iterations": len(means),
        "median_expectancy": Decimal(str(median(ordered))),
        "percentile_5_expectancy": ordered[max(0, int(len(ordered) * 0.05) - 1)],
        "probability_expectancy_positive": sum(value > 0 for value in means) / len(means),
        "note": "Uncertainty diagnostic only; it never overrides actual untouched OOS evidence.",
    }


def _final_classification(
    base: QualificationStatus,
    metrics: PerformanceMetrics,
    walk: PerformanceMetrics | None,
    stress_passed: bool,
) -> QualificationStatus:
    if base is QualificationStatus.CHAMPION_CANDIDATE and not stress_passed:
        return QualificationStatus.STRESS_TEST_FAIL
    if (
        base is QualificationStatus.CHAMPION_CANDIDATE
        and stress_passed
        and walk is not None
        and walk.trade_count >= 100
        and walk.expectancy is not None
        and walk.expectancy > 0
        and metrics.expectancy is not None
        and metrics.expectancy > 0
    ):
        return QualificationStatus.READY_FOR_DEMO_QUALIFICATION
    return base


def _blocked_reason(
    acquired: ResearchDataset | None,
    error: str | None,
    broker: BrokerEvidence | None,
    cost: CostEvidence | None,
    alignment: AlignmentResult | None,
) -> tuple[QualificationStatus, str] | None:
    if acquired is None:
        return QualificationStatus.DATA_NOT_AVAILABLE, error or "Dataset unavailable."
    if alignment is not None and alignment.status.value == "MATERIAL_SOURCE_DIVERGENCE":
        return QualificationStatus.SOURCE_DIVERGENCE, alignment.reason
    if acquired.dataset.has_quality_failure:
        return (
            QualificationStatus.DATA_QUALITY_FAIL,
            "Unexplained external-data gap remains fail-closed.",
        )
    if acquired.depth_status.value == "LOW_DATA_DEPTH":
        return (
            QualificationStatus.LOW_DATA_DEPTH,
            "Dataset does not meet the reviewed timeframe depth target.",
        )
    if friction_model(broker, cost, stress_multiplier=Decimal("1")) is None:
        return (
            QualificationStatus.COST_MODEL_INCOMPLETE,
            "DQ-03 fingerprint-bound cost evidence is incomplete.",
        )
    return None


def _blocked_entry(
    specification: InstrumentSpec,
    strategy_id: str,
    timeframe: Timeframe,
    acquired: ResearchDataset | None,
    broker: BrokerEvidence | None,
    alignment: AlignmentResult | None,
    blocked: tuple[QualificationStatus, str],
) -> dict[str, object]:
    status, reason = blocked
    return {
        "instrument": specification.symbol,
        "ig_epic": broker.epic if broker else None,
        "asset_class": specification.asset_class.value,
        "strategy": strategy_id,
        "strategy_version": "frozen-v1-reference" if strategy_id == "S0" else "SL03_NOT_SIMULATED",
        "strategy_description": sl03_hypothesis(strategy_id),
        "timeframe": timeframe.value,
        "data_source": acquired.provenance.provider if acquired else None,
        "dataset_fingerprint": acquired.dataset.dataset_fingerprint if acquired else None,
        "data_depth": acquired.depth_status.value if acquired else "DATA_NOT_AVAILABLE",
        "candle_count": len(acquired.dataset.candles) if acquired else 0,
        "ig_alignment": asdict(alignment) if alignment else None,
        "parameter_sets_evaluated": 0,
        "trade_count": 0,
        "oos_trade_count": 0,
        "classification": status.value,
        "evaluation_state": "PRE_SIMULATION_BLOCKED",
        "why_rejected": [reason],
        "champion_challenger_rank": None,
        "demo_ready": False,
    }


def _entry(
    specification: InstrumentSpec,
    strategy: RuleStrategy,
    timeframe: Timeframe,
    acquired: ResearchDataset,
    broker: BrokerEvidence,
    alignment: AlignmentResult | None,
    metrics: PerformanceMetrics,
    development: PerformanceMetrics,
    validation: PerformanceMetrics,
    walk: PerformanceMetrics | None,
    classification: QualificationStatus,
    variants: int,
    selection: _Selection,
    stress: dict[str, object],
    bootstrap: dict[str, object],
) -> dict[str, object]:
    return {
        "instrument": specification.symbol,
        "ig_epic": broker.epic,
        "asset_class": specification.asset_class.value,
        "strategy": strategy.definition.strategy_id,
        "strategy_version": strategy.definition.version,
        "strategy_description": strategy.definition.change_reason,
        "timeframe": timeframe.value,
        "parameters": dict(strategy.definition.parameters),
        "parameter_fingerprint": strategy.definition.configuration_fingerprint,
        "parameter_sets_evaluated": variants,
        "selection_criteria": selection.rationale,
        "robustness_score": selection.robustness_score,
        "positive_neighbour_count": selection.positive_neighbour_count,
        "data_source": acquired.provenance.provider,
        "dataset_fingerprint": acquired.dataset.dataset_fingerprint,
        "data_depth": acquired.depth_status.value,
        "candle_count": len(acquired.dataset.candles),
        "ig_alignment": asdict(alignment) if alignment else None,
        **_metrics(metrics),
        "development_trade_count": development.trade_count,
        "validation_trade_count": validation.trade_count,
        "untouched_test_trade_count": metrics.trade_count,
        "oos_trade_count": walk.trade_count if walk else 0,
        "validation_expectancy": validation.expectancy,
        "oos_expectancy": walk.expectancy if walk else None,
        "stress_base_25_50_passed": stress["passed_base_25_50"],
        "stress_scenarios": stress["scenarios"],
        "bootstrap": bootstrap,
        "classification": classification.value,
        "evaluation_state": _simulation_state(classification),
        "why_rejected": _reasons(classification, selection, stress, walk),
        "champion_challenger_rank": None,
        "demo_ready": classification is QualificationStatus.READY_FOR_DEMO_QUALIFICATION,
        "metric_breakdown": {
            "session": metrics.by_session,
            "weekday": metrics.by_weekday,
            "month": metrics.by_month,
            "volatility_regime": metrics.by_volatility_regime,
        },
    }


def _metrics(metrics: PerformanceMetrics | None) -> dict[str, object]:
    if metrics is None:
        return {
            "trade_count": 0,
            "wins": 0,
            "losses": 0,
            "win_rate": None,
            "net_r": None,
            "expectancy": None,
            "profit_factor": None,
            "max_drawdown_r": None,
            "maximum_losing_streak": 0,
        }
    return {
        "trade_count": metrics.trade_count,
        "wins": metrics.wins,
        "losses": metrics.losses,
        "win_rate": metrics.win_rate,
        "net_r": metrics.net_r,
        "expectancy": metrics.expectancy,
        "profit_factor": metrics.profit_factor,
        "max_drawdown_r": metrics.maximum_drawdown_r,
        "maximum_losing_streak": metrics.maximum_losing_streak,
    }


def _gap_rows(
    symbol: str, timeframe: Timeframe, audited: AuditedDataset
) -> list[dict[str, object]]:
    return [{"instrument": symbol, "timeframe": timeframe.value, **item} for item in audited.gaps]


def _quality_document(rows: list[dict[str, object]]) -> dict[str, object]:
    classifications = (
        "EXPECTED_WEEKEND",
        "EXPECTED_MARKET_SESSION_CLOSE",
        "EXPECTED_HOLIDAY_OR_EXCHANGE_CLOSE",
        "PROVIDER_OUTAGE",
        "UNEXPLAINED_MISSING_DATA",
        "SOURCE_TRUNCATION",
        "INVALID_ROW",
        "DUPLICATE",
        "OUT_OF_ORDER",
    )
    return {
        "datasets_audited": len({(item["instrument"], item["timeframe"]) for item in rows}),
        "gaps_examined": len(rows),
        "classification_counts": {
            name: sum(item["classification"] == name for item in rows) for name in classifications
        },
        "gaps": rows,
        "policy": (
            "Only deterministic weekends, US index holidays, or three-or-more identical "
            "recurring session transitions are recovered."
        ),
    }


def _blocked_funnel(entry: dict[str, object]) -> dict[str, object]:
    return {
        "instrument": entry["instrument"],
        "strategy": entry["strategy"],
        "strategy_version": entry["strategy_version"],
        "timeframe": entry["timeframe"],
        **asdict(SignalFunnel(0, 0, 0, 0, 0, 0, 0, 0, 0)),
        "reason": entry["why_rejected"],
    }


def _blocked_auxiliary(entry: dict[str, object], kind: str) -> dict[str, object]:
    return {
        "instrument": entry["instrument"],
        "strategy": entry["strategy"],
        "timeframe": entry["timeframe"],
        "classification": entry["classification"],
        "reason": entry["why_rejected"],
        "kind": kind,
        "window_count": 0,
    }


def _expectancy(metrics: PerformanceMetrics) -> Decimal:
    return metrics.expectancy if metrics.expectancy is not None else Decimal("-Infinity")


def _reasons(
    classification: QualificationStatus,
    selection: _Selection,
    stress: dict[str, object],
    walk: PerformanceMetrics | None,
) -> list[str]:
    result = [classification.value, "UNTUCHED_TEST_NOT_USED_FOR_PARAMETER_SELECTION"]
    if selection.isolated_peak:
        result.append("ISOLATED_VALIDATION_PEAK")
    if walk is None:
        result.append("WALK_FORWARD_NOT_RUN_BELOW_30_UNTOUCHED_TEST_TRADES")
    if not stress["passed_base_25_50"]:
        result.append("BASE_25_50_FRICTION_NOT_ALL_POSITIVE")
    return result


def _simulation_state(classification: QualificationStatus) -> str:
    return (
        "SIMULATED_AND_QUALIFIED"
        if classification
        in {
            QualificationStatus.RESEARCH_WATCH,
            QualificationStatus.CHALLENGER,
            QualificationStatus.CHAMPION_CANDIDATE,
            QualificationStatus.READY_FOR_DEMO_QUALIFICATION,
        }
        else "SIMULATED_AND_FAILED"
    )


def _rank(entries: list[dict[str, object]]) -> None:
    eligible = {
        QualificationStatus.CHAMPION_CANDIDATE.value,
        QualificationStatus.READY_FOR_DEMO_QUALIFICATION.value,
    }
    for symbol in SL02_VERIFIED_SYMBOLS:
        candidates = [
            entry
            for entry in entries
            if entry["instrument"] == symbol and entry["classification"] in eligible
        ]
        for position, entry in enumerate(
            sorted(
                candidates,
                key=lambda item: _score(item.get("robustness_score")),
                reverse=True,
            ),
            start=1,
        ):
            entry["champion_challenger_rank"] = (
                "CHAMPION_CANDIDATE" if position == 1 else f"CHALLENGER_{position - 1}"
            )
            if (
                position > 1
                and entry["classification"] == QualificationStatus.CHAMPION_CANDIDATE.value
            ):
                entry["classification"] = QualificationStatus.CHALLENGER.value


def _ranked(entries: list[dict[str, object]]) -> list[dict[str, object]]:
    return sorted(
        entries,
        key=lambda item: (
            _status_score(str(item["classification"])),
            _score(item.get("robustness_score")),
            _score(item.get("oos_expectancy")),
        ),
        reverse=True,
    )


def _status_score(value: str) -> int:
    return {
        QualificationStatus.READY_FOR_DEMO_QUALIFICATION.value: 5,
        QualificationStatus.CHAMPION_CANDIDATE.value: 4,
        QualificationStatus.CHALLENGER.value: 3,
        QualificationStatus.RESEARCH_WATCH.value: 2,
    }.get(value, 0)


def _score(value: object) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal("-Infinity")


def _watchlist(entries: list[dict[str, object]]) -> list[dict[str, object]]:
    statuses = {
        QualificationStatus.CHAMPION_CANDIDATE.value,
        QualificationStatus.CHALLENGER.value,
        QualificationStatus.LOW_SAMPLE_CONFIDENCE.value,
    }
    return [
        {
            **entry,
            "remaining_blocker": (
                "Need at least 100 aggregate walk-forward OOS trades and a separate DQ-04 review."
                if entry["classification"]
                in {
                    QualificationStatus.CHAMPION_CANDIDATE.value,
                    QualificationStatus.CHALLENGER.value,
                }
                else (
                    "Research evidence is below the required OOS sample or another "
                    "non-execution gate remains incomplete."
                )
            ),
        }
        for entry in _ranked(entries)
        if entry["classification"] in statuses
    ]


def _portfolio(
    entries: list[dict[str, object]],
    returns: dict[str, tuple[Decimal, ...]],
    specifications: dict[str, InstrumentSpec],
) -> dict[str, object]:
    eligible = [
        item
        for item in entries
        if item["classification"]
        in {
            QualificationStatus.READY_FOR_DEMO_QUALIFICATION.value,
            QualificationStatus.CHAMPION_CANDIDATE.value,
            QualificationStatus.CHALLENGER.value,
        }
        and _entry_key(item) in returns
    ]
    selected: list[dict[str, object]] = []
    currencies: set[str] = set()
    families: set[str] = set()
    for entry in _ranked(eligible):
        symbol = str(entry["instrument"])
        exposure = {symbol[:3], symbol[3:]} if entry["asset_class"] == "FX" else {symbol}
        family = str(entry["strategy"])
        if exposure & currencies or family in families:
            continue
        selected.append(entry)
        currencies.update(exposure)
        families.add(family)
    analysis = analyse_portfolio(
        {key: returns[key] for key in (_entry_key(item) for item in selected) if key in returns},
        specifications,
    )
    return {
        "eligible_count": len(eligible),
        "diversified_preferred_list": selected,
        "analysis": asdict(analysis),
        "note": (
            "Research diversification only; this list cannot grant allocation or "
            "execution authority."
        ),
    }


def _entry_key(entry: dict[str, object]) -> str:
    return ":".join(
        str(entry.get(name, ""))
        for name in ("instrument", "strategy", "strategy_version", "timeframe")
    )


def _failure_counts(entries: list[dict[str, object]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for entry in entries:
        status = str(entry["classification"])
        counts[status] = counts.get(status, 0) + 1
    return dict(sorted(counts.items()))


def _safety() -> dict[str, object]:
    return {
        "ig_create_calls": 0,
        "ig_close_calls": 0,
        "live_calls": 0,
        "execution_authority": "OFF",
        "order_endpoints": "NOT_PRESENT",
        "azure_calls": 0,
    }


def strategy_id_to_family(strategy_id: str):
    return sl03_challenger_variants(strategy_id)[0].definition.family
