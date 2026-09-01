"""Fake-only tests for the bounded SHADOW01 V2 live-smoke orchestrator."""

from __future__ import annotations

import ast
import inspect
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

import src.ig_trader.shadow01.live_readonly_smoke as smoke_module
from src.ig_trader.shadow01.config import ShadowTournamentConfig, load_config
from src.ig_trader.shadow01.live_contract_probe import Gate09LiveContractProbe
from src.ig_trader.shadow01.live_readonly_smoke import (
    LiveReadOnlySmokeV2,
    ShadowSmokeRequestBudget,
    ShadowSmokeRequestBudgetError,
    SmokeDatabaseState,
    read_smoke_database_state,
)
from src.ig_trader.shadow01.local_demo_read_only import (
    LocalDemoReadOnlyStatus,
    ShadowStreamSessionMaterial,
)
from src.ig_trader.shadow01.read_only_broker import Shadow01ReadOnlyBroker
from src.ig_trader.shadow01.registry import ShadowMarketRegistry, load_verified_dq03_registry
from src.ig_trader.shadow01.stream_bridge import (
    ShadowPriceUpdate,
    ShadowReadOnlyStreamBridge,
    ShadowStreamDisconnected,
)
from tests.shadow01_dq03_fixtures import write_verified_dq03_documents

NOW = datetime(2026, 9, 2, 21, 10, tzinfo=UTC)
_AUTHORIZATION = "SHADOW01_LIVE_READONLY_SMOKE_V2"
_STREAM_REPRESENTATIVES = ("EURUSD", "USDJPY", "XAUUSD", "US500")


def _stream_update(epic: str) -> ShadowPriceUpdate:
    return ShadowPriceUpdate(
        epic=epic,
        bid_value="1.0000",
        ask_value="1.0002",
        timestamp_milliseconds=int(NOW.timestamp() * 1000),
        market_state="DEAL",
    )


class RecordingReadOnlyTransport:
    """In-memory REST transport with only the V2 read routes and cleanup hook."""

    def __init__(
        self,
        *,
        account_valid: bool = True,
        mismatch_on_auth: bool = False,
        allowance_on_history: bool = False,
        allowance_on_logout: bool = False,
        timeline: list[str] | None = None,
    ) -> None:
        self.account_valid = account_valid
        self.mismatch_on_auth = mismatch_on_auth
        self.allowance_on_history = allowance_on_history
        self.allowance_on_logout = allowance_on_logout
        self.timeline = timeline
        self.calls: list[tuple[str, str]] = []
        self.v3_schedule_epics: list[str] = []
        self.logout_calls = 0
        self._response_diagnostic: dict[str, int | str | None] | None = None

    def authorized_request(self, method: str, endpoint: str, **kwargs: Any) -> dict[str, object]:
        self.calls.append((method, endpoint))
        if kwargs.get("api_version") == "3":
            self.v3_schedule_epics.append(endpoint.rsplit("/", maxsplit=1)[-1])
        if self.timeline is not None:
            self.timeline.append(f"rest:{method}:{endpoint}")
        if self.mismatch_on_auth and method == "POST":
            raise RuntimeError("SHADOW01_DEMO_ACCOUNT_MISMATCH")
        if method == "POST" and endpoint == "/session":
            return {"authenticated": True, "dealingEnabled": True}
        if endpoint == "/accounts":
            return {
                "accounts": [
                    {
                        "accountId": "account-not-for-output",
                        "preferred": True,
                        "status": "ENABLED",
                    }
                ]
            }
        if endpoint.startswith("/markets/"):
            return _market_document(endpoint.rsplit("/", maxsplit=1)[-1])
        if endpoint.startswith("/prices/"):
            if self.allowance_on_history:
                self._response_diagnostic = {
                    "status_code": 403,
                    "upstream_error_code": "error.public-api.exceeded-api-key-allowance",
                }
                raise RuntimeError("SHADOW01_READ_ONLY_HTTP_403")
            return _history_document()
        raise AssertionError(f"unexpected read route: {method} {endpoint}")

    def account_state_is_valid(self, _document: object) -> bool:
        return self.account_valid

    def stream_session_material(self) -> ShadowStreamSessionMaterial:
        return ShadowStreamSessionMaterial(
            account_identifier="account-not-for-output",
            lightstreamer_endpoint="https://stream.example.test",
            cst="cst-not-for-output",
            x_security_token="test-placeholder",
        )

    def logout(self) -> bool:
        self.logout_calls += 1
        if self.allowance_on_logout:
            self._response_diagnostic = {
                "status_code": 403,
                "upstream_error_code": "error.public-api.exceeded-api-key-allowance",
            }
            raise RuntimeError("SHADOW01_DEMO_SESSION_LOGOUT_FAILED")
        return True

    def latest_response_diagnostic(self) -> dict[str, int | str | None] | None:
        return self._response_diagnostic


class FakeRestFactory:
    def __init__(self, broker: Shadow01ReadOnlyBroker, status: LocalDemoReadOnlyStatus) -> None:
        self.broker = broker
        self._status = status
        self.build_calls = 0

    def status(self) -> LocalDemoReadOnlyStatus:
        return self._status

    def build(self) -> Shadow01ReadOnlyBroker:
        self.build_calls += 1
        return self.broker


