"""One bounded, non-persisting SHADOW01 IG Demo read-only smoke runner.

This module is intentionally separate from the Shadow runtime and CLI.  It
cannot create an epoch, open a store, write a cache, materialize a decision,
or start a worker.  Its only broker dependencies are the reviewed read-only
REST adapter and the reviewed stream bridge.  It returns sanitized value
objects only; raw account, token, market, and exception payloads never leave
this module.
"""

from __future__ import annotations

import re
import sqlite3
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from math import isfinite
from pathlib import Path
from time import monotonic

from src.ig_trader.shadow01.clock_diagnostic import (
    ClockDiagnosticReport,
    ClockDiagnosticState,
    verify_shadow_session_clock,
)
from src.ig_trader.shadow01.config import DEFAULT_CONFIG_PATH, ShadowTournamentConfig, load_config
from src.ig_trader.shadow01.data import ShadowDataError, parse_completed_daily_bars
from src.ig_trader.shadow01.dry_snapshot import (
    DrySnapshotContext,
    DrySnapshotMarketInput,
    DrySnapshotResult,
    run_shadow_dry_snapshot,
)
from src.ig_trader.shadow01.live_quote import ShadowLiveQuote
from src.ig_trader.shadow01.local_demo_read_only import (
    LocalDemoReadOnlyStatus,
    Shadow01LocalDemoReadOnlyFactory,
)
from src.ig_trader.shadow01.local_demo_stream_transport import (
    Shadow01LocalDemoReadOnlyStreamFactory,
)
from src.ig_trader.shadow01.models import fingerprint
from src.ig_trader.shadow01.read_only_broker import (
    ReadOnlyBrokerRequestCounters,
    Shadow01ReadOnlyBroker,
)
from src.ig_trader.shadow01.registry import (
    ShadowMarketRegistry,
    ShadowRegistryError,
    load_verified_dq03_registry,
    require_exact_twenty,
)
from src.ig_trader.shadow01.stream_bridge import (
    ShadowReadOnlyStreamBridge,
    ShadowStreamDisconnected,
    ShadowStreamSubscriptionDiagnostic,
)
from src.ig_trader.shadow01.warmup_diagnostic import (
    WarmupDiagnosticReport,
    WarmupMarketReport,
    run_shadow_warmup_diagnostic,
)

_LIVE_AUTHORIZATION = "SHADOW01_LIVE_READONLY_SMOKE_V2"
_DQ03_REPRESENTATIVES = ("EURUSD", "USDJPY", "XAUUSD", "US500")
_HISTORY_REPRESENTATIVES = ("EURUSD", "XAUUSD", "US500", "USTECH100")
_WARMUP_REPRESENTATIVES = ("EURUSD", "XAUUSD", "US500")
_INITIAL_STREAM_UPDATE_WAIT_SECONDS = 5.0
_MAX_INITIAL_STREAM_RECEIVE_ATTEMPTS = 64
_QUOTE_TIMESTAMP_MILLISECONDS_THRESHOLD = 100_000_000_000
_QUOTE_TIMESTAMP_MODERN_LOWER_BOUND = datetime(2000, 1, 1, tzinfo=UTC)
_QUOTE_TIMESTAMP_FUTURE_SKEW_SECONDS = 300
_SAFE_REASON = re.compile(r"SHADOW01_[A-Z0-9_]+\Z")
_STREAM_REJECTION_CODES = (
    ("item_resolution_failure", "SHADOW01_STREAM_ITEM_RESOLUTION_FAILURE"),
    ("missing_bid", "SHADOW01_STREAM_MISSING_BID"),
    ("invalid_bid", "SHADOW01_STREAM_INVALID_BID"),
    ("missing_ask", "SHADOW01_STREAM_MISSING_ASK"),
    ("invalid_ask", "SHADOW01_STREAM_INVALID_ASK"),
    ("missing_timestamp", "SHADOW01_STREAM_MISSING_TIMESTAMP"),
    ("invalid_timestamp", "SHADOW01_STREAM_INVALID_TIMESTAMP"),
    ("stale_timestamp", "SHADOW01_STREAM_STALE_TIMESTAMP"),
)
_ROOT = DEFAULT_CONFIG_PATH.parent
DEFAULT_DQ03_REGISTRY_PATH = _ROOT / "artifacts" / "dq03" / "instrument_registry.json"
DEFAULT_SHADOW_DATABASE_PATH = _ROOT / "runtime" / "shadow_tournament.sqlite3"


@dataclass(frozen=True)
class SmokeDatabaseState:
    """Read-only database contamination facts, without a database handle."""

    known: bool
    epoch_created: bool
    decision_count: int
    outcome_count: int
    auxiliary_persistent_record_count: int = 0

    @property
    def clean(self) -> bool:
        return (
            self.known
            and not self.epoch_created
            and self.decision_count == 0
            and self.outcome_count == 0
            and self.auxiliary_persistent_record_count == 0
        )


class ShadowSmokeRequestBudgetError(RuntimeError):
    """The fixed read-only smoke request envelope would be exceeded locally."""


@dataclass(frozen=True)
class ShadowSmokeRequestBudget:
    """A fixed upper bound for V4 metadata and representative history reads.

    Stream connect/reconnect uses the one already-authenticated REST session,
    so the envelope has no stream-specific REST authentication or logout.
    """

    maximum_auth: int = 1
    maximum_account: int = 1
    maximum_market: int = 20
    maximum_history: int = 4
    maximum_logout: int = 1
    used_auth: int = 0
    used_account: int = 0
    used_market: int = 0
    used_history: int = 0
    used_logout: int = 0

    def __post_init__(self) -> None:
        maximums = (
            self.maximum_auth,
            self.maximum_account,
            self.maximum_market,
            self.maximum_history,
            self.maximum_logout,
        )
        used = (
            self.used_auth,
            self.used_account,
            self.used_market,
            self.used_history,
            self.used_logout,
        )
        if any(
            isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in maximums
        ):
            raise ValueError("Shadow smoke request-budget maximum is invalid")
        if any(
            isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in used
        ):
            raise ValueError("Shadow smoke request-budget use is invalid")
        if any(current > maximum for current, maximum in zip(used, maximums, strict=True)):
            raise ValueError("Shadow smoke request-budget use exceeds its maximum")

    @property
    def maximum_requests(self) -> int:
        return sum(
            (
                self.maximum_auth,
                self.maximum_account,
                self.maximum_market,
                self.maximum_history,
                self.maximum_logout,
            )
        )

    @property
    def used_requests(self) -> int:
        return sum(
            (
                self.used_auth,
                self.used_account,
                self.used_market,
                self.used_history,
                self.used_logout,
            )
        )

    def reserve(self, category: str, count: int = 1) -> ShadowSmokeRequestBudget:
        """Return a new budget state after reserving a known request family."""

        if (
            category not in {"auth", "account", "market", "history", "logout"}
            or isinstance(count, bool)
            or not isinstance(count, int)
            or count < 1
        ):
            raise ShadowSmokeRequestBudgetError("SHADOW01_SMOKE_REST_BUDGET_INVALID")
        used_name = f"used_{category}"
        maximum_name = f"maximum_{category}"
        if getattr(self, used_name) + count > getattr(self, maximum_name):
            raise ShadowSmokeRequestBudgetError("SHADOW01_SMOKE_REST_BUDGET_EXCEEDED")
        return replace(self, **{used_name: getattr(self, used_name) + count})

    def document(self) -> dict[str, object]:
        """Return request counts only; it contains no transport or account data."""

        return {
            "maximum_requests": self.maximum_requests,
            "reserved_requests": self.used_requests,
            "maximum": {
                "auth": self.maximum_auth,
                "account": self.maximum_account,
                "market": self.maximum_market,
                "history": self.maximum_history,
                "logout": self.maximum_logout,
            },
            "reserved": {
                "auth": self.used_auth,
                "account": self.used_account,
                "market": self.used_market,
                "history": self.used_history,
                "logout": self.used_logout,
            },
        }


@dataclass(frozen=True)
class MarketSmokeObservation:
    """Sanitized REST-metadata and canonical-stream facts for one DQ-03 symbol."""

    symbol: str
    metadata_available: bool
    identity_verified: bool
    market_status: str
    quote_availability: str
    quote_timestamp_freshness: str
    minimum_stop_metadata_available: bool
    reason_code: str | None = None
    metadata_health: str = "UNKNOWN"
    live_quote_health: str = "NOT_OBSERVED"
    streaming_prices_available: bool = False
    last_quote_age_seconds: float | None = None
    stream_connection_status: str = "NOT_CONNECTED"


