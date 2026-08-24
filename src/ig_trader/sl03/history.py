"""Provider-neutral, cache-first SL-03 external research history adapters."""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Protocol

from src.ig_trader.sl02.contracts import DatasetDepthStatus
from src.ig_trader.sl02.history import ExternalHistoryUnavailable, YahooFinanceHistorySource
from src.ig_trader.strategy_lab.data import (
    TIMEFRAME_INTERVALS,
    CanonicalDataset,
    DataContractError,
    GapClassification,
    LabCandle,
    SourceQuality,
    build_dataset,
)
from src.ig_trader.strategy_lab.models import AssetClass, Timeframe

DUKASCOPY_ENDPOINT_DESCRIPTION = (
    "https://freeserv.dukascopy.com/2.0/?path=api/historicalPrices "
    "(structured historical-prices API; local cache only in SL-03)"
)


@dataclass(frozen=True)
class ProviderProvenance:
    provider: str
    provider_symbol: str
    acquisition_timestamp_utc: datetime
    source_url: str
    raw_source_fingerprint: str
    normalized_fingerprint: str
    license_source_note: str
    parent_dataset_fingerprint: str | None = None


@dataclass(frozen=True)
class ResearchDataset:
    dataset: CanonicalDataset
    provenance: ProviderProvenance
    depth_status: DatasetDepthStatus
    cached: bool


class ResearchHistorySource(Protocol):
    def load(
        self, symbol: str, timeframe: Timeframe, asset_class: AssetClass
    ) -> ResearchDataset: ...


class CachedYahooResearchSource:
    """Read only an explicitly supplied SL-02 public cache; never downloads Yahoo."""

    def __init__(self, cache_directory: Path) -> None:
        self.cache_directory = cache_directory
        self._source = YahooFinanceHistorySource(cache_directory)

    def load(self, symbol: str, timeframe: Timeframe, asset_class: AssetClass) -> ResearchDataset:
        del asset_class
        required = Timeframe.H1 if timeframe is Timeframe.H4 else timeframe
        cache_path = self.cache_directory / f"{symbol.lower()}_{required.value.lower()}_yahoo.json"
        if not cache_path.is_file():
            raise ExternalHistoryUnavailable(
                "SL-03 cache-only policy: public Yahoo cache missing for "
                f"{symbol} {required.value}."
            )
        acquired = self._source.load(symbol, timeframe)
        return ResearchDataset(
            dataset=acquired.dataset,
            provenance=ProviderProvenance(
                provider=acquired.provider,
                provider_symbol=acquired.provider_symbol,
                acquisition_timestamp_utc=acquired.acquisition_timestamp_utc,
                source_url=acquired.source_url,
                raw_source_fingerprint=acquired.raw_source_fingerprint,
                normalized_fingerprint=acquired.dataset.dataset_fingerprint,
                license_source_note=(
                    "Public Yahoo chart payload cached by SL-02; external research history, "
                    "not IG broker candles."
                ),
                parent_dataset_fingerprint=(
                    acquired.dataset.source_fingerprint if timeframe is Timeframe.H4 else None
                ),
            ),
            depth_status=acquired.depth_status,
            cached=True,
        )


class DukascopyStructuredCacheSource:
    """Read an approved structured Dukascopy cache when an operator provides one.

    The official historical-prices API supports minute bars, but SL-03 does not
    embed a key or scrape pages.  This adapter intentionally consumes only local
    raw JSON evidence and can deterministically derive 5M/15M/1H/4H datasets.
    """

    provider = "DUKASCOPY_STRUCTURED_HISTORY"

    def __init__(self, cache_directory: Path) -> None:
        self.cache_directory = cache_directory

    def available(self, symbol: str) -> bool:
        return (self.cache_directory / f"{symbol.lower()}_1m_dukascopy.json").is_file()

    def load(self, symbol: str, timeframe: Timeframe, asset_class: AssetClass) -> ResearchDataset:
        if asset_class not in {AssetClass.FX, AssetClass.METAL}:
            raise ExternalHistoryUnavailable(
                f"Dukascopy structured cache is not approved for {asset_class.value} {symbol}."
            )
        path = self.cache_directory / f"{symbol.lower()}_1m_dukascopy.json"
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
            rows = document["candles"]
            acquired_at = _timestamp(document["acquired_at_utc"])
            provider_symbol = str(document["provider_symbol"])
        except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError) as error:
            raise ExternalHistoryUnavailable(
                f"Invalid structured Dukascopy cache for {symbol}; no substitute was invented."
            ) from error
        if acquired_at is None or not isinstance(rows, list):
            raise ExternalHistoryUnavailable(
                f"Invalid structured Dukascopy provenance for {symbol}."
            )
        raw = json.dumps(document, sort_keys=True, separators=(",", ":")).encode("utf-8")
        minute = build_dataset(
            (_candle(symbol, row) for row in rows),
            source_documents=(raw,),
        )
        dataset = minute if timeframe is Timeframe.M1 else resample_complete(minute, timeframe)
        return ResearchDataset(
            dataset=dataset,
            provenance=ProviderProvenance(
                provider=self.provider,
                provider_symbol=provider_symbol,
                acquisition_timestamp_utc=acquired_at,
                source_url=str(document.get("source_url", DUKASCOPY_ENDPOINT_DESCRIPTION)),
                raw_source_fingerprint=hashlib.sha256(raw).hexdigest(),
                normalized_fingerprint=dataset.dataset_fingerprint,
                license_source_note=str(
                    document.get(
                        "license_source_note",
                        "Structured Dukascopy history; external research data, not IG candles.",
                    )
                ),
                parent_dataset_fingerprint=minute.dataset_fingerprint
                if timeframe is not Timeframe.M1
                else None,
            ),
            depth_status=_depth_status(dataset),
            cached=True,
        )


