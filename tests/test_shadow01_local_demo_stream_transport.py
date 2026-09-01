"""Fake-only safety tests for the lazy Shadow01 local Demo stream factory."""

from __future__ import annotations

import ast
import inspect
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

import src.ig_trader.shadow01.local_demo_stream_transport as stream_transport_module
from src.ig_trader.shadow01.config import load_config
from src.ig_trader.shadow01.local_demo_read_only import ShadowStreamSessionMaterial
from src.ig_trader.shadow01.local_demo_stream_transport import (
    Shadow01LocalDemoReadOnlyStreamFactory,
)
from src.ig_trader.shadow01.registry import ShadowMarketRegistry, load_verified_dq03_registry
from src.ig_trader.shadow01.stream_bridge import (
    ShadowPriceUpdate,
    ShadowReadOnlyStreamError,
    ShadowStreamDisconnected,
)
from tests.shadow01_dq03_fixtures import write_verified_dq03_documents


@dataclass
class FakeSettings:
    """A credential-shaped local-only settings object with no real values."""

    ig_demo: bool = True
    ig_base_url: str = "https://demo-api.ig.com/gateway/deal"
    demo_operator_local: bool = True
    paper_trading: bool = True
    ig_expected_demo_account_id: str = "unit-demo-account"
    ig_api_key: str = "unit-api-key"
    ig_identifier: str = "unit-identifier"
    ig_password: str = "unit-password"


class FakeSessionManager:
    """In-memory SessionManager contract with an observable exact-login path."""

    def __init__(
        self,
        *,
        authenticated: bool = False,
        account_id: str = "unit-demo-account",
        logout_succeeds: bool = True,
    ) -> None:
        self._observer: Callable[[str, str], None] | None = None
        self._authenticated = authenticated
        self._logout_succeeds = logout_succeeds
        self.account_id = account_id
        self.cst = "unit-cst"
        self.x_security_token = "unit-xst"
        self.lightstreamer_endpoint = "https://stream.example.test"
        self.login_calls = 0
        self.logout_calls = 0
        self.authorized_requests: list[tuple[str, str]] = []
        self.observed_requests: list[tuple[str, str]] = []

    def bind_observer(self, observer: Callable[[str, str], None]) -> None:
        self._observer = observer

    def login(self) -> bool:
        self.login_calls += 1
        assert self._observer is not None
        self._observer("POST", "session")
        self.observed_requests.append(("POST", "session"))
        self._authenticated = True
        return True

    def is_authenticated(self) -> bool:
        return self._authenticated

    def logout(self) -> bool:
        assert self._observer is not None
        assert self._authenticated is True
        self._observer("DELETE", "session")
        self.observed_requests.append(("DELETE", "session"))
        self.logout_calls += 1
        if not self._logout_succeeds:
            return False
        self._authenticated = False
        self.account_id = None
        self.cst = None
        self.x_security_token = None
        self.lightstreamer_endpoint = None
        return True

    def authorized_request(self, method: str, endpoint: str, **_kwargs: Any) -> object:
        self.authorized_requests.append((method, endpoint))
        raise AssertionError("the stream transport must not issue a generic REST read")


class FakeStreamClient:
    """Small injected stream client with no network implementation."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[str, ...]]] = []
        self.updates: list[ShadowPriceUpdate | None] = []
        self.after_connect: Callable[[], None] | None = None
        self.after_subscribe: Callable[[], None] | None = None
        self.after_receive: Callable[[], None] | None = None
        self.after_unsubscribe: Callable[[], None] | None = None
        self.after_disconnect: Callable[[], None] | None = None

    def connect(self) -> None:
        self.calls.append(("connect", ()))
        if self.after_connect is not None:
            self.after_connect()

    def subscribe_prices(self, epics: tuple[str, ...]) -> None:
        self.calls.append(("subscribe_prices", epics))
        if self.after_subscribe is not None:
            self.after_subscribe()

    def receive_price_update(self, *, timeout_seconds: float) -> ShadowPriceUpdate | None:
        self.calls.append(("receive_price_update", ()))
        assert timeout_seconds >= 0
        if self.after_receive is not None:
            self.after_receive()
        if not self.updates:
            return None
        return self.updates.pop(0)

    def unsubscribe_prices(self, epics: tuple[str, ...]) -> None:
        self.calls.append(("unsubscribe_prices", epics))
        if self.after_unsubscribe is not None:
            self.after_unsubscribe()

    def disconnect(self) -> None:
        self.calls.append(("disconnect", ()))
        if self.after_disconnect is not None:
            self.after_disconnect()


class FakeRawLightstreamerClient:
    """A direct-client fake that retains distinct subscription handles."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []
        self.after_connect: Callable[[], None] | None = None
        self.after_subscribe: Callable[[], None] | None = None

    def connect(self) -> None:
        self.calls.append(("connect", ()))
        if self.after_connect is not None:
            self.after_connect()

    def subscribe(self, subscription: object) -> None:
        self.calls.append(("subscribe", subscription))
        if self.after_subscribe is not None:
            self.after_subscribe()

    def unsubscribe(self, subscription: object) -> None:
        self.calls.append(("unsubscribe", subscription))

    def disconnect(self) -> None:
        self.calls.append(("disconnect", ()))