@dataclass(frozen=True)
class StreamSmokeResult:
    """Bounded stream lifecycle facts, never a stream client or subscription."""

    subscriptions_attempted: int = 0
    subscriptions_successful: int = 0
    subscription_call_successes: int = 0
    updates_received: int = 0
    invalid_update_symbols: tuple[str, ...] = ()
    representative_reconnect_symbols: tuple[str, ...] = ()
    representative_reconnect_updates: int = 0
    subscription_diagnostics: tuple[ShadowStreamSubscriptionDiagnostic, ...] = ()
    callback_contract_diagnostics: tuple[dict[str, object], ...] = ()
    invalid_reason_counts: tuple[tuple[str, int], ...] = ()
    no_update_symbols: tuple[str, ...] = ()
    reconnect_attempts: int = 0
    restore_result: str = "NOT_REQUIRED"
    unsubscribe_result: str = "NOT_REQUIRED"
    disconnect_result: str = "NOT_REQUIRED"
    error_code: str | None = None


@dataclass(frozen=True)
class ClockSmokeAssessment:
    """A compact, safe projection of the no-wait clock diagnostic."""

    asset_class: str
    state: str
    evidence: tuple[str, ...]
    proposed_clock: str | None


@dataclass(frozen=True)
class WarmupSmokeObservation:
    """A compact, safe projection of one bounded history diagnostic row."""

    symbol: str
    historical_requests: int
    bars_received: int
    completed_sessions: int
    t1_ready: bool
    m1_ready: bool
    q1_ready: bool
    quality_result: str
    reason_codes: tuple[str, ...]
    http_status_code: int | None
    upstream_error_code: str | None


@dataclass(frozen=True)
class DrySnapshotSmokeResult:
    """The six engines and four policies from one non-prospective snapshot."""

    status: str = "NOT_RUN"
    t1: str = "NOT_RUN"
    m1: str = "NOT_RUN"
    x1: str = "NOT_RUN"
    f1: str = "NOT_RUN"
    q1: str = "NOT_RUN"
    c1: str = "NOT_RUN"
    p0: str = "NOT_RUN"
    p1: str = "NOT_RUN"
    p2: str = "NOT_RUN"
    p3: str = "NOT_RUN"


@dataclass(frozen=True)
class LiveReadOnlySmokeV2Result:
    """Complete sanitized result of at most one authorized live smoke."""

    status: str
    preflight_passed: bool
    env_file_ignored: bool
    demo_confirmed: bool
    expected_account_match: bool
    token_presence: bool
    account_state_valid: bool
    rest_status: LocalDemoReadOnlyStatus | None
    stream_status: LocalDemoReadOnlyStatus | None
    dq03_observations: tuple[MarketSmokeObservation, ...] = ()
    stream: StreamSmokeResult = field(default_factory=StreamSmokeResult)
    clock: tuple[ClockSmokeAssessment, ...] = ()
    clock_overall_state: str = "NOT_RUN"
    clock_diagnostic: Mapping[str, object] = field(default_factory=dict)
    warmup: tuple[WarmupSmokeObservation, ...] = ()
    dry_snapshot: DrySnapshotSmokeResult = field(default_factory=DrySnapshotSmokeResult)
    database_before: SmokeDatabaseState = field(
        default_factory=lambda: SmokeDatabaseState(False, False, 0, 0)
    )
    database_after: SmokeDatabaseState = field(
        default_factory=lambda: SmokeDatabaseState(False, False, 0, 0)
    )
    counters: ReadOnlyBrokerRequestCounters = field(
        default_factory=ReadOnlyBrokerRequestCounters.zero
    )
    request_budget: ShadowSmokeRequestBudget = field(default_factory=ShadowSmokeRequestBudget)
    rest_logout_result: str = "NOT_REQUIRED"
    rest_logout_http_status: int | None = None
    rest_logout_upstream_error_code: str | None = None
    rest_logout_allowance_affected: bool = False
    cleanup_passed: bool = False
    workers_left_active: bool = False

    def document(self) -> dict[str, object]:
        """Return only the Gate V2 report fields that are safe to render."""

        observations = list(self.dq03_observations)
        quote_totals = {
            "valid": sum(item.quote_availability == "VALID_QUOTE" for item in observations),
            "closed": sum(item.quote_availability == "CLOSED" for item in observations),
            "unavailable": sum(item.quote_availability == "UNAVAILABLE" for item in observations),
            "stale": sum(item.quote_availability == "STALE" for item in observations),
        }
        return {
            "status": self.status,
            "preflight": {
                "passed": self.preflight_passed,
                "ig_demo": self.rest_status.demo_mode if self.rest_status else False,
                "demo_operator_local": (
                    self.rest_status.local_operator if self.rest_status else False
                ),
                "paper_trading": self.rest_status.paper_trading if self.rest_status else False,
                "credentials_present": self.rest_status.credentials_present
                if self.rest_status
                else False,
                "env_ignored": self.env_file_ignored,
                "execution_authority": "OFF",
            },
            "auth": {
                "passed": self.demo_confirmed and self.expected_account_match,
                "demo_confirmed": self.demo_confirmed,
                "expected_account_match": self.expected_account_match,
                "token_presence": self.token_presence,
                "account_state_valid": self.account_state_valid,
            },
            "dq03_live": {
                "verified_identities": sum(item.identity_verified for item in observations),
                "substitutions": 0,
                "metadata_failures": [
                    item.symbol for item in observations if not item.metadata_available
                ],
                "markets": [
                    {
                        "symbol": item.symbol,
                        "metadata_available": item.metadata_available,
                        "metadata_health": item.metadata_health,
                        "market_status": item.market_status,
                        "streaming_prices_available": item.streaming_prices_available,
                        "live_quote_health": item.live_quote_health,
                        "quote_availability": item.quote_availability,
                        "quote_timestamp_freshness": item.quote_timestamp_freshness,
                        "last_quote_age_seconds": item.last_quote_age_seconds,
                        "stream_connection_status": item.stream_connection_status,
                        "minimum_stop_metadata_available": item.minimum_stop_metadata_available,
                        "reason_code": item.reason_code,
                    }
                    for item in observations
                ],
            },
            "market_read": {
                **quote_totals,
                "exceptions": [
                    item.symbol
                    for item in observations
                    if item.quote_availability in {"CLOSED", "UNAVAILABLE", "STALE"}
                ],
            },
            "stream": {
                "subscriptions_attempted": self.stream.subscriptions_attempted,
                "subscription_call_successes": self.stream.subscription_call_successes,
                "initial_update_wait_seconds": _INITIAL_STREAM_UPDATE_WAIT_SECONDS,
                "updates_received": self.stream.updates_received,
                "invalid_update_symbols": list(self.stream.invalid_update_symbols),
                "no_update_symbols": list(self.stream.no_update_symbols),
                "reconnect_attempts": self.stream.reconnect_attempts,
                "restore_result": self.stream.restore_result,
                "representative_reconnect_symbols": list(
                    self.stream.representative_reconnect_symbols
                ),
                "representative_reconnect_updates": self.stream.representative_reconnect_updates,
                "subscription_diagnostics": [
                    item.document() for item in self.stream.subscription_diagnostics
                ],
                "callback_contract_diagnostics": list(self.stream.callback_contract_diagnostics),
                "invalid_reason_counts": dict(self.stream.invalid_reason_counts),
                "unsubscribe_result": self.stream.unsubscribe_result,
                "disconnect_result": self.stream.disconnect_result,
                "error_code": self.stream.error_code,
            },
            "clock": {
                "overall": self.clock_overall_state,
                "diagnostic": dict(self.clock_diagnostic),
                "assessments": [
                    {
                        "asset_class": item.asset_class,
                        "state": item.state,
                        "evidence": list(item.evidence),
                        "proposed_clock": item.proposed_clock,
                    }
                    for item in self.clock
                ],
            },
            "warmup": [
                {
                    "symbol": item.symbol,
                    "historical_requests": item.historical_requests,
                    "bars_received": item.bars_received,
                    "completed_sessions": item.completed_sessions,
                    "t1_ready": item.t1_ready,
                    "m1_ready": item.m1_ready,
                    "q1_ready": item.q1_ready,
                    "quality_result": item.quality_result,
                    "reason_codes": list(item.reason_codes),
                    "http_status_code": item.http_status_code,
                    "upstream_error_code": item.upstream_error_code,
                }
                for item in self.warmup
            ],
            "dry_snapshot": {
                "status": self.dry_snapshot.status,
                "T1": self.dry_snapshot.t1,
                "M1": self.dry_snapshot.m1,
                "X1": self.dry_snapshot.x1,
                "F1": self.dry_snapshot.f1,
                "Q1": self.dry_snapshot.q1,
                "C1": self.dry_snapshot.c1,
                "P0": self.dry_snapshot.p0,
                "P1": self.dry_snapshot.p1,
                "P2": self.dry_snapshot.p2,
                "P3": self.dry_snapshot.p3,
            },
            "database": {
                "epoch_before": self.database_before.epoch_created,
                "epoch_after": self.database_after.epoch_created,
                "decisions_before": self.database_before.decision_count,
                "decisions_after": self.database_after.decision_count,
                "outcomes_before": self.database_before.outcome_count,
                "outcomes_after": self.database_after.outcome_count,
            },
            "ig_counters": {
                "auth": self.counters.authentication_request_count,
                "account_reads": self.counters.account_read_count,
                "market_metadata_reads": self.counters.market_read_count,
                "rest_live_price_reads": 0,
                "historical_reads": self.counters.historical_price_read_count,
                "stream_subscriptions": self.stream.subscriptions_attempted,
                "session_logouts": self.counters.session_logout_count,
            },
            "rest_budget": self.request_budget.document(),
            "execution": {
                **self.counters.execution_safety_document(),
                "position_updates": 0,
                "live_actions": 0,
                "azure_actions": 0,
                "execution_authority": "OFF",
            },
            "process_cleanup": {
                "passed": self.cleanup_passed,
                "rest_logout": self.rest_logout_result,
                "rest_logout_http_status": self.rest_logout_http_status,
                "rest_logout_upstream_error_code": self.rest_logout_upstream_error_code,
                "rest_logout_allowance_affected": self.rest_logout_allowance_affected,
                "workers_left_active": self.workers_left_active,
            },
            "tournament_epoch_created": self.database_after.epoch_created,
            "prospective_decisions": self.database_after.decision_count,
            "historically_qualified_strategies": 0,
            "demo_approved_strategies": 0,
        }


