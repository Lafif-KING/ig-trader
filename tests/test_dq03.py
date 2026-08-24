"""Offline contract tests for the DQ-03 resolver and bounded data acquisition."""

# ruff: noqa: E501

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from src.ig_trader.dq03.acquisition import DQ03HistoryAcquirer
from src.ig_trader.dq03.artifacts import phase_context_matches, write_dq03_artifacts
from src.ig_trader.dq03.models import DataStatus, DQ03Status, RequestCounters
from src.ig_trader.dq03.phases import phase_context
from src.ig_trader.dq03.registry import SEARCH_REGISTRY
from src.ig_trader.dq03.resolver import DQ03InstrumentResolver
from src.ig_trader.dq03.strategy_lab import build_strategy_lab_context
from src.ig_trader.strategy_lab.models import INITIAL_INSTRUMENT_REGISTRY

NOW = datetime(2026, 8, 24, tzinfo=UTC)


@dataclass(frozen=True)
class FakeMarket:
    epic: str
    display_name: str | None
    asset_class: str | None
    expiry: str | None = "DFB"
    market_status: str | None = "TRADEABLE"
    currency: str | None = "USD"
    minimum_deal_size: Decimal | None = Decimal("1")
    minimum_stop_distance: Decimal | None = Decimal("2")
    decimal_places: int | None = 4
    pip_or_tick_size: Decimal | None = Decimal("0.0001")
    value_of_one_pip: Decimal | None = Decimal("1")
    streaming_available: bool | None = True
    bid: Decimal | None = Decimal("1.1000")
    offer: Decimal | None = Decimal("1.1002")
    observed_at: datetime = NOW
    controlled_risk_supported: bool | None = False


class FakeTransport:
    def __init__(
        self, searches: dict[str, tuple[dict[str, object], ...]], markets: dict[str, FakeMarket]
    ) -> None:
        self.searches = searches
        self.markets = markets
        self.search_calls: list[str] = []
        self.market_calls: list[str] = []
        self.history_calls: list[tuple[str, str, int]] = []

    def search_markets(self, term: str) -> tuple[dict[str, object], ...]:
        self.search_calls.append(term)
        return self.searches.get(term, ())

    def get_market(self, epic: str) -> FakeMarket:
        self.market_calls.append(epic)
        return self.markets[epic]

    def get_historical_prices(self, epic: str, resolution: str, points: int) -> dict[str, object]:
        self.history_calls.append((epic, resolution, points))
        return {
            "prices": [
                _price("2026-08-21T08:00:00Z", "1.1000"),
                _price("2026-08-21T08:05:00Z", "1.1002"),
            ]
        }


def _candidate(epic: str, name: str, asset_type: str, expiry: str = "DFB") -> dict[str, object]:
    return {
        "epic": epic,
        "name": name,
        "type": asset_type,
        "expiry": expiry,
        "market_status": "TRADEABLE",
    }


def _price(timestamp: str, value: str) -> dict[str, object]:
    price = {"bid": value, "offer": str(Decimal(value) + Decimal("0.0002"))}
    return {
        "snapshotTimeUTC": timestamp,
        "openPrice": price,
        "highPrice": {
            "bid": str(Decimal(value) + Decimal("0.0001")),
            "offer": str(Decimal(value) + Decimal("0.0003")),
        },
        "lowPrice": {
            "bid": str(Decimal(value) - Decimal("0.0001")),
            "offer": str(Decimal(value) + Decimal("0.0001")),
        },
        "closePrice": price,
    }


def test_search_registry_has_explicit_aliases_for_every_research_symbol() -> None:
    assert set(SEARCH_REGISTRY) == set(INITIAL_INSTRUMENT_REGISTRY)
    assert SEARCH_REGISTRY["GER40"].aliases == ("Germany 40", "Allemagne 40", "DAX")
    assert SEARCH_REGISTRY["EURUSD"].aliases == ("EURUSD", "EUR/USD")


