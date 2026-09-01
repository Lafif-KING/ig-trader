"""Tests for SessionManager using respx for HTTP mocking."""

import re
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import httpx
import pytest
import respx

from src.ig_trader.session import SessionManager


@respx.mock
@patch("src.ig_trader.session.settings")
def test_login_success(mock_settings: object) -> None:
    """Test successful login."""
    mock_settings.ig_identifier = "test@example.com"
    mock_settings.ig_password = "password123"
    mock_settings.ig_base_url = "https://demo-api.ig.com"
    mock_settings.ig_api_key = "test-api-key"
    mock_settings.session_timeout_seconds = 21600

    respx.post("https://demo-api.ig.com/session").mock(
        return_value=httpx.Response(
            200,
            headers={"CST": "cst123", "X-SECURITY-TOKEN": "sec123"},
            json={"currentAccountId": "ACC-001"},
        )
    )
    sm = SessionManager()
    assert sm.login() is True
    assert sm.is_authenticated() is True
    assert sm.account_id == "ACC-001"


@respx.mock
@patch("src.ig_trader.session.settings")
def test_login_failure(mock_settings: object) -> None:
    """Test failed login."""
    mock_settings.ig_identifier = "test@example.com"
    mock_settings.ig_password = "password123"
    mock_settings.ig_base_url = "https://demo-api.ig.com"
    mock_settings.ig_api_key = "test-api-key"
    mock_settings.session_timeout_seconds = 21600

    respx.post("https://demo-api.ig.com/session").mock(
        return_value=httpx.Response(403, json={"errorCode": "error"})
    )
    sm = SessionManager()
    assert sm.login() is False
    assert sm.is_authenticated() is False


@pytest.mark.parametrize("status_code", (200, 204))
@respx.mock
def test_logout_success_uses_v1_and_accepts_ig_success_statuses(status_code: int) -> None:
    """Test successful logout with IG's reviewed cleanup response contract."""
    route = respx.delete(re.compile(r".*/session$")).mock(return_value=httpx.Response(status_code))

    sm = SessionManager()
    sm.cst = "cst123"
    sm.x_security_token = "sec123"
    sm.account_id = "ACC-001"
    # Use timezone-aware date
    sm.token_expiry = datetime.now(UTC) + timedelta(hours=1)
    assert sm.logout() is True
    assert sm.is_authenticated() is False
    assert route.calls.last is not None
    assert route.calls.last.request.headers["VERSION"] == "1"


@respx.mock
@patch("src.ig_trader.session.settings")
def test_authorized_403_notifies_observer_without_resetting_session_state(
    mock_settings: object,
) -> None:
    mock_settings.ig_base_url = "https://demo-api.ig.com"
    mock_settings.ig_api_key = "test-api-key"
    mock_settings.session_timeout_seconds = 21600
    route = respx.get("https://demo-api.ig.com/markets/CS.TEST.IP").mock(
        side_effect=[
            httpx.Response(403, json={"errorCode": "error.public-api.access-denied"}),
            httpx.Response(200, json={}),
        ]
    )
    observed_statuses: list[int] = []
    sm = SessionManager(
        response_error_observer=lambda response: observed_statuses.append(response.status_code)
    )
    sm.cst = "cst-for-test"
    sm.x_security_token = "xst-for-test"
    sm.account_id = "account-for-test"
    sm.token_expiry = datetime.now(UTC) + timedelta(hours=1)

    response = sm.authorized_request("GET", "markets/CS.TEST.IP", headers={"VERSION": "4"})
    follow_up = sm.authorized_request("GET", "markets/CS.TEST.IP", headers={"VERSION": "4"})

    assert response.status_code == 403
    assert follow_up.status_code == 200
    assert observed_statuses == [403]
    assert sm.cst == "cst-for-test"
    assert sm.x_security_token == "xst-for-test"
    assert sm.account_id == "account-for-test"
    assert [call.request.headers["VERSION"] for call in route.calls] == ["4", "4"]


@respx.mock
@patch("src.ig_trader.session.settings")
def test_authorized_v3_proves_the_final_http_dispatch_version(mock_settings: object) -> None:
    """The safe observer sees only VERSION, while respx proves the wire header."""

    mock_settings.ig_base_url = "https://demo-api.ig.com"
    mock_settings.ig_api_key = "test-api-key"
    mock_settings.session_timeout_seconds = 21600
    route = respx.get("https://demo-api.ig.com/markets/CS.TEST.IP").mock(
        return_value=httpx.Response(200, json={})
    )
    observed: list[tuple[str, str, str | None]] = []
    sm = SessionManager(
        request_version_observer=lambda method, endpoint, version: observed.append(
            (method, endpoint, version)
        )
    )
    sm.cst = "cst-for-test"
    sm.x_security_token = "xst-for-test"
    sm.account_id = "account-for-test"
    sm.token_expiry = datetime.now(UTC) + timedelta(hours=1)

    response = sm.authorized_request("GET", "markets/CS.TEST.IP", headers={"VERSION": "3"})

    assert response.status_code == 200
    assert observed == [("GET", "markets/CS.TEST.IP", "3")]
    assert route.calls.last is not None
    assert route.calls.last.request.headers["VERSION"] == "3"


@respx.mock
@patch("src.ig_trader.session.settings")
def test_single_attempt_session_does_not_retry_a_connect_failure(mock_settings: object) -> None:
    """Gate12 can use a SessionManager with a hard one-physical-attempt bound."""

    mock_settings.ig_base_url = "https://demo-api.ig.com"
    mock_settings.ig_api_key = "test-api-key"
    mock_settings.session_timeout_seconds = 21600
    route = respx.get("https://demo-api.ig.com/markets/CS.TEST.IP").mock(
        side_effect=httpx.ConnectError("test-only connection failure")
    )
    sm = SessionManager(max_retries=1)
    sm.cst = "cst-for-test"
    sm.x_security_token = "xst-for-test"
    sm.account_id = "account-for-test"
    sm.token_expiry = datetime.now(UTC) + timedelta(hours=1)

    with pytest.raises(httpx.ConnectError):
        sm.authorized_request("GET", "markets/CS.TEST.IP", headers={"VERSION": "3"})

    assert route.call_count == 1