class _SmokeBlocked(RuntimeError):
    """A stable safe reason that must halt later network activity."""

    def __init__(self, reason_code: str) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code


class _SmokeApiAllowanceExceeded(RuntimeError):
    """A sanitized IG allowance response requires an immediate local stop."""


class _BudgetedCachedSmokeBroker:
    """In-memory read cache and pre-request budget directly above the broker.

    The wrapper exposes no action methods.  A document already read in this
    smoke is returned from memory, so diagnostics can reuse it without
    spending another IG REST request.
    """

    def __init__(
        self,
        broker: Shadow01ReadOnlyBroker,
        request_budget: ShadowSmokeRequestBudget,
    ) -> None:
        self._broker = broker
        self._request_budget = request_budget
        self._market_documents: dict[str, object] = {}
        self._history_documents: dict[str, object] = {}
        self._allowance_exhausted = False

    @property
    def execution_authority(self) -> str:
        return self._broker.execution_authority

    @property
    def request_counters(self) -> ReadOnlyBrokerRequestCounters:
        return self._broker.request_counters

    @property
    def request_budget(self) -> ShadowSmokeRequestBudget:
        return self._request_budget

    @property
    def allowance_exhausted(self) -> bool:
        return self._allowance_exhausted

    @property
    def underlying_broker(self) -> Shadow01ReadOnlyBroker:
        return self._broker

    def authenticate(self) -> bool:
        return self._request("auth", self._broker.authenticate)

    def read_account(self) -> object:
        return self._request("account", self._broker.read_account)

    def read_market(self, epic: str) -> object:
        if epic in self._market_documents:
            return self._market_documents[epic]
        document = self._request("market", self._broker.read_market, epic)
        self._market_documents[epic] = document
        return document

    def read_historical_prices(self, epic: str, resolution: str, points: int) -> object:
        if epic in self._history_documents and resolution == "DAY" and points <= 300:
            return self._history_documents[epic]
        document = self._request(
            "history",
            self._broker.read_historical_prices,
            epic,
            resolution,
            points,
        )
        if resolution == "DAY" and points >= 300:
            self._history_documents[epic] = document
        return document

    def logout(self) -> bool:
        return self._request("logout", self._broker.logout, allow_after_allowance=True)

    def account_state_is_valid(self, document: object) -> bool:
        return self._broker.account_state_is_valid(document)

    def stream_session_material(self) -> object:
        return self._broker.stream_session_material()

    def latest_response_diagnostic(self) -> dict[str, int | str | None] | None:
        return self._broker.latest_response_diagnostic()

    def reserve_stream_envelope(self) -> None:
        """Confirm stream lifecycle has no separate REST session envelope."""

    def _request(
        self,
        category: str,
        operation: Callable[..., object],
        *args: object,
        allow_after_allowance: bool = False,
    ) -> object:
        if self._allowance_exhausted and not allow_after_allowance:
            raise _SmokeApiAllowanceExceeded("SHADOW01_BLOCKED_IG_API_ALLOWANCE")
        self._request_budget = self._request_budget.reserve(category)
        try:
            return operation(*args)
        except Exception:
            if _ig_api_allowance_exceeded(self._broker.latest_response_diagnostic()):
                self._allowance_exhausted = True
                raise _SmokeApiAllowanceExceeded("SHADOW01_BLOCKED_IG_API_ALLOWANCE") from None
            raise


class _CapturingWarmupBroker:
    """Retain one bounded history document only in memory for the dry snapshot."""

    def __init__(self, broker: _BudgetedCachedSmokeBroker) -> None:
        self._broker = broker
        self._documents: dict[str, Mapping[str, object]] = {}

    @property
    def execution_authority(self) -> str:
        return self._broker.execution_authority

    def read_historical_prices(self, epic: str, resolution: str, points: int) -> object:
        document = self._broker.read_historical_prices(epic, resolution, points)
        if isinstance(document, Mapping):
            self._documents[epic] = document
        return document

    def latest_response_diagnostic(self) -> dict[str, int | str | None] | None:
        """Forward only the broker's sanitized response evidence."""

        return self._broker.latest_response_diagnostic()

    def document_for(self, epic: str) -> Mapping[str, object] | None:
        return self._documents.get(epic)

    def clear(self) -> None:
        self._documents.clear()


