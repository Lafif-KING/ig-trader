"""Gate12 tests for the V3 shape proof and seven-call final-clock envelope."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from src.ig_trader.shadow01.bounded_final_clock_smoke import (
    BoundedFinalClockSmokeV12,
    PriorV11ClockEvidence,
)
from src.ig_trader.shadow01.config import ShadowTournamentConfig, load_config
from src.ig_trader.shadow01.live_schedule_contract_probe import Gate12ScheduleContractProbe
from src.ig_trader.shadow01.local_demo_read_only import LocalDemoReadOnlyStatus
from src.ig_trader.shadow01.read_only_broker import Shadow01ReadOnlyBroker
from src.ig_trader.shadow01.registry import ShadowMarketRegistry, load_verified_dq03_registry
from src.ig_trader.shadow01.schedule_contract import v3_schedule_response_contract
from tests.shadow01_dq03_fixtures import write_verified_dq03_documents


class Gate12Transport:
    """A no-network, observer-accounted V3 transport with no response escape hatch."""

    def __init__(
        self,
        *,
        documents: dict[str, dict[str, object]],
        allowance_epic: str | None = None,
        logout_allowance: bool = False,
        logout_error_code: str | None = None,
    ) -> None:
        self.documents = documents
        self.allowance_epic = allowance_epic
        self.logout_allowance = logout_allowance
        self.logout_error_code = logout_error_code
        self.calls: list[tuple[str, str, dict[str, Any]]] = []
        self.logout_calls = 0
        self.outbound_http_request_count = 0
        self._contract: dict[str, object] | None = None
        self._diagnostic: dict[str, int | str | None] | None = None

    def authorized_request(self, method: str, endpoint: str, **kwargs: Any) -> dict[str, object]:
        self.outbound_http_request_count += 1
        self.calls.append((method, endpoint, kwargs))
        if method == "POST" and endpoint == "/session":
            return {"authenticated": True}
        if endpoint == "/accounts":
            return {"accounts": []}
        if endpoint.startswith("/markets/") and kwargs.get("api_version") == "3":
            epic = endpoint.rsplit("/", maxsplit=1)[-1]
            if epic == self.allowance_epic:
                self._diagnostic = {
                    "status_code": 403,
                    "upstream_error_code": "error.public-api.exceeded-api-key-allowance",
                }
                self._contract = v3_schedule_response_contract(
                    response_status=403,
                    dispatched_version="3",
                    document=None,
                )
                raise RuntimeError("SHADOW01_READ_ONLY_HTTP_403")
            document = self.documents[epic]
            self._contract = v3_schedule_response_contract(
                response_status=200,
                dispatched_version="3",
                document=document,
            )
            return document
        raise AssertionError(f"unexpected Gate12 route: {method} {endpoint}")

    def account_state_is_valid(self, _document: object) -> bool:
        return True

    def consume_v3_schedule_response_contract(self) -> dict[str, object] | None:
        value = self._contract
        self._contract = None
        return value

    def latest_response_diagnostic(self) -> dict[str, int | str | None] | None:
        return self._diagnostic

    def logout(self) -> bool:
        self.logout_calls += 1
        self.outbound_http_request_count += 1
        if self.logout_allowance or self.logout_error_code is not None:
            self._diagnostic = {
                "status_code": 403,
                "upstream_error_code": (
                    "error.public-api.exceeded-api-key-allowance"
                    if self.logout_allowance
                    else self.logout_error_code
                ),
            }
            raise RuntimeError("SHADOW01_DEMO_SESSION_LOGOUT_FAILED")
        return True


class Gate12Factory:
    max_http_attempts = 1

    def __init__(self, broker: Shadow01ReadOnlyBroker) -> None:
        self.broker = broker
        self.build_calls = 0

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
        self.build_calls += 1
        return self.broker


@pytest.fixture
def config_and_registry(tmp_path: Path) -> tuple[ShadowTournamentConfig, ShadowMarketRegistry]:
    config = load_config()
    write_verified_dq03_documents(tmp_path, config)
    return config, load_verified_dq03_registry(config, tmp_path / "instrument_registry.json")


def _v3_document() -> dict[str, object]:
    return {
        "instrument": {
            "openingHours": {
                "marketTimes": [
                    {
                        "openTime": "00:00",
                        "closeTime": "23:59",
                        "must_not_escape": "source-value-not-for-output",
                    }
                ]
            },
            "epic": "source-value-not-for-output",
            "SECRET_FIELD_MUST_NOT_ESCAPE": "source-value-not-for-output",
        },
        "snapshot": {"bid": "source-value-not-for-output"},
        "SECRET_TOP_LEVEL_MUST_NOT_ESCAPE": "source-value-not-for-output",
    }


def _documents(
    registry: ShadowMarketRegistry,
    *,
    missing_eurusd: bool = False,
) -> dict[str, dict[str, object]]:
    values: dict[str, dict[str, object]] = {}
    for symbol in ("EURUSD", "XAUUSD", "US500", "USTECH100"):
        epic = registry.by_symbol(symbol).epic
        assert isinstance(epic, str)
        values[epic] = _v3_document()
    if missing_eurusd:
        epic = registry.by_symbol("EURUSD").epic
        assert isinstance(epic, str)
        values[epic] = {"instrument": {"epic": "source-value-not-for-output"}}
    return values


def _factory(
    documents: dict[str, dict[str, object]],
    **kwargs: Any,
) -> tuple[Gate12Factory, Gate12Transport]:
    transport = Gate12Transport(documents=documents, **kwargs)
    return Gate12Factory(Shadow01ReadOnlyBroker(transport)), transport


def _prior_v11() -> PriorV11ClockEvidence:
    return PriorV11ClockEvidence(
        source_label="SHADOW01_LIVE_READONLY_SMOKE_V11",
        operator_attested=True,
        configuration_fingerprint_verified=True,
        registry_fingerprint_verified=True,
        v4_identity_status_verified_count=20,
        history_ready_symbols=("EURUSD", "XAUUSD", "US500", "USTECH100"),
        stream_valid_quote_count=20,
        stream_reconnect_symbols=("EURUSD", "USDJPY", "XAUUSD", "US500"),
        execution_authority="OFF",
        tournament_epoch_created=False,
    )


def test_gate12_probe_is_three_calls_and_never_serializes_source_values(
    config_and_registry: tuple[ShadowTournamentConfig, ShadowMarketRegistry],
    tmp_path: Path,
) -> None:
    config, registry = config_and_registry
    factory, transport = _factory(_documents(registry))
    result = Gate12ScheduleContractProbe(
        authorization="SHADOW01_GATE12_SCHEDULE_CONTRACT_PROBE",
        rest_factory=factory,
        config_loader=lambda: config,
        registry_loader=lambda _config, _path: registry,
        registry_path=tmp_path / "instrument_registry.json",
    ).run()
    document = result.document()

    assert result.status == "SHADOW01_GATE12_SCHEDULE_CONTRACT_PROBE_PASS"
    assert document["ig_call_budget"] == {
        "maximum_physical": 3,
        "observed_physical": 3,
        "logical": {
            "auth": 1,
            "account": 0,
            "v4_market": 0,
            "v3_schedule": 1,
            "history": 0,
            "successful_logout": 1,
        },
    }
    assert (
        document["schedule_contract"]["EURUSD"]["response"]["http_client_dispatch_VERSION"] == "3"
    )
    assert document["schedule_contract"]["EURUSD"]["response"]["top_level_key_names"] == [
        "instrument",
        "snapshot",
    ]
    assert document["schedule_contract"]["EURUSD"]["response"]["instrument"] == {
        "present": True,
        "type": "object",
        "key_names": ["epic", "openingHours"],
        "unknown_key_count": 1,
    }
    assert document["schedule_contract"]["EURUSD"]["response"]["openingHours"] == {
        "present": True,
        "type": "object",
    }
    assert transport.calls == [
        ("POST", "/session", {}),
        ("GET", "/markets/TEST.EURUSD", {"api_version": "3"}),
    ]
    assert transport.logout_calls == 1
    assert "source-value-not-for-output" not in str(document)
    assert "00:00" not in str(document)
    assert "23:59" not in str(document)
    assert "SECRET_TOP_LEVEL_MUST_NOT_ESCAPE" not in str(document)
    assert "SECRET_FIELD_MUST_NOT_ESCAPE" not in str(document)


def test_gate12_probe_uses_xauusd_only_for_an_explicit_missing_hours_comparison(
    config_and_registry: tuple[ShadowTournamentConfig, ShadowMarketRegistry],
    tmp_path: Path,
) -> None:
    config, registry = config_and_registry
    factory, transport = _factory(_documents(registry, missing_eurusd=True))
    result = Gate12ScheduleContractProbe(
        authorization="SHADOW01_GATE12_SCHEDULE_CONTRACT_PROBE",
        rest_factory=factory,
        config_loader=lambda: config,
        registry_loader=lambda _config, _path: registry,
        registry_path=tmp_path / "instrument_registry.json",
        compare_xau_if_eurusd_hours_absent=True,
    ).run()

    assert result.status == "SHADOW01_GATE12_V3_HOURS_HUMAN_GATE_REQUIRED"
    assert result.eurusd_contract is not None
    assert result.eurusd_contract["openingHours"] == {"present": False, "type": None}
    assert result.xauusd_result == "PASS"
    assert result.observed_physical_ig_calls == 4
    assert result.document()["scope"]["v2_live_market_reads"] == 0
    assert result.document()["offline_v2_contract_reference"] == {
        "comparison_scope": "OFFLINE_DOCUMENTATION_ONLY",
        "live_v2_request_performed": False,
        "endpoint": "GET /markets/{epic}",
        "request_VERSION": "2",
        "instrument.openingHours": {"documented_type": "object"},
        "instrument.openingHours.marketTimes": {"documented_type": "array"},
        "marketTimes.openTime": {"documented_type": "string"},
        "marketTimes.closeTime": {"documented_type": "string"},
    }
    assert [endpoint for _, endpoint, _ in transport.calls] == [
        "/session",
        "/markets/TEST.EURUSD",
        "/markets/TEST.XAUUSD",
    ]
    assert transport.logout_calls == 1


def test_gate12_probe_stops_after_an_allowance_response_then_attempts_logout(
    config_and_registry: tuple[ShadowTournamentConfig, ShadowMarketRegistry],
    tmp_path: Path,
) -> None:
    config, registry = config_and_registry
    eurusd_epic = registry.by_symbol("EURUSD").epic
    assert isinstance(eurusd_epic, str)
    factory, transport = _factory(_documents(registry), allowance_epic=eurusd_epic)
    result = Gate12ScheduleContractProbe(
        authorization="SHADOW01_GATE12_SCHEDULE_CONTRACT_PROBE",
        rest_factory=factory,
        config_loader=lambda: config,
        registry_loader=lambda _config, _path: registry,
        registry_path=tmp_path / "instrument_registry.json",
        compare_xau_if_eurusd_hours_absent=True,
    ).run()

    assert result.status == "SHADOW01_BLOCKED_IG_API_ALLOWANCE"
    assert result.eurusd_contract is not None
    assert result.eurusd_contract["response_status"] == 403
    assert result.eurusd_contract["document_is_object"] is False
    assert result.xauusd_result == "NOT_REQUESTED"
    assert result.observed_physical_ig_calls == 3
    assert transport.logout_calls == 1


def test_gate12_probe_counts_an_attempted_allowance_failed_logout(
    config_and_registry: tuple[ShadowTournamentConfig, ShadowMarketRegistry],
    tmp_path: Path,
) -> None:
    config, registry = config_and_registry
    factory, transport = _factory(_documents(registry), logout_allowance=True)
    result = Gate12ScheduleContractProbe(
        authorization="SHADOW01_GATE12_SCHEDULE_CONTRACT_PROBE",
        rest_factory=factory,
        config_loader=lambda: config,
        registry_loader=lambda _config, _path: registry,
        registry_path=tmp_path / "instrument_registry.json",
    ).run()

    assert result.status == "SHADOW01_BLOCKED_IG_API_ALLOWANCE"
    assert result.rest_logout_result == "FAILED_ALLOWANCE_AFFECTED"
    assert result.observed_physical_ig_calls == 3
    assert result.counters.session_logout_count == 0
    assert transport.logout_calls == 1


def test_gate12_probe_redacts_an_unrecognized_logout_error_code(
    config_and_registry: tuple[ShadowTournamentConfig, ShadowMarketRegistry],
    tmp_path: Path,
) -> None:
    config, registry = config_and_registry
    factory, _transport = _factory(
        _documents(registry),
        logout_error_code="token-value-must-not-escape",
    )

    result = Gate12ScheduleContractProbe(
        authorization="SHADOW01_GATE12_SCHEDULE_CONTRACT_PROBE",
        rest_factory=factory,
        config_loader=lambda: config,
        registry_loader=lambda _config, _path: registry,
        registry_path=tmp_path / "instrument_registry.json",
    ).run()

    document = result.document()
    assert result.status == "SHADOW01_BLOCKED_GATE12_CLEANUP_FAILED"
    assert document["cleanup"]["rest_logout_upstream_error_code"] is None
    assert "token-value-must-not-escape" not in str(document)


def test_gate12_probe_requires_an_exact_integer_single_attempt_factory(
    config_and_registry: tuple[ShadowTournamentConfig, ShadowMarketRegistry],
    tmp_path: Path,
) -> None:
    config, registry = config_and_registry
    factory, transport = _factory(_documents(registry))
    factory.max_http_attempts = True

    result = Gate12ScheduleContractProbe(
        authorization="SHADOW01_GATE12_SCHEDULE_CONTRACT_PROBE",
        rest_factory=factory,
        config_loader=lambda: config,
        registry_loader=lambda _config, _path: registry,
        registry_path=tmp_path / "instrument_registry.json",
    ).run()

    assert result.status == "SHADOW01_BLOCKED_GATE12_SINGLE_ATTEMPT_TRANSPORT_REQUIRED"
    assert factory.build_calls == 0
    assert transport.outbound_http_request_count == 0


def test_bounded_final_clock_smoke_reuses_labeled_v11_facts_and_is_seven_calls(
    config_and_registry: tuple[ShadowTournamentConfig, ShadowMarketRegistry],
    tmp_path: Path,
) -> None:
    config, registry = config_and_registry
    factory, transport = _factory(_documents(registry))
    result = BoundedFinalClockSmokeV12(
        authorization="SHADOW01_BOUNDED_FINAL_CLOCK_SMOKE_V12",
        prior_v11_evidence=_prior_v11(),
        rest_factory=factory,
        config_loader=lambda: config,
        registry_loader=lambda _config, _path: registry,
        registry_path=tmp_path / "instrument_registry.json",
    ).run()
    document = result.document()

    assert result.status == "SHADOW01_BOUNDED_FINAL_CLOCK_SMOKE_V12_PASS"
    assert document["prior_v11_evidence"]["freshly_revalidated_in_v12"] is False
    assert document["prior_v11_evidence"]["operator_attested"] is True
    assert document["prior_v11_evidence"]["evidence_class"] == (
        "OPERATOR_ATTESTED_SANITIZED_PRIOR_FACTS"
    )
    assert document["rest_budget"]["maximum_requests"] == 7
    assert document["rest_budget"]["reserved_requests"] == 7
    assert document["physical_ig_calls"] == {
        "maximum": 7,
        "observed": 7,
        "single_attempt_transport": True,
    }
    assert document["scope"]["fresh_v4_market_reads"] == 0
    assert document["scope"]["fresh_history_reads"] == 0
    assert document["scope"]["fresh_stream_connections"] == 0
    assert len(document["schedules"]) == 4
    assert len(document["v3_schedule_contracts"]) == 4
    assert all(
        item["http_client_dispatch_VERSION"] == "3" and item["response_status"] == 200
        for item in document["v3_schedule_contracts"]
    )
    assert transport.logout_calls == 1
    assert "source-value-not-for-output" not in str(document)
    assert "00:00" not in str(document)
    assert "00:00" not in repr(result)
    assert "23:59" not in repr(result)


def test_bounded_final_clock_requires_each_v3_read_to_have_wire_header_proof(
    config_and_registry: tuple[ShadowTournamentConfig, ShadowMarketRegistry],
    tmp_path: Path,
) -> None:
    config, registry = config_and_registry
    factory, transport = _factory(_documents(registry))
    original_consume = transport.consume_v3_schedule_response_contract

    def no_header_proof() -> dict[str, object] | None:
        contract = original_consume()
        if contract is not None:
            contract["http_client_dispatch_VERSION"] = None
        return contract

    transport.consume_v3_schedule_response_contract = no_header_proof  # type: ignore[method-assign]
    result = BoundedFinalClockSmokeV12(
        authorization="SHADOW01_BOUNDED_FINAL_CLOCK_SMOKE_V12",
        prior_v11_evidence=_prior_v11(),
        rest_factory=factory,
        config_loader=lambda: config,
        registry_loader=lambda _config, _path: registry,
        registry_path=tmp_path / "instrument_registry.json",
    ).run()

    assert result.status == "SHADOW01_BLOCKED_GATE12_V3_HEADER_UNPROVEN"
    assert result.observed_physical_ig_calls == 4
    assert transport.logout_calls == 1
