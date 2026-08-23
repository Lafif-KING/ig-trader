"""Broker-neutral SHADOW_DEMO runtime orchestration with no order authority."""

from __future__ import annotations

import hashlib
import json
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime, timedelta
from math import isfinite
from threading import Lock
from typing import Any, Protocol
from uuid import NAMESPACE_URL, UUID, uuid5

import pandas as pd

from src.ig_trader.frozen_v1_policy import FrozenV1Config, PortfolioRisk
from src.ig_trader.offline_paper.domain import AccountSnapshot, Quote, Side, Signal, TradeCandidate
from src.ig_trader.shadow_execution import (
    ExecutionMode,
    InstrumentRegistry,
    MarketQuote,
    ShadowExecutionCore,
    ShadowExecutionError,
    ShadowIntentRecord,
    ShadowLifecycle,
    ShadowPerformance,
    ShadowStore,
)
from src.ig_trader.strategies.scalper import ScalperStrategy


class ShadowRuntimeError(ShadowExecutionError):
    """A sanitized SHADOW_DEMO runtime rejection."""


@dataclass(frozen=True)
class ShadowMarketQuote:
    epic: str
    bid: float
    offer: float
    timestamp: datetime
    pip_value_account_currency: float
    minimum_size: float
    minimum_stop_pips: float

    def validate(self, *, now: datetime, pip_size: float, max_age: timedelta) -> None:
        now_utc = _utc(now)
        timestamp = _utc(self.timestamp)
        values = (
            self.bid,
            self.offer,
            pip_size,
            self.pip_value_account_currency,
            self.minimum_size,
            self.minimum_stop_pips,
        )
        if (
            not self.epic.strip()
            or any(not _positive(value) for value in values)
            or self.offer <= self.bid
            or timestamp > now_utc
            or now_utc - timestamp > max_age
        ):
            raise ShadowRuntimeError("market quote is missing, stale, future, or crossed")


@dataclass(frozen=True)
class FinalizedMinuteCandle:
    epic: str
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0

    def validate(self, *, now: datetime) -> None:
        timestamp = _utc(self.timestamp)
        if (
            not self.epic.strip()
            or timestamp.second != 0
            or timestamp.microsecond != 0
            or timestamp >= _utc(now)
            or not all(_positive(value) for value in (self.open, self.high, self.low, self.close))
            or self.low > min(self.open, self.close)
            or self.high < max(self.open, self.close)
            or not isfinite(self.volume)
            or self.volume < 0
        ):
            raise ShadowRuntimeError("finalized one-minute candle is invalid")


class FinalizedCandleBuffer:
    """Bounded contiguous UTC candle history with no synthetic gap filling."""

    def __init__(self, capacity: int) -> None:
        if isinstance(capacity, bool) or not isinstance(capacity, int) or capacity <= 0:
            raise ShadowRuntimeError("finalized candle capacity is invalid")
        self.capacity = capacity
        self._candles: dict[str, deque[FinalizedMinuteCandle]] = {}
        self._lock = Lock()

    def append(self, candle: FinalizedMinuteCandle, *, now: datetime) -> CandleAppendResult:
        candle.validate(now=now)
        finalized = replace(candle, timestamp=_utc(candle.timestamp))
        with self._lock:
            candles = self._candles.setdefault(finalized.epic, deque(maxlen=self.capacity))
            if candles:
                delta = finalized.timestamp - candles[-1].timestamp
                if delta <= timedelta(0):
                    if delta == timedelta(0):
                        raise ShadowRuntimeError("finalized candle was already emitted")
                    raise ShadowRuntimeError("finalized candle sequence is not strictly increasing")
                if delta != timedelta(minutes=1):
                    candles.clear()
                    candles.append(finalized)
                    return CandleAppendResult(tuple(candles), gap_reset=True)
            candles.append(finalized)
            return CandleAppendResult(tuple(candles), gap_reset=False)

    def snapshot(self, epic: str) -> tuple[FinalizedMinuteCandle, ...]:
        with self._lock:
            return tuple(self._candles.get(epic, ()))


