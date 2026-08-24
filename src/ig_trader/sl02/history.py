"""Cached, structured external OHLCV acquisition for SL-02 research only."""

from __future__ import annotations

import hashlib
import json
import subprocess
from collections import Counter
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from src.ig_trader.sl02.contracts import AcquiredDataset, DatasetDepthStatus
from src.ig_trader.strategy_lab.data import (
    DataContractError,
    GapClassification,
    LabCandle,
    SourceQuality,
    TIMEFRAME_INTERVALS,
    build_dataset,
)
from src.ig_trader.strategy_lab.models import Timeframe

PROVIDER = "YAHOO_FINANCE_CHART_UNOFFICIAL"
CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"

# Every mapping is explicit. Metals and indices are proxies, not claims that a
# Yahoo contract is the same as an IG CFD; qualification blocks on that fact.
YAHOO_SYMBOLS: dict[str, str] = {
    "EURUSD": "EURUSD=X",
    "GBPUSD": "GBPUSD=X",
    "EURGBP": "EURGBP=X",
    "USDJPY": "JPY=X",
    "EURJPY": "EURJPY=X",
    "GBPJPY": "GBPJPY=X",
    "AUDUSD": "AUDUSD=X",
    "NZDUSD": "NZDUSD=X",
    "USDCAD": "CAD=X",
    "USDCHF": "CHF=X",
    "EURCHF": "EURCHF=X",
    "EURAUD": "EURAUD=X",
    "GBPAUD": "GBPAUD=X",
    "AUDJPY": "AUDJPY=X",
    "CADJPY": "CADJPY=X",
    "CHFJPY": "CHFJPY=X",
    "XAUUSD": "GC=F",
    "XAGUSD": "SI=F",
    "US500": "^GSPC",
    "USTECH100": "^NDX",
}

_API_INTERVALS = {Timeframe.H1: "1h", Timeframe.M15: "15m", Timeframe.M5: "5m"}
_API_RANGES = {Timeframe.H1: "2y", Timeframe.M15: "60d", Timeframe.M5: "60d"}
_MINIMUM_DAYS = {
    Timeframe.H4: 365,
    Timeframe.H1: 180,
    Timeframe.M15: 90,
    Timeframe.M5: 60,
}


class ExternalHistoryUnavailable(RuntimeError):
    """A source failure that must be recorded rather than replaced with invented data."""


