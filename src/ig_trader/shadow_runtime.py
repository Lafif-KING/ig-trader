"""Local orchestration for broker-neutral SHADOW_DEMO cycles."""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime
from typing import Any, Protocol
from uuid import NAMESPACE_URL, UUID, uuid5

from src.ig_trader.models import SignalDirection
from src.ig_trader.shadow_execution import (
    ExecutionMode,
    MarketQuote,
    ShadowExecutionCore,
    ShadowExecutionError,
    ShadowLifecycle,
)


class ShadowMarketDataPort(Protocol):
    def quote(self, epic: str, *, as_of: datetime) -> MarketQuote | None: ...


class ShadowStrategyPort(Protocol):
    def generate_signal(self, epic: str, market_frame: Any) -> Any: ...


class ShadowRuntimeOrchestrator:
    """One deterministic global cycle, intentionally shared across instruments."""

    def __init__(
        self,
        *,
        mode: ExecutionMode,
        epic: str,
        market_data: ShadowMarketDataPort,
        strategy: ShadowStrategyPort,
        shadow: ShadowExecutionCore,
    ) -> None:
        self.mode = mode
        self.epic = epic
        self.market_data = market_data
        self.strategy = strategy
        self.shadow = shadow

    def run_cycle(
        self,
        cycle_id: str,
        market_frame: Any,
        *,
        now: datetime,
        stop_price: float,
        target_price: float,
        daily_loss_pct: float,
    ) -> dict[str, object]:
        if self.mode is ExecutionMode.NO_EXECUTION:
            return _evidence("NO_TRADE", "NO_EXECUTION", cycle_id, self.mode)
        if self.mode is not ExecutionMode.SHADOW_DEMO:
            return _evidence("FAILED_SAFE", "EXECUTION_MODE_DISABLED", cycle_id, self.mode)
        try:
            intent_id = _cycle_intent_id(cycle_id)
            existing = self.shadow.store.get(intent_id)
            if existing is not None:
                if existing.instrument != self.epic:
                    return _evidence("FAILED_SAFE", "GLOBAL_CYCLE_ALREADY_CLAIMED", cycle_id)
                if existing.lifecycle is ShadowLifecycle.SHADOW_INTENT_CREATED:
                    existing = self.shadow.open_intent(existing, now=now)
                return {
                    **_evidence("SHADOW_OPEN", "IDEMPOTENT_GLOBAL_CYCLE", cycle_id),
                    "intent_id": str(existing.intent_id),
                    "lifecycle": existing.lifecycle.value,
                }
            active_position_count = self.shadow.store.active_position_count()
            if (
                isinstance(active_position_count, bool)
                or not isinstance(active_position_count, int)
                or active_position_count < 0
            ):
                raise ShadowExecutionError("shadow active-position count is ambiguous")
            if active_position_count >= 1:
                return _evidence("NO_TRADE", "SHADOW_V1_POSITION_LIMIT", cycle_id)
            quote = self.market_data.quote(self.epic, as_of=now)
            if quote is None:
                return _evidence("NO_TRADE", "MARKET_DATA_UNAVAILABLE", cycle_id)
            signal = self.strategy.generate_signal(self.epic, market_frame)
            if getattr(signal, "epic", None) != self.epic:
                return _evidence("FAILED_SAFE", "SIGNAL_INSTRUMENT_MISMATCH", cycle_id)
            direction = getattr(signal.direction, "value", signal.direction)
            if direction == SignalDirection.WAIT.value:
                return _evidence("NO_TRADE", "S0_WAIT", cycle_id)
            intent = self.shadow.create_intent(
                signal,
                quote,
                intent_id=intent_id,
                stop_price=stop_price,
                target_price=target_price,
                open_positions_for_strategy=active_position_count,
                daily_loss_pct=daily_loss_pct,
                now=now,
            )
            opened = self.shadow.open_intent(intent, now=now)
            return {
                **_evidence("SHADOW_OPEN", "S0_RISK_APPROVED", cycle_id),
                "intent_id": str(opened.intent_id),
                "lifecycle": opened.lifecycle.value,
            }
        except Exception:
            return _evidence("FAILED_SAFE", "SHADOW_CYCLE_EXCEPTION", cycle_id)

    def recover(
        self,
        intent_id: UUID,
        *,
        now: datetime,
        quote: MarketQuote | None,
    ) -> dict[str, object]:
        if self.mode is not ExecutionMode.SHADOW_DEMO:
            return _evidence("FAILED_SAFE", "EXECUTION_MODE_DISABLED", str(intent_id), self.mode)
        try:
            record = self.shadow.store.get(intent_id)
            if record is None:
                return _evidence("FAILED_SAFE", "SHADOW_STATE_UNKNOWN", str(intent_id))
            if record.lifecycle is ShadowLifecycle.SHADOW_INTENT_CREATED:
                record = self.shadow.open_intent(record, now=now)
            if record.lifecycle is ShadowLifecycle.OPEN:
                if quote is None:
                    return _evidence("NO_TRADE", "MARKET_DATA_UNAVAILABLE", str(intent_id))
                record = self.shadow.close_on_quote(record, quote, now=now)
            if record.lifecycle is ShadowLifecycle.CLOSED:
                performance = self.shadow.performance(record)
                reconciled = self.shadow.reconcile(record, now=now)
                return {
                    **_evidence("SHADOW_RECONCILED", "HYPOTHETICAL_PERFORMANCE", str(intent_id)),
                    "lifecycle": reconciled.lifecycle.value,
                    "performance": asdict(performance),
                }
            if record.lifecycle is ShadowLifecycle.RECONCILED:
                return {
                    **_evidence("SHADOW_RECONCILED", "IDEMPOTENT_RECOVERY", str(intent_id)),
                    "lifecycle": record.lifecycle.value,
                    "performance": asdict(self.shadow.performance(record)),
                }
            return {
                **_evidence("SHADOW_OPEN", "NO_EXIT_CONDITION", str(intent_id)),
                "lifecycle": record.lifecycle.value,
            }
        except ShadowExecutionError:
            return _evidence("FAILED_SAFE", "SHADOW_RECOVERY_EXCEPTION", str(intent_id))
        except Exception:
            return _evidence("FAILED_SAFE", "SHADOW_RECOVERY_EXCEPTION", str(intent_id))


def _cycle_intent_id(cycle_id: str) -> UUID:
    """Bind one intent to the global cycle; epic is deliberately excluded."""

    if not cycle_id.strip():
        raise ShadowExecutionError("cycle identity is invalid")
    return uuid5(NAMESPACE_URL, f"ig-trader-shadow:{cycle_id}")


def _evidence(
    status: str,
    reason: str,
    cycle_id: str,
    mode: ExecutionMode = ExecutionMode.SHADOW_DEMO,
) -> dict[str, object]:
    return {
        "authorized": False,
        "broker_order_call_count": 0,
        "cycle_id": cycle_id,
        "execution_mode": mode.value,
        "order_authority": False,
        "reason": reason,
        "status": status,
    }
