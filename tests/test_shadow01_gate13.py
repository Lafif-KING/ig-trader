"""Gate 13 offline tests for the bounded V2 declared-hours probe."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from src.ig_trader.shadow01.config import ShadowTournamentConfig, load_config
from src.ig_trader.shadow01.live_v2_schedule_contract_probe import Gate13V2ScheduleProbe
from src.ig_trader.shadow01.local_demo_read_only import LocalDemoReadOnlyStatus
from src.ig_trader.shadow01.read_only_broker import Shadow01ReadOnlyBroker
from src.ig_trader.shadow01.registry import ShadowMarketRegistry, load_verified_dq03_registry
from src.ig_trader.shadow01.schedule_contract import v2_schedule_response_contract
from tests.shadow01_dq03_fixtures import write_verified_dq03_documents


class Gate13Transport:
    """No-network V2 transport that exposes only sanitized shape evidence."""

    def __init__(self, document: dict[str, object]) -> None:
        self.document = document
        self.calls: list[tuple[str, str, dict[str, Any]]] = []
        self.outbound_http_request_count = 0
        self.logout_calls = 0
        self._contract: dict[str, object] | None = None

    def authorized_request(self, method: str, endpoint: str, **kwargs: Any) -> dict[str, object]:
        self.outbound_http_request_count += 1
        self.calls.append((method, endpoint, kwargs))
        if method == "POST" and endpoint == "/session":
            return {"authenticated": True}
        if method == "GET" and endpoint.startswith("/markets/") and kwargs == {"api_version": "2"}:
            self._contract = v2_schedule_response_contract(
                response_status=200,
                dispatched_version="2",
                document=self.document,
            )
            return self.document
        raise AssertionError(f"unexpected Gate13 route: {method} {endpoint}")

    def consume_v2_schedule_response_contract(self) -> dict[str, object] | None:
        value = self._contract
        self._contract = None
        return value

    def logout(self) -> bool:
        self.logout_calls += 1
        self.outbound_http_request_count += 1
        return True


class Gate13Factory:
    max_http_attempts = 1

    def __init__(self, broker: Shadow01ReadOnlyBroker) -> None:
        self.broker = broker

    def status(self) -> LocalDemoReadOnlyStatus:
        return LocalDemoReadOnlyStatus(
            ready=True,
            reason_code="SHADOW01_DEMO_READ_ONLY_READY",
            execution_authority="OFF",
            demo_mode=True,
            demo_endpoint=True,
            local_operator=True,
            paper_trading=True,
            expected_demo_account_configured=True,
            credentials_present=True,
        )

    def build(self) -> Shadow01ReadOnlyBroker:
        return self.broker


@pytest.fixture
def config_and_registry(tmp_path: Path) -> tuple[ShadowTournamentConfig, ShadowMarketRegistry]:
    config = load_config()
    write_verified_dq03_documents(tmp_path, config)
    return config, load_verified_dq03_registry(config, tmp_path / "instrument_registry.json")


def _run(
    *,
    document: dict[str, object],
    config: ShadowTournamentConfig,
    registry: ShadowMarketRegistry,
    registry_path: Path,
) -> tuple[object, Gate13Transport]:
    transport = Gate13Transport(document)
    result = Gate13V2ScheduleProbe(
        authorization="SHADOW01_GATE13_V2_DECLARED_HOURS_PROBE",
        rest_factory=Gate13Factory(Shadow01ReadOnlyBroker(transport)),
        config_loader=lambda: config,
        registry_loader=lambda _config, _path: registry,
        registry_path=registry_path,
    ).run()
    return result, transport


def test_gate13_v2_available_is_exactly_three_calls_and_never_reports_values(
    config_and_registry: tuple[ShadowTournamentConfig, ShadowMarketRegistry], tmp_path: Path
) -> None:
    config, registry = config_and_registry
    source_value = "source-value-not-for-output"
    result, transport = _run(
        document={
            "instrument": {
                "openingHours": {
                    "marketTimes": [{"openTime": source_value, "closeTime": source_value}]
                },
                "epic": source_value,
            },
            "snapshot": {"bid": source_value},
        },
        config=config,
        registry=registry,
        registry_path=tmp_path / "instrument_registry.json",
    )
    output = result.document()

    assert result.status == "SHADOW01_V2_DECLARED_HOURS_AVAILABLE"
    assert output["v2_probe"] == {
        "method": "GET",
        "route_category": "market-details",
        "requested_version": "2",
        "dispatched_version": "2",
        "http_status": 200,
        "top_level_key_names": ["instrument", "snapshot"],
        "instrument": {
            "present": True,
            "type": "object",
            "key_names": ["epic", "openingHours"],
            "unknown_key_count": 0,
        },
        "openingHours": {"present": True, "type": "object", "is_null": False},
        "marketTimes": {"present": True, "type": "array", "count": 1},
        "openTime": {"present": True, "type": "string"},
        "closeTime": {"present": True, "type": "string"},
    }
    assert output["ig_calls"] == {
        "maximum": 3,
        "observed_physical": 3,
        "auth": 1,
        "account": 0,
        "v2_market_details": 1,
        "v3_market_details": 0,
        "history": 0,
        "stream": 0,
        "logout": 1,
    }
    assert transport.calls[0] == ("POST", "/session", {})
    assert transport.calls[1][0] == "GET"
    assert transport.calls[1][1].startswith("/markets/")
    assert transport.calls[1][2] == {"api_version": "2"}
    assert transport.logout_calls == 1
    assert source_value not in str(output)


def test_gate13_v2_null_hours_requires_the_human_session_clock_gate(
    config_and_registry: tuple[ShadowTournamentConfig, ShadowMarketRegistry], tmp_path: Path
) -> None:
    config, registry = config_and_registry
    result, transport = _run(
        document={"instrument": {"openingHours": None}},
        config=config,
        registry=registry,
        registry_path=tmp_path / "instrument_registry.json",
    )

    assert result.status == "SHADOW01_V2_DECLARED_HOURS_NULL"
    assert result.document()["v2_probe"]["openingHours"] == {
        "present": True,
        "type": "null",
        "is_null": True,
    }
    assert transport.outbound_http_request_count == 3
