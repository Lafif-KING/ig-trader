"""Explicit, reviewable IG search aliases and identity rules for DQ-03."""

# ruff: noqa: E501

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from src.ig_trader.strategy_lab.models import AssetClass, InstrumentSpec


@dataclass(frozen=True)
class InstrumentSearchRule:
    """Canonical identity is separate from IG's locale-dependent search terms."""

    symbol: str
    asset_class: AssetClass
    aliases: tuple[str, ...]
    identity_terms: tuple[str, ...]


def _fx(symbol: str) -> InstrumentSearchRule:
    return InstrumentSearchRule(
        symbol=symbol,
        asset_class=AssetClass.FX,
        aliases=(symbol, f"{symbol[:3]}/{symbol[3:]}"),
        identity_terms=(symbol,),
    )


SEARCH_REGISTRY: Final[dict[str, InstrumentSearchRule]] = {
    symbol: _fx(symbol)
    for symbol in (
        "EURUSD",
        "GBPUSD",
        "EURGBP",
        "USDJPY",
        "EURJPY",
        "GBPJPY",
        "AUDUSD",
        "NZDUSD",
        "USDCAD",
        "USDCHF",
        "EURCHF",
        "EURAUD",
        "GBPAUD",
        "AUDJPY",
        "CADJPY",
        "CHFJPY",
    )
}
SEARCH_REGISTRY.update(
    {
        "XAUUSD": InstrumentSearchRule(
            "XAUUSD", AssetClass.METAL, ("Gold", "Spot Gold", "Or au comptant"), ("gold",)
        ),
        "XAGUSD": InstrumentSearchRule(
            "XAGUSD", AssetClass.METAL, ("Silver", "Spot Silver", "Argent"), ("silver", "argent")
        ),
        "GER40": InstrumentSearchRule(
            "GER40",
            AssetClass.INDEX,
            ("Germany 40", "Allemagne 40", "DAX"),
            ("germany 40", "allemagne 40", "dax"),
        ),
        "UK100": InstrumentSearchRule(
            "UK100",
            AssetClass.INDEX,
            ("UK 100", "FTSE 100", "FTSE"),
            ("uk 100", "ftse 100", "ftse"),
        ),
        "US500": InstrumentSearchRule(
            "US500",
            AssetClass.INDEX,
            ("US 500", "S&P 500", "S&P500"),
            ("us 500", "sp 500", "s&p 500"),
        ),
        "USTECH100": InstrumentSearchRule(
            "USTECH100",
            AssetClass.INDEX,
            ("US Tech 100", "NASDAQ", "Nasdaq 100"),
            ("us tech 100", "nasdaq", "nasdaq 100"),
        ),
        "US30": InstrumentSearchRule(
            "US30",
            AssetClass.INDEX,
            ("Wall Street", "US 30", "Dow Jones"),
            ("wall street", "us 30", "dow jones"),
        ),
        "FRA40": InstrumentSearchRule(
            "FRA40",
            AssetClass.INDEX,
            ("France 40", "CAC 40", "CAC"),
            ("france 40", "cac 40", "cac"),
        ),
        "USCRUDE": InstrumentSearchRule(
            "USCRUDE",
            AssetClass.ENERGY,
            ("US Crude", "WTI", "Oil - US Crude", "Pétrole - US Brut Léger"),
            ("us crude", "wti", "oil us crude", "us brut léger"),
        ),
        "BRENT": InstrumentSearchRule(
            "BRENT", AssetClass.ENERGY, ("Brent", "Brent Crude"), ("brent",)
        ),
    }
)


def search_rule(instrument: InstrumentSpec | str) -> InstrumentSearchRule:
    """Return the declared rule, never fabricate an alias from an unknown symbol."""

    symbol = instrument if isinstance(instrument, str) else instrument.symbol
    try:
        return SEARCH_REGISTRY[symbol.upper()]
    except KeyError as error:
        raise ValueError(f"DQ-03 search aliases are not defined for {symbol!r}") from error
