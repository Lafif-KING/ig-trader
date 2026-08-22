from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pytest

from src.ig_trader.shadow_execution import (
    ExecutionMode,
    InMemoryShadowStore,
    InstrumentMetadata,
    InstrumentRegistry,
    MarketQuote,
    ShadowExecutionCore,
    ShadowLifecycle,
)
from src.ig_trader.shadow_runtime import (
    ShadowAccountState,
    ShadowAtrSnapshot,
    ShadowInstrumentMetadata,
    ShadowRuntimeOrchestrator,
)

NOW = datetime(2026, 8, 21, 12, tzinfo=UTC)
EPIC = "CS.D.EURGBP.MINI.IP"
DEFAULT_QUOTE = MarketQuote(0.8499, 0.8500, NOW)


class Lease:
    authorized = True
    fencing_token = 11


class Account:
    def __init__(self, value: ShadowAccountState | None = None) -> None:
        self.value = value if value is not None else ShadowAccountState(0.0, NOW)

    def account_state(self, *, as_of: datetime) -> ShadowAccountState | None:
        assert as_of == NOW
        return self.value


class Metadata:
    def __init__(self, value: ShadowInstrumentMetadata | None = None) -> None:
        self.value = value or ShadowInstrumentMetadata(InstrumentMetadata(EPIC, 0.0001), NOW)

    def instrument_metadata(self, epic: str, *, as_of: datetime) -> ShadowInstrumentMetadata | None:
        assert epic == EPIC and as_of == NOW
        return self.value


class Market:
    def __init__(self, value: MarketQuote | None = DEFAULT_QUOTE) -> None:
        self.value = value

    def quote(self, epic: str, *, as_of: datetime) -> MarketQuote | None:
        assert epic == EPIC and as_of == NOW
        return self.value


class Historical:
    def __init__(self, value: ShadowAtrSnapshot | None = None) -> None:
        self.value = value if value is not None else ShadowAtrSnapshot(0.0005, NOW)

    def atr(self, epic: str, *, as_of: datetime) -> ShadowAtrSnapshot | None:
        assert epic == EPIC and as_of == NOW
        return self.value


class Strategy:
    def __init__(self, direction: str = "BUY", epic: str = EPIC, fail: bool = False) -> None:
        self.direction = direction
        self.epic = epic
        self.fail = fail

    def generate_signal(self, epic: str, market_frame: object) -> SimpleNamespace:
        if self.fail:
            raise RuntimeError("strategy unavailable")
        return SimpleNamespace(
            direction=SimpleNamespace(value=self.direction), strategy_name="S0", epic=self.epic
        )


def orchestrator(
    *,
    mode: ExecutionMode = ExecutionMode.SHADOW_DEMO,
    account: ShadowAccountState | None = None,
    metadata: ShadowInstrumentMetadata | None = None,
    quote: MarketQuote | None = DEFAULT_QUOTE,
    atr: ShadowAtrSnapshot | None = None,
    strategy: Strategy | None = None,
    store: InMemoryShadowStore | None = None,
) -> tuple[ShadowRuntimeOrchestrator, InMemoryShadowStore, ShadowExecutionCore]:
    lease = Lease()
    store = store or InMemoryShadowStore(lease.fencing_token)
    shadow = ShadowExecutionCore(
        mode=mode,
        lease=lease,
        store=store,
        risk_gate=lambda *_a, **_k: True,
        instruments=InstrumentRegistry.frozen_v1(),
    )
    return (
        ShadowRuntimeOrchestrator(
            mode=mode,
            epic=EPIC,
            account_state=Account(account),
            instrument_metadata=Metadata(metadata),
            market_data=Market(quote),
            historical_data=Historical(atr),
            strategy=strategy or Strategy(),
            shadow=shadow,
        ),
        store,
        shadow,
    )


def run(runtime: ShadowRuntimeOrchestrator, cycle_id: str = "cycle-0001") -> dict[str, object]:
    return runtime.run_cycle(cycle_id, object(), now=NOW)


def test_runtime_derives_protection_without_caller_risk_inputs() -> None:
    runtime, store, _shadow = orchestrator()

    evidence = run(runtime)

    assert evidence["status"] == "SHADOW_OPEN"
    record = store.get(UUID(str(evidence["intent_id"])))
    assert record is not None
    assert record.entry_price == 0.8500
    assert record.stop_price == pytest.approx(0.8490)
    assert record.target_price == pytest.approx(0.8515)
    assert set(evidence) >= {
        "authorized",
        "broker_order_call_count",
        "cycle_id",
        "execution_mode",
        "order_authority",
        "reason",
        "status",
    }
    assert evidence["authorized"] is False
    assert evidence["order_authority"] is False
    assert evidence["broker_order_call_count"] == 0