class LiveReadOnlySmokeV2:
    """Execute only one explicitly authorized bounded V2 read-only smoke."""

    def __init__(
        self,
        *,
        authorization: str,
        rest_factory: object | None = None,
        stream_factory: object | None = None,
        config_loader: Callable[[], ShadowTournamentConfig] = load_config,
        registry_loader: Callable[[ShadowTournamentConfig, Path], ShadowMarketRegistry] = (
            load_verified_dq03_registry
        ),
        registry_path: Path = DEFAULT_DQ03_REGISTRY_PATH,
        database_path: Path = DEFAULT_SHADOW_DATABASE_PATH,
        database_reader: Callable[[Path], SmokeDatabaseState] | None = None,
        env_file_ignored: Callable[[], bool] | None = None,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        request_budget: ShadowSmokeRequestBudget | None = None,
    ) -> None:
        self._authorization = authorization
        self._rest_factory = rest_factory or Shadow01LocalDemoReadOnlyFactory()
        self._stream_factory = stream_factory or Shadow01LocalDemoReadOnlyStreamFactory()
        self._config_loader = config_loader
        self._registry_loader = registry_loader
        self._registry_path = registry_path
        self._database_path = database_path
        self._database_reader = database_reader or read_smoke_database_state
        self._env_file_ignored = env_file_ignored or _env_file_is_ignored
        self._now = now
        if request_budget is not None and not isinstance(request_budget, ShadowSmokeRequestBudget):
            raise TypeError("Shadow smoke request budget must be verified")
        if request_budget is not None and request_budget.used_requests != 0:
            raise ValueError("Shadow smoke request budget must be unused")
        self._request_budget = request_budget or ShadowSmokeRequestBudget()

    def run(self) -> LiveReadOnlySmokeV2Result:
        """Run the fixed sequence once, with cleanup and DB checks in ``finally``."""

        rest_status: LocalDemoReadOnlyStatus | None = None
        stream_status: LocalDemoReadOnlyStatus | None = None
        database_before = SmokeDatabaseState(False, False, 0, 0)
        database_after = SmokeDatabaseState(False, False, 0, 0)
        observations: tuple[MarketSmokeObservation, ...] = ()
        live_quotes: dict[str, ShadowLiveQuote] = {}
        stream = StreamSmokeResult()
        clock: tuple[ClockSmokeAssessment, ...] = ()
        clock_overall = "NOT_RUN"
        clock_diagnostic: Mapping[str, object] = {}
        warmup: tuple[WarmupSmokeObservation, ...] = ()
        dry = DrySnapshotSmokeResult()
        broker: _BudgetedCachedSmokeBroker | None = None
        bridge: ShadowReadOnlyStreamBridge | None = None
        preflight_passed = False
        env_file_ignored = False
        demo_confirmed = False
        expected_account_match = False
        token_presence = False
        account_state_valid = False
        rest_logout_result = "NOT_REQUIRED"
        rest_logout_http_status: int | None = None
        rest_logout_upstream_error_code: str | None = None
        rest_logout_allowance_affected = False
        cleanup_passed = False
        provisional_status: str | None = None
        config: ShadowTournamentConfig | None = None
        registry: ShadowMarketRegistry | None = None

        try:
            if self._authorization != _LIVE_AUTHORIZATION:
                raise _SmokeBlocked("SHADOW01_BLOCKED_LIVE_AUTHORIZATION_REQUIRED")
            config = self._config_loader()
            _require_frozen_config(config)
            registry = self._registry_loader(config, self._registry_path)
            _require_authoritative_registry(registry)
            database_before = self._database_reader(self._database_path)
            if not database_before.known:
                raise _SmokeBlocked("SHADOW01_BLOCKED_DATABASE_STATE_UNVERIFIED")
            if not database_before.clean:
                raise _SmokeBlocked("SHADOW01_BLOCKED_PERSISTENT_STATE_PRESENT")
            env_file_ignored = self._env_file_ignored() is True
            if not env_file_ignored:
                raise _SmokeBlocked("SHADOW01_BLOCKED_ENV_IGNORE_UNVERIFIED")
            rest_status = _factory_status(self._rest_factory)
            stream_status = _factory_status(self._stream_factory)
            if rest_status is None or stream_status is None:
                raise _SmokeBlocked("SHADOW01_BLOCKED_DEMO_READ_ONLY_STATUS_UNAVAILABLE")
            if not rest_status.ready:
                raise _SmokeBlocked(_blocked_reason(rest_status.reason_code))
            if not stream_status.ready:
                raise _SmokeBlocked(_blocked_reason(stream_status.reason_code))
            if (
                rest_status.execution_authority != "OFF"
                or stream_status.execution_authority != "OFF"
            ):
                raise _SmokeBlocked("SHADOW01_BLOCKED_EXECUTION_AUTHORITY_VIOLATION")

            base_broker = _build_read_only_broker(self._rest_factory)
            broker = _BudgetedCachedSmokeBroker(base_broker, self._request_budget)
            if (
                broker.execution_authority != "OFF"
                or broker.request_counters != ReadOnlyBrokerRequestCounters.zero()
            ):
                raise _SmokeBlocked("SHADOW01_BLOCKED_READ_ONLY_BOUNDARY_UNVERIFIED")
            preflight_passed = True

            broker.authenticate()
            demo_confirmed = True
            expected_account_match = True
            token_presence = True
            account_document = broker.read_account()
            account_state_valid = broker.account_state_is_valid(account_document)
            del account_document
            if not account_state_valid:
                raise _SmokeBlocked("SHADOW01_BLOCKED_DEMO_ACCOUNT_STATE_INVALID")
            stream_material = broker.stream_session_material()
            bridge = _build_read_only_bridge(
                self._stream_factory,
                registry,
                session_material=stream_material,
            )
            if bridge.execution_authority != "OFF" or bridge.connected or bridge.subscribed_epics:
                raise _SmokeBlocked("SHADOW01_BLOCKED_READ_ONLY_BOUNDARY_UNVERIFIED")

            observations = self._read_all_dq03_markets(config, registry, broker)
            if sum(item.identity_verified for item in observations) != 20:
                raise _SmokeBlocked("SHADOW01_BLOCKED_DQ03_LIVE_IDENTITY_INCOMPLETE")
            if any(not item.metadata_available for item in observations):
                raise _SmokeBlocked("SHADOW01_BLOCKED_LIVE_MARKET_METADATA_INCOMPLETE")

            observed_at = _require_utc_now(self._now())
            self._prefetch_representative_history(registry, broker)
            warmup_report, capture = self._run_warmup(
                config=config,
                registry=registry,
                broker=broker,
                observed_at=observed_at,
            )
            try:
                warmup = _warmup_observations(warmup_report)
                if any(
                    item.upstream_error_code == "error.public-api.exceeded-api-key-allowance"
                    for item in warmup
                ):
                    raise _SmokeApiAllowanceExceeded("SHADOW01_BLOCKED_IG_API_ALLOWANCE")

                broker.reserve_stream_envelope()
                stream, live_quotes = self._exercise_stream(
                    config=config,
                    registry=registry,
                    bridge=bridge,
                    observations=observations,
                )
                observations = _with_stream_quotes(observations, live_quotes)
                stream = _cleanup_stream(bridge, stream)
                if stream.disconnect_result == "FAILED":
                    raise _SmokeBlocked("SHADOW01_BLOCKED_STREAM_SESSION_CLEANUP_FAILED")
                bridge = None

                # Quote callbacks are collected after the warmup timestamp.  Recheck the
                # same canonical EPIC map against a post-stream time, never against the
                # pre-stream timestamp that would make a later valid quote look future.
                clock_observed_at = _require_utc_now(self._now())
                clock_report = verify_shadow_session_clock(
                    config=config,
                    registry=registry,
                    broker=broker,
                    observed_at_utc=clock_observed_at,
                    live_quotes=live_quotes,
                )
                clock = _clock_observations(clock_report)
                clock_overall = clock_report.overall_state.value
                clock_diagnostic = clock_report.document()

                dry = _run_dry_snapshot_if_ready(
                    config=config,
                    registry=registry,
                    broker=broker.underlying_broker,
                    warmup=warmup_report,
                    capture=capture,
                    observed_at=observed_at,
                    live_quotes=live_quotes,
                )
            finally:
                capture.clear()
        except _SmokeApiAllowanceExceeded:
            provisional_status = "SHADOW01_BLOCKED_IG_API_ALLOWANCE"
        except _SmokeBlocked as error:
            provisional_status = error.reason_code
        except Exception as error:
            code = _safe_reason_code(error, "SHADOW01_BLOCKED_READ_ONLY_SMOKE_UNAVAILABLE")
            provisional_status = _blocked_reason(code)
        finally:
            if bridge is not None:
                stream = _cleanup_stream(bridge, stream)
            if broker is not None:
                try:
                    broker.logout()
                    rest_logout_result = "LOGGED_OUT"
                except Exception as error:
                    code = _safe_reason_code(error, "SHADOW01_DEMO_SESSION_LOGOUT_FAILED")
                    rest_logout_result = "FAILED"
                    diagnostic = broker.latest_response_diagnostic()
                    if diagnostic is not None:
                        rest_logout_http_status = diagnostic["status_code"]
                        rest_logout_upstream_error_code = diagnostic["upstream_error_code"]
                    rest_logout_allowance_affected = (
                        broker.allowance_exhausted or _ig_api_allowance_exceeded(diagnostic)
                    )
                    if rest_logout_allowance_affected:
                        provisional_status = "SHADOW01_BLOCKED_IG_API_ALLOWANCE"
                    if code == "SHADOW01_DEMO_ACCOUNT_MISMATCH":
                        provisional_status = "SHADOW01_BLOCKED_DEMO_ACCOUNT_MISMATCH"
            if database_before.known:
                database_after = self._database_reader(self._database_path)
            cleanup_passed = rest_logout_result in {
                "LOGGED_OUT",
                "NOT_REQUIRED",
            } and stream.disconnect_result in {"PASS", "NOT_REQUIRED", "ALREADY_DISCONNECTED"}

        status = _final_status(
            provisional_status=provisional_status,
            database_before=database_before,
            database_after=database_after,
            observations=observations,
            stream=stream,
            clock_overall=clock_overall,
            warmup=warmup,
            dry=dry,
            cleanup_passed=cleanup_passed,
        )
        return LiveReadOnlySmokeV2Result(
            status=status,
            preflight_passed=preflight_passed,
            env_file_ignored=env_file_ignored,
            demo_confirmed=demo_confirmed,
            expected_account_match=expected_account_match,
            token_presence=token_presence,
            account_state_valid=account_state_valid,
            rest_status=rest_status,
            stream_status=stream_status,
            dq03_observations=observations,
            stream=stream,
            clock=clock,
            clock_overall_state=clock_overall,
            clock_diagnostic=clock_diagnostic,
            warmup=warmup,
            dry_snapshot=dry,
            database_before=database_before,
            database_after=database_after,
            counters=(
                broker.request_counters
                if broker is not None
                else ReadOnlyBrokerRequestCounters.zero()
            ),
            request_budget=(broker.request_budget if broker is not None else self._request_budget),
            rest_logout_result=rest_logout_result,
            rest_logout_http_status=rest_logout_http_status,
            rest_logout_upstream_error_code=rest_logout_upstream_error_code,
            rest_logout_allowance_affected=rest_logout_allowance_affected,
            cleanup_passed=cleanup_passed,
            workers_left_active=False,
        )

    def _read_all_dq03_markets(
        self,
        config: ShadowTournamentConfig,
        registry: ShadowMarketRegistry,
        broker: _BudgetedCachedSmokeBroker,
    ) -> tuple[MarketSmokeObservation, ...]:
        maximum_age = _maximum_quote_age_seconds(config)
        readings: list[MarketSmokeObservation] = []
        for market in require_exact_twenty(registry):
            assert market.epic is not None
            try:
                document = broker.read_market(market.epic)
            except (_SmokeApiAllowanceExceeded, ShadowSmokeRequestBudgetError):
                raise
            except Exception as error:
                code = _safe_reason_code(error, "SHADOW01_LIVE_MARKET_METADATA_UNAVAILABLE")
                if code == "SHADOW01_DEMO_ACCOUNT_MISMATCH":
                    raise _SmokeBlocked("SHADOW01_BLOCKED_DEMO_ACCOUNT_MISMATCH") from None
                readings.append(
                    MarketSmokeObservation(
                        symbol=market.symbol,
                        metadata_available=False,
                        identity_verified=False,
                        market_status="UNKNOWN",
                        quote_availability="UNAVAILABLE",
                        quote_timestamp_freshness="UNAVAILABLE",
                        minimum_stop_metadata_available=False,
                        reason_code=code,
                    )
                )
                continue
            readings.append(
                _market_observation(
                    symbol=market.symbol,
                    epic=market.epic,
                    document=document,
                    observed_at=_require_utc_now(self._now()),
                    maximum_age_seconds=maximum_age,
                )
            )
        return tuple(readings)

    def _prefetch_representative_history(
        self,
        registry: ShadowMarketRegistry,
        broker: _BudgetedCachedSmokeBroker,
    ) -> None:
        """Acquire each required DAY/300 document once before cached diagnostics."""

        for symbol in _HISTORY_REPRESENTATIVES:
            market = registry.by_symbol(symbol)
            if market.epic is None:
                raise _SmokeBlocked("SHADOW01_BLOCKED_DQ03_LIVE_IDENTITY_INCOMPLETE")
            try:
                broker.read_historical_prices(market.epic, "DAY", 300)
            except _SmokeApiAllowanceExceeded:
                raise
            except Exception:
                if broker.allowance_exhausted:
                    raise _SmokeApiAllowanceExceeded("SHADOW01_BLOCKED_IG_API_ALLOWANCE") from None
                raise _SmokeBlocked("SHADOW01_BLOCKED_WARMUP_HISTORY_UNAVAILABLE") from None

    def _exercise_stream(
        self,
        *,
        config: ShadowTournamentConfig,
        registry: ShadowMarketRegistry,
        bridge: ShadowReadOnlyStreamBridge,
        observations: tuple[MarketSmokeObservation, ...],
    ) -> tuple[StreamSmokeResult, dict[str, ShadowLiveQuote]]:
        eligible_symbols = tuple(
            item.symbol
            for item in observations
            if (
                item.metadata_available
                and item.market_status == "TRADEABLE"
                and item.streaming_prices_available
            )
        )
        unavailable_symbols = tuple(
            item.symbol for item in observations if item.symbol not in eligible_symbols
        )
        if len(eligible_symbols) != 20:
            return (
                StreamSmokeResult(
                    no_update_symbols=unavailable_symbols,
                    error_code="SHADOW01_STREAM_METADATA_UNAVAILABLE",
                ),
                {},
            )
        epics = tuple(registry.by_symbol(symbol).epic for symbol in eligible_symbols)
        if any(not isinstance(epic, str) for epic in epics):
            raise _SmokeBlocked("SHADOW01_BLOCKED_STREAM_DQ03_IDENTITY_INCOMPLETE")
        verified_epics = tuple(epic for epic in epics if isinstance(epic, str))
        result = StreamSmokeResult(
            subscriptions_attempted=len(verified_epics),
            no_update_symbols=unavailable_symbols,
        )
        try:
            bridge.connect()
            bridge.subscribe_prices(verified_epics)
            result = replace(
                result,
                subscriptions_successful=len(verified_epics),
                subscription_call_successes=len(verified_epics),
            )
        except Exception as error:
            code = _safe_reason_code(error, "SHADOW01_STREAM_CONNECTION_UNAVAILABLE")
            if code == "SHADOW01_DEMO_ACCOUNT_MISMATCH":
                raise _SmokeBlocked("SHADOW01_BLOCKED_DEMO_ACCOUNT_MISMATCH") from None
            return replace(result, error_code=code), {}

        quotes_by_epic, invalid_epics, initial_error = self._collect_stream_quotes(
            config=config,
            bridge=bridge,
            epics=verified_epics,
        )
        initial_no_update_symbols = tuple(
            symbol
            for symbol, epic in zip(eligible_symbols, verified_epics, strict=True)
            if epic not in quotes_by_epic
        )
        symbols_by_epic = dict(zip(verified_epics, eligible_symbols, strict=True))
        invalid_symbols = tuple(
            symbols_by_epic[epic] for epic in invalid_epics if epic in symbols_by_epic
        )
        result = replace(
            result,
            updates_received=len(quotes_by_epic),
            no_update_symbols=initial_no_update_symbols,
            invalid_update_symbols=invalid_symbols,
            subscription_diagnostics=bridge.subscription_diagnostics,
            callback_contract_diagnostics=bridge.field_contract_diagnostics,
            invalid_reason_counts=tuple(sorted(bridge.invalid_reason_counts.items())),
            error_code=(
                initial_error
                or (
                    _classified_stream_rejection_code(bridge.invalid_reason_counts)
                    if invalid_symbols
                    else (
                        "SHADOW01_STREAM_SUBSCRIBED_NO_UPDATE"
                        if initial_no_update_symbols
                        else None
                    )
                )
            ),
        )
        if result.error_code is not None:
            return result, quotes_by_epic

        representative_epics = tuple(
            registry.by_symbol(symbol).epic for symbol in _DQ03_REPRESENTATIVES
        )
        if any(not isinstance(epic, str) for epic in representative_epics):
            raise _SmokeBlocked("SHADOW01_BLOCKED_STREAM_DQ03_IDENTITY_INCOMPLETE")
        verified_representative_epics = tuple(
            epic for epic in representative_epics if isinstance(epic, str)
        )
        try:
            bridge.reconnect_representative_prices(verified_representative_epics)
        except Exception as error:
            return (
                replace(
                    result,
                    reconnect_attempts=1,
                    restore_result="FAILED",
                    representative_reconnect_symbols=_DQ03_REPRESENTATIVES,
                    subscription_diagnostics=bridge.subscription_diagnostics,
                    error_code=_safe_reason_code(error, "SHADOW01_STREAM_RECONNECT_UNAVAILABLE"),
                ),
                quotes_by_epic,
            )
        (
            representative_quotes,
            representative_invalid,
            reconnect_error,
        ) = self._collect_stream_quotes(
            config=config,
            bridge=bridge,
            epics=verified_representative_epics,
        )
        if reconnect_error is None and len(representative_quotes) != len(
            verified_representative_epics
        ):
            reconnect_error = (
                _classified_stream_rejection_code(bridge.invalid_reason_counts)
                if representative_invalid
                else "SHADOW01_STREAM_SUBSCRIBED_NO_UPDATE"
            )
        return (
            replace(
                result,
                reconnect_attempts=1,
                restore_result=(
                    "REPRESENTATIVE_RECONNECTED" if reconnect_error is None else "FAILED"
                ),
                representative_reconnect_symbols=_DQ03_REPRESENTATIVES,
                representative_reconnect_updates=len(representative_quotes),
                subscription_diagnostics=bridge.subscription_diagnostics,
                error_code=reconnect_error,
            ),
            quotes_by_epic,
        )

    def _collect_stream_quotes(
        self,
        *,
        config: ShadowTournamentConfig,
        bridge: ShadowReadOnlyStreamBridge,
        epics: tuple[str, ...],
    ) -> tuple[dict[str, ShadowLiveQuote], tuple[str, ...], str | None]:
        """Wait once per queued update, under one frozen monotonic deadline."""

        valid_quotes: dict[str, ShadowLiveQuote] = {}
        invalid_epics: set[str] = set()
        deadline = monotonic() + _INITIAL_STREAM_UPDATE_WAIT_SECONDS
        receive_attempts = 0
        while (
            len(valid_quotes) < len(epics)
            and receive_attempts < _MAX_INITIAL_STREAM_RECEIVE_ATTEMPTS
        ):
            remaining_seconds = deadline - monotonic()
            if remaining_seconds <= 0:
                break
            receive_attempts += 1
            try:
                quote = bridge.receive_price_update(
                    observed_at=_require_utc_now(self._now()),
                    maximum_age_seconds=_maximum_quote_age_seconds(config),
                    timeout_seconds=remaining_seconds,
                )
            except ShadowStreamDisconnected:
                return valid_quotes, tuple(sorted(invalid_epics)), "SHADOW01_STREAM_DISCONNECTED"
            except Exception as error:
                return (
                    valid_quotes,
                    tuple(sorted(invalid_epics)),
                    _safe_reason_code(error, "SHADOW01_STREAM_RECEIVE_UNAVAILABLE"),
                )
            if quote is None:
                break
            if quote.quality == "VALID_QUOTE":
                valid_quotes[quote.epic] = quote
            else:
                invalid_epics.add(quote.epic)
        return valid_quotes, tuple(sorted(invalid_epics)), None

    def _run_warmup(
        self,
        *,
        config: ShadowTournamentConfig,
        registry: ShadowMarketRegistry,
        broker: _BudgetedCachedSmokeBroker,
        observed_at: datetime,
    ) -> tuple[WarmupDiagnosticReport, _CapturingWarmupBroker]:
        capture = _CapturingWarmupBroker(broker)
        try:
            report = run_shadow_warmup_diagnostic(
                config=config,
                registry=registry,
                broker=capture,
                observed_at_utc=observed_at,
                request_budget=len(_WARMUP_REPRESENTATIVES),
                representative_symbols=_WARMUP_REPRESENTATIVES,
            )
        except Exception:
            capture.clear()
            raise
        return report, capture