def test_weekend_and_unrelated_security_candidates_are_hard_rejected() -> None:
    searches = {
        "Gold": (
            _candidate("IX.D.SUNGOLD.CEF.IP", "Weekend Spot Gold", "COMMODITIES"),
            _candidate("CS.D.GOLDMINE.IP", "Gold Mining PLC share", "SHARES"),
            _candidate("CS.D.GOLDCORP.IP", "Golden Ocean Group Ltd", "SHARES"),
        )
    }
    result = DQ03InstrumentResolver(FakeTransport(searches, {}), clock=lambda: NOW).resolve_symbol(
        "XAUUSD"
    )

    assert result.classification is DQ03Status.UNSUPPORTED_PRODUCT
    assert all(not candidate.selected for candidate in result.candidates)
    assert any(
        "Weekend" in reason for candidate in result.candidates for reason in candidate.reasons
    )
    assert any(
        "excluded equity" in reason
        for candidate in result.candidates
        for reason in candidate.reasons
    )


def test_french_us_crude_identity_is_an_explicit_energy_alias() -> None:
    epic = "CC.D.CL.UEF.IP"
    searches = {"US Crude": (_candidate(epic, "Pétrole - US Brut Léger (1$)", "COMMODITIES"),)}
    market = FakeMarket(epic, "Pétrole - US Brut Léger (1$)", "COMMODITIES")

    result = DQ03InstrumentResolver(
        FakeTransport(searches, {epic: market}), clock=lambda: NOW
    ).resolve_symbol("USCRUDE")

    assert result.classification is DQ03Status.VERIFIED
    assert result.selected_epic == epic


def test_fx_mini_preference_selects_mini_contract_after_complete_metadata() -> None:
    standard = "CS.D.EURUSD.CEF.IP"
    mini = "CS.D.EURUSD.CEFM.IP"
    searches = {
        "EURUSD": (
            _candidate(standard, "EUR/USD", "CURRENCIES"),
            _candidate(mini, "EUR/USD Mini", "CURRENCIES"),
        ),
        "EUR/USD": (_candidate(mini, "EUR/USD Mini", "CURRENCIES"),),
    }
    markets = {
        standard: FakeMarket(standard, "EUR/USD", "CURRENCIES", minimum_deal_size=Decimal("1")),
        mini: FakeMarket(mini, "EUR/USD Mini", "CURRENCIES", minimum_deal_size=Decimal("1")),
    }
    result = DQ03InstrumentResolver(
        FakeTransport(searches, markets), clock=lambda: NOW
    ).resolve_symbol("EURUSD")

    assert result.classification is DQ03Status.VERIFIED
    assert result.selected_epic == mini
    assert "Mini contract preference" in " ".join(result.selection_reasons)


def test_cash_index_and_small_contract_preferences_are_deterministic() -> None:
    one_euro = "IX.D.DAX.IFMM.IP"
    five_euro = "IX.D.DAX.IMF.IP"
    future = "IX.D.DAX.FWM2.IP"
    searches = {
        "Germany 40": (
            _candidate(one_euro, "Allemagne 40 au comptant (1€)", "INDICES"),
            _candidate(five_euro, "Allemagne 40 au comptant (5€)", "INDICES"),
            _candidate(future, "Germany 40 futures-style", "INDICES", "SEP-26"),
        ),
        "Allemagne 40": (_candidate(one_euro, "Allemagne 40 au comptant (1€)", "INDICES"),),
        "DAX": (_candidate(five_euro, "Allemagne 40 au comptant (5€)", "INDICES"),),
    }
    markets = {
        one_euro: FakeMarket(
            one_euro, "Allemagne 40 au comptant (1€)", "INDICES", minimum_deal_size=Decimal("1")
        ),
        five_euro: FakeMarket(
            five_euro, "Allemagne 40 au comptant (5€)", "INDICES", minimum_deal_size=Decimal("5")
        ),
    }
    result = DQ03InstrumentResolver(
        FakeTransport(searches, markets), clock=lambda: NOW
    ).resolve_symbol("GER40")

    assert result.classification is DQ03Status.VERIFIED
    assert result.selected_epic == one_euro
    assert any("Smallest practical" in reason for reason in result.selection_reasons)
    assert any(
        candidate.epic == future and not candidate.selected for candidate in result.candidates
    )


