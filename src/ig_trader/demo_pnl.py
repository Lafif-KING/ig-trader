"""Instrument-aware display P&L for reconciled IG Demo positions.

No generic pip value or EUR conversion is assumed.  The caller must supply
validated broker metadata; otherwise this module returns an explicit unavailable
state rather than a misleading account-currency value.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from src.ig_trader.demo_execution import DemoDirection


@dataclass(frozen=True)
class PositionMark:
    direction: DemoDirection
    size: Decimal
    entry_level: Decimal
    bid: Decimal | None
    offer: Decimal | None
    stop_level: Decimal | None = None


@dataclass(frozen=True)
class PnlContract:
    """Validated IG instrument economics needed for an exact native P&L mark."""

    pip_or_tick_size: Decimal | None
    value_of_one_pip: Decimal | None
    position_currency: str | None
    account_currency: str | None
    conversion_to_account: Decimal | None = None


@dataclass(frozen=True)
class PositionPnl:
    mark_price: Decimal | None
    native_pnl: Decimal | None
    native_currency: str | None
    account_pnl: Decimal | None
    account_currency: str | None
    current_r: Decimal | None
    status: str


def calculate_position_pnl(mark: PositionMark, contract: PnlContract) -> PositionPnl:
    """Mark buys against bid and sells against offer with explicit contract values."""

    mark_price = mark.bid if mark.direction is DemoDirection.BUY else mark.offer
    if (
        mark_price is None
        or mark.size <= 0
        or contract.pip_or_tick_size is None
        or contract.pip_or_tick_size <= 0
        or contract.value_of_one_pip is None
        or contract.value_of_one_pip <= 0
        or not contract.position_currency
    ):
        return PositionPnl(
            mark_price=mark_price,
            native_pnl=None,
            native_currency=contract.position_currency,
            account_pnl=None,
            account_currency=contract.account_currency,
            current_r=None,
            status="PNL_METADATA_INCOMPLETE",
        )
    move = mark_price - mark.entry_level
    if mark.direction is DemoDirection.SELL:
        move = -move
    native_pnl = move / contract.pip_or_tick_size * contract.value_of_one_pip * mark.size
    current_r = _current_r(mark, mark_price)
    if contract.account_currency == contract.position_currency:
        return PositionPnl(
            mark_price,
            native_pnl,
            contract.position_currency,
            native_pnl,
            contract.account_currency,
            current_r,
            "ACCOUNT_CURRENCY_PNL_AVAILABLE",
        )
    if contract.conversion_to_account is None or contract.conversion_to_account <= 0:
        return PositionPnl(
            mark_price,
            native_pnl,
            contract.position_currency,
            None,
            contract.account_currency,
            current_r,
            "ACCOUNT_CURRENCY_PNL_UNAVAILABLE",
        )
    return PositionPnl(
        mark_price,
        native_pnl,
        contract.position_currency,
        native_pnl * contract.conversion_to_account,
        contract.account_currency,
        current_r,
        "ACCOUNT_CURRENCY_PNL_AVAILABLE",
    )


def _current_r(mark: PositionMark, mark_price: Decimal) -> Decimal | None:
    if mark.stop_level is None:
        return None
    initial_risk = abs(mark.entry_level - mark.stop_level)
    if initial_risk <= 0:
        return None
    gain = mark_price - mark.entry_level
    if mark.direction is DemoDirection.SELL:
        gain = -gain
    return gain / initial_risk