class FakeRawSubscription:
    """A fake Lightstreamer subscription retaining its one listener."""

    def __init__(self, mode: str, items: list[str], fields: list[str]) -> None:
        self.mode = mode
        self.items = items
        self.fields = fields
        self.data_adapter: str | None = None
        self.listener: FakeInjectedListener | None = None

    def addListener(self, listener: object) -> None:  # noqa: N802 - library callback spelling
        assert isinstance(listener, FakeInjectedListener)
        self.listener = listener

    def setDataAdapter(self, value: str) -> None:  # noqa: N802 - library callback spelling
        self.data_adapter = value


class FakeInjectedListener:
    """A listener fake that can emit a selected safe price update."""

    def __init__(
        self,
        *,
        adapter: object,
        expected_epic: str,
    ) -> None:
        self._adapter = adapter
        self.expected_epic = expected_epic

    def emit(self, update: ShadowPriceUpdate) -> None:
        self._adapter.record_update(update)

    def emit_item(
        self,
        *,
        item_name: str,
        bid_value: object,
        ask_value: object,
        timestamp_milliseconds: object,
    ) -> None:
        self._adapter.record_item_update(
            epic=self.expected_epic,
            item_name=item_name,
            bid_value=bid_value,
            ask_value=ask_value,
            timestamp_milliseconds=timestamp_milliseconds,
            market_state=None,
        )


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


def _material() -> ShadowStreamSessionMaterial:
    return ShadowStreamSessionMaterial(
        account_identifier="unit-demo-account",
        lightstreamer_endpoint="https://stream.example.test",
        cst="unit-cst",
        x_security_token="unit-xst",
    )


def _receive(bridge: object) -> object:
    return bridge.receive_price_update(
        observed_at=datetime(2026, 8, 29, 17, 10, tzinfo=UTC),
        maximum_age_seconds=60,
        timeout_seconds=0,
    )


def _factory(
    session: FakeSessionManager,
    client: FakeStreamClient,
    *,
    settings: FakeSettings | None = None,
    settings_provider_calls: list[object] | None = None,
    session_factory_calls: list[object] | None = None,
    stream_factory_calls: list[object] | None = None,
) -> Shadow01LocalDemoReadOnlyStreamFactory:
    active_settings = settings or FakeSettings()

    def session_factory(observer: Callable[[str, str], None]) -> FakeSessionManager:
        if session_factory_calls is not None:
            session_factory_calls.append(observer)
        session.bind_observer(observer)
        return session

    def stream_factory(
        _endpoint: str,
        _account_id: str,
        _cst: str,
        _x_security_token: str,
    ) -> FakeStreamClient:
        if stream_factory_calls is not None:
            stream_factory_calls.append(object())
        return client

    def settings_provider() -> FakeSettings:
        if settings_provider_calls is not None:
            settings_provider_calls.append(object())
        return active_settings

    return Shadow01LocalDemoReadOnlyStreamFactory(
        settings_provider=settings_provider,
        session_factory=session_factory,
        stream_client_factory=stream_factory,
    )


