"""Execution Engine to place real trades on IG."""

from __future__ import annotations

import structlog

from src.ig_trader.models import Signal, SignalDirection
from src.ig_trader.session import SessionManager

logger = structlog.get_logger(__name__)


class ExecutionEngine:
    """Handles sending orders to the IG API."""

    def __init__(self, session: SessionManager) -> None:
        self.session = session

    def execute_trade(
        self,
        signal: Signal,
        size: float,
        sl_pips: int,
        tp_pips: int,
        pip_value: float | None = None,
    ) -> bool:
        """
        Opens a position based on a signal, with SL/TP in pips.

        Args:
            signal: The trade signal.
            size: Lot size.
            sl_pips: Stop loss distance in pips.
            tp_pips: Take profit distance in pips.
            pip_value: IG point value, optional. If None, we approximate.

        Returns:
            True if order accepted, False otherwise.
        """
        logger.info(
            "execution_attempt",
            epic=signal.epic,
            direction=signal.direction.value,
            size=size,
            sl_pips=sl_pips,
            tp_pips=tp_pips,
        )

        ig_direction = "BUY" if signal.direction == SignalDirection.BUY else "SELL"

        # IG uses "points", not "pips". For majors, 1 pip ≈ 0.0001.
        # Simple approximation; we can refine later using market metadata.
        point_size = pip_value or 0.0001

        price = signal.price
        if ig_direction == "BUY":
            stop_level = price - sl_pips * point_size
            limit_level = price + tp_pips * point_size
        else:
            stop_level = price + sl_pips * point_size
            limit_level = price - tp_pips * point_size

        endpoint = "/positions/otc"
        payload = {
            "epic": signal.epic,
            "direction": ig_direction,
            "orderType": "MARKET",
            "size": str(size),
            "expiry": "DFB",
            "guaranteedStop": False,
            "currencyCode": "EUR",  # adjust to your account currency
            "forceOpen": True,
            "stopLevel": round(stop_level, 5),
            "limitLevel": round(limit_level, 5),
        }

        response = self.session.authorized_request(
            "POST",
            endpoint,
            json=payload,
            headers={"VERSION": "2"},
        )

        if response.status_code == 200:
            deal_ref = response.json().get("dealReference")
            logger.info("execution_success", deal_reference=deal_ref)
            return True

        logger.error(
            "execution_failed",
            status=response.status_code,
            error=response.text,
        )
        return False
