"""Strict local fixture adapters for market and historical data ports."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from math import isfinite
from pathlib import Path
from typing import Any

from src.ig_trader.offline_paper.domain import Candle, Quote

FROZEN_INSTRUMENTS = (
    ("EURGBP", "CS.D.EURGBP.MINI.IP", "EUR", "GBP"),
    ("EURUSD", "CS.D.EURUSD.MINI.IP", "EUR", "USD"),
    ("GBPUSD", "CS.D.GBPUSD.MINI.IP", "GBP", "USD"),
)


@dataclass(frozen=True)
class FixtureAccount:
    account_id: str
    currency: str
    starting_balance: float


@dataclass(frozen=True)
class FixtureInstrument:
    symbol: str
    epic: str
    base_currency: str
    quote_currency: str
    pip_size: float
    pip_value_account_currency: float
    minimum_size: float
    minimum_stop_pips: float


class LocalFixtureData:
    """Immutable, synthetic and explicitly labelled local candle source."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).resolve()
        raw = self.path.read_bytes()
        self.document_fingerprint = hashlib.sha256(raw).hexdigest()
        try:
            document = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ValueError("offline fixture is not valid UTF-8 JSON") from error
        if not isinstance(document, dict):
            raise ValueError("offline fixture root must be an object")
        if (
            document.get("schema_version") != "1.0"
            or document.get("source_type") != "SYNTHETIC_DETERMINISTIC_OFFLINE_PAPER"
            or document.get("execution_mode") != "OFFLINE_PAPER"
        ):
            raise ValueError("offline fixture identity is invalid")
        self.cycle_id = _required_text(document.get("cycle_id"), "cycle_id")
        self.evaluation_time = _utc(document.get("evaluation_time"), "evaluation_time")
        account = document.get("account")
        if not isinstance(account, dict):
            raise ValueError("account fixture is missing")
        self.account = FixtureAccount(
            _required_text(account.get("account_id"), "account_id"),
            _currency(account.get("currency")),
            _positive(account.get("starting_balance"), "starting_balance"),
        )
        instruments = document.get("instruments")
        if not isinstance(instruments, list) or len(instruments) != 3:
            raise ValueError("exactly three frozen fixture instruments are required")
        self._instruments: dict[str, FixtureInstrument] = {}
        self._candles: dict[str, tuple[Candle, ...]] = {}
        self._quotes: dict[str, Quote] = {}
        self._exits: dict[str, Candle] = {}
        observed = []
        for item in instruments:
            if not isinstance(item, dict):
                raise ValueError("instrument fixture is invalid")
            instrument = self._parse_instrument(item)
            if instrument.epic in self._instruments:
                raise ValueError("duplicate fixture EPIC")
            observed.append(
                (
                    instrument.symbol,
                    instrument.epic,
                    instrument.base_currency,
                    instrument.quote_currency,
                )
            )
            self._instruments[instrument.epic] = instrument
            self._candles[instrument.epic] = self._expand_candles(item, instrument)
            self._quotes[instrument.epic] = self._parse_quote(item, instrument)
            self._exits[instrument.epic] = self._parse_exit(item, instrument)
        if tuple(observed) != FROZEN_INSTRUMENTS:
            raise ValueError("fixture universe or order differs from frozen V1")

    @property
    def instruments(self) -> tuple[FixtureInstrument, ...]:
        return tuple(self._instruments[epic] for _, epic, _, _ in FROZEN_INSTRUMENTS)

    def instrument(self, epic: str) -> FixtureInstrument | None:
        return self._instruments.get(epic)

    def quote(self, epic: str, *, as_of: datetime) -> Quote | None:
        quote = self._quotes.get(epic)
        if quote is None or as_of.astimezone(UTC) != self.evaluation_time:
            return None
        return quote if quote.timestamp == self.evaluation_time else None

    def candles(self, epic: str, *, before: datetime) -> tuple[Candle, ...] | None:
        candles = self._candles.get(epic)
        if candles is None or before.astimezone(UTC) != self.evaluation_time:
            return None
        if len(candles) != 60 or candles[-1].timestamp >= before.astimezone(UTC):
            return None
        return candles

    def exit_candle(self, epic: str, *, after: datetime) -> Candle | None:
        candle = self._exits.get(epic)
        if candle is None or after.astimezone(UTC) != self.evaluation_time:
            return None
        return candle if candle.timestamp > after.astimezone(UTC) else None

    def source_references(self, epic: str) -> tuple[dict[str, Any], ...]:
        candles = self._candles.get(epic)
        if candles is None:
            return ()
        encoded = json.dumps(
            [_candle_document(item) for item in candles],
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode()
        return (
            {
                "fixture_sha256": self.document_fingerprint,
                "candle_sha256": hashlib.sha256(encoded).hexdigest(),
                "first_timestamp": candles[0].timestamp.isoformat(),
                "last_timestamp": candles[-1].timestamp.isoformat(),
                "candle_count": len(candles),
            },
        )

    def _parse_instrument(self, item: dict[str, Any]) -> FixtureInstrument:
        return FixtureInstrument(
            _required_text(item.get("symbol"), "symbol"),
            _required_text(item.get("epic"), "epic"),
            _currency(item.get("base_currency")),
            _currency(item.get("quote_currency")),
            _positive(item.get("pip_size"), "pip_size"),
            _positive(item.get("pip_value_account_currency"), "pip_value"),
            _positive(item.get("minimum_size"), "minimum_size"),
            _positive(item.get("minimum_stop_pips"), "minimum_stop_pips"),
        )

    def _expand_candles(
        self,
        item: dict[str, Any],
        instrument: FixtureInstrument,
    ) -> tuple[Candle, ...]:
        generation = item.get("candle_generation")
        if not isinstance(generation, dict):
            raise ValueError("candle_generation is missing")
        count = generation.get("count")
        if isinstance(count, bool) or count != 60:
            raise ValueError("frozen warm-up requires exactly 60 source candles")
        start = _utc(generation.get("start_time"), "start_time")
        if start + timedelta(minutes=60) != self.evaluation_time:
            raise ValueError("candle generation window is not closed at evaluation time")
        start_close = _positive(generation.get("start_close"), "start_close")
        step = _finite(generation.get("step"), "step")
        oscillation = _nonnegative(generation.get("oscillation"), "oscillation")
        range_pips = _positive(generation.get("range_pips"), "range_pips")
        spread_pips = _nonnegative(generation.get("spread_pips"), "spread_pips")
        result = []
        previous = start_close
        for index in range(60):
            close = start_close + step * index
            if oscillation:
                close += oscillation if index % 2 == 0 else -oscillation
            open_price = previous if index else close - step
            half_range = range_pips * instrument.pip_size / 2.0
            bid_high = max(open_price, close) + half_range
            bid_low = min(open_price, close) - half_range
            spread = spread_pips * instrument.pip_size
            result.append(
                Candle(
                    instrument.epic,
                    start + timedelta(minutes=index),
                    open_price,
                    bid_high,
                    bid_low,
                    close,
                    open_price + spread,
                    bid_high + spread,
                    bid_low + spread,
                    close + spread,
                    100.0,
                )
            )
            previous = close
        return tuple(result)

    def _parse_quote(self, item: dict[str, Any], instrument: FixtureInstrument) -> Quote:
        quote = item.get("quote")
        if not isinstance(quote, dict):
            raise ValueError("quote is missing")
        timestamp = _utc(quote.get("timestamp"), "quote timestamp")
        bid = _positive(quote.get("bid"), "quote bid")
        offer = _positive(quote.get("offer"), "quote offer")
        if timestamp != self.evaluation_time or offer <= bid:
            raise ValueError("quote is stale or crossed")
        return Quote(
            instrument.epic,
            bid,
            offer,
            timestamp,
            instrument.pip_size,
            instrument.pip_value_account_currency,
            instrument.minimum_size,
            instrument.minimum_stop_pips,
        )

    def _parse_exit(self, item: dict[str, Any], instrument: FixtureInstrument) -> Candle:
        value = item.get("exit_candle")
        if not isinstance(value, dict):
            raise ValueError("exit candle is missing")
        values = {
            name: _positive(value.get(name), name)
            for name in (
                "bid_open",
                "bid_high",
                "bid_low",
                "bid_close",
                "offer_open",
                "offer_high",
                "offer_low",
                "offer_close",
            )
        }
        if not (
            values["bid_low"]
            <= min(values["bid_open"], values["bid_close"])
            <= max(values["bid_open"], values["bid_close"])
            <= values["bid_high"]
        ):
            raise ValueError("exit bid OHLC is invalid")
        if not (
            values["offer_low"]
            <= min(values["offer_open"], values["offer_close"])
            <= max(values["offer_open"], values["offer_close"])
            <= values["offer_high"]
        ):
            raise ValueError("exit offer OHLC is invalid")
        return Candle(
            instrument.epic,
            _utc(value.get("timestamp"), "exit timestamp"),
            values["bid_open"],
            values["bid_high"],
            values["bid_low"],
            values["bid_close"],
            values["offer_open"],
            values["offer_high"],
            values["offer_low"],
            values["offer_close"],
            _nonnegative(value.get("volume"), "exit volume"),
        )


def _candle_document(candle: Candle) -> dict[str, Any]:
    return {
        "epic": candle.epic,
        "timestamp": candle.timestamp.astimezone(UTC).isoformat(),
        "bid": [candle.bid_open, candle.bid_high, candle.bid_low, candle.bid_close],
        "offer": [
            candle.offer_open,
            candle.offer_high,
            candle.offer_low,
            candle.offer_close,
        ],
        "volume": candle.volume,
    }


def _required_text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(f"{field} is invalid")
    return value


def _currency(value: object) -> str:
    parsed = _required_text(value, "currency")
    if len(parsed) != 3 or not parsed.isalpha() or parsed != parsed.upper():
        raise ValueError("currency is invalid")
    return parsed


def _utc(value: object, field: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"{field} is invalid")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise ValueError(f"{field} must be explicit UTC")
    return parsed.astimezone(UTC)


def _finite(value: object, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError(f"{field} is invalid")
    parsed = float(value)
    if not isfinite(parsed):
        raise ValueError(f"{field} is invalid")
    return parsed


def _positive(value: object, field: str) -> float:
    parsed = _finite(value, field)
    if parsed <= 0:
        raise ValueError(f"{field} must be positive")
    return parsed


def _nonnegative(value: object, field: str) -> float:
    parsed = _finite(value, field)
    if parsed < 0:
        raise ValueError(f"{field} must be nonnegative")
    return parsed
