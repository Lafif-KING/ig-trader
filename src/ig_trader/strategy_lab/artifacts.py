"""Versioned, machine-readable Strategy Lab evidence artifacts."""

from __future__ import annotations

import csv
import json
from collections.abc import Iterable
from dataclasses import asdict, dataclass, is_dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from src.ig_trader.strategy_lab.engine import BacktestResult, QualificationStatus
from src.ig_trader.strategy_lab.models import AssetClass, Timeframe

ARTIFACT_SCHEMA_VERSION = "strategy-lab-artifacts/1.0"
LEADERBOARD_COLUMNS = (
    "instrument",
    "asset_class",
    "strategy",
    "version",
    "timeframe",
    "trades",
    "win_rate",
    "net_r",
    "expectancy",
    "profit_factor",
    "max_drawdown",
    "oos_expectancy",
    "status",
)


@dataclass(frozen=True)
class LeaderboardEntry:
    instrument: str
    asset_class: AssetClass
    strategy: str
    version: str
    timeframe: Timeframe
    trades: int
    win_rate: float | None
    net_r: Decimal | None
    expectancy: Decimal | None
    profit_factor: Decimal | None
    max_drawdown: Decimal | None
    oos_expectancy: Decimal | None
    status: QualificationStatus
    dataset_fingerprint: str | None
    configuration_fingerprint: str | None
    strategy_fingerprint: str | None

    @classmethod
    def from_result(
        cls,
        result: BacktestResult,
        *,
        instrument: str,
        asset_class: AssetClass,
        timeframe: Timeframe,
        status: QualificationStatus | None = None,
        oos_expectancy: Decimal | None = None,
    ) -> LeaderboardEntry:
        metrics = result.metrics
        return cls(
            instrument=instrument,
            asset_class=asset_class,
            strategy=result.strategy.strategy_id,
            version=result.strategy.version,
            timeframe=timeframe,
            trades=metrics.trade_count,
            win_rate=metrics.win_rate,
            net_r=metrics.net_r,
            expectancy=metrics.expectancy,
            profit_factor=metrics.profit_factor,
            max_drawdown=metrics.maximum_drawdown_r,
            oos_expectancy=oos_expectancy,
            status=status or result.status,
            dataset_fingerprint=result.dataset_fingerprint,
            configuration_fingerprint=result.configuration_fingerprint,
            strategy_fingerprint=result.strategy.configuration_fingerprint,
        )


def write_artifacts(
    output_directory: Path,
    entries: Iterable[LeaderboardEntry],
    *,
    instrument_summary: dict[str, object] | None = None,
    strategy_summary: dict[str, object] | None = None,
    champion_challenger: Iterable[dict[str, object]] = (),
    data_quality: Iterable[dict[str, object]] = (),
    run_metadata: dict[str, object] | None = None,
) -> dict[str, Path]:
    """Write all required artifact files atomically enough for local research use."""

    output_directory.mkdir(parents=True, exist_ok=True)
    ranked = tuple(sorted(entries, key=_ranking_key, reverse=True))
    manifest = {
        "schema_version": ARTIFACT_SCHEMA_VERSION,
        "generated_at_utc": datetime.now(UTC).isoformat(),
        "entry_count": len(ranked),
        "run_metadata": run_metadata or {},
        "artifact_files": [
            "leaderboard.csv",
            "leaderboard.json",
            "instrument_summary.json",
            "strategy_summary.json",
            "rejections.json",
            "champion_challenger.json",
            "data_quality.json",
        ],
    }
    documents = {
        "run_manifest.json": manifest,
        "leaderboard.json": {"schema_version": ARTIFACT_SCHEMA_VERSION, "entries": ranked},
        "instrument_summary.json": instrument_summary or _default_instrument_summary(ranked),
        "strategy_summary.json": strategy_summary or _default_strategy_summary(ranked),
        "rejections.json": {
            "entries": [
                entry
                for entry in ranked
                if entry.status
                not in {QualificationStatus.CHALLENGER, QualificationStatus.CHAMPION_CANDIDATE}
            ]
        },
        "champion_challenger.json": {"comparisons": tuple(champion_challenger)},
        "data_quality.json": {"datasets": tuple(data_quality)},
    }
    paths: dict[str, Path] = {}
    for name, document in documents.items():
        path = output_directory / name
        rendered = json.dumps(document, default=_json_default, sort_keys=True, indent=2) + "\n"
        path.write_text(rendered, encoding="utf-8")
        paths[name] = path
    csv_path = output_directory / "leaderboard.csv"
    with csv_path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=LEADERBOARD_COLUMNS)
        writer.writeheader()
        for entry in ranked:
            writer.writerow(_csv_row(entry))
    paths["leaderboard.csv"] = csv_path
    return paths