class MultiSourceHistory:
    """Prefer valid deep structured evidence, otherwise use cached Yahoo honestly."""

    def __init__(self, *, dukascopy_cache: Path, yahoo_cache: Path) -> None:
        self.dukascopy = DukascopyStructuredCacheSource(dukascopy_cache)
        self.yahoo = CachedYahooResearchSource(yahoo_cache)

    def load(self, symbol: str, timeframe: Timeframe, asset_class: AssetClass) -> ResearchDataset:
        if self.dukascopy.available(symbol) and asset_class in {AssetClass.FX, AssetClass.METAL}:
            return self.dukascopy.load(symbol, timeframe, asset_class)
        return self.yahoo.load(symbol, timeframe, asset_class)


def resample_complete(dataset: CanonicalDataset, target: Timeframe) -> CanonicalDataset:
    """Aggregate only complete source buckets and preserve parent provenance."""

    source_interval = TIMEFRAME_INTERVALS[dataset.timeframe]
    target_interval = TIMEFRAME_INTERVALS[target]
    if target_interval <= source_interval or target_interval % source_interval:
        raise DataContractError("SL-03 resampling requires an integral larger target interval")
    expected = int(target_interval / source_interval)
    grouped: dict[datetime, list[LabCandle]] = defaultdict(list)
    for candle in dataset.candles:
        seconds = int(candle.timestamp_utc.timestamp())
        bucket_seconds = int(target_interval.total_seconds())
        bucket = datetime.fromtimestamp(seconds - seconds % bucket_seconds, tz=UTC)
        grouped[bucket].append(candle)
    candles: list[LabCandle] = []
    for bucket, members in sorted(grouped.items()):
        ordered = tuple(sorted(members, key=lambda item: item.timestamp_utc))
        expected_timestamps = tuple(bucket + source_interval * index for index in range(expected))
        timestamps = tuple(item.timestamp_utc for item in ordered)
        if len(ordered) != expected or timestamps != expected_timestamps:
            continue
        candles.append(
            LabCandle(
                instrument=dataset.instrument,
                timestamp_utc=bucket,
                timeframe=target,
                open=ordered[0].open,
                high=max(item.high for item in ordered),
                low=min(item.low for item in ordered),
                close=ordered[-1].close,
                spread=ordered[-1].spread,
                volume=(
                    sum((item.volume for item in ordered if item.volume is not None), Decimal("0"))
                    if all(item.volume is not None for item in ordered)
                    else None
                ),
                source=ordered[0].source,
                source_quality=ordered[0].source_quality,
                gap_classification=GapClassification.NONE,
                synthetic=False,
            )
        )
    if not candles:
        raise DataContractError("No complete target-timeframe buckets were available")
    return build_dataset(
        candles,
        source_documents=(
            dataset.source_fingerprint,
            dataset.dataset_fingerprint,
            f"sl03-complete-resample/{dataset.timeframe.value}-to-{target.value}",
        ),
    )


def _candle(symbol: str, row: object) -> LabCandle:
    if not isinstance(row, dict):
        raise DataContractError("Structured provider row must be an object")
    try:
        timestamp = _timestamp(row["timestamp_utc"])
        if timestamp is None:
            raise ValueError("timestamp required")
        return LabCandle(
            instrument=symbol,
            timestamp_utc=timestamp,
            timeframe=Timeframe.M1,
            open=Decimal(str(row["open"])),
            high=Decimal(str(row["high"])),
            low=Decimal(str(row["low"])),
            close=Decimal(str(row["close"])),
            spread=_decimal(row.get("spread")),
            volume=_decimal(row.get("volume")),
            source=DukascopyStructuredCacheSource.provider,
            source_quality=SourceQuality.EXTERNAL_UNVERIFIED,
            gap_classification=GapClassification.NONE,
            synthetic=False,
        )
    except (KeyError, InvalidOperation, TypeError, ValueError) as error:
        raise DataContractError("Structured provider row is invalid") from error


def _depth_status(dataset: CanonicalDataset) -> DatasetDepthStatus:
    minimum_days = {
        Timeframe.H4: 365,
        Timeframe.H1: 180,
        Timeframe.M15: 90,
        Timeframe.M5: 60,
        Timeframe.M1: 60,
    }[dataset.timeframe]
    duration = dataset.candles[-1].timestamp_utc - dataset.candles[0].timestamp_utc
    return (
        DatasetDepthStatus.SUFFICIENT
        if duration >= timedelta(days=minimum_days)
        else DatasetDepthStatus.LOW_DATA_DEPTH
    )


def _timestamp(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)
    except ValueError:
        return None


def _decimal(value: object) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
