"""Offline safety tests for DQ-02 durable state, controller, and P&L."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from src.ig_trader.demo_execution import (
    DemoDirection,
    DemoExecutionLifecycle,
    DemoExecutionRecord,
    DemoExecutionRequest,
    DemoOrderType,
    DemoPosition,
    DemoRiskApproval,
)
from src.ig_trader.demo_operator import (
    DemoOperatorError,
    DemoRobotController,
    LocalDemoOperatorConfig,
    discover_research_universe,
)
from src.ig_trader.demo_pnl import PnlContract, PositionMark, calculate_position_pnl
from src.ig_trader.demo_qualification_evaluator import DemoTradeOutcome, evaluate_demo_results
from src.ig_trader.demo_store import SQLiteDemoExecutionStore
from src.ig_trader.demo_stream import DemoPriceStream, DemoStreamingError
from src.ig_trader.demo_transport import IGDemoAccount

NOW = datetime(2026, 8, 23, 14, 34, 36, tzinfo=UTC)


def _request(
    intent_id: UUID | None = None, deal_reference: str | None = None
) -> DemoExecutionRequest:
    intent = intent_id or uuid4()
    return DemoExecutionRequest(
        intent_id=intent,
        global_cycle_id=uuid4(),
        epic="CS.D.EURGBP.MINI.IP",
        direction=DemoDirection.BUY,
        size=Decimal("1"),
        currency_code="GBP",
        expiry="DFB",
        order_type=DemoOrderType.MARKET,
        force_open=True,
        guaranteed_stop=False,
        stop_distance=Decimal("2"),
        stop_level=None,
        limit_distance=Decimal("3"),
        limit_level=None,
        deal_reference=deal_reference or f"DQ02_{intent.hex[:12]}",
        configuration_identity="dq02-test",
        risk_approval=DemoRiskApproval("dq02-test", True, NOW),
        fencing_token=7,
        created_at=NOW,
    )


class FakeTransport:
    def __init__(self) -> None:
        self.account_calls = 0
        self.position_calls = 0

    def get_account(self) -> IGDemoAccount:
        self.account_calls += 1
        return IGDemoAccount("DEMO-EXPECTED", "GBP", Decimal("1000"), Decimal("900"), Decimal("0"))

    def list_position_details(self) -> tuple[object, ...]:
        self.position_calls += 1
        return ()

    def list_positions(self) -> tuple[DemoPosition, ...]:
        self.position_calls += 1
        return ()

    def get_confirmation(self, _deal_reference: str) -> None:
        return None


def _config() -> LocalDemoOperatorConfig:
    return LocalDemoOperatorConfig(
        base_url="https://demo-api.ig.com/gateway/deal",
        expected_demo_account_id="DEMO-EXPECTED",
        control_enabled=True,
        hosted=False,
        credentials_available=True,
    )


def _controller(tmp_path: Path, transport: FakeTransport) -> DemoRobotController:
    return DemoRobotController(
        config=_config(),
        store=SQLiteDemoExecutionStore(tmp_path / "demo_execution.sqlite"),
        transport_factory=lambda: transport,  # type: ignore[arg-type]
        worker_launcher=lambda _path: 4242,
        snapshot_path=tmp_path / "operator_snapshot.json",
    )


def test_sqlite_store_survives_restart_and_rejects_duplicate_deal_reference(tmp_path: Path) -> None:
    path = tmp_path / "demo_execution.sqlite"
    request = _request(UUID("00000000-0000-0000-0000-000000000001"), "DQ02_UNIQUE")
    store = SQLiteDemoExecutionStore(path)
    stored = store.put(DemoExecutionRecord(request, DemoExecutionLifecycle.PREPARED))

    restarted = SQLiteDemoExecutionStore(path)
    assert restarted.get(request.intent_id) == stored
    assert restarted.put(stored) == stored
    with pytest.raises(Exception, match="deal reference"):
        restarted.put(
            DemoExecutionRecord(
                _request(UUID("00000000-0000-0000-0000-000000000002"), "DQ02_UNIQUE"),
                DemoExecutionLifecycle.PREPARED,
            )
        )


def test_sqlite_store_lists_restart_sensitive_lifecycles_only(tmp_path: Path) -> None:
    store = SQLiteDemoExecutionStore(tmp_path / "demo_execution.sqlite")
    pending = DemoExecutionRecord(_request(), DemoExecutionLifecycle.SUBMITTED)
    complete = DemoExecutionRecord(_request(), DemoExecutionLifecycle.CLOSED_RECONCILED)
    store.put(pending)
    store.put(complete)

    assert store.incomplete_records() == (pending,)


def test_controller_requires_verified_local_mode_and_exact_demo_account(tmp_path: Path) -> None:
    transport = FakeTransport()
    controller = _controller(tmp_path, transport)

    snapshot = controller.preflight()

    assert snapshot.rest_status == "CONNECTED"
    assert snapshot.total_open_positions == 0
    assert "Demo account" in (snapshot.account or "")
    assert transport.account_calls == 1

    mismatch = DemoRobotController(
        config=LocalDemoOperatorConfig(
            **{**_config().__dict__, "expected_demo_account_id": "DIFFERENT"}
        ),
        store=SQLiteDemoExecutionStore(tmp_path / "mismatch.sqlite"),
        transport_factory=lambda: transport,  # type: ignore[arg-type]
        worker_launcher=lambda _path: 4242,
    )
    with pytest.raises(DemoOperatorError, match="does not match"):
        mismatch.start()


def test_controller_starts_one_worker_and_kill_blocks_another_start(tmp_path: Path) -> None:
    transport = FakeTransport()
    controller = _controller(tmp_path, transport)

    started = controller.start()
    assert started.state == "RUNNING"
    assert started.pid == 4242
    with pytest.raises(DemoOperatorError):
        controller.start()

    killed = controller.emergency_kill()
    assert killed.kill_switch_state.value == "BLOCKING"
    with pytest.raises(DemoOperatorError, match="EMERGENCY KILL"):
        controller.start()


def test_pause_keeps_worker_state_distinct_from_stop_and_emergency_kill(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr("src.ig_trader.demo_operator._pid_alive", lambda _pid: True)
    controller = _controller(tmp_path, FakeTransport())
    controller.start()

    paused = controller.pause()
    assert paused.state == "PAUSED"
    assert paused.kill_switch_state.value == "RELEASED"

    resumed = controller.resume()
    assert resumed.state == "RUNNING"
    assert resumed.kill_switch_state.value == "RELEASED"

    controller.pause()

    stopped = controller.stop()
    assert stopped.state == "STOP_REQUESTED"
    assert stopped.kill_switch_state.value == "RELEASED"

    killed = controller.emergency_kill()
    assert killed.state == "SAFE_STOP"
    assert killed.kill_switch_state.value == "BLOCKING"


def test_paused_worker_continues_reconciliation_without_enabling_entries(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr("src.ig_trader.demo_operator._pid_alive", lambda _pid: True)
    transport = FakeTransport()
    controller = _controller(tmp_path, transport)
    controller.start()
    controller.pause()
    position_reads_before = transport.position_calls

    state = controller.worker_once(4242)

    assert state.state == "PAUSED"
    assert transport.position_calls > position_reads_before
    snapshot = controller.snapshot_path.read_text(encoding="utf-8")
    assert '"robot_state":"PAUSED"' in snapshot
    assert "New entries paused; monitoring and reconciliation remain active." in snapshot

    controller.emergency_kill()
    with pytest.raises(DemoOperatorError, match="EMERGENCY KILL"):
        controller.resume()


def test_worker_reconciles_only_and_stale_prices_block_entries(tmp_path: Path) -> None:
    transport = FakeTransport()
    controller = _controller(tmp_path, transport)
    controller.start()

    state = controller.worker_once(4242)

    assert state.state == "RUNNING"
    snapshot = controller.snapshot_path.read_text(encoding="utf-8")
    assert "STALE PRICE FEED" in snapshot
    assert "no qualified execution registration exists" in snapshot


def test_pnl_marks_buy_with_bid_sell_with_offer_and_keeps_native_currency() -> None:
    contract = PnlContract(
        pip_or_tick_size=Decimal("0.01"),
        value_of_one_pip=Decimal("2"),
        position_currency="GBP",
        account_currency="EUR",
    )
    buy = calculate_position_pnl(
        PositionMark(
            DemoDirection.BUY, Decimal("3"), Decimal("100"), Decimal("101"), Decimal("102")
        ),
        contract,
    )
    sell = calculate_position_pnl(
        PositionMark(
            DemoDirection.SELL, Decimal("3"), Decimal("100"), Decimal("101"), Decimal("99")
        ),
        contract,
    )

    assert buy.mark_price == Decimal("101")
    assert sell.mark_price == Decimal("99")
    assert buy.native_pnl == sell.native_pnl == Decimal("600")
    assert buy.status == sell.status == "ACCOUNT_CURRENCY_PNL_UNAVAILABLE"


def test_demo_qualification_needs_sample_and_risk_evidence_not_profit_alone() -> None:
    tiny = evaluate_demo_results(
        (DemoTradeOutcome("EURGBP", "S3", "1.0.0", Decimal("10"), Decimal("1"), None, None, None),),
        minimum_trade_count=2,
    )
    risky = evaluate_demo_results(
        tuple(
            DemoTradeOutcome(
                "EURGBP", "S3", "1.0.0", Decimal("1"), Decimal(value), None, None, None
            )
            for value in ("5", "-3", "-3", "2")
        ),
        minimum_trade_count=4,
    )

    assert tiny.classification.value == "DEMO_LOW_SAMPLE"
    assert risky.net_pnl > 0
    assert risky.classification.value == "DEMO_WATCH"


def test_discovery_keeps_all_research_instruments_visible_without_inventing_epics() -> None:
    class EmptyDiscoveryTransport:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def search_markets(self, symbol: str) -> tuple[dict[str, object], ...]:
            self.calls.append(symbol)
            return ()

    transport = EmptyDiscoveryTransport()
    results = discover_research_universe(transport, request_budget=26)  # type: ignore[arg-type]

    assert len(results) == 26
    assert {row["classification"] for row in results} == {"NOT_FOUND"}
    assert all(row["epic"] is None for row in results)
    assert len(transport.calls) == 26


def test_one_demo_streaming_session_rejects_stale_data_and_duplicate_subscriptions() -> None:
    class FakeConnection:
        def setUser(self, _value: str) -> None:  # noqa: N802
            pass

        def setPassword(self, _value: str) -> None:  # noqa: N802
            pass

    class FakeClient:
        def __init__(self, _server_address: str, _adapter_set: object) -> None:
            self.connectionDetails = FakeConnection()
            self.subscriptions: list[object] = []
            self.listeners: list[object] = []

        def addListener(self, listener: object) -> None:  # noqa: N802
            self.listeners.append(listener)

        def connect(self) -> None:
            pass

        def disconnect(self) -> None:
            pass

        def subscribe(self, item: object) -> None:
            self.subscriptions.append(item)

    class FakeSubscription:
        def __init__(self, _mode: str, _items: list[str], _fields: list[str]) -> None:
            self.listener: object | None = None

        def addListener(self, listener: object) -> None:  # noqa: N802
            self.listener = listener

    class Tokens:
        account_id = "DEMO-TEST"
        cst = "cst"
        x_security_token = "xst"

    with pytest.raises(DemoStreamingError):
        DemoPriceStream(
            endpoint="https://demo-stream.example.test",
            session=Tokens(),
            rest_demo_proven=False,
            client_factory=FakeClient,
            subscription_factory=FakeSubscription,
        )
    stream = DemoPriceStream(
        endpoint="https://demo-stream.example.test",
        session=Tokens(),
        rest_demo_proven=True,
        client_factory=FakeClient,
        subscription_factory=FakeSubscription,
    )
    stream.connect()
    stream.subscribe_prices(("CS.TEST",))
    with pytest.raises(DemoStreamingError, match="another price subscription"):
        stream.subscribe_prices(("CS.OTHER",))
    assert stream.quote("CS.TEST", maximum_age=timedelta(seconds=1)) is None
