"""Deterministic SL-02 orchestration for broad, read-only strategy research."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from time import monotonic
from typing import Any

from src.ig_trader.sl02.artifacts import write_sl02_artifacts
from src.ig_trader.sl02.contracts import AcquiredDataset, AlignmentResult, BrokerEvidence
from src.ig_trader.sl02.costs import cost_evidence_preflight, friction_model, load_cost_evidence
from src.ig_trader.sl02.evidence import (
    compare_with_broker_sample,
    preflight_dq03_evidence,
    write_preflight_report,
)
from src.ig_trader.sl02.history import ExternalHistoryUnavailable, YahooFinanceHistorySource
from src.ig_trader.strategy_lab.engine import (
    CandleBacktestEngine,
    PerformanceMetrics,
    QualificationStatus,
    analyse_portfolio,
    calculate_metrics,
    chronological_splits,
    classify_result,
    walk_forward_windows,
)
from src.ig_trader.strategy_lab.data import DataContractError
from src.ig_trader.strategy_lab.models import (
    INITIAL_INSTRUMENT_REGISTRY,
    AssetClass,
    InstrumentSpec,
    StrategyFamily,
    Timeframe,
    is_timeframe_compatible,
    suitable_families,
)
from src.ig_trader.strategy_lab.strategies import RuleStrategy, bounded_parameter_variants, strategy_registry

ROOT = Path(__file__).resolve().parents[3]
DEFAULT_ARTIFACT_DIRECTORY = ROOT / "artifacts" / "strategy_lab"
DEFAULT_CACHE_DIRECTORY = DEFAULT_ARTIFACT_DIRECTORY / "cache" / "sl02"
DEFAULT_DQ03_DIRECTORY = ROOT / "artifacts" / "dq03"
DEFAULT_COST_EVIDENCE_PATH = DEFAULT_ARTIFACT_DIRECTORY / "sl02_cost_model.json"

SL02_VERIFIED_SYMBOLS = (
    "EURUSD",
    "GBPUSD",
    "EURGBP",
    "USDJPY",
    "EURJPY",
    "GBPJPY",
    "AUDUSD",
    "NZDUSD",
    "USDCAD",
    "USDCHF",
    "EURCHF",
    "EURAUD",
    "GBPAUD",
    "AUDJPY",
    "CADJPY",
    "CHFJPY",
    "XAUUSD",
    "XAGUSD",
    "US500",
    "USTECH100",
)
SL02_EXCLUDED_SYMBOLS = {
    "GER40": "AMBIGUOUS_CONTRACT_SELECTION",
    "UK100": "AMBIGUOUS_CONTRACT_SELECTION",
    "US30": "METADATA_INCOMPLETE",
    "FRA40": "METADATA_INCOMPLETE",
    "USCRUDE": "METADATA_INCOMPLETE",
    "BRENT": "METADATA_INCOMPLETE",
}
_RESEARCH_TIMEFRAMES = (Timeframe.H4, Timeframe.H1, Timeframe.M15, Timeframe.M5)


@dataclass(frozen=True)
class SL02Run:
    artifact_paths: dict[str, Path]
    evidence_preflight_path: Path
    combinations_scheduled: int
    combinations_simulated: int
    parameter_sets_evaluated: int
    dataset_count: int
    runtime_seconds: float


@dataclass(frozen=True)
class _Evaluation:
    entry: dict[str, object]
    walk_forward: dict[str, object]
    stress_test: dict[str, object]
    returns: tuple[Decimal, ...]
    intervals: tuple[tuple[datetime, datetime], ...]


class SL02BrokerEvidenceRequired(RuntimeError):
    """Raised before acquisition when DQ-03 cannot prove the required broker facts."""

    def __init__(self, report_path: Path) -> None:
        super().__init__("SL02_BROKER_EVIDENCE_REQUIRED")
        self.report_path = report_path


class SL02Runner:
    """Runs external GET-only data collection and local-only research calculations."""

    def __init__(
        self,
        *,
        artifact_directory: Path = DEFAULT_ARTIFACT_DIRECTORY,
        cache_directory: Path = DEFAULT_CACHE_DIRECTORY,
        dq03_directory: Path = DEFAULT_DQ03_DIRECTORY,
        cost_evidence_path: Path = DEFAULT_COST_EVIDENCE_PATH,
        history_source: YahooFinanceHistorySource | None = None,
        max_workers: int = 4,
    ) -> None:
        if not 1 <= max_workers <= 4:
            raise ValueError("SL-02 external acquisition is bounded to one through four workers")
        self.artifact_directory = artifact_directory
        self.dq03_directory = dq03_directory
        self.cost_evidence_path = cost_evidence_path
        self.history_source = history_source or YahooFinanceHistorySource(cache_directory)
        self.max_workers = max_workers
        self.strategies = strategy_registry()
        self.engine = CandleBacktestEngine()

    def run(self) -> SL02Run:
        """Execute the complete 20-instrument batch; failures stay explicit per dataset."""

        started = monotonic()
        preflight = preflight_dq03_evidence(
            self.dq03_directory, expected_symbols=SL02_VERIFIED_SYMBOLS
        )
        cost_preflight = cost_evidence_preflight(
            self.cost_evidence_path,
            preflight.evidence,
            expected_symbols=SL02_VERIFIED_SYMBOLS,
        )
        preflight_report = {
            **preflight.document(),
            "cost_evidence": cost_preflight,
        }
        preflight_path = write_preflight_report(
            self.artifact_directory / "sl02_evidence_preflight.json", preflight_report
        )
        if not preflight.broker_ready:
            raise SL02BrokerEvidenceRequired(preflight_path)
        broker_evidence = preflight.evidence
        cost_evidence = load_cost_evidence(self.cost_evidence_path)
        specifications = {symbol: INITIAL_INSTRUMENT_REGISTRY[symbol] for symbol in SL02_VERIFIED_SYMBOLS}
        planned = self._planned_combinations(specifications)
        datasets, acquisition_errors = self._acquire(planned)
        manifest = self._dataset_manifest(specifications, datasets, acquisition_errors, broker_evidence)

        evaluations: list[_Evaluation] = []
        simulated = 0
        parameters = 0
        for symbol, strategy_id, timeframe in planned:
            dataset = datasets.get((symbol, timeframe))
            broker = broker_evidence.get(symbol)
            alignment = (
                compare_with_broker_sample(dataset.dataset, broker)
                if dataset is not None
                else None
            )
            blocked_status, reason = _blocked_status(
                dataset, acquisition_errors.get((symbol, timeframe)), alignment, broker, cost_evidence.get(symbol)
            )
            strategy = self.strategies[strategy_id]
            if blocked_status is not None:
                evaluations.append(
                    _Evaluation(
                        _blocked_entry(
                            specifications[symbol], strategy, timeframe, dataset, broker, alignment, blocked_status, reason
                        ),
                        _blocked_walk_forward(symbol, strategy_id, timeframe, blocked_status, reason),
                        _blocked_stress_test(symbol, strategy_id, timeframe, blocked_status, reason),
                        (),
                        (),
                    )
                )
                continue
            assert dataset is not None
            evaluation = self._evaluate(
                specifications[symbol], strategy, timeframe, dataset, broker, cost_evidence[symbol], alignment
            )
            evaluations.append(evaluation)
            simulated += 1
            parameters += int(evaluation.entry["parameter_sets_evaluated"])

        entries = [evaluation.entry for evaluation in evaluations]
        _rank_candidates(entries)
        evaluation_summary = _evaluation_summary(entries)
        portfolio = _portfolio_document(evaluations, specifications)
        demo_candidates = [
            entry
            for entry in entries
            if entry["classification"] == QualificationStatus.READY_FOR_DEMO_QUALIFICATION.value
        ]
        documents = {
            "sl02_dataset_manifest.json": {
                "datasets": manifest,
                "excluded_instruments": SL02_EXCLUDED_SYMBOLS,
                "data_source_policy": "External structured OHLCV is never labelled as IG data.",
            },
            "sl02_results.json": {
                "run_started_at_utc": datetime.now(UTC).isoformat(),
                "combinations_scheduled": len(planned),
                "combinations_simulated": simulated,
                "parameter_sets_evaluated": parameters,
                "evaluation_summary": evaluation_summary,
                "evidence_preflight": preflight_report,
                "results": entries,
                "safety": _safety_document(),
            },
            "sl02_leaderboard.json": {"entries": _ranked_entries(entries)},
            "sl02_walk_forward.json": {"evaluations": [item.walk_forward for item in evaluations]},
            "sl02_stress_tests.json": {"evaluations": [item.stress_test for item in evaluations]},
            "sl02_portfolio.json": portfolio,
            "demo_candidate_registry.json": {
                "execution_authority": "OFF",
                "activation_required": "separate reviewed DQ-04 activation",
                "registrations": demo_candidates,
                "reason": "Research evidence never enables Demo execution.",
            },
        }
        paths = write_sl02_artifacts(self.artifact_directory, documents)
        return SL02Run(
            paths,
            preflight_path,
            len(planned),
            simulated,
            parameters,
            len(datasets),
            round(monotonic() - started, 3),
        )

    def _planned_combinations(
        self, specifications: dict[str, InstrumentSpec]
    ) -> tuple[tuple[str, str, Timeframe], ...]:
        planned: list[tuple[str, str, Timeframe]] = []
        for symbol, spec in specifications.items():
            families = list(suitable_families(spec))
            if spec.asset_class is AssetClass.FX:
                families.insert(0, StrategyFamily.S0_FROZEN_RSI_ADX)
            for family in families:
                for timeframe in _RESEARCH_TIMEFRAMES:
                    if is_timeframe_compatible(family, spec.asset_class, timeframe):
                        planned.append((symbol, family.value, timeframe))
        return tuple(planned)

    def _acquire(
        self, planned: tuple[tuple[str, str, Timeframe], ...]
    ) -> tuple[dict[tuple[str, Timeframe], AcquiredDataset], dict[tuple[str, Timeframe], str]]:
        requested = sorted({(symbol, timeframe) for symbol, _, timeframe in planned})
        direct = tuple(item for item in requested if item[1] is not Timeframe.H4)
        datasets: dict[tuple[str, Timeframe], AcquiredDataset] = {}
        errors: dict[tuple[str, Timeframe], str] = {}
        with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            futures = {
                executor.submit(self.history_source.load, symbol, timeframe): (symbol, timeframe)
                for symbol, timeframe in direct
            }
            for future in as_completed(futures):
                key = futures[future]
                try:
                    datasets[key] = future.result()
                except (DataContractError, ExternalHistoryUnavailable) as error:
                    errors[key] = str(error)
        for symbol, timeframe in requested:
            if timeframe is not Timeframe.H4:
                continue
            try:
                datasets[(symbol, timeframe)] = self.history_source.load(symbol, timeframe)
            except (DataContractError, ExternalHistoryUnavailable) as error:
                errors[(symbol, timeframe)] = str(error)
        return datasets, errors

    def _dataset_manifest(
        self,
        specifications: dict[str, InstrumentSpec],
        datasets: dict[tuple[str, Timeframe], AcquiredDataset],
        errors: dict[tuple[str, Timeframe], str],
        evidence: dict[str, BrokerEvidence],
    ) -> list[dict[str, object]]:
        rows: list[dict[str, object]] = []
        requested = sorted(set(datasets) | set(errors), key=lambda item: (item[0], item[1].value))
        for symbol, timeframe in requested:
            dataset = datasets.get((symbol, timeframe))
            broker = evidence.get(symbol)
            if dataset is None:
                rows.append(
                    {
                        "instrument": symbol,
                        "ig_epic": broker.epic if broker else None,
                        "timeframe": timeframe.value,
                        "provider": None,
                        "status": "DATA_NOT_AVAILABLE",
                        "reason": errors[(symbol, timeframe)],
                    }
                )
                continue
            alignment = compare_with_broker_sample(dataset.dataset, broker)
            gaps = dataset.dataset.gaps
            rows.append(
                {
                    "instrument": symbol,
                    "display_name": specifications[symbol].display_name,
                    "ig_epic": broker.epic if broker else None,
                    "timeframe": timeframe.value,
                    "provider": dataset.provider,
                    "provider_symbol": dataset.provider_symbol,
                    "source_url": dataset.source_url,
                    "acquired_at_utc": dataset.acquisition_timestamp_utc,
                    "cached": dataset.cached,
                    "date_range": {
                        "first_utc": dataset.dataset.candles[0].timestamp_utc,
                        "last_utc": dataset.dataset.candles[-1].timestamp_utc,
                    },
                    "candle_count": len(dataset.dataset.candles),
                    "gap_count": len(gaps),
                    "missing_gap_count": sum(
                        gap.classification.value == "MISSING_DATA" for gap in gaps
                    ),
                    "source_fingerprint": dataset.dataset.source_fingerprint,
                    "dataset_fingerprint": dataset.dataset.dataset_fingerprint,
                    "raw_source_fingerprint": dataset.raw_source_fingerprint,
                    "data_depth": dataset.depth_status.value,
                    "ig_alignment": asdict(alignment),
                }
            )
        return rows

    def _evaluate(
        self,
        specification: InstrumentSpec,
        strategy: RuleStrategy,
        timeframe: Timeframe,
        acquired: AcquiredDataset,
        broker: BrokerEvidence | None,
        cost,
        alignment: AlignmentResult | None,
    ) -> _Evaluation:
        base_friction = friction_model(broker, cost, stress_multiplier=Decimal("1"))
        assert base_friction is not None
        splits = chronological_splits(acquired.dataset)
        variants = bounded_parameter_variants(strategy)
        development = {item.definition.configuration_fingerprint: self.engine.run(splits.development, item, base_friction) for item in variants}
        validation = {item.definition.configuration_fingerprint: self.engine.run(splits.validation, item, base_friction) for item in variants}
        selected = variants[0] if strategy.definition.baseline_only else max(
            variants,
            key=lambda item: _expectancy(validation[item.definition.configuration_fingerprint].metrics),
        )
        selected_development = development[selected.definition.configuration_fingerprint]
        selected_validation = validation[selected.definition.configuration_fingerprint]
        test_result = self.engine.run(splits.untouched_test, selected, base_friction)
        peak_risk = _isolated_validation_peak(validation.values()) if not strategy.definition.baseline_only else False
        walk_document, walk_metrics = self._walk_forward(acquired.dataset, variants, selected, base_friction)
        stress_document, stress_passed = self._stress(splits.untouched_test, selected, broker, cost)
        classification = classify_result(
            test_result,
            validation=selected_validation.metrics,
            out_of_sample=walk_metrics,
            overfit_risk=peak_risk,
        )
        if classification not in {
            QualificationStatus.INSUFFICIENT_TRADES,
            QualificationStatus.LOW_SAMPLE_CONFIDENCE,
            QualificationStatus.NEGATIVE_EXPECTANCY,
            QualificationStatus.OVERFIT_RISK,
        } and not stress_passed:
            classification = QualificationStatus.STRESS_TEST_FAIL
        if acquired.depth_status.value == "LOW_DATA_DEPTH":
            classification = QualificationStatus.LOW_DATA_DEPTH
        metrics = test_result.metrics
        evaluation_state = _simulation_state(classification)
        entry = _entry(
            specification,
            selected,
            timeframe,
            acquired,
            broker,
            alignment,
            evaluation_state,
            classification,
            metrics,
            selected_validation.metrics,
            walk_metrics,
            len(variants),
            "Selected only from chronological validation expectancy; test was not used for selection.",
            stress_passed,
            [
                classification.value,
                "COST_EVIDENCE_FINGERPRINT_MATCHED",
                "ISOLATED_VALIDATION_PEAK" if peak_risk else "NO_ISOLATED_VALIDATION_PEAK",
            ],
        )
        walk_document.update(
            {
                "instrument": specification.symbol,
                "strategy": selected.definition.strategy_id,
                "timeframe": timeframe.value,
                "selected_parameter_fingerprint": selected.definition.configuration_fingerprint,
                "evaluation_state": evaluation_state,
                "classification": classification.value,
            }
        )
        stress_document.update(
            {
                "instrument": specification.symbol,
                "strategy": selected.definition.strategy_id,
                "timeframe": timeframe.value,
                "parameter_fingerprint": selected.definition.configuration_fingerprint,
                "evaluation_state": evaluation_state,
                "classification": classification.value,
            }
        )
        return _Evaluation(
            entry,
            walk_document,
            stress_document,
            tuple(trade.r_multiple for trade in test_result.trades),
            tuple((trade.entry_timestamp_utc, trade.exit_timestamp_utc) for trade in test_result.trades),
        )

    def _walk_forward(self, dataset, variants, selected, friction) -> tuple[dict[str, object], PerformanceMetrics | None]:
        total = len(dataset.candles)
        development_size = max(60, int(total * 0.40))
        validation_size = max(30, int(total * 0.20))
        windows = walk_forward_windows(
            dataset,
            development_size=development_size,
            validation_size=validation_size,
            step=validation_size,
        )
        rows: list[dict[str, object]] = []
        all_trades = []
        for number, window in enumerate(windows, start=1):
            if selected.definition.baseline_only:
                chosen = selected
            else:
                development = {item.definition.configuration_fingerprint: self.engine.run(window.development, item, friction) for item in variants}
                chosen = max(variants, key=lambda item: _expectancy(development[item.definition.configuration_fingerprint].metrics))
            result = self.engine.run(window.validation, chosen, friction)
            all_trades.extend(result.trades)
            rows.append(
                {
                    "window": number,
                    "development_first_utc": window.development.candles[0].timestamp_utc,
                    "development_last_utc": window.development.candles[-1].timestamp_utc,
                    "validation_first_utc": window.validation.candles[0].timestamp_utc,
                    "validation_last_utc": window.validation.candles[-1].timestamp_utc,
                    "parameter_fingerprint": chosen.definition.configuration_fingerprint,
                    "trade_count": result.metrics.trade_count,
                    "expectancy": result.metrics.expectancy,
                }
            )
        metrics = calculate_metrics(all_trades, friction.tick_size) if all_trades else None
        return (
            {
                "window_count": len(rows),
                "configuration": {
                    "development_size": development_size,
                    "validation_size": validation_size,
                    "step": validation_size,
                },
                "windows": rows,
                "aggregate_oos": _metrics_document(metrics) if metrics else None,
            },
            metrics,
        )

    def _stress(self, dataset, strategy, broker, cost) -> tuple[dict[str, object], bool]:
        rows = []
        passed = True
        for multiplier in (Decimal("1"), Decimal("1.25"), Decimal("1.50")):
            friction = friction_model(broker, cost, stress_multiplier=multiplier)
            assert friction is not None
            result = self.engine.run(dataset, strategy, friction)
            metrics = result.metrics
            row_passed = metrics.expectancy is not None and metrics.expectancy > 0
            passed = passed and row_passed
            rows.append(
                {
                    "cost_multiplier": multiplier,
                    "trade_count": metrics.trade_count,
                    "expectancy": metrics.expectancy,
                    "profit_factor": metrics.profit_factor,
                    "max_drawdown_r": metrics.maximum_drawdown_r,
                    "passed": row_passed,
                }
            )
        return {"passed": passed, "scenarios": rows}, passed


def _blocked_status(
    dataset: AcquiredDataset | None,
    acquisition_error: str | None,
    alignment: AlignmentResult | None,
    broker: BrokerEvidence | None,
    cost,
) -> tuple[QualificationStatus | None, str | None]:
    if dataset is None:
        return QualificationStatus.DATA_NOT_AVAILABLE, acquisition_error or "Dataset was not acquired."
    if alignment is not None and alignment.status.value == "MATERIAL_SOURCE_DIVERGENCE":
        return QualificationStatus.SOURCE_DIVERGENCE, alignment.reason
    if dataset.dataset.has_quality_failure:
        return QualificationStatus.DATA_QUALITY_FAIL, "Dataset contains explicitly detected missing-data gaps."
    if dataset.depth_status.value == "LOW_DATA_DEPTH":
        return QualificationStatus.LOW_DATA_DEPTH, "Dataset does not meet the reviewed timeframe depth target."
    if friction_model(broker, cost, stress_multiplier=Decimal("1")) is None:
        return (
            QualificationStatus.COST_MODEL_INCOMPLETE,
            "DQ-03 metadata and matching reviewed slippage/commission/session cost evidence are required.",
        )
    return None, None


def _blocked_entry(
    specification: InstrumentSpec,
    strategy: RuleStrategy,
    timeframe: Timeframe,
    dataset: AcquiredDataset | None,
    broker: BrokerEvidence | None,
    alignment: AlignmentResult | None,
    classification: QualificationStatus,
    reason: str | None,
) -> dict[str, object]:
    return _entry(
        specification,
        strategy,
        timeframe,
        dataset,
        broker,
        alignment,
        "PRE_SIMULATION_BLOCKED",
        classification,
        None,
        None,
        None,
        0,
        "No parameter selection: the combination is blocked before valid execution simulation.",
        False,
        [reason or classification.value],
    )


def _entry(
    specification: InstrumentSpec,
    strategy: RuleStrategy,
    timeframe: Timeframe,
    acquired: AcquiredDataset | None,
    broker: BrokerEvidence | None,
    alignment: AlignmentResult | None,
    evaluation_state: str,
    classification: QualificationStatus,
    metrics: PerformanceMetrics | None,
    validation: PerformanceMetrics | None,
    walk_forward: PerformanceMetrics | None,
    parameter_sets: int,
    selection_criteria: str,
    stress_passed: bool,
    reasons: list[str],
) -> dict[str, object]:
    document = _metrics_document(metrics)
    return {
        "instrument": specification.symbol,
        "ig_epic": broker.epic if broker else None,
        "asset_class": specification.asset_class.value,
        "strategy": strategy.definition.strategy_id,
        "strategy_version": strategy.definition.version,
        "strategy_description": strategy.definition.change_reason,
        "timeframe": timeframe.value,
        "parameters": dict(strategy.definition.parameters),
        "parameter_fingerprint": strategy.definition.configuration_fingerprint,
        "parameter_sets_evaluated": parameter_sets,
        "selection_criteria": selection_criteria,
        "data_source": acquired.provider if acquired else None,
        "dataset_fingerprint": acquired.dataset.dataset_fingerprint if acquired else None,
        "source_fingerprint": acquired.raw_source_fingerprint if acquired else None,
        "data_depth": acquired.depth_status.value if acquired else "DATA_NOT_AVAILABLE",
        "candle_count": len(acquired.dataset.candles) if acquired else 0,
        "ig_alignment": asdict(alignment) if alignment else None,
        "trade_count": document["trade_count"],
        "wins": document["wins"],
        "losses": document["losses"],
        "win_rate": document["win_rate"],
        "net_pips_or_ticks": document["net_pips_or_ticks"],
        "net_r": document["net_r"],
        "average_r": document["average_r"],
        "median_r": document["median_r"],
        "expectancy": document["expectancy"],
        "profit_factor": document["profit_factor"],
        "max_drawdown_r": document["maximum_drawdown_r"],
        "maximum_losing_streak": document["maximum_losing_streak"],
        "average_duration_seconds": document["average_trade_duration_seconds"],
        "exposure_seconds": document["exposure_seconds"],
        "spread_paid_r": document["spread_cost"],
        "slippage_estimate_r": document["slippage_cost"],
        "commission_cost_r": document["commission_cost"],
        "validation_expectancy": validation.expectancy if validation else None,
        "oos_expectancy": walk_forward.expectancy if walk_forward else None,
        "stress_test_passed": stress_passed,
        "evaluation_state": evaluation_state,
        "classification": classification.value,
        "why_rejected": reasons,
        "champion_challenger_rank": None,
        "demo_ready": classification is QualificationStatus.READY_FOR_DEMO_QUALIFICATION,
        "metric_breakdown": document.get("breakdown"),
    }


def _metrics_document(metrics: PerformanceMetrics | None) -> dict[str, object]:
    if metrics is None:
        return {
            "trade_count": 0,
            "wins": 0,
            "losses": 0,
            "win_rate": None,
            "net_pips_or_ticks": None,
            "net_r": None,
            "average_r": None,
            "median_r": None,
            "expectancy": None,
            "profit_factor": None,
            "maximum_drawdown_r": None,
            "maximum_losing_streak": 0,
            "average_trade_duration_seconds": None,
            "exposure_seconds": 0,
            "spread_cost": None,
            "slippage_cost": None,
            "commission_cost": None,
            "breakdown": None,
        }
    return {
        "trade_count": metrics.trade_count,
        "wins": metrics.wins,
        "losses": metrics.losses,
        "win_rate": metrics.win_rate,
        "net_pips_or_ticks": metrics.net_pips_or_ticks,
        "net_r": metrics.net_r,
        "average_r": metrics.average_r,
        "median_r": metrics.median_r,
        "expectancy": metrics.expectancy,
        "profit_factor": metrics.profit_factor,
        "maximum_drawdown_r": metrics.maximum_drawdown_r,
        "maximum_losing_streak": metrics.maximum_losing_streak,
        "average_trade_duration_seconds": metrics.average_trade_duration_seconds,
        "exposure_seconds": metrics.exposure_seconds,
        "spread_cost": metrics.spread_cost,
        "slippage_cost": metrics.slippage_cost,
        "commission_cost": metrics.commission_cost,
        "breakdown": {
            "session": metrics.by_session,
            "weekday": metrics.by_weekday,
            "month": metrics.by_month,
            "volatility_regime": metrics.by_volatility_regime,
        },
    }


def _blocked_walk_forward(
    symbol: str, strategy: str, timeframe: Timeframe, status: QualificationStatus, reason: str | None
) -> dict[str, object]:
    return {
        "instrument": symbol,
        "strategy": strategy,
        "timeframe": timeframe.value,
        "window_count": 0,
        "classification": status.value,
        "evaluation_state": "PRE_SIMULATION_BLOCKED",
        "reason": reason,
    }


def _blocked_stress_test(
    symbol: str, strategy: str, timeframe: Timeframe, status: QualificationStatus, reason: str | None
) -> dict[str, object]:
    return {
        "instrument": symbol,
        "strategy": strategy,
        "timeframe": timeframe.value,
        "passed": False,
        "classification": status.value,
        "evaluation_state": "PRE_SIMULATION_BLOCKED",
        "reason": reason,
        "scenarios": [],
    }


def _expectancy(metrics: PerformanceMetrics) -> Decimal:
    return metrics.expectancy if metrics.expectancy is not None else Decimal("-Infinity")


def _isolated_validation_peak(results) -> bool:
    values = sorted(_expectancy(result.metrics) for result in results)
    return len(values) >= 3 and values[-1] > 0 and values[-2] <= 0


def _rank_candidates(entries: list[dict[str, object]]) -> None:
    eligible = {
        QualificationStatus.CHAMPION_CANDIDATE.value,
        QualificationStatus.CHALLENGER.value,
        QualificationStatus.READY_FOR_DEMO_QUALIFICATION.value,
    }
    for symbol in SL02_VERIFIED_SYMBOLS:
        candidates = [entry for entry in entries if entry["instrument"] == symbol and entry["classification"] in eligible]
        ranked = sorted(candidates, key=lambda item: (_decimal_or_low(item["oos_expectancy"]), _decimal_or_low(item["expectancy"])), reverse=True)
        for number, entry in enumerate(ranked, start=1):
            entry["champion_challenger_rank"] = "CHAMPION_CANDIDATE" if number == 1 else f"CHALLENGER_{number - 1}"
            if number > 1 and entry["classification"] == QualificationStatus.CHAMPION_CANDIDATE.value:
                entry["classification"] = QualificationStatus.CHALLENGER.value


def _ranked_entries(entries: list[dict[str, object]]) -> list[dict[str, object]]:
    return sorted(
        entries,
        key=lambda item: (
            item["instrument"],
            _status_score(str(item["classification"])),
            _decimal_or_low(item["oos_expectancy"]),
            _decimal_or_low(item["expectancy"]),
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


def _simulation_state(classification: QualificationStatus) -> str:
    qualified = {
        QualificationStatus.RESEARCH_WATCH,
        QualificationStatus.CHALLENGER,
        QualificationStatus.CHAMPION_CANDIDATE,
        QualificationStatus.READY_FOR_DEMO_QUALIFICATION,
    }
    return "SIMULATED_AND_QUALIFIED" if classification in qualified else "SIMULATED_AND_FAILED"


def _evaluation_summary(entries: list[dict[str, object]]) -> dict[str, int]:
    return {
        "pre_simulation_blocked": sum(
            item.get("evaluation_state") == "PRE_SIMULATION_BLOCKED" for item in entries
        ),
        "simulated_and_failed": sum(
            item.get("evaluation_state") == "SIMULATED_AND_FAILED" for item in entries
        ),
        "simulated_and_qualified": sum(
            item.get("evaluation_state") == "SIMULATED_AND_QUALIFIED" for item in entries
        ),
    }


def _decimal_or_low(value: object) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal("-Infinity")


def _portfolio_document(evaluations: list[_Evaluation], specifications: dict[str, InstrumentSpec]) -> dict[str, object]:
    named_returns = {
        f"{item.entry['instrument']}-{item.entry['strategy']}-{item.entry['timeframe']}": item.returns
        for item in evaluations
        if item.returns
    }
    if not named_returns:
        return {
            "status": "DATA_NOT_AVAILABLE",
            "reason": "No cost-complete OOS simulations were available for portfolio correlation.",
            "correlations": {},
            "trade_time_overlap": {},
            "currency_concentration": {},
            "strategy_family_concentration": {},
            "session_concentration": {},
            "diversified_shortlist": [],
        }
    analysis = analyse_portfolio(named_returns, specifications)
    family_counts: dict[str, int] = {}
    for item in evaluations:
        family = str(item.entry["strategy"])
        if item.returns:
            family_counts[family] = family_counts.get(family, 0) + 1
    return {
        "status": "RESEARCH_ONLY",
        "correlations": analysis.correlations,
        "currency_concentration": analysis.currency_concentration,
        "asset_class_concentration": analysis.asset_class_concentration,
        "simultaneous_loss_count": analysis.simultaneous_loss_count,
        "diversification_score": analysis.diversification_score,
        "strategy_family_concentration": family_counts,
        "trade_time_overlap": _trade_time_overlap(evaluations),
        "session_concentration": "See per-result metric_breakdown.session; no portfolio claim is made from missing rows.",
        "diversified_shortlist": [],
    }


def _trade_time_overlap(evaluations: list[_Evaluation]) -> dict[str, int]:
    result: dict[str, int] = {}
    populated = [item for item in evaluations if item.intervals]
    for index, left in enumerate(populated):
        for right in populated[index + 1 :]:
            key = f"{left.entry['instrument']}-{left.entry['strategy']}|{right.entry['instrument']}-{right.entry['strategy']}"
            result[key] = sum(
                max(start_a, start_b) < min(end_a, end_b)
                for start_a, end_a in left.intervals
                for start_b, end_b in right.intervals
            )
    return result


def _safety_document() -> dict[str, object]:
    return {
        "ig_create_calls": 0,
        "ig_close_calls": 0,
        "live_calls": 0,
        "execution_authority": "OFF",
        "broker_order_mutation_available": False,
        "external_history_requests": "GET-only public provider requests; no IG endpoint is constructed.",
    }
