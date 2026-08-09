from unittest.mock import patch

import httpx
import pandas as pd
import respx

from src.ig_trader.market_data import MarketDataClient


@respx.mock
@patch("src.ig_trader.session.settings")
def test_get_prices_parses_dataframe(mock_settings: object) -> None:
    # Setup mock settings
    mock_settings.ig_identifier = "test@example.com"
    mock_settings.ig_password = "password123"
    mock_settings.ig_base_url = "https://demo-api.ig.com"
    mock_settings.ig_api_key = "test-api-key"
    mock_settings.session_timeout_seconds = 21600

    # Mock the Login (which is called by authorized_request)
    respx.post("https://demo-api.ig.com/session").mock(
        return_value=httpx.Response(
            200,
            headers={"CST": "mock-cst", "X-SECURITY-TOKEN": "mock-token"},
            json={"currentAccountId": "ACC123"},
        )
    )

    # Mock the Prices call
    payload = {
        "prices": [
            {
                "snapshotTimeUTC": "2024-01-01T00:00:00",
                "openPrice": {"lastTraded": 1.10},
                "highPrice": {"lastTraded": 1.12},
                "lowPrice": {"lastTraded": 1.09},
                "closePrice": {"lastTraded": 1.11},
                "volume": 1000,
            }
        ]
    }
    respx.get("https://demo-api.ig.com/prices/CS.D.EURUSD.MINI.IP").mock(
        return_value=httpx.Response(200, json=payload)
    )

    md = MarketDataClient()
    df = md.get_prices("CS.D.EURUSD.MINI.IP", "MINUTE", max_points=1)

    assert isinstance(df, pd.DataFrame)
    assert not df.empty
    assert "close" in df.columns