class FakeStreamTransport:
    def __init__(
        self,
        *,
        naturally_disconnect_once: bool = False,
        disconnect_fails: bool = False,
        invalid_initial_update: bool = False,
        timeline: list[str] | None = None,
    ) -> None:
        self.naturally_disconnect_once = naturally_disconnect_once
        self.disconnect_fails = disconnect_fails
        self.invalid_initial_update = invalid_initial_update
        self.timeline = timeline
        self.calls: list[tuple[str, tuple[str, ...]]] = []
        self.updates: list[ShadowPriceUpdate] = []
        self._disconnect_calls = 0

    def connect(self) -> None:
        self.calls.append(("connect", ()))
        if self.timeline is not None:
            self.timeline.append("stream:connect")

    def subscribe_prices(self, epics: tuple[str, ...]) -> None:
        self.calls.append(("subscribe_prices", epics))
        self.updates.extend(_stream_update(epic) for epic in epics)
        if self.invalid_initial_update:
            self.invalid_initial_update = False
            self.updates[0] = ShadowPriceUpdate(
                epic=epics[0],
                bid_value=None,
                ask_value="1.0002",
                timestamp_milliseconds=int(NOW.timestamp() * 1000),
            )
        if self.timeline is not None:
            self.timeline.append("stream:subscribe")

    def receive_price_update(self, *, timeout_seconds: float) -> ShadowPriceUpdate | None:
        self.calls.append(("receive_price_update", ()))
        assert timeout_seconds >= 0
        if self.timeline is not None:
            self.timeline.append("stream:receive")
        if self.naturally_disconnect_once:
            self.naturally_disconnect_once = False
            raise ShadowStreamDisconnected("SHADOW01_TEST_NATURAL_DISCONNECT")
        return self.updates.pop(0) if self.updates else None

    def unsubscribe_prices(self, epics: tuple[str, ...]) -> None:
        self.calls.append(("unsubscribe_prices", epics))
        if self.timeline is not None:
            self.timeline.append("stream:unsubscribe")

    def disconnect(self) -> None:
        self.calls.append(("disconnect", ()))
        self._disconnect_calls += 1
        if self.timeline is not None:
            self.timeline.append("stream:disconnect")
        if self.disconnect_fails and self._disconnect_calls >= 2:
            raise RuntimeError("SHADOW01_TEST_STREAM_DISCONNECT_FAILED")


class FakeStreamFactory:
    def __init__(
        self,
        registry: ShadowMarketRegistry,
        status: LocalDemoReadOnlyStatus,
        *,
        naturally_disconnect_once: bool = False,
        disconnect_fails: bool = False,
        invalid_initial_update: bool = False,
        timeline: list[str] | None = None,
    ) -> None:
        self.registry = registry
        self._status = status
        self.transport = FakeStreamTransport(
            naturally_disconnect_once=naturally_disconnect_once,
            disconnect_fails=disconnect_fails,
            invalid_initial_update=invalid_initial_update,
            timeline=timeline,
        )
        self.build_calls = 0
        self.max_reconnect_attempts: int | None = None

    def status(self) -> LocalDemoReadOnlyStatus:
        return self._status

    def build(
        self,
        registry: ShadowMarketRegistry,
        *,
        session_material: ShadowStreamSessionMaterial,
        max_reconnect_attempts: int,
    ) -> ShadowReadOnlyStreamBridge:
        assert registry is self.registry
        assert session_material.presence_document() == {
            "account_identifier_present": True,
            "lightstreamer_endpoint_present": True,
            "cst_present": True,
            "x_security_token_present": True,
        }
        self.build_calls += 1
        self.max_reconnect_attempts = max_reconnect_attempts
        return ShadowReadOnlyStreamBridge(
            registry,
            self.transport,
            max_reconnect_attempts=max_reconnect_attempts,
        )


@pytest.fixture
def config_and_registry(tmp_path: Path) -> tuple[ShadowTournamentConfig, ShadowMarketRegistry]:
    config = load_config()
    write_verified_dq03_documents(tmp_path, config)
    return config, load_verified_dq03_registry(config, tmp_path / "instrument_registry.json")


