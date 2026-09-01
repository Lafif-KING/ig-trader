"""Local orchestration for the observation-only Shadow01 tournament.

This module owns no credentials and creates no broker client.  A caller must
explicitly inject the narrow ``Shadow01ReadOnlyBroker`` adapter after it has
been configured elsewhere.  In particular, importing or using this module
cannot authenticate, submit an order, start a Demo process, or create an
epoch.
"""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from math import isfinite
from pathlib import Path

from src.ig_trader.shadow01.clock import (
    ClockAvailability,
    ShadowClockError,
    assess_universal_clock,
    new_york_local_date,
    require_decision_anchor,
)
from src.ig_trader.shadow01.config import ShadowTournamentConfig
from src.ig_trader.shadow01.data import (
    ShadowDataError,
    cache_history,
    load_cached_history,
    parse_completed_daily_bars,
)
from src.ig_trader.shadow01.engines import (
    CostInputs,
    assess_c1_cost,
    assess_q1_quality,
    build_f1_context,
    compute_technical_state,
    evaluate_m1_reversion,
    evaluate_t1_trend,
    evaluate_x1_context,
)
from src.ig_trader.shadow01.live_quote import ShadowLiveQuote
from src.ig_trader.shadow01.models import (
    DailyBar,
    MarketDataState,
    MarketSnapshot,
    MarketSpec,
    OutcomeLabel,
    QualityState,
    document,
    fingerprint,
    require_utc,
)
from src.ig_trader.shadow01.outcomes import OutcomeResolutionInput, resolve_outcomes
from src.ig_trader.shadow01.policies import evaluate_policies, materialize_decisions
from src.ig_trader.shadow01.read_only_broker import (
    ReadOnlyBrokerRequestCounters,
    Shadow01ReadOnlyBroker,
)
from src.ig_trader.shadow01.registry import ShadowMarketRegistry
from src.ig_trader.shadow01.storage import ShadowStoreError, ShadowTournamentStore

_ANCHOR_GRACE_SECONDS = 1.0


class ShadowRuntimeError(RuntimeError):
    """The local Shadow01 orchestration preconditions were not met safely."""


@dataclass(frozen=True)
class Shadow01MarketResult:
    """One explicit market result; unavailable never means substituted."""

    instrument: str
    epic: str | None
    status: str
    reason_codes: tuple[str, ...] = ()
    decisions_recorded: int = 0
    outcomes_resolved: int = 0

    def document(self) -> dict[str, object]:
        return {
            "instrument": self.instrument,
            "epic": self.epic,
            "status": self.status,
            "reason_codes": list(self.reason_codes),
            "decisions_recorded": self.decisions_recorded,
            "outcomes_resolved": self.outcomes_resolved,
        }


@dataclass(frozen=True)
class Shadow01RuntimeResult:
    """A safe, displayable outcome of one probe, cycle, or monitor check."""

    status: str
    observed_at_utc: datetime | None
    market_results: tuple[Shadow01MarketResult, ...] = ()
    decisions_recorded: int = 0
    outcomes_resolved: int = 0
    detail: str | None = None

    def document(self) -> dict[str, object]:
        return {
            "status": self.status,
            "observed_at_utc": self.observed_at_utc.isoformat() if self.observed_at_utc else None,
            "execution_authority": "OFF",
            "decisions_recorded": self.decisions_recorded,
            "outcomes_resolved": self.outcomes_resolved,
            "detail": self.detail,
            "markets": [item.document() for item in self.market_results],
        }