def run_live_readonly_smoke_v2(
    *,
    authorization: str,
) -> LiveReadOnlySmokeV2Result:
    """Run the default local V2 smoke only with the exact explicit authorization."""

    return LiveReadOnlySmokeV2(authorization=authorization).run()


def read_smoke_database_state(path: Path) -> SmokeDatabaseState:
    """Read Shadow persistence only through SQLite ``mode=ro``; never create it."""

    if not path.exists():
        return SmokeDatabaseState(True, False, 0, 0)
    if not path.is_file():
        return SmokeDatabaseState(False, False, 0, 0)
    try:
        connection = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
        with connection:
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                ).fetchall()
                if isinstance(row[0], str)
            }
            required = {
                "tournament_runs",
                "shadow_decisions",
                "outcome_labels",
                "provider_health",
                "epoch_readiness",
                "market_snapshots",
                "engine_insights",
            }
            if not required.issubset(tables):
                return SmokeDatabaseState(False, False, 0, 0)
            epoch_created = _query_count(
                connection,
                "SELECT COUNT(*) FROM tournament_runs WHERE epoch_utc IS NOT NULL",
            )
            decision_count = _query_count(connection, "SELECT COUNT(*) FROM shadow_decisions")
            outcome_count = _query_count(connection, "SELECT COUNT(*) FROM outcome_labels")
            auxiliary = sum(
                _query_count(connection, f"SELECT COUNT(*) FROM {table}")
                for table in (
                    "tournament_runs",
                    "provider_health",
                    "epoch_readiness",
                    "market_snapshots",
                    "engine_insights",
                )
            )
    except (OSError, sqlite3.Error, ValueError):
        return SmokeDatabaseState(False, False, 0, 0)
    return SmokeDatabaseState(True, epoch_created > 0, decision_count, outcome_count, auxiliary)


