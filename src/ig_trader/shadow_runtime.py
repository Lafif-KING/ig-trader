"""Broker-neutral SHADOW_DEMO orchestration with trusted market/risk inputs."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from math import isfinite
from typing import Any, Protocol
from uuid import NAMESPACE_URL, UUID, uuid5

from src.ig_trader.models import SignalDirection
from src.ig_trader.offline_paper.conductor import FrozenV1Config, PortfolioRisk
from src.ig_trader.shadow_execution import (
    ExecutionMode,
    InstrumentMetadata,
    MarketQuote,
    ShadowExecutionCore,
    ShadowExecutionError,
    ShadowIntentRecord,
    ShadowLifecycle,
)


@dataclass(frozen=True)
class ShadowAccountState:
    """Fresh broker-neutral account state required for a Shadow decision."""

    daily_loss_pct: float
    as_of: datetime
    state_known: bool = True


@dataclass(frozen=True)
class ShadowInstrumentMetadata:
    """Timestamped instrument metadata from the authoritative metadata port."""

    metadata: InstrumentMetadata
    as_of: datetime


@dataclass(frozen=True)
class ShadowAtrSnapshot:
    """Timestamped finalized-candle ATR in quote-price units."""

    atr: float
    as_of: datetime


class ShadowAccountStatePort(Protocol):
    def account_state(self, *, as_of: datetime) -> ShadowAccountState | None: ...


class InstrumentMetadataPort(Protocol):
    def instrument_metadata(self, epic: str, *, as_of: datetime) -> ShadowInstrumentMetadata | None: ...


class ShadowMarketDataPort(Protocol):
    def quote(self, epic: str, *, as_of: datetime) -> MarketQuote | None: ...


class ShadowHistoricalDataPort(Protocol):
    def atr(self, epic: str, *, as_of: datetime) -> ShadowAtrSnapshot | None: ...


class ShadowStrategyPort(Protocol):
    def generate_signal(self, epic: str, market_frame: Any) -> Any: ...


class ShadowRuntimeOrchestrator:
    """One globally deterministic, permanently broker-inert Shadow cycle."""

    def __init__(
        self,
        *,
        mode: ExecutionMode,
        epic: str,
        account_state: ShadowAccountStatePort,
        instrument_metadata: InstrumentMetadataPort,
        market_data: ShadowMarketDataPort,
        historical_data: ShadowHistoricalDataPort,
        strategy: ShadowStrategyPort,
        shadow: ShadowExecutionCore,
        config: FrozenV1Config | None = None,
        max_state_age: timedelta = timedelta(seconds=10),
    ) -> None:
        if not epic.strip() or max_state_age <= timedelta(0):
            raise ValueError("Shadow runtime configuration is invalid")
        self.mode = mode
        self.epic = epic
        self.account_state = account_state
        self.instrument_metadata = instrument_metadata
        self.market_data = market_data
        self.historical_data = historical_data
        self.strategy = strategy
        self.shadow = shadow
        self.config = config or FrozenV1Config()
        # The accepted G2 policy remains the canonical V1 portfolio authority.
        # The runtime supplies only trusted state; its adapter never accepts a
        # caller-provided risk count, daily loss, stop, or target.
        self.portfolio_risk = PortfolioRisk(self.config)
        self.max_state_age = max_state_age

    def run_cycle(
        self,
        cycle_id: str,
        market_frame: Any,
        *,
        now: datetime,
    ) -> dict[str, object]:
        if self.mode is ExecutionMode.NO_EXECUTION:
            return _evidence("NO_TRADE", "NO_EXECUTION", cycle_id, self.mode)
        if self.mode is not ExecutionMode.SHADOW_DEMO:
            return _evidence("FAILED_SAFE", "EXECUTION_MODE_DISABLED", cycle_id, self.mode)
        try:
            now_utc = _required_utc(now)
            intent_id = _cycle_intent_id(cycle_id)
            existing = self.shadow.store.get(intent_id)
            if existing is not None:
                if existing.instrument != self.epic:
                    return _evidence("FAILED_SAFE", "GLOBAL_CYCLE_ALREADY_CLAIMED", cycle_id, self.mode)
                if existing.lifecycle is ShadowLifecycle.SHADOW_INTENT_CREATED:
                    existing = self.shadow.open_intent(existing, now=now_utc)
                return _record_evidence(existing, cycle_id, "IDEMPOTENT_GLOBAL_CYCLE", self.mode)

            active_position_count = self.shadow.store.active_position_count()
            if not _valid_count(active_position_count):
                raise ShadowExecutionError("shadow active-position count is ambiguous")
            if active_position_count >= self.config.maximum_total_positions:
                return _evidence("NO_TRADE", "SHADOW_V1_POSITION_LIMIT", cycle_id, self.mode)

            account = self.account_state.account_state(as_of=now_utc)
            _validate_account_state(account, now_utc, self.max_state_age)
            metadata = self.instrument_metadata.instrument_metadata(self.epic, as_of=now_utc)
            _validate_metadata(metadata, self.epic, now_utc, self.max_state_age)
            quote = self.market_data.quote(self.epic, as_of=now_utc)
            if quote is None:
                return _evidence("NO_TRADE", "MARKET_DATA_UNAVAILABLE", cycle_id, self.mode)
            quote.validate(now_utc, self.shadow.max_quote_age)
            atr = self.historical_data.atr(self.epic, as_of=now_utc)
            _validate_atr(atr, now_utc, self.max_state_age)

            signal = self.strategy.generate_signal(self.epic, market_frame)
            if getattr(signal, "epic", None) != self.epic:
                return _evidence("FAILED_SAFE", "SIGNAL_INSTRUMENT_MISMATCH", cycle_id, self.mode)
            direction = getattr(signal.direction, "value", signal.direction)
            if direction == SignalDirection.WAIT.value:
                return _evidence("NO_TRADE", "S0_WAIT", cycle_id, self.mode)
            if direction not in {SignalDirection.BUY.value, SignalDirection.SELL.value}:
                return _evidence("FAILED_SAFE", "SIGNAL_DIRECTION_INVALID", cycle_id, self.mode)

            stop_price, target_price = _derive_protection(
                direction=direction,
                quote=quote,
                atr=atr.atr,
                pip_size=metadata.metadata.pip_size,
                config=self.config,
            )
            intent = self.shadow.create_intent(
                signal,
                quote,
                intent_id=intent_id,
                stop_price=stop_price,
                target_price=target_price,
                open_positions_for_strategy=active_position_count,
                daily_loss_pct=account.daily_loss_pct,
                now=now_utc,
            )
            opened = self.shadow.open_intent(intent, now=now_utc)
            return _record_evidence(opened, cycle_id, "S0_RISK_APPROVED", self.mode)
        except ShadowExecutionError:
            return _evidence("FAILED_SAFE", "SHADOW_CYCLE_REJECTED", cycle_id, self.mode)
        except Exception:
            return _evidence("FAILED_SAFE", "SHADOW_CYCLE_EXCEPTION", cycle_id, self.mode)

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
                return _evidence("FAILED_SAFE", "SHADOW_STATE_UNKNOWN", str(intent_id), self.mode)
            if record.lifecycle is ShadowLifecycle.SHADOW_INTENT_CREATED:
                record = self.shadow.open_intent(record, now=now)
            if record.lifecycle is ShadowLifecycle.OPEN:
                if quote is None:
                    return _record_evidence(record, str(intent_id), "MARKET_DATA_UNAVAILABLE", self.mode)
                record = self.shadow.close_on_quote(record, quote, now=now)
            if record.lifecycle is ShadowLifecycle.CLOSED:
                performance = self.shadow.performance(record)
                reconciled = self.shadow.reconcile(record, now=now)
                return {
                    **_record_evidence(reconciled, str(intent_id), "HYPOTHETICAL_PERFORMANCE", self.mode),
                    "performance": asdict(performance),
                }
            if record.lifecycle is ShadowLifecycle.RECONCILED:
                return {
                    **_record_evidence(record, str(intent_id), "IDEMPOTENT_RECOVERY", self.mode),
                    "performance": asdict(self.shadow.performance(record)),
                }
            return _record_evidence(record, str(intent_id), "RECOVERY_STATE", self.mode)
        except ShadowExecutionError:
            return _evidence("FAILED_SAFE", "SHADOW_RECOVERY_REJECTED", str(intent_id), self.mode)
        except Exception:
            return _evidence("FAILED_SAFE", "SHADOW_RECOVERY_EXCEPTION", str(intent_id), self.mode)


def _derive_protection(
    *,
    direction: str,
    quote: MarketQuote,
    atr: float,
    pip_size: float,
    config: FrozenV1Config,
) -> tuple[float, float]:
    if not _positive_finite(atr) or not _positive_finite(pip_size):
        raise ShadowExecutionError("ATR or pip size is invalid")
    entry = quote.offer if direction == SignalDirection.BUY.value else quote.bid
    raw_stop_distance = atr * config.stop_atr_multiplier
    maximum_stop_distance = config.maximum_stop_pips * pip_size
    stop_distance = min(raw_stop_distance, maximum_stop_distance)
    target_distance = stop_distance * config.reward_to_risk
    spread = quote.offer - quote.bid
    if (
        not _positive_finite(stop_distance)
        or not _positive_finite(target_distance)
        or stop_distance > maximum_stop_distance
        or spread < 0
        or spread / pip_size > config.maximum_spread_pips
        or spread / target_distance > config.maximum_spread_to_target_ratio
    ):
        raise ShadowExecutionError("Shadow protection or spread gates reject the cycle")
    if direction == SignalDirection.BUY.value:
        return entry - stop_distance, entry + target_distance
    return entry + stop_distance, entry - target_distance


def _validate_account_state(
    state: ShadowAccountState | None,
    now: datetime,
    max_age: timedelta,
) -> None:
    if (
        state is None
        or not state.state_known
        or not _finite_number(state.daily_loss_pct)
        or not _fresh(state.as_of, now, max_age)
    ):
        raise ShadowExecutionError("account state is unavailable or stale")


def _validate_metadata(
    snapshot: ShadowInstrumentMetadata | None,
    epic: str,
    now: datetime,
    max_age: timedelta,
) -> None:
    if (
        snapshot is None
        or snapshot.metadata.epic != epic
        or not _positive_finite(snapshot.metadata.pip_size)
        or not _fresh(snapshot.as_of, now, max_age)
    ):
        raise ShadowExecutionError("instrument metadata is unavailable or stale")


def _validate_atr(snapshot: ShadowAtrSnapshot | None, now: datetime, max_age: timedelta) -> None:
    if snapshot is None or not _positive_finite(snapshot.atr) or not _fresh(snapshot.as_of, now, max_age):
        raise ShadowExecutionError("ATR state is unavailable or stale")


def _record_evidence(
    record: ShadowIntentRecord,
    cycle_id: str,
    reason: str,
    mode: ExecutionMode,
) -> dict[str, object]:
    status = {
        ShadowLifecycle.SHADOW_INTENT_CREATED: "SHADOW_INTENT_CREATED",
        ShadowLifecycle.OPEN: "SHADOW_OPEN",
        ShadowLifecycle.CLOSED: "SHADOW_CLOSED",
        ShadowLifecycle.RECONCILED: "SHADOW_RECONCILED",
        ShadowLifecycle.FAILED_SAFE: "FAILED_SAFE",
    }[record.lifecycle]
    return {
        **_evidence(status, reason, cycle_id, mode),
        "intent_id": str(record.intent_id),
        "lifecycle": record.lifecycle.value,
    }


def _cycle_intent_id(cycle_id: str) -> UUID:
    """Bind one intent to a global cycle; the epic is deliberately excluded."""

    if not isinstance(cycle_id, str) or not cycle_id.strip():
        raise ShadowExecutionError("cycle identity is invalid")
    return uuid5(NAMESPACE_URL, f"ig-trader-shadow:{cycle_id}")


def _evidence(
    status: str,
    reason: str,
    cycle_id: str,
    mode: ExecutionMode,
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


def _fresh(value: datetime, now: datetime, max_age: timedelta) -> bool:
    try:
        value_utc = _required_utc(value)
        now_utc = _required_utc(now)
    except ShadowExecutionError:
        return False
    return value_utc <= now_utc and now_utc - value_utc <= max_age


def _required_utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ShadowExecutionError("Shadow timestamp is invalid")
    return value.astimezone(UTC)


def _finite_number(value: object) -> bool:
    return not isinstance(value, bool) and isinstance(value, int | float) and isfinite(float(value))


def _positive_finite(value: object) -> bool:
    return _finite_number(value) and float(value) > 0


def _valid_count(value: object) -> bool:
    return not isinstance(value, bool) and isinstance(value, int) and value >= 0