def test_equal_metal_spot_contracts_remain_ambiguous_without_currency_guessing() -> None:
    eur = "CS.D.CFEGOLD.CFE.IP"
    usd = "CS.D.CFEGOLD.CEF.IP"
    searches = {
        "Gold": (
            _candidate(eur, "Or au comptant (Contrat 1€)", "COMMODITIES"),
            _candidate(usd, "Spot Gold ($1)", "COMMODITIES"),
        ),
        "Spot Gold": (_candidate(usd, "Spot Gold ($1)", "COMMODITIES"),),
        "Or au comptant": (_candidate(eur, "Or au comptant (Contrat 1€)", "COMMODITIES"),),
    }
    markets = {
        eur: FakeMarket(eur, "Or au comptant (Contrat 1€)", "COMMODITIES", currency="EUR"),
        usd: FakeMarket(usd, "Spot Gold ($1)", "COMMODITIES", currency="USD"),
    }
    result = DQ03InstrumentResolver(
        FakeTransport(searches, markets), clock=lambda: NOW
    ).resolve_symbol("XAUUSD")

    assert result.classification is DQ03Status.AMBIGUOUS
    assert result.selected_epic is None


def test_identity_proven_precious_metal_can_use_ig_currencies_type() -> None:
    epic = "CS.D.CFDSILVER.CFM.IP"
    searches = {"Silver": (_candidate(epic, "Argent au comptant mini", "CURRENCIES"),)}
    market = FakeMarket(epic, "Argent au comptant mini", "CURRENCIES")

    result = DQ03InstrumentResolver(
        FakeTransport(searches, {epic: market}), clock=lambda: NOW
    ).resolve_symbol("XAGUSD")

    assert result.classification is DQ03Status.VERIFIED
    assert result.selected_epic == epic


def test_missing_dealing_metadata_blocks_selection_without_defaults() -> None:
    epic = "CS.D.EURUSD.CEFM.IP"
    searches = {"EURUSD": (_candidate(epic, "EUR/USD Mini", "CURRENCIES"),), "EUR/USD": ()}
    markets = {epic: FakeMarket(epic, "EUR/USD Mini", "CURRENCIES", streaming_available=None)}
    result = DQ03InstrumentResolver(
        FakeTransport(searches, markets), clock=lambda: NOW
    ).resolve_symbol("EURUSD")

    assert result.classification is DQ03Status.METADATA_INCOMPLETE
    assert result.selected_epic == epic
    assert result.metadata is not None
    assert result.metadata.missing_fields == ("streaming_prices_available",)


def test_energy_identity_rejects_brent_when_resolving_us_crude() -> None:
    wti = "CS.D.USCRUDE.CFD.IP"
    brent = "CS.D.BRENT.CFD.IP"
    searches = {
        "US Crude": (
            _candidate(wti, "Oil - US Crude", "COMMODITIES"),
            _candidate(brent, "Brent Crude", "COMMODITIES"),
        ),
        "WTI": (_candidate(wti, "Oil - US Crude", "COMMODITIES"),),
        "Oil - US Crude": (_candidate(wti, "Oil - US Crude", "COMMODITIES"),),
    }
    markets = {wti: FakeMarket(wti, "Oil - US Crude", "COMMODITIES")}
    result = DQ03InstrumentResolver(
        FakeTransport(searches, markets), clock=lambda: NOW
    ).resolve_symbol("USCRUDE")

    assert result.classification is DQ03Status.VERIFIED
    assert result.selected_epic == wti
    assert any(candidate.epic == brent for candidate in result.candidates)