def _query_count(connection: sqlite3.Connection, query: str) -> int:
    value = connection.execute(query).fetchone()
    count = value[0] if value else None
    if isinstance(count, bool) or not isinstance(count, int) or count < 0:
        raise sqlite3.DatabaseError("invalid read-only count")
    return count


def _env_file_is_ignored() -> bool:
    try:
        completed = subprocess.run(
            ["git", "check-ignore", "-q", "--", ".env"],
            cwd=_ROOT,
            check=False,
            capture_output=True,
            text=False,
        )
    except OSError:
        return False
    return completed.returncode == 0


def _factory_status(factory: object) -> LocalDemoReadOnlyStatus | None:
    status = getattr(factory, "status", None)
    if not callable(status):
        return None
    try:
        value = status()
    except Exception:
        return None
    return value if isinstance(value, LocalDemoReadOnlyStatus) else None


def _build_read_only_broker(factory: object) -> Shadow01ReadOnlyBroker:
    build = getattr(factory, "build", None)
    if not callable(build):
        raise _SmokeBlocked("SHADOW01_BLOCKED_READ_ONLY_FACTORY_UNAVAILABLE")
    try:
        broker = build()
    except Exception as error:
        code = _safe_reason_code(error, "SHADOW01_DEMO_SESSION_CONSTRUCTION_FAILED")
        raise _SmokeBlocked(_blocked_reason(code)) from None
    if not isinstance(broker, Shadow01ReadOnlyBroker):
        raise _SmokeBlocked("SHADOW01_BLOCKED_READ_ONLY_BROKER_UNVERIFIED")
    return broker


def _build_read_only_bridge(
    factory: object,
    registry: ShadowMarketRegistry,
    *,
    session_material: object,
) -> ShadowReadOnlyStreamBridge:
    build = getattr(factory, "build", None)
    if not callable(build):
        raise _SmokeBlocked("SHADOW01_BLOCKED_STREAM_FACTORY_UNAVAILABLE")
    try:
        bridge = build(
            registry,
            session_material=session_material,
            max_reconnect_attempts=1,
        )
    except Exception as error:
        code = _safe_reason_code(error, "SHADOW01_STREAM_CONSTRUCTION_UNAVAILABLE")
        raise _SmokeBlocked(_blocked_reason(code)) from None
    if not isinstance(bridge, ShadowReadOnlyStreamBridge):
        raise _SmokeBlocked("SHADOW01_BLOCKED_STREAM_BRIDGE_UNVERIFIED")
    return bridge


def _require_frozen_config(config: object) -> None:
    if (
        not isinstance(config, ShadowTournamentConfig)
        or not config.fingerprint_is_valid
        or config.payload.get("execution_authority") != "OFF"
    ):
        raise _SmokeBlocked("SHADOW01_BLOCKED_FROZEN_CONFIG_UNVERIFIED")


def _require_authoritative_registry(registry: object) -> None:
    if not isinstance(registry, ShadowMarketRegistry):
        raise _SmokeBlocked("SHADOW01_BLOCKED_DQ03_REGISTRY_UNVERIFIED")
    try:
        markets = require_exact_twenty(registry)
    except ShadowRegistryError:
        raise _SmokeBlocked("SHADOW01_BLOCKED_DQ03_REGISTRY_UNVERIFIED") from None
    if (
        registry.verified_count != 20
        or registry.unavailable_count != 0
        or registry.source_path is None
        or not _valid_fingerprint(registry.source_fingerprint)
        or any(market.epic is None for market in markets)
    ):
        raise _SmokeBlocked("SHADOW01_BLOCKED_DQ03_REGISTRY_UNVERIFIED")


def _maximum_quote_age_seconds(config: ShadowTournamentConfig) -> int:
    quality = config.payload.get("quality")
    value = quality.get("maximum_price_age_seconds") if isinstance(quality, Mapping) else None
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise _SmokeBlocked("SHADOW01_BLOCKED_QUOTE_FRESHNESS_CONFIG_INVALID")
    return value


def sanitize_market_contract(document: object) -> dict[str, object]:
    """Describe one market response's shape without retaining any value data."""

    root = _mapping(document)
    return {
        "document_is_object": isinstance(document, Mapping),
        "top_level_keys": _safe_key_names(root),
        "snapshot": _contract_block_shape(root.get("snapshot")),
        "instrument": _contract_block_shape(root.get("instrument")),
        "dealing_rules": _contract_block_shape(root.get("dealingRules")),
    }


def quote_contract_diagnostic(document: object) -> dict[str, object]:
    """Compatibility alias for the V4 unordered price-ladder diagnostic."""

    return price_ladder_contract_diagnostic(document)


def price_ladder_contract_diagnostic(document: object) -> dict[str, object]:
    """Describe V4 ``priceLadder`` shape without selecting or exposing a tier."""

    snapshot_value = _mapping(document).get("snapshot")
    snapshot = _mapping(snapshot_value)
    ladder = snapshot.get("priceLadder")
    return {
        "priceLadder_present": "priceLadder" in snapshot,
        "priceLadder_type": _value_type(ladder),
        "priceLadder_length": len(ladder) if isinstance(ladder, list) else None,
        "selected_entry_index": None,
        "selected_entry_type": None,
        "bid_present": False,
        "bid_type": None,
        "bid_numeric": False,
        "ask_present": False,
        "ask_type": None,
        "ask_numeric": False,
    }


