import os
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pytest

from src.ig_trader.db_bootstrap import apply_required_migrations, load_migration_sources
from src.ig_trader.execution_lease import (
    ExecutionLeaseCoordinator,
    PostgresExecutionLeaseStore,
)
from src.ig_trader.shadow_execution import (
    ExecutionMode,
    InMemoryShadowStore,
    InstrumentRegistry,
    MarketQuote,
    ShadowExecutionCore,
    ShadowLifecycle,
)
from src.ig_trader.shadow_postgres_store import PostgresShadowStore
from src.ig_trader.shadow_runtime import ShadowRuntimeOrchestrator

NOW = datetime(2026, 8, 21, 12, tzinfo=UTC)
EPIC = "CS.D.EURGBP.MINI.IP"
DEFAULT_QUOTE = MarketQuote(0.8498, 0.8500, NOW)
MIGRATIONS = Path(__file__).resolve().parents[1] / "migrations" / "postgresql"


class Lease:
    authorized = True
    fencing_token = 11


class Market:
    def __init__(self, value: MarketQuote | None, epic: str = EPIC) -> None:
        self.value = value
        self.epic = epic

    def quote(self, epic: str, *, as_of: datetime) -> MarketQuote | None:
        assert epic == self.epic and as_of == NOW
        return self.value


class Strategy:
    def __init__(
        self,
        direction: str = "BUY",
        fail: bool = False,
        signal_epic: str | None = None,
    ) -> None:
        self.direction = direction
        self.fail = fail
        self.signal_epic = signal_epic

    def generate_signal(self, epic: str, market_frame: object) -> SimpleNamespace:
        if self.fail:
            raise RuntimeError("strategy unavailable")
        return SimpleNamespace(
            direction=SimpleNamespace(value=self.direction),
            strategy_name="S0",
            epic=self.signal_epic or epic,
        )


def orchestrator(
    *,
    mode: ExecutionMode = ExecutionMode.SHADOW_DEMO,
    market: MarketQuote | None = DEFAULT_QUOTE,
    strategy: Strategy | None = None,
    risk_gate=lambda *_a, **_k: True,
    epic: str = EPIC,
    store: InMemoryShadowStore | None = None,
) -> tuple[ShadowRuntimeOrchestrator, InMemoryShadowStore]:
    lease = Lease()
    store = store or InMemoryShadowStore(lease.fencing_token)
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
            epic=epic,
            market_data=Market(market, epic),
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
    first, store = orchestrator()
    assert run(first, "cycle-existing")["status"] == "SHADOW_OPEN"
    runtime, _store = orchestrator(strategy=Strategy(fail=True), store=store)
    evidence = run(runtime, "cycle-limit")
    assert evidence["reason"] == "SHADOW_V1_POSITION_LIMIT"
    assert len(store.records) == 1


@pytest.mark.parametrize("count", [-1, True, None])
def test_unavailable_or_ambiguous_active_count_fails_closed(count: object) -> None:
    runtime, store = orchestrator()
    store.active_position_count = lambda: count  # type: ignore[method-assign,return-value]

    assert run(runtime)["status"] == "FAILED_SAFE"
    assert store.records == {}


def test_active_position_count_error_fails_closed() -> None:
    runtime, store = orchestrator()

    def unavailable() -> int:
        raise RuntimeError("durable state unavailable")

    store.active_position_count = unavailable  # type: ignore[method-assign]
    assert run(runtime)["status"] == "FAILED_SAFE"
    assert store.records == {}


def test_signal_must_match_orchestrator_instrument() -> None:
    runtime, store = orchestrator(strategy=Strategy(signal_epic="CS.D.EURUSD.CEFM.IP"))

    evidence = run(runtime)
    assert evidence["reason"] == "SIGNAL_INSTRUMENT_MISMATCH"
    assert store.records == {}


def test_same_global_cycle_across_instruments_creates_at_most_one_intent() -> None:
    first, store = orchestrator()
    second, _store = orchestrator(
        epic="CS.D.EURUSD.CEFM.IP",
        store=store,
    )

    winner = run(first, "global-cycle-1")
    rejected = run(second, "global-cycle-1")
    assert winner["status"] == "SHADOW_OPEN"
    assert rejected["reason"] == "GLOBAL_CYCLE_ALREADY_CLAIMED"
    assert len(store.records) == 1


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

    workflow = Path(".github/workflows/ci.yaml").read_text(encoding="utf-8")
    assert "tests-g4c-shadow-runtime-postgres.xml" in workflow
    assert "test_postgres_shadow_runtime_end_to_end" in workflow


def _local_postgres_dsn() -> str:
    if os.environ.get("RUN_POSTGRES_INTEGRATION") != "1":
        pytest.skip("disposable PostgreSQL integration gate is disabled")
    dsn = os.environ.get("TEST_POSTGRES_DSN", "").strip()
    if not dsn:
        pytest.skip("disposable PostgreSQL DSN is unavailable")
    from psycopg.conninfo import conninfo_to_dict

    values = conninfo_to_dict(dsn)
    if values.get("host") not in {"127.0.0.1", "localhost"}:
        pytest.fail("shadow-runtime test refuses non-loopback PostgreSQL")
    if values.get("dbname") != "postgres" or values.get("user") != "postgres":
        pytest.fail("shadow-runtime test requires the disposable CI database")
    return dsn