def load_leaderboard(path: Path) -> tuple[dict[str, object], ...]:
    """Safely read local artifact output for the read-only dashboard."""

    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return ()
    entries = value.get("entries") if isinstance(value, dict) else None
    if not isinstance(entries, list):
        return ()
    return tuple(item for item in entries if isinstance(item, dict))


def _ranking_key(entry: LeaderboardEntry) -> tuple[int, Decimal, Decimal, int]:
    status_score = {
        QualificationStatus.CHAMPION_CANDIDATE: 5,
        QualificationStatus.CHALLENGER: 4,
        QualificationStatus.RESEARCH_WATCH: 3,
        QualificationStatus.LOW_SAMPLE_CONFIDENCE: 2,
    }.get(entry.status, 0)
    return (
        status_score,
        entry.oos_expectancy or Decimal("-999999"),
        entry.expectancy or Decimal("-999999"),
        entry.trades,
    )


def _default_instrument_summary(entries: tuple[LeaderboardEntry, ...]) -> dict[str, object]:
    return {
        "instrument_count": len({entry.instrument for entry in entries}),
        "dataset_status": {
            "available": sum(entry.dataset_fingerprint is not None for entry in entries),
            "not_available": sum(entry.dataset_fingerprint is None for entry in entries),
        },
    }


def _default_strategy_summary(entries: tuple[LeaderboardEntry, ...]) -> dict[str, object]:
    return {
        "strategies_tested": len({entry.strategy for entry in entries}),
        "combinations_tested": len(entries),
        "champion_candidates": sum(
            entry.status is QualificationStatus.CHAMPION_CANDIDATE for entry in entries
        ),
        "challengers": sum(entry.status is QualificationStatus.CHALLENGER for entry in entries),
        "rejected": sum(
            entry.status
            in {
                QualificationStatus.RESEARCH_REJECTED,
                QualificationStatus.NEGATIVE_EXPECTANCY,
                QualificationStatus.OVERFIT_RISK,
                QualificationStatus.UNSTABLE_ACROSS_PERIODS,
            }
            for entry in entries
        ),
        "insufficient_data": sum(
            entry.status
            in {
                QualificationStatus.DATA_NOT_AVAILABLE,
                QualificationStatus.DATA_QUALITY_FAIL,
                QualificationStatus.COST_MODEL_INCOMPLETE,
                QualificationStatus.INSUFFICIENT_TRADES,
            }
            for entry in entries
        ),
    }


def _csv_row(entry: LeaderboardEntry) -> dict[str, object]:
    value = asdict(entry)
    return {
        column: _csv_value(value[column]) if column in value else ""
        for column in LEADERBOARD_COLUMNS
    }


def _csv_value(value: object) -> object:
    if value is None or isinstance(value, str | int | float):
        return value
    return _json_default(value)


def _json_default(value: object) -> object:
    if is_dataclass(value):
        return asdict(value)
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (AssetClass, Timeframe, QualificationStatus)):
        return value.value
    if isinstance(value, datetime):
        return value.isoformat()
    if hasattr(value, "value"):
        return value.value
    raise TypeError(f"not JSON serializable: {type(value)!r}")
