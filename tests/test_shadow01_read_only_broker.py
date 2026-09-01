"""Safety tests for the isolated Shadow01 broker-read boundary."""

from __future__ import annotations

import ast
import inspect
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

import src.ig_trader.shadow01.read_only_broker as broker_module
from src.ig_trader.shadow01.read_only_broker import (
    ReadOnlyBrokerError,
    Shadow01ReadOnlyBroker,
)


class RecordingTransport:
    """Record every attempted call so tests can prove pre-transport blocking."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    def authorized_request(self, method: str, endpoint: str, **kwargs: Any) -> dict[str, object]:
        self.calls.append((method, endpoint, kwargs))
        return {"method": method, "endpoint": endpoint}


class CleanupTransport(RecordingTransport):
    """Fake dedicated cleanup surface; it records no generic DELETE route."""

    def __init__(self, *, logout_result: bool = True) -> None:
        super().__init__()
        self.logout_result = logout_result
        self.logout_calls = 0

    def logout(self) -> bool:
        self.logout_calls += 1
        return self.logout_result


class DiagnosticTransport(RecordingTransport):
    """Return a deliberately small diagnostic document for boundary tests."""

    def __init__(self, diagnostic: object) -> None:
        super().__init__()
        self.diagnostic = diagnostic

    def latest_response_diagnostic(self) -> object:
        return self.diagnostic


class StreamMaterialTransport(RecordingTransport):
    """Expose only the dedicated non-REST stream handoff capability."""

    def __init__(self) -> None:
        super().__init__()
        self.material = object()
        self.material_calls = 0

    def stream_session_material(self) -> object:
        self.material_calls += 1
        return self.material


def test_allowlisted_methods_use_only_the_explicit_read_routes() -> None:
    transport = RecordingTransport()
    adapter = Shadow01ReadOnlyBroker(transport)

    assert adapter.authenticate(json={"test": "authentication"}) == {
        "method": "POST",
        "endpoint": "/session",
    }
    assert adapter.read_session()["endpoint"] == "/session"
    assert adapter.read_account()["endpoint"] == "/accounts"
    assert adapter.read_market_catalog()["endpoint"] == "/markets"
    assert adapter.read_market("CS.D.EURUSD.CFD.IP")["endpoint"] == "/markets/CS.D.EURUSD.CFD.IP"
    assert adapter.read_market_schedule_v3("CS.D.EURUSD.CFD.IP") == {"instrument": {}}
    assert set(adapter.read_markets(("CS.A", "CS.B"))) == {"CS.A", "CS.B"}
    assert adapter.read_historical_prices("CS.A", "DAY", 300)["endpoint"] == "/prices/CS.A/DAY/300"

    assert [(method, endpoint) for method, endpoint, _ in transport.calls] == [
        ("POST", "/session"),
        ("GET", "/session"),
        ("GET", "/accounts"),
        ("GET", "/markets"),
        ("GET", "/markets/CS.D.EURUSD.CFD.IP"),
        ("GET", "/markets/CS.D.EURUSD.CFD.IP"),
        ("GET", "/markets/CS.A"),
        ("GET", "/markets/CS.B"),
        ("GET", "/prices/CS.A/DAY/300"),
    ]
    counters = adapter.request_counters
    assert counters.authentication_request_count == 1
    assert counters.session_read_count == 1
    assert counters.account_read_count == 1
    assert counters.market_catalog_read_count == 1
    assert counters.market_read_count == 3
    assert counters.schedule_metadata_read_count == 1
    assert counters.historical_price_read_count == 1
    assert counters.total_rest_request_count == len(transport.calls)
    assert adapter.request_counters_document()["execution_authority"] == "OFF"
    assert counters.execution_safety_document() == {
        "create": 0,
        "close": 0,
        "working_orders": 0,
        "demo_starts": 0,
    }
    assert transport.calls[4][2] == {}
    assert transport.calls[5][2] == {"api_version": "3"}


@pytest.mark.parametrize(
    ("method", "endpoint"),
    (
        ("POST", "/positions/otc"),
        ("DELETE", "/positions/otc"),
        ("GET", "/positions"),
        ("GET", "/confirms/REFERENCE"),
        ("GET", "/markets/CS.A?searchTerm=EURUSD"),
        ("GET", "/prices/CS.A/DAY/0"),
    ),
)
def test_disallowed_requests_are_blocked_before_transport(
    method: str,
    endpoint: str,
) -> None:
    transport = RecordingTransport()
    adapter = Shadow01ReadOnlyBroker(transport)

    with pytest.raises(ReadOnlyBrokerError):
        adapter._request(method, endpoint)

    assert transport.calls == []
    assert adapter.request_counters.blocked_request_count == 1
    assert adapter.request_counters.total_rest_request_count == 0


@pytest.mark.parametrize(
    ("operation", "method", "endpoint"),
    (
        ("create_position", "POST", "/positions/otc"),
        ("close_position", "DELETE", "/positions/otc"),
        ("create_working_order", "POST", "/workingorders/otc"),
        ("modify_working_order", "PUT", "/workingorders/otc/WORKING-ORDER-ID"),
    ),
)
def test_execution_route_matrix_is_blocked_before_transport(
    operation: str,
    method: str,
    endpoint: str,
) -> None:
    """Prove each Gate-01 action family fails before a transport can see it."""

    transport = RecordingTransport()
    adapter = Shadow01ReadOnlyBroker(transport)

    with pytest.raises(ReadOnlyBrokerError):
        adapter._request(method, endpoint)

    assert operation
    assert transport.calls == []
    assert adapter.request_counters.blocked_request_count == 1
    assert adapter.request_counters.total_rest_request_count == 0
    assert adapter.request_counters.execution_safety_document() == {
        "create": 0,
        "close": 0,
        "working_orders": 0,
        "demo_starts": 0,
    }


def test_invalid_path_inputs_and_get_arguments_are_blocked_before_transport() -> None:
    transport = RecordingTransport()
    adapter = Shadow01ReadOnlyBroker(transport)

    with pytest.raises(ReadOnlyBrokerError):
        adapter.read_market("CS.A/../positions")
    with pytest.raises(ReadOnlyBrokerError):
        adapter.read_historical_prices("CS.A", "DAY", 0)
    with pytest.raises(ReadOnlyBrokerError):
        adapter._request("GET", "/markets", params={"searchTerm": "EURUSD"})

    assert transport.calls == []
    assert adapter.request_counters.blocked_request_count == 3


def test_v3_schedule_read_discards_prices_and_cannot_replace_v4_metadata() -> None:
    class V3Transport(RecordingTransport):
        def authorized_request(
            self, method: str, endpoint: str, **kwargs: Any
        ) -> dict[str, object]:
            self.calls.append((method, endpoint, kwargs))
            return {
                "instrument": {
                    "epic": "wrong-if-used",
                    "openingHours": {"marketTimes": [{"openTime": "00:00", "closeTime": "23:59"}]},
                },
                "snapshot": {"bid": "must-not-escape", "offer": "must-not-escape"},
            }

    transport = V3Transport()
    adapter = Shadow01ReadOnlyBroker(transport)

    schedule = adapter.read_market_schedule_v3("CS.TEST")

    assert schedule == {
        "instrument": {
            "openingHours": {"marketTimes": [{"openTime": "00:00", "closeTime": "23:59"}]}
        }
    }
    assert "must-not-escape" not in str(schedule)
    assert "wrong-if-used" not in str(schedule)
    assert transport.calls == [("GET", "/markets/CS.TEST", {"api_version": "3"})]


def test_stream_subscription_uses_only_the_injected_factory() -> None:
    transport = RecordingTransport()
    factory_calls: list[tuple[str, ...]] = []

    def subscription_factory(epics: tuple[str, ...]) -> object:
        factory_calls.append(epics)
        return object()

    adapter = Shadow01ReadOnlyBroker(
        transport,
        stream_subscription_factory=subscription_factory,
    )

    subscription = adapter.subscribe_prices(("CS.B", "CS.A"))

    assert subscription is not None
    assert factory_calls == [("CS.B", "CS.A")]
    assert transport.calls == []
    assert adapter.request_counters.streaming_subscription_count == 1


def test_stream_operations_without_factories_block_without_transport() -> None:
    transport = RecordingTransport()
    adapter = Shadow01ReadOnlyBroker(transport)

    with pytest.raises(ReadOnlyBrokerError):
        adapter.subscribe_prices(("CS.A",))
    with pytest.raises(ReadOnlyBrokerError):
        adapter.reconnect_prices(("CS.A",))

    assert transport.calls == []
    assert adapter.request_counters.streaming_subscription_count == 0
    assert adapter.request_counters.streaming_reconnection_count == 0
    assert adapter.request_counters.blocked_request_count == 2


def test_stream_session_material_uses_only_the_dedicated_transport_capability() -> None:
    transport = StreamMaterialTransport()
    adapter = Shadow01ReadOnlyBroker(transport)

    assert adapter.stream_session_material() is transport.material

    assert transport.material_calls == 1
    assert transport.calls == []
    assert adapter.request_counters.total_rest_request_count == 0


def test_stream_reconnection_uses_only_an_injected_factory() -> None:
    transport = RecordingTransport()
    reconnect_calls: list[tuple[str, ...]] = []

    def reconnect_factory(epics: tuple[str, ...]) -> object:
        reconnect_calls.append(epics)
        return object()

    adapter = Shadow01ReadOnlyBroker(
        transport,
        stream_reconnection_factory=reconnect_factory,
    )

    reconnection = adapter.reconnect_prices(("CS.B", "CS.A"))

    assert reconnection is not None
    assert reconnect_calls == [("CS.B", "CS.A")]
    assert transport.calls == []
    assert adapter.request_counters.streaming_reconnection_count == 1
    assert adapter.request_counters.streaming_subscription_count == 0


def test_dedicated_session_logout_never_falls_back_to_a_generic_delete_request() -> None:
    transport = CleanupTransport()
    adapter = Shadow01ReadOnlyBroker(transport)

    assert adapter.logout() is True

    assert transport.logout_calls == 1
    assert transport.calls == []
    assert adapter.request_counters.session_logout_count == 1
    assert adapter.request_counters.total_rest_request_count == 1


def test_session_logout_without_a_dedicated_transport_surface_is_blocked_locally() -> None:
    transport = RecordingTransport()
    adapter = Shadow01ReadOnlyBroker(transport)

    with pytest.raises(ReadOnlyBrokerError, match="logout is unavailable"):
        adapter.logout()

    assert transport.calls == []
    assert adapter.request_counters.session_logout_count == 0
    assert adapter.request_counters.blocked_request_count == 1


@pytest.mark.parametrize(
    ("diagnostic", "expected"),
    (
        (
            {
                "status_code": 403,
                "upstream_error_code": "error.public-api.access-denied",
                "response_body": "must-not-leave-transport",
            },
            {
                "status_code": 403,
                "upstream_error_code": "error.public-api.access-denied",
            },
        ),
        ({"status_code": 403, "upstream_error_code": "unsafe error value"}, None),
        ({"status_code": 200, "upstream_error_code": None}, None),
    ),
)
def test_response_diagnostic_boundary_returns_only_validated_safe_fields(
    diagnostic: object,
    expected: dict[str, int | str | None] | None,
) -> None:
    adapter = Shadow01ReadOnlyBroker(DiagnosticTransport(diagnostic))

    assert adapter.latest_response_diagnostic() == expected
    assert "must-not-leave-transport" not in str(adapter.latest_response_diagnostic())


def test_module_has_no_broker_action_surface_or_execution_import() -> None:
    source_path = Path(inspect.getsourcefile(broker_module) or "")
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
        for name, value in vars(Shadow01ReadOnlyBroker).items()
        if isinstance(value, Callable) and not name.startswith("_")
    }

    assert not any(
        module.startswith(
            (
                "src.ig_trader.demo",
                "src.ig_trader.execution",
                "src.ig_trader.shadow_execution",
            )
        )
        for module in imported_modules
    )
    assert (
        not {
            "create_position",
            "close_position",
            "create_working_order",
            "delete_working_order",
        }
        & public_methods
    )
    assert '"/positions' not in source
    assert '"/workingorders' not in source
    assert Shadow01ReadOnlyBroker(RecordingTransport()).execution_authority == "OFF"