def _ready_status(**overrides: object) -> LocalDemoReadOnlyStatus:
    values: dict[str, object] = {
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
    values.update(overrides)
    return LocalDemoReadOnlyStatus(**values)


def _market_document(epic: str) -> dict[str, object]:
    return {
        "instrument": {
            "epic": epic,
            "streamingPricesAvailable": True,
            "openingHours": {
                "timezone": "America/New_York",
                "marketTimes": [{"openTime": "00:00", "closeTime": "23:59"}],
            },
        },
        "snapshot": {
            "marketStatus": "TRADEABLE",
            "bid": "100.0",
            "ask": "100.1",
            "updateTimestampUTC": int((NOW - timedelta(seconds=30)).timestamp()),
        },
        "dealingRules": {"minNormalStopOrLimitDistance": {"value": 1}},
    }


def _synthetic_v4_millisecond_document(epic: str) -> dict[str, object]:
    """A value-free-live-data V4 fixture with numeric-string quotes and milliseconds."""

    document = _market_document(epic)
    document["snapshot"]["updateTimestampUTC"] = int(
        (NOW - timedelta(seconds=30)).timestamp() * 1000
    )
    return document


def _history_document() -> dict[str, object]:
    prices: list[dict[str, object]] = []
    current_day = datetime.combine(NOW.date(), datetime.min.time(), UTC)
    for index in range(300):
        value = 100.0 + index * 0.1
        timestamp = current_day - timedelta(days=299 - index)

        def quote(number: float) -> dict[str, float]:
            return {"bid": number - 0.01, "offer": number + 0.01}

        prices.append(
            {
                "snapshotTimeUTC": timestamp.isoformat(),
                "openPrice": quote(value),
                "highPrice": quote(value + 0.5),
                "lowPrice": quote(value - 0.5),
                "closePrice": quote(value + 0.1),
            }
        )
    return {"prices": prices}


def test_v4_market_contract_uses_rest_for_metadata_only() -> None:
    document = _market_document("CS.TEST.EPIC")

    observation = smoke_module._market_observation(
        symbol="EURUSD",
        epic="CS.TEST.EPIC",
        document=document,
        observed_at=NOW,
        maximum_age_seconds=60,
    )

    assert observation.identity_verified is True
    assert observation.market_status == "TRADEABLE"
    assert observation.metadata_health == "PASS"
    assert observation.streaming_prices_available is True
    assert observation.quote_availability == "NOT_OBSERVED"
    assert observation.quote_timestamp_freshness == "NOT_OBSERVED"
    assert observation.minimum_stop_metadata_available is True


def test_v4_market_contract_does_not_select_rest_ladder_or_direct_quote_fields() -> None:
    document = _synthetic_v4_millisecond_document("CS.TEST.EPIC")
    document["snapshot"]["priceLadder"] = [
        {"bid": "1.0", "ask": "1.1"},
        {"bid": "0.9", "ask": "1.2"},
    ]

    observation = smoke_module._market_observation(
        symbol="EURUSD",
        epic="CS.TEST.EPIC",
        document=document,
        observed_at=NOW,
        maximum_age_seconds=60,
    )

    assert observation.metadata_health == "PASS"
    assert observation.quote_availability == "NOT_OBSERVED"
    assert observation.quote_timestamp_freshness == "NOT_OBSERVED"
    assert observation.reason_code is None


@pytest.mark.parametrize(
    "mutate",
    (
        lambda document: document["snapshot"].pop("bid"),
        lambda document: document["snapshot"].pop("ask"),
        lambda document: document["snapshot"].update({"bid": "not-a-number"}),
        lambda document: document["snapshot"].update({"ask": "not-a-number"}),
        lambda document: document["snapshot"].update(
            {"priceLadder": [{"bid": "1.0", "ask": "1.1"}]}
        ),
    ),
)
def test_v4_metadata_remains_healthy_without_selecting_any_rest_quote(
    mutate: Callable[[dict[str, object]], None],
) -> None:
    document = _market_document("CS.TEST.EPIC")
    mutate(document)

    observation = smoke_module._market_observation(
        symbol="EURUSD",
        epic="CS.TEST.EPIC",
        document=document,
        observed_at=NOW,
        maximum_age_seconds=60,
    )

    assert observation.metadata_health == "PASS"
    assert observation.quote_availability == "NOT_OBSERVED"
    assert observation.live_quote_health == "NOT_OBSERVED"


def test_market_contract_sanitizer_returns_key_type_and_presence_only() -> None:
    document = _market_document("CS.TEST.EPIC")
    document["secret_like_value"] = "must-not-appear"
    document["snapshot"] = {
        **document["snapshot"],
        "unsafe-key": "must-not-appear",
    }

    audit = smoke_module.sanitize_market_contract(document)

    assert audit == {
        "document_is_object": True,
        "top_level_keys": ["dealingRules", "instrument", "snapshot"],
        "snapshot": {
            "present": True,
            "type": "object",
            "keys": ["ask", "bid", "marketStatus", "updateTimestampUTC"],
        },
        "instrument": {
            "present": True,
            "type": "object",
            "keys": ["epic", "openingHours", "streamingPricesAvailable"],
        },
        "dealing_rules": {
            "present": True,
            "type": "object",
            "keys": ["minNormalStopOrLimitDistance"],
        },
    }
    assert "must-not-appear" not in str(audit)
    assert "100.0" not in str(audit)


def test_price_ladder_diagnostic_reports_only_unordered_shape() -> None:
    document = _market_document("CS.TEST.EPIC")
    document["snapshot"]["priceLadder"] = [{"bid": "1.0000", "ask": "1.0002"}]

    diagnostic = smoke_module.price_ladder_contract_diagnostic(document)

    assert diagnostic == {
        "priceLadder_present": True,
        "priceLadder_type": "list",
        "priceLadder_length": 1,
        "selected_entry_index": None,
        "selected_entry_type": None,
        "bid_present": False,
        "bid_type": None,
        "bid_numeric": False,
        "ask_present": False,
        "ask_type": None,
        "ask_numeric": False,
    }
    assert "1.0000" not in str(diagnostic)
    assert "1.0002" not in str(diagnostic)


@pytest.mark.parametrize(
    ("ladder", "expected_type", "expected_length"),
    (
        (None, None, None),
        ([], "list", 0),
        ("not-an-array", "str", None),
        ([None], "list", 1),
        ([{"bid": "1.0000"}], "list", 1),
        ([{"ask": "1.0002"}], "list", 1),
        ([{"bid": "1.0000", "ask": "1.0002"}] * 2, "list", 2),
    ),
)
def test_price_ladder_diagnostic_never_selects_a_rest_tier(
    ladder: object,
    expected_type: str | None,
    expected_length: int | None,
) -> None:
    document = _market_document("CS.TEST.EPIC")
    document["snapshot"]["priceLadder"] = ladder

    diagnostic = smoke_module.quote_contract_diagnostic(document)

    assert diagnostic["priceLadder_present"] is True
    assert diagnostic["priceLadder_type"] == expected_type
    assert diagnostic["priceLadder_length"] == expected_length
    assert diagnostic["selected_entry_index"] is None
    assert diagnostic["selected_entry_type"] is None
    assert diagnostic["bid_present"] is False
    assert diagnostic["ask_present"] is False


@pytest.mark.parametrize(
    ("timestamp", "freshness", "_availability", "_reason_code"),
    (
        (int((NOW - timedelta(seconds=30)).timestamp()), "FRESH", "VALID_QUOTE", None),
        (float((NOW - timedelta(seconds=30)).timestamp()), "FRESH", "VALID_QUOTE", None),
        (
            int((NOW - timedelta(seconds=30)).timestamp() * 1000),
            "FRESH",
            "VALID_QUOTE",
            None,
        ),
        (
            float((NOW - timedelta(seconds=30)).timestamp() * 1000),
            "FRESH",
            "VALID_QUOTE",
            None,
        ),
        (
            "unsupported-string",
            "SCHEMA_UNSUPPORTED",
            "UNAVAILABLE",
            "SHADOW01_QUOTE_TIMESTAMP_SCHEMA_UNSUPPORTED",
        ),
        (
            float("nan"),
            "INVALID",
            "UNAVAILABLE",
            "SHADOW01_QUOTE_TIMESTAMP_INVALID",
        ),
        (
            float("inf"),
            "INVALID",
            "UNAVAILABLE",
            "SHADOW01_QUOTE_TIMESTAMP_INVALID",
        ),
        (
            -1,
            "INVALID",
            "UNAVAILABLE",
            "SHADOW01_QUOTE_TIMESTAMP_INVALID",
        ),
        (
            10**400,
            "INVALID",
            "UNAVAILABLE",
            "SHADOW01_QUOTE_TIMESTAMP_INVALID",
        ),
        (
            int(datetime(1999, 12, 31, tzinfo=UTC).timestamp()),
            "IMPLAUSIBLY_OLD",
            "UNAVAILABLE",
            "SHADOW01_QUOTE_TIMESTAMP_INVALID",
        ),
        (
            int((NOW + timedelta(seconds=301)).timestamp()),
            "FUTURE_INVALID",
            "UNAVAILABLE",
            "SHADOW01_QUOTE_TIMESTAMP_INVALID",
        ),
    ),
)
def test_quote_timestamp_contract_normalizes_numeric_epochs_and_rejects_invalid_values(
    timestamp: object,
    freshness: str,
    _availability: str,
    _reason_code: str | None,
) -> None:
    assert smoke_module._quote_timestamp_freshness(timestamp, NOW, 60) == freshness


def _runner(
    config: ShadowTournamentConfig,
    registry: ShadowMarketRegistry,
    *,
    transport: RecordingReadOnlyTransport | None = None,
    rest_status: LocalDemoReadOnlyStatus | None = None,
    database_reader: Callable[[Path], SmokeDatabaseState] | None = None,
    database_path: Path | None = None,
    natural_stream_disconnect: bool = False,
    stream_disconnect_fails: bool = False,
    invalid_initial_update: bool = False,
    timeline: list[str] | None = None,
    request_budget: ShadowSmokeRequestBudget | None = None,
    now: Callable[[], datetime] | None = None,
) -> tuple[LiveReadOnlySmokeV2, FakeRestFactory, FakeStreamFactory, RecordingReadOnlyTransport]:
    active_transport = transport or RecordingReadOnlyTransport()
    rest_factory = FakeRestFactory(
        Shadow01ReadOnlyBroker(active_transport),
        rest_status or _ready_status(),
    )
    stream_factory = FakeStreamFactory(
        registry,
        _ready_status(),
        naturally_disconnect_once=natural_stream_disconnect,
        disconnect_fails=stream_disconnect_fails,
        invalid_initial_update=invalid_initial_update,
        timeline=timeline,
    )
    return (
        LiveReadOnlySmokeV2(
            authorization=_AUTHORIZATION,
            rest_factory=rest_factory,
            stream_factory=stream_factory,
            config_loader=lambda: config,
            registry_loader=lambda _config, _path: registry,
            database_path=database_path or Path("missing-shadow-smoke.sqlite3"),
            database_reader=(
                database_reader or (lambda _path: SmokeDatabaseState(True, False, 0, 0))
            ),
            env_file_ignored=lambda: True,
            now=now if now is not None else lambda: NOW,
            request_budget=request_budget,
        ),
        rest_factory,
        stream_factory,
        active_transport,
    )


def _contract_probe(
    config: ShadowTournamentConfig,
    registry: ShadowMarketRegistry,
    *,
    authorization: str = "SHADOW01_GATE09_LIVE_CONTRACT_PROBE",
) -> tuple[Gate09LiveContractProbe, FakeRestFactory, FakeStreamFactory, RecordingReadOnlyTransport]:
    transport = RecordingReadOnlyTransport()
    rest_factory = FakeRestFactory(Shadow01ReadOnlyBroker(transport), _ready_status())
    stream_factory = FakeStreamFactory(registry, _ready_status())
    return (
        Gate09LiveContractProbe(
            authorization=authorization,
            rest_factory=rest_factory,
            stream_factory=stream_factory,
            config_loader=lambda: config,
            registry_loader=lambda _config, _path: registry,
            now=lambda: NOW,
        ),
        rest_factory,
        stream_factory,
        transport,
    )


def test_preflight_failure_blocks_factory_build_before_any_authentication(
    config_and_registry: tuple[ShadowTournamentConfig, ShadowMarketRegistry],
) -> None:
    config, registry = config_and_registry
    blocked_status = _ready_status(
        ready=False,
        reason_code="SHADOW01_PAPER_TRADING_REQUIRED",
        paper_trading=False,
    )
    runner, rest_factory, stream_factory, transport = _runner(
        config,
        registry,
        rest_status=blocked_status,
    )

    result = runner.run()

    assert result.status == "SHADOW01_BLOCKED_PAPER_TRADING_REQUIRED"
    assert result.preflight_passed is False
    assert rest_factory.build_calls == 0
    assert stream_factory.build_calls == 0
    assert transport.calls == []


def test_gate09_contract_probe_is_one_market_read_only_and_cleans_up(
    config_and_registry: tuple[ShadowTournamentConfig, ShadowMarketRegistry],
) -> None:
    config, registry = config_and_registry
    probe, rest_factory, stream_factory, transport = _contract_probe(config, registry)

    result = probe.run()
    document = result.document()

    assert result.status == "SHADOW01_GATE09_LIVE_CONTRACT_PROBE_PASS"
    assert document["scope"] == {
        "symbol": "EURUSD",
        "history_resolution": "DAY",
        "history_points": 5,
        "stream_wait_seconds": 5.0,
        "execution_authority": "OFF",
        "tournament_epoch_created": False,
        "prospective_decisions": 0,
        "outcomes": 0,
    }
    assert document["history"]["row_contract"]["snapshotTimeUTC"]["format"] == "ISO8601_OFFSET"
    assert document["ig_counters"] == {
        "auth": 1,
        "account_reads": 0,
        "market_metadata_reads": 0,
        "historical_reads": 1,
        "stream_subscriptions": 1,
        "session_logouts": 1,
    }
    assert document["execution"] == {
        "create": 0,
        "close": 0,
        "working_orders": 0,
        "demo_starts": 0,
        "execution_authority": "OFF",
        "live_actions": 0,
        "azure_actions": 0,
    }
    assert rest_factory.build_calls == 1
    assert stream_factory.build_calls == 1
    assert [endpoint for _, endpoint in transport.calls] == [
        "/session",
        "/prices/TEST.EURUSD/DAY/5",
    ]
    assert stream_factory.transport.calls[0][0] == "connect"
    assert stream_factory.transport.calls[1][0] == "subscribe_prices"
    assert stream_factory.transport.calls[-2:] == [
        ("unsubscribe_prices", ("TEST.EURUSD",)),
        ("disconnect", ()),
    ]
    assert transport.logout_calls == 1
    assert "account-not-for-output" not in str(document)


def test_gate09_contract_probe_rejects_missing_authorization_before_factory_build(
    config_and_registry: tuple[ShadowTournamentConfig, ShadowMarketRegistry],
) -> None:
    config, registry = config_and_registry
    probe, rest_factory, stream_factory, transport = _contract_probe(
        config,
        registry,
        authorization="SHADOW01_OTHER",
    )

    result = probe.run()

    assert result.status == "SHADOW01_BLOCKED_GATE09_AUTHORIZATION_REQUIRED"
    assert rest_factory.build_calls == 0
    assert stream_factory.build_calls == 0
    assert transport.calls == []


def test_full_fake_smoke_is_non_persisting_and_returns_only_sanitized_facts(
    tmp_path: Path,
    config_and_registry: tuple[ShadowTournamentConfig, ShadowMarketRegistry],
) -> None:
    config, registry = config_and_registry
    database_path = tmp_path / "absent-shadow.sqlite3"
    timeline: list[str] = []
    runner, _rest_factory, stream_factory, transport = _runner(
        config,
        registry,
        transport=RecordingReadOnlyTransport(timeline=timeline),
        database_path=database_path,
        database_reader=read_smoke_database_state,
        timeline=timeline,
    )

    result = runner.run()
    document = result.document()

    assert result.status == "SHADOW01_LIVE_READONLY_SMOKE_PASS"
    assert result.preflight_passed is True
    assert document["dq03_live"]["verified_identities"] == 20
    assert document["market_read"] == {
        "valid": 20,
        "closed": 0,
        "unavailable": 0,
        "stale": 0,
        "exceptions": [],
    }
    assert document["dry_snapshot"]["status"] == "DRY_RUN_NON_PROSPECTIVE"
    clock = document["clock"]
    diagnostic = clock["diagnostic"]
    assert clock["overall"] == "PASS"
    assert diagnostic["diagnostic"] == "SHADOW01_SESSION_CLOCK_READ_ONLY"
    assert diagnostic["execution_authority"] == "OFF"
    assert diagnostic["non_persisting"] is True
    assert diagnostic["request_budget"] == 8
    assert diagnostic["requests_used"] == 8
    assert diagnostic["overall_state"] == "PASS"
    assert len(diagnostic["assessments"]) == 4
    assert all(
        assessment["opening_hours_state"]
        in {"DECLARED_HOURS_AVAILABLE", "DECLARED_HOURS_NOT_PROVIDED"}
        and assessment["history_rows_received"] == 300
        and assessment["evidence"]["market_metadata_available"] is True
        and assessment["evidence"]["latest_completed_day_history_available"] is True
        and assessment["evidence"]["target_anchor_computable"] is True
        and assessment["evidence"]["dst_conversion_valid"] is True
        and assessment["evidence"]["restart_idempotency_key_deterministic"] is True
        and assessment["evidence"]["schedule_source_version"] is None
        and assessment["evidence"]["target_anchor_in_declared_operational_window"] is None
        for assessment in diagnostic["assessments"]
    )
    assert document["database"] == {
        "epoch_before": False,
        "epoch_after": False,
        "decisions_before": 0,
        "decisions_after": 0,
        "outcomes_before": 0,
        "outcomes_after": 0,
    }
    assert database_path.exists() is False
    assert "account-not-for-output" not in str(document)
    assert transport.logout_calls == 1
    assert not any("position" in endpoint or "order" in endpoint for _, endpoint in transport.calls)
    market_endpoints = [
        endpoint
        for method, endpoint in transport.calls
        if method == "GET" and endpoint.startswith("/markets/")
    ]
    assert len(market_endpoints) == 20
    assert len(set(market_endpoints)) == 20
    assert transport.v3_schedule_epics == []
    assert stream_factory.max_reconnect_attempts == 1
    expected_epics = tuple(market.epic for market in registry.markets)
    assert all(isinstance(epic, str) for epic in expected_epics)
    representative_epics = tuple(
        registry.by_symbol(symbol).epic for symbol in ("EURUSD", "USDJPY", "XAUUSD", "US500")
    )
    assert stream_factory.transport.calls[-2:] == [
        ("unsubscribe_prices", representative_epics),
        ("disconnect", ()),
    ]
    assert document["stream"]["subscriptions_attempted"] == 20
    assert document["stream"]["subscription_call_successes"] == 20
    assert document["stream"]["updates_received"] == 20
    assert document["stream"]["reconnect_attempts"] == 1
    assert document["stream"]["representative_reconnect_symbols"] == [
        "EURUSD",
        "USDJPY",
        "XAUUSD",
        "US500",
    ]
    assert document["stream"]["representative_reconnect_updates"] == 4
    assert len(document["stream"]["subscription_diagnostics"]) == 24
    assert document["ig_counters"]["rest_live_price_reads"] == 0
    history_events = [event for event in timeline if event.startswith("rest:GET:/prices/")]
    assert len(history_events) == 4
    assert all(event.endswith("/DAY/300") for event in history_events)
    market_events = [event for event in timeline if event.startswith("rest:GET:/markets/")]
    assert len(market_events) == 20
    assert timeline.index("stream:connect") > max(
        index for index, event in enumerate(timeline) if event.startswith("rest:GET:/prices/")
    )
    assert document["rest_budget"] == {
        "maximum_requests": 27,
        "reserved_requests": 27,
        "maximum": {
            "auth": 1,
            "account": 1,
            "market": 20,
            "history": 4,
            "logout": 1,
        },
        "reserved": {
            "auth": 1,
            "account": 1,
            "market": 20,
            "history": 4,
            "logout": 1,
        },
    }


def test_clock_handoff_uses_the_same_post_stream_canonical_quote_map(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    config_and_registry: tuple[ShadowTournamentConfig, ShadowMarketRegistry],
) -> None:
    config, registry = config_and_registry
    database_path = tmp_path / "absent-shadow.sqlite3"
    clock_calls = 0

    def advancing_now() -> datetime:
        nonlocal clock_calls
        clock_calls += 1
        return NOW - timedelta(seconds=1) if clock_calls <= 21 else NOW

    handoffs: dict[str, object] = {}
    original_join = smoke_module._with_stream_quotes
    original_clock = smoke_module.verify_shadow_session_clock
    original_dry_snapshot = smoke_module._run_dry_snapshot_if_ready

    def record_market_read(observations: object, quotes_by_epic: object) -> object:
        handoffs["market_read"] = quotes_by_epic
        return original_join(observations, quotes_by_epic)

    def record_clock(*, live_quotes: object, **kwargs: object) -> object:
        handoffs["clock"] = live_quotes
        return original_clock(live_quotes=live_quotes, **kwargs)

    def record_dry_snapshot(*, live_quotes: object, **kwargs: object) -> object:
        handoffs["dry_snapshot"] = live_quotes
        return original_dry_snapshot(live_quotes=live_quotes, **kwargs)

    monkeypatch.setattr(smoke_module, "_with_stream_quotes", record_market_read)
    monkeypatch.setattr(smoke_module, "verify_shadow_session_clock", record_clock)
    monkeypatch.setattr(smoke_module, "_run_dry_snapshot_if_ready", record_dry_snapshot)
    runner, _rest_factory, stream_factory, _transport = _runner(
        config,
        registry,
        database_path=database_path,
        database_reader=read_smoke_database_state,
        now=advancing_now,
    )

    result = runner.run()
    document = result.document()
    assessments = {item["symbol"]: item for item in document["clock"]["diagnostic"]["assessments"]}
    market_rows = {item.symbol: item for item in result.dq03_observations}

    assert result.status == "SHADOW01_LIVE_READONLY_SMOKE_PASS"
    assert document["market_read"]["valid"] == 20
    assert document["stream"]["updates_received"] == 20
    assert document["stream"]["representative_reconnect_updates"] == 4
    assert document["ig_counters"]["rest_live_price_reads"] == 0
    assert document["dry_snapshot"]["status"] == "DRY_RUN_NON_PROSPECTIVE"
    assert document["database"] == {
        "epoch_before": False,
        "epoch_after": False,
        "decisions_before": 0,
        "decisions_after": 0,
        "outcomes_before": 0,
        "outcomes_after": 0,
    }
    assert database_path.exists() is False
    assert handoffs["market_read"] is handoffs["clock"] is handoffs["dry_snapshot"]
    canonical_quotes = handoffs["clock"]
    for symbol in ("EURUSD", "XAUUSD", "US500", "USTECH100"):
        epic = registry.by_symbol(symbol).epic
        assert isinstance(epic, str)
        quote = canonical_quotes[epic]
        assert quote.epic == epic
        assert quote.symbol == symbol
        assert quote.source == "IG_PRICE_STREAM"
        assert quote.quality == "VALID_QUOTE"
        assert assessments[symbol]["state"] == "PASS"
        assert assessments[symbol]["evidence"]["streaming_price_available"] is True
        assert market_rows[symbol].live_quote_health == "VALID_QUOTE"
        assert market_rows[symbol].quote_timestamp_freshness == "FRESH"
        assert market_rows[symbol].stream_connection_status == "QUOTE_RECEIVED"


def test_request_budget_blocks_the_twentieth_market_before_transport(
    config_and_registry: tuple[ShadowTournamentConfig, ShadowMarketRegistry],
) -> None:
    config, registry = config_and_registry
    transport = RecordingReadOnlyTransport()
    runner, _rest_factory, stream_factory, _transport = _runner(
        config,
        registry,
        transport=transport,
        request_budget=ShadowSmokeRequestBudget(maximum_market=19),
    )

    result = runner.run()

    assert result.status == "SHADOW01_BLOCKED_SMOKE_REST_BUDGET_EXCEEDED"
    assert (
        len(
            [
                endpoint
                for method, endpoint in transport.calls
                if method == "GET" and endpoint.startswith("/markets/")
            ]
        )
        == 19
    )
    assert not any(endpoint.startswith("/prices/") for _, endpoint in transport.calls)
    assert stream_factory.transport.calls == []
    with pytest.raises(ShadowSmokeRequestBudgetError, match="SHADOW01_SMOKE_REST_BUDGET_EXCEEDED"):
        ShadowSmokeRequestBudget(maximum_history=0).reserve("history")


def test_first_ig_allowance_response_stops_remaining_history_and_stream_requests(
    config_and_registry: tuple[ShadowTournamentConfig, ShadowMarketRegistry],
) -> None:
    config, registry = config_and_registry
    transport = RecordingReadOnlyTransport(allowance_on_history=True)
    runner, _rest_factory, stream_factory, _transport = _runner(
        config,
        registry,
        transport=transport,
    )

    result = runner.run()

    assert result.status == "SHADOW01_BLOCKED_IG_API_ALLOWANCE"
    assert [endpoint for _, endpoint in transport.calls if endpoint.startswith("/prices/")] == [
        "/prices/TEST.EURUSD/DAY/300"
    ]
    assert stream_factory.transport.calls == []
    assert transport.logout_calls == 1
    assert result.request_budget.document()["reserved"]["history"] == 1


def test_logout_failure_is_marked_as_affected_by_api_allowance(
    config_and_registry: tuple[ShadowTournamentConfig, ShadowMarketRegistry],
) -> None:
    config, registry = config_and_registry
    transport = RecordingReadOnlyTransport(allowance_on_logout=True)
    runner, _rest_factory, _stream_factory, _transport = _runner(
        config,
        registry,
        transport=transport,
    )

    result = runner.run()

    assert result.status == "SHADOW01_BLOCKED_IG_API_ALLOWANCE"
    assert result.rest_logout_result == "FAILED"
    assert result.rest_logout_http_status == 403
    assert result.rest_logout_upstream_error_code == "error.public-api.exceeded-api-key-allowance"
    assert result.rest_logout_allowance_affected is True
    assert result.cleanup_passed is False


def test_auth_account_mismatch_maps_to_the_exact_required_block_and_redacts_account_data(
    config_and_registry: tuple[ShadowTournamentConfig, ShadowMarketRegistry],
) -> None:
    config, registry = config_and_registry
    runner, _rest_factory, stream_factory, transport = _runner(
        config,
        registry,
        transport=RecordingReadOnlyTransport(mismatch_on_auth=True),
    )

    result = runner.run()

    assert result.status == "SHADOW01_BLOCKED_DEMO_ACCOUNT_MISMATCH"
    assert transport.calls == [("POST", "/session")]
    assert transport.logout_calls == 1
    assert stream_factory.transport.calls == []
    assert "account-not-for-output" not in str(result.document())


def test_initial_stream_disconnect_fails_closed_without_an_all_market_reconnect(
    config_and_registry: tuple[ShadowTournamentConfig, ShadowMarketRegistry],
) -> None:
    config, registry = config_and_registry
    runner, _rest_factory, stream_factory, _transport = _runner(
        config,
        registry,
        natural_stream_disconnect=True,
    )

    result = runner.run()

    assert result.status == "SHADOW01_BLOCKED_STREAM_SMOKE_INCOMPLETE"
    assert result.stream.reconnect_attempts == 0
    assert result.stream.restore_result == "NOT_REQUIRED"
    assert result.stream.error_code == "SHADOW01_STREAM_DISCONNECTED"
    assert stream_factory.max_reconnect_attempts == 1
    assert sum(call[0] == "connect" for call in stream_factory.transport.calls) == 1
    assert result.stream.disconnect_result == "ALREADY_DISCONNECTED"


def test_initial_invalid_stream_update_is_reported_separately_from_no_update(
    config_and_registry: tuple[ShadowTournamentConfig, ShadowMarketRegistry],
) -> None:
    config, registry = config_and_registry
    runner, _rest_factory, _stream_factory, _transport = _runner(
        config,
        registry,
        invalid_initial_update=True,
    )

    result = runner.run()

    assert result.status == "SHADOW01_BLOCKED_STREAM_SMOKE_INCOMPLETE"
    assert result.stream.error_code == "SHADOW01_STREAM_QUOTE_REJECTION_UNCLASSIFIED"
    assert result.stream.invalid_update_symbols == ("EURUSD",)
    assert result.stream.invalid_reason_counts == (
        ("invalid_ask", 0),
        ("invalid_bid", 0),
        ("invalid_timestamp", 0),
        ("item_resolution_failure", 0),
        ("missing_ask", 0),
        ("missing_bid", 0),
        ("missing_timestamp", 0),
        ("stale_timestamp", 0),
    )
    assert "EURUSD" in result.stream.no_update_symbols


def test_failed_stream_disconnect_does_not_trigger_additional_rest_history_read(
    config_and_registry: tuple[ShadowTournamentConfig, ShadowMarketRegistry],
) -> None:
    config, registry = config_and_registry
    timeline: list[str] = []
    transport = RecordingReadOnlyTransport(timeline=timeline)
    runner, _rest_factory, stream_factory, _transport = _runner(
        config,
        registry,
        transport=transport,
        stream_disconnect_fails=True,
        timeline=timeline,
    )

    result = runner.run()

    assert result.status == "SHADOW01_BLOCKED_STREAM_SESSION_CLEANUP_FAILED"
    assert result.stream.disconnect_result == "FAILED"
    assert stream_factory.transport.calls[-1] == ("disconnect", ())
    assert timeline.index("stream:disconnect") > max(
        index for index, event in enumerate(timeline) if event.startswith("rest:GET:/prices/")
    )


def test_database_change_after_smoke_is_a_contamination_block(
    config_and_registry: tuple[ShadowTournamentConfig, ShadowMarketRegistry],
) -> None:
    config, registry = config_and_registry
    states = iter(
        (
            SmokeDatabaseState(True, False, 0, 0),
            SmokeDatabaseState(True, True, 1, 0),
        )
    )
    runner, _rest_factory, _stream_factory, _transport = _runner(
        config,
        registry,
        database_reader=lambda _path: next(states),
    )

    result = runner.run()

    assert result.status == "SHADOW01_BLOCKED_PROSPECTIVE_DATA_CONTAMINATION"


def test_runner_module_has_no_runtime_store_execution_or_write_surface() -> None:
    source_path = Path(inspect.getsourcefile(smoke_module) or "")
    source = source_path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported_modules = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module is not None
    }

    assert not any(
        module.startswith(
            (
                "src.ig_trader.shadow01.runtime",
                "src.ig_trader.shadow01.storage",
                "src.ig_trader.shadow01.outcomes",
                "src.ig_trader.execution",
                "src.ig_trader.demo_execution",
            )
        )
        for module in imported_modules
    )
    assert ".write_text(" not in source
    assert ".mkdir(" not in source
    assert '"/positions' not in source
    assert '"/workingorders' not in source
