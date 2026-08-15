"""G3A-01 GET-only IG Demo market-data acquisition and evidence runner.

The module deliberately does not import the bot, strategy runner, database, or
execution adapter.  Its transport allow-list contains authentication plus
market-data reads only; every dealing endpoint is blocked before transmission.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from collections.abc import Callable, Iterable, Mapping
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import IntEnum
from math import isfinite
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlparse

import httpx

from src.ig_trader.g3a_data import (
    CANONICAL_SCHEMA_VERSION,
    EMPTY_FINDINGS,
    ENVIRONMENT,
    FROZEN_INSTRUMENTS,
    MINIMUM_SCALPER_CANDLES,
    NORMALIZATION_VERSION,
    RESOLUTION_LABELS,
    RESOLUTION_MINUTES,
    SOURCE,
    WORK_ORDER,
    EpicVerification,
    FinalClassification,
    FrozenInstrument,
    RawFileEvidence,
    SeriesDataset,
    SeriesQuality,
    build_series_manifest,
    canonical_candle_document,
    canonical_json_bytes,
    fingerprint,
    json_value,
    normalize_candle,
    overall_classification,
    pretty_json_bytes,
    qualify_series,
    replay_sufficiency,
    sha256_bytes,
    utc_text,
    verify_epic,
)
from src.ig_trader.http_client import build_system_ssl_context

DEMO_REST_HOST = "demo-api.ig.com"
DEMO_REST_BASE_URL = f"https://{DEMO_REST_HOST}/gateway/deal"
EVIDENCE_SCHEMA_VERSION = "g3a-data-quality-evidence/1.0.0"
DOCUMENTED_WEEKLY_ALLOWANCE = 10_000
HISTORICAL_PRICE_RESOLUTIONS = frozenset((*RESOLUTION_MINUTES, "SECOND"))
DEFAULT_INTERVALS_PER_SERIES = 700
MAXIMUM_INTERVALS_PER_SERIES = 750
PAGE_SIZE = 500
MAXIMUM_PAGES_PER_SERIES = 4
MAXIMUM_REQUEST_ATTEMPTS = 3
DEFAULT_EVIDENCE_JSON = Path(".runtime/evidence/g3a-data-quality.json")
DEFAULT_DATA_ROOT = Path(".runtime/g3a/data")

ACCOUNT_ENV_NAMES = (
    "IG_ACCOUNT_ID",
    "IG_ACCOUNT_NUMBER",
    "IG_SERVICE_ACC_NUMBER",
)
SECRET_ENV_NAMES = ("IG_API_KEY", "IG_IDENTIFIER", "IG_PASSWORD", *ACCOUNT_ENV_NAMES)
PREFLIGHT_ENV_NAMES = ("IG_DEMO", "IG_BASE_URL", "PAPER_TRADING")
ORDER_PATH_PREFIXES = (
    "/positions",
    "/workingorders",
    "/working-orders",
    "/confirms",
)
ORDER_METHODS = {"POST", "PUT", "DELETE"}
EPIC_PATTERN = re.compile(r"^[A-Za-z0-9._-]+$")
RUN_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,79}$")
HISTORICAL_ALLOWANCE_ERROR = "error.public-api.exceeded-account-historical-data-allowance"
RETRYABLE_ALLOWANCE_ERRORS = {
    "error.public-api.exceeded-account-allowance",
    "error.public-api.exceeded-api-key-allowance",
}
FORBIDDEN_RAW_KEYS = {
    "access_token",
    "refresh_token",
    "oauthToken",
    "password",
    "apiKey",
    "api_key",
    "CST",
    "X-SECURITY-TOKEN",
    "accountId",
    "dealId",
    "dealReference",
}


class ExitCode(IntEnum):
    SUCCESS = 0
    CONFIGURATION_FAILURE = 2
    AUTHENTICATION_FAILURE = 3
    ACQUISITION_OR_QUALITY_FAILURE = 4
    OUTPUT_FAILURE = 5


class G3AError(RuntimeError):
    """A sanitized and classified pipeline error."""

    def __init__(self, classification: FinalClassification, reason: str) -> None:
        super().__init__(reason)
        self.classification = classification
        self.reason = reason


class EndpointBlockedError(G3AError):
    def __init__(self) -> None:
        super().__init__(FinalClassification.INCONCLUSIVE, "ENDPOINT_BLOCKED")


@dataclass(frozen=True)
class G3AConfig:
    environment: str
    base_url: str
    paper_trading: bool
    data_root: Path
    evidence_json: Path
    run_id: str
    requested_end_utc: datetime
    intervals_per_series: int
    api_key: str = field(repr=False)
    identifier: str = field(repr=False)
    password: str = field(repr=False)
    account_id: str = field(repr=False)
    raw_cache_run_id: str | None = None
    offline_cache_only: bool = False
    source_acquisition_evidence: Path | None = None
    connect_timeout_seconds: float = 30.0
    maximum_request_attempts: int = MAXIMUM_REQUEST_ATTEMPTS
    minimum_request_interval_seconds: float = 2.1
    page_size: int = PAGE_SIZE
    maximum_pages_per_series: int = MAXIMUM_PAGES_PER_SERIES

    def __post_init__(self) -> None:
        if self.environment != "demo" or self.base_url != DEMO_REST_BASE_URL:
            raise ValueError("Demo configuration is required")
        if not self.paper_trading:
            raise ValueError("PAPER_TRADING must be true")
        if not RUN_ID_PATTERN.fullmatch(self.run_id):
            raise ValueError("run_id is invalid")
        if self.raw_cache_run_id is not None and not RUN_ID_PATTERN.fullmatch(
            self.raw_cache_run_id
        ):
            raise ValueError("raw_cache_run_id is invalid")
        if self.offline_cache_only and (
            self.raw_cache_run_id is None or self.source_acquisition_evidence is None
        ):
            raise ValueError("offline cache mode requires raw cache and source evidence")
        if not _aware_utc(self.requested_end_utc):
            raise ValueError("requested_end_utc must be UTC")
        if self.requested_end_utc.second or self.requested_end_utc.microsecond:
            raise ValueError("requested_end_utc must be minute aligned")
        if not MINIMUM_SCALPER_CANDLES <= self.intervals_per_series <= 750:
            raise ValueError("intervals_per_series is outside the bounded range")
        if not 1 <= self.maximum_request_attempts <= MAXIMUM_REQUEST_ATTEMPTS:
            raise ValueError("request retry limit is invalid")
        if not 0 <= self.minimum_request_interval_seconds <= 10:
            raise ValueError("request pacing is invalid")
        if not 1 <= self.page_size <= PAGE_SIZE:
            raise ValueError("page_size is invalid")
        if not 1 <= self.maximum_pages_per_series <= MAXIMUM_PAGES_PER_SERIES:
            raise ValueError("page bound is invalid")

    @property
    def evidence_markdown(self) -> Path:
        return self.evidence_json.with_suffix(".md")

    def secret_values(self) -> tuple[str, ...]:
        return tuple(
            value
            for value in (self.api_key, self.identifier, self.password, self.account_id)
            if value
        )


@dataclass(frozen=True)
class RestResponse:
    status_code: int
    payload: Mapping[str, Any] | None
    content: bytes
    headers: Mapping[str, str]
    error_code: str | None


@dataclass(frozen=True)
class SessionTokens:
    cst: str = field(repr=False)
    x_security_token: str = field(repr=False)
    account_id: str = field(repr=False)

    def headers(self) -> dict[str, str]:
        return {"CST": self.cst, "X-SECURITY-TOKEN": self.x_security_token}


@dataclass(frozen=True)
class PipelineResult:
    classification: FinalClassification
    evidence: dict[str, object]
    manifest_paths: tuple[Path, ...]
    normalized_paths: tuple[Path, ...]


class RequestingClient(Protocol):
    def request(self, method: str, url: str, **kwargs: Any) -> httpx.Response: ...

    def close(self) -> None: ...


class SafeG3ARestClient:
    """Strict G3A transport whose only write methods are login and logout."""

    def __init__(
        self,
        config: G3AConfig,
        *,
        client: RequestingClient | None = None,
        sleeper: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.config = config
        self.client = client or httpx.Client(
            timeout=config.connect_timeout_seconds,
            verify=build_system_ssl_context(),
            headers={"X-IG-API-KEY": config.api_key},
        )
        self.sleeper = sleeper
        self.monotonic = monotonic
        self.last_request_at: float | None = None
        self.request_history: list[dict[str, object]] = []
        self.network_request_count = 0
        self.market_data_get_call_count = 0
        self.authentication_endpoint_call_count = 0
        self.order_endpoint_call_count = 0
        self.blocked_endpoint_attempt_count = 0

    def close(self) -> None:
        self.client.close()

    def request(
        self,
        method: str,
        path: str,
        *,
        version: str,
        params: Mapping[str, object] | None = None,
        headers: Mapping[str, str] | None = None,
        body: Mapping[str, object] | None = None,
    ) -> RestResponse:
        method = method.upper()
        if not endpoint_is_allowed(method, path, version=version, params=params):
            self.blocked_endpoint_attempt_count += 1
            raise EndpointBlockedError()
        if method in ORDER_METHODS and path.startswith(ORDER_PATH_PREFIXES):
            self.order_endpoint_call_count += 1
        self._pace()
        merged_headers = {"VERSION": version, "Accept": "application/json; charset=UTF-8"}
        if headers:
            merged_headers.update(headers)
        started = self.monotonic()
        try:
            response = self.client.request(
                method,
                self.config.base_url + path,
                headers=merged_headers,
                params=params,
                json=body,
            )
        except (httpx.TimeoutException, httpx.ConnectError) as error:
            self._record(method, path, None, None, started, "TIMEOUT_OR_CONNECT_ERROR")
            raise G3AError(FinalClassification.INCONCLUSIVE, "REST_NETWORK_FAILURE") from error
        self.last_request_at = self.monotonic()
        self.network_request_count += 1
        if method == "GET" and path.startswith(("/markets", "/prices")):
            self.market_data_get_call_count += 1
        if path == "/session" and method in {"POST", "DELETE"}:
            self.authentication_endpoint_call_count += 1
        content = bytes(response.content)
        payload: Mapping[str, Any] | None
        if response.status_code == 204 or not content:
            payload = None
        else:
            try:
                decoded = response.json()
            except (ValueError, json.JSONDecodeError) as error:
                self._record(method, path, response.status_code, None, started, None)
                raise G3AError(FinalClassification.SCHEMA_GAP, "MALFORMED_JSON_RESPONSE") from error
            if not isinstance(decoded, Mapping):
                raise G3AError(FinalClassification.SCHEMA_GAP, "MALFORMED_RESPONSE_SHAPE")
            payload = decoded
        error_code = _safe_error_code(payload)
        self._record(method, path, response.status_code, error_code, started, None)
        return RestResponse(
            int(response.status_code),
            payload,
            content,
            response.headers,
            error_code,
        )

    def _pace(self) -> None:
        now = self.monotonic()
        if self.last_request_at is not None:
            delay = self.config.minimum_request_interval_seconds - (now - self.last_request_at)
            if delay > 0:
                self.sleeper(delay)

    def _record(
        self,
        method: str,
        path: str,
        status: int | None,
        error_code: str | None,
        started: float,
        transport_error: str | None,
    ) -> None:
        self.request_history.append(
            {
                "timestamp_utc": utc_text(datetime.now(UTC)),
                "method": method,
                "path": path,
                "http_status": status,
                "ig_errorCode": error_code,
                "duration_ms": round((self.monotonic() - started) * 1000, 3),
                "transport_error": transport_error,
            }
        )


class RawStore:
    """Create-only exact response-body storage with a fail-closed secret scan."""

    def __init__(self, data_root: Path, run_id: str) -> None:
        self.data_root = data_root
        self.directory = data_root / "raw" / run_id

    def load(self, name: str) -> tuple[Mapping[str, Any], RawFileEvidence] | None:
        path = self.directory / name
        if not path.is_file():
            return None
        body = path.read_bytes()
        payload = safe_raw_payload(body)
        return payload, self._evidence(path, body)

    def persist(self, name: str, body: bytes) -> tuple[Mapping[str, Any], RawFileEvidence]:
        payload = safe_raw_payload(body)
        path = self.directory / name
        write_create_or_verify(path, body)
        return payload, self._evidence(path, body)

    def _evidence(self, path: Path, body: bytes) -> RawFileEvidence:
        return RawFileEvidence(
            path.relative_to(self.data_root).as_posix(),
            sha256_bytes(body),
            len(body),
        )


class G3APipeline:
    def __init__(
        self,
        config: G3AConfig,
        *,
        rest: SafeG3ARestClient | None = None,
        retrieval_started_at: datetime | None = None,
    ) -> None:
        self.config = config
        self.rest = rest or SafeG3ARestClient(config)
        self.retrieval_started_at = (retrieval_started_at or datetime.now(UTC)).astimezone(UTC)
        self.raw = RawStore(config.data_root, config.raw_cache_run_id or config.run_id)
        self.tokens: SessionTokens | None = None
        self.raw_files: dict[str, RawFileEvidence] = {}
        self.epic_results: tuple[EpicVerification, ...] = ()
        self.datasets: list[SeriesDataset] = []
        self.manifest_paths: list[Path] = []
        self.normalized_paths: list[Path] = []
        self.session_cleanup = "NOT_REQUIRED"
        self.source_acquisition: dict[str, object] = {"mode": "DIRECT_BROKER_READ"}
        self.source_retrieval_completed_at: datetime | None = None

    def run(self) -> PipelineResult:
        if self.config.evidence_json.exists() or self.config.evidence_markdown.exists():
            raise G3AError(FinalClassification.INCONCLUSIVE, "EVIDENCE_OUTPUT_EXISTS")
        classification = FinalClassification.INCONCLUSIVE
        reason = "UNSET"
        evidence: dict[str, object]
        try:
            run_state = self._load_or_create_run_state()
            if self.config.offline_cache_only:
                timezone_offset = self._load_source_acquisition()
            else:
                self.tokens = self._login()
                timezone_offset = self._account_timezone_offset()
            self.epic_results = tuple(
                self._verify_instrument(instrument) for instrument in FROZEN_INSTRUMENTS
            )
            if not all(result.verified for result in self.epic_results):
                raise G3AError(FinalClassification.SCHEMA_GAP, "EPIC_VERIFICATION_FAILED")
            for instrument in FROZEN_INSTRUMENTS:
                for resolution in RESOLUTION_MINUTES:
                    self.datasets.append(
                        self._acquire_series(instrument, resolution, timezone_offset)
                    )
            qualities = qualify_series(tuple(self.datasets))
            replay = replay_sufficiency(self.datasets, qualities)
            classification = overall_classification(qualities.values(), replay)
            qualification_generated_at = datetime.now(UTC)
            completed_at = self.source_retrieval_completed_at or qualification_generated_at
            dataset_fingerprint, series_rows = self._write_datasets(qualities, completed_at)
            run_manifest = self._write_run_manifest(
                run_state=run_state,
                completed_at=completed_at,
                classification=classification,
                dataset_fingerprint=dataset_fingerprint,
            )
            self.manifest_paths.append(run_manifest)
            evidence = self._evidence(
                classification=classification,
                reason="QUALIFICATION_COMPLETE",
                completed_at=completed_at,
                qualification_generated_at=qualification_generated_at,
                timezone_offset=timezone_offset,
                dataset_fingerprint=dataset_fingerprint,
                series_rows=series_rows,
                replay=replay,
            )
        except G3AError as error:
            classification = error.classification
            reason = error.reason
            evidence = self._evidence(
                classification=classification,
                reason=reason,
                completed_at=datetime.now(UTC),
                qualification_generated_at=datetime.now(UTC),
                timezone_offset=None,
                dataset_fingerprint=None,
                series_rows=[],
                replay=_unavailable_replay(reason),
            )
        finally:
            self._logout_safely()
            self.rest.close()
        safety = evidence["safety"]
        assert isinstance(safety, dict)
        safety.update(
            {
                "authentication_endpoint_call_count": (
                    self.rest.authentication_endpoint_call_count
                ),
                "market_data_get_call_count": self.rest.market_data_get_call_count,
                "order_endpoint_call_count": self.rest.order_endpoint_call_count,
                "blocked_endpoint_attempt_count": self.rest.blocked_endpoint_attempt_count,
                "session_cleanup": self.session_cleanup,
            }
        )
        evidence["request_history"] = self.rest.request_history
        evidence["secret_scan"] = scan_for_secrets(
            [*self._artifact_paths(), self.config.evidence_json],
            self.config.secret_values(),
            prospective_evidence=evidence,
        )
        if evidence["secret_scan"]["status"] != "PASS":  # type: ignore[index]
            raise G3AError(FinalClassification.DATA_QUALITY_FAILURE, "SECRET_SCAN_FAILED")
        write_json_create_only(self.config.evidence_json, evidence)
        write_create_or_verify(
            self.config.evidence_markdown,
            render_markdown(evidence).encode("utf-8"),
        )
        return PipelineResult(
            classification,
            evidence,
            tuple(self.manifest_paths),
            tuple(self.normalized_paths),
        )

    def _load_or_create_run_state(self) -> dict[str, object]:
        if self.config.raw_cache_run_id is not None:
            source_state_path = (
                self.config.data_root
                / "manifests"
                / self.config.raw_cache_run_id
                / "run-state.json"
            )
            try:
                source_state = json.loads(source_state_path.read_text(encoding="utf-8"))
                self.retrieval_started_at = _parse_utc_text(
                    source_state.get("retrieval_started_at_utc")
                )
            except (OSError, json.JSONDecodeError) as error:
                raise G3AError(
                    FinalClassification.INCONCLUSIVE,
                    "RAW_CACHE_RUN_STATE_INVALID",
                ) from error
        path = self.config.data_root / "manifests" / self.config.run_id / "run-state.json"
        expected = {
            "schema_version": "g3a-run-state/1.0.0",
            "work_order": WORK_ORDER,
            "run_id": self.config.run_id,
            "retrieval_started_at_utc": utc_text(self.retrieval_started_at),
            "requested_end_utc": utc_text(self.config.requested_end_utc),
            "intervals_per_series": self.config.intervals_per_series,
            "raw_cache_run_id": self.config.raw_cache_run_id,
            "offline_cache_only": self.config.offline_cache_only,
        }
        if path.is_file():
            try:
                existing = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as error:
                raise G3AError(FinalClassification.INCONCLUSIVE, "RUN_STATE_INVALID") from error
            stable_fields = {
                key: value for key, value in expected.items() if key != "retrieval_started_at_utc"
            }
            if any(existing.get(key) != value for key, value in stable_fields.items()):
                raise G3AError(FinalClassification.INCONCLUSIVE, "RUN_STATE_MISMATCH")
            self.retrieval_started_at = _parse_utc_text(existing.get("retrieval_started_at_utc"))
            return existing
        write_json_create_only(path, expected)
        self.manifest_paths.append(path)
        return expected

    def _login(self) -> SessionTokens:
        response = self.rest.request(
            "POST",
            "/session",
            version="2",
            body={
                "identifier": self.config.identifier,
                "password": self.config.password,
            },
        )
        if response.status_code != 200 or response.payload is None:
            raise G3AError(FinalClassification.INCONCLUSIVE, "AUTHENTICATION_FAILED")
        cst = response.headers.get("CST")
        xst = response.headers.get("X-SECURITY-TOKEN")
        account_id = response.payload.get("currentAccountId")
        rerouting = response.payload.get("reroutingEnvironment")
        if (
            not isinstance(cst, str)
            or not cst
            or not isinstance(xst, str)
            or not xst
            or account_id != self.config.account_id
            or rerouting == "LIVE"
        ):
            raise G3AError(FinalClassification.INCONCLUSIVE, "DEMO_SESSION_VALIDATION_FAILED")
        return SessionTokens(cst, xst, str(account_id))

    def _load_source_acquisition(self) -> timedelta:
        source_path = self.config.source_acquisition_evidence
        if source_path is None:
            raise G3AError(
                FinalClassification.INCONCLUSIVE,
                "SOURCE_ACQUISITION_EVIDENCE_REQUIRED",
            )
        try:
            body = source_path.read_bytes()
            source = json.loads(body.decode("utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise G3AError(
                FinalClassification.INCONCLUSIVE,
                "SOURCE_ACQUISITION_EVIDENCE_INVALID",
            ) from error
        if not isinstance(source, Mapping):
            raise G3AError(
                FinalClassification.INCONCLUSIVE,
                "SOURCE_ACQUISITION_EVIDENCE_INVALID",
            )
        safety = source.get("safety")
        secret_scan = source.get("secret_scan")
        discovery = source.get("instrument_epic_discovery")
        if (
            source.get("run_id") != self.config.raw_cache_run_id
            or not isinstance(safety, Mapping)
            or safety.get("order_endpoint_call_count") != 0
            or safety.get("execution_adapter_initialized") is not False
            or not isinstance(secret_scan, Mapping)
            or secret_scan.get("status") != "PASS"
            or not isinstance(discovery, list)
            or len(discovery) != len(FROZEN_INSTRUMENTS)
            or not all(
                isinstance(item, Mapping) and item.get("verified") is True for item in discovery
            )
        ):
            raise G3AError(
                FinalClassification.INCONCLUSIVE,
                "SOURCE_ACQUISITION_SAFETY_INVALID",
            )
        time_handling = source.get("time_handling")
        if not isinstance(time_handling, Mapping):
            raise G3AError(
                FinalClassification.TIMEZONE_GAP,
                "SOURCE_ACQUISITION_TIMEZONE_INVALID",
            )
        timezone_offset = _parse_offset_text(time_handling.get("source_timezone"))
        self.source_retrieval_completed_at = _parse_utc_text(
            source.get("retrieval_completed_at_utc")
        )
        try:
            relative = source_path.resolve().relative_to(Path.cwd().resolve()).as_posix()
        except ValueError:
            relative = source_path.name
        self.source_acquisition = {
            "mode": "OFFLINE_IMMUTABLE_RAW_CACHE_REQUALIFICATION",
            "source_evidence_relative_path": relative,
            "source_evidence_sha256": sha256_bytes(body),
            "source_run_id": source.get("run_id"),
            "source_classification": source.get("classification"),
            "source_market_data_get_call_count": safety.get("market_data_get_call_count"),
            "source_order_endpoint_call_count": safety.get("order_endpoint_call_count"),
        }
        return timezone_offset

    def _account_timezone_offset(self) -> timedelta:
        response = self._authenticated_request("GET", "/session", version="1")
        if response.status_code != 200 or response.payload is None:
            raise G3AError(FinalClassification.TIMEZONE_GAP, "SESSION_GET_FAILED")
        value = response.payload.get("timezoneOffset")
        if (
            isinstance(value, bool)
            or not isinstance(value, int | float)
            or not isfinite(float(value))
            or not -24 <= float(value) <= 24
        ):
            raise G3AError(FinalClassification.TIMEZONE_GAP, "ACCOUNT_TIMEZONE_OFFSET_INVALID")
        return timedelta(hours=float(value))

    def _verify_instrument(self, instrument: FrozenInstrument) -> EpicVerification:
        search, search_file = self._request_market_json(
            f"discovery-{instrument.symbol}.json",
            "/markets",
            version="1",
            params={"searchTerm": instrument.symbol},
        )
        detail, detail_file = self._request_market_json(
            f"market-{instrument.symbol}.json",
            f"/markets/{instrument.epic}",
            version="3",
        )
        return verify_epic(
            instrument,
            search,
            detail,
            source_files=(search_file, detail_file),
        )

    def _acquire_series(
        self,
        instrument: FrozenInstrument,
        resolution: str,
        timezone_offset: timedelta,
    ) -> SeriesDataset:
        interval = timedelta(minutes=RESOLUTION_MINUTES[resolution])
        requested_end = self.config.requested_end_utc
        requested_start = requested_end - interval * self.config.intervals_per_series
        query_from = requested_start + timezone_offset
        query_to = requested_end - interval + timezone_offset
        candles = []
        findings = EMPTY_FINDINGS
        source_files: list[RawFileEvidence] = []
        source_metadata: list[dict[str, object]] = []
        allowance_remaining: int | None = None
        total_pages: int | None = None
        for page_number in range(1, self.config.maximum_pages_per_series + 1):
            name = f"prices-{instrument.symbol}-{resolution}-page-{page_number:03d}.json"
            payload, raw_file = self._request_market_json(
                name,
                f"/prices/{instrument.epic}",
                version="3",
                params={
                    "resolution": resolution,
                    "from": _request_time(query_from),
                    "to": _request_time(query_to),
                    "pageSize": self.config.page_size,
                    "pageNumber": page_number,
                },
            )
            page = parse_price_page(payload)
            if page is None:
                raise G3AError(FinalClassification.SCHEMA_GAP, "PRICE_PAGE_SCHEMA_INVALID")
            prices, metadata, actual_page, observed_total = page
            if actual_page != page_number:
                raise G3AError(FinalClassification.SCHEMA_GAP, "PAGE_NUMBER_MISMATCH")
            if total_pages is None:
                total_pages = observed_total
            if (
                observed_total != total_pages
                or observed_total > self.config.maximum_pages_per_series
            ):
                raise G3AError(FinalClassification.SCHEMA_GAP, "PAGINATION_BOUNDS_INVALID")
            source_files.append(raw_file)
            source_metadata.append(_source_metadata(metadata, raw_file, page_number))
            observed_allowance = _allowance_remaining(metadata)
            if observed_allowance is not None:
                allowance_remaining = observed_allowance
            for source_index, raw_candle in enumerate(prices):
                candle, candle_findings = normalize_candle(
                    raw_candle,
                    epic=instrument.epic,
                    resolution=resolution,
                    source_page=page_number,
                    source_index=source_index,
                    source_raw_file=raw_file.relative_path,
                    requested_start_utc=requested_start,
                    requested_end_utc=requested_end,
                )
                findings += candle_findings
                if candle is not None:
                    candles.append(candle)
            if page_number == observed_total:
                break
        else:
            raise G3AError(FinalClassification.SCHEMA_GAP, "PAGINATION_NOT_COMPLETE")
        return SeriesDataset(
            instrument.symbol,
            instrument.instrument_name,
            instrument.epic,
            resolution,
            requested_start,
            requested_end,
            tuple(candles),
            findings,
            tuple(source_files),
            tuple(source_metadata),
            allowance_remaining,
        )

    def _request_market_json(
        self,
        raw_name: str,
        path: str,
        *,
        version: str,
        params: Mapping[str, object] | None = None,
    ) -> tuple[Mapping[str, Any], RawFileEvidence]:
        cached = self.raw.load(raw_name)
        if cached is not None:
            payload, raw_file = cached
            self.raw_files[raw_file.relative_path] = raw_file
            return payload, raw_file
        for attempt in range(1, self.config.maximum_request_attempts + 1):
            try:
                response = self._authenticated_request("GET", path, version=version, params=params)
            except G3AError:
                if attempt == self.config.maximum_request_attempts:
                    raise
                self.rest.sleeper(float(2 ** (attempt - 1)))
                continue
            if response.status_code == 200 and response.payload is not None:
                payload, raw_file = self.raw.persist(raw_name, response.content)
                self.raw_files[raw_file.relative_path] = raw_file
                return payload, raw_file
            with suppress(G3AError):
                error_name = (
                    f"errors/{Path(raw_name).stem}-attempt-{attempt}-"
                    f"http-{response.status_code}.json"
                )
                _, error_file = self.raw.persist(error_name, response.content)
                self.raw_files[error_file.relative_path] = error_file
            if response.error_code == HISTORICAL_ALLOWANCE_ERROR:
                raise G3AError(
                    FinalClassification.API_ALLOWANCE_LIMIT,
                    "HISTORICAL_ALLOWANCE_EXCEEDED",
                )
            retryable = response.status_code in {429, 500, 502, 503, 504} or (
                response.error_code in RETRYABLE_ALLOWANCE_ERRORS
            )
            if not retryable or attempt == self.config.maximum_request_attempts:
                raise G3AError(FinalClassification.INCONCLUSIVE, "MARKET_DATA_GET_REJECTED")
            self.rest.sleeper(_retry_delay(response.headers, attempt))
        raise G3AError(FinalClassification.INCONCLUSIVE, "RETRY_BOUND_EXHAUSTED")

    def _authenticated_request(self, method: str, path: str, **kwargs: Any) -> RestResponse:
        if self.tokens is None:
            raise G3AError(FinalClassification.INCONCLUSIVE, "SESSION_NOT_AVAILABLE")
        return self.rest.request(method, path, headers=self.tokens.headers(), **kwargs)

    def _write_datasets(
        self,
        qualities: Mapping[tuple[str, str], SeriesQuality],
        completed_at: datetime,
    ) -> tuple[str, list[dict[str, object]]]:
        normalized_hashes: list[tuple[str, str, str]] = []
        rows: list[dict[str, object]] = []
        for dataset in self.datasets:
            quality = qualities[(dataset.symbol, dataset.resolution)]
            normalized_path = (
                self.config.data_root
                / "normalized"
                / self.config.run_id
                / f"{dataset.symbol}-{dataset.resolution}.jsonl"
            )
            normalized_body = b"".join(
                canonical_json_bytes(canonical_candle_document(candle)) + b"\n"
                for candle in dataset.candles
            )
            write_create_or_verify(normalized_path, normalized_body)
            normalized_sha = sha256_bytes(normalized_body)
            self.normalized_paths.append(normalized_path)
            normalized_hashes.append((dataset.epic, dataset.resolution, normalized_sha))
            manifest = build_series_manifest(
                dataset,
                quality,
                retrieval_started_at=self.retrieval_started_at,
                retrieval_completed_at=completed_at,
                normalized_relative_path=normalized_path.relative_to(
                    self.config.data_root
                ).as_posix(),
                normalized_sha256=normalized_sha,
            )
            manifest_path = (
                self.config.data_root
                / "manifests"
                / self.config.run_id
                / f"{dataset.symbol}-{dataset.resolution}.manifest.json"
            )
            write_json_create_only(manifest_path, manifest)
            self.manifest_paths.append(manifest_path)
            rows.append(_series_row(dataset, quality, manifest))
        dataset_fingerprint = fingerprint(
            {
                "schema_version": CANONICAL_SCHEMA_VERSION,
                "normalization_version": NORMALIZATION_VERSION,
                "series": sorted(normalized_hashes),
            }
        )
        return dataset_fingerprint, rows

    def _write_run_manifest(
        self,
        *,
        run_state: Mapping[str, object],
        completed_at: datetime,
        classification: FinalClassification,
        dataset_fingerprint: str,
    ) -> Path:
        series_manifests = []
        for path in self.manifest_paths:
            if not path.name.endswith(".manifest.json"):
                continue
            series_manifests.append(
                {
                    "relative_path": path.relative_to(self.config.data_root).as_posix(),
                    "sha256": sha256_bytes(path.read_bytes()),
                }
            )
        payload: dict[str, object] = {
            "schema_version": "g3a-run-manifest/1.0.0",
            "work_order": WORK_ORDER,
            "source": SOURCE,
            "environment": ENVIRONMENT,
            "run_state": dict(run_state),
            "retrieval_completed_at_utc": utc_text(completed_at),
            "classification": classification.value,
            "dataset_fingerprint": dataset_fingerprint,
            "instrument_epic_discovery": json_value(self.epic_results),
            "series_manifests": series_manifests,
        }
        payload["manifest_fingerprint"] = fingerprint(payload)
        path = self.config.data_root / "manifests" / self.config.run_id / "run.manifest.json"
        write_json_create_only(path, payload)
        return path

    def _evidence(
        self,
        *,
        classification: FinalClassification,
        reason: str,
        completed_at: datetime,
        qualification_generated_at: datetime,
        timezone_offset: timedelta | None,
        dataset_fingerprint: str | None,
        series_rows: list[dict[str, object]],
        replay: dict[str, object],
    ) -> dict[str, object]:
        allowances = [
            int(row["allowance_remaining"])
            for row in series_rows
            if row.get("allowance_remaining") is not None
        ]
        return {
            "schema_version": EVIDENCE_SCHEMA_VERSION,
            "work_order": WORK_ORDER,
            "source": SOURCE,
            "environment": ENVIRONMENT,
            "run_id": self.config.run_id,
            "retrieval_started_at_utc": utc_text(self.retrieval_started_at),
            "retrieval_completed_at_utc": utc_text(completed_at),
            "qualification_generated_at_utc": utc_text(qualification_generated_at),
            "classification": classification.value,
            "classification_reason": reason,
            "source_acquisition": self.source_acquisition,
            "dataset_fingerprint": dataset_fingerprint,
            "instrument_epic_discovery": json_value(self.epic_results),
            "series": series_rows,
            "exact_scalper_replay": replay,
            "time_handling": {
                "canonical_timezone": "UTC",
                "source_timezone": (
                    _offset_text(timezone_offset) if timezone_offset is not None else None
                ),
                "source_timestamp_authority": "snapshotTimeUTC",
                "dst_handling": (
                    "UTC is never inferred from snapshotTime. The explicit snapshotTimeUTC "
                    "field is authoritative, so repeated or skipped local DST times do not "
                    "change canonical timestamps."
                ),
                "conversion_rule": (
                    "Parse snapshotTimeUTC as a UTC instant and serialize ISO-8601 +00:00. "
                    "The active-account offset is used only for IG V3 date parameters."
                ),
                "candle_boundary_rule": (
                    "timestamp_utc is the inclusive candle start; requested_end_utc is exclusive."
                ),
                "weekend_handling": (
                    "No fill. Wholly closed weekend gaps are classified separately; the "
                    "one-hour DST edge requires cross-instrument session consensus."
                ),
                "timezone_ambiguity_count": sum(
                    int(row.get("timezone_ambiguity", 0)) for row in series_rows
                ),
            },
            "gap_classification": {
                "expected_weekend_closure": (
                    "Conservative UTC Friday 22:00 through Sunday 21:00 rule."
                ),
                "expected_market_session_gap": (
                    "Same absent candle boundary observed for all three frozen instruments "
                    "at the same resolution."
                ),
                "broker_maintenance": (
                    "Never asserted without an explicit broker maintenance marker; none is "
                    "invented from price absence alone."
                ),
                "actual_missing_data": (
                    "An absent interval not explained by weekend closure or three-instrument "
                    "session consensus."
                ),
                "api_allowance_limitation": (
                    "Only the documented IG historical-allowance error is classified as such."
                ),
                "missing_candles_filled": 0,
            },
            "api_limits": {
                "documented_weekly_historical_points": DOCUMENTED_WEEKLY_ALLOWANCE,
                "requested_maximum_intervals": (
                    len(FROZEN_INSTRUMENTS)
                    * len(RESOLUTION_MINUTES)
                    * self.config.intervals_per_series
                ),
                "page_size": self.config.page_size,
                "maximum_pages_per_series": self.config.maximum_pages_per_series,
                "maximum_request_attempts": self.config.maximum_request_attempts,
                "request_pacing_seconds": self.config.minimum_request_interval_seconds,
                "single_session": True,
                "parallel_sessions_or_connections": 0,
                "resume_cache_enabled": True,
                "raw_cache_source_run_id": self.config.raw_cache_run_id,
                "last_observed_remaining_allowance": min(allowances) if allowances else None,
            },
            "safety": {
                "environment_demo": True,
                "paper_trading_true": True,
                "order_authority": "NONE",
                "order_endpoint_call_count": self.rest.order_endpoint_call_count,
                "working_order_endpoint_called": False,
                "position_creation_endpoint_called": False,
                "execution_adapter_initialized": False,
                "main_bot_started": False,
            },
            "request_history": [],
            "secret_scan": {
                "status": "NOT_RUN",
                "files_scanned": 0,
                "secret_match_count": 0,
                "secret_values_emitted": False,
            },
        }

    def _logout_safely(self) -> None:
        if self.tokens is None:
            return
        try:
            response = self._authenticated_request("DELETE", "/session", version="1")
            self.session_cleanup = (
                "LOGGED_OUT" if response.status_code in {200, 204} else "FAILED_SANITIZED"
            )
        except G3AError:
            self.session_cleanup = "FAILED_SANITIZED"
        finally:
            self.tokens = None

    def _artifact_paths(self) -> list[Path]:
        raw_paths = [
            self.config.data_root / evidence.relative_path for evidence in self.raw_files.values()
        ]
        source_evidence = (
            [self.config.source_acquisition_evidence]
            if self.config.source_acquisition_evidence is not None
            else []
        )
        return [*raw_paths, *source_evidence, *self.normalized_paths, *self.manifest_paths]


def endpoint_is_allowed(
    method: str,
    path: str,
    *,
    version: str,
    params: Mapping[str, object] | None = None,
) -> bool:
    """Bind each permitted path to an exact method, version, and parameter set."""

    method = method.upper()
    params = params or {}
    if method == "POST" and path == "/session":
        return version == "2" and not params
    if method == "DELETE" and path == "/session":
        return version == "1" and not params
    if method == "GET" and path == "/session":
        return version == "1" and not params
    if method == "GET" and path == "/markets":
        return version == "1" and set(params) == {"searchTerm"}
    if method == "GET" and path.startswith("/markets/"):
        epic = path.removeprefix("/markets/")
        return version == "3" and not params and bool(EPIC_PATTERN.fullmatch(epic))
    if method == "GET" and path.startswith("/prices/"):
        epic = path.removeprefix("/prices/")
        return (
            version == "3"
            and bool(EPIC_PATTERN.fullmatch(epic))
            and set(params) == {"resolution", "from", "to", "pageSize", "pageNumber"}
            and isinstance(params.get("resolution"), str)
            and params.get("resolution") in HISTORICAL_PRICE_RESOLUTIONS
        )
    return False


def parse_price_page(
    payload: object,
) -> tuple[list[object], Mapping[str, object], int, int] | None:
    if not isinstance(payload, Mapping):
        return None
    prices = payload.get("prices")
    metadata = payload.get("metadata")
    if not isinstance(prices, list) or not isinstance(metadata, Mapping):
        return None
    page_data = metadata.get("pageData")
    if not isinstance(page_data, Mapping):
        return None
    page_number = _positive_integer(page_data.get("pageNumber"))
    total_pages = _positive_integer(page_data.get("totalPages"))
    if page_number is None or total_pages is None or page_number > total_pages:
        return None
    return prices, metadata, page_number, total_pages


def safe_raw_payload(body: bytes) -> Mapping[str, Any]:
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise G3AError(FinalClassification.SCHEMA_GAP, "RAW_JSON_INVALID") from error
    if not isinstance(payload, Mapping):
        raise G3AError(FinalClassification.SCHEMA_GAP, "RAW_JSON_SHAPE_INVALID")
    if contains_forbidden_raw_key(payload):
        raise G3AError(
            FinalClassification.DATA_QUALITY_FAILURE,
            "RAW_RESPONSE_CONTAINS_SENSITIVE_FIELD",
        )
    return payload


def contains_forbidden_raw_key(value: object) -> bool:
    forbidden = {_normalized_key(item) for item in FORBIDDEN_RAW_KEYS}
    if isinstance(value, Mapping):
        return any(
            _normalized_key(str(key)) in forbidden or contains_forbidden_raw_key(item)
            for key, item in value.items()
        )
    if isinstance(value, list):
        return any(contains_forbidden_raw_key(item) for item in value)
    return False


def scan_for_secrets(
    paths: Iterable[Path],
    secrets: Iterable[str],
    *,
    prospective_evidence: Mapping[str, object] | None = None,
) -> dict[str, object]:
    encoded_secrets = tuple(
        value.encode("utf-8") for value in secrets if isinstance(value, str) and len(value) >= 4
    )
    scanned = matches = 0
    for path in paths:
        if not path.is_file():
            continue
        body = path.read_bytes()
        scanned += 1
        if any(secret in body for secret in encoded_secrets):
            matches += 1
    if prospective_evidence is not None:
        body = pretty_json_bytes(prospective_evidence)
        scanned += 1
        if any(secret in body for secret in encoded_secrets):
            matches += 1
    return {
        "status": "PASS" if matches == 0 else "FAIL",
        "files_scanned": scanned,
        "secret_match_count": matches,
        "secret_values_emitted": False,
    }


def write_create_or_verify(path: Path, body: bytes) -> None:
    """Atomically create an artifact, or accept an identical cached artifact."""

    if path.exists():
        if path.is_file() and path.read_bytes() == body:
            return
        raise G3AError(FinalClassification.INCONCLUSIVE, "IMMUTABLE_OUTPUT_CONFLICT")
    temporary = path.with_suffix(path.suffix + ".tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with temporary.open("xb") as stream:
            stream.write(body)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, path)
    except OSError as error:
        raise G3AError(FinalClassification.INCONCLUSIVE, "OUTPUT_WRITE_FAILED") from error
    finally:
        with suppress(OSError):
            temporary.unlink(missing_ok=True)


def write_json_create_only(path: Path, value: object) -> None:
    write_create_or_verify(path, pretty_json_bytes(value))


def render_markdown(evidence: Mapping[str, object]) -> str:
    discovery = evidence.get("instrument_epic_discovery", [])
    series = evidence.get("series", [])
    safety = evidence.get("safety", {})
    replay = evidence.get("exact_scalper_replay", {})
    lines = [
        "# G3A-01 Authoritative Market Data Evidence",
        "",
        f"Final classification: **{evidence.get('classification')}**",
        "",
        "## Instrument and EPIC discovery",
        "",
        "| Instrument | Configured EPIC | Discovered EPIC | Detail EPIC | Result |",
        "|---|---|---|---|---|",
    ]
    if isinstance(discovery, list):
        for item in discovery:
            if isinstance(item, Mapping):
                lines.append(
                    "| {symbol} | {configured_epic} | {discovered_epic} | "
                    "{detail_epic} | {reason} |".format(**item)
                )
    lines.extend(
        [
            "",
            "## Qualified series",
            "",
            "| EPIC | Resolution | Start UTC | End UTC | Candle Count | Missing | "
            "Duplicates | Quality Status | Fingerprint |",
            "|---|---:|---|---|---:|---:|---:|---|---|",
        ]
    )
    if isinstance(series, list):
        for item in series:
            if isinstance(item, Mapping):
                lines.append(
                    "| {epic} | {resolution_label} | {actual_start_utc} | "
                    "{actual_end_utc} | {candle_count} | {missing_intervals} | "
                    "{duplicate_timestamps} | {quality_status} | `{fingerprint}` |".format(**item)
                )
    lines.extend(
        [
            "",
            "## Replay and safety declaration",
            "",
            f"- Exact Scalper replay ready: `{_mapping_value(replay, 'ready')}`",
            f"- Common 1M candle count: `{_mapping_value(replay, 'common_minute_candle_count')}`",
            f"- Order endpoint call count: `{_mapping_value(safety, 'order_endpoint_call_count')}`",
            "- Execution adapter initialized: "
            f"`{_mapping_value(safety, 'execution_adapter_initialized')}`",
            f"- Dataset fingerprint: `{evidence.get('dataset_fingerprint')}`",
            "",
            "No missing candle was filled or presented as broker evidence.",
            "",
        ]
    )
    return "\n".join(lines)


def load_config(args: argparse.Namespace) -> G3AConfig:
    """Validate Demo/paper state before retaining any credential value."""

    dotenv_path = Path(args.dotenv)
    dotenv_keys = _dotenv_keys(dotenv_path)
    live_keys = sorted(key for key in dotenv_keys if key.upper().startswith("IG_LIVE"))
    live_keys.extend(key for key in os.environ if key.upper().startswith("IG_LIVE"))
    if live_keys:
        raise G3AError(FinalClassification.INCONCLUSIVE, "LIVE_CREDENTIAL_CONFIGURATION_PRESENT")
    preflight = _selected_dotenv_values(dotenv_path, set(PREFLIGHT_ENV_NAMES))
    base_url = _configured_value("IG_BASE_URL", preflight) or DEMO_REST_BASE_URL
    parsed = urlparse(base_url)
    if (
        parsed.scheme != "https"
        or parsed.hostname != DEMO_REST_HOST
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path.rstrip("/") not in {"", "/gateway/deal"}
    ):
        raise G3AError(FinalClassification.INCONCLUSIVE, "DEMO_HOSTNAME_REQUIRED")
    if not _parse_bool(_configured_value("IG_DEMO", preflight), default=True):
        raise G3AError(FinalClassification.INCONCLUSIVE, "IG_DEMO_MUST_BE_TRUE")
    if not _parse_bool(_configured_value("PAPER_TRADING", preflight), default=True):
        raise G3AError(FinalClassification.INCONCLUSIVE, "PAPER_TRADING_MUST_BE_TRUE")
    secret_values = _selected_dotenv_values(dotenv_path, set(SECRET_ENV_NAMES))
    accounts = {
        name: _configured_value(name, secret_values)
        for name in ACCOUNT_ENV_NAMES
        if _configured_value(name, secret_values)
    }
    if len(set(accounts.values())) > 1:
        raise G3AError(FinalClassification.INCONCLUSIVE, "ACCOUNT_CONFIG_AMBIGUOUS")
    account_id = next(iter(accounts.values()), "")
    required = {
        "IG_API_KEY": _configured_value("IG_API_KEY", secret_values),
        "IG_IDENTIFIER": _configured_value("IG_IDENTIFIER", secret_values),
        "IG_PASSWORD": _configured_value("IG_PASSWORD", secret_values),
        "IG_ACCOUNT_ID": account_id,
    }
    if any(not value for value in required.values()):
        raise G3AError(FinalClassification.INCONCLUSIVE, "MISSING_REQUIRED_CONFIG")
    try:
        return G3AConfig(
            environment="demo",
            base_url=DEMO_REST_BASE_URL,
            paper_trading=True,
            data_root=Path(args.data_root),
            evidence_json=Path(args.evidence_json),
            run_id=str(args.run_id),
            requested_end_utc=args.end_utc,
            intervals_per_series=int(args.intervals_per_series),
            api_key=required["IG_API_KEY"],
            identifier=required["IG_IDENTIFIER"],
            password=required["IG_PASSWORD"],
            account_id=required["IG_ACCOUNT_ID"],
            raw_cache_run_id=(str(args.raw_cache_run_id) if args.raw_cache_run_id else None),
            offline_cache_only=bool(args.offline_cache_only),
            source_acquisition_evidence=(
                Path(args.source_acquisition_evidence) if args.source_acquisition_evidence else None
            ),
        )
    except ValueError as error:
        raise G3AError(FinalClassification.INCONCLUSIVE, "CONFIGURATION_INVALID") from error


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="G3A-01 GET-only IG Demo canonical market-data pipeline"
    )
    parser.add_argument("--environment", required=True, choices=("demo",))
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--end-utc", required=True, type=_cli_time)
    parser.add_argument(
        "--intervals-per-series",
        type=int,
        default=DEFAULT_INTERVALS_PER_SERIES,
    )
    parser.add_argument("--data-root", type=Path, default=DEFAULT_DATA_ROOT)
    parser.add_argument("--evidence-json", type=Path, default=DEFAULT_EVIDENCE_JSON)
    parser.add_argument("--raw-cache-run-id")
    parser.add_argument("--offline-cache-only", action="store_true")
    parser.add_argument("--source-acquisition-evidence", type=Path)
    parser.add_argument("--dotenv", type=Path, default=Path(".env"))
    return parser


def main(argv: list[str] | None = None) -> ExitCode:
    args = build_parser().parse_args(argv)
    try:
        config = load_config(args)
    except G3AError as error:
        return _failure(ExitCode.CONFIGURATION_FAILURE, error)
    try:
        result = G3APipeline(config).run()
    except G3AError as error:
        code = (
            ExitCode.AUTHENTICATION_FAILURE
            if error.reason in {"AUTHENTICATION_FAILED", "DEMO_SESSION_VALIDATION_FAILED"}
            else ExitCode.ACQUISITION_OR_QUALITY_FAILURE
        )
        return _failure(code, error)
    print("G3A_DATA_PIPELINE_COMPLETE")
    print(f"classification={result.classification.value}")
    print(f"series_count={len(result.normalized_paths)}")
    print("order_endpoint_call_count=0")
    return ExitCode.SUCCESS


def _failure(code: ExitCode, error: G3AError) -> ExitCode:
    print("G3A_DATA_PIPELINE_FAILED", file=sys.stderr)
    print(f"code={code.name}", file=sys.stderr)
    print(f"classification={error.classification.value}", file=sys.stderr)
    print(f"reason={error.reason}", file=sys.stderr)
    print("order_endpoint_call_count=0", file=sys.stderr)
    return code


def _series_row(
    dataset: SeriesDataset,
    quality: SeriesQuality,
    manifest: Mapping[str, object],
) -> dict[str, object]:
    normalized = manifest.get("normalized_data")
    normalized_hash = normalized.get("sha256") if isinstance(normalized, Mapping) else None
    gap_counts: dict[str, int] = {}
    for gap in quality.detected_gaps:
        gap_counts[gap.classification.value] = (
            gap_counts.get(gap.classification.value, 0) + gap.missing_intervals
        )
    return {
        "symbol": dataset.symbol,
        "instrument_name": dataset.instrument_name,
        "epic": dataset.epic,
        "resolution": dataset.resolution,
        "resolution_label": RESOLUTION_LABELS[dataset.resolution],
        "requested_start_utc": utc_text(dataset.requested_start_utc),
        "requested_end_utc": utc_text(dataset.requested_end_utc),
        "actual_start_utc": utc_text(quality.actual_start_utc),
        "actual_end_utc": utc_text(quality.actual_end_utc),
        "candle_count": quality.candle_count,
        "expected_count": quality.expected_count,
        "missing_intervals": quality.missing_intervals,
        "missing_by_classification": gap_counts,
        "duplicate_timestamps": quality.duplicate_timestamps,
        "non_monotonic_timestamps": quality.non_monotonic_timestamps,
        "invalid_ohlc": quality.invalid_ohlc,
        "crossed_bid_offer_anomalies": quality.crossed_bid_offer_anomalies,
        "zero_or_negative_spread_anomalies": quality.zero_or_negative_spread_anomalies,
        "stale_sequence_count": len(quality.stale_sequences),
        "large_market_gap_count": len(quality.large_market_gaps),
        "timezone_ambiguity": quality.timezone_ambiguity,
        "quality_status": quality.status.value,
        "quality_reason_codes": list(quality.reason_codes),
        "fingerprint": normalized_hash,
        "manifest_fingerprint": manifest.get("manifest_fingerprint"),
        "allowance_remaining": dataset.allowance_remaining,
    }


def _source_metadata(
    metadata: Mapping[str, object], raw_file: RawFileEvidence, page_number: int
) -> dict[str, object]:
    result: dict[str, object] = {
        "page_number": page_number,
        "source_raw_file": raw_file.relative_path,
        "source_raw_sha256": raw_file.sha256,
    }
    for key in ("pageData", "allowance"):
        if isinstance(metadata.get(key), Mapping):
            result[key] = json_value(metadata[key])
    return result


def _allowance_remaining(metadata: Mapping[str, object]) -> int | None:
    allowance = metadata.get("allowance")
    if not isinstance(allowance, Mapping):
        return None
    value = allowance.get("remainingAllowance")
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _unavailable_replay(reason: str) -> dict[str, object]:
    return {
        "strategy": "Scalper",
        "optimization_run": False,
        "required_resolutions": list(RESOLUTION_MINUTES),
        "minimum_candles_per_resolution": MINIMUM_SCALPER_CANDLES,
        "common_minute_candle_count": 0,
        "common_minute_start_utc": None,
        "common_minute_end_utc": None,
        "ready": False,
        "reason_codes": [reason],
    }


def _safe_error_code(payload: Mapping[str, Any] | None) -> str | None:
    if payload is None:
        return None
    value = payload.get("errorCode")
    return value if isinstance(value, str) and value else None


def _retry_delay(headers: Mapping[str, str], attempt: int) -> float:
    try:
        value = float(headers.get("Retry-After", ""))
    except (TypeError, ValueError):
        value = float(2 ** (attempt - 1))
    return max(0.0, min(value, 10.0))


def _positive_integer(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


def _normalized_key(value: str) -> str:
    return value.replace("-", "").replace("_", "").casefold()


def _mapping_value(value: object, key: str) -> object:
    return value.get(key) if isinstance(value, Mapping) else None


def _offset_text(value: timedelta) -> str:
    seconds = int(value.total_seconds())
    sign = "+" if seconds >= 0 else "-"
    absolute = abs(seconds)
    return (
        f"IG active-account fixed offset UTC{sign}{absolute // 3600:02d}:"
        f"{absolute % 3600 // 60:02d}"
    )


def _parse_offset_text(value: object) -> timedelta:
    if not isinstance(value, str):
        raise G3AError(
            FinalClassification.TIMEZONE_GAP,
            "SOURCE_ACQUISITION_TIMEZONE_INVALID",
        )
    match = re.fullmatch(
        r"IG active-account fixed offset UTC(?P<sign>[+-])(?P<hours>\d{2}):(?P<minutes>\d{2})",
        value,
    )
    if match is None:
        raise G3AError(
            FinalClassification.TIMEZONE_GAP,
            "SOURCE_ACQUISITION_TIMEZONE_INVALID",
        )
    hours = int(match.group("hours"))
    minutes = int(match.group("minutes"))
    if hours > 24 or minutes > 59 or (hours == 24 and minutes):
        raise G3AError(
            FinalClassification.TIMEZONE_GAP,
            "SOURCE_ACQUISITION_TIMEZONE_INVALID",
        )
    result = timedelta(hours=hours, minutes=minutes)
    return result if match.group("sign") == "+" else -result


def _request_time(value: datetime) -> str:
    return value.strftime("%Y-%m-%dT%H:%M:%S")


def _aware_utc(value: object) -> bool:
    return (
        isinstance(value, datetime)
        and value.tzinfo is not None
        and value.utcoffset() == timedelta(0)
    )


def _parse_utc_text(value: object) -> datetime:
    if not isinstance(value, str):
        raise G3AError(FinalClassification.INCONCLUSIVE, "RUN_STATE_TIME_INVALID")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise G3AError(FinalClassification.INCONCLUSIVE, "RUN_STATE_TIME_INVALID") from error
    if not _aware_utc(parsed):
        raise G3AError(FinalClassification.INCONCLUSIVE, "RUN_STATE_TIME_INVALID")
    return parsed.astimezone(UTC)


def _cli_time(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise argparse.ArgumentTypeError("timestamp must be ISO-8601 UTC") from error
    if not _aware_utc(parsed):
        raise argparse.ArgumentTypeError("timestamp must have explicit UTC offset")
    return parsed.astimezone(UTC)


def _parse_bool(value: str | None, *, default: bool) -> bool:
    if value is None or not value.strip():
        return default
    normalized = value.strip().casefold()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise G3AError(FinalClassification.INCONCLUSIVE, "BOOLEAN_CONFIG_INVALID")


def _dotenv_keys(path: Path) -> set[str]:
    if not path.exists():
        return set()
    keys: set[str] = set()
    for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key = line.split("=", 1)[0].strip()
        if key.casefold().startswith("export "):
            key = key[7:].strip()
        if key:
            keys.add(key)
    return keys


def _selected_dotenv_values(path: Path, names: set[str]) -> dict[str, str]:
    if not path.exists():
        return {}
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        raw_key, raw_value = line.split("=", 1)
        key = raw_key.strip()
        if key.casefold().startswith("export "):
            key = key[7:].strip()
        if key not in names:
            continue
        value = raw_value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        if "${" in value:
            raise G3AError(FinalClassification.INCONCLUSIVE, "DOTENV_INTERPOLATION_UNSUPPORTED")
        values[key] = value
    return values


def _configured_value(name: str, dotenv_values: Mapping[str, str]) -> str:
    return os.environ.get(name, dotenv_values.get(name, "")).strip()


if __name__ == "__main__":
    raise SystemExit(main())