def test_search_and_metadata_are_cached_and_history_quota_is_enforced() -> None:
    epic = "CS.D.EURUSD.CEFM.IP"
    searches = {"EURUSD": (_candidate(epic, "EUR/USD Mini", "CURRENCIES"),), "EUR/USD": ()}
    transport = FakeTransport(searches, {epic: FakeMarket(epic, "EUR/USD Mini", "CURRENCIES")})
    resolver = DQ03InstrumentResolver(transport, clock=lambda: NOW)
    first = resolver.resolve_symbol("EURUSD")
    second = resolver.resolve_symbol("EURUSD")

    assert first.classification is DQ03Status.VERIFIED
    assert second.classification is DQ03Status.VERIFIED
    assert transport.search_calls == ["EURUSD"]
    assert transport.market_calls == [epic]
    updated, samples = DQ03HistoryAcquirer(
        transport, resolver.counters, request_budget=1, point_budget=2
    ).validate_verified((first,), points=2)
    assert samples[0].status is DataStatus.BROKER_VALIDATED
    assert updated[0].data_status is DataStatus.BROKER_VALIDATED
    assert resolver.counters.history_points_consumed == 2


def test_all_26_symbols_and_required_sanitized_artifacts_are_retained(tmp_path: Path) -> None:
    transport = FakeTransport({}, {})
    resolver = DQ03InstrumentResolver(transport, clock=lambda: NOW)
    results = resolver.resolve_universe()
    paths = write_dq03_artifacts(tmp_path, results, resolver.counters)

    assert len(results) == 26
    assert {item.classification for item in results} == {DQ03Status.NOT_FOUND}
    assert {
        "instrument_registry.json",
        "candidate_evidence.json",
        "metadata_summary.json",
        "discovery_manifest.json",
        "candidate_demo_execution_registry.json",
    } == set(paths)
    registry = json.loads(paths["instrument_registry.json"].read_text(encoding="utf-8"))
    assert len(registry["instruments"]) == 26
    manifest = json.loads(paths["discovery_manifest.json"].read_text(encoding="utf-8"))
    assert manifest["demo_create_calls"] == manifest["demo_close_calls"] == 0


def test_phase_one_artifacts_require_matching_sanitized_demo_context(tmp_path: Path) -> None:
    transport = FakeTransport({}, {})
    results = DQ03InstrumentResolver(transport, clock=lambda: NOW).resolve_universe()
    context = phase_context("DEMO-TEST")
    write_dq03_artifacts(tmp_path, results, RequestCounters(), run_context=context)

    assert phase_context_matches(tmp_path, context)
    assert not phase_context_matches(tmp_path, phase_context("DIFFERENT-DEMO-TEST"))


def test_cli_is_network_mockable_and_only_writes_ignored_local_evidence(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    from src.ig_trader.dq03 import __main__ as cli

    epic = "CS.D.EURUSD.CEFM.IP"
    transport = FakeTransport(
        {"EURUSD": (_candidate(epic, "EUR/USD Mini", "CURRENCIES"),), "EUR/USD": ()},
        {epic: FakeMarket(epic, "EUR/USD Mini", "CURRENCIES")},
    )
    monkeypatch.setattr(cli, "_read_only_preflight", lambda *_: (transport, "DEMO-TEST"))

    assert cli.main(["resolve", "--symbol", "EURUSD", "--output-directory", str(tmp_path)]) == 0
    assert "DQ03_READ_ONLY_COMPLETE" in capsys.readouterr().out
    assert (tmp_path / "instrument_registry.json").is_file()
    assert not hasattr(transport, "create_position")
    assert not hasattr(transport, "close_position")


def test_strategy_lab_context_preserves_broker_attribution_and_data_unavailability() -> None:
    epic = "CS.D.EURUSD.CEFM.IP"
    transport = FakeTransport(
        {"EURUSD": (_candidate(epic, "EUR/USD Mini", "CURRENCIES"),), "EUR/USD": ()},
        {epic: FakeMarket(epic, "EUR/USD Mini", "CURRENCIES")},
    )
    resolution = DQ03InstrumentResolver(transport, clock=lambda: NOW).resolve_symbol("EURUSD")
    document = build_strategy_lab_context((resolution,))
    item = document["instruments"][0]

    assert item["source_attribution"] == "IG_DEMO_READ_ONLY_METADATA"
    assert item["data_status"] == "DATA_NOT_AVAILABLE"
    assert item["cost_model_status"] == "COST_MODEL_INCOMPLETE"