def _market_observation(
    *,
    symbol: str,
    epic: str,
    document: object,
    observed_at: datetime,
    maximum_age_seconds: int,
) -> MarketSmokeObservation:
    """Classify V4 REST metadata without selecting a REST price ladder tier."""

    del observed_at, maximum_age_seconds
    if not isinstance(document, Mapping):
        return MarketSmokeObservation(
            symbol=symbol,
            metadata_available=False,
            identity_verified=False,
            market_status="UNKNOWN",
            quote_availability="NOT_OBSERVED",
            quote_timestamp_freshness="NOT_OBSERVED",
            minimum_stop_metadata_available=False,
            reason_code="SHADOW01_LIVE_MARKET_METADATA_INVALID",
            metadata_health="UNAVAILABLE",
        )
    instrument_value = document.get("instrument")
    snapshot_value = document.get("snapshot")
    instrument = _mapping(instrument_value)
    snapshot = _mapping(snapshot_value)
    identity_verified = instrument.get("epic") == epic
    market_status = _market_status(document, instrument, snapshot)
    min_stop = _minimum_stop_present(_mapping(document.get("dealingRules")))
    streaming_prices_available = instrument.get("streamingPricesAvailable") is True
    if not identity_verified:
        return MarketSmokeObservation(
            symbol=symbol,
            metadata_available=False,
            identity_verified=False,
            market_status=market_status,
            quote_availability="NOT_OBSERVED",
            quote_timestamp_freshness="NOT_OBSERVED",
            minimum_stop_metadata_available=min_stop,
            reason_code="SHADOW01_LIVE_MARKET_IDENTITY_MISMATCH",
            metadata_health="UNAVAILABLE",
            streaming_prices_available=streaming_prices_available,
        )
    if not isinstance(instrument_value, Mapping) or not isinstance(snapshot_value, Mapping):
        return MarketSmokeObservation(
            symbol=symbol,
            metadata_available=False,
            identity_verified=True,
            market_status=market_status,
            quote_availability="NOT_OBSERVED",
            quote_timestamp_freshness="NOT_OBSERVED",
            minimum_stop_metadata_available=min_stop,
            reason_code="SHADOW01_LIVE_MARKET_METADATA_INVALID",
            metadata_health="UNAVAILABLE",
            streaming_prices_available=streaming_prices_available,
        )
    return MarketSmokeObservation(
        symbol=symbol,
        metadata_available=True,
        identity_verified=True,
        market_status=market_status,
        quote_availability="NOT_OBSERVED",
        quote_timestamp_freshness="NOT_OBSERVED",
        minimum_stop_metadata_available=min_stop,
        reason_code=(
            None if streaming_prices_available else "SHADOW01_STREAMING_PRICES_UNAVAILABLE"
        ),
        metadata_health="PASS",
        streaming_prices_available=streaming_prices_available,
    )


def _with_stream_quotes(
    observations: tuple[MarketSmokeObservation, ...],
    quotes_by_epic: Mapping[str, ShadowLiveQuote],
) -> tuple[MarketSmokeObservation, ...]:
    """Join canonical stream health into REST metadata without retaining prices."""

    quotes_by_symbol = {item.symbol: item for item in quotes_by_epic.values()}
    merged: list[MarketSmokeObservation] = []
    for observation in observations:
        quote = quotes_by_symbol.get(observation.symbol)
        if quote is None:
            merged.append(
                replace(
                    observation,
                    quote_availability="UNAVAILABLE",
                    quote_timestamp_freshness="UNAVAILABLE",
                    live_quote_health="LIVE_QUOTE_UNAVAILABLE",
                    last_quote_age_seconds=None,
                    stream_connection_status="SUBSCRIBED_NO_QUOTE",
                    reason_code="SHADOW01_LIVE_STREAM_QUOTE_MISSING",
                )
            )
            continue
        freshness = {
            "VALID_QUOTE": "FRESH",
            "STALE": "STALE",
        }.get(quote.quality, "INVALID")
        merged.append(
            replace(
                observation,
                quote_availability=quote.quality,
                quote_timestamp_freshness=freshness,
                live_quote_health=(
                    "VALID_QUOTE" if quote.quality == "VALID_QUOTE" else "LIVE_QUOTE_UNAVAILABLE"
                ),
                last_quote_age_seconds=quote.quote_age_seconds,
                stream_connection_status="QUOTE_RECEIVED",
                reason_code=quote.reason_codes[0] if quote.reason_codes else None,
            )
        )
    return tuple(merged)


def _mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def _safe_key_names(document: Mapping[str, object]) -> list[str]:
    """Return bounded JSON key names only, never response values."""

    return sorted(
        key
        for key in document
        if isinstance(key, str) and re.fullmatch(r"[A-Za-z][A-Za-z0-9]{0,63}", key)
    )


def _contract_block_shape(value: object) -> dict[str, object]:
    """Return only presence, container type, and safe key names for one block."""

    if value is None:
        return {"present": False, "type": None, "keys": []}
    if isinstance(value, Mapping):
        return {"present": True, "type": "object", "keys": _safe_key_names(value)}
    if isinstance(value, list):
        return {"present": True, "type": "array", "keys": []}
    return {"present": True, "type": type(value).__name__, "keys": []}


def _market_status(*blocks: Mapping[str, object]) -> str:
    for block in blocks:
        value = block.get("marketStatus")
        if isinstance(value, str) and value.strip():
            return value.strip().upper()
    return "UNKNOWN"


def _value_type(value: object) -> str | None:
    return None if value is None else type(value).__name__


def _positive_number(value: object) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (OverflowError, TypeError, ValueError):
        return None
    return number if isfinite(number) and number > 0 else None


def _minimum_stop_present(rules: Mapping[str, object]) -> bool:
    value = rules.get("minNormalStopOrLimitDistance")
    if not isinstance(value, Mapping):
        return False
    return _positive_number(value.get("value")) is not None


def _quote_timestamp_freshness(
    value: object,
    observed_at: datetime,
    maximum_age_seconds: int,
) -> str:
    parse_status, seconds = _quote_timestamp_parse_status(value)
    if parse_status not in {"SECONDS_EPOCH", "MILLISECONDS_EPOCH"} or seconds is None:
        return parse_status
    try:
        timestamp = datetime.fromtimestamp(seconds, UTC)
    except (OverflowError, OSError, ValueError):
        return "SCHEMA_UNSUPPORTED"
    if timestamp < _QUOTE_TIMESTAMP_MODERN_LOWER_BOUND:
        return "IMPLAUSIBLY_OLD"
    age_seconds = (observed_at - timestamp).total_seconds()
    if age_seconds < -_QUOTE_TIMESTAMP_FUTURE_SKEW_SECONDS:
        return "FUTURE_INVALID"
    return "STALE" if age_seconds > maximum_age_seconds else "FRESH"


def _quote_timestamp_parse_status(value: object) -> tuple[str, float | None]:
    """Normalize numeric epoch seconds or milliseconds without accepting strings."""

    if value is None:
        return "MISSING", None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return "SCHEMA_UNSUPPORTED", None
    try:
        numeric_timestamp = float(value)
    except (OverflowError, TypeError, ValueError):
        return "INVALID", None
    if not isfinite(numeric_timestamp) or numeric_timestamp <= 0:
        return "INVALID", None
    if numeric_timestamp >= _QUOTE_TIMESTAMP_MILLISECONDS_THRESHOLD:
        return "MILLISECONDS_EPOCH", numeric_timestamp / 1000
    return "SECONDS_EPOCH", numeric_timestamp


def _clock_observations(report: ClockDiagnosticReport) -> tuple[ClockSmokeAssessment, ...]:
    return tuple(
        ClockSmokeAssessment(
            asset_class=item.asset_class,
            state=item.state.value,
            evidence=item.reason_codes,
            proposed_clock=item.proposed_clock_for_human_review,
        )
        for item in report.assessments
    )


def _warmup_observations(report: WarmupDiagnosticReport) -> tuple[WarmupSmokeObservation, ...]:
    return tuple(_warmup_observation(item) for item in report.markets)


def _warmup_observation(item: WarmupMarketReport) -> WarmupSmokeObservation:
    return WarmupSmokeObservation(
        symbol=item.symbol,
        historical_requests=int(item.request_dispatched),
        bars_received=item.bars_received,
        completed_sessions=item.completed_sessions,
        t1_ready=item.t1_history_ready,
        m1_ready=item.m1_history_ready,
        q1_ready=item.q1_history_ready,
        quality_result=item.data_quality_result.value,
        reason_codes=item.reason_codes,
        http_status_code=item.http_status_code,
        upstream_error_code=item.upstream_error_code,
    )


