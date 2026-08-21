from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pytest

from src.ig_trader.shadow_execution import (
    ExecutionMode,
    InMemoryShadowStore,
    InstrumentRegistry,
    MarketQuote,
    ShadowExecutionCore,
)
from src.ig_trader.shadow_runtime import ShadowRuntimeOrchestrator

NOW = datetime(2026, 8, 21, 12, tzinfo=UTC)
EPIC = "CS.D.EURGBP.MINI.IP"
DEFAULT_QUOTE = MarketQuote(0.8498, 0.8500, NOW)


class Lease:
    authorized = True
    fencing_token = 11


class Market:
    def __init__(self, value: MarketQuote | None) -> None:
        self.value = value

    def quote(self, epic: str, *, as_of: datetime) -> MarketQuote | None:
        assert epic == EPIC and as_of == NOW
        return self.value


class Strategy:
    def __init__(self, direction: str = "BUY", fail: bool = False) -> None:
        self.direction = direction
        self.fail = fail

    def generate_signal(self, epic: str, market_frame: object) -> SimpleNamespace:
        if self.fail:
            raise RuntimeError("strategy unavailable")
        return SimpleNamespace(
            direction=SimpleNamespace(value=self.direction),
            strategy_name="S0",
            epic=epic,
        )


def orchestrator(
    *,
    mode: ExecutionMode = ExecutionMode.SHADOW_DEMO,
    market: MarketQuote | None = DEFAULT_QUOTE,
    strategy: Strategy | None = None,
    risk_gate=lambda *_a, **_k: True,
) -> tuple[ShadowRuntimeOrchestrator, InMemoryShadowStore]:
    lease = Lease()
    store = InMemoryShadowStore(lease.fencing_token)
    core = ShadowExecutionCore(
        mode=mode,
        lease=lease,
        store=store,
        risk_gate=risk_gate,
        instruments=InstrumentRegistry.frozen_v1(),
    )
    return (
        ShadowRuntimeOrchestrator(
            mode=mode,
            epic=EPIC,
            market_data=Market(market),
            strategy=strategy or Strategy(),
            shadow=core,
        ),
        store,
    )


def run(runtime: ShadowRuntimeOrchestrator, cycle_id: str = "cycle-0001"):
    return runtime.run_cycle(
        cycle_id,
        market_frame=object(),
        now=NOW,
        stop_price=0.8490,
        target_price=0.8510,
        open_position_count=0,
        daily_loss_pct=0,
    )


def test_complete_local_shadow_lifecycle_and_performance_evidence() -> None:
    runtime, store = orchestrator()
    opened = run(runtime)
    assert opened["status"] == "SHADOW_OPEN"
    assert opened["authorized"] is False
    assert opened["order_authority"] is False
    intent_id = UUID(str(opened["intent_id"]))
    closed = runtime.recover(
        intent_id,
        now=NOW,
        quote=MarketQuote(0.8510, 0.8512, NOW),
    )
    assert closed["status"] == "SHADOW_RECONCILED"
    assert closed["performance"]["exit_price"] == 0.8510
    assert closed["performance"]["pips"] == pytest.approx(10.0)
    assert len(store.records) == 1
    assert runtime.recover(intent_id, now=NOW, quote=None)["reason"] == "IDEMPOTENT_RECOVERY"


def test_duplicate_cycle_is_deterministic_and_creates_one_position() -> None:
    runtime, store = orchestrator()
    first = run(runtime)
    second = run(runtime)
    assert first["intent_id"] == second["intent_id"]
    assert len(store.records) == 1


def test_shadow_v1_one_position_limit_blocks_before_strategy() -> None:
    runtime, store = orchestrator(strategy=Strategy(fail=True))
    evidence = runtime.run_cycle(
        "cycle-limit",
        object(),
        now=NOW,
        stop_price=0.8490,
        target_price=0.8510,
        open_position_count=1,
        daily_loss_pct=0,
    )
    assert evidence["reason"] == "SHADOW_V1_POSITION_LIMIT"
    assert store.records == {}


def test_missing_data_wait_and_exception_are_no_trade_or_failed_safe() -> None:
    missing, _store = orchestrator(market=None)
    assert run(missing)["status"] == "NO_TRADE"
    waiting, _store = orchestrator(strategy=Strategy(direction="WAIT"))
    assert run(waiting)["reason"] == "S0_WAIT"
    failing, _store = orchestrator(strategy=Strategy(fail=True))
    assert run(failing)["status"] == "FAILED_SAFE"


@pytest.mark.parametrize(
    "mode",
    [ExecutionMode.NO_EXECUTION, ExecutionMode.DEMO_EXECUTION, ExecutionMode.LIVE_EXECUTION],
)
def test_non_shadow_modes_never_advance_or_gain_order_authority(mode: ExecutionMode) -> None:
    runtime, store = orchestrator(mode=mode)
    evidence = run(runtime)
    assert evidence["status"] in {"NO_TRADE", "FAILED_SAFE"}
    assert evidence["execution_mode"] == mode.value
    assert evidence["authorized"] is False
    assert evidence["order_authority"] is False
    assert evidence["broker_order_call_count"] == 0
    assert store.records == {}


def test_runtime_source_has_no_broker_trading_http_verbs() -> None:
    source = Path("src/ig_trader/shadow_runtime.py").read_text(encoding="utf-8")
    for verb in ('"POST"', '"PUT"', '"DELETE"', "/positions", "/workingorders"):
        assert verb not in source
