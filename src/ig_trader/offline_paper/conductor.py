"""Exact frozen Scalper conductor for broker-isolated OFFLINE_PAPER."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from enum import StrEnum
from math import floor, isfinite
from uuid import NAMESPACE_URL, uuid5

import pandas as pd

from src.ig_trader.models import SignalDirection
from src.ig_trader.offline_paper.domain import (
    BrokerOrder,
    ExecutionMode,
    Exit,
    LifecycleState,
    RiskDecision,
    RunResult,
    RunStatus,
    Side,
    Signal,
    TradeCandidate,
    TradeIntent,
)
from src.ig_trader.offline_paper.fixture import FROZEN_INSTRUMENTS, LocalFixtureData
from src.ig_trader.offline_paper.paper_broker import PaperBroker
from src.ig_trader.offline_paper.persistence import TradeIntentStore
from src.ig_trader.offline_paper.ports import (
    AccountPort,
    ExecutionPort,
    HistoricalDataPort,
    MarketDataPort,
    ReconciliationPort,
)
from src.ig_trader.strategies.scalper import ScalperStrategy


class FaultPoint(StrEnum):
    BEFORE_TRADE_INTENT = "BEFORE_TRADE_INTENT"
    AFTER_INTENT_CREATED = "AFTER_INTENT_CREATED"
    AFTER_ORDER_SUBMITTED = "AFTER_ORDER_SUBMITTED"
    AFTER_PAPER_SUBMISSION = "AFTER_PAPER_SUBMISSION"
    AFTER_ORDER_ACCEPTED = "AFTER_ORDER_ACCEPTED"
    AFTER_POSITION_OPEN = "AFTER_POSITION_OPEN"
    AFTER_EXIT_REQUESTED = "AFTER_EXIT_REQUESTED"
    AFTER_PAPER_CLOSE = "AFTER_PAPER_CLOSE"
    AFTER_POSITION_CLOSED = "AFTER_POSITION_CLOSED"


class InjectedCrash(RuntimeError):
    """Test-only abrupt stop used to prove restart recovery."""


@dataclass(frozen=True)
class FrozenV1Config:
    rsi_period: int = 7
    confidence_threshold: float = 0.70
    adx_threshold: float = 20.0
    warmup_candles: int = 60
    stop_atr_multiplier: float = 2.0
    reward_to_risk: float = 1.5
    maximum_stop_pips: float = 12.0
    maximum_spread_pips: float = 1.2
    maximum_spread_to_target_ratio: float = 0.15
    maximum_total_positions: int = 1
    maximum_positions_per_instrument: int = 1
    maximum_executions_per_cycle: int = 1
    scalper_budget_fraction: float = 0.30
    scalper_risk_fraction: float = 0.005
    maximum_daily_loss_fraction: float = 0.05

    def __post_init__(self) -> None:
        if asdict(self) != asdict(FrozenV1Config.__new_defaults__()):
            raise ValueError("frozen V1 configuration cannot be changed")

    @classmethod
    def __new_defaults__(cls) -> FrozenV1Config:
        value = object.__new__(cls)
        for name, field in cls.__dataclass_fields__.items():
            object.__setattr__(value, name, field.default)
        return value

    @property
    def configuration_hash(self) -> str:
        document = {
            "parameters": asdict(self),
            "instruments": FROZEN_INSTRUMENTS,
            "strategy": "Scalper:rsi-adx-v1",
            "execution_mode": ExecutionMode.OFFLINE_PAPER.value,
            "ai_trading_authority": False,
            "strategy_optimization": False,
            "advanced_management": False,
            "autonomous_intraday_authority": False,
        }
        return hashlib.sha256(_encode(document).encode()).hexdigest()


class PortfolioRisk:
    """Absolute-veto portfolio policy with explicit current state."""

    def __init__(self, config: FrozenV1Config) -> None:
        self.config = config

    def evaluate(
        self,
        candidate: TradeCandidate,
        *,
        account: object,
        executions_in_cycle: int,
        stop_pips: float,
    ) -> RiskDecision:
        from src.ig_trader.offline_paper.domain import AccountSnapshot

        if not isinstance(account, AccountSnapshot) or not account.state_known:
            return _risk_block("ACCOUNT_STATE_UNKNOWN")
        if account.captured_at != candidate.quote.timestamp:
            return _risk_block("ACCOUNT_STATE_STALE")
        daily_loss = account.daily_loss_pct
        if daily_loss is None:
            return _risk_block("DAILY_RISK_UNKNOWN")
        if daily_loss <= -self.config.maximum_daily_loss_fraction:
            return _risk_block("DAILY_LOSS_LIMIT")
        if executions_in_cycle < 0:
            return _risk_block("CYCLE_EXECUTION_STATE_UNKNOWN")
        if executions_in_cycle >= self.config.maximum_executions_per_cycle:
            return _risk_block("CYCLE_EXECUTION_LIMIT")
        if len(account.positions) >= self.config.maximum_total_positions:
            return _risk_block("TOTAL_POSITION_LIMIT")
        same_epic = sum(position.epic == candidate.signal.epic for position in account.positions)
        if same_epic >= self.config.maximum_positions_per_instrument:
            return _risk_block("INSTRUMENT_POSITION_LIMIT")
        if not isfinite(stop_pips) or stop_pips <= 0:
            return _risk_block("STOP_STATE_UNKNOWN")
        monetary_risk = (
            account.balance
            * self.config.scalper_budget_fraction
            * self.config.scalper_risk_fraction
        )
        raw_size = monetary_risk / (stop_pips * candidate.quote.pip_value_account_currency)
        size = floor(raw_size * 100.0) / 100.0
        if not isfinite(size) or size < candidate.quote.minimum_size:
            return _risk_block("POSITION_SIZE_BELOW_MINIMUM")
        return RiskDecision(
            True,
            "ALLOWED",
            account.balance,
            daily_loss,
            len(account.positions),
            len(account.positions) + 1,
            executions_in_cycle,
            monetary_risk,
            size,
            stop_pips,
            stop_pips * self.config.reward_to_risk,
        )


class OfflinePaperConductor:
    """Run and recover one exact frozen paper lifecycle with no broker client."""

    def __init__(
        self,
        *,
        market_data: MarketDataPort,
        historical_data: HistoricalDataPort,
        source: LocalFixtureData,
        execution: ExecutionPort,
        account: AccountPort,
        reconciliation: ReconciliationPort,
        intents: TradeIntentStore,
        config: FrozenV1Config | None = None,
        fault_hook: Callable[[FaultPoint], None] | None = None,
    ) -> None:
        if type(execution) is not PaperBroker:
            raise TypeError("OFFLINE_PAPER ExecutionPort must be PaperBroker")
        if account is not execution or reconciliation is not execution:
            raise TypeError("paper execution/account/reconciliation must share one authority")
        if not isinstance(market_data, MarketDataPort) or not isinstance(
            historical_data, HistoricalDataPort
        ):
            raise TypeError("broker-neutral data ports are required")
        self.market_data = market_data
        self.historical_data = historical_data
        self.source = source
        self.broker = execution
        self.intents = intents
        self.config = config or FrozenV1Config()
        self.strategy = ScalperStrategy(
            rsi_period=self.config.rsi_period,
            minimum_confidence=self.config.confidence_threshold,
            adx_threshold=self.config.adx_threshold,
            minimum_candles=self.config.warmup_candles,
        )
        self.portfolio_risk = PortfolioRisk(self.config)
        self.fault_hook = fault_hook

    def run(self) -> RunResult:
        time = self.source.evaluation_time
        cycle_id = self.source.cycle_id
        if not self.intents.initialize_run(
            cycle_id=cycle_id,
            input_fingerprint=self.source.document_fingerprint,
            configuration_hash=self.config.configuration_hash,
            occurred_at=time,
        ):
            return self._blocked("RUN_CONFIGURATION_OR_STATE_CONFLICT")
        current = self.intents.intents()
        snapshot = self.broker.reconciliation_snapshot(as_of=time)
        if current is None or snapshot is None:
            return self._blocked("PERSISTED_STATE_UNKNOWN")
        if len(current) > 1:
            return self._blocked("MULTIPLE_INTENTS_AMBIGUOUS")
        if not current and (
            snapshot.orders or snapshot.fills or snapshot.exits or snapshot.account.positions
        ):
            return self._blocked("ORPHAN_PAPER_BROKER_STATE")
        if current:
            intent = current[0]
            if not self._intent_matches_source(intent):
                return self._blocked("MISMATCHED_INTENT")
            if intent.lifecycle_state is LifecycleState.FAILED_SAFE:
                return self._blocked("FAILED_SAFE_REQUIRES_RECONCILIATION")
            if intent.lifecycle_state is LifecycleState.RECONCILED:
                return self._completed(intent, idempotent=True)
            return self._resume(intent.intent_id)

        candidates = self._evaluate_cycle()
        if candidates is None:
            return self._blocked("MARKET_OR_STRATEGY_STATE_UNKNOWN")
        if not candidates:
            return RunResult(
                RunStatus.NO_TRADE,
                "NO_SCALPER_CANDIDATE",
                cycle_id,
                (),
                (),
                "NOT_EVALUATED",
                "NOT_SUBMITTED",
                "NO_POSITION",
                False,
            )
        ranked = tuple(
            sorted(candidates, key=lambda item: (-item.signal.confidence, item.signal.epic))
        )
        selected = ranked[0]
        if not self.intents.append_lineage(
            cycle_id=cycle_id,
            identity=cycle_id,
            phase="RANKING",
            payload={
                "candidate_ids": [item.candidate_id for item in ranked],
                "selected_candidate_id": selected.candidate_id,
                "maximum_execution_per_cycle": 1,
            },
            occurred_at=time,
        ):
            return self._blocked("RANKING_EVIDENCE_FAILURE")
        decision = self._risk(selected)
        if not self.intents.record_risk_decision(
            cycle_id=cycle_id,
            candidate_id=selected.candidate_id,
            decision=decision,
            occurred_at=time,
        ) or not self.intents.append_lineage(
            cycle_id=cycle_id,
            identity=selected.candidate_id,
            phase="PORTFOLIO_RISK",
            payload=asdict(decision),
            occurred_at=time,
        ):
            return self._blocked("RISK_EVIDENCE_FAILURE")
        if not decision.allowed:
            return RunResult(
                RunStatus.NO_TRADE,
                decision.code,
                cycle_id,
                (),
                (LifecycleState.RISK_REJECTED,),
                decision.code,
                "NOT_SUBMITTED",
                "NO_POSITION",
                False,
            )
        if decision.size is None or decision.stop_pips is None or decision.target_pips is None:
            return self._blocked("ALLOWED_RISK_DECISION_INCOMPLETE")
        entry = selected.quote.offer if selected.signal.side is Side.BUY else selected.quote.bid
        distance = decision.stop_pips * selected.quote.pip_size
        target_distance = decision.target_pips * selected.quote.pip_size
        stop = entry - distance if selected.signal.side is Side.BUY else entry + distance
        target = (
            entry + target_distance if selected.signal.side is Side.BUY else entry - target_distance
        )
        if not self.intents.append_lineage(
            cycle_id=cycle_id,
            identity=selected.candidate_id,
            phase="POSITION_SIZING",
            payload={
                "size": decision.size,
                "entry": entry,
                "stop_level": stop,
                "target_level": target,
                "monetary_risk": decision.monetary_risk,
            },
            occurred_at=time,
        ):
            return self._blocked("SIZING_EVIDENCE_FAILURE")
        self._fault(FaultPoint.BEFORE_TRADE_INTENT)
        intent_id = uuid5(
            NAMESPACE_URL,
            f"{cycle_id}:{selected.candidate_id}:{self.config.configuration_hash}",
        ).hex
        intent = TradeIntent(
            intent_id,
            time,
            cycle_id,
            selected.candidate_id,
            selected.signal.epic,
            selected.signal.side,
            selected.signal.strategy,
            selected.signal.strategy_version,
            {
                **selected.signal.inputs,
                "reference_price": selected.signal.reference_price,
                "signal_timestamp": selected.signal.timestamp.isoformat(),
                "requested_entry": entry,
            },
            selected.signal.confidence,
            selected.quote.spread_pips,
            decision,
            decision.size,
            stop,
            target,
            selected.source_candle_references,
            selected.source_fingerprint,
            ExecutionMode.OFFLINE_PAPER,
            LifecycleState.SIGNAL_DETECTED,
        )
        if not self.intents.create_intent(intent) or not self.intents.append_lineage(
            cycle_id=cycle_id,
            identity=intent_id,
            phase="TRADE_INTENT",
            payload={
                "intent_id": intent_id,
                "candidate_id": selected.candidate_id,
                "persisted_before_execution": True,
                "execution_mode": ExecutionMode.OFFLINE_PAPER.value,
            },
            occurred_at=time,
        ):
            return self._blocked("TRADE_INTENT_PERSISTENCE_FAILURE")
        if not self.intents.transition(
            intent_id,
            LifecycleState.INTENT_CREATED,
            reason="PERSISTENT_INTENT_COMPLETE",
            occurred_at=time,
        ):
            return self._fail_safe(intent_id, "INTENT_TRANSITION_FAILURE")
        self._fault(FaultPoint.AFTER_INTENT_CREATED)
        return self._resume(intent_id)

    def _evaluate_cycle(self) -> tuple[TradeCandidate, ...] | None:
        result = []
        for _, epic, _, _ in FROZEN_INSTRUMENTS:
            candles = self.historical_data.candles(epic, before=self.source.evaluation_time)
            quote = self.market_data.quote(epic, as_of=self.source.evaluation_time)
            references = self.source.source_references(epic)
            if candles is None or quote is None or len(candles) != self.config.warmup_candles:
                return None
            frame = pd.DataFrame(
                {
                    "open": [item.open for item in candles],
                    "high": [item.high for item in candles],
                    "low": [item.low for item in candles],
                    "close": [item.close for item in candles],
                    "volume": [item.volume for item in candles],
                },
                index=pd.DatetimeIndex([item.timestamp for item in candles]),
            )
            if not self.intents.append_lineage(
                cycle_id=self.source.cycle_id,
                identity=epic,
                phase="MARKET_CANDLE_INPUT",
                payload={"quote": _quote_document(quote), "source_references": references},
                occurred_at=self.source.evaluation_time,
            ):
                return None
            produced = self.strategy.generate_signal(epic, frame)
            if (
                produced.timestamp.tzinfo is None
                or produced.timestamp >= self.source.evaluation_time
            ):
                return None
            side = (
                Side(produced.direction.value)
                if produced.direction in {SignalDirection.BUY, SignalDirection.SELL}
                else None
            )
            inputs = {
                key: float(value) if isinstance(value, int | float) else value
                for key, value in (produced.metadata or {}).items()
            }
            signal = Signal(
                epic,
                side,
                produced.timestamp.to_pydatetime()
                if hasattr(produced.timestamp, "to_pydatetime")
                else produced.timestamp,
                float(produced.price),
                "Scalper",
                "Scalper:rsi-adx-v1",
                float(produced.confidence),
                inputs,
            )
            if not self.intents.append_lineage(
                cycle_id=self.source.cycle_id,
                identity=epic,
                phase="STRATEGY_CALCULATION",
                payload={
                    "strategy": signal.strategy,
                    "strategy_version": signal.strategy_version,
                    "inputs": signal.inputs,
                    "frozen_parameters": {
                        "rsi_period": 7,
                        "confidence_threshold": 0.70,
                        "adx_threshold": 20,
                        "warmup_candles": 60,
                    },
                },
                occurred_at=self.source.evaluation_time,
            ) or not self.intents.append_lineage(
                cycle_id=self.source.cycle_id,
                identity=epic,
                phase="SIGNAL",
                payload={
                    "side": signal.side.value if signal.side else "WAIT",
                    "confidence": signal.confidence,
                    "reference_price": signal.reference_price,
                },
                occurred_at=self.source.evaluation_time,
            ):
                return None
            if signal.side is None:
                continue
            source_fingerprint = hashlib.sha256(
                _encode({"epic": epic, "references": references}).encode()
            ).hexdigest()
            candidate_id = uuid5(
                NAMESPACE_URL,
                f"{self.source.cycle_id}:{epic}:{signal.side.value}:{source_fingerprint}",
            ).hex
            candidate = TradeCandidate(
                candidate_id,
                self.source.cycle_id,
                signal,
                quote,
                references,
                source_fingerprint,
            )
            if not self.intents.append_lineage(
                cycle_id=self.source.cycle_id,
                identity=candidate_id,
                phase="CANDIDATE",
                payload={
                    "candidate_id": candidate_id,
                    "epic": epic,
                    "side": signal.side.value,
                    "confidence": signal.confidence,
                },
                occurred_at=self.source.evaluation_time,
            ):
                return None
            result.append(candidate)
        return tuple(result)

    def _risk(self, candidate: TradeCandidate) -> RiskDecision:
        atr = candidate.signal.inputs.get("atr")
        if isinstance(atr, bool) or not isinstance(atr, int | float) or not isfinite(atr):
            return _risk_block("ATR_UNKNOWN")
        raw_stop = float(atr) / candidate.quote.pip_size * self.config.stop_atr_multiplier
        stop_pips = max(raw_stop, candidate.quote.minimum_stop_pips)
        target_pips = stop_pips * self.config.reward_to_risk
        if stop_pips > self.config.maximum_stop_pips:
            return _risk_block("MAXIMUM_STOP_EXCEEDED")
        if candidate.quote.spread_pips > self.config.maximum_spread_pips:
            return _risk_block("MAXIMUM_SPREAD_EXCEEDED")
        if candidate.quote.spread_pips / target_pips > self.config.maximum_spread_to_target_ratio:
            return _risk_block("SPREAD_TARGET_RATIO_EXCEEDED")
        account = self.broker.account_snapshot(as_of=self.source.evaluation_time)
        existing = self.intents.intents()
        if existing is None:
            return _risk_block("CYCLE_EXECUTION_STATE_UNKNOWN")
        return self.portfolio_risk.evaluate(
            candidate,
            account=account,
            executions_in_cycle=len(existing),
            stop_pips=stop_pips,
        )

    def _resume(self, intent_id: str) -> RunResult:
        while True:
            intent = self.intents.get(intent_id)
            if intent is None or not self._intent_matches_source(intent):
                return self._blocked("INTENT_STATE_UNKNOWN_OR_MISMATCHED")
            state = intent.lifecycle_state
            if state is LifecycleState.SIGNAL_DETECTED:
                if not self.intents.transition(
                    intent_id,
                    LifecycleState.INTENT_CREATED,
                    reason="RECOVERED_PERSISTENT_SIGNAL_INTENT",
                    occurred_at=intent.created_at,
                ):
                    return self._fail_safe(intent_id, "SIGNAL_INTENT_RECOVERY_FAILURE")
                continue
            if state is LifecycleState.INTENT_CREATED:
                if not self.intents.transition(
                    intent_id,
                    LifecycleState.ORDER_SUBMITTED,
                    reason="PAPER_SUBMISSION_CLAIMED_ONCE",
                    occurred_at=intent.created_at,
                ):
                    return self._fail_safe(intent_id, "ORDER_SUBMISSION_CLAIM_FAILURE")
                self._fault(FaultPoint.AFTER_ORDER_SUBMITTED)
                continue
            if state is LifecycleState.ORDER_SUBMITTED:
                order = self.broker.order_for_intent(intent_id)
                fill = self.broker.fill_for_intent(intent_id)
                if (order is None) != (fill is None):
                    return self._fail_safe(intent_id, "PARTIAL_PAPER_ORDER_STATE")
                if order is None:
                    order = self._order(intent)
                    fill = self.broker.submit(order)
                    self._fault(FaultPoint.AFTER_PAPER_SUBMISSION)
                if fill is None:
                    return self._fail_safe(intent_id, "PAPER_FILL_UNKNOWN")
                if not self.intents.append_lineage(
                    cycle_id=intent.cycle_id,
                    identity=intent_id,
                    phase="EXECUTION",
                    payload={
                        "order_reference": order.order_reference,
                        "execution_port": "PaperBroker",
                        "accepted": fill.accepted,
                        "reason": fill.reason,
                    },
                    occurred_at=intent.created_at,
                ):
                    return self._fail_safe(intent_id, "EXECUTION_EVIDENCE_FAILURE")
                target = (
                    LifecycleState.ORDER_ACCEPTED
                    if fill.accepted
                    else LifecycleState.ORDER_REJECTED
                )
                if not self.intents.transition(
                    intent_id,
                    target,
                    reason=fill.reason,
                    occurred_at=fill.timestamp,
                ):
                    return self._fail_safe(intent_id, "ORDER_RESULT_TRANSITION_FAILURE")
                if target is LifecycleState.ORDER_REJECTED:
                    if not self.intents.transition(
                        intent_id,
                        LifecycleState.RECONCILED,
                        reason="REJECTED_ORDER_HAS_NO_POSITION",
                        occurred_at=fill.timestamp,
                    ):
                        return self._fail_safe(intent_id, "REJECTION_RECONCILIATION_FAILURE")
                    return self._completed(self.intents.get(intent_id), idempotent=False)
                self._fault(FaultPoint.AFTER_ORDER_ACCEPTED)
                continue
            if state is LifecycleState.ORDER_ACCEPTED:
                fill = self.broker.fill_for_intent(intent_id)
                position = self.broker.position_for_intent(intent_id)
                if (
                    fill is None
                    or not fill.accepted
                    or position is None
                    or fill.position_reference != position.position_reference
                ):
                    return self._fail_safe(intent_id, "ACCEPTED_POSITION_STATE_UNKNOWN")
                if not self.intents.append_lineage(
                    cycle_id=intent.cycle_id,
                    identity=intent_id,
                    phase="CONFIRMATION",
                    payload={
                        **asdict(fill),
                        "timestamp": fill.timestamp.astimezone(UTC).isoformat(),
                    },
                    occurred_at=fill.timestamp,
                ) or not self.intents.transition(
                    intent_id,
                    LifecycleState.POSITION_OPEN,
                    reason="PAPER_POSITION_CONFIRMED",
                    occurred_at=fill.timestamp,
                ):
                    return self._fail_safe(intent_id, "POSITION_OPEN_TRANSITION_FAILURE")
                if not self.intents.append_lineage(
                    cycle_id=intent.cycle_id,
                    identity=intent_id,
                    phase="POSITION",
                    payload={
                        "position_reference": position.position_reference,
                        "stop_level": position.stop_level,
                        "target_level": position.target_level,
                    },
                    occurred_at=position.opened_at,
                ):
                    return self._fail_safe(intent_id, "POSITION_EVIDENCE_FAILURE")
                self._fault(FaultPoint.AFTER_POSITION_OPEN)
                continue
            if state is LifecycleState.POSITION_OPEN:
                position = self.broker.position_for_intent(intent_id)
                candle = self.historical_data.exit_candle(
                    intent.epic, after=self.source.evaluation_time
                )
                if position is None or candle is None:
                    return self._fail_safe(intent_id, "EXIT_MARKET_OR_POSITION_UNKNOWN")
                exit_price = _exit_price(
                    position.side, position.stop_level, position.target_level, candle
                )
                if exit_price is None:
                    return self._fail_safe(intent_id, "EXIT_STATE_MISSING_OR_AMBIGUOUS")
                price, reason = exit_price
                request = Exit(
                    f"PAPER-EXIT-{intent_id}",
                    position.position_reference,
                    intent_id,
                    price,
                    reason,
                    0.0,
                    candle.timestamp,
                )
                if not self.intents.record_exit_request(
                    intent_id,
                    _exit_request_document(request),
                    occurred_at=candle.timestamp,
                ) or not self.intents.transition(
                    intent_id,
                    LifecycleState.EXIT_REQUESTED,
                    reason=f"{reason}_LEVEL_REACHED",
                    occurred_at=candle.timestamp,
                ):
                    return self._fail_safe(intent_id, "EXIT_REQUEST_PERSISTENCE_FAILURE")
                self._fault(FaultPoint.AFTER_EXIT_REQUESTED)
                continue
            if state is LifecycleState.EXIT_REQUESTED:
                request_value = self.intents.exit_request(intent_id)
                if request_value is None:
                    return self._fail_safe(intent_id, "EXIT_REQUEST_UNKNOWN")
                request = _exit_request_from_document(request_value)
                closed = self.broker.exit_for_intent(intent_id)
                if closed is None:
                    closed = self.broker.close(request)
                    self._fault(FaultPoint.AFTER_PAPER_CLOSE)
                if closed is None:
                    return self._fail_safe(intent_id, "PAPER_CLOSE_UNKNOWN")
                if not self.intents.append_lineage(
                    cycle_id=intent.cycle_id,
                    identity=intent_id,
                    phase="EXIT",
                    payload={
                        "exit_reference": closed.exit_reference,
                        "price": closed.price,
                        "reason": closed.reason,
                        "profit_loss": closed.profit_loss,
                    },
                    occurred_at=closed.closed_at,
                ) or not self.intents.transition(
                    intent_id,
                    LifecycleState.POSITION_CLOSED,
                    reason="PAPER_CLOSE_CONFIRMED",
                    occurred_at=closed.closed_at,
                ):
                    return self._fail_safe(intent_id, "POSITION_CLOSE_TRANSITION_FAILURE")
                self._fault(FaultPoint.AFTER_POSITION_CLOSED)
                continue
            if state is LifecycleState.POSITION_CLOSED:
                closed = self.broker.exit_for_intent(intent_id)
                reconciliation = (
                    self.broker.reconciliation_snapshot(as_of=closed.closed_at)
                    if closed is not None
                    else None
                )
                if (
                    reconciliation is None
                    or closed is None
                    or any(item.intent_id == intent_id for item in reconciliation.account.positions)
                ):
                    return self._fail_safe(intent_id, "RECONCILIATION_STATE_UNKNOWN")
                if not self.intents.append_lineage(
                    cycle_id=intent.cycle_id,
                    identity=intent_id,
                    phase="RECONCILIATION",
                    payload={
                        "position_open": False,
                        "order_count": len(reconciliation.orders),
                        "fill_count": len(reconciliation.fills),
                        "exit_count": len(reconciliation.exits),
                        "result": "MATCHED",
                    },
                    occurred_at=closed.closed_at,
                ) or not self.intents.transition(
                    intent_id,
                    LifecycleState.RECONCILED,
                    reason="PAPER_STATE_MATCHED",
                    occurred_at=closed.closed_at,
                ):
                    return self._fail_safe(intent_id, "RECONCILIATION_TRANSITION_FAILURE")
                continue
            if state is LifecycleState.RECONCILED:
                return self._completed(intent, idempotent=False)
            if state is LifecycleState.ORDER_REJECTED:
                return self._fail_safe(intent_id, "UNEXPECTED_RECOVERY_STATE")
            return self._blocked("FAILED_SAFE_REQUIRES_RECONCILIATION")

    def _order(self, intent: TradeIntent) -> BrokerOrder:
        quote = self.market_data.quote(intent.epic, as_of=self.source.evaluation_time)
        if quote is None:
            raise ValueError("persisted intent quote unavailable")
        entry = quote.offer if intent.side is Side.BUY else quote.bid
        return BrokerOrder(
            f"PAPER-ORDER-{intent.intent_id}",
            intent.intent_id,
            intent.epic,
            intent.side,
            intent.size,
            entry,
            intent.stop_level,
            intent.target_level,
            quote.pip_size,
            quote.pip_value_account_currency,
            intent.created_at,
        )

    def _intent_matches_source(self, intent: TradeIntent) -> bool:
        references = self.source.source_references(intent.epic)
        fingerprint = hashlib.sha256(
            _encode({"epic": intent.epic, "references": references}).encode()
        ).hexdigest()
        return bool(
            intent.cycle_id == self.source.cycle_id
            and intent.execution_mode is ExecutionMode.OFFLINE_PAPER
            and intent.strategy == "Scalper"
            and intent.strategy_version == "Scalper:rsi-adx-v1"
            and intent.source_candle_references == references
            and intent.source_fingerprint == fingerprint
            and intent.epic in {item[1] for item in FROZEN_INSTRUMENTS}
        )

    def _completed(self, intent: TradeIntent | None, *, idempotent: bool) -> RunResult:
        if intent is None:
            return self._blocked("COMPLETED_INTENT_UNKNOWN")
        events = self.intents.events(intent.intent_id)
        if events is None:
            return self._blocked("LIFECYCLE_EVENTS_UNKNOWN")
        fill = self.broker.fill_for_intent(intent.intent_id)
        if fill is not None and not fill.accepted:
            paper_result = "REJECTED"
            reconciliation = "NO_POSITION_MATCHED"
        else:
            paper_result = "ACCEPTED_AND_CLOSED"
            reconciliation = "MATCHED"
        return RunResult(
            RunStatus.COMPLETE,
            "OFFLINE_PAPER_LIFECYCLE_COMPLETE",
            intent.cycle_id,
            (intent.intent_id,),
            tuple(item.to_state for item in events),
            intent.risk_decision.code,
            paper_result,
            reconciliation,
            idempotent,
        )

    def _fail_safe(self, intent_id: str, reason: str) -> RunResult:
        intent = self.intents.get(intent_id)
        if intent is not None and intent.lifecycle_state not in {
            LifecycleState.FAILED_SAFE,
            LifecycleState.RECONCILED,
            LifecycleState.RISK_REJECTED,
        }:
            events = self.intents.events(intent_id)
            occurred_at = events[-1].occurred_at if events else self.source.evaluation_time
            self.intents.transition(
                intent_id,
                LifecycleState.FAILED_SAFE,
                reason=reason,
                occurred_at=occurred_at,
            )
        return self._blocked(reason)

    def _blocked(self, reason: str) -> RunResult:
        return RunResult(
            RunStatus.BLOCKED,
            reason,
            self.source.cycle_id,
            (),
            (),
            "UNKNOWN_OR_BLOCKED",
            "NOT_SUBMITTED_OR_HALTED",
            "UNKNOWN_OR_BLOCKED",
            False,
        )

    def _fault(self, point: FaultPoint) -> None:
        if self.fault_hook is not None:
            self.fault_hook(point)


def raising_fault(point: FaultPoint) -> Callable[[FaultPoint], None]:
    def hook(observed: FaultPoint) -> None:
        if observed is point:
            raise InjectedCrash(point.value)

    return hook


def _risk_block(code: str) -> RiskDecision:
    return RiskDecision(False, code, None, None, None, None, None, None, None, None, None)


def _exit_price(side: Side, stop: float, target: float, candle: object) -> tuple[float, str] | None:
    from src.ig_trader.offline_paper.domain import Candle

    if not isinstance(candle, Candle):
        return None
    if side is Side.BUY:
        stop_hit = candle.bid_low <= stop
        target_hit = candle.bid_high >= target
    else:
        stop_hit = candle.offer_high >= stop
        target_hit = candle.offer_low <= target
    if stop_hit == target_hit:
        return None
    return (stop, "STOP") if stop_hit else (target, "TARGET")


def _quote_document(quote: object) -> dict[str, object]:
    from src.ig_trader.offline_paper.domain import Quote

    if not isinstance(quote, Quote):
        return {}
    return {
        **asdict(quote),
        "timestamp": quote.timestamp.astimezone(UTC).isoformat(),
        "spread_pips": quote.spread_pips,
    }


def _exit_request_document(value: Exit) -> dict[str, object]:
    return {
        **asdict(value),
        "closed_at": value.closed_at.astimezone(UTC).isoformat(),
    }


def _exit_request_from_document(value: dict[str, object]) -> Exit:
    parsed = datetime.fromisoformat(str(value["closed_at"]))
    if parsed.tzinfo is None:
        raise ValueError("exit request timestamp is unaware")
    return Exit(
        str(value["exit_reference"]),
        str(value["position_reference"]),
        str(value["intent_id"]),
        float(value["price"]),
        str(value["reason"]),
        float(value["profit_loss"]),
        parsed.astimezone(UTC),
    )


def _encode(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