def test_status_and_build_are_nonactivating(
    verified_registry: ShadowMarketRegistry,
) -> None:
    session_factory_calls: list[object] = []
    stream_factory_calls: list[object] = []
    settings_provider_calls: list[object] = []
    factory = _factory(
        FakeSessionManager(),
        FakeStreamClient(),
        settings_provider_calls=settings_provider_calls,
        session_factory_calls=session_factory_calls,
        stream_factory_calls=stream_factory_calls,
    )

    bridge = factory.build(verified_registry, session_material=_material())

    assert settings_provider_calls == []
    status = factory.status()

    assert factory.execution_authority == "OFF"
    assert status.document() == {
        "ready": True,
        "reason_code": "SHADOW01_DEMO_READ_ONLY_READY",
        "execution_authority": "OFF",
        "demo_mode": True,
        "demo_endpoint": True,
        "local_operator": True,
        "paper_trading": True,
        "expected_demo_account_configured": True,
        "credentials_present": True,
    }
    assert bridge.execution_authority == "OFF"
    assert len(settings_provider_calls) == 1
    assert session_factory_calls == []
    assert stream_factory_calls == []


def test_connect_uses_existing_session_material_without_rest_authentication(
    verified_registry: ShadowMarketRegistry,
) -> None:
    session = FakeSessionManager(authenticated=False)
    client = FakeStreamClient()
    session_factory_calls: list[object] = []
    stream_factory_calls: list[object] = []
    bridge = _factory(
        session,
        client,
        session_factory_calls=session_factory_calls,
        stream_factory_calls=stream_factory_calls,
    ).build(verified_registry, session_material=_material())

    bridge.connect()

    assert session.login_calls == 0
    assert session.observed_requests == []
    assert session.authorized_requests == []
    assert session_factory_calls == []
    assert len(stream_factory_calls) == 1
    assert client.calls == [("connect", ())]


def test_stream_disconnect_never_closes_the_parent_rest_session(
    verified_registry: ShadowMarketRegistry,
) -> None:
    session = FakeSessionManager()
    client = FakeStreamClient()
    bridge = _factory(session, client).build(verified_registry, session_material=_material())

    bridge.connect()
    bridge.disconnect()

    assert client.calls == [("connect", ()), ("disconnect", ())]
    assert session.observed_requests == []
    assert session.logout_calls == 0
    assert bridge.connected is False
    assert bridge.subscribed_epics == ()


def test_stream_disconnect_ignores_parent_logout_capability(
    verified_registry: ShadowMarketRegistry,
) -> None:
    session = FakeSessionManager(logout_succeeds=False)
    client = FakeStreamClient()
    bridge = _factory(session, client).build(verified_registry, session_material=_material())
    bridge.connect()

    bridge.disconnect()

    assert client.calls == [("connect", ()), ("disconnect", ())]
    assert session.observed_requests == []
    assert session.logout_calls == 0
    assert bridge.connected is False


def test_stream_factory_rejects_non_material_handoffs_before_client_construction(
    verified_registry: ShadowMarketRegistry,
) -> None:
    session = FakeSessionManager()
    client = FakeStreamClient()
    stream_factory_calls: list[object] = []
    _factory(
        session,
        client,
        stream_factory_calls=stream_factory_calls,
    )

    with pytest.raises(ValueError, match="Shadow stream session material"):
        ShadowStreamSessionMaterial("", "https://stream.example.test", "cst", "xst")

    assert session.login_calls == 0
    assert stream_factory_calls == []
    assert client.calls == []


def test_retired_stream_transport_cannot_construct_a_second_rest_session() -> None:
    session = FakeSessionManager()
    client = FakeStreamClient()

    with pytest.raises(
        stream_transport_module.Shadow01LocalDemoReadOnlyStreamError,
        match="SHADOW01_STREAM_LEGACY_TRANSPORT_RETIRED",
    ):
        stream_transport_module._LazyLocalDemoReadOnlyStreamTransport(
            settings_provider=lambda: FakeSettings(),
            session_factory=lambda _observer: session,
            stream_client_factory=lambda _endpoint, _account, _cst, _xst: client,
        )

    assert session.login_calls == 0
    assert session.logout_calls == 0
    assert client.calls == []


def test_stream_session_material_cannot_render_its_account_endpoint_or_tokens() -> None:
    material = _material()

    rendered = f"{material!r} {material.presence_document()}"

    assert "unit-demo-account" not in rendered
    assert "stream.example.test" not in rendered
    assert "unit-cst" not in rendered
    assert "unit-xst" not in rendered


