"""Official Dukascopy structured-history adapter for offline research only.

The adapter uses only documented safe GET requests to the Dukascopy Trading
Tools API.  It never emits an API key, stores an API key, or constructs a
broker-order request.  Cached source documents contain sanitized provider data
and request metadata without credentials, enabling deterministic resume.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from tempfile import NamedTemporaryFile

import httpx

from src.ig_trader.sl02.contracts import DatasetDepthStatus
from src.ig_trader.sl02.history import ExternalHistoryUnavailable
from src.ig_trader.sl03.history import ProviderProvenance, ResearchDataset, resample_complete
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

DUKASCOPY_BASE_URL = "https://freeserv.dukascopy.com/2.0/"
INSTRUMENTS_PATH = "api/instruments"
HISTORICAL_PRICES_PATH = "api/historicalPrices"
PROVIDER = "DUKASCOPY_OFFICIAL_STRUCTURED_HISTORY"
CACHE_SCHEMA = "strategy-lab-sl04-dukascopy-cache/1.0"
NORMALIZED_SCHEMA = "strategy-lab-sl04-dukascopy-normalized/1.0"
MAX_PAGE_ROWS = 5000
_API_TIMEFRAMES = {Timeframe.M1: "1min", Timeframe.H1: "1hour"}


class DukascopyAccessError(ExternalHistoryUnavailable):
    """A safe-to-report provider-access failure without a credential or URL."""


class DukascopyApiKeyRequired(DukascopyAccessError):
    """Raised only when provider authentication is required and no key exists."""


@dataclass
class AcquisitionAccounting:
    """Counters for one bounded SL-04 acquisition attempt."""

    provider_get_count: int = 0
    retry_count: int = 0
    rate_limit_responses: int = 0
    cache_hits: int = 0
    new_downloads: int = 0
    downloaded_rows: int = 0
    normalized_rows: int = 0
    derived_rows: int = 0
    bytes_downloaded: int = 0

    def document(self, *, runtime_seconds: float) -> dict[str, object]:
        return {
            "provider_get_count": self.provider_get_count,
            "retry_count": self.retry_count,
            "rate_limit_responses": self.rate_limit_responses,
            "cache_hits": self.cache_hits,
            "new_downloads": self.new_downloads,
            "downloaded_rows": self.downloaded_rows,
            "normalized_rows": self.normalized_rows,
            "derived_rows": self.derived_rows,
            "bytes_downloaded": self.bytes_downloaded,
            "runtime_seconds": runtime_seconds,
        }


@dataclass(frozen=True)
class DukascopyInstrument:
    """An instrument ID resolved from an official provider response."""

    canonical_symbol: str
    provider_id: int
    provider_symbol: str
    provider_name: str | None
    resolver_fingerprint: str


@dataclass(frozen=True)
class DukascopyPreflight:
    """Bounded official-provider capability evidence, never a credential record."""

    auth_mode: str
    key_present: bool
    instrument_resolution_count: int
    resolver_fingerprint: str
    resolver_cached: bool
    historical_endpoint_status: int

    def document(self) -> dict[str, object]:
        return {
            "auth_mode": self.auth_mode,
            "key_present": self.key_present,
            "instrument_resolution_count": self.instrument_resolution_count,
            "resolver_fingerprint": self.resolver_fingerprint,
            "resolver_cached": self.resolver_cached,
            "historical_endpoint_status": self.historical_endpoint_status,
            "instrument_resolver_endpoint": _safe_endpoint(INSTRUMENTS_PATH),
            "historical_endpoint": _safe_endpoint(HISTORICAL_PRICES_PATH),
        }


@dataclass(frozen=True)
class _CachedPage:
    payload: object
    raw_sha256: str
    row_count: int
    cached: bool


@dataclass(frozen=True)
class _SideCandle:
    timestamp_utc: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal | None


@dataclass(frozen=True)
class DatasetRecord:
    """Manifest facts for one raw or derived normalized dataset."""

    symbol: str
    timeframe: Timeframe
    provider: str
    provider_id: int | None
    provider_symbol: str | None
    source_fingerprints: tuple[str, ...]
    dataset_fingerprint: str
    bid_ask_availability: str
    parent_fingerprint: str | None
    aggregation_rule: str | None
    expected_source_row_count: int | None
    actual_source_row_count: int | None
    raw_page_count: int

    def document(self) -> dict[str, object]:
        return {
            "instrument": self.symbol,
            "timeframe": self.timeframe.value,
            "provider": self.provider,
            "provider_instrument_id": self.provider_id,
            "provider_symbol": self.provider_symbol,
            "source_fingerprints": list(self.source_fingerprints),
            "dataset_fingerprint": self.dataset_fingerprint,
            "bid_ask_availability": self.bid_ask_availability,
            "parent_fingerprint": self.parent_fingerprint,
            "aggregation_rule": self.aggregation_rule,
            "expected_source_row_count": self.expected_source_row_count,
            "actual_source_row_count": self.actual_source_row_count,
            "raw_page_count": self.raw_page_count,
        }


class DukascopyOfficialClient:
    """Small, synchronous, bounded client for documented read-only endpoints."""

    def __init__(
        self,
        *,
        cache_directory: Path,
        api_key: str | None = None,
        http_client: httpx.Client | None = None,
        timeout_seconds: float = 30.0,
        max_attempts: int = 3,
        backoff_seconds: float = 0.25,
        sleep: Callable[[float], None] = time.sleep,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        if max_attempts < 1:
            raise ValueError("max_attempts must be at least one")
        self.cache_directory = cache_directory
        self.api_key = api_key if api_key and api_key.strip() else None
        self.timeout_seconds = timeout_seconds
        self.max_attempts = max_attempts
        self.backoff_seconds = backoff_seconds
        self.sleep = sleep
        self.now = now
        self.http_client = http_client or httpx.Client(
            timeout=timeout_seconds,
            follow_redirects=False,
            headers={"User-Agent": "ig-trader-sl04-research/1.0"},
        )
        self.accounting = AcquisitionAccounting()
        self._instruments: tuple[DukascopyInstrument, ...] | None = None
        self._resolver_fingerprint: str | None = None
        self._resolver_cached = False

    @classmethod
    def from_environment(
        cls, *, cache_directory: Path, **kwargs: object
    ) -> DukascopyOfficialClient:
        """Read only the supported environment variable, retaining no printed value."""

        return cls(
            cache_directory=cache_directory,
            api_key=os.environ.get("DUKASCOPY_API_KEY"),
            **kwargs,
        )

    @property
    def key_present(self) -> bool:
        return self.api_key is not None

    def close(self) -> None:
        self.http_client.close()

    def resolve_instruments(self) -> tuple[DukascopyInstrument, ...]:
        """Resolve provider IDs only from a cached or newly fetched official list."""

        if self._instruments is not None:
            return self._instruments
        cache_path = self.cache_directory / "instrument_resolver.json"
        cached = _read_cached_document(cache_path)
        if cached is not None and cached.get("endpoint") == _safe_endpoint(INSTRUMENTS_PATH):
            raw = cached.get("raw_payload")
            raw_sha = cached.get("raw_sha256")
            if isinstance(raw, str) and _sha256(raw.encode("utf-8")) == raw_sha:
                try:
                    payload = json.loads(raw)
                    values = _parse_instruments(payload, str(raw_sha))
                except (DataContractError, ValueError, json.JSONDecodeError):
                    values = ()
                if values:
                    self.accounting.cache_hits += 1
                    self._instruments = values
                    self._resolver_fingerprint = str(raw_sha)
                    self._resolver_cached = True
                    return values
        response = self._get({"path": INSTRUMENTS_PATH})
        try:
            raw_text = _redact_secret(response.content.decode("utf-8"), self.api_key)
            payload = json.loads(raw_text)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise DukascopyAccessError("DUKASCOPY_INSTRUMENT_RESPONSE_INVALID") from error
        fingerprint = _sha256(raw_text.encode("utf-8"))
        values = _parse_instruments(payload, fingerprint)
        if not values:
            raise DukascopyAccessError("DUKASCOPY_INSTRUMENT_RESOLUTION_EMPTY")
        document = {
            "schema_version": CACHE_SCHEMA,
            "provider": PROVIDER,
            "acquired_at_utc": self.now().isoformat(),
            "endpoint": _safe_endpoint(INSTRUMENTS_PATH),
            "raw_sha256": fingerprint,
            "raw_payload": raw_text,
        }
        _write_json(cache_path, document)
        self._instruments = values
        self._resolver_fingerprint = fingerprint
        self._resolver_cached = False
        return values

    def resolve(self, canonical_symbol: str) -> DukascopyInstrument:
        """Require exactly one official matching provider instrument; never guess IDs."""

        candidates = tuple(
            item for item in self.resolve_instruments() if item.canonical_symbol == canonical_symbol
        )
        if not candidates:
            raise DukascopyAccessError(f"DUKASCOPY_INSTRUMENT_NOT_RESOLVED:{canonical_symbol}")
        if len(candidates) != 1:
            raise DukascopyAccessError(f"DUKASCOPY_INSTRUMENT_AMBIGUOUS:{canonical_symbol}")
        return candidates[0]

    def preflight(self) -> DukascopyPreflight:
        """Perform two small GETs before any broad historical acquisition."""

        instruments = self.resolve_instruments()
        eurusd = self.resolve("EURUSD")
        end = self.now()
        start = end - timedelta(minutes=5)
        response = self._get(
            {
                "path": HISTORICAL_PRICES_PATH,
                "instrument": str(eurusd.provider_id),
                "timeFrame": "1min",
                "count": "1",
                "start": str(_epoch_milliseconds(start)),
                "end": str(_epoch_milliseconds(end)),
                "dayStartTime": "UTC",
                "offerSide": "B",
            }
        )
        assert self._resolver_fingerprint is not None
        return DukascopyPreflight(
            auth_mode=(
                "KEY_ACCEPTED_BY_PROVIDER" if self.key_present else "ANONYMOUS_ACCEPTED_BY_PROVIDER"
            ),
            key_present=self.key_present,
            instrument_resolution_count=len(instruments),
            resolver_fingerprint=self._resolver_fingerprint,
            resolver_cached=self._resolver_cached,
            historical_endpoint_status=response.status_code,
        )

    def fetch_pages(
        self,
        *,
        instrument: DukascopyInstrument,
        source_timeframe: Timeframe,
        offer_side: str,
        start_utc: datetime,
        end_utc: datetime,
    ) -> tuple[_CachedPage, ...]:
        """Fetch deterministic half-open windows with a strict 5,000-row ceiling."""

        if source_timeframe not in _API_TIMEFRAMES:
            raise DataContractError("Dukascopy direct acquisition supports only 1M or 1H")
        if offer_side not in {"B", "A"}:
            raise DataContractError("Dukascopy offer side must be B or A")
        if start_utc.tzinfo is None or end_utc.tzinfo is None or end_utc <= start_utc:
            raise DataContractError("Dukascopy request range must be increasing UTC timestamps")
        interval = TIMEFRAME_INTERVALS[source_timeframe]
        window = interval * MAX_PAGE_ROWS
        pages: list[_CachedPage] = []
        cursor = start_utc.astimezone(UTC)
        end = end_utc.astimezone(UTC)
        while cursor < end:
            next_cursor = min(cursor + window, end)
            page = self._fetch_page(
                instrument=instrument,
                source_timeframe=source_timeframe,
                offer_side=offer_side,
                start_utc=cursor,
                end_utc=next_cursor,
            )
            pages.append(page)
            cursor = next_cursor
        return tuple(pages)

    def _fetch_page(
        self,
        *,
        instrument: DukascopyInstrument,
        source_timeframe: Timeframe,
        offer_side: str,
        start_utc: datetime,
        end_utc: datetime,
    ) -> _CachedPage:
        path = _page_cache_path(
            self.cache_directory,
            instrument.canonical_symbol,
            source_timeframe,
            offer_side,
            start_utc,
            end_utc,
        )
        cached = _read_cached_document(path)
        if cached is not None:
            page = _cached_page(cached)
            if page is not None:
                self.accounting.cache_hits += 1
                return page
        params = {
            "path": HISTORICAL_PRICES_PATH,
            "instrument": str(instrument.provider_id),
            "timeFrame": _API_TIMEFRAMES[source_timeframe],
            "count": str(MAX_PAGE_ROWS),
            "start": str(_epoch_milliseconds(start_utc)),
            "end": str(_epoch_milliseconds(end_utc)),
            "dayStartTime": "UTC",
            "offerSide": offer_side,
        }
        response = self._get(params)
        try:
            raw_text = _redact_secret(response.content.decode("utf-8"), self.api_key)
            payload = json.loads(raw_text)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise DukascopyAccessError("DUKASCOPY_HISTORICAL_RESPONSE_INVALID") from error
        rows = _payload_rows(payload)
        if len(rows) > MAX_PAGE_ROWS:
            raise DukascopyAccessError("DUKASCOPY_PAGE_ROW_LIMIT_EXCEEDED")
        page = _CachedPage(payload, _sha256(raw_text.encode("utf-8")), len(rows), cached=False)
        _write_json(
            path,
            {
                "schema_version": CACHE_SCHEMA,
                "provider": PROVIDER,
                "provider_instrument_id": instrument.provider_id,
                "provider_symbol": instrument.provider_symbol,
                "timeframe": source_timeframe.value,
                "offer_side": offer_side,
                "request_interval": {
                    "start_utc": start_utc.astimezone(UTC).isoformat(),
                    "end_utc": end_utc.astimezone(UTC).isoformat(),
                    "boundary_rule": "[start_utc,end_utc)",
                },
                "acquisition_timestamp_utc": self.now().isoformat(),
                "source_endpoint": _safe_endpoint(HISTORICAL_PRICES_PATH),
                "row_count": page.row_count,
                "raw_sha256": page.raw_sha256,
                "raw_payload": raw_text,
            },
        )
        self.accounting.new_downloads += 1
        self.accounting.downloaded_rows += page.row_count
        self.accounting.bytes_downloaded += len(response.content)
        return page

    def _get(self, parameters: dict[str, str]) -> httpx.Response:
        """Retry only transient safe GET failures and never expose request strings."""

        params = dict(parameters)
        if self.api_key is not None:
            params["key"] = self.api_key
        last_problem = "DUKASCOPY_GET_FAILED"
        for attempt in range(self.max_attempts):
            self.accounting.provider_get_count += 1
            try:
                response = self.http_client.get(DUKASCOPY_BASE_URL, params=params)
            except httpx.HTTPError:
                last_problem = "DUKASCOPY_NETWORK_ERROR"
                response = None
            if response is not None and response.status_code < 400:
                return response
            status = response.status_code if response is not None else None
            if status in {401, 403}:
                if not self.key_present:
                    raise DukascopyApiKeyRequired("DUKASCOPY_API_KEY_REQUIRED")
                raise DukascopyAccessError("DUKASCOPY_API_KEY_REJECTED")
            if status == 429:
                self.accounting.rate_limit_responses += 1
                last_problem = "DUKASCOPY_RATE_LIMITED"
            elif status is not None and 400 <= status < 500:
                raise DukascopyAccessError(f"DUKASCOPY_HTTP_{status}")
            elif status is not None:
                last_problem = f"DUKASCOPY_HTTP_{status}"
            if attempt + 1 < self.max_attempts:
                self.accounting.retry_count += 1
                self.sleep(self.backoff_seconds * (2**attempt))
        raise DukascopyAccessError(last_problem)


class DukascopyStructuredHistorySource:
    """Normalize cached official bid/ask pages into canonical mid-price datasets."""

    provider = PROVIDER

    def __init__(self, *, cache_directory: Path, client: DukascopyOfficialClient) -> None:
        self.cache_directory = cache_directory
        self.client = client
        self.records: dict[tuple[str, Timeframe], DatasetRecord] = {}

    def acquire(
        self,
        *,
        symbol: str,
        asset_class: AssetClass,
        source_timeframe: Timeframe,
        start_utc: datetime,
        end_utc: datetime,
    ) -> ResearchDataset:
        if asset_class not in {AssetClass.FX, AssetClass.METAL}:
            raise ExternalHistoryUnavailable(f"DUKASCOPY_ASSET_CLASS_UNSUPPORTED:{symbol}")
        if source_timeframe not in _API_TIMEFRAMES:
            raise DataContractError("SL-04 direct sources must be 1M or 1H")
        normalized = self._load_normalized(symbol, source_timeframe)
        if normalized is not None:
            return normalized
        instrument = self.client.resolve(symbol)
        bid_pages = self.client.fetch_pages(
            instrument=instrument,
            source_timeframe=source_timeframe,
            offer_side="B",
            start_utc=start_utc,
            end_utc=end_utc,
        )
        ask_pages = self.client.fetch_pages(
            instrument=instrument,
            source_timeframe=source_timeframe,
            offer_side="A",
            start_utc=start_utc,
            end_utc=end_utc,
        )
        bid_rows = _side_rows(
            bid_pages, "B", start_utc=start_utc, end_utc=end_utc, source_timeframe=source_timeframe
        )
        ask_rows = _side_rows(
            ask_pages, "A", start_utc=start_utc, end_utc=end_utc, source_timeframe=source_timeframe
        )
        if tuple(bid_rows) != tuple(ask_rows):
            raise DataContractError("DUKASCOPY_BID_ASK_TIMESTAMP_MISMATCH")
        candles = tuple(
            _mid_candle(symbol, source_timeframe, bid_rows[timestamp], ask_rows[timestamp])
            for timestamp in sorted(bid_rows)
        )
        if not candles:
            raise ExternalHistoryUnavailable(
                f"DUKASCOPY_NO_CANDLES:{symbol}:{source_timeframe.value}"
            )
        source_fingerprints = tuple(page.raw_sha256 for page in (*bid_pages, *ask_pages))
        dataset = build_dataset(candles, source_documents=source_fingerprints)
        self.client.accounting.normalized_rows += len(candles)
        acquired_at = self.client.now()
        document = {
            "schema_version": NORMALIZED_SCHEMA,
            "provider": PROVIDER,
            "provider_instrument_id": instrument.provider_id,
            "provider_symbol": instrument.provider_symbol,
            "provider_name": instrument.provider_name,
            "acquired_at_utc": acquired_at.isoformat(),
            "source_endpoint": _safe_endpoint(HISTORICAL_PRICES_PATH),
            "source_fingerprints": list(source_fingerprints),
            "dataset_fingerprint": dataset.dataset_fingerprint,
            "bid_ask_availability": "ALIGNED_BID_ASK",
            "mid_ohlc_rule": {
                "open": "(bid_open + ask_open) / 2",
                "high": "(bid_high + ask_high) / 2",
                "low": "(bid_low + ask_low) / 2",
                "close": "(bid_close + ask_close) / 2",
                "historical_spread": "ask_close - bid_close; source-quality evidence only",
                "pnl_cost_rule": "SL-03 IG fingerprint-bound friction remains separate",
            },
            "request_interval": {
                "start_utc": start_utc.astimezone(UTC).isoformat(),
                "end_utc": end_utc.astimezone(UTC).isoformat(),
            },
            "raw_page_count": len(source_fingerprints),
            "candles": [_candle_document(candle) for candle in dataset.candles],
        }
        _write_json(_normalized_path(self.cache_directory, symbol, source_timeframe), document)
        research = ResearchDataset(
            dataset=dataset,
            provenance=ProviderProvenance(
                provider=PROVIDER,
                provider_symbol=instrument.provider_symbol,
                acquisition_timestamp_utc=acquired_at,
                source_url=_safe_endpoint(HISTORICAL_PRICES_PATH),
                raw_source_fingerprint=_combined_fingerprint(source_fingerprints),
                normalized_fingerprint=dataset.dataset_fingerprint,
                license_source_note=(
                    "Official Dukascopy structured bid/ask history; external research data, "
                    "not IG broker candles."
                ),
            ),
            depth_status=_depth_status(dataset),
            cached=False,
        )
        self.records[(symbol, source_timeframe)] = DatasetRecord(
            symbol=symbol,
            timeframe=source_timeframe,
            provider=PROVIDER,
            provider_id=instrument.provider_id,
            provider_symbol=instrument.provider_symbol,
            source_fingerprints=source_fingerprints,
            dataset_fingerprint=dataset.dataset_fingerprint,
            bid_ask_availability="ALIGNED_BID_ASK",
            parent_fingerprint=None,
            aggregation_rule=None,
            expected_source_row_count=None,
            actual_source_row_count=len(dataset.candles),
            raw_page_count=len(source_fingerprints),
        )
        return research

    def load(self, symbol: str, timeframe: Timeframe, asset_class: AssetClass) -> ResearchDataset:
        """Load one coherent cached official source and derive only complete buckets."""

        if asset_class not in {AssetClass.FX, AssetClass.METAL}:
            raise ExternalHistoryUnavailable(f"DUKASCOPY_ASSET_CLASS_UNSUPPORTED:{symbol}")
        direct = self._load_normalized(symbol, timeframe)
        if direct is not None:
            return direct
        for parent_timeframe in (Timeframe.M1, Timeframe.H1):
            parent = self._load_normalized(symbol, parent_timeframe)
            if parent is None or timeframe is parent_timeframe:
                continue
            try:
                derived = resample_complete(parent.dataset, timeframe)
            except DataContractError:
                continue
            expected = int(TIMEFRAME_INTERVALS[timeframe] / TIMEFRAME_INTERVALS[parent_timeframe])
            record = self.records.get((symbol, parent_timeframe))
            self.client.accounting.derived_rows += len(derived.candles)
            self.records[(symbol, timeframe)] = DatasetRecord(
                symbol=symbol,
                timeframe=timeframe,
                provider=PROVIDER,
                provider_id=record.provider_id if record else None,
                provider_symbol=parent.provenance.provider_symbol,
                source_fingerprints=(parent.dataset.dataset_fingerprint,),
                dataset_fingerprint=derived.dataset_fingerprint,
                bid_ask_availability="DERIVED_FROM_ALIGNED_BID_ASK",
                parent_fingerprint=parent.dataset.dataset_fingerprint,
                aggregation_rule=(
                    f"complete_utc_{parent_timeframe.value}_to_{timeframe.value}_ohlcv"
                ),
                expected_source_row_count=expected,
                actual_source_row_count=len(parent.dataset.candles),
                raw_page_count=record.raw_page_count if record else 0,
            )
            return ResearchDataset(
                dataset=derived,
                provenance=ProviderProvenance(
                    provider=PROVIDER,
                    provider_symbol=parent.provenance.provider_symbol,
                    acquisition_timestamp_utc=parent.provenance.acquisition_timestamp_utc,
                    source_url=parent.provenance.source_url,
                    raw_source_fingerprint=parent.provenance.raw_source_fingerprint,
                    normalized_fingerprint=derived.dataset_fingerprint,
                    license_source_note=parent.provenance.license_source_note,
                    parent_dataset_fingerprint=parent.dataset.dataset_fingerprint,
                ),
                depth_status=_depth_status(derived),
                cached=True,
            )
        raise ExternalHistoryUnavailable(
            f"DUKASCOPY_NORMALIZED_DATA_NOT_AVAILABLE:{symbol}:{timeframe.value}"
        )

    def _load_normalized(self, symbol: str, timeframe: Timeframe) -> ResearchDataset | None:
        path = _normalized_path(self.cache_directory, symbol, timeframe)
        document = _read_cached_document(path)
        if document is None or document.get("schema_version") != NORMALIZED_SCHEMA:
            return None
        candles_value = document.get("candles")
        source_fingerprints = document.get("source_fingerprints")
        if not isinstance(candles_value, list) or not isinstance(source_fingerprints, list):
            return None
        try:
            candles = tuple(_canonical_candle(symbol, timeframe, value) for value in candles_value)
            dataset = build_dataset(
                candles, source_documents=tuple(str(item) for item in source_fingerprints)
            )
            if dataset.dataset_fingerprint != document.get("dataset_fingerprint"):
                return None
            acquired_at = _timestamp(document.get("acquired_at_utc"))
            provider_symbol = document.get("provider_symbol")
            if acquired_at is None or not isinstance(provider_symbol, str):
                return None
        except (DataContractError, TypeError, ValueError):
            return None
        self.accounting_cache_hit_once()
        record = DatasetRecord(
            symbol=symbol,
            timeframe=timeframe,
            provider=PROVIDER,
            provider_id=_integer(document.get("provider_instrument_id")),
            provider_symbol=provider_symbol,
            source_fingerprints=tuple(str(item) for item in source_fingerprints),
            dataset_fingerprint=dataset.dataset_fingerprint,
            bid_ask_availability=str(document.get("bid_ask_availability", "UNKNOWN")),
            parent_fingerprint=None,
            aggregation_rule=None,
            expected_source_row_count=None,
            actual_source_row_count=len(dataset.candles),
            raw_page_count=_integer(document.get("raw_page_count")) or 0,
        )
        self.records[(symbol, timeframe)] = record
        return ResearchDataset(
            dataset=dataset,
            provenance=ProviderProvenance(
                provider=PROVIDER,
                provider_symbol=provider_symbol,
                acquisition_timestamp_utc=acquired_at,
                source_url=_safe_endpoint(HISTORICAL_PRICES_PATH),
                raw_source_fingerprint=_combined_fingerprint(record.source_fingerprints),
                normalized_fingerprint=dataset.dataset_fingerprint,
                license_source_note=(
                    "Cached official Dukascopy structured bid/ask history; external research "
                    "data, not IG broker candles."
                ),
            ),
            depth_status=_depth_status(dataset),
            cached=True,
        )

    def accounting_cache_hit_once(self) -> None:
        self.client.accounting.cache_hits += 1


def _parse_instruments(payload: object, fingerprint: str) -> tuple[DukascopyInstrument, ...]:
    instruments: list[DukascopyInstrument] = []
    for row in _instrument_rows(payload):
        provider_id = _integer(row.get("id", row.get("instrumentId", row.get("instrument_id"))))
        provider_symbol = _provider_symbol(row)
        if provider_id is None or provider_symbol is None:
            continue
        canonical = _canonical_symbol(provider_symbol)
        if canonical is None:
            continue
        name = row.get("name", row.get("displayName", row.get("description")))
        instruments.append(
            DukascopyInstrument(
                canonical_symbol=canonical,
                provider_id=provider_id,
                provider_symbol=provider_symbol,
                provider_name=name if isinstance(name, str) and name.strip() else None,
                resolver_fingerprint=fingerprint,
            )
        )
    return tuple(instruments)


def _instrument_rows(value: object) -> Iterable[dict[str, object]]:
    if isinstance(value, list):
        for row in value:
            if isinstance(row, dict):
                yield row
        return
    if not isinstance(value, dict):
        return
    for name in ("instruments", "items", "data", "results"):
        nested = value.get(name)
        if isinstance(nested, list):
            yield from _instrument_rows(nested)
            return
    for nested in value.values():
        if isinstance(nested, list):
            yield from _instrument_rows(nested)


def _provider_symbol(row: dict[str, object]) -> str | None:
    for name in ("symbol", "instrument", "name", "displayName", "label"):
        value = row.get(name)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _canonical_symbol(value: str) -> str | None:
    candidate = "".join(character for character in value.upper() if character.isalnum())
    return candidate if candidate.isascii() and candidate.isalnum() and candidate else None


def _payload_rows(payload: object) -> list[dict[str, object]]:
    if isinstance(payload, list):
        rows = payload
    elif isinstance(payload, dict):
        rows = payload.get("candles", payload.get("data", payload.get("prices", ())))
    else:
        rows = ()
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise DukascopyAccessError("DUKASCOPY_HISTORICAL_ROWS_INVALID")
    return [dict(row) for row in rows]


def _side_rows(
    pages: Iterable[_CachedPage],
    side: str,
    *,
    start_utc: datetime,
    end_utc: datetime,
    source_timeframe: Timeframe,
) -> dict[datetime, _SideCandle]:
    values: dict[datetime, _SideCandle] = {}
    for page in pages:
        for row in _payload_rows(page.payload):
            candle = _parse_side_candle(row, side)
            if candle.timestamp_utc < start_utc or candle.timestamp_utc >= end_utc:
                continue
            if candle.timestamp_utc in values:
                raise DataContractError("DUKASCOPY_DUPLICATE_BOUNDARY_CANDLE")
            values[candle.timestamp_utc] = candle
    if tuple(values) != tuple(sorted(values)):
        values = dict(sorted(values.items()))
    interval = TIMEFRAME_INTERVALS[source_timeframe]
    if any(
        timestamp.tzinfo is None or timestamp.utcoffset() != timedelta(0) for timestamp in values
    ):
        raise DataContractError("DUKASCOPY_TIMESTAMP_NOT_UTC")
    if interval <= timedelta(0):
        raise DataContractError("DUKASCOPY_TIMEFRAME_INVALID")
    return values


def _parse_side_candle(row: dict[str, object], side: str) -> _SideCandle:
    timestamp = _timestamp(row.get("timestamp", row.get("time", row.get("timestamp_utc"))))
    prefix = "bid" if side == "B" else "ask"
    try:
        if timestamp is None:
            raise ValueError("timestamp required")
        return _SideCandle(
            timestamp_utc=timestamp,
            open=_required_decimal(row, prefix, "open"),
            high=_required_decimal(row, prefix, "high"),
            low=_required_decimal(row, prefix, "low"),
            close=_required_decimal(row, prefix, "close"),
            volume=_optional_decimal(row.get("volume", row.get(f"{prefix}_volume"))),
        )
    except (InvalidOperation, ValueError, TypeError) as error:
        raise DataContractError("DUKASCOPY_OHLC_ROW_INVALID") from error


def _required_decimal(row: dict[str, object], prefix: str, field: str) -> Decimal:
    candidates = (f"{prefix}_{field}", f"{prefix}{field.title()}", field)
    for name in candidates:
        if name in row:
            value = _optional_decimal(row[name])
            if value is not None:
                return value
    nested = row.get(prefix)
    if isinstance(nested, dict):
        value = _optional_decimal(nested.get(field))
        if value is not None:
            return value
    raise ValueError(f"{prefix} {field} required")


def _mid_candle(symbol: str, timeframe: Timeframe, bid: _SideCandle, ask: _SideCandle) -> LabCandle:
    if bid.timestamp_utc != ask.timestamp_utc:
        raise DataContractError("DUKASCOPY_BID_ASK_TIMESTAMP_MISMATCH")
    return LabCandle(
        instrument=symbol,
        timestamp_utc=bid.timestamp_utc,
        timeframe=timeframe,
        open=(bid.open + ask.open) / Decimal("2"),
        high=(bid.high + ask.high) / Decimal("2"),
        low=(bid.low + ask.low) / Decimal("2"),
        close=(bid.close + ask.close) / Decimal("2"),
        spread=ask.close - bid.close,
        volume=(bid.volume + ask.volume) / Decimal("2")
        if bid.volume is not None and ask.volume is not None
        else None,
        source=PROVIDER,
        source_quality=SourceQuality.EXTERNAL_UNVERIFIED,
        gap_classification=GapClassification.NONE,
        synthetic=False,
    )


def _canonical_candle(symbol: str, timeframe: Timeframe, row: object) -> LabCandle:
    if not isinstance(row, dict):
        raise DataContractError("Dukascopy normalized candle must be an object")
    timestamp = _timestamp(row.get("timestamp_utc"))
    if timestamp is None:
        raise DataContractError("Dukascopy normalized timestamp invalid")
    try:
        return LabCandle(
            instrument=symbol,
            timestamp_utc=timestamp,
            timeframe=timeframe,
            open=Decimal(str(row["open"])),
            high=Decimal(str(row["high"])),
            low=Decimal(str(row["low"])),
            close=Decimal(str(row["close"])),
            spread=_optional_decimal(row.get("spread")),
            volume=_optional_decimal(row.get("volume")),
            source=PROVIDER,
            source_quality=SourceQuality.EXTERNAL_UNVERIFIED,
            gap_classification=GapClassification.NONE,
            synthetic=False,
        )
    except (KeyError, InvalidOperation, ValueError) as error:
        raise DataContractError("Dukascopy normalized candle invalid") from error


def _candle_document(candle: LabCandle) -> dict[str, object]:
    return {
        "timestamp_utc": candle.timestamp_utc.isoformat(),
        "open": str(candle.open),
        "high": str(candle.high),
        "low": str(candle.low),
        "close": str(candle.close),
        "spread": str(candle.spread) if candle.spread is not None else None,
        "volume": str(candle.volume) if candle.volume is not None else None,
    }


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


def _page_cache_path(
    root: Path,
    symbol: str,
    timeframe: Timeframe,
    side: str,
    start_utc: datetime,
    end_utc: datetime,
) -> Path:
    return (
        root
        / "pages"
        / symbol.lower()
        / timeframe.value.lower()
        / side.lower()
        / f"{_epoch_milliseconds(start_utc)}_{_epoch_milliseconds(end_utc)}.json"
    )


def _normalized_path(root: Path, symbol: str, timeframe: Timeframe) -> Path:
    return root / "normalized" / f"{symbol.lower()}_{timeframe.value.lower()}_dukascopy.json"


def _cached_page(document: dict[str, object]) -> _CachedPage | None:
    raw = document.get("raw_payload")
    expected = document.get("raw_sha256")
    count = _integer(document.get("row_count"))
    if not isinstance(raw, str) or not isinstance(expected, str) or count is None:
        return None
    if _sha256(raw.encode("utf-8")) != expected:
        return None
    try:
        payload = json.loads(raw)
        row_count = len(_payload_rows(payload))
    except (DukascopyAccessError, json.JSONDecodeError):
        return None
    if row_count != count or row_count > MAX_PAGE_ROWS:
        return None
    return _CachedPage(payload, expected, count, cached=True)


def _read_cached_document(path: Path) -> dict[str, object] | None:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return document if isinstance(document, dict) else None


def _write_json(path: Path, document: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, delete=False, suffix=".tmp"
    ) as handle:
        handle.write(json.dumps(document, sort_keys=True, indent=2) + "\n")
        temporary = Path(handle.name)
    temporary.replace(path)


def _safe_endpoint(path: str) -> str:
    """An artifact-safe endpoint record that deliberately excludes every query value."""

    return f"{DUKASCOPY_BASE_URL}?path={path}"


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _combined_fingerprint(fingerprints: Iterable[str]) -> str:
    return _sha256("\n".join(fingerprints).encode("ascii"))


def _redact_secret(value: str, secret: str | None) -> str:
    return value.replace(secret, "[REDACTED]") if secret else value


def _epoch_milliseconds(value: datetime) -> int:
    return int(value.astimezone(UTC).timestamp() * 1000)


def _timestamp(value: object) -> datetime | None:
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)
        except ValueError:
            try:
                value = Decimal(value)
            except InvalidOperation:
                return None
    if isinstance(value, int | float | Decimal) and not isinstance(value, bool):
        numeric = Decimal(str(value))
        if numeric <= 0:
            return None
        seconds = numeric / Decimal("1000") if numeric >= Decimal("100000000000") else numeric
        try:
            return datetime.fromtimestamp(float(seconds), tz=UTC)
        except (OverflowError, OSError, ValueError):
            return None
    return None


def _integer(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = int(str(value))
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def _optional_decimal(value: object) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
