"""Tests for SessionManager using respx for HTTP mocking."""

from datetime import datetime, timedelta
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


@respx.mock
def test_logout_success() -> None:
    """Test successful logout."""
    respx.delete("https://demo-api.ig.com/session").mock(
        return_value=httpx.Response(200)
    )
    sm = SessionManager()
    sm.cst = "cst123"
    sm.x_security_token = "sec123"
    sm.account_id = "ACC-001"
    sm.token_expiry = datetime.utcnow() + timedelta(hours=1)
    assert sm.logout() is True
    assert sm.is_authenticated() is False