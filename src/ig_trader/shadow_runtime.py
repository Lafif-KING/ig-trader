"""Broker-neutral SHADOW_DEMO runtime orchestration with no order authority."""

from __future__ import annotations

import hashlib
import json
from collections import deque
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from math import isfinite
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
    """Bounded UTC finalized-candle history with no gap filling or replay."""

    def __init__(self, capacity: int) -> None:
        if isinstance(capacity, bool) or not isinstance(capacity, int) or capacity <= 0:
            raise ShadowRuntimeError("finalized candle capacity is invalid")
        self.capacity = capacity
        self._candles: dict[str, deque[FinalizedMinuteCandle]] = {}

    def append(
        self, candle: FinalizedMinuteCandle, *, now: datetime
    ) -> tuple[FinalizedMinuteCandle, ...]:
        candle.validate(now=now)
        finalized = replace(candle, timestamp=_utc(candle.timestamp))
        candles = self._candles.setdefault(finalized.epic, deque(maxlen=self.capacity))
        if candles and finalized.timestamp <= candles[-1].timestamp:
            if finalized.timestamp == candles[-1].timestamp:
                raise ShadowRuntimeError("finalized candle was already emitted")
            raise ShadowRuntimeError("finalized candle sequence is not strictly increasing")
        candles.append(finalized)
        return tuple(candles)

    def snapshot(self, epic: str) -> tuple[FinalizedMinuteCandle, ...]:
        return tuple(self._candles.get(epic, ()))


@dataclass(frozen=True)
class ShadowAccountState:
    account_id: str
    currency: str
    balance: float
    starting_balance: float
    captured_at: datetime
    state_known: bool = True


@dataclass(frozen=True)
class ShadowCycleEvidence:
    cycle_id: str
    fingerprint: str
    decision_code: str
    intent_id: UUID | None
    lifecycle: ShadowLifecycle | None
    observed_at: datetime


class ShadowEvidenceStore(Protocol):
    def record(self, evidence: ShadowCycleEvidence) -> ShadowCycleEvidence: ...


class InMemoryShadowEvidenceStore:
    """Disposable cycle ledger that rejects conflicting global decisions."""

    def __init__(self) -> None:
        self.records: dict[str, ShadowCycleEvidence] = {}

    def record(self, evidence: ShadowCycleEvidence) -> ShadowCycleEvidence:
        existing = self.records.get(evidence.cycle_id)
        if existing is not None and existing.fingerprint != evidence.fingerprint:
            raise ShadowRuntimeError("shadow cycle identity conflicts")
        self.records[evidence.cycle_id] = existing or evidence
        return self.records[evidence.cycle_id]


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
        self._portfolio_risk_permitted = False
        self._execution = ShadowExecutionCore(
            mode=ExecutionMode.SHADOW_DEMO,
            lease=lease,
            store=store,
            risk_gate=self._consume_portfolio_risk_permit,
            instruments=self.instruments,
            max_quote_age=self.max_quote_age,
        )
        self._candles = FinalizedCandleBuffer(self.config.warmup_candles)

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
        cycle_id: str,
        quote: ShadowMarketQuote,
        candle: FinalizedMinuteCandle,
        account: ShadowAccountState,
        now: datetime,
    ) -> ShadowRuntimeResult:
        if not cycle_id.strip() or quote.epic != candle.epic:
            raise ShadowRuntimeError("shadow cycle input is invalid")
        metadata = self.instruments.require(quote.epic)
        quote.validate(now=now, pip_size=metadata.pip_size, max_age=self.max_quote_age)
        candles = self._candles.append(candle, now=now)
        if len(candles) < self.config.warmup_candles:
            return self._record(cycle_id, "WARMUP_INCOMPLETE", None, now, {"candles": len(candles)})

        produced = self.strategy.generate_signal(quote.epic, _frame(candles))
        direction = getattr(produced.direction, "value", produced.direction)
        if direction not in {"BUY", "SELL"}:
            return self._record(cycle_id, "S0_WAIT", None, now, {"signal": "WAIT"})
        atr = (produced.metadata or {}).get("atr")
        if isinstance(atr, bool) or not isinstance(atr, int | float) or not _positive(atr):
            return self._record(cycle_id, "ATR_UNKNOWN", None, now, {"signal": direction})
        stop_pips = float(atr) / metadata.pip_size * self.config.stop_atr_multiplier
        target_pips = stop_pips * self.config.reward_to_risk
        if stop_pips > self.config.maximum_stop_pips:
            return self._record(cycle_id, "MAXIMUM_STOP_EXCEEDED", None, now, {"signal": direction})
        spread_pips = (quote.offer - quote.bid) / metadata.pip_size
        if spread_pips > self.config.maximum_spread_pips:
            return self._record(
                cycle_id, "MAXIMUM_SPREAD_EXCEEDED", None, now, {"signal": direction}
            )
        if spread_pips / target_pips > self.config.maximum_spread_to_target_ratio:
            return self._record(
                cycle_id, "SPREAD_TARGET_RATIO_EXCEEDED", None, now, {"signal": direction}
            )

        entry = quote.offer if direction == "BUY" else quote.bid
        distance = stop_pips * metadata.pip_size
        target_distance = target_pips * metadata.pip_size
        stop = entry - distance if direction == "BUY" else entry + distance
        target = entry + target_distance if direction == "BUY" else entry - target_distance
        intent_id = uuid5(
            NAMESPACE_URL, f"shadow-runtime:{cycle_id}:{self.config.configuration_hash}"
        )
        existing = self.store.get(intent_id)
        if existing is not None:
            self._require_matching_intent(
                existing, produced, quote.epic, direction, entry, stop, target
            )
            evidence = self._record(
                cycle_id,
                "PORTFOLIO_RISK_ALLOWED",
                existing.intent_id,
                now,
                {"direction": direction, "entry": entry, "stop": stop, "target": target},
            ).evidence
            return ShadowRuntimeResult(cycle_id, "DUPLICATE_CYCLE", existing, evidence)

        active_positions = self.store.active_position_count()
        if not isinstance(active_positions, int) or active_positions < 0:
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
            stop_pips=stop_pips,
        )
        if not decision.allowed:
            return self._record(cycle_id, decision.code, None, now, {"signal": direction})
        if not isinstance(normalized_account, AccountSnapshot) or decision.target_pips is None:
            raise ShadowRuntimeError("approved shadow risk decision is incomplete")
        pre_evidence = self._record(
            cycle_id,
            "PORTFOLIO_RISK_ALLOWED",
            intent_id,
            now,
            {"direction": direction, "entry": entry, "stop": stop, "target": target},
        ).evidence
        self._portfolio_risk_permitted = True
        try:
            record = self._execution.create_intent(
                produced,
                MarketQuote(quote.bid, quote.offer, quote.timestamp),
                intent_id=intent_id,
                stop_price=stop,
                target_price=target,
                open_positions_for_strategy=active_positions,
                daily_loss_pct=normalized_account.daily_loss_pct or 0.0,
                now=now,
            )
        finally:
            self._portfolio_risk_permitted = False
        evidence = ShadowCycleEvidence(
            pre_evidence.cycle_id,
            pre_evidence.fingerprint,
            pre_evidence.decision_code,
            record.intent_id,
            record.lifecycle,
            _utc(now),
        )
        self.evidence.record(evidence)
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
        market_quote = MarketQuote(quote.bid, quote.offer, quote.timestamp)
        if record.lifecycle is ShadowLifecycle.SHADOW_INTENT_CREATED:
            return self._execution.open_intent(record, now=now)
        if record.lifecycle is ShadowLifecycle.OPEN:
            return self._execution.close_on_quote(record, market_quote, now=now)
        if record.lifecycle is ShadowLifecycle.CLOSED:
            return self._execution.reconcile(record, now=now)
        return record

    def _consume_portfolio_risk_permit(self, *_args: object, **_kwargs: object) -> bool:
        """Permit exactly the synchronous core call following a risk approval."""

        return self._portfolio_risk_permitted

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
        intent_id: UUID | None,
        now: datetime,
        details: Mapping[str, object],
    ) -> ShadowRuntimeResult:
        evidence = ShadowCycleEvidence(
            cycle_id,
            _fingerprint({"cycle": cycle_id, "code": code, "details": details}),
            code,
            intent_id,
            None,
            _utc(now),
        )
        evidence = self.evidence.record(evidence)
        return ShadowRuntimeResult(cycle_id, code, None, evidence)


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