def test_parent_session_object_is_not_retained_by_stream_transport(
    verified_registry: ShadowMarketRegistry,
) -> None:
    session = FakeSessionManager()
    client = FakeStreamClient()
    bridge = _factory(session, client).build(verified_registry, session_material=_material())

    bridge.connect()
    bridge.disconnect()

    transport = bridge._transport
    assert client.calls == [("connect", ()), ("disconnect", ())]
    assert transport._stream_client is None
    assert transport._subscribed_epics == ()
    assert session.logout_calls == 0


@pytest.mark.parametrize("operation", ("subscribe", "receive", "unsubscribe"))
def test_stream_lifecycle_never_rechecks_or_closes_parent_rest_session(
    verified_registry: ShadowMarketRegistry,
    operation: str,
) -> None:
    session = FakeSessionManager()
    client = FakeStreamClient()
    bridge = _factory(session, client).build(verified_registry, session_material=_material())
    eurusd = _epic(verified_registry, "EURUSD")

    bridge.connect()
    if operation in {"receive", "unsubscribe"}:
        bridge.subscribe_prices((eurusd,))
    session.account_id = "wrong-demo-account"

    if operation == "subscribe":
        bridge.subscribe_prices((eurusd,))
    elif operation == "receive":
        assert _receive(bridge) is None
    else:
        bridge.unsubscribe_prices((eurusd,))

    transport = bridge._transport
    assert transport._stream_client is client
    assert session.logout_calls == 0
    assert session.observed_requests == []
    assert bridge.connected is True


@pytest.mark.parametrize("operation", ("subscribe", "receive", "unsubscribe", "disconnect"))
def test_parent_session_mutation_cannot_change_stream_lifecycle_ownership(
    verified_registry: ShadowMarketRegistry,
    operation: str,
) -> None:
    session = FakeSessionManager()
    client = FakeStreamClient()
    bridge = _factory(session, client).build(verified_registry, session_material=_material())
    eurusd = _epic(verified_registry, "EURUSD")
    bridge.connect()

    def mutate_account() -> None:
        session.account_id = "wrong-demo-account"

    if operation == "subscribe":
        client.after_subscribe = mutate_account
        bridge.subscribe_prices((eurusd,))
    elif operation == "receive":
        bridge.subscribe_prices((eurusd,))
        client.after_receive = mutate_account
        assert _receive(bridge) is None
    elif operation == "unsubscribe":
        bridge.subscribe_prices((eurusd,))
        client.after_unsubscribe = mutate_account
        bridge.unsubscribe_prices((eurusd,))
    else:
        client.after_disconnect = mutate_account
        bridge.disconnect()

    transport = bridge._transport
    assert session.observed_requests == []
    assert session.logout_calls == 0
    if operation == "disconnect":
        assert transport._stream_client is None
    else:
        assert transport._stream_client is client


def test_stream_disconnect_still_tears_down_after_parent_session_object_changes(
    verified_registry: ShadowMarketRegistry,
) -> None:
    session = FakeSessionManager()
    client = FakeStreamClient()
    bridge = _factory(session, client).build(verified_registry, session_material=_material())
    bridge.connect()
    session.account_id = "wrong-demo-account"

    bridge.disconnect()

    transport = bridge._transport
    assert client.calls == [("connect", ()), ("disconnect", ())]
    assert bridge.connected is False
    assert bridge.subscribed_epics == ()
    assert transport._stream_client is None
    assert session.logout_calls == 0


def test_receive_returns_only_currently_subscribed_epics(
    verified_registry: ShadowMarketRegistry,
) -> None:
    session = FakeSessionManager()
    client = FakeStreamClient()
    bridge = _factory(session, client).build(verified_registry, session_material=_material())
    eurusd = _epic(verified_registry, "EURUSD")
    usdjpy = _epic(verified_registry, "USDJPY")
    bridge.connect()
    bridge.subscribe_prices((eurusd,))
    expected_update = _update(eurusd)
    client.updates.append(expected_update)

    quote = _receive(bridge)
    assert quote is not None
    assert quote.quality == "VALID_QUOTE"

    client.updates.append(_update(usdjpy))
    with pytest.raises(ShadowReadOnlyStreamError, match="SHADOW01_STREAM_RECEIVE_FAILED"):
        _receive(bridge)
    assert client.calls[-1] == ("disconnect", ())


