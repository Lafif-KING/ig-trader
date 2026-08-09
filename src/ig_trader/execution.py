"""Execution Engine to place real trades on IG."""

import structlog

from src.ig_trader.models import Signal, SignalDirection
from src.ig_trader.session import SessionManager

logger = structlog.get_logger(__name__)


class ExecutionEngine:
    """Handles sending orders to the IG API."""

    def __init__(self, session: SessionManager):
        self.session = session

    def execute_trade(self, signal: Signal, size: float) -> bool:
        """Opens a position based on a signal."""
        logger.info(
            "execution_attempt", epic=signal.epic, direction=signal.direction.value, size=size
        )

        # Convert our direction to IG format
        ig_direction = "BUY" if signal.direction == SignalDirection.BUY else "SELL"

        endpoint = "/positions/otc"
        payload = {
            "epic": signal.epic,
            "direction": ig_direction,
            "orderType": "MARKET",
            "size": str(size),
            "expiry": "DFB",
            "guaranteedStop": False,
            "currencyCode": "USD",
            "forceOpen": True,
        }

        response = self.session.authorized_request(
            "POST", endpoint, json=payload, headers={"VERSION": "2"}
        )

        if response.status_code == 200:
            deal_ref = response.json().get("dealReference")
            logger.info("execution_success", deal_reference=deal_ref)
            return True
        else:
            logger.error("execution_failed", status=response.status_code, error=response.text)
            return False