class Shadow01Runtime:
    """Run a daily observation cycle through only proven, read-only facts."""

    execution_authority = "OFF"

    def __init__(
        self,
        *,
        config: ShadowTournamentConfig,
        store: ShadowTournamentStore,
        registry: ShadowMarketRegistry | None,
        broker: Shadow01ReadOnlyBroker | None,
        canonical_quote_provider: Callable[[MarketSpec, datetime], ShadowLiveQuote | None]
        | None = None,
        history_cache_directory: Path | None = None,
        stop_marker_path: Path | None = None,
    ) -> None:
        self.config = config
        self.store = store
        self.registry = registry
        self.broker = broker
        self.canonical_quote_provider = canonical_quote_provider
        self.history_cache_directory = (
            history_cache_directory or store.path.parent / "shadow01-history"
        )
        self.stop_marker_path = stop_marker_path or store.path.parent / "shadow01-monitor.stop"

    def status(self) -> dict[str, object]:
        """Report local readiness without creating an epoch or contacting a provider."""

        epoch: datetime | None
        storage_status = "READY"
        try:
            epoch = self.store.epoch(self.config)
        except (OSError, ShadowStoreError, sqlite3.Error):
            epoch = None
            storage_status = "SHADOW01_STORAGE_UNREADABLE"
        registry_status, verified, unavailable = self._registry_status()
        provider_status = "READY" if self.broker is not None else "SHADOW01_PROVIDER_REQUIRED"
        state = _first_blocker(
            "SHADOW01_MONITOR_STOP_REQUESTED" if self.stop_requested() else None,
            storage_status if storage_status != "READY" else None,
            "SHADOW01_EPOCH_NOT_CREATED" if epoch is None else None,
            registry_status if registry_status != "READY" else None,
            provider_status if provider_status != "READY" else None,
            "SHADOW01_STATUS_READY",
        )
        counters = (
            self.broker.request_counters_document()
            if self.broker is not None
            else ReadOnlyBrokerRequestCounters.zero().document()
        )
        return {
            "status": state,
            "execution_authority": "OFF",
            "tournament_version": self.config.version,
            "config_fingerprint": self.config.fingerprint,
            "epoch_utc": epoch.isoformat() if epoch is not None else None,
            "epoch_created": epoch is not None,
            "registry_status": registry_status,
            "verified_market_count": verified,
            "unavailable_market_count": unavailable,
            "provider_status": provider_status,
            "broker_constructed": self.broker is not None,
            "stop_requested": self.stop_requested(),
            "broker_request_counters": counters,
            "execution_safety_counters": counters["execution_safety_counters"],
        }

    def stop_requested(self) -> bool:
        """Return whether the Shadow-only local monitor marker is present."""

        return self.stop_marker_path.is_file()

    def request_stop(self, *, requested_at: datetime | None = None) -> Shadow01RuntimeResult:
        """Write only the Shadow monitor stop marker; it cannot affect other workers."""

        timestamp = require_utc(requested_at or datetime.now(UTC))
        self.stop_marker_path.parent.mkdir(parents=True, exist_ok=True)
        self.stop_marker_path.write_text(
            json.dumps(
                {
                    "scope": "SHADOW01_MONITOR_ONLY",
                    "requested_at_utc": timestamp.isoformat(),
                    "execution_authority": "OFF",
                },
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        return Shadow01RuntimeResult("SHADOW01_MONITOR_STOP_REQUESTED", timestamp)

    def pre_epoch_provider_probe(self, *, observed_at: datetime) -> Shadow01RuntimeResult:
        """Perform one optional, read-only provider health probe without a decision."""

        try:
            timestamp = require_decision_anchor(self.config, observed_at)
        except ShadowClockError:
            return Shadow01RuntimeResult("SHADOW01_PROVIDER_PROBE_TIMESTAMP_INVALID", None)
        try:
            if self.store.epoch(self.config) is not None:
                return Shadow01RuntimeResult(
                    "SHADOW01_PRE_EPOCH_PROBE_SKIPPED_EPOCH_ALREADY_EXISTS", timestamp
                )
        except (OSError, ShadowStoreError, sqlite3.Error):
            return Shadow01RuntimeResult("SHADOW01_STORAGE_UNREADABLE", timestamp)

        registry_status, _, _ = self._registry_status()
        if registry_status != "READY":
            self._record_provider_health(timestamp, "REGISTRY_UNAVAILABLE", registry_status)
            return Shadow01RuntimeResult(registry_status, timestamp)
        if self.broker is None:
            self._record_provider_health(
                timestamp,
                "PROVIDER_UNAVAILABLE",
                "SHADOW01_PROVIDER_REQUIRED",
            )
            return Shadow01RuntimeResult("SHADOW01_PROVIDER_REQUIRED", timestamp)

        availability: list[ClockAvailability] = []
        results: list[Shadow01MarketResult] = []
        for asset_class in ("FX", "METAL", "INDEX"):
            market = next(
                (
                    item
                    for item in self.registry.markets
                    if item.asset_class.value == asset_class
                    and item.state is MarketDataState.AVAILABLE
                    and item.epic is not None
                ),
                None,
            )
            if market is None:
                availability.append(
                    ClockAvailability(asset_class, False, False, "DQ03_MARKET_UNAVAILABLE")
                )
                results.append(
                    Shadow01MarketResult(
                        asset_class,
                        None,
                        "MARKET_DATA_UNAVAILABLE",
                        ("DQ03_MARKET_UNAVAILABLE",),
                    )
                )
                self._record_provider_health(
                    timestamp,
                    "CLOCK_UNAVAILABLE",
                    f"SHADOW01_{asset_class}_DQ03_MARKET_UNAVAILABLE",
                )
                continue
            availability.append(self._probe_clock_availability(market, timestamp, results))

        clock_status, blockers = assess_universal_clock(tuple(availability))
        if clock_status != "SHADOW01_SESSION_CLOCK_VERIFIED":
            self._record_provider_health(
                timestamp,
                "CLOCK_UNAVAILABLE",
                clock_status,
                {"asset_class_blockers": list(blockers)},
            )
            return Shadow01RuntimeResult(clock_status, timestamp, tuple(results))

        if not self._has_full_verified_registry():
            self._record_provider_health(
                timestamp,
                "READINESS_INCOMPLETE",
                "SHADOW01_EPOCH_READINESS_20_PROVEN_MARKETS_REQUIRED",
                {"verified_market_count": self.registry.verified_count},
            )
            return Shadow01RuntimeResult(
                "SHADOW01_EPOCH_READINESS_20_PROVEN_MARKETS_REQUIRED",
                timestamp,
                tuple(results),
            )

        readiness_snapshot: MarketSnapshot | None = None
        readiness_evidence: list[dict[str, object]] = []
        for market in self.registry.markets:
            snapshot, market_result = self._read_pre_epoch_snapshot(market, timestamp)
            results.append(market_result)
            if snapshot is None:
                self._record_provider_health(
                    timestamp,
                    "READINESS_INCOMPLETE",
                    "SHADOW01_EPOCH_READINESS_MARKET_DATA_REQUIRED",
                    {"instrument": market.symbol, "epic": market.epic},
                )
                return Shadow01RuntimeResult(
                    "SHADOW01_EPOCH_READINESS_MARKET_DATA_REQUIRED",
                    timestamp,
                    tuple(results),
                )
            readiness_snapshot = readiness_snapshot or snapshot
            readiness_evidence.append(
                {
                    "instrument": snapshot.instrument,
                    "epic": snapshot.epic,
                    "input_data_fingerprint": snapshot.input_data_fingerprint,
                    "latest_completed_bar_utc": snapshot.completed_bars[-1].completed_at,
                }
            )

        readiness_fingerprint = fingerprint(readiness_evidence)
        if readiness_snapshot is not None:
            readiness_snapshot = MarketSnapshot(
                decision_timestamp_utc=readiness_snapshot.decision_timestamp_utc,
                instrument=readiness_snapshot.instrument,
                epic=readiness_snapshot.epic,
                asset_class=readiness_snapshot.asset_class,
                completed_bars=readiness_snapshot.completed_bars,
                metadata={
                    **readiness_snapshot.metadata,
                    "readiness_summary": {
                        "configured_market_count": len(self.registry.markets),
                        "causal_snapshot_count": len(readiness_evidence),
                        "causal_snapshot_fingerprint": readiness_fingerprint,
                    },
                },
                data_quality=readiness_snapshot.data_quality,
                input_data_fingerprint=readiness_snapshot.input_data_fingerprint,
            )

        self._record_provider_health(
            timestamp,
            "HEALTHY",
            "SHADOW01_READ_ONLY_CLOCK_PROBE_OK",
            {
                "asset_classes": [item.asset_class for item in availability],
                "verified_market_count": self.registry.verified_count,
                "causal_snapshot_count": len(readiness_evidence),
                "causal_snapshot_fingerprint": readiness_fingerprint,
                "readiness_snapshot_recorded": readiness_snapshot is not None,
            },
        )
        if readiness_snapshot is None:
            return Shadow01RuntimeResult(
                "SHADOW01_EPOCH_READINESS_MARKET_DATA_REQUIRED",
                timestamp,
                tuple(results),
            )
        try:
            self.store.record_epoch_readiness(
                self.config,
                snapshot=readiness_snapshot,
                provider_probe_observed_at=timestamp,
            )
        except (OSError, ShadowStoreError, sqlite3.Error, ValueError) as error:
            return Shadow01RuntimeResult(
                "SHADOW01_EPOCH_READINESS_RECORD_FAILED",
                timestamp,
                tuple(results),
                detail=_error_code(error),
            )
        return Shadow01RuntimeResult(
            "SHADOW01_PRE_EPOCH_READINESS_RECORDED",
            timestamp,
            tuple(results),
        )

    def run_observation_cycle(self, *, observed_at: datetime) -> Shadow01RuntimeResult:
        """Record one anchored, post-epoch observation cycle and its four policies."""

        try:
            timestamp = require_decision_anchor(self.config, observed_at)
        except ShadowClockError as error:
            return Shadow01RuntimeResult(str(error), _safe_utc(observed_at))
        if self.stop_requested():
            return Shadow01RuntimeResult("SHADOW01_MONITOR_STOP_REQUESTED", timestamp)
        try:
            epoch = self.store.epoch(self.config)
        except (OSError, ShadowStoreError, sqlite3.Error):
            return Shadow01RuntimeResult("SHADOW01_STORAGE_UNREADABLE", timestamp)
        if epoch is None:
            return Shadow01RuntimeResult("SHADOW01_EPOCH_NOT_CREATED", timestamp)
        if timestamp < epoch:
            return Shadow01RuntimeResult("SHADOW01_NO_RETROSPECTIVE_DECISIONS", timestamp)

        registry_status, _, _ = self._registry_status()
        if registry_status != "READY":
            return Shadow01RuntimeResult(
                registry_status,
                timestamp,
                self._unavailable_market_results(),
            )
        if self.broker is None:
            return Shadow01RuntimeResult(
                "SHADOW01_PROVIDER_REQUIRED",
                timestamp,
                self._unavailable_market_results(),
            )

        results: list[Shadow01MarketResult] = []
        for market in self.registry.markets:
            if market.state is not MarketDataState.AVAILABLE or market.epic is None:
                results.append(
                    Shadow01MarketResult(
                        market.symbol,
                        None,
                        "MARKET_DATA_UNAVAILABLE",
                        (market.reason or "DQ03_MARKET_UNAVAILABLE",),
                    )
                )
                continue
            results.append(self._observe_market(market, timestamp))
        decisions_recorded = sum(item.decisions_recorded for item in results)
        outcomes_resolved = sum(item.outcomes_resolved for item in results)
        status = (
            "SHADOW01_OBSERVATION_RECORDED"
            if decisions_recorded
            else (
                "SHADOW01_OBSERVATION_ALREADY_RECORDED"
                if results
                and all(
                    item.status == "SHADOW01_MARKET_OBSERVATION_ALREADY_RECORDED"
                    for item in results
                )
                else "SHADOW01_OBSERVATION_NO_MARKETS_RECORDED"
            )
        )
        return Shadow01RuntimeResult(
            status,
            timestamp,
            tuple(results),
            decisions_recorded,
            outcomes_resolved,
        )

    def monitor(
        self,
        *,
        poll_interval_seconds: float = 60.0,
        now: Callable[[], datetime] | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> Iterator[Shadow01RuntimeResult]:
        """Yield anchored observation cycles until the Shadow-only marker is present.

        The worker deliberately does not poll the broker every ``poll_interval``.
        It uses that interval only to wake up and check its local stop marker while
        waiting for the next frozen 17:10 New York anchor.  This keeps every
        provider read and every decision tied to one canonical timestamp per
        New York market day.
        """

        if poll_interval_seconds <= 0:
            raise ShadowRuntimeError("SHADOW01_MONITOR_POLL_INTERVAL_INVALID")
        clock = now or (lambda: datetime.now(UTC))
        preflight_status = str(self.status()["status"])
        if preflight_status != "SHADOW01_STATUS_READY":
            yield Shadow01RuntimeResult(
                preflight_status,
                _safe_utc(clock()),
                self._unavailable_market_results(),
            )
            return
        last_anchor: datetime | None = None
        scheduled_anchor: datetime | None = None
        while True:
            if self.stop_requested():
                yield Shadow01RuntimeResult("SHADOW01_MONITOR_STOP_REQUESTED", last_anchor)
                return
            observed_now = clock()
            try:
                current = require_utc(observed_now)
                if scheduled_anchor is None:
                    latest_anchor = _latest_decision_anchor(self.config, current)
                    if (
                        latest_anchor is not None
                        and latest_anchor != last_anchor
                        and new_york_local_date(latest_anchor) == new_york_local_date(current)
                    ):
                        late_seconds = (current - latest_anchor).total_seconds()
                        if late_seconds > _ANCHOR_GRACE_SECONDS:
                            if self._anchor_is_fully_recorded(latest_anchor):
                                result = self.run_observation_cycle(observed_at=latest_anchor)
                                yield result
                            else:
                                yield Shadow01RuntimeResult(
                                    "SHADOW01_MONITOR_ANCHOR_MISSED",
                                    latest_anchor,
                                    detail="SHADOW01_MONITOR_WAKE_LATE",
                                )
                            last_anchor = latest_anchor
                            continue
                        scheduled_anchor = latest_anchor
                    else:
                        scheduled_anchor = _next_decision_anchor(self.config, current)
                        if last_anchor is not None and scheduled_anchor <= last_anchor:
                            scheduled_anchor = _next_decision_anchor(
                                self.config,
                                last_anchor + timedelta(microseconds=1),
                            )
            except (ShadowClockError, ValueError) as error:
                yield Shadow01RuntimeResult(
                    "SHADOW01_MONITOR_CLOCK_INVALID",
                    _safe_utc(observed_now),
                    detail=_error_code(error),
                )
                return
            seconds_until_anchor = (scheduled_anchor - current).total_seconds()
            if seconds_until_anchor > 0:
                sleep(min(poll_interval_seconds, seconds_until_anchor))
                continue
            if seconds_until_anchor < -_ANCHOR_GRACE_SECONDS:
                if self._anchor_is_fully_recorded(scheduled_anchor):
                    yield self.run_observation_cycle(observed_at=scheduled_anchor)
                else:
                    yield Shadow01RuntimeResult(
                        "SHADOW01_MONITOR_ANCHOR_MISSED",
                        scheduled_anchor,
                        detail="SHADOW01_MONITOR_WAKE_LATE",
                    )
                last_anchor = scheduled_anchor
                scheduled_anchor = None
                continue

            result = self.run_observation_cycle(observed_at=scheduled_anchor)
            yield result
            last_anchor = scheduled_anchor
            scheduled_anchor = None
            if result.status in {
                "SHADOW01_MONITOR_STOP_REQUESTED",
                "SHADOW01_EPOCH_NOT_CREATED",
                "SHADOW01_NO_RETROSPECTIVE_DECISIONS",
                "SHADOW01_DQ03_REGISTRY_REQUIRED",
                "SHADOW01_NO_PROVEN_MARKETS",
                "SHADOW01_PROVIDER_REQUIRED",
                "SHADOW01_STORAGE_UNREADABLE",
            }:
                return

    def _observe_market(self, market: MarketSpec, timestamp: datetime) -> Shadow01MarketResult:
        assert self.broker is not None
        assert market.epic is not None
        try:
            observation_state = self.store.market_observation_state(
                self.config,
                decision_timestamp_utc=timestamp,
                instrument=market.symbol,
            )
        except (OSError, ShadowStoreError, sqlite3.Error, ValueError) as error:
            return Shadow01MarketResult(
                market.symbol,
                market.epic,
                "SHADOW01_MARKET_OBSERVATION_BLOCKED",
                (_error_code(error),),
            )
        if observation_state == "COMPLETE":
            return Shadow01MarketResult(
                market.symbol,
                market.epic,
                "SHADOW01_MARKET_OBSERVATION_ALREADY_RECORDED",
            )
        if observation_state == "PARTIAL":
            self._record_provider_health(
                timestamp,
                "OBSERVATION_BLOCKED",
                "SHADOW01_MARKET_OBSERVATION_PARTIAL_EVIDENCE",
                {"instrument": market.symbol, "epic": market.epic},
            )
            return Shadow01MarketResult(
                market.symbol,
                market.epic,
                "SHADOW01_MARKET_OBSERVATION_BLOCKED",
                ("SHADOW01_MARKET_OBSERVATION_PARTIAL_EVIDENCE",),
            )
        live_quote = self._fresh_canonical_quote(market, timestamp)
        if live_quote is None:
            self._record_provider_health(
                timestamp,
                "NO_DECISION",
                "SHADOW01_CANONICAL_STREAM_QUOTE_UNAVAILABLE",
                {"instrument": market.symbol, "epic": market.epic},
            )
            return Shadow01MarketResult(
                market.symbol,
                market.epic,
                "SHADOW01_MARKET_NO_DECISION",
                ("SHADOW01_CANONICAL_STREAM_QUOTE_UNAVAILABLE",),
            )
        cache_path = self.history_cache_directory / f"{market.symbol.lower()}-daily.json"
        try:
            market_metadata = self.broker.read_market(market.epic)
        except Exception as error:
            self._record_provider_health(
                timestamp,
                "MARKET_DATA_UNAVAILABLE",
                _error_code(error),
                {"instrument": market.symbol, "epic": market.epic, "stage": "MARKET_METADATA"},
            )
            return Shadow01MarketResult(
                market.symbol,
                market.epic,
                "MARKET_DATA_UNAVAILABLE",
                ("SHADOW01_READ_ONLY_MARKET_METADATA_UNAVAILABLE",),
            )
        if not isinstance(market_metadata, Mapping):
            self._record_provider_health(
                timestamp,
                "MARKET_DATA_UNAVAILABLE",
                "SHADOW01_MARKET_RESPONSE_INVALID",
                {"instrument": market.symbol, "epic": market.epic, "stage": "MARKET_METADATA"},
            )
            return Shadow01MarketResult(
                market.symbol,
                market.epic,
                "MARKET_DATA_UNAVAILABLE",
                ("SHADOW01_MARKET_RESPONSE_INVALID",),
            )

        cached = load_cached_history(cache_path, epic=market.epic)
        bars: tuple[DailyBar, ...] | None = None
        history_source = "IG_READ_ONLY_HISTORY"
        if cached is not None:
            try:
                cached_bars = parse_completed_daily_bars(cached, decision_timestamp_utc=timestamp)
            except ShadowDataError:
                cached_bars = ()
            if self._cache_is_fresh(cached_bars, timestamp):
                bars = cached_bars
                raw_history = cached
                history_source = "WARMUP_CACHE"
            else:
                raw_history = None
        else:
            raw_history = None
        if bars is not None:
            input_data_fingerprint = fingerprint(raw_history)
            cache_state = "WARMUP_CACHE_REUSED"
            return self._persist_observed_market(
                market,
                timestamp,
                bars,
                input_data_fingerprint,
                cache_state,
                history_source,
                market_metadata,
                live_quote,
            )
        try:
            raw_history = self.broker.read_historical_prices(market.epic, "DAY", 300)
        except Exception as error:
            self._record_provider_health(
                timestamp,
                "MARKET_DATA_UNAVAILABLE",
                _error_code(error),
                {
                    "instrument": market.symbol,
                    "epic": market.epic,
                    "warmup_cache_present": cached is not None,
                },
            )
            return Shadow01MarketResult(
                market.symbol,
                market.epic,
                "MARKET_DATA_UNAVAILABLE",
                ("SHADOW01_READ_ONLY_HISTORY_UNAVAILABLE",),
            )
        if not isinstance(raw_history, Mapping):
            self._record_provider_health(
                timestamp,
                "MARKET_DATA_UNAVAILABLE",
                "SHADOW01_HISTORY_RESPONSE_INVALID",
                {"instrument": market.symbol, "epic": market.epic},
            )
            return Shadow01MarketResult(
                market.symbol,
                market.epic,
                "MARKET_DATA_UNAVAILABLE",
                ("SHADOW01_HISTORY_RESPONSE_INVALID",),
            )
        try:
            bars = parse_completed_daily_bars(raw_history, decision_timestamp_utc=timestamp)
        except ShadowDataError as error:
            self._record_provider_health(
                timestamp,
                "MARKET_DATA_UNAVAILABLE",
                _error_code(error),
                {"instrument": market.symbol, "epic": market.epic},
            )
            return Shadow01MarketResult(
                market.symbol,
                market.epic,
                "MARKET_DATA_UNAVAILABLE",
                (_error_code(error),),
            )
        cache_state = "WARMUP_CACHE_WRITTEN"
        try:
            input_data_fingerprint = cache_history(
                cache_path,
                epic=market.epic,
                document=raw_history,
            )
        except (OSError, ShadowDataError, TypeError, ValueError):
            input_data_fingerprint = fingerprint(raw_history)
            cache_state = "WARMUP_CACHE_UNAVAILABLE"

        return self._persist_observed_market(
            market,
            timestamp,
            bars,
            input_data_fingerprint,
            cache_state,
            history_source,
            market_metadata,
            live_quote,
        )

    def _persist_observed_market(
        self,
        market: MarketSpec,
        timestamp: datetime,
        bars: tuple[DailyBar, ...],
        input_data_fingerprint: str,
        cache_state: str,
        history_source: str,
        market_metadata: Mapping[str, object],
        live_quote: ShadowLiveQuote,
    ) -> Shadow01MarketResult:
        try:
            result = self._record_market_observation(
                market,
                timestamp,
                bars,
                input_data_fingerprint,
                cache_state,
                history_source,
                market_metadata,
                live_quote,
            )
        except (ShadowStoreError, sqlite3.Error, ValueError, ShadowRuntimeError) as error:
            self._record_provider_health(
                timestamp,
                "OBSERVATION_BLOCKED",
                _error_code(error),
                {"instrument": market.symbol, "epic": market.epic},
            )
            return Shadow01MarketResult(
                market.symbol,
                market.epic,
                "SHADOW01_MARKET_OBSERVATION_BLOCKED",
                (_error_code(error),),
            )
        self._record_provider_health(
            timestamp,
            "HEALTHY",
            "SHADOW01_HISTORY_READ_AND_OBSERVED",
            {"instrument": market.symbol, "epic": market.epic, "cache_state": cache_state},
        )
        return result

    def _read_pre_epoch_snapshot(
        self,
        market: MarketSpec,
        timestamp: datetime,
    ) -> tuple[MarketSnapshot | None, Shadow01MarketResult]:
        """Read one causal, non-decision readiness snapshot through the adapter."""

        assert self.broker is not None
        if market.state is not MarketDataState.AVAILABLE or market.epic is None:
            return None, Shadow01MarketResult(
                market.symbol,
                None,
                "MARKET_DATA_UNAVAILABLE",
                (market.reason or "DQ03_MARKET_UNAVAILABLE",),
            )
        try:
            metadata = self.broker.read_market(market.epic)
        except Exception as error:
            return None, Shadow01MarketResult(
                market.symbol,
                market.epic,
                "MARKET_DATA_UNAVAILABLE",
                (_error_code(error),),
            )
        if not isinstance(metadata, Mapping):
            return None, Shadow01MarketResult(
                market.symbol,
                market.epic,
                "MARKET_DATA_UNAVAILABLE",
                ("SHADOW01_MARKET_RESPONSE_INVALID",),
            )
        cache_path = self.history_cache_directory / f"{market.symbol.lower()}-daily.json"
        cached = load_cached_history(cache_path, epic=market.epic)
        try:
            cached_bars = (
                parse_completed_daily_bars(cached, decision_timestamp_utc=timestamp)
                if cached is not None
                else ()
            )
        except ShadowDataError:
            cached_bars = ()
        if self._cache_is_fresh(cached_bars, timestamp):
            bars = cached_bars
            input_data_fingerprint = fingerprint(cached)
            cache_state = "WARMUP_CACHE_REUSED"
            history_source = "WARMUP_CACHE"
        else:
            try:
                raw_history = self.broker.read_historical_prices(market.epic, "DAY", 300)
                if not isinstance(raw_history, Mapping):
                    raise ShadowDataError("SHADOW01_HISTORY_RESPONSE_INVALID")
                bars = parse_completed_daily_bars(raw_history, decision_timestamp_utc=timestamp)
                input_data_fingerprint = cache_history(
                    cache_path,
                    epic=market.epic,
                    document=raw_history,
                )
            except Exception as error:
                return None, Shadow01MarketResult(
                    market.symbol,
                    market.epic,
                    "MARKET_DATA_UNAVAILABLE",
                    (_error_code(error),),
                )
            cache_state = "WARMUP_CACHE_WRITTEN"
            history_source = "IG_READ_ONLY_HISTORY"

        history = self.config.payload.get("history")
        if not isinstance(history, dict):
            return None, Shadow01MarketResult(
                market.symbol,
                market.epic,
                "MARKET_DATA_UNAVAILABLE",
                ("SHADOW01_HISTORY_CONFIG_INVALID",),
            )
        quality = assess_q1_quality(
            bars,
            self.config,
            decision_timestamp_utc=timestamp,
            provider_healthy=True,
            stream_healthy=None,
            session_complete=True,
            feature_available=len(bars) >= int(history["minimum_completed_observations"]),
        )
        try:
            snapshot = MarketSnapshot(
                decision_timestamp_utc=timestamp,
                instrument=market.symbol,
                epic=market.epic,
                asset_class=market.asset_class,
                completed_bars=bars,
                metadata={
                    "provider": history_source,
                    "cache_state": cache_state,
                    "market_metadata_fingerprint": fingerprint(metadata),
                    "market_cost_facts": _market_cost_facts(metadata),
                    "execution_authority": "OFF",
                    "pre_epoch_readiness": True,
                },
                data_quality=quality,
                input_data_fingerprint=input_data_fingerprint,
            )
        except (ValueError, TypeError) as error:
            return None, Shadow01MarketResult(
                market.symbol,
                market.epic,
                "MARKET_DATA_UNAVAILABLE",
                (_error_code(error),),
            )
        return snapshot, Shadow01MarketResult(
            market.symbol,
            market.epic,
            "SHADOW01_PRE_EPOCH_MARKET_SNAPSHOT_OK",
        )

    def _record_market_observation(
        self,
        market: MarketSpec,
        timestamp: datetime,
        bars: tuple[DailyBar, ...],
        input_data_fingerprint: str,
        cache_state: str,
        history_source: str,
        market_metadata: Mapping[str, object],
        live_quote: ShadowLiveQuote,
    ) -> Shadow01MarketResult:
        history = self.config.payload["history"]
        if not isinstance(history, dict):
            raise ShadowRuntimeError("SHADOW01_HISTORY_CONFIG_INVALID")
        feature_available = len(bars) >= int(history["minimum_completed_observations"])
        quality = assess_q1_quality(
            bars,
            self.config,
            decision_timestamp_utc=timestamp,
            provider_healthy=True,
            stream_healthy=True,
            session_complete=True,
            feature_available=feature_available,
        )
        snapshot = MarketSnapshot(
            decision_timestamp_utc=timestamp,
            instrument=market.symbol,
            epic=market.epic or "",
            asset_class=market.asset_class,
            completed_bars=bars,
            metadata={
                "provider": history_source,
                "cache_state": cache_state,
                "market_metadata_fingerprint": fingerprint(market_metadata),
                "market_cost_facts": _market_cost_facts(market_metadata),
                "execution_authority": "OFF",
            },
            data_quality=quality,
            input_data_fingerprint=input_data_fingerprint,
        )
        technical = compute_technical_state(snapshot, self.config)
        trend = evaluate_t1_trend(technical)
        reversion = evaluate_m1_reversion(snapshot, self.config)
        cross_asset = evaluate_x1_context(None, self.config, decision_timestamp_utc=timestamp)
        fundamental = build_f1_context(None, decision_timestamp_utc=timestamp)
        cost = assess_c1_cost(
            _cost_inputs_from_live_quote(live_quote, market_metadata),
            technical,
            self.config,
        )
        recommendations = evaluate_policies(
            self.config,
            trend=trend,
            reversion=reversion,
            cross_asset=cross_asset,
            fundamental=fundamental,
            quality=quality,
            cost=cost,
        )
        decisions = materialize_decisions(
            self.config,
            decision_timestamp_utc=timestamp,
            instrument=market.symbol,
            epic=market.epic or "",
            input_data_fingerprint=input_data_fingerprint,
            recommendations=recommendations,
            cross_asset=cross_asset,
            fundamental=fundamental,
            quality=quality,
            cost=cost,
            created_at=timestamp,
        )
        decision_timestamps = {item.decision_timestamp_utc for item in decisions}
        if len(decisions) != 4 or decision_timestamps != {timestamp}:
            raise ShadowRuntimeError("SHADOW01_POLICY_TIMESTAMP_CONTRACT_INVALID")

        outcomes_resolved = self._resolve_available_outcomes(market, bars)

        persisted = self.store.append_market_observation(
            self.config,
            decision_timestamp_utc=timestamp,
            instrument=market.symbol,
            epic=market.epic or "",
            snapshot_data=document(snapshot),
            input_data_fingerprint=input_data_fingerprint,
            engine_insights={
                "TECHNICAL_STATE": document(technical),
                "T1": document(trend),
                "M1": document(reversion),
                "X1": document(cross_asset),
                "F1": document(fundamental),
                "Q1": document(quality),
                "C1": document(cost),
            },
            decisions=decisions,
        )
        if not persisted:
            return Shadow01MarketResult(
                market.symbol,
                market.epic,
                "SHADOW01_MARKET_OBSERVATION_ALREADY_RECORDED",
                outcomes_resolved=outcomes_resolved,
            )
        return Shadow01MarketResult(
            market.symbol,
            market.epic,
            "SHADOW01_MARKET_OBSERVATION_RECORDED",
            decisions_recorded=len(decisions),
            outcomes_resolved=outcomes_resolved,
        )

    def persist_resolved_outcomes(self, outcomes: tuple[OutcomeLabel, ...]) -> int:
        """Persist externally resolved later labels through the isolated store only.

        Feature construction never calls this method.  It exists so a later
        read-only resolver can persist already-derived labels without gaining
        any broker or execution capability.
        """

        if self.store.epoch(self.config) is None:
            raise ShadowRuntimeError("SHADOW01_EPOCH_NOT_CREATED")
        if any(outcome.quality is QualityState.BLOCKED for outcome in outcomes):
            raise ShadowRuntimeError("SHADOW01_OUTCOME_PENDING_LABEL_FORBIDDEN")
        for outcome in outcomes:
            self.store.append_outcome(outcome)
        return len(outcomes)

    def _resolve_available_outcomes(
        self,
        market: MarketSpec,
        completed_bars: tuple[DailyBar, ...],
    ) -> int:
        """Append only horizons that later completed sessions can truly resolve.

        The outcome table is append-only.  A not-yet-due horizon is therefore
        left absent rather than written as a provisional blocked label that
        would prevent its eventual resolution.
        """

        resolved = 0
        for basis in self.store.pending_outcome_bases(self.config, instrument=market.symbol):
            later_bars = tuple(
                bar
                for bar in completed_bars
                if bar.completed_at > basis.decision.decision_timestamp_utc
            )
            labels = resolve_outcomes(
                OutcomeResolutionInput(
                    decision=basis.decision,
                    entry_price=basis.entry_price,
                    atr20_over_price=basis.atr20_over_price,
                    future_completed_bars=later_bars,
                )
            )
            for outcome in labels:
                if (
                    outcome.horizon_sessions in basis.missing_horizons
                    and outcome.quality is not QualityState.BLOCKED
                ):
                    self.store.append_outcome(outcome)
                    resolved += 1
        return resolved

    def _probe_clock_availability(
        self,
        market: MarketSpec,
        timestamp: datetime,
        results: list[Shadow01MarketResult],
    ) -> ClockAvailability:
        assert self.broker is not None
        assert market.epic is not None
        asset_class = market.asset_class.value
        try:
            metadata = self.broker.read_market(market.epic)
        except Exception as error:
            results.append(
                Shadow01MarketResult(
                    market.symbol,
                    market.epic,
                    "MARKET_DATA_UNAVAILABLE",
                    ("SHADOW01_READ_ONLY_MARKET_METADATA_UNAVAILABLE",),
                )
            )
            self._record_provider_health(
                timestamp,
                "CLOCK_UNAVAILABLE",
                _error_code(error),
                {"asset_class": asset_class, "instrument": market.symbol},
            )
            return ClockAvailability(asset_class, False, False, _error_code(error))
        if not isinstance(metadata, Mapping):
            results.append(
                Shadow01MarketResult(
                    market.symbol,
                    market.epic,
                    "MARKET_DATA_UNAVAILABLE",
                    ("SHADOW01_MARKET_RESPONSE_INVALID",),
                )
            )
            return ClockAvailability(asset_class, False, False, "SHADOW01_MARKET_RESPONSE_INVALID")

        cache_path = self.history_cache_directory / f"{market.symbol.lower()}-daily.json"
        cached = load_cached_history(cache_path, epic=market.epic)
        try:
            cached_bars = (
                parse_completed_daily_bars(cached, decision_timestamp_utc=timestamp)
                if cached is not None
                else ()
            )
        except ShadowDataError:
            cached_bars = ()
        if self._cache_is_fresh(cached_bars, timestamp):
            results.append(
                Shadow01MarketResult(
                    market.symbol,
                    market.epic,
                    "SHADOW01_CLOCK_PROBE_OK",
                    ("WARMUP_CACHE_REUSED",),
                )
            )
            self._record_provider_health(
                timestamp,
                "HEALTHY",
                "SHADOW01_READ_ONLY_CLOCK_PROBE_OK",
                {"asset_class": asset_class, "instrument": market.symbol, "cache_state": "REUSED"},
            )
            return ClockAvailability(asset_class, True, True, "WARMUP_CACHE_REUSED")
        try:
            raw_history = self.broker.read_historical_prices(market.epic, "DAY", 300)
            if not isinstance(raw_history, Mapping):
                raise ShadowDataError("SHADOW01_HISTORY_RESPONSE_INVALID")
            bars = parse_completed_daily_bars(raw_history, decision_timestamp_utc=timestamp)
            cache_history(cache_path, epic=market.epic, document=raw_history)
        except Exception as error:
            results.append(
                Shadow01MarketResult(
                    market.symbol,
                    market.epic,
                    "MARKET_DATA_UNAVAILABLE",
                    ("SHADOW01_READ_ONLY_HISTORY_UNAVAILABLE",),
                )
            )
            self._record_provider_health(
                timestamp,
                "CLOCK_UNAVAILABLE",
                _error_code(error),
                {"asset_class": asset_class, "instrument": market.symbol},
            )
            return ClockAvailability(asset_class, True, False, _error_code(error))
        results.append(Shadow01MarketResult(market.symbol, market.epic, "SHADOW01_CLOCK_PROBE_OK"))
        self._record_provider_health(
            timestamp,
            "HEALTHY",
            "SHADOW01_READ_ONLY_CLOCK_PROBE_OK",
            {"asset_class": asset_class, "instrument": market.symbol, "cache_state": "WRITTEN"},
        )
        return ClockAvailability(
            asset_class,
            True,
            bool(bars),
            "SHADOW01_COMPLETED_SESSION_CONFIRMED",
        )

    def _cache_is_fresh(self, bars: tuple[DailyBar, ...], timestamp: datetime) -> bool:
        if not bars:
            return False
        quality = self.config.payload.get("quality")
        if not isinstance(quality, dict):
            return False
        maximum_age = quality.get("maximum_price_age_seconds")
        if isinstance(maximum_age, bool) or not isinstance(maximum_age, int) or maximum_age < 0:
            return False
        age = (timestamp - bars[-1].completed_at).total_seconds()
        return 0.0 <= age <= maximum_age

    def _fresh_canonical_quote(
        self, market: MarketSpec, timestamp: datetime
    ) -> ShadowLiveQuote | None:
        """Return one fresh IG Price-stream quote or fail closed before any decision read."""

        reader = self.canonical_quote_provider
        if not callable(reader) or market.epic is None:
            return None
        try:
            quote = reader(market, timestamp)
        except Exception:
            return None
        if (
            not isinstance(quote, ShadowLiveQuote)
            or quote.epic != market.epic
            or quote.symbol != market.symbol
            or quote.source != "IG_PRICE_STREAM"
            or quote.quality != "VALID_QUOTE"
            or quote.timestamp_utc is None
            or quote.quote_age_seconds is None
        ):
            return None
        quality = self.config.payload.get("quality")
        maximum_age = (
            quality.get("maximum_price_age_seconds") if isinstance(quality, dict) else None
        )
        if isinstance(maximum_age, bool) or not isinstance(maximum_age, int) or maximum_age < 1:
            return None
        try:
            quote_timestamp = require_utc(quote.timestamp_utc)
        except ValueError:
            return None
        age_seconds = (timestamp - quote_timestamp).total_seconds()
        if not 0.0 <= age_seconds <= maximum_age or quote.quote_age_seconds > maximum_age:
            return None
        return quote

    def _registry_status(self) -> tuple[str, int, int]:
        if self.registry is None:
            return "SHADOW01_DQ03_REGISTRY_REQUIRED", 0, 20
        if self.registry.verified_count == 0:
            return "SHADOW01_NO_PROVEN_MARKETS", 0, self.registry.unavailable_count
        return "READY", self.registry.verified_count, self.registry.unavailable_count

    def _has_full_verified_registry(self) -> bool:
        return (
            self.registry is not None
            and len(self.registry.markets) == 20
            and all(
                item.state is MarketDataState.AVAILABLE and item.epic is not None
                for item in self.registry.markets
            )
        )

    def _anchor_is_fully_recorded(self, timestamp: datetime) -> bool:
        """Check a prior anchor locally before classifying a late restart.

        This intentionally performs no provider access.  A restart after an
        already-committed anchor can therefore report its idempotent local
        result, while an unrecorded late anchor remains a missed observation
        rather than a retrospective market read.
        """

        if self.registry is None:
            return False
        available_markets = tuple(
            market
            for market in self.registry.markets
            if market.state is MarketDataState.AVAILABLE and market.epic is not None
        )
        if not available_markets:
            return False
        try:
            return all(
                self.store.market_observation_state(
                    self.config,
                    decision_timestamp_utc=timestamp,
                    instrument=market.symbol,
                )
                == "COMPLETE"
                for market in available_markets
            )
        except (OSError, ShadowStoreError, sqlite3.Error, ValueError):
            return False

    def _unavailable_market_results(self) -> tuple[Shadow01MarketResult, ...]:
        if self.registry is None:
            return ()
        return tuple(
            Shadow01MarketResult(
                item.symbol,
                item.epic,
                "MARKET_DATA_UNAVAILABLE",
                (item.reason or "SHADOW01_CYCLE_PRECONDITION_UNMET",),
            )
            for item in self.registry.markets
            if item.state is MarketDataState.MARKET_DATA_UNAVAILABLE
        )

    def _record_provider_health(
        self,
        observed_at: datetime,
        status: str,
        detail: str | None,
        data: dict[str, object] | None = None,
    ) -> None:
        try:
            self.store.append_provider_health(
                observed_at=observed_at,
                provider="IG_READ_ONLY",
                status=status,
                detail=detail,
                data=data,
            )
        except (OSError, ShadowStoreError, sqlite3.Error):
            return


def _cost_inputs_from_market_metadata(metadata: Mapping[str, object]) -> CostInputs:
    """Extract only explicitly present read-only cost facts from IG metadata."""

    snapshot = _mapping(metadata.get("snapshot"))
    bid = _positive_number(snapshot.get("bid"))
    offer = _positive_number(snapshot.get("offer")) or _positive_number(snapshot.get("ask"))
    if bid is not None and offer is not None and offer >= bid:
        reference_price = (bid + offer) / 2.0
        spread = offer - bid
    else:
        # A historical bar is not a substitute for the contemporaneous broker
        # snapshot used by C1.  Missing read-market bid/offer facts stay
        # explicit and make the cost engine fail closed as UNKNOWN.
        reference_price = None
        spread = None
    instrument = _mapping(metadata.get("instrument"))
    dealing_rules = _mapping(metadata.get("dealingRules"))
    minimum_stop_distance = _distance_value(
        dealing_rules.get("minStopOrLimitDistance")
        if "minStopOrLimitDistance" in dealing_rules
        else dealing_rules.get("minNormalStopOrLimitDistance")
    )
    product_type = _first_text(
        metadata.get("instrumentType"),
        instrument.get("instrumentType"),
        instrument.get("kind"),
    )
    funding_metadata = _first_text(
        metadata.get("fundingInfo"),
        metadata.get("funding"),
        instrument.get("fundingInfo"),
        instrument.get("expiry"),
    )
    return CostInputs(
        reference_price=reference_price,
        spread=spread,
        minimum_stop_distance=minimum_stop_distance,
        product_type=product_type,
        funding_metadata=funding_metadata,
    )


def _cost_inputs_from_live_quote(
    quote: ShadowLiveQuote, metadata: Mapping[str, object]
) -> CostInputs:
    """Use the canonical stream quote for contemporaneous cost values only."""

    metadata_inputs = _cost_inputs_from_market_metadata(metadata)
    try:
        bid = float(quote.bid)
        ask = float(quote.ask)
    except (TypeError, ValueError, OverflowError):
        bid = ask = float("nan")
    if not (isfinite(bid) and isfinite(ask) and bid > 0 and ask >= bid):
        return metadata_inputs
    return CostInputs(
        reference_price=(bid + ask) / 2.0,
        spread=ask - bid,
        minimum_stop_distance=metadata_inputs.minimum_stop_distance,
        product_type=metadata_inputs.product_type,
        funding_metadata=metadata_inputs.funding_metadata,
    )


def _market_cost_facts(metadata: Mapping[str, object]) -> dict[str, object]:
    """Render the exact C1 input facts without inventing cross-market costs."""

    inputs = _cost_inputs_from_market_metadata(metadata)
    return {
        "reference_price": inputs.reference_price,
        "spread": inputs.spread,
        "minimum_stop_distance": inputs.minimum_stop_distance,
        "product_type": inputs.product_type,
        "funding_metadata": inputs.funding_metadata,
    }


def _mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def _positive_number(value: object) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0.0 else None


def _distance_value(value: object) -> float | None:
    if isinstance(value, Mapping):
        value = value.get("value")
    number = _positive_number(value)
    return number if number is not None else None


def _first_text(*values: object) -> str | None:
    for value in values:
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _safe_utc(value: datetime) -> datetime | None:
    try:
        return require_utc(value)
    except ValueError:
        return None


def _next_decision_anchor(config: ShadowTournamentConfig, after: datetime) -> datetime:
    """Find the next exact frozen anchor without touching the broker.

    ``require_decision_anchor`` is the single authority for New York time and
    its DST fallback.  Scanning bounded UTC minutes keeps this scheduler aligned
    with that authority without reimplementing timezone rules in the runtime.
    """

    instant = require_utc(after)
    candidate = instant.replace(second=0, microsecond=0)
    if candidate < instant:
        candidate += timedelta(minutes=1)
    for _ in range(26 * 60 + 1):
        try:
            return require_decision_anchor(config, candidate)
        except ShadowClockError as error:
            if str(error) == "SHADOW01_SESSION_CLOCK_HUMAN_GATE_REQUIRED":
                raise
            candidate += timedelta(minutes=1)
    raise ShadowClockError("SHADOW01_MONITOR_NEXT_ANCHOR_UNAVAILABLE")


def _latest_decision_anchor(config: ShadowTournamentConfig, at: datetime) -> datetime | None:
    """Return the most recent frozen anchor at or before ``at``.

    Like the forward scheduler this scans UTC minutes through the single clock
    authority.  It avoids duplicating New York DST arithmetic in monitor code,
    including the deterministic fallback used when IANA zone data is absent.
    """

    instant = require_utc(at)
    candidate = instant.replace(second=0, microsecond=0)
    for _ in range(26 * 60 + 1):
        try:
            return require_decision_anchor(config, candidate)
        except ShadowClockError as error:
            if str(error) == "SHADOW01_SESSION_CLOCK_HUMAN_GATE_REQUIRED":
                raise
            candidate -= timedelta(minutes=1)
    return None


def _error_code(error: BaseException) -> str:
    """Return a secret-safe error classification, never a transport payload."""

    return f"SHADOW01_{type(error).__name__.upper()}"


def _first_blocker(*values: str | None) -> str:
    for value in values:
        if value:
            return value
    raise AssertionError("At least one runtime status is required")


__all__ = (
    "Shadow01MarketResult",
    "Shadow01Runtime",
    "Shadow01RuntimeResult",
    "ShadowRuntimeError",
)