def test_queued_lightstreamer_adapter_uses_per_epic_handles_and_filters_removed_updates(
    verified_registry: ShadowMarketRegistry,
) -> None:
    raw_client = FakeRawLightstreamerClient()
    subscriptions: list[FakeRawSubscription] = []

    def subscription_factory(
        mode: str,
        items: list[str],
        fields: list[str],
    ) -> FakeRawSubscription:
        subscription = FakeRawSubscription(mode, items, fields)
        subscriptions.append(subscription)
        return subscription

    adapter: object

    def listener_factory(epic: str) -> FakeInjectedListener:
        return FakeInjectedListener(adapter=adapter, expected_epic=epic)

    adapter = stream_transport_module._QueuedLightstreamerPriceClient(
        client=raw_client,
        account_id="unit-demo-account",
        subscription_factory=subscription_factory,
        listener_factory=listener_factory,
    )
    eurusd = _epic(verified_registry, "EURUSD")
    usdjpy = _epic(verified_registry, "USDJPY")
    us500 = _epic(verified_registry, "US500")

    adapter.connect()
    adapter.subscribe_prices((eurusd, usdjpy))

    assert [subscription.items for subscription in subscriptions] == [
        [f"PRICE:unit-demo-account:{eurusd}"],
        [f"PRICE:unit-demo-account:{usdjpy}"],
    ]
    assert all(
        subscription.fields == ["BIDPRICE1", "ASKPRICE1", "TIMESTAMP"]
        for subscription in subscriptions
    )
    assert all(subscription.data_adapter == "Pricing" for subscription in subscriptions)
    assert all(subscription.listener is not None for subscription in subscriptions)
    subscriptions[0].listener.emit(_update(eurusd))  # type: ignore[union-attr]
    subscriptions[0].listener.emit(_update(us500))  # type: ignore[union-attr]
    subscriptions[1].listener.emit(_update(usdjpy))  # type: ignore[union-attr]

    adapter.unsubscribe_prices((eurusd,))

    assert adapter.receive_price_update(timeout_seconds=0) == _update(usdjpy)
    assert adapter.receive_price_update(timeout_seconds=0) is None
    assert raw_client.calls[1:4] == [
        ("subscribe", subscriptions[0]),
        ("subscribe", subscriptions[1]),
        ("unsubscribe", subscriptions[0]),
    ]


def test_queued_lightstreamer_adapter_keeps_the_initial_merge_image_and_same_epic_partials(
    verified_registry: ShadowMarketRegistry,
) -> None:
    raw_client = FakeRawLightstreamerClient()
    subscriptions: list[FakeRawSubscription] = []

    def subscription_factory(
        mode: str,
        items: list[str],
        fields: list[str],
    ) -> FakeRawSubscription:
        subscription = FakeRawSubscription(mode, items, fields)
        subscriptions.append(subscription)
        return subscription

    adapter: object

    def listener_factory(epic: str) -> FakeInjectedListener:
        return FakeInjectedListener(adapter=adapter, expected_epic=epic)

    adapter = stream_transport_module._QueuedLightstreamerPriceClient(
        client=raw_client,
        account_id="unit-demo-account",
        subscription_factory=subscription_factory,
        listener_factory=listener_factory,
    )
    eurusd = _epic(verified_registry, "EURUSD")
    usdjpy = _epic(verified_registry, "USDJPY")
    timestamp = 1_788_007_800_000

    def emit_initial_image() -> None:
        listener = subscriptions[-1].listener
        assert listener is not None
        listener.emit_item(
            item_name=f"PRICE:unit-demo-account:{eurusd}",
            bid_value="1.0000",
            ask_value="1.0002",
            timestamp_milliseconds=timestamp,
        )

    raw_client.after_subscribe = emit_initial_image
    adapter.connect()
    adapter.subscribe_prices((eurusd,))
    initial = adapter.receive_price_update(timeout_seconds=0)

    assert initial == ShadowPriceUpdate(eurusd, "1.0000", "1.0002", timestamp)
    listener = subscriptions[0].listener
    assert listener is not None
    listener.emit_item(
        item_name=f"PRICE:unit-demo-account:{eurusd}",
        bid_value=None,
        ask_value="1.0003",
        timestamp_milliseconds=None,
    )
    listener.emit_item(
        item_name=f"PRICE:unit-demo-account:{usdjpy}",
        bid_value="9.0000",
        ask_value="9.0002",
        timestamp_milliseconds=timestamp,
    )

    assert adapter.receive_price_update(timeout_seconds=0) == ShadowPriceUpdate(
        eurusd,
        "1.0000",
        "1.0003",
        timestamp,
    )
    assert adapter.receive_price_update(timeout_seconds=0) is None
    assert adapter.subscription_diagnostic(eurusd) == {
        "subscription_requested": True,
        "subscription_active": None,
        "listener_registered": True,
        "update_callback_count": 2,
    }


