"""Offline orchestration for local Strategy Lab runs and batch evidence."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from src.ig_trader.strategy_lab.artifacts import LeaderboardEntry, write_artifacts
from src.ig_trader.strategy_lab.data import DataContractError, LocalDatasetSource
from src.ig_trader.strategy_lab.engine import (
    BacktestConfig,
    CandleBacktestEngine,
    FrictionModel,
    QualificationStatus,
    classify_result,
)
from src.ig_trader.strategy_lab.models import (
    INITIAL_INSTRUMENT_REGISTRY,
    INITIAL_INSTRUMENTS,
    InstrumentSpec,
    StrategyFamily,
    Timeframe,
    is_timeframe_compatible,
    suitable_families,
)
from src.ig_trader.strategy_lab.strategies import RuleStrategy, strategy_registry

ROOT = Path(__file__).resolve().parents[3]
DEFAULT_FIXTURE_DIRECTORY = ROOT / "fixtures" / "strategy_lab"
DEFAULT_ARTIFACT_DIRECTORY = ROOT / "artifacts" / "strategy_lab"


@dataclass(frozen=True)
class LabRun:
    entries: tuple[LeaderboardEntry, ...]
    data_quality: tuple[dict[str, object], ...]


class StrategyLabRunner:
    """Runs only local files; no broker, cloud, or external client is constructed."""

    def __init__(
        self,
        *,
        fixture_directory: Path = DEFAULT_FIXTURE_DIRECTORY,
        registry: dict[str, InstrumentSpec] = INITIAL_INSTRUMENT_REGISTRY,
    ) -> None:
        self.fixture_directory = fixture_directory
        self.registry = registry
        self.strategies = strategy_registry()
        self.engine = CandleBacktestEngine()

    def run_one(self, instrument: str, strategy_id: str, timeframe: Timeframe) -> LabRun:
        spec = self._instrument(instrument)
        strategy = self._strategy(strategy_id)
        family = strategy.definition.family
        if family is not StrategyFamily.S0_FROZEN_RSI_ADX and family not in suitable_families(spec):
            return self._unavailable(
                spec, strategy, timeframe, "SUITABILITY_MATRIX_EXCLUDES_COMBINATION"
            )
        if not is_timeframe_compatible(family, spec.asset_class, timeframe):
            return self._unavailable(
                spec, strategy, timeframe, "TIMEFRAME_MATRIX_EXCLUDES_COMBINATION"
            )
        fixture = (
            self.fixture_directory / f"{instrument.casefold()}_{timeframe.value.casefold()}.csv"
        )
        if not fixture.is_file():
            return self._unavailable(spec, strategy, timeframe, "LOCAL_DATASET_NOT_AVAILABLE")
        try:
            dataset = LocalDatasetSource().load(fixture)
        except DataContractError as error:
            return self._unavailable(spec, strategy, timeframe, f"DATA_CONTRACT_ERROR:{error}")
        result = self.engine.run(
            dataset, strategy, FrictionModel.from_instrument(spec), BacktestConfig()
        )
        status = classify_result(result)
        entry = LeaderboardEntry.from_result(
            result,
            instrument=spec.symbol,
            asset_class=spec.asset_class,
            timeframe=timeframe,
            status=status,
        )
        quality = {
            "instrument": spec.symbol,
            "timeframe": timeframe.value,
            "dataset_fingerprint": dataset.dataset_fingerprint,
            "source_fingerprint": dataset.source_fingerprint,
            "gap_count": len(dataset.gaps),
            "quality_status": "FAIL" if dataset.has_quality_failure else "PASS",
            "synthetic_rows": sum(candle.synthetic for candle in dataset.candles),
            "source_qualities": sorted({candle.source_quality.value for candle in dataset.candles}),
        }
        return LabRun((entry,), (quality,))

    def batch_initial(self) -> LabRun:
        """Evaluate only suitability/timeframe hypotheses with local data where present."""

        entries: list[LeaderboardEntry] = []
        quality: list[dict[str, object]] = []
        for spec in INITIAL_INSTRUMENTS:
            for family in suitable_families(spec):
                strategy = self.strategies[family.value]
                for timeframe in Timeframe:
                    if is_timeframe_compatible(family, spec.asset_class, timeframe):
                        run = self.run_one(spec.symbol, strategy.definition.strategy_id, timeframe)
                        entries.extend(run.entries)
                        quality.extend(run.data_quality)
        return LabRun(tuple(entries), tuple(quality))

    def write(
        self, run: LabRun, output_directory: Path = DEFAULT_ARTIFACT_DIRECTORY
    ) -> dict[str, Path]:
        return write_artifacts(
            output_directory,
            run.entries,
            data_quality=run.data_quality,
            run_metadata={
                "offline": True,
                "broker_order_calls": 0,
                "network_calls": 0,
                "execution_authority": "OFF",
            },
        )

    def _instrument(self, symbol: str) -> InstrumentSpec:
        try:
            return self.registry[symbol.upper()]
        except KeyError as error:
            raise ValueError(f"unknown research instrument: {symbol}") from error

    def _strategy(self, strategy_id: str) -> RuleStrategy:
        try:
            return self.strategies[strategy_id.upper()]
        except KeyError as error:
            raise ValueError(f"unknown strategy family: {strategy_id}") from error

    @staticmethod
    def _unavailable(
        spec: InstrumentSpec,
        strategy: RuleStrategy,
        timeframe: Timeframe,
        reason: str,
    ) -> LabRun:
        return LabRun(
            (
                LeaderboardEntry(
                    instrument=spec.symbol,
                    asset_class=spec.asset_class,
                    strategy=strategy.definition.strategy_id,
                    version=strategy.definition.version,
                    timeframe=timeframe,
                    trades=0,
                    win_rate=None,
                    net_r=None,
                    expectancy=None,
                    profit_factor=None,
                    max_drawdown=None,
                    oos_expectancy=None,
                    status=QualificationStatus.DATA_NOT_AVAILABLE,
                    dataset_fingerprint=None,
                    configuration_fingerprint=None,
                    strategy_fingerprint=strategy.definition.configuration_fingerprint,
                ),
            ),
            (
                {
                    "instrument": spec.symbol,
                    "timeframe": timeframe.value,
                    "quality_status": "DATA_NOT_AVAILABLE",
                    "reason": reason,
                },
            ),
        )