class YahooFinanceHistorySource:
    """Structured JSON adapter with a local raw-payload cache and bounded GET requests."""

    def __init__(self, cache_directory: Path, *, timeout_seconds: float = 30.0) -> None:
        self.cache_directory = cache_directory
        self.timeout_seconds = timeout_seconds

    def load(self, symbol: str, timeframe: Timeframe) -> AcquiredDataset:
        if symbol not in YAHOO_SYMBOLS:
            raise ExternalHistoryUnavailable(f"No approved SL-02 source mapping for {symbol}.")
        if timeframe is Timeframe.H4:
            hourly = self.load(symbol, Timeframe.H1)
            dataset = _resample_h4(hourly.dataset)
            return AcquiredDataset(
                dataset,
                hourly.provider,
                hourly.provider_symbol,
                hourly.acquisition_timestamp_utc,
                hourly.source_url,
                hourly.raw_source_fingerprint,
                hourly.cached,
                _depth_status(dataset),
            )
        if timeframe not in _API_INTERVALS:
            raise ExternalHistoryUnavailable(f"{timeframe.value} is intentionally not scheduled in SL-02.")
        path = self.cache_directory / _cache_name(symbol, timeframe)
        cached = self._read_cache(path)
        if cached is not None:
            payload, acquired_at, source_url = cached
            return self._decode(symbol, timeframe, payload, acquired_at, source_url, cached=True)
        payload, source_url = self._download(symbol, timeframe)
        acquired_at = datetime.now(UTC)
        self.cache_directory.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "schema_version": "sl02-yahoo-cache/1.0",
                    "provider": PROVIDER,
                    "acquired_at_utc": acquired_at.isoformat(),
                    "source_url": source_url,
                    "payload": payload,
                },
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        return self._decode(symbol, timeframe, payload, acquired_at, source_url, cached=False)

    def _download(self, symbol: str, timeframe: Timeframe) -> tuple[dict[str, Any], str]:
        ticker = YAHOO_SYMBOLS[symbol]
        url = CHART_URL.format(ticker=ticker)
        try:
            source_url = f"{url}?{urlencode({'interval': _API_INTERVALS[timeframe], 'range': _API_RANGES[timeframe]})}"
            response = subprocess.run(
                [
                    "curl.exe",
                    "--fail",
                    "--silent",
                    "--show-error",
                    "--max-time",
                    str(int(self.timeout_seconds)),
                    "--user-agent",
                    "ig-trader-sl02-research/1.0",
                    source_url,
                ],
                check=False,
                capture_output=True,
                timeout=self.timeout_seconds + 5,
            )
            if response.returncode != 0:
                message = response.stderr.decode("utf-8", errors="replace").strip()
                raise ExternalHistoryUnavailable(message or f"curl exited {response.returncode}")
            payload = json.loads(response.stdout.decode("utf-8"))
        except (OSError, UnicodeDecodeError, ValueError, subprocess.TimeoutExpired) as error:
            raise ExternalHistoryUnavailable(f"{symbol} {timeframe.value} download failed: {error}") from error
        if not isinstance(payload, dict):
            raise ExternalHistoryUnavailable(f"{symbol} {timeframe.value} source response is not an object.")
        return payload, source_url

    def _read_cache(self, path: Path) -> tuple[dict[str, Any], datetime, str] | None:
        try:
            cached = json.loads(path.read_text(encoding="utf-8"))
            payload = cached["payload"]
            acquired_at = datetime.fromisoformat(cached["acquired_at_utc"].replace("Z", "+00:00"))
            source_url = cached["source_url"]
        except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError):
            return None
        if not isinstance(payload, dict) or not isinstance(source_url, str):
            return None
        return payload, acquired_at.astimezone(UTC), source_url

    def _decode(
        self,
        symbol: str,
        timeframe: Timeframe,
        payload: dict[str, Any],
        acquired_at: datetime,
        source_url: str,
        *,
        cached: bool,
    ) -> AcquiredDataset:
        raw_document = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        candles = _candles_from_payload(symbol, timeframe, payload)
        dataset = build_dataset(candles, source_documents=(raw_document,))
        return AcquiredDataset(
            dataset,
            PROVIDER,
            YAHOO_SYMBOLS[symbol],
            acquired_at,
            source_url,
            hashlib.sha256(raw_document).hexdigest(),
            cached,
            _depth_status(dataset),
        )


def _candles_from_payload(
    symbol: str, timeframe: Timeframe, payload: dict[str, Any]
) -> tuple[LabCandle, ...]:
    chart = payload.get("chart")
    results = chart.get("result") if isinstance(chart, dict) else None
    if not isinstance(results, list) or len(results) != 1 or not isinstance(results[0], dict):
        error = chart.get("error") if isinstance(chart, dict) else None
        raise ExternalHistoryUnavailable(f"{symbol} {timeframe.value} source rejected request: {error}")
    result = results[0]
    timestamps = result.get("timestamp")
    quote_rows = result.get("indicators", {}).get("quote")
    if not isinstance(timestamps, list) or not isinstance(quote_rows, list) or len(quote_rows) != 1:
        raise ExternalHistoryUnavailable(f"{symbol} {timeframe.value} source response lacks OHLC arrays.")
    quote = quote_rows[0]
    if not isinstance(quote, dict) or any(
        not isinstance(quote.get(field), list) for field in ("open", "high", "low", "close", "volume")
    ):
        raise ExternalHistoryUnavailable(f"{symbol} {timeframe.value} source OHLC shape is invalid.")
    if any(len(quote[field]) != len(timestamps) for field in ("open", "high", "low", "close", "volume")):
        raise ExternalHistoryUnavailable(f"{symbol} {timeframe.value} source OHLC lengths differ.")
    parsed: list[tuple[datetime, Decimal, Decimal, Decimal, Decimal, Decimal | None]] = []
    for index, value in enumerate(timestamps):
        try:
            timestamp = datetime.fromtimestamp(int(value), UTC)
        except (OSError, TypeError, ValueError):
            raise ExternalHistoryUnavailable(f"{symbol} {timeframe.value} has an invalid timestamp.") from None
        ohlc = tuple(_decimal(quote[field][index]) for field in ("open", "high", "low", "close"))
        if any(item is None for item in ohlc):
            continue
        parsed.append((timestamp, *ohlc, _decimal(quote["volume"][index])))
    if not parsed:
        raise ExternalHistoryUnavailable(f"{symbol} {timeframe.value} has no valid OHLC rows.")
    parsed_timestamps = tuple(item[0] for item in parsed)
    if parsed_timestamps != tuple(sorted(parsed_timestamps)):
        raise DataContractError("external source returned out-of-order timestamps")
    if len(set(parsed_timestamps)) != len(parsed_timestamps):
        raise DataContractError("external source returned duplicate timestamps")
    classifications = _gap_classifications(parsed_timestamps, timeframe)
    return tuple(
        LabCandle(
            instrument=symbol,
            timestamp_utc=timestamp,
            timeframe=timeframe,
            open=open_price,
            high=high,
            low=low,
            close=close,
            spread=None,
            volume=volume,
            source=f"{PROVIDER}:{YAHOO_SYMBOLS[symbol]}",
            source_quality=SourceQuality.EXTERNAL_UNVERIFIED,
            gap_classification=classifications[index],
            synthetic=False,
        )
        for index, (timestamp, open_price, high, low, close, volume) in enumerate(parsed)
    )