def test_queued_adapter_records_value_free_callback_shape_and_rejection_categories(
    verified_registry: ShadowMarketRegistry,
) -> None:
    raw_client = FakeRawLightstreamerClient()
    adapter: object

    def listener_factory(epic: str) -> FakeInjectedListener:
        return FakeInjectedListener(adapter=adapter, expected_epic=epic)

    adapter = stream_transport_module._QueuedLightstreamerPriceClient(
        client=raw_client,
        account_id="unit-demo-account",
        subscription_factory=lambda mode, items, fields: FakeRawSubscription(mode, items, fields),
        listener_factory=listener_factory,
    )
    eurusd = _epic(verified_registry, "EURUSD")
    adapter.connect()
    adapter.subscribe_prices((eurusd,))
    adapter.record_item_update(
        epic=eurusd,
        item_name=f"PRICE:unit-demo-account:{eurusd}",
        bid_value="1.0000",
        ask_value="1.0002",
        timestamp_milliseconds="1788007800000",
        market_state=None,
        changed_field_names=("ASKPRICE1", "BIDPRICE1", "TIMESTAMP"),
        is_snapshot=True,
    )

    contract = adapter.field_contract_diagnostic(eurusd)

    assert contract == {
        "callback_observed": True,
        "item_name_recognized": True,
        "BIDPRICE1": {
            "present": True,
            "runtime_type": "str",
            "is_none": False,
            "is_numeric_string": True,
            "is_numeric_object": False,
            "parse_success": True,
        },
        "ASKPRICE1": {
            "present": True,
            "runtime_type": "str",
            "is_none": False,
            "is_numeric_string": True,
            "is_numeric_object": False,
            "parse_success": True,
        },
        "TIMESTAMP": {
            "present": True,
            "runtime_type": "str",
            "is_none": False,
            "is_digit_string": True,
            "is_numeric_object": False,
            "string_length": 13,
            "parse_success": True,
            "milliseconds_plausible": True,
        },
        "is_snapshot": True,
        "changed_field_names": ["ASKPRICE1", "BIDPRICE1", "TIMESTAMP"],
    }
    assert "1.0000" not in str(contract)
    assert "1788007800000" not in str(contract)

    adapter.record_quote_validation(
        eurusd,
        ("SHADOW01_STREAM_TIMESTAMP_SCHEMA_UNSUPPORTED",),
    )
    adapter.record_item_update(
        epic=eurusd,
        item_name="PRICE:wrong-account:wrong-epic",
        bid_value=None,
        ask_value=None,
        timestamp_milliseconds=None,
        market_state=None,
    )

    assert adapter.invalid_reason_counts(eurusd) == {
        "missing_bid": 0,
        "invalid_bid": 0,
        "missing_ask": 0,
        "invalid_ask": 0,
        "missing_timestamp": 0,
        "invalid_timestamp": 1,
        "stale_timestamp": 0,
        "item_resolution_failure": 1,
    }


def test_lightstreamer_client_forces_http_streaming_to_avoid_websocket_cleanup_warning() -> None:
    class ConnectionOptions:
        def __init__(self) -> None:
            self.transports: list[str] = []

        def setForcedTransport(self, transport: str) -> None:  # noqa: N802
            self.transports.append(transport)

    class Client:
        connectionOptions = ConnectionOptions()

    client = Client()
    stream_transport_module._configure_http_streaming_transport(client)

    assert client.connectionOptions.transports == ["HTTP-STREAMING"]


