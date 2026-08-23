"""Offline-only, fail-closed domain for future IG Demo qualification.

This module intentionally contains no HTTP client, session manager, broker
credentials, or production transport.  DQ-01 validates immutable requests and
the lifecycle using ``FakeIGDemoTransport`` only; a later work order must add
an explicitly authorized network adapter.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Final, Protocol
from uuid import UUID

from src.ig_trader.frozen_v1_policy import FROZEN_V1_PRODUCTION_INSTRUMENTS

DEAL_REFERENCE_PATTERN: Final = re.compile(r"[A-Za-z0-9_-]{1,30}\Z")
DEMO_ENVIRONMENT: Final = "IG_DEMO"
TRADEABLE_MARKET_STATUS: Final = "TRADEABLE"
DEFAULT_METADATA_MAX_AGE: Final = timedelta(seconds=60)
APPROVED_DEMO_EPICS: Final = frozenset(
    epic for _symbol, epic, _base_currency, _quote_currency in FROZEN_V1_PRODUCTION_INSTRUMENTS
)


class DemoExecutionError(RuntimeError):
    """A Demo qualification operation was rejected without an implicit retry."""


class DemoExecutionMode(StrEnum):
    NO_EXECUTION = "NO_EXECUTION"
    SHADOW_DEMO = "SHADOW_DEMO"
    DEMO_EXECUTION = "DEMO_EXECUTION"
    LIVE_EXECUTION = "LIVE_EXECUTION"


class DemoExecutionLifecycle(StrEnum):
    PREPARED = "PREPARED"
    SUBMITTING = "SUBMITTING"
    SUBMITTED = "SUBMITTED"
    CONFIRMED_ACCEPTED = "CONFIRMED_ACCEPTED"
    CONFIRMED_REJECTED = "CONFIRMED_REJECTED"
    OPEN_RECONCILED = "OPEN_RECONCILED"
    CLOSE_REQUESTED = "CLOSE_REQUESTED"
    CLOSED_RECONCILED = "CLOSED_RECONCILED"
    FAILED_SAFE = "FAILED_SAFE"
    AMBIGUOUS = "AMBIGUOUS"


class DemoDirection(StrEnum):
    BUY = "BUY"
    SELL = "SELL"

    @property
    def opposite(self) -> DemoDirection:
        return DemoDirection.SELL if self is DemoDirection.BUY else DemoDirection.BUY


class DemoOrderType(StrEnum):
    MARKET = "MARKET"


class DemoConfirmationStatus(StrEnum):
    ACCEPTED = "ACCEPTED"
    REJECTED = "REJECTED"


class KillSwitchState(StrEnum):
    BLOCKING = "BLOCKING"
    RELEASED = "RELEASED"


@dataclass(frozen=True)
class DemoRiskApproval:
    """Immutable proof that Frozen V1 / PortfolioRisk already allowed the intent."""

    configuration_identity: str
    allowed: bool
    evaluated_at: datetime


@dataclass(frozen=True)
class DemoExecutionRequest:
    """Frozen economic facts; this domain never accepts a raw strategy signal."""

    intent_id: UUID
    global_cycle_id: UUID
    epic: str
    direction: DemoDirection
    size: Decimal
    currency_code: str
    expiry: str
    order_type: DemoOrderType
    force_open: bool
    guaranteed_stop: bool
    stop_distance: Decimal | None
    stop_level: Decimal | None
    limit_distance: Decimal | None
    limit_level: Decimal | None
    deal_reference: str
    configuration_identity: str
    risk_approval: DemoRiskApproval
    fencing_token: int
    created_at: datetime

    def __post_init__(self) -> None:
        _validate_request_shape(self)


@dataclass(frozen=True)
class DemoMarketMetadata:
    """Read-only market facts that DQ-02 will obtain from IG market metadata."""

    epic: str
    instrument_currency: str | None
    expiry: str | None
    pip_scale: Decimal | None
    decimal_places: int | None
    minimum_deal_size: Decimal | None
    minimum_stop_distance: Decimal | None
    guaranteed_stop_supported: bool | None
    market_status: str | None
    observed_at: datetime | None


@dataclass(frozen=True)
class DemoSubmission:
    """Sanitized broker submission reference, without response-body retention."""

    deal_reference: str
    deal_id: str | None = None


@dataclass(frozen=True)
class DemoConfirmation:
    """The minimum broker facts needed to verify an accepted or rejected dealing result."""

    deal_reference: str
    deal_id: str | None
    deal_status: DemoConfirmationStatus
    status: str
    epic: str | None
    direction: DemoDirection | None
    size: Decimal | None
    level: Decimal | None = None
    stop_level: Decimal | None = None
    limit_level: Decimal | None = None


@dataclass(frozen=True)
class DemoPosition:
    """Safe projected facts from a broker position snapshot."""

    deal_id: str
    epic: str
    direction: DemoDirection
    size: Decimal


@dataclass(frozen=True)
class DemoExecutionRecord:
    """Durable-intent-shaped lifecycle record; in DQ-01 the store is in-memory only."""

    request: DemoExecutionRequest
    lifecycle: DemoExecutionLifecycle
    submission: DemoSubmission | None = None
    confirmation: DemoConfirmation | None = None
    position: DemoPosition | None = None
    close_submission: DemoSubmission | None = None


@dataclass(frozen=True)
class DemoAuthorityGate:
    """All explicit authority facts required for a broker-mutating Demo action."""

    execution_mode: DemoExecutionMode
    demo_order_authority: bool
    environment: str | None
    expected_demo_account_id: str | None
    authenticated_account_id: str | None
    lease_valid: bool
    current_fencing_token: int | None
    global_position_count: int | None
    global_position_limit: int | None
    approved_epics: frozenset[str]
    kill_switch_state: KillSwitchState

    def validate_create(self, request: DemoExecutionRequest) -> None:
        self._validate_common(request, require_capacity=True)

    def validate_close(self, request: DemoExecutionRequest) -> None:
        self._validate_common(request, require_capacity=False)

    def _validate_common(self, request: DemoExecutionRequest, *, require_capacity: bool) -> None:
        if self.execution_mode is not DemoExecutionMode.DEMO_EXECUTION:
            raise DemoExecutionError("Demo execution mode is not explicitly enabled")
        if not self.demo_order_authority:
            raise DemoExecutionError("Demo order authority is not explicitly enabled")
        if self.environment != DEMO_ENVIRONMENT:
            raise DemoExecutionError("Demo environment cannot be proven")
        if (
            not isinstance(self.expected_demo_account_id, str)
            or not self.expected_demo_account_id.strip()
            or not isinstance(self.authenticated_account_id, str)
            or not self.authenticated_account_id.strip()
        ):
            raise DemoExecutionError("Demo account identity is missing")
        if self.expected_demo_account_id != self.authenticated_account_id:
            raise DemoExecutionError("Demo account identity does not match")
        if not self.lease_valid:
            raise DemoExecutionError("execution lease is not valid")
        if (
            isinstance(self.current_fencing_token, bool)
            or not isinstance(self.current_fencing_token, int)
            or self.current_fencing_token <= 0
            or request.fencing_token != self.current_fencing_token
        ):
            raise DemoExecutionError("execution fencing token is stale or unavailable")
        if (
            isinstance(self.global_position_count, bool)
            or not isinstance(self.global_position_count, int)
            or self.global_position_count < 0
            or isinstance(self.global_position_limit, bool)
            or not isinstance(self.global_position_limit, int)
            or self.global_position_limit <= 0
        ):
            raise DemoExecutionError("global position state is unknown")
        if self.global_position_count > self.global_position_limit or (
            require_capacity and self.global_position_count >= self.global_position_limit
        ):
            raise DemoExecutionError("global position limit vetoed Demo execution")
        if request.epic not in self.approved_epics:
            raise DemoExecutionError("instrument is outside the approved Demo registry")
        if self.kill_switch_state is not KillSwitchState.RELEASED:
            raise DemoExecutionError("Demo kill switch is blocking")
        if not request.risk_approval.allowed:
            raise DemoExecutionError("Frozen V1 PortfolioRisk did not approve the request")
        if request.risk_approval.configuration_identity != request.configuration_identity:
            raise DemoExecutionError("Frozen V1 configuration identity does not match")
        _required_utc(request.risk_approval.evaluated_at)


class IGDemoTransport(Protocol):
    """Narrow broker protocol; no implementation may use the network in DQ-01."""

    def create_position(self, payload: Mapping[str, object]) -> DemoSubmission: ...

    def get_confirmation(self, deal_reference: str) -> DemoConfirmation | None: ...

    def list_positions(self) -> tuple[DemoPosition, ...]: ...

    def get_position(self, deal_id: str) -> DemoPosition | None: ...

    def close_position(self, payload: Mapping[str, object]) -> DemoSubmission: ...


class FakeIGDemoTransport:
    """Deterministic test transport that records calls and has no network capability."""

    def __init__(self) -> None:
        self.create_payloads: list[dict[str, object]] = []
        self.close_payloads: list[dict[str, object]] = []
        self.confirmation_references: list[str] = []
        self.position_reads: int = 0
        self._create_results: list[DemoSubmission | Exception] = []
        self._close_results: list[DemoSubmission | Exception] = []
        self._confirmation_results: dict[str, list[DemoConfirmation | None | Exception]] = {}
        self._position_results: list[tuple[DemoPosition, ...] | Exception] = []

    @property
    def broker_create_call_count(self) -> int:
        return len(self.create_payloads)

    @property
    def broker_close_call_count(self) -> int:
        return len(self.close_payloads)

    @property
    def broker_update_call_count(self) -> int:
        return 0

    @property
    def confirmation_read_count(self) -> int:
        return len(self.confirmation_references)

    @property
    def position_read_count(self) -> int:
        return self.position_reads

    def queue_create(self, *results: DemoSubmission | Exception) -> None:
        self._create_results.extend(results)

    def queue_close(self, *results: DemoSubmission | Exception) -> None:
        self._close_results.extend(results)

    def queue_confirmation(
        self,
        deal_reference: str,
        *results: DemoConfirmation | None | Exception,
    ) -> None:
        self._confirmation_results.setdefault(deal_reference, []).extend(results)

    def queue_positions(self, *results: tuple[DemoPosition, ...] | Exception) -> None:
        self._position_results.extend(results)

    def create_position(self, payload: Mapping[str, object]) -> DemoSubmission:
        self.create_payloads.append(dict(payload))
        result = self._create_results.pop(0) if self._create_results else None
        if isinstance(result, Exception):
            raise result
        if result is not None:
            return result
        return DemoSubmission(str(payload["dealReference"]))

    def get_confirmation(self, deal_reference: str) -> DemoConfirmation | None:
        self.confirmation_references.append(deal_reference)
        results = self._confirmation_results.get(deal_reference, [])
        result = results.pop(0) if results else None
        if isinstance(result, Exception):
            raise result
        return result

    def list_positions(self) -> tuple[DemoPosition, ...]:
        self.position_reads += 1
        result = self._position_results.pop(0) if self._position_results else ()
        if isinstance(result, Exception):
            raise result
        return result

    def get_position(self, deal_id: str) -> DemoPosition | None:
        return next(
            (position for position in self.list_positions() if position.deal_id == deal_id), None
        )

    def close_position(self, payload: Mapping[str, object]) -> DemoSubmission:
        self.close_payloads.append(dict(payload))
        result = self._close_results.pop(0) if self._close_results else None
        if isinstance(result, Exception):
            raise result
        if result is not None:
            return result
        return DemoSubmission(str(payload["dealReference"]))


class DemoExecutionStore(Protocol):
    def get(self, intent_id: UUID) -> DemoExecutionRecord | None: ...

    def put(self, record: DemoExecutionRecord) -> DemoExecutionRecord: ...

    def replace(self, record: DemoExecutionRecord) -> DemoExecutionRecord: ...

    def has_other_cycle_record(self, global_cycle_id: UUID, intent_id: UUID) -> bool: ...


class InMemoryDemoExecutionStore:
    """Test-only durable-shape store that enforces idempotency and cycle fencing."""

    def __init__(self) -> None:
        self.records: dict[UUID, DemoExecutionRecord] = {}

    def get(self, intent_id: UUID) -> DemoExecutionRecord | None:
        return self.records.get(intent_id)

    def put(self, record: DemoExecutionRecord) -> DemoExecutionRecord:
        existing = self.records.get(record.request.intent_id)
        if existing is not None:
            if existing.request != record.request:
                raise DemoExecutionError("duplicate Demo intent conflicts")
            return existing
        self.records[record.request.intent_id] = record
        return record

    def replace(self, record: DemoExecutionRecord) -> DemoExecutionRecord:
        existing = self.records.get(record.request.intent_id)
        if existing is None or existing.request != record.request:
            raise DemoExecutionError("Demo execution record is unknown or conflicting")
        self.records[record.request.intent_id] = record
        return record

    def has_other_cycle_record(self, global_cycle_id: UUID, intent_id: UUID) -> bool:
        return any(
            record.request.global_cycle_id == global_cycle_id
            and record.request.intent_id != intent_id
            for record in self.records.values()
        )


class DemoExecutionCore:
    """Idempotent future-Demo dealing state machine with confirmation-first recovery."""

    def __init__(self, *, transport: IGDemoTransport, store: DemoExecutionStore) -> None:
        self.transport = transport
        self.store = store

    def submit(
        self,
        request: DemoExecutionRequest,
        metadata: DemoMarketMetadata | None,
        authority: DemoAuthorityGate,
        *,
        now: datetime,
    ) -> DemoExecutionRecord:
        """Submit once only; uncertain results become AMBIGUOUS and are never retried."""

        record = self._prepare(request, metadata, authority, now=now)
        if record.lifecycle is not DemoExecutionLifecycle.PREPARED:
            return record
        submitting = self.store.replace(
            replace(record, lifecycle=DemoExecutionLifecycle.SUBMITTING)
        )
        try:
            submission = self.transport.create_position(request_to_ig_payload(request))
        except Exception:
            return self.store.replace(
                replace(submitting, lifecycle=DemoExecutionLifecycle.AMBIGUOUS)
            )
        if submission.deal_reference != request.deal_reference:
            return self.store.replace(
                replace(submitting, lifecycle=DemoExecutionLifecycle.FAILED_SAFE)
            )
        return self.store.replace(
            replace(submitting, lifecycle=DemoExecutionLifecycle.SUBMITTED, submission=submission)
        )

    def reconcile_open(self, intent_id: UUID) -> DemoExecutionRecord:
        """Read confirmation and positions before considering an accepted order open."""

        record = self._require_record(intent_id)
        if record.lifecycle is DemoExecutionLifecycle.OPEN_RECONCILED:
            return record
        if record.lifecycle not in {
            DemoExecutionLifecycle.SUBMITTED,
            DemoExecutionLifecycle.AMBIGUOUS,
            DemoExecutionLifecycle.CONFIRMED_ACCEPTED,
        }:
            return record
        try:
            confirmation = self.transport.get_confirmation(record.request.deal_reference)
        except Exception:
            return self.store.replace(replace(record, lifecycle=DemoExecutionLifecycle.AMBIGUOUS))
        if confirmation is None:
            return self.store.replace(replace(record, lifecycle=DemoExecutionLifecycle.AMBIGUOUS))
        if confirmation.deal_status is DemoConfirmationStatus.REJECTED:
            return self.store.replace(
                replace(
                    record,
                    lifecycle=DemoExecutionLifecycle.CONFIRMED_REJECTED,
                    confirmation=confirmation,
                )
            )
        if not _accepted_confirmation_matches(record.request, confirmation):
            return self.store.replace(replace(record, lifecycle=DemoExecutionLifecycle.FAILED_SAFE))
        confirmed = self.store.replace(
            replace(
                record,
                lifecycle=DemoExecutionLifecycle.CONFIRMED_ACCEPTED,
                confirmation=confirmation,
            )
        )
        try:
            positions = self.transport.list_positions()
        except Exception:
            return self.store.replace(
                replace(confirmed, lifecycle=DemoExecutionLifecycle.AMBIGUOUS)
            )
        matches = _matching_positions(positions, confirmation, record.request)
        if len(matches) != 1:
            return self.store.replace(
                replace(confirmed, lifecycle=DemoExecutionLifecycle.FAILED_SAFE)
            )
        return self.store.replace(
            replace(
                confirmed, lifecycle=DemoExecutionLifecycle.OPEN_RECONCILED, position=matches[0]
            )
        )

    def request_close(
        self,
        intent_id: UUID,
        authority: DemoAuthorityGate,
    ) -> DemoExecutionRecord:
        """Close only the reconciled broker deal, once, using its exact opposing economics."""

        record = self._require_record(intent_id)
        if record.lifecycle is DemoExecutionLifecycle.CLOSED_RECONCILED:
            return record
        if record.lifecycle is DemoExecutionLifecycle.CLOSE_REQUESTED:
            return record
        if (
            record.lifecycle is not DemoExecutionLifecycle.OPEN_RECONCILED
            or record.position is None
        ):
            raise DemoExecutionError("only a reconciled Demo position may be closed")
        authority.validate_close(record.request)
        close_reference = deterministic_close_reference(record.request, record.position.deal_id)
        payload = {
            "dealId": record.position.deal_id,
            "dealReference": close_reference,
            "direction": record.position.direction.opposite.value,
            "orderType": DemoOrderType.MARKET.value,
            "size": _decimal_text(record.position.size),
        }
        try:
            submission = self.transport.close_position(payload)
        except Exception:
            return self.store.replace(replace(record, lifecycle=DemoExecutionLifecycle.AMBIGUOUS))
        if submission.deal_reference != close_reference:
            return self.store.replace(replace(record, lifecycle=DemoExecutionLifecycle.FAILED_SAFE))
        return self.store.replace(
            replace(
                record,
                lifecycle=DemoExecutionLifecycle.CLOSE_REQUESTED,
                close_submission=submission,
            )
        )

    def reconcile_close(self, intent_id: UUID) -> DemoExecutionRecord:
        """Require accepted close confirmation and absence from the position snapshot."""

        record = self._require_record(intent_id)
        if record.lifecycle is DemoExecutionLifecycle.CLOSED_RECONCILED:
            return record
        if (
            record.lifecycle is not DemoExecutionLifecycle.CLOSE_REQUESTED
            or record.position is None
            or record.close_submission is None
        ):
            return record
        try:
            confirmation = self.transport.get_confirmation(record.close_submission.deal_reference)
        except Exception:
            return self.store.replace(replace(record, lifecycle=DemoExecutionLifecycle.AMBIGUOUS))
        if confirmation is None:
            return self.store.replace(replace(record, lifecycle=DemoExecutionLifecycle.AMBIGUOUS))
        if not _close_confirmation_matches(record, confirmation):
            return self.store.replace(replace(record, lifecycle=DemoExecutionLifecycle.FAILED_SAFE))
        try:
            positions = self.transport.list_positions()
        except Exception:
            return self.store.replace(replace(record, lifecycle=DemoExecutionLifecycle.AMBIGUOUS))
        if any(position.deal_id == record.position.deal_id for position in positions):
            return self.store.replace(replace(record, lifecycle=DemoExecutionLifecycle.FAILED_SAFE))
        return self.store.replace(
            replace(
                record,
                lifecycle=DemoExecutionLifecycle.CLOSED_RECONCILED,
                confirmation=confirmation,
            )
        )

    def evidence(self, authority: DemoAuthorityGate) -> dict[str, object]:
        """Return safe counters only; fake calls are test evidence, never real broker activity."""

        return {
            "broker_create_call_count": getattr(self.transport, "broker_create_call_count", 0),
            "broker_close_call_count": getattr(self.transport, "broker_close_call_count", 0),
            "broker_update_call_count": getattr(self.transport, "broker_update_call_count", 0),
            "confirmation_read_count": getattr(self.transport, "confirmation_read_count", 0),
            "position_read_count": getattr(self.transport, "position_read_count", 0),
            "demo_order_authority": authority.demo_order_authority,
            "execution_mode": authority.execution_mode.value,
            "kill_switch_state": authority.kill_switch_state.value,
        }

    def _prepare(
        self,
        request: DemoExecutionRequest,
        metadata: DemoMarketMetadata | None,
        authority: DemoAuthorityGate,
        *,
        now: datetime,
    ) -> DemoExecutionRecord:
        _validate_metadata_for_request(metadata, request, now=now)
        authority.validate_create(request)
        existing = self.store.get(request.intent_id)
        if existing is not None:
            if existing.request != request:
                raise DemoExecutionError("duplicate Demo intent conflicts")
            return existing
        if self.store.has_other_cycle_record(request.global_cycle_id, request.intent_id):
            raise DemoExecutionError("global execution cycle already has a Demo intent")
        return self.store.put(DemoExecutionRecord(request, DemoExecutionLifecycle.PREPARED))

    def _require_record(self, intent_id: UUID) -> DemoExecutionRecord:
        record = self.store.get(intent_id)
        if record is None:
            raise DemoExecutionError("Demo execution record is unknown")
        return record


def deterministic_deal_reference(
    *,
    intent_id: UUID,
    global_cycle_id: UUID,
    epic: str,
    configuration_identity: str,
) -> str:
    """Derive a non-secret stable IG-safe reference from immutable execution identity."""

    identity = "|".join((str(intent_id), str(global_cycle_id), epic, configuration_identity))
    return f"DQ01_{hashlib.sha256(identity.encode()).hexdigest()[:25]}"


def deterministic_close_reference(request: DemoExecutionRequest, deal_id: str) -> str:
    """Derive a separate safe close reference without using an account identifier."""

    identity = f"{request.deal_reference}|{deal_id}|CLOSE"
    return f"DQ01C_{hashlib.sha256(identity.encode()).hexdigest()[:24]}"


def request_to_ig_payload(request: DemoExecutionRequest) -> dict[str, object]:
    """Render exact IG v2 MARKET facts without inferred pip, currency, or price values."""

    payload: dict[str, object] = {
        "currencyCode": request.currency_code,
        "dealReference": request.deal_reference,
        "direction": request.direction.value,
        "epic": request.epic,
        "expiry": request.expiry,
        "forceOpen": request.force_open,
        "guaranteedStop": request.guaranteed_stop,
        "orderType": request.order_type.value,
        "size": _decimal_text(request.size),
    }
    _add_one_stop_representation(payload, request)
    _add_one_limit_representation(payload, request)
    return payload


def default_no_execution_authority() -> DemoAuthorityGate:
    """Explicit default: no execution, no account identity, and a blocking kill switch."""

    return DemoAuthorityGate(
        execution_mode=DemoExecutionMode.NO_EXECUTION,
        demo_order_authority=False,
        environment=None,
        expected_demo_account_id=None,
        authenticated_account_id=None,
        lease_valid=False,
        current_fencing_token=None,
        global_position_count=None,
        global_position_limit=None,
        approved_epics=APPROVED_DEMO_EPICS,
        kill_switch_state=KillSwitchState.BLOCKING,
    )


def _validate_request_shape(request: DemoExecutionRequest) -> None:
    if not all(
        isinstance(value, str) and value.strip()
        for value in (
            request.epic,
            request.expiry,
            request.currency_code,
            request.configuration_identity,
        )
    ):
        raise DemoExecutionError("Demo request identity is incomplete")
    if not isinstance(request.direction, DemoDirection) or not isinstance(
        request.order_type, DemoOrderType
    ):
        raise DemoExecutionError("Demo request order facts are invalid")
    if not re.fullmatch(r"[A-Z]{3}", request.currency_code):
        raise DemoExecutionError("Demo request currency is invalid")
    if not isinstance(request.force_open, bool) or not isinstance(request.guaranteed_stop, bool):
        raise DemoExecutionError("Demo request boolean facts are invalid")
    _positive_decimal(request.size, "Demo request size is invalid")
    _one_distance_or_level(request.stop_distance, request.stop_level, "stop")
    _one_distance_or_level(request.limit_distance, request.limit_level, "limit")
    if not DEAL_REFERENCE_PATTERN.fullmatch(request.deal_reference):
        raise DemoExecutionError("Demo deal reference is invalid")
    if (
        isinstance(request.fencing_token, bool)
        or not isinstance(request.fencing_token, int)
        or request.fencing_token <= 0
    ):
        raise DemoExecutionError("Demo request fencing token is invalid")
    _required_utc(request.created_at)


def _validate_metadata_for_request(
    metadata: DemoMarketMetadata | None,
    request: DemoExecutionRequest,
    *,
    now: datetime,
) -> None:
    if metadata is None:
        raise DemoExecutionError("market dealing metadata is unavailable")
    required = (
        metadata.instrument_currency,
        metadata.expiry,
        metadata.pip_scale,
        metadata.decimal_places,
        metadata.minimum_deal_size,
        metadata.minimum_stop_distance,
        metadata.guaranteed_stop_supported,
        metadata.market_status,
        metadata.observed_at,
    )
    if (
        not isinstance(metadata.epic, str)
        or not metadata.epic.strip()
        or any(value is None for value in required)
    ):
        raise DemoExecutionError("market dealing metadata is incomplete")
    if metadata.epic != request.epic:
        raise DemoExecutionError("market metadata epic does not match request")
    if metadata.instrument_currency != request.currency_code or metadata.expiry != request.expiry:
        raise DemoExecutionError("market metadata economics do not match request")
    if metadata.market_status != TRADEABLE_MARKET_STATUS:
        raise DemoExecutionError("market is not tradeable")
    observed_at = _required_utc(metadata.observed_at)
    now_utc = _required_utc(now)
    if observed_at > now_utc or now_utc - observed_at > DEFAULT_METADATA_MAX_AGE:
        raise DemoExecutionError("market dealing metadata is stale")
    pip_scale = _positive_decimal(metadata.pip_scale, "market pip scale is unavailable")
    del pip_scale
    if (
        isinstance(metadata.decimal_places, bool)
        or not isinstance(metadata.decimal_places, int)
        or not 0 <= metadata.decimal_places <= 12
    ):
        raise DemoExecutionError("market decimal precision is invalid")
    minimum_size = _positive_decimal(
        metadata.minimum_deal_size, "market minimum size is unavailable"
    )
    minimum_stop = _positive_decimal(
        metadata.minimum_stop_distance,
        "market minimum stop distance is unavailable",
    )
    if request.guaranteed_stop and metadata.guaranteed_stop_supported is not True:
        raise DemoExecutionError("guaranteed stop capability is unavailable")
    if request.size < minimum_size or not _fits_decimal_places(
        request.size, metadata.decimal_places
    ):
        raise DemoExecutionError("Demo request size violates market dealing metadata")
    if request.stop_distance is not None and request.stop_distance < minimum_stop:
        raise DemoExecutionError("Demo request stop distance violates market dealing metadata")


def _accepted_confirmation_matches(
    request: DemoExecutionRequest,
    confirmation: DemoConfirmation,
) -> bool:
    return (
        confirmation.deal_status is DemoConfirmationStatus.ACCEPTED
        and confirmation.deal_reference == request.deal_reference
        and bool(confirmation.deal_id)
        and isinstance(confirmation.status, str)
        and bool(confirmation.status.strip())
        and confirmation.epic == request.epic
        and confirmation.direction is request.direction
        and confirmation.size == request.size
        and _optional_confirmation_levels_are_valid(confirmation)
    )


def _close_confirmation_matches(
    record: DemoExecutionRecord,
    confirmation: DemoConfirmation,
) -> bool:
    position = record.position
    submission = record.close_submission
    return bool(
        position
        and submission
        and confirmation.deal_status is DemoConfirmationStatus.ACCEPTED
        and confirmation.deal_reference == submission.deal_reference
        and confirmation.deal_id == position.deal_id
        and isinstance(confirmation.status, str)
        and bool(confirmation.status.strip())
        and confirmation.epic == position.epic
        and confirmation.direction is position.direction.opposite
        and confirmation.size == position.size
        and _optional_confirmation_levels_are_valid(confirmation)
    )


def _matching_positions(
    positions: tuple[DemoPosition, ...],
    confirmation: DemoConfirmation,
    request: DemoExecutionRequest,
) -> tuple[DemoPosition, ...]:
    return tuple(
        position
        for position in positions
        if position.deal_id == confirmation.deal_id
        and position.epic == request.epic
        and position.direction is request.direction
        and position.size == request.size
    )


def _optional_confirmation_levels_are_valid(confirmation: DemoConfirmation) -> bool:
    try:
        for value in (confirmation.level, confirmation.stop_level, confirmation.limit_level):
            if value is not None:
                _positive_decimal(value, "confirmation price is invalid")
    except DemoExecutionError:
        return False
    return True


def _add_one_stop_representation(payload: dict[str, object], request: DemoExecutionRequest) -> None:
    if request.stop_distance is not None:
        payload["stopDistance"] = _decimal_text(request.stop_distance)
    elif request.stop_level is not None:
        payload["stopLevel"] = _decimal_text(request.stop_level)
    else:  # pragma: no cover - constructor validation makes this unreachable.
        raise DemoExecutionError("Demo request stop representation is missing")


def _add_one_limit_representation(
    payload: dict[str, object], request: DemoExecutionRequest
) -> None:
    if request.limit_distance is not None:
        payload["limitDistance"] = _decimal_text(request.limit_distance)
    elif request.limit_level is not None:
        payload["limitLevel"] = _decimal_text(request.limit_level)
    else:  # pragma: no cover - constructor validation makes this unreachable.
        raise DemoExecutionError("Demo request limit representation is missing")


def _one_distance_or_level(
    distance: Decimal | None,
    level: Decimal | None,
    label: str,
) -> None:
    if (distance is None) == (level is None):
        raise DemoExecutionError(f"Demo request requires exactly one {label} representation")
    _positive_decimal(
        distance if distance is not None else level, f"Demo request {label} is invalid"
    )


def _positive_decimal(value: object, error: str) -> Decimal:
    if not isinstance(value, Decimal):
        raise DemoExecutionError(error)
    if not value.is_finite() or value <= 0:
        raise DemoExecutionError(error)
    return value


def _fits_decimal_places(value: Decimal, decimal_places: int) -> bool:
    quantum = Decimal(1).scaleb(-decimal_places)
    try:
        return value.quantize(quantum) == value
    except InvalidOperation:
        return False


def _decimal_text(value: Decimal) -> str:
    return format(value, "f")


def _required_utc(value: datetime | None) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise DemoExecutionError("Demo timestamp is invalid")
    return value.astimezone(UTC)
