"""Focused fake-only tests for the Shadow01 read-only stream bridge."""

from __future__ import annotations

import ast
import inspect
import json
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

import src.ig_trader.shadow01.stream_bridge as stream_bridge_module
from src.ig_trader.shadow01.config import load_config
from src.ig_trader.shadow01.registry import ShadowMarketRegistry, load_verified_dq03_registry
from src.ig_trader.shadow01.stream_bridge import (
    ShadowPriceUpdate,
    ShadowReadOnlyStreamBridge,
    ShadowReadOnlyStreamError,
    ShadowStreamDisconnected,
)
from tests.shadow01_dq03_fixtures import write_verified_dq03_documents


class RecordingStreamTransport:
    """In-memory transport that makes every possible stream call observable."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[str, ...]]] = []
        self.updates: list[object] = []
        self.connect_failures_remaining = 0
        self.subscribe_failures_remaining = 0

    def connect(self) -> None:
        self.calls.append(("connect", ()))
        if self.connect_failures_remaining:
            self.connect_failures_remaining -= 1
            raise RuntimeError("sanitized connect failure")

    def subscribe_prices(self, epics: tuple[str, ...]) -> None:
        self.calls.append(("subscribe_prices", epics))
        if self.subscribe_failures_remaining:
            self.subscribe_failures_remaining -= 1
            raise RuntimeError("sanitized subscribe failure")

    def receive_price_update(self, *, timeout_seconds: float) -> ShadowPriceUpdate | None:
        self.calls.append(("receive_price_update", ()))
        assert timeout_seconds >= 0
        if not self.updates:
            return None
        update = self.updates.pop(0)
        if isinstance(update, BaseException):
            raise update
        assert update is None or isinstance(update, ShadowPriceUpdate)
        return update

    def unsubscribe_prices(self, epics: tuple[str, ...]) -> None:
        self.calls.append(("unsubscribe_prices", epics))

    def disconnect(self) -> None:
        self.calls.append(("disconnect", ()))

    # These exist only to prove the bridge cannot reach an execution method.
    def create_position(self) -> None:
        raise AssertionError("an execution method must never be called")

    def close_position(self) -> None:
        raise AssertionError("an execution method must never be called")

    def create_working_order(self) -> None:
        raise AssertionError("an execution method must never be called")

    def update_position(self) -> None:
        raise AssertionError("an execution method must never be called")


@pytest.fixture
def verified_registry(tmp_path: Path) -> ShadowMarketRegistry:
    config = load_config()
    write_verified_dq03_documents(tmp_path, config)
    return load_verified_dq03_registry(config, tmp_path / "instrument_registry.json")


def _epic(registry: ShadowMarketRegistry, symbol: str) -> str:
    value = registry.by_symbol(symbol).epic
    assert isinstance(value, str)
    return value


def _update(epic: str) -> ShadowPriceUpdate:
    return ShadowPriceUpdate(
        epic=epic,
        bid_value="1.0000",
        ask_value="1.0002",
        timestamp_milliseconds=int(datetime(2026, 8, 29, 17, 10, tzinfo=UTC).timestamp() * 1000),
        market_state="DEAL",
    )


def _receive(bridge: ShadowReadOnlyStreamBridge):
    return bridge.receive_price_update(
        observed_at=datetime(2026, 8, 29, 17, 10, tzinfo=UTC),
        maximum_age_seconds=60,
    )


def test_bridge_uses_only_injected_read_only_lifecycle_calls(
    verified_registry: ShadowMarketRegistry,
) -> None:
    transport = RecordingStreamTransport()
    bridge = ShadowReadOnlyStreamBridge(verified_registry, transport)
    eurusd = _epic(verified_registry, "EURUSD")
    us500 = _epic(verified_registry, "US500")

    assert bridge.execution_authority == "OFF"
    assert bridge.connected is False
    assert bridge.subscribed_epics == ()
    assert transport.calls == []

    bridge.connect()
    bridge.subscribe_prices((eurusd, us500))
    transport.updates.append(_update(eurusd))

    quote = _receive(bridge)
    assert quote is not None
    assert quote.quality == "VALID_QUOTE"
    assert quote.epic == eurusd
    bridge.unsubscribe_prices((us500,))
    bridge.disconnect()

    assert bridge.connected is False
    assert bridge.subscribed_epics == ()
    assert transport.calls == [
        ("connect", ()),
        ("subscribe_prices", (eurusd, us500)),
        ("receive_price_update", ()),
        ("unsubscribe_prices", (us500,)),
        ("disconnect", ()),
    ]


def test_all_twenty_verified_epics_can_be_registered_before_transport(
    verified_registry: ShadowMarketRegistry,
) -> None:
    transport = RecordingStreamTransport()
    bridge = ShadowReadOnlyStreamBridge(verified_registry, transport)
    epics = tuple(market.epic for market in verified_registry.markets)
    assert all(isinstance(epic, str) for epic in epics)
    bridge.connect()
    bridge.subscribe_prices(epics)

    assert len(bridge.subscribed_epics) == 20
    assert transport.calls == [("connect", ()), ("subscribe_prices", epics)]


def test_unknown_or_malformed_epics_fail_before_transport(
    verified_registry: ShadowMarketRegistry,
) -> None:
    transport = RecordingStreamTransport()
    bridge = ShadowReadOnlyStreamBridge(verified_registry, transport)
    bridge.connect()

    with pytest.raises(ShadowReadOnlyStreamError, match="SHADOW01_STREAM_EPIC_NOT_VERIFIED"):
        bridge.subscribe_prices(("TEST.UNKNOWN",))
    with pytest.raises(ShadowReadOnlyStreamError, match="SHADOW01_STREAM_EPIC_COLLECTION_INVALID"):
        bridge.subscribe_prices((" TEST.UNKNOWN ",))

    assert transport.calls == [("connect", ())]


def test_bridge_requires_all_twenty_available_registry_bound_markets(tmp_path: Path) -> None:
    config = load_config()
    write_verified_dq03_documents(tmp_path, config)
    registry_path = tmp_path / "instrument_registry.json"
    document = json.loads(registry_path.read_text(encoding="utf-8"))
    assert isinstance(document, dict)
    entries = document["instruments"]
    assert isinstance(entries, list)
    for entry in entries:
        if isinstance(entry, dict) and entry.get("canonical_symbol") == "XAUUSD":
            entry["classification"] = "UNAVAILABLE"
    registry_path.write_text(json.dumps(document), encoding="utf-8")
    registry = load_verified_dq03_registry(config, registry_path)
    transport = RecordingStreamTransport()

    with pytest.raises(
        ShadowReadOnlyStreamError,
        match="SHADOW01_STREAM_MARKET_UNAVAILABLE",
    ):
        ShadowReadOnlyStreamBridge(registry, transport)

    assert transport.calls == []


def test_bridge_requires_exact_twenty_market_registry_before_transport(
    verified_registry: ShadowMarketRegistry,
) -> None:
    incomplete = replace(verified_registry, markets=verified_registry.markets[:-1])
    transport = RecordingStreamTransport()

    with pytest.raises(ShadowReadOnlyStreamError, match="SHADOW01_STREAM_REGISTRY_SCOPE_INVALID"):
        ShadowReadOnlyStreamBridge(incomplete, transport)

    assert transport.calls == []


@pytest.mark.parametrize(
    "operation",
    (
        "create_position",
        "close_position",
        "create_order",
        "close_order",
        "modify_order",
        "working_orders",
        "create_working_order",
        "update_working_order",
        "delete_working_order",
        "modify_position",
        "update_position",
    ),
)
def test_execution_operation_names_are_denied_locally_before_transport(
    verified_registry: ShadowMarketRegistry,
    operation: str,
) -> None:
    transport = RecordingStreamTransport()
    bridge = ShadowReadOnlyStreamBridge(verified_registry, transport)

    with pytest.raises(ShadowReadOnlyStreamError, match="SHADOW01_STREAM_OPERATION_DENIED"):
        getattr(bridge, operation)()

    assert transport.calls == []


@pytest.mark.parametrize(
    "operation",
    (
        "POST /positions/otc",
        "DELETE /positions/otc",
        "POST /workingorders/otc",
        "PUT /workingorders/ID",
    ),
)
def test_stream_operation_allowlist_rejects_execution_routes_before_transport(
    verified_registry: ShadowMarketRegistry,
    operation: str,
) -> None:
    transport = RecordingStreamTransport()
    bridge = ShadowReadOnlyStreamBridge(verified_registry, transport)

    with pytest.raises(ShadowReadOnlyStreamError, match="SHADOW01_STREAM_OPERATION_DENIED"):
        bridge._require_allowed_stream_operation(operation)

    assert transport.calls == []


def test_disconnect_then_bounded_reconnect_restores_only_prior_verified_subscriptions(
    verified_registry: ShadowMarketRegistry,
) -> None:
    transport = RecordingStreamTransport()
    bridge = ShadowReadOnlyStreamBridge(verified_registry, transport, max_reconnect_attempts=2)
    epics = tuple(
        _epic(verified_registry, symbol) for symbol in ("EURUSD", "USDJPY", "XAUUSD", "US500")
    )

    bridge.connect()
    bridge.subscribe_prices(epics)
    transport.updates.append(ShadowStreamDisconnected("sanitized drop"))

    with pytest.raises(ShadowStreamDisconnected):
        _receive(bridge)
    assert bridge.connected is False
    assert bridge.subscribed_epics == epics

    bridge.reconnect_and_restore()

    assert bridge.connected is True
    assert bridge.subscribed_epics == epics
    assert transport.calls == [
        ("connect", ()),
        ("subscribe_prices", epics),
        ("receive_price_update", ()),
        ("connect", ()),
        ("subscribe_prices", epics),
    ]


def test_generic_transport_failure_marks_bridge_disconnected_before_recovery(
    verified_registry: ShadowMarketRegistry,
) -> None:
    transport = RecordingStreamTransport()
    bridge = ShadowReadOnlyStreamBridge(verified_registry, transport, max_reconnect_attempts=1)
    epics = (_epic(verified_registry, "EURUSD"),)
    bridge.connect()
    bridge.subscribe_prices(epics)
    transport.updates.append(RuntimeError("sanitized receive failure"))

    with pytest.raises(ShadowReadOnlyStreamError, match="SHADOW01_STREAM_RECEIVE_FAILED"):
        _receive(bridge)

    assert bridge.connected is False
    assert bridge.subscribed_epics == epics
    bridge.reconnect_and_restore()
    assert bridge.connected is True
    assert bridge.subscribed_epics == epics


def test_reconnect_attempts_are_bounded_and_do_not_restore_after_manual_disconnect(
    verified_registry: ShadowMarketRegistry,
) -> None:
    transport = RecordingStreamTransport()
    bridge = ShadowReadOnlyStreamBridge(verified_registry, transport, max_reconnect_attempts=2)
    epics = (_epic(verified_registry, "EURUSD"),)

    bridge.connect()
    bridge.subscribe_prices(epics)
    transport.updates.append(ShadowStreamDisconnected("sanitized drop"))
    with pytest.raises(ShadowStreamDisconnected):
        _receive(bridge)
    transport.connect_failures_remaining = 2

    with pytest.raises(ShadowReadOnlyStreamError, match="SHADOW01_STREAM_RECONNECT_EXHAUSTED"):
        bridge.reconnect_and_restore()

    assert bridge.connected is False
    assert bridge.subscribed_epics == epics
    assert transport.calls == [
        ("connect", ()),
        ("subscribe_prices", epics),
        ("receive_price_update", ()),
        ("connect", ()),
        ("disconnect", ()),
        ("connect", ()),
        ("disconnect", ()),
    ]

    with pytest.raises(ShadowReadOnlyStreamError, match="SHADOW01_STREAM_RECONNECT_UNAVAILABLE"):
        bridge.reconnect_and_restore()

    bridge.disconnect()
    assert bridge.subscribed_epics == ()
    with pytest.raises(ShadowReadOnlyStreamError, match="SHADOW01_STREAM_RECONNECT_UNAVAILABLE"):
        bridge.reconnect_and_restore()


def test_unknown_incoming_epic_fails_closed_without_a_second_transport_call(
    verified_registry: ShadowMarketRegistry,
) -> None:
    transport = RecordingStreamTransport()
    bridge = ShadowReadOnlyStreamBridge(verified_registry, transport)
    eurusd = _epic(verified_registry, "EURUSD")
    bridge.connect()
    bridge.subscribe_prices((eurusd,))
    transport.updates.append(_update("TEST.UNKNOWN"))

    with pytest.raises(ShadowReadOnlyStreamError, match="SHADOW01_STREAM_EPIC_NOT_VERIFIED"):
        _receive(bridge)

    assert transport.calls == [
        ("connect", ()),
        ("subscribe_prices", (eurusd,)),
        ("receive_price_update", ()),
    ]


def test_bridge_exposes_only_value_free_callback_and_rejection_diagnostics(
    verified_registry: ShadowMarketRegistry,
) -> None:
    transport = RecordingStreamTransport()
    eurusd = _epic(verified_registry, "EURUSD")
    transport.field_contract_diagnostic = lambda epic: {
        "callback_observed": epic == eurusd,
        "item_name_recognized": True,
        "BIDPRICE1": {
            "present": True,
            "runtime_type": "str",
            "is_none": False,
            "is_numeric_string": True,
            "is_numeric_object": False,
            "parse_success": True,
            "unsafe_value": "must-not-appear",
        },
        "ASKPRICE1": {},
        "TIMESTAMP": {
            "present": True,
            "runtime_type": "str",
            "is_none": False,
            "is_numeric_string": False,
            "is_numeric_object": False,
            "parse_success": True,
            "is_digit_string": True,
            "string_length": 13,
            "milliseconds_plausible": True,
        },
        "is_snapshot": True,
        "changed_field_names": ["TIMESTAMP", "unsafe-key"],
    }
    transport.invalid_reason_counts = lambda epic: {
        "invalid_timestamp": 2 if epic == eurusd else 0,
        "unsafe": 99,
    }
    bridge = ShadowReadOnlyStreamBridge(verified_registry, transport)
    bridge.connect()
    bridge.subscribe_prices((eurusd,))

    diagnostics = bridge.field_contract_diagnostics

    assert diagnostics[0]["symbol"] == "EURUSD"
    assert diagnostics[0]["BIDPRICE1"]["parse_success"] is True
    assert diagnostics[0]["TIMESTAMP"]["string_length"] == 13
    assert diagnostics[0]["changed_field_names"] == ["TIMESTAMP"]
    assert bridge.invalid_reason_counts["invalid_timestamp"] == 2
    assert "must-not-appear" not in str(diagnostics)
    assert "unsafe" not in str(diagnostics)


def test_public_surface_has_only_the_reviewed_stream_operations_and_no_execution_imports() -> None:
    source_path = Path(inspect.getsourcefile(stream_bridge_module) or "")
    source = source_path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported_modules = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module is not None
    }
    imported_modules.update(
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    )
    public_methods = {
        name
        for name, value in vars(ShadowReadOnlyStreamBridge).items()
        if isinstance(value, Callable) and not name.startswith("_")
    }

    assert public_methods == {
        "connect",
        "subscribe_prices",
        "receive_price_update",
        "unsubscribe_prices",
        "disconnect",
        "reconnect_and_restore",
        "reconnect_representative_prices",
    }
    assert not any(
        module.startswith(
            (
                "src.ig_trader.demo",
                "src.ig_trader.execution",
                "src.ig_trader.session",
                "src.ig_trader.streaming",
                "lightstreamer",
            )
        )
        for module in imported_modules
    )
    assert "sleep(" not in source
    assert "settings" not in imported_modules