def test_queued_lightstreamer_adapter_fails_closed_on_bounded_buffer_overflow(
    verified_registry: ShadowMarketRegistry,
) -> None:
    raw_client = FakeRawLightstreamerClient()
    subscriptions: list[FakeRawSubscription] = []

    def subscription_factory(
        mode: str,
        items: list[str],
        fields: list[str],
    ) -> FakeRawSubscription:
        subscription = FakeRawSubscription(mode, items, fields)
        subscriptions.append(subscription)
        return subscription

    adapter: object

    def listener_factory(epic: str) -> FakeInjectedListener:
        return FakeInjectedListener(adapter=adapter, expected_epic=epic)

    adapter = stream_transport_module._QueuedLightstreamerPriceClient(
        client=raw_client,
        account_id="unit-demo-account",
        subscription_factory=subscription_factory,
        listener_factory=listener_factory,
    )
    eurusd = _epic(verified_registry, "EURUSD")
    adapter.connect()
    adapter.subscribe_prices((eurusd,))
    listener = subscriptions[0].listener
    assert isinstance(listener, FakeInjectedListener)

    for _ in range(adapter._MAX_PENDING_UPDATES):
        listener.emit(_update(eurusd))
    listener.emit(_update(eurusd))

    assert len(adapter._updates) == 0
    with pytest.raises(ShadowStreamDisconnected, match="SHADOW01_STREAM_UPDATE_BUFFER_OVERFLOW"):
        adapter.receive_price_update(timeout_seconds=0)


def test_queued_lightstreamer_adapter_rejects_synchronous_disconnect_callbacks(
    verified_registry: ShadowMarketRegistry,
) -> None:
    raw_client = FakeRawLightstreamerClient()

    def subscription_factory(
        mode: str,
        items: list[str],
        fields: list[str],
    ) -> FakeRawSubscription:
        return FakeRawSubscription(mode, items, fields)

    adapter: object

    def listener_factory(epic: str) -> FakeInjectedListener:
        return FakeInjectedListener(adapter=adapter, expected_epic=epic)

    adapter = stream_transport_module._QueuedLightstreamerPriceClient(
        client=raw_client,
        account_id="unit-demo-account",
        subscription_factory=subscription_factory,
        listener_factory=listener_factory,
    )
    raw_client.after_connect = adapter.mark_disconnected

    with pytest.raises(ShadowStreamDisconnected, match="SHADOW01_STREAM_CONNECTION_UNAVAILABLE"):
        adapter.connect()
    assert adapter._connected is False
    assert adapter._disconnected is True

    raw_client.after_connect = None
    adapter.connect()
    raw_client.after_subscribe = adapter.mark_disconnected

    with pytest.raises(ShadowStreamDisconnected, match="SHADOW01_STREAM_SUBSCRIPTION_UNAVAILABLE"):
        adapter.subscribe_prices((_epic(verified_registry, "EURUSD"),))
    assert adapter._subscriptions == {}


def test_module_keeps_lazy_imports_and_no_generic_or_execution_surface() -> None:
    source_path = Path(inspect.getsourcefile(stream_transport_module) or "")
    source = source_path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    top_level_imports = {
        alias.name for node in tree.body if isinstance(node, ast.Import) for alias in node.names
    }
    top_level_imports.update(
        node.module
        for node in tree.body
        if isinstance(node, ast.ImportFrom) and node.module is not None
    )
    public_factory_methods = {
        name
        for name, value in vars(Shadow01LocalDemoReadOnlyStreamFactory).items()
        if isinstance(value, Callable) and not name.startswith("_")
    }
    active_transport_source = inspect.getsource(
        stream_transport_module._SessionBoundLocalDemoReadOnlyStreamTransport
    )
    transport_methods = {
        name
        for name, value in vars(
            stream_transport_module._SessionBoundLocalDemoReadOnlyStreamTransport
        ).items()
        if isinstance(value, Callable) and not name.startswith("_")
    }

    assert public_factory_methods == {"status", "build"}
    assert transport_methods == {
        "connect",
        "subscribe_prices",
        "receive_price_update",
        "unsubscribe_prices",
        "disconnect",
        "subscription_diagnostic",
        "field_contract_diagnostic",
        "invalid_reason_counts",
        "record_quote_validation",
    }
    assert not any(
        module.startswith(
            (
                "lightstreamer",
                "src.ig_trader.config",
                "src.ig_trader.session",
                "src.ig_trader.demo_stream",
                "src.ig_trader.execution",
                "src.ig_trader.orders",
                "src.ig_trader.positions",
            )
        )
        for module in top_level_imports
    )
    assert "requests." not in source
    assert "httpx." not in source
    assert "_session_manager" not in active_transport_source
    assert "SessionManagerReadOnlyTransport" not in active_transport_source
    assert "logout(" not in active_transport_source