def _gap_classifications(
    timestamps: tuple[datetime, ...], timeframe: Timeframe
) -> tuple[GapClassification, ...]:
    interval = TIMEFRAME_INTERVALS[timeframe]
    transitions = tuple(zip(timestamps, timestamps[1:], strict=False))
    recurring = Counter(
        (before.weekday(), before.hour, after.weekday(), after.hour)
        for before, after in transitions
        if after - before > interval
        and before.weekday() < 5
        and after.weekday() < 5
    )
    values = [GapClassification.NONE]
    for before, after in transitions:
        difference = after - before
        weekend = before.weekday() == 4 and after.weekday() in {6, 0}
        recurring_session = recurring[(before.weekday(), before.hour, after.weekday(), after.hour)] >= 2
        values.append(
            GapClassification.WEEKEND_OR_SESSION
            if difference > interval and (weekend or recurring_session)
            else GapClassification.NONE
        )
    return tuple(values)


def _resample_h4(dataset) -> object:
    candles = dataset.candles
    groups: dict[datetime, list[LabCandle]] = {}
    for candle in candles:
        bucket = candle.timestamp_utc.replace(hour=(candle.timestamp_utc.hour // 4) * 4)
        groups.setdefault(bucket, []).append(candle)
    resampled: list[LabCandle] = []
    for bucket in sorted(groups):
        group = sorted(groups[bucket], key=lambda item: item.timestamp_utc)
        expected = tuple(bucket + timedelta(hours=number) for number in range(4))
        if tuple(item.timestamp_utc for item in group) != expected:
            continue
        volumes = tuple(item.volume for item in group)
        resampled.append(
            LabCandle(
                instrument=dataset.instrument,
                timestamp_utc=bucket,
                timeframe=Timeframe.H4,
                open=group[0].open,
                high=max(item.high for item in group),
                low=min(item.low for item in group),
                close=group[-1].close,
                spread=None,
                volume=sum(volumes, Decimal("0")) if all(item is not None for item in volumes) else None,
                source=group[0].source,
                source_quality=group[0].source_quality,
                gap_classification=group[0].gap_classification,
                synthetic=False,
            )
        )
    if not resampled:
        raise ExternalHistoryUnavailable(f"{dataset.instrument} has no complete H4 aggregation windows.")
    raw_fingerprint = dataset.source_fingerprint
    return build_dataset(resampled, source_documents=(raw_fingerprint,))


def _depth_status(dataset) -> DatasetDepthStatus:
    minimum_days = _MINIMUM_DAYS[dataset.timeframe]
    span = dataset.candles[-1].timestamp_utc - dataset.candles[0].timestamp_utc
    return DatasetDepthStatus.SUFFICIENT if span >= timedelta(days=minimum_days) else DatasetDepthStatus.LOW_DATA_DEPTH


def _decimal(value: object) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _cache_name(symbol: str, timeframe: Timeframe) -> str:
    return f"{symbol.casefold()}_{timeframe.value.casefold()}_yahoo.json"