@dataclass(frozen=True)
class CandleAppendResult:
    candles: tuple[FinalizedMinuteCandle, ...]
    gap_reset: bool


@dataclass(frozen=True)
class ShadowAccountState:
    account_id: str
    currency: str
    balance: float
    starting_balance: float
    captured_at: datetime
    state_known: bool = True


@dataclass(frozen=True)
class ShadowRiskEvidence:
    """Sanitized risk facts for a single Shadow candidate evaluation."""

    allowed: bool | None
    code: str
    current_positions: int | None
    projected_positions: int | None
    daily_loss_pct: float | None
    effective_stop_pips: float | None
    target_pips: float | None
    spread_pips: float | None
    spread_to_target_ratio: float | None
    hypothetical_size: float | None
    monetary_risk: float | None

    def __post_init__(self) -> None:
        if not isinstance(self.code, str) or not self.code:
            raise ShadowRuntimeError("shadow risk evidence is invalid")
        if self.allowed is not None and not isinstance(self.allowed, bool):
            raise ShadowRuntimeError("shadow risk evidence is invalid")
        for value in (self.current_positions, self.projected_positions):
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, int) or value < 0
            ):
                raise ShadowRuntimeError("shadow risk evidence is invalid")
        for value in (
            self.daily_loss_pct,
            self.effective_stop_pips,
            self.target_pips,
            self.spread_pips,
            self.spread_to_target_ratio,
            self.hypothetical_size,
            self.monetary_risk,
        ):
            if value is not None and (
                isinstance(value, bool)
                or not isinstance(value, int | float)
                or not isfinite(float(value))
            ):
                raise ShadowRuntimeError("shadow risk evidence is invalid")


@dataclass(frozen=True)
class ShadowCycleEvidence:
    cycle_id: str
    configuration_identity: str
    fingerprint: str
    decision_code: str
    portfolio_risk_code: str
    intent_id: UUID | None
    lifecycle: ShadowLifecycle | None
    observed_at: datetime
    risk: ShadowRiskEvidence | None = None
    performance: ShadowPerformance | None = None
    authorized: bool = False
    order_authority: bool = False
    broker_order_call_count: int = 0

    def __post_init__(self) -> None:
        if (
            not self.cycle_id.strip()
            or not self.configuration_identity
            or not self.fingerprint
            or not self.decision_code
            or not self.portfolio_risk_code
            or self.authorized
            or self.order_authority
            or self.broker_order_call_count != 0
        ):
            raise ShadowRuntimeError("shadow evidence is invalid")
        _utc(self.observed_at)
        if self.lifecycle in {ShadowLifecycle.CLOSED, ShadowLifecycle.RECONCILED}:
            if self.performance is None:
                raise ShadowRuntimeError("closed shadow evidence requires performance")
        elif self.performance is not None:
            raise ShadowRuntimeError("unfinished shadow evidence cannot contain performance")


class ShadowEvidenceStore(Protocol):
    """Evidence ledger contract.

    Production SHADOW_DEMO must use a durable evidence implementation.  The
    in-memory implementation below is test-only; Cloud V2 will compose the
    durable implementation with lifecycle persistence.
    """

    def record(self, evidence: ShadowCycleEvidence) -> ShadowCycleEvidence: ...

    def by_intent(self, intent_id: UUID) -> ShadowCycleEvidence | None: ...


