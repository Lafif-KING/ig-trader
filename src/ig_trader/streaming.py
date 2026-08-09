"""Live streaming module using Lightstreamer."""

from typing import Any
import structlog
from lightstreamer.client import LightstreamerClient, Subscription

logger = structlog.get_logger(__name__)


class StreamManager:
    """Handles the live connection to IG's price servers."""

    def __init__(self, endpoint: str, api_key: str, cst: str, security_token: str):
        self.client = LightstreamerClient(None, endpoint)
        self.client.connectionDetails.setUser(api_key)
        self.client.connectionDetails.setPassword(f"CST-{cst}|XST-{security_token}")

    def connect(self) -> None:
        """Start the live connection."""
        logger.info("streaming_connecting")
        self.client.connect()

    def subscribe_to_market(self, epic: str) -> None:
        """Watch a specific market (e.g. EURUSD)."""
        subscription = Subscription("MERGE", [f"L1:{epic}"], ["BID", "OFFER"])
        subscription.addListener(MarketListener(epic))
        self.client.subscribe(subscription)
        logger.info("streaming_subscribed", epic=epic)


class MarketListener:
    """This class reacts every time a new price arrives."""

    def __init__(self, epic: str):
        self.epic = epic

    def onItemUpdate(self, update: Any) -> None:
        """Called by Lightstreamer when a new price arrives."""
        bid = update.getValue("BID")
        ask = update.getValue("OFFER")
        logger.info("price_tick", epic=self.epic, bid=bid, ask=ask)
