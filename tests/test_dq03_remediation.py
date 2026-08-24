"""Regression tests for DQ-03 allowance control and durable partial evidence."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from src.ig_trader.dq03.models import RequestCounters
from src.ig_trader.dq03.rate_limit import DQ03RateLimiter
from src.ig_trader.dq03.resolver import DQ03InstrumentResolver
from src.ig_trader.strategy_lab.models import INITIAL_INSTRUMENTS


def test_rolling_limiter_defaults_to_25_and_waits_without_a_burst() -> None:
    clock = [0.0]
    waits: list[float] = []

    def sleep_for(seconds: float) -> None:
        waits.append(seconds)
        clock[0] += seconds

    counters = RequestCounters()
    limiter = DQ03RateLimiter(counters, clock=lambda: clock[0], sleeper=sleep_for)
    for _ in range(25):
        limiter.before_request("GET", "/markets")
    limiter.before_request("GET", "/markets")

    assert limiter.maximum_requests == 25
    assert waits == [60.0]
    assert counters.rate_limit_wait_count == 1
    assert counters.rate_limit_wait_seconds == 60.0
    assert counters.total_non_trading_requests == 26


class _Market:
    def __init__(self, epic: str, name: str) -> None:
        self.epic = epic
        self.display_name = name
        self.asset_class = "CURRENCIES"
        self.expiry = "DFB"
        self.market_status = "TRADEABLE"
        self.currency = "USD"
        self.minimum_deal_size = Decimal("1")
        self.minimum_stop_distance = Decimal("2")
        self.decimal_places = 4
        self.pip_or_tick_size = Decimal("0.0001")
        self.value_of_one_pip = Decimal("1")
        self.streaming_available = True
        self.bid = Decimal("1.1")
        self.offer = Decimal("1.1002")
        self.observed_at = datetime(2026, 8, 24, tzinfo=UTC)
        self.controlled_risk_supported = False


class _BatchedTransport:
    def __init__(self) -> None:
        self.batch_calls: list[tuple[str, ...]] = []
        self.single_calls: list[str] = []

    def search_markets(self, term: str) -> tuple[dict[str, object], ...]:
        epic = {"EURUSD": "CS.D.EURUSD.CEFM.IP", "GBPUSD": "CS.D.GBPUSD.MINI.IP"}.get(term)
        return (
            (
                {
                    "epic": epic,
                    "name": f"{term} Mini",
                    "type": "CURRENCIES",
                    "expiry": "DFB",
                    "market_status": "TRADEABLE",
                },
            )
            if epic
            else ()
        )

    def get_markets(self, epics: tuple[str, ...]) -> dict[str, _Market]:
        self.batch_calls.append(epics)
        return {epic: _Market(epic, f"{epic} Mini") for epic in epics}

    def get_market(self, epic: str) -> _Market:
        self.single_calls.append(epic)
        return _Market(epic, f"{epic} Mini")


def test_metadata_prefetch_batches_multiple_known_epics() -> None:
    transport = _BatchedTransport()
    instruments = tuple(item for item in INITIAL_INSTRUMENTS if item.symbol in {"EURUSD", "GBPUSD"})
    results = DQ03InstrumentResolver(transport).resolve_universe(instruments)

    assert len(transport.batch_calls) == 1
    assert set(transport.batch_calls[0]) == {"CS.D.EURUSD.CEFM.IP", "CS.D.GBPUSD.MINI.IP"}
    assert transport.single_calls == []
    assert all(item.selected_epic for item in results)


class _UnavailableBatchTransport(_BatchedTransport):
    def get_markets(self, epics: tuple[str, ...]) -> dict[str, _Market]:
        self.batch_calls.append(epics)
        raise RuntimeError("IG Demo request failed with HTTP status 500")


def test_non_403_batch_failure_uses_bounded_single_market_fallback() -> None:
    transport = _UnavailableBatchTransport()
    instrument = next(item for item in INITIAL_INSTRUMENTS if item.symbol == "EURUSD")
    result = DQ03InstrumentResolver(transport).resolve_symbol(instrument)

    assert transport.batch_calls == [("CS.D.EURUSD.CEFM.IP",)]
    assert transport.single_calls == ["CS.D.EURUSD.CEFM.IP"]
    assert result.selected_epic == "CS.D.EURUSD.CEFM.IP"


class _UnavailableMetadataTransport(_UnavailableBatchTransport):
    def get_market(self, epic: str) -> _Market:
        self.single_calls.append(epic)
        raise RuntimeError("IG Demo request failed with HTTP status 500")


def test_metadata_failure_retains_candidate_epic_and_unavailability_evidence() -> None:
    transport = _UnavailableMetadataTransport()
    result = DQ03InstrumentResolver(transport).resolve_symbol("EURUSD")

    assert result.classification.value == "METADATA_INCOMPLETE"
    assert result.selected_epic == "CS.D.EURUSD.CEFM.IP"
    assert result.missing_fields == ("metadata_response_unavailable",)
    assert result.candidates[0].missing_fields == ("metadata_response_unavailable",)
