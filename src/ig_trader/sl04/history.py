"""SL-04 deterministic offline research source selection and local export ingestion."""

from __future__ import annotations

import csv
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

from src.ig_trader.sl02.contracts import DatasetDepthStatus
from src.ig_trader.sl02.history import ExternalHistoryUnavailable
from src.ig_trader.sl03.history import (
    CachedYahooResearchSource,
    ProviderProvenance,
    ResearchDataset,
)
from src.ig_trader.strategy_lab.data import DataContractError, build_dataset
from src.ig_trader.strategy_lab.models import AssetClass, Timeframe

from .dukascopy import _canonical_symbol, _mid_candle, _parse_side_candle
from .local_csv import LocalDukascopyGoCsvSource

EXPORT_PROVIDER = "DUKASCOPY_OFFICIAL_HISTORICAL_EXPORT"
EXPORT_SCHEMA = "strategy-lab-sl04-dukascopy-export/1.0"


class DukascopyHistoricalExportSource:
    """Read a manual official-export file through a strict, documented local contract.

    Supported files are ``symbol_1m_dukascopy_export.json`` and
    ``symbol_1m_dukascopy_export.csv``.  Each row needs a UTC timestamp plus
    bid/ask OHLC fields.  The adapter rejects one-sided, ambiguous, synthetic,
    or non-UTC data instead of inferring values from an export UI.
    """

    def __init__(self, export_directory: Path) -> None:
        self.export_directory = export_directory

    def available(self, symbol: str) -> bool:
        return any(path.is_file() for path in self._paths(symbol))

    def load(self, symbol: str, timeframe: Timeframe, asset_class: AssetClass) -> ResearchDataset:
        if asset_class not in {AssetClass.FX, AssetClass.METAL}:
            raise ExternalHistoryUnavailable(f"DUKASCOPY_EXPORT_UNSUPPORTED:{symbol}")
        path = next((item for item in self._paths(symbol) if item.is_file()), None)
        if path is None:
            raise ExternalHistoryUnavailable(f"DUKASCOPY_EXPORT_NOT_AVAILABLE:{symbol}")
        raw = path.read_bytes()
        document, rows = self._parse(path, raw)
        provider_symbol = document.get("provider_symbol")
        if not isinstance(provider_symbol, str) or _canonical_symbol(provider_symbol) != symbol:
            raise DataContractError(f"DUKASCOPY_EXPORT_SYMBOL_MISMATCH:{symbol}")
        bid: dict[datetime, object] = {}
        ask: dict[datetime, object] = {}
        for row in rows:
            bid_candle = _parse_side_candle(row, "B")
            ask_candle = _parse_side_candle(row, "A")
            if bid_candle.timestamp_utc in bid or ask_candle.timestamp_utc in ask:
                raise DataContractError("DUKASCOPY_EXPORT_DUPLICATE_TIMESTAMP")
            bid[bid_candle.timestamp_utc] = bid_candle
            ask[ask_candle.timestamp_utc] = ask_candle
        if tuple(bid) != tuple(ask):
            raise DataContractError("DUKASCOPY_EXPORT_BID_ASK_TIMESTAMP_MISMATCH")
        candles = tuple(
            _mid_candle(symbol, Timeframe.M1, bid[timestamp], ask[timestamp])
            for timestamp in sorted(bid)
        )
        if not candles:
            raise DataContractError("DUKASCOPY_EXPORT_EMPTY")
        minute = build_dataset(candles, source_documents=(raw,))
        dataset = minute
        if timeframe is not Timeframe.M1:
            from src.ig_trader.sl03.history import resample_complete

            dataset = resample_complete(minute, timeframe)
        acquired = datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)
        return ResearchDataset(
            dataset=dataset,
            provenance=ProviderProvenance(
                provider=EXPORT_PROVIDER,
                provider_symbol=provider_symbol,
                acquisition_timestamp_utc=acquired,
                source_url="LOCAL_OFFICIAL_DUKASCOPY_HISTORICAL_EXPORT",
                raw_source_fingerprint=hashlib.sha256(raw).hexdigest(),
                normalized_fingerprint=dataset.dataset_fingerprint,
                license_source_note=(
                    "Locally supplied Dukascopy Historical Data Export; external research "
                    "history, not IG candles."
                ),
                parent_dataset_fingerprint=(
                    minute.dataset_fingerprint if timeframe is not Timeframe.M1 else None
                ),
            ),
            depth_status=_depth_status(dataset),
            cached=True,
        )

    def _paths(self, symbol: str) -> tuple[Path, Path]:
        stem = f"{symbol.lower()}_1m_dukascopy_export"
        return self.export_directory / f"{stem}.json", self.export_directory / f"{stem}.csv"

    def _parse(self, path: Path, raw: bytes) -> tuple[dict[str, object], list[dict[str, object]]]:
        if path.suffix.lower() == ".json":
            try:
                document = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as error:
                raise DataContractError("DUKASCOPY_EXPORT_JSON_INVALID") from error
            if not isinstance(document, dict) or document.get("schema_version") != EXPORT_SCHEMA:
                raise DataContractError("DUKASCOPY_EXPORT_SCHEMA_INVALID")
            rows = document.get("candles")
            if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
                raise DataContractError("DUKASCOPY_EXPORT_ROWS_INVALID")
            return document, [dict(row) for row in rows]
        try:
            parsed = list(csv.DictReader(raw.decode("utf-8").splitlines()))
        except (UnicodeDecodeError, csv.Error) as error:
            raise DataContractError("DUKASCOPY_EXPORT_CSV_INVALID") from error
        if not parsed or not all(isinstance(row, dict) for row in parsed):
            raise DataContractError("DUKASCOPY_EXPORT_ROWS_INVALID")
        provider_symbol = parsed[0].get("provider_symbol")
        if not isinstance(provider_symbol, str) or not provider_symbol.strip():
            raise DataContractError("DUKASCOPY_EXPORT_PROVIDER_SYMBOL_REQUIRED")
        return {"schema_version": EXPORT_SCHEMA, "provider_symbol": provider_symbol}, [
            dict(row) for row in parsed
        ]


class SL04SourcePriority:
    """One coherent source per dataset, selected without outbound acquisition."""

    def __init__(
        self,
        *,
        local_csv: LocalDukascopyGoCsvSource,
        export_directory: Path,
        yahoo_cache_directory: Path,
    ) -> None:
        self.local_csv = local_csv
        self.exports = DukascopyHistoricalExportSource(export_directory)
        self.yahoo = CachedYahooResearchSource(yahoo_cache_directory)

    def load(self, symbol: str, timeframe: Timeframe, asset_class: AssetClass) -> ResearchDataset:
        if asset_class in {AssetClass.FX, AssetClass.METAL}:
            try:
                return self.local_csv.load(symbol, timeframe, asset_class)
            except (DataContractError, ExternalHistoryUnavailable):
                if self.exports.available(symbol):
                    return self.exports.load(symbol, timeframe, asset_class)
        return self.yahoo.load(symbol, timeframe, asset_class)


def _depth_status(dataset) -> DatasetDepthStatus:
    from src.ig_trader.sl04.dukascopy import _depth_status as canonical_depth_status

    return canonical_depth_status(dataset)
