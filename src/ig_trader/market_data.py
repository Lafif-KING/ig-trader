"""Market data client for IG REST API (prices -> pandas DataFrame)."""

from typing import Any

import pandas as pd
import structlog

from src.ig_trader.session import SessionManager

logger = structlog.get_logger(__name__)


class MarketDataClient:
    def __init__(self, session: SessionManager | None = None) -> None:
        self.session = session or SessionManager()

    def get_prices(
        self, epic: str, resolution: str, max_points: int = 100
    ) -> pd.DataFrame:
        """
        Fetch OHLC prices from IG REST API.
        epic: e.g., 'CS.D.EURUSD.MINI.IP'
        resolution: e.g., 'MINUTE', 'HOUR', 'DAY'
        """
        endpoint = f"/prices/{epic}"
        params = {"resolution": resolution, "max": max_points}
        resp = self.session.authorized_request(
            "GET",
            endpoint,
            params=params,
            headers={"VERSION": "3"},
        )
        if resp.status_code != 200:
            logger.error("get_prices_failed", status=resp.status_code, text=resp.text)
            raise RuntimeError(f"Failed to get prices: {resp.status_code}")

        data = resp.json()
        candles: list[dict[str, Any]] = data.get("prices", []) or data.get(
            "candles", []
        )
        if not candles:
            cols = ["time", "open", "high", "low", "close", "volume"]
            df_empty = pd.DataFrame(columns=cols)
            return df_empty.set_index("time")

        def price(block: dict[str, Any] | None) -> float | None:
            if not block:
                return None
            lt = block.get("lastTraded")
            if lt is not None:
                return float(lt)
            bid, ask = block.get("bid"), block.get("ask")
            if bid is not None and ask is not None:
                return (float(bid) + float(ask)) / 2.0
            return None

        rows: list[dict[str, Any]] = []
        for c in candles:
            t = c.get("snapshotTimeUTC") or c.get("snapshotTime")
            rows.append(
                {
                    "time": pd.to_datetime(t, utc=True),
                    "open": price(c.get("openPrice")),
                    "high": price(c.get("highPrice")),
                    "low": price(c.get("lowPrice")),
                    "close": price(c.get("closePrice")),
                    "volume": c.get("volume"),
                }
            )

        df = pd.DataFrame(rows).dropna(subset=["open", "high", "low", "close"])
        df = df.set_index("time").sort_index()
        return df