def test_postgres_shadow_runtime_end_to_end() -> None:
    admin_dsn = _local_postgres_dsn()
    import psycopg
    from psycopg.conninfo import make_conninfo

    with psycopg.connect(admin_dsn, autocommit=True) as admin:
        admin.execute("DROP DATABASE IF EXISTS ig_trader")
        admin.execute("CREATE DATABASE ig_trader")
    app_dsn = make_conninfo(admin_dsn, dbname="ig_trader")

    def connection_factory():
        return psycopg.connect(app_dsn)

    with connection_factory() as connection:
        apply_required_migrations(
            connection,
            load_migration_sources(MIGRATIONS),
            "ephemeral-shadow-runtime-test",
        )

    lease_store = PostgresExecutionLeaseStore(connection_factory)
    coordinator = ExecutionLeaseCoordinator(
        store=lease_store,
        replica_instance_id="shadow-runtime-leader",
        execution_enabled=True,
        ttl_seconds=30,
    )
    assert coordinator.try_acquire()

    def postgres_runtime(
        *,
        mode: ExecutionMode = ExecutionMode.SHADOW_DEMO,
        epic: str = EPIC,
        risk_gate=lambda *_a, **_k: True,
        lease: object = coordinator,
    ) -> tuple[ShadowRuntimeOrchestrator, PostgresShadowStore, ShadowExecutionCore]:
        store = PostgresShadowStore(lease, connection_factory)  # type: ignore[arg-type]
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
                epic=epic,
                market_data=Market(DEFAULT_QUOTE, epic),
                strategy=Strategy(),
                shadow=core,
            ),
            store,
            core,
        )

    runtime, store, _core = postgres_runtime()
    opened_evidence = run(runtime, "postgres-global-cycle")
    intent_id = UUID(str(opened_evidence["intent_id"]))
    assert opened_evidence["status"] == "SHADOW_OPEN"
    assert store.active_position_count() == 1

    restarted, restarted_store, restarted_core = postgres_runtime()
    opened = restarted_store.get(intent_id)
    assert opened is not None and opened.lifecycle is ShadowLifecycle.OPEN
    closed = restarted_core.close_on_quote(
        opened,
        MarketQuote(0.8510, 0.8512, NOW),
        now=NOW,
    )
    assert closed.lifecycle is ShadowLifecycle.CLOSED

    recovered, recovered_store, _recovered_core = postgres_runtime()
    reconciled = recovered.recover(intent_id, now=NOW, quote=None)
    assert reconciled["status"] == "SHADOW_RECONCILED"
    assert reconciled["performance"]["exit_price"] == 0.8510
    assert reconciled["performance"]["pips"] == pytest.approx(10.0)
    assert recovered_store.active_position_count() == 0

    risk_veto, _risk_store, _risk_core = postgres_runtime(risk_gate=lambda *_a, **_k: False)
    assert run(risk_veto, "risk-veto-cycle")["status"] == "FAILED_SAFE"

    for mode in (
        ExecutionMode.NO_EXECUTION,
        ExecutionMode.DEMO_EXECUTION,
        ExecutionMode.LIVE_EXECUTION,
    ):
        disabled, _disabled_store, _disabled_core = postgres_runtime(mode=mode)
        assert run(disabled, f"disabled-{mode.value}")["status"] in {
            "NO_TRADE",
            "FAILED_SAFE",
        }

    same_cycle, _same_store, _same_core = postgres_runtime()
    assert run(same_cycle, "postgres-global-cycle")["reason"] == "IDEMPOTENT_GLOBAL_CYCLE"
    second_instrument, _second_store, _second_core = postgres_runtime(epic="CS.D.EURUSD.CEFM.IP")
    assert (
        run(second_instrument, "postgres-global-cycle")["reason"] == "GLOBAL_CYCLE_ALREADY_CLAIMED"
    )

    stale_lease = coordinator.lease
    assert stale_lease is not None and coordinator.release()
    successor = ExecutionLeaseCoordinator(
        store=lease_store,
        replica_instance_id="shadow-runtime-successor",
        execution_enabled=True,
        ttl_seconds=30,
    )
    assert successor.try_acquire()
    stale_coordinator = SimpleNamespace(
        authorized=True,
        lease=stale_lease,
        run_state_change=lambda operation, callback: lease_store.run_fenced(
            stale_lease,
            operation,
            callback,
        ),
    )
    stale_runtime, _stale_store, _stale_core = postgres_runtime(lease=stale_coordinator)
    assert run(stale_runtime, "stale-fence-cycle")["status"] == "FAILED_SAFE"

    with connection_factory() as connection:
        assert (
            connection.execute(
                "SELECT count(*) FROM trading.trade_intents WHERE execution_mode = 'SHADOW_DEMO'"
            ).fetchone()[0]
            == 1
        )
        assert (
            connection.execute("SELECT count(*) FROM trading.shadow_position_state").fetchone()[0]
            == 1
        )