def _run_dry_snapshot_if_ready(
    *,
    config: ShadowTournamentConfig,
    registry: ShadowMarketRegistry,
    broker: Shadow01ReadOnlyBroker,
    warmup: WarmupDiagnosticReport,
    capture: _CapturingWarmupBroker,
    observed_at: datetime,
    live_quotes: Mapping[str, ShadowLiveQuote],
) -> DrySnapshotSmokeResult:
    for item in warmup.markets:
        if not (item.t1_history_ready and item.m1_history_ready and item.q1_history_ready):
            continue
        market = registry.by_symbol(item.symbol)
        if market.epic is None:
            continue
        quote = live_quotes.get(market.epic)
        if quote is None or quote.quality != "VALID_QUOTE":
            continue
        document = capture.document_for(market.epic)
        if document is None:
            continue
        try:
            bars = parse_completed_daily_bars(document, decision_timestamp_utc=observed_at)
            result = run_shadow_dry_snapshot(
                config=config,
                registry=registry,
                broker=broker,
                context=DrySnapshotContext(
                    observed_at_utc=observed_at,
                    markets=(
                        DrySnapshotMarketInput(
                            instrument=market.symbol,
                            epic=market.epic,
                            completed_bars=bars,
                            input_data_fingerprint=fingerprint(document),
                            live_quote=quote,
                        ),
                    ),
                    stream_healthy=True,
                ),
            )
        except (ShadowDataError, ValueError):
            return DrySnapshotSmokeResult(status="NOT_RUN")
        return _dry_snapshot_projection(result)
    return DrySnapshotSmokeResult(status="NOT_RUN")


def _dry_snapshot_projection(result: DrySnapshotResult) -> DrySnapshotSmokeResult:
    market = result.markets[0]
    policies = {
        item.recommendation.policy_id.value: item.recommendation.direction.value
        for item in market.policies
    }
    return DrySnapshotSmokeResult(
        status=result.status,
        t1=market.trend.direction.value,
        m1=market.reversion.direction.value,
        x1=market.cross_asset.state.value,
        f1=market.fundamental.state.value,
        q1=market.quality.state.value,
        c1=market.cost.state.value,
        p0=policies.get("P0_TECHNICAL_TREND_ONLY", "NOT_RUN"),
        p1=policies.get("P1_TECHNICAL_REVERSION_ONLY", "NOT_RUN"),
        p2=policies.get("P2_TREND_PLUS_CROSS_ASSET", "NOT_RUN"),
        p3=policies.get("P3_CONSERVATIVE_CONTEXT", "NOT_RUN"),
    )


def _cleanup_stream(
    bridge: ShadowReadOnlyStreamBridge,
    result: StreamSmokeResult,
) -> StreamSmokeResult:
    unsubscribe_result = result.unsubscribe_result
    disconnect_result = result.disconnect_result
    error_code = result.error_code
    if bridge.connected and bridge.subscribed_epics:
        try:
            bridge.unsubscribe_prices(bridge.subscribed_epics)
            unsubscribe_result = "PASS"
        except Exception as error:
            unsubscribe_result = "FAILED"
            error_code = error_code or _safe_reason_code(
                error,
                "SHADOW01_STREAM_UNSUBSCRIPTION_UNAVAILABLE",
            )
    if bridge.connected:
        try:
            bridge.disconnect()
            disconnect_result = "PASS"
        except Exception as error:
            disconnect_result = "FAILED"
            error_code = error_code or _safe_reason_code(
                error,
                "SHADOW01_STREAM_DISCONNECT_UNAVAILABLE",
            )
    elif result.subscriptions_attempted and disconnect_result == "NOT_REQUIRED":
        disconnect_result = "ALREADY_DISCONNECTED"
    return replace(
        result,
        unsubscribe_result=unsubscribe_result,
        disconnect_result=disconnect_result,
        error_code=error_code,
    )


def _final_status(
    *,
    provisional_status: str | None,
    database_before: SmokeDatabaseState,
    database_after: SmokeDatabaseState,
    observations: tuple[MarketSmokeObservation, ...],
    stream: StreamSmokeResult,
    clock_overall: str,
    warmup: tuple[WarmupSmokeObservation, ...],
    dry: DrySnapshotSmokeResult,
    cleanup_passed: bool,
) -> str:
    if database_before.known and (not database_after.known or database_after != database_before):
        return "SHADOW01_BLOCKED_PROSPECTIVE_DATA_CONTAMINATION"
    if provisional_status is not None:
        return provisional_status
    if not database_after.known:
        return "SHADOW01_BLOCKED_DATABASE_STATE_UNVERIFIED"
    if not cleanup_passed:
        return "SHADOW01_BLOCKED_PROCESS_CLEANUP_FAILED"
    if len(observations) != 20 or sum(item.identity_verified for item in observations) != 20:
        return "SHADOW01_BLOCKED_DQ03_LIVE_IDENTITY_INCOMPLETE"
    if any(item.metadata_health != "PASS" for item in observations):
        return "SHADOW01_BLOCKED_LIVE_MARKET_METADATA_INCOMPLETE"
    if stream.error_code is not None:
        return "SHADOW01_BLOCKED_STREAM_SMOKE_INCOMPLETE"
    if any(item.live_quote_health != "VALID_QUOTE" for item in observations):
        return "SHADOW01_BLOCKED_LIVE_MARKET_DATA_INCOMPLETE"
    if clock_overall == ClockDiagnosticState.UNSUITABLE.value:
        return "SHADOW01_SESSION_CLOCK_HUMAN_GATE_REQUIRED"
    if clock_overall != ClockDiagnosticState.PASS.value:
        return "SHADOW01_BLOCKED_CLOCK_LIVE_VERIFICATION_INCOMPLETE"
    if len(warmup) != len(_WARMUP_REPRESENTATIVES) or not all(
        item.t1_ready and item.m1_ready and item.q1_ready for item in warmup
    ):
        return "SHADOW01_BLOCKED_WARMUP_READINESS_INSUFFICIENT"
    if dry.status != "DRY_RUN_NON_PROSPECTIVE":
        return "SHADOW01_BLOCKED_DRY_SNAPSHOT_NOT_RUN"
    return "SHADOW01_LIVE_READONLY_SMOKE_PASS"


def _safe_reason_code(error: BaseException, fallback: str) -> str:
    """Allow only stable SHADOW reason codes to leave an exception boundary."""

    candidate = str(error)
    return candidate if _SAFE_REASON.fullmatch(candidate) is not None else fallback


def _classified_stream_rejection_code(reason_counts: Mapping[str, int]) -> str:
    """Name the first observed safe rejection category; never use a generic invalid code."""

    for category, code in _STREAM_REJECTION_CODES:
        count = reason_counts.get(category)
        if isinstance(count, int) and not isinstance(count, bool) and count > 0:
            return code
    return "SHADOW01_STREAM_QUOTE_REJECTION_UNCLASSIFIED"


def _ig_api_allowance_exceeded(diagnostic: object) -> bool:
    """Recognize only IG's documented API-key allowance code from safe evidence."""

    return isinstance(diagnostic, Mapping) and diagnostic.get("upstream_error_code") == (
        "error.public-api.exceeded-api-key-allowance"
    )


def _blocked_reason(reason: str) -> str:
    if reason == "SHADOW01_DEMO_ACCOUNT_MISMATCH":
        return "SHADOW01_BLOCKED_DEMO_ACCOUNT_MISMATCH"
    if reason.startswith("SHADOW01_BLOCKED_"):
        return reason
    suffix = reason.removeprefix("SHADOW01_")
    return f"SHADOW01_BLOCKED_{suffix}"


def _require_utc_now(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise _SmokeBlocked("SHADOW01_BLOCKED_DIAGNOSTIC_CLOCK_INVALID")
    return value.astimezone(UTC)


def _valid_fingerprint(value: object) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


__all__ = (
    "ClockSmokeAssessment",
    "DEFAULT_DQ03_REGISTRY_PATH",
    "DEFAULT_SHADOW_DATABASE_PATH",
    "DrySnapshotSmokeResult",
    "LiveReadOnlySmokeV2",
    "LiveReadOnlySmokeV2Result",
    "MarketSmokeObservation",
    "ShadowSmokeRequestBudget",
    "ShadowSmokeRequestBudgetError",
    "SmokeDatabaseState",
    "StreamSmokeResult",
    "WarmupSmokeObservation",
    "read_smoke_database_state",
    "run_live_readonly_smoke_v2",
    "price_ladder_contract_diagnostic",
    "quote_contract_diagnostic",
    "sanitize_market_contract",
)