class InMemoryShadowEvidenceStore:
    """Test-only disposable ledger that rejects conflicting global decisions."""

    def __init__(self) -> None:
        self.records: dict[str, ShadowCycleEvidence] = {}
        self._intent_cycles: dict[UUID, str] = {}
        self._lock = Lock()

    def record(self, evidence: ShadowCycleEvidence) -> ShadowCycleEvidence:
        with self._lock:
            existing = self.records.get(evidence.cycle_id)
            if existing is not None:
                _validate_evidence_update(existing, evidence)
            if evidence.intent_id is not None:
                cycle = self._intent_cycles.get(evidence.intent_id)
                if cycle is not None and cycle != evidence.cycle_id:
                    raise ShadowRuntimeError("shadow evidence intent identity conflicts")
                self._intent_cycles[evidence.intent_id] = evidence.cycle_id
            self.records[evidence.cycle_id] = evidence
            return evidence

    def by_intent(self, intent_id: UUID) -> ShadowCycleEvidence | None:
        with self._lock:
            cycle_id = self._intent_cycles.get(intent_id)
            return self.records.get(cycle_id) if cycle_id is not None else None


class OneShotRiskPermit:
    """Evaluation-local authorization that the core can consume exactly once."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._consumed = False

    @property
    def consumed(self) -> bool:
        with self._lock:
            return self._consumed

    def consume(self, *_args: object, **_kwargs: object) -> bool:
        with self._lock:
            if self._consumed:
                return False
            self._consumed = True
            return True


@dataclass(frozen=True)
class ShadowRuntimeResult:
    cycle_id: str
    decision_code: str
    intent: ShadowIntentRecord | None
    evidence: ShadowCycleEvidence


@dataclass(frozen=True)
class _CountedPosition:
    epic: str


class ShadowRuntime:
    """Frozen S0 runtime that can persist hypothetical state but never submit orders."""

    def __init__(
        self,
        *,
        lease: Any,
        store: ShadowStore,
        evidence: ShadowEvidenceStore,
    ) -> None:
        self.config = FrozenV1Config()
        self.lease = lease
        self.store = store
        self.evidence = evidence
        self.instruments = InstrumentRegistry.frozen_v1()
        self.max_quote_age = timedelta(seconds=10)
        self.strategy = ScalperStrategy(
            rsi_period=self.config.rsi_period,
            minimum_confidence=self.config.confidence_threshold,
            adx_threshold=self.config.adx_threshold,
            minimum_candles=self.config.warmup_candles,
        )
        self.portfolio_risk = PortfolioRisk(self.config)
        self._candles = FinalizedCandleBuffer(self.config.warmup_candles)
        # A completed minute may be at most 90 seconds behind its validated
        # quote.  Retaining a stale contiguous window is conservative: only a
        # later validated candle/quote pair can resume evaluation.
        self.max_completed_candle_age = timedelta(seconds=90)

    @property
    def authorized(self) -> bool:
        return False

    @property
    def order_authority(self) -> bool:
        return False

    @property
    def broker_order_call_count(self) -> int:
        return 0

    def evaluate_cycle(
        self,
        *,
        quote: ShadowMarketQuote,
        candle: FinalizedMinuteCandle,
        account: object,
        now: datetime,
        cycle_id: str | None = None,
    ) -> ShadowRuntimeResult:
        if quote.epic != candle.epic:
            raise ShadowRuntimeError("shadow cycle input is invalid")
        derived_cycle_id = derive_global_cycle_id(
            candle.timestamp,
            self.config.shadow_configuration_hash,
        )
        if cycle_id is not None and cycle_id != derived_cycle_id:
            raise ShadowRuntimeError("caller cycle identity does not match finalized candle")
        metadata = self.instruments.require(quote.epic)
        quote.validate(now=now, pip_size=metadata.pip_size, max_age=self.max_quote_age)
        appended = self._candles.append(candle, now=now)
        cycle_id = derived_cycle_id
        if appended.gap_reset:
            return self._record(
                cycle_id,
                "CANDLE_GAP_WARMUP_RESET",
                "NOT_EVALUATED",
                None,
                now,
                {"candles": len(appended.candles)},
            )
        candles = appended.candles
        if len(candles) < self.config.warmup_candles:
            return self._record(
                cycle_id,
                "WARMUP_INCOMPLETE",
                "NOT_EVALUATED",
                None,
                now,
                {"candles": len(candles)},
            )

        newest_candle = candles[-1]
        if not _completed_candle_is_current(
            newest_candle,
            quote,
            max_age=self.max_completed_candle_age,
        ):
            return self._record(
                cycle_id,
                "CANDLE_WINDOW_STALE",
                "NOT_EVALUATED",
                None,
                now,
                {"candles": len(candles)},
            )

        try:
            produced = self.strategy.generate_signal(quote.epic, _frame(candles))
            direction = validate_s0_strategy_output(
                produced,
                quote=quote,
                newest_candle=newest_candle,
                pip_size=metadata.pip_size,
                actionable_confidence=self.config.confidence_threshold,
            )
        except Exception:
            return self._record(
                cycle_id,
                "STRATEGY_OUTPUT_INVALID",
                "NOT_EVALUATED",
                None,
                now,
                {"candles": len(candles)},
            )
        if direction not in {"BUY", "SELL"}:
            return self._record(cycle_id, "S0_WAIT", "NOT_EVALUATED", None, now, {"signal": "WAIT"})
        atr = produced.metadata["atr"]
        raw_stop_pips = float(atr) / metadata.pip_size * self.config.stop_atr_multiplier
        effective_stop_pips = max(raw_stop_pips, quote.minimum_stop_pips)
        target_pips = effective_stop_pips * self.config.reward_to_risk
        spread_pips = (quote.offer - quote.bid) / metadata.pip_size
        spread_to_target_ratio = spread_pips / target_pips
        pre_risk = _pre_risk_evidence(
            effective_stop_pips=effective_stop_pips,
            target_pips=target_pips,
            spread_pips=spread_pips,
            spread_to_target_ratio=spread_to_target_ratio,
        )
        if effective_stop_pips > self.config.maximum_stop_pips:
            return self._record(
                cycle_id,
                "MAXIMUM_STOP_EXCEEDED",
                "NOT_EVALUATED",
                None,
                now,
                {"signal": direction},
                risk=pre_risk,
            )
        if spread_pips > self.config.maximum_spread_pips:
            return self._record(
                cycle_id,
                "MAXIMUM_SPREAD_EXCEEDED",
                "NOT_EVALUATED",
                None,
                now,
                {"signal": direction},
                risk=pre_risk,
            )
        if spread_to_target_ratio > self.config.maximum_spread_to_target_ratio:
            return self._record(
                cycle_id,
                "SPREAD_TARGET_RATIO_EXCEEDED",
                "NOT_EVALUATED",
                None,
                now,
                {"signal": direction},
                risk=pre_risk,
            )

        entry = quote.offer if direction == "BUY" else quote.bid
        distance = effective_stop_pips * metadata.pip_size
        target_distance = target_pips * metadata.pip_size
        stop = entry - distance if direction == "BUY" else entry + distance
        target = entry + target_distance if direction == "BUY" else entry - target_distance
        intent_id = uuid5(
            NAMESPACE_URL, f"shadow-runtime:{cycle_id}:{self.config.shadow_configuration_hash}"
        )
        existing = self.store.get(intent_id)
        if existing is not None:
            self._require_matching_intent(
                existing, produced, quote.epic, direction, entry, stop, target
            )
            evidence = self._record_intent(
                cycle_id,
                existing,
                portfolio_risk_code="ALLOWED",
                now=now,
            )
            return ShadowRuntimeResult(cycle_id, "DUPLICATE_CYCLE", existing, evidence)

        active_positions = self.store.active_position_count()
        if (
            isinstance(active_positions, bool)
            or not isinstance(active_positions, int)
            or active_positions < 0
        ):
            raise ShadowRuntimeError("shadow active-position state is ambiguous")
        domain_quote = Quote(
            quote.epic,
            quote.bid,
            quote.offer,
            quote.timestamp,
            metadata.pip_size,
            quote.pip_value_account_currency,
            quote.minimum_size,
            quote.minimum_stop_pips,
        )
        candidate = TradeCandidate(
            uuid5(NAMESPACE_URL, f"shadow-candidate:{cycle_id}:{quote.epic}:{direction}").hex,
            cycle_id,
            Signal(
                quote.epic,
                Side(direction),
                produced.timestamp.to_pydatetime()
                if hasattr(produced.timestamp, "to_pydatetime")
                else produced.timestamp,
                float(produced.price),
                "S0",
                "Scalper:rsi-adx-v1",
                float(produced.confidence),
                dict(produced.metadata or {}),
            ),
            domain_quote,
            (),
            _fingerprint({"cycle": cycle_id, "epic": quote.epic, "candle": candle.timestamp}),
        )
        normalized_account = _account_snapshot(
            account,
            epic=quote.epic,
            active_positions=active_positions,
        )
        decision = self.portfolio_risk.evaluate(
            candidate,
            account=normalized_account,
            executions_in_cycle=0,
            stop_pips=effective_stop_pips,
        )
        risk_evidence = _risk_evidence(
            decision,
            effective_stop_pips=effective_stop_pips,
            target_pips=target_pips,
            spread_pips=spread_pips,
            spread_to_target_ratio=spread_to_target_ratio,
            current_positions=active_positions,
            daily_loss_pct=(
                normalized_account.daily_loss_pct
                if isinstance(normalized_account, AccountSnapshot)
                else None
            ),
        )
        if not decision.allowed:
            return self._record(
                cycle_id,
                decision.code,
                decision.code,
                None,
                now,
                {"signal": direction},
                risk=risk_evidence,
            )
        if not isinstance(normalized_account, AccountSnapshot) or decision.target_pips is None:
            raise ShadowRuntimeError("approved shadow risk decision is incomplete")
        permit = OneShotRiskPermit()
        record = self._new_execution_core(permit.consume).create_intent(
            produced,
            MarketQuote(quote.bid, quote.offer, quote.timestamp),
            intent_id=intent_id,
            stop_price=stop,
            target_price=target,
            open_positions_for_strategy=active_positions,
            daily_loss_pct=normalized_account.daily_loss_pct or 0.0,
            now=now,
        )
        evidence = self._record_intent(
            cycle_id,
            record,
            portfolio_risk_code=decision.code,
            risk=risk_evidence,
            now=now,
        )
        return ShadowRuntimeResult(cycle_id, decision.code, record, evidence)

    def advance(
        self, intent_id: UUID, quote: ShadowMarketQuote, *, now: datetime
    ) -> ShadowIntentRecord:
        metadata = self.instruments.require(quote.epic)
        quote.validate(now=now, pip_size=metadata.pip_size, max_age=self.max_quote_age)
        record = self.store.get(intent_id)
        if record is None:
            raise ShadowRuntimeError("shadow intent is unavailable")
        if record.instrument != quote.epic:
            raise ShadowRuntimeError("shadow position quote does not match its instrument")
        existing_evidence = self.evidence.by_intent(record.intent_id)
        if existing_evidence is None:
            raise ShadowRuntimeError("EVIDENCE_STATE_MISSING")
        # Prove the evidence ledger can accept a monotonic write before any
        # lifecycle mutation.  Production composition must make this durable.
        self.evidence.record(replace(existing_evidence, observed_at=_utc(now)))
        market_quote = MarketQuote(quote.bid, quote.offer, quote.timestamp)
        execution = self._new_execution_core(_deny_risk_gate)
        if record.lifecycle is ShadowLifecycle.SHADOW_INTENT_CREATED:
            updated = execution.open_intent(record, now=now)
        elif record.lifecycle is ShadowLifecycle.OPEN:
            updated = execution.close_on_quote(record, market_quote, now=now)
        elif record.lifecycle is ShadowLifecycle.CLOSED:
            updated = execution.reconcile(record, now=now)
        else:
            updated = record
        self._record_lifecycle(updated, existing=existing_evidence, now=now)
        return updated

    def _new_execution_core(self, risk_gate: Callable[..., bool]) -> ShadowExecutionCore:
        return ShadowExecutionCore(
            mode=ExecutionMode.SHADOW_DEMO,
            lease=self.lease,
            store=self.store,
            risk_gate=risk_gate,
            instruments=self.instruments,
            max_quote_age=self.max_quote_age,
        )

    @staticmethod
    def _require_matching_intent(
        existing: ShadowIntentRecord,
        signal: object,
        epic: str,
        direction: str,
        entry: float,
        stop: float,
        target: float,
    ) -> None:
        expected = (
            str(getattr(signal, "strategy_name", "")),
            epic,
            direction,
            entry,
            stop,
            target,
        )
        observed = (
            existing.strategy_id,
            existing.instrument,
            existing.direction,
            existing.entry_price,
            existing.stop_price,
            existing.target_price,
        )
        if observed != expected:
            raise ShadowExecutionError("duplicate shadow intent conflicts")

    def _record(
        self,
        cycle_id: str,
        code: str,
        portfolio_risk_code: str,
        intent_id: UUID | None,
        now: datetime,
        details: Mapping[str, object],
        *,
        risk: ShadowRiskEvidence | None = None,
    ) -> ShadowRuntimeResult:
        evidence = self.evidence.record(
            ShadowCycleEvidence(
                cycle_id=cycle_id,
                configuration_identity=self.config.shadow_configuration_hash,
                fingerprint=_fingerprint(
                    {
                        "cycle": cycle_id,
                        "configuration": self.config.shadow_configuration_hash,
                        "decision": code,
                        "portfolio_risk": portfolio_risk_code,
                        "details": details,
                        "risk": _risk_document(risk),
                    }
                ),
                decision_code=code,
                portfolio_risk_code=portfolio_risk_code,
                intent_id=intent_id,
                lifecycle=None,
                observed_at=_utc(now),
                risk=risk,
            )
        )
        return ShadowRuntimeResult(cycle_id, code, None, evidence)

    def _record_intent(
        self,
        cycle_id: str,
        record: ShadowIntentRecord,
        *,
        portfolio_risk_code: str,
        risk: ShadowRiskEvidence | None = None,
        now: datetime,
    ) -> ShadowCycleEvidence:
        existing = self.evidence.by_intent(record.intent_id)
        if existing is not None:
            return self.evidence.record(replace(existing, observed_at=_utc(now)))
        performance = self._performance_for(record)
        return self.evidence.record(
            ShadowCycleEvidence(
                cycle_id=cycle_id,
                configuration_identity=self.config.shadow_configuration_hash,
                fingerprint=_fingerprint(
                    {
                        "cycle": cycle_id,
                        "configuration": self.config.shadow_configuration_hash,
                        "intent_id": record.intent_id,
                        "strategy": record.strategy_id,
                        "instrument": record.instrument,
                        "direction": record.direction,
                        "entry": record.entry_price,
                        "stop": record.stop_price,
                        "target": record.target_price,
                        "risk": _risk_document(risk),
                    }
                ),
                decision_code="SHADOW_INTENT_CREATED",
                portfolio_risk_code=portfolio_risk_code,
                intent_id=record.intent_id,
                lifecycle=record.lifecycle,
                observed_at=_utc(now),
                risk=risk,
                performance=performance,
            )
        )

    def _record_lifecycle(
        self,
        record: ShadowIntentRecord,
        *,
        existing: ShadowCycleEvidence,
        now: datetime,
    ) -> None:
        self.evidence.record(
            replace(
                existing,
                lifecycle=record.lifecycle,
                observed_at=_utc(now),
                performance=self._performance_for(record),
            )
        )

    def _performance_for(self, record: ShadowIntentRecord) -> ShadowPerformance | None:
        if record.lifecycle not in {ShadowLifecycle.CLOSED, ShadowLifecycle.RECONCILED}:
            return None
        return self._new_execution_core(_deny_risk_gate).performance(record)


def _validate_evidence_update(
    existing: ShadowCycleEvidence,
    replacement: ShadowCycleEvidence,
) -> None:
    if (
        existing.configuration_identity != replacement.configuration_identity
        or existing.fingerprint != replacement.fingerprint
        or existing.decision_code != replacement.decision_code
        or existing.portfolio_risk_code != replacement.portfolio_risk_code
        or existing.intent_id != replacement.intent_id
        or existing.risk != replacement.risk
        or replacement.observed_at < existing.observed_at
    ):
        raise ShadowRuntimeError("shadow evidence identity conflicts")
    if existing.performance != replacement.performance:
        if existing.performance is not None or replacement.performance is None:
            raise ShadowRuntimeError("shadow evidence performance conflicts")
        if replacement.lifecycle not in {ShadowLifecycle.CLOSED, ShadowLifecycle.RECONCILED}:
            raise ShadowRuntimeError("shadow evidence performance conflicts")
    if existing.lifecycle is None:
        if replacement.lifecycle is not None:
            raise ShadowRuntimeError("shadow evidence lifecycle conflicts")
        return
    if replacement.lifecycle is None:
        raise ShadowRuntimeError("shadow evidence lifecycle regressed")
    allowed = {
        ShadowLifecycle.SHADOW_INTENT_CREATED: {ShadowLifecycle.OPEN},
        ShadowLifecycle.OPEN: {ShadowLifecycle.CLOSED, ShadowLifecycle.FAILED_SAFE},
        ShadowLifecycle.CLOSED: {ShadowLifecycle.RECONCILED},
    }
    if replacement.lifecycle is existing.lifecycle:
        return
    if replacement.lifecycle not in allowed.get(existing.lifecycle, set()):
        raise ShadowRuntimeError("shadow evidence lifecycle regressed")


def derive_global_cycle_id(
    finalized_candle_timestamp: datetime,
    shadow_configuration_hash: str,
) -> str:
    """Return an instrument-neutral UUID5 for one finalized UTC candle minute."""

    timestamp = _utc(finalized_candle_timestamp)
    if timestamp.second != 0 or timestamp.microsecond != 0 or not shadow_configuration_hash:
        raise ShadowRuntimeError("shadow cycle identity is invalid")
    return uuid5(
        NAMESPACE_URL,
        f"ig-trader-shadow-cycle:{shadow_configuration_hash}:{timestamp.isoformat()}",
    ).hex


def validate_s0_strategy_output(
    produced: object,
    *,
    quote: ShadowMarketQuote,
    newest_candle: FinalizedMinuteCandle,
    pip_size: float,
    actionable_confidence: float,
) -> str:
    """Validate untrusted S0 output without retaining invalid payload values.

    Price may differ from the newest close by at most one tenth of a pip.
    """

    epic = getattr(produced, "epic", None)
    strategy_name = getattr(produced, "strategy_name", None)
    timestamp = getattr(produced, "timestamp", None)
    price = getattr(produced, "price", None)
    confidence = getattr(produced, "confidence", None)
    metadata = getattr(produced, "metadata", None)
    direction = getattr(getattr(produced, "direction", None), "value", None)
    if direction is None:
        direction = getattr(produced, "direction", None)
    try:
        produced_timestamp = _utc(timestamp)
        candle_timestamp = _utc(newest_candle.timestamp)
    except (AttributeError, TypeError, ShadowRuntimeError):
        raise ShadowRuntimeError("strategy output is invalid") from None
    if (
        epic != quote.epic
        or epic != newest_candle.epic
        or strategy_name != "Scalper"
        or produced_timestamp != candle_timestamp
        or not _positive(price)
        or abs(float(price) - newest_candle.close) > float(pip_size) * 0.1
        or isinstance(confidence, bool)
        or not isinstance(confidence, int | float)
        or not isfinite(float(confidence))
        or not 0.0 <= float(confidence) <= 1.0
        or not isinstance(metadata, Mapping)
        or direction not in {"BUY", "SELL", "WAIT"}
    ):
        raise ShadowRuntimeError("strategy output is invalid")
    if direction in {"BUY", "SELL"}:
        atr = metadata.get("atr")
        if float(confidence) < actionable_confidence or not _positive(atr):
            raise ShadowRuntimeError("strategy output is invalid")
    return direction


def _completed_candle_is_current(
    candle: FinalizedMinuteCandle,
    quote: ShadowMarketQuote,
    *,
    max_age: timedelta,
) -> bool:
    candle_end = _utc(candle.timestamp) + timedelta(minutes=1)
    quote_timestamp = _utc(quote.timestamp)
    return quote_timestamp > candle_end and quote_timestamp - candle_end <= max_age


def _pre_risk_evidence(
    *,
    effective_stop_pips: float,
    target_pips: float,
    spread_pips: float,
    spread_to_target_ratio: float,
) -> ShadowRiskEvidence:
    return ShadowRiskEvidence(
        None,
        "NOT_EVALUATED",
        None,
        None,
        None,
        effective_stop_pips,
        target_pips,
        spread_pips,
        spread_to_target_ratio,
        None,
        None,
    )


def _risk_evidence(
    decision: object,
    *,
    effective_stop_pips: float,
    target_pips: float,
    spread_pips: float,
    spread_to_target_ratio: float,
    current_positions: int,
    daily_loss_pct: float | None,
) -> ShadowRiskEvidence:
    decision_current_positions = getattr(decision, "current_positions", None)
    decision_projected_positions = getattr(decision, "projected_positions", None)
    return ShadowRiskEvidence(
        getattr(decision, "allowed", None),
        str(getattr(decision, "code", "NOT_EVALUATED")),
        (
            decision_current_positions
            if decision_current_positions is not None
            else current_positions
        ),
        (
            decision_projected_positions
            if decision_projected_positions is not None
            else current_positions + 1
        ),
        (
            getattr(decision, "daily_loss_pct", None)
            if getattr(decision, "daily_loss_pct", None) is not None
            else daily_loss_pct
        ),
        effective_stop_pips,
        target_pips,
        spread_pips,
        spread_to_target_ratio,
        getattr(decision, "size", None),
        getattr(decision, "monetary_risk", None),
    )


def _risk_document(risk: ShadowRiskEvidence | None) -> Mapping[str, object] | None:
    return asdict(risk) if risk is not None else None


def _deny_risk_gate(*_args: object, **_kwargs: object) -> bool:
    """Lifecycle transitions never receive a trading-risk permit."""

    return False


def _frame(candles: Sequence[FinalizedMinuteCandle]) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "open": [item.open for item in candles],
            "high": [item.high for item in candles],
            "low": [item.low for item in candles],
            "close": [item.close for item in candles],
            "volume": [item.volume for item in candles],
        },
        index=pd.DatetimeIndex([item.timestamp for item in candles]),
    )


def _fingerprint(value: Mapping[str, object]) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ShadowRuntimeError("shadow runtime timestamp is invalid")
    return value.astimezone(UTC)


def _positive(value: object) -> bool:
    return (
        isinstance(value, int | float)
        and not isinstance(value, bool)
        and isfinite(value)
        and value > 0
    )


def _account_snapshot(
    account: object,
    *,
    epic: str,
    active_positions: int,
) -> AccountSnapshot | None:
    if not isinstance(account, ShadowAccountState):
        return None
    if (
        not isinstance(account.account_id, str)
        or not isinstance(account.currency, str)
        or not account.account_id.strip()
        or len(account.currency) != 3
        or not account.currency.isalpha()
        or account.currency != account.currency.upper()
        or not _positive(account.balance)
        or not _positive(account.starting_balance)
        or not isinstance(account.state_known, bool)
    ):
        return None
    try:
        captured_at = _utc(account.captured_at)
    except ShadowRuntimeError:
        return None
    return AccountSnapshot(
        account.account_id,
        account.currency,
        account.balance,
        account.starting_balance,
        tuple(_CountedPosition(epic) for _ in range(active_positions)),
        captured_at,
        account.state_known,
    )
