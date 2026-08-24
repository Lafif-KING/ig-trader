"""Contract tests for the endpoint-locked real IG Demo adapter."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from src.ig_trader.demo_execution import DemoDirection
from src.ig_trader.demo_transport import (
    IG_DEMO_BASE_URL,
    DemoTransportError,
    IGDemo403Error,
    IGDemoRESTTransport,
    validate_ig_demo_endpoint,
)


class FakeSession:
    account_id = "DEMO-1"

    def __init__(self, *responses: httpx.Response) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, str, dict[str, object]]] = []

    def authorized_request(self, method: str, endpoint: str, **kwargs: object) -> httpx.Response:
        self.calls.append((method, endpoint, kwargs))
        return self.responses.pop(0)


@pytest.mark.parametrize(
    "value",
    [
        None,
        "",
        "http://demo-api.ig.com/gateway/deal",
        "https://api.ig.com/gateway/deal",
        "https://demo-api.ig.com/gateway/deal/",
        "https://demo-api.ig.com/gateway/deal?target=live",
    ],
)
def test_only_the_exact_documented_demo_gateway_is_accepted(value: str | None) -> None:
    with pytest.raises(DemoTransportError):
        validate_ig_demo_endpoint(value)
    assert validate_ig_demo_endpoint(IG_DEMO_BASE_URL) == IG_DEMO_BASE_URL


def test_create_and_close_use_ig_otc_contracts_with_supported_versions() -> None:
    session = FakeSession(
        httpx.Response(200, json={"dealReference": "create-ref"}),
        httpx.Response(200, json={"dealReference": "close-ref"}),
    )
    transport = IGDemoRESTTransport(session=session, base_url=IG_DEMO_BASE_URL)

    created = transport.create_position({"dealReference": "create-ref", "epic": "CS.TEST"})
    closed = transport.close_position(
        {"dealReference": "close-ref", "dealId": "D-1", "direction": "SELL"}
    )

    assert created.deal_reference == "create-ref"
    assert closed.deal_reference == "close-ref"
    assert [(method, endpoint) for method, endpoint, _kwargs in session.calls] == [
        ("POST", "/positions/otc"),
        ("DELETE", "/positions/otc"),
    ]
    assert session.calls[0][2]["headers"] == {
        "VERSION": "2",
        "Accept": "application/json; charset=UTF-8",
        "Content-Type": "application/json",
    }
    assert session.calls[1][2]["headers"]["VERSION"] == "1"


def test_redirect_to_any_other_endpoint_is_rejected_before_json_parsing() -> None:
    session = FakeSession(
        httpx.Response(302, headers={"location": "https://api.ig.com/gateway/deal/positions"})
    )
    transport = IGDemoRESTTransport(session=session, base_url=IG_DEMO_BASE_URL)

    with pytest.raises(DemoTransportError, match="redirected"):
        transport.list_positions()


def test_positions_confirmation_and_market_metadata_are_strictly_parsed() -> None:
    session = FakeSession(
        httpx.Response(
            200,
            json={
                "positions": [
                    {
                        "position": {
                            "dealId": "D-1",
                            "dealReference": "ref-1",
                            "direction": "BUY",
                            "size": 2,
                            "level": 1.2,
                            "stopLevel": 1.1,
                            "limitLevel": 1.3,
                            "currency": "GBP",
                            "createdDateUTC": "2026-08-23T12:00:00Z",
                        },
                        "market": {
                            "epic": "CS.TEST",
                            "instrumentName": "Test",
                            "bid": 1.21,
                            "offer": 1.22,
                        },
                    }
                ]
            },
        ),
        httpx.Response(
            200,
            json={
                "dealReference": "ref-1",
                "dealId": "D-1",
                "dealStatus": "ACCEPTED",
                "status": "OPEN",
                "epic": "CS.TEST",
                "direction": "BUY",
                "size": 2,
            },
        ),
        httpx.Response(
            200,
            json={
                "instrument": {
                    "name": "Test market",
                    "type": "CURRENCIES",
                    "expiry": "DFB",
                    "currencies": [{"code": "GBP"}],
                    "onePipMeans": "0.0001",
                    "valueOfOnePip": "10",
                    "streamingPricesAvailable": True,
                },
                "snapshot": {
                    "marketStatus": "TRADEABLE",
                    "decimalPlacesFactor": 4,
                    "bid": 1.2,
                    "offer": 1.21,
                },
                "dealingRules": {
                    "minDealSize": {"unit": "POINTS", "value": 1},
                    "minNormalStopOrLimitDistance": {"unit": "POINTS", "value": 2},
                },
            },
        ),
    )
    transport = IGDemoRESTTransport(session=session, base_url=IG_DEMO_BASE_URL)

    positions = transport.list_position_details()
    confirmation = transport.get_confirmation("ref-1")
    metadata = transport.get_market("CS.TEST")

    assert positions[0].core_position.direction is DemoDirection.BUY
    assert positions[0].size == Decimal("2")
    assert confirmation is not None and confirmation.deal_id == "D-1"
    assert metadata.minimum_deal_size == Decimal("1")
    assert metadata.minimum_stop_distance == Decimal("2")
    assert metadata.to_execution_metadata().pip_scale == Decimal("0.0001")


def test_transport_source_never_logs_authentication_values() -> None:
    source = (Path(__file__).parents[1] / "src" / "ig_trader" / "demo_transport.py").read_text(
        encoding="utf-8"
    )
    assert "logger" not in source


def test_market_parser_uses_default_currency_text_pip_and_ask_shape() -> None:
    session = FakeSession(
        httpx.Response(
            200,
            json={
                "instrument": {
                    "name": "EUR/USD Mini",
                    "type": "CURRENCIES",
                    "expiry": "-",
                    "currencies": [
                        {"code": "EUR", "isDefault": False},
                        {"code": "USD", "isDefault": True},
                    ],
                    "onePipMeans": "0.0001 USD",
                    "valueOfOnePip": "1.00",
                    "streamingPricesAvailable": True,
                    "contractSize": "10000",
                    "lotSize": "1",
                },
                "snapshot": {
                    "marketStatus": "TRADEABLE",
                    "decimalPlacesFactor": 5,
                    "scalingFactor": 1,
                    "bid": "1.10000",
                    "ask": "1.10020",
                },
                "dealingRules": {
                    "minDealSize": {"unit": "POINTS", "value": "0.1"},
                    "minNormalStopOrLimitDistance": {"unit": "POINTS", "value": "2"},
                },
            },
        )
    )
    metadata = IGDemoRESTTransport(session=session, base_url=IG_DEMO_BASE_URL).get_market("CS.TEST")

    assert metadata.currency == "USD"
    assert metadata.pip_or_tick_size == Decimal("0.0001")
    assert metadata.offer == Decimal("1.10020")
    assert metadata.minimum_stop_distance_unit == "POINTS"
    assert metadata.contract_size == Decimal("10000")


def test_percentage_dealing_rule_is_retained_as_unit_but_not_invented_as_points() -> None:
    session = FakeSession(
        httpx.Response(
            200,
            json={
                "instrument": {"currencies": [{"code": "USD", "isDefault": True}]},
                "snapshot": {},
                "dealingRules": {
                    "minDealSize": {"unit": "PERCENTAGE", "value": "1"},
                    "minNormalStopOrLimitDistance": {"unit": "PERCENTAGE", "value": "2"},
                },
            },
        )
    )
    metadata = IGDemoRESTTransport(session=session, base_url=IG_DEMO_BASE_URL).get_market("CS.TEST")

    assert metadata.minimum_deal_size is None
    assert metadata.minimum_stop_distance is None
    assert metadata.minimum_deal_size_unit == "PERCENTAGE"
    assert metadata.minimum_stop_distance_unit == "PERCENTAGE"


@pytest.mark.parametrize(
    ("error_code", "classification"),
    [
        ("error.public-api.exceeded-account-allowance", "RATE_LIMIT_ACCOUNT"),
        ("error.public-api.exceeded-api-key-allowance", "RATE_LIMIT_API_KEY"),
        ("error.public-api.exceeded-account-historical-data-allowance", "HISTORICAL_ALLOWANCE"),
        ("error.security.api-key-restricted", "API_KEY_INVALID_OR_RESTRICTED"),
    ],
)
def test_403_is_classified_once_without_a_blind_retry(error_code: str, classification: str) -> None:
    session = FakeSession(httpx.Response(403, json={"errorCode": error_code}))
    seen: list[str] = []
    transport = IGDemoRESTTransport(
        session=session,
        base_url=IG_DEMO_BASE_URL,
        error_observer=lambda error: seen.append(error.classification),
    )

    with pytest.raises(IGDemo403Error, match=classification):
        transport.search_markets("EURUSD")

    assert seen == [classification]
    assert len(session.calls) == 1