@pytest.mark.parametrize(
    ("quote", "atr"),
    [
        (MarketQuote(0.8498, 0.8500, NOW), ShadowAtrSnapshot(0.0005, NOW)),
        (MarketQuote(0.8499, 0.8500, NOW), ShadowAtrSnapshot(0.0, NOW)),
    ],
)
def test_spread_and_atr_policy_gates_fail_closed(
    quote: MarketQuote, atr: ShadowAtrSnapshot
) -> None:
    runtime, store, _shadow = orchestrator(quote=quote, atr=atr)

    assert run(runtime)["status"] == "FAILED_SAFE"
    assert store.records == {}


def test_atr_stop_is_capped_at_twelve_pips_before_target_derivation() -> None:
    runtime, store, _shadow = orchestrator(atr=ShadowAtrSnapshot(0.002, NOW))

    evidence = run(runtime)

    record = store.get(UUID(str(evidence["intent_id"])))
    assert record is not None
    assert record.entry_price - record.stop_price == pytest.approx(0.0012)
    assert record.target_price - record.entry_price == pytest.approx(0.0018)


@pytest.mark.parametrize(
    "account",
    [
        None,
        ShadowAccountState(0.0, NOW - timedelta(seconds=11)),
        ShadowAccountState(float("nan"), NOW),
        ShadowAccountState(0.0, NOW, state_known=False),
    ],
)
def test_missing_or_stale_account_state_fails_closed(account: ShadowAccountState | None) -> None:
    runtime, store, _shadow = orchestrator(account=account)
    if account is None:
        runtime.account_state = Account(None)
        runtime.account_state.value = None

    assert run(runtime)["status"] == "FAILED_SAFE"
    assert store.records == {}


@pytest.mark.parametrize(
    "metadata",
    [
        ShadowInstrumentMetadata(InstrumentMetadata(EPIC, 0.0001), NOW - timedelta(seconds=11)),
        ShadowInstrumentMetadata(InstrumentMetadata("CS.D.EURUSD.CEFM.IP", 0.0001), NOW),
    ],
)
def test_missing_or_stale_metadata_fails_closed(metadata: ShadowInstrumentMetadata) -> None:
    runtime, store, _shadow = orchestrator(metadata=metadata)
    assert run(runtime)["status"] == "FAILED_SAFE"
    assert store.records == {}


def test_existing_lifecycle_evidence_uses_durable_label() -> None:
    runtime, store, shadow = orchestrator()
    cycle_id = "durable-labels"
    opened = run(runtime, cycle_id)
    intent_id = UUID(str(opened["intent_id"]))
    assert run(runtime, cycle_id)["status"] == "SHADOW_OPEN"

    record = store.get(intent_id)
    assert record is not None
    closed = shadow.close_on_quote(record, MarketQuote(0.8515, 0.8516, NOW), now=NOW)
    assert closed.lifecycle is ShadowLifecycle.CLOSED
    assert run(runtime, cycle_id)["status"] == "SHADOW_CLOSED"

    reconciled = shadow.reconcile(closed, now=NOW)
    assert reconciled.lifecycle is ShadowLifecycle.RECONCILED
    assert run(runtime, cycle_id)["status"] == "SHADOW_RECONCILED"


def test_global_cycle_and_durable_position_limit_are_authoritative() -> None:
    first, store, _shadow = orchestrator()
    assert run(first, "first")["status"] == "SHADOW_OPEN"
    second, _store, _shadow = orchestrator(store=store)
    assert run(second, "second")["reason"] == "SHADOW_V1_POSITION_LIMIT"
    assert len(store.records) == 1


@pytest.mark.parametrize(
    "mode",
    [ExecutionMode.NO_EXECUTION, ExecutionMode.DEMO_EXECUTION, ExecutionMode.LIVE_EXECUTION],
)
def test_all_non_shadow_modes_are_broker_inert(mode: ExecutionMode) -> None:
    runtime, store, _shadow = orchestrator(mode=mode)
    evidence = run(runtime)
    assert evidence["status"] in {"NO_TRADE", "FAILED_SAFE"}
    assert evidence["authorized"] is False
    assert evidence["order_authority"] is False
    assert evidence["broker_order_call_count"] == 0
    assert store.records == {}


def test_shadow_runtime_has_no_execution_adapter_or_order_endpoint_paths() -> None:
    source = Path("src/ig_trader/shadow_runtime.py").read_text(encoding="utf-8")
    for prohibited in ("/positions", "/workingorders", '"POST"', '"PUT"', '"DELETE"'):
        assert prohibited not in source
    assert "src.ig_trader.execution" not in source
