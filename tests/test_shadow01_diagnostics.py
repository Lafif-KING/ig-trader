"""Regression coverage for Gate-02's non-persisting diagnostic surfaces."""

from __future__ import annotations

import ast
import inspect
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

import src.ig_trader.shadow01.clock_diagnostic as clock_module
import src.ig_trader.shadow01.warmup_diagnostic as warmup_module
from src.ig_trader.shadow01.clock_diagnostic import (
    ClockDiagnosticState,
    ShadowClockDiagnosticError,
    verify_shadow_session_clock,
)
from src.ig_trader.shadow01.config import ShadowTournamentConfig, load_config
from src.ig_trader.shadow01.live_quote import build_ig_price_stream_quote
from src.ig_trader.shadow01.models import AssetClass, MarketDataState, MarketSpec
from src.ig_trader.shadow01.registry import ShadowMarketRegistry
from src.ig_trader.shadow01.warmup_diagnostic import (
    ShadowWarmupDiagnosticError,
    WarmupDataQualityState,
    run_shadow_live_readonly_smoke_warmup_v2,
    run_shadow_warmup_diagnostic,
)

NOW = datetime(2026, 9, 2, 21, 10, tzinfo=UTC)


class RecordingReadOnlyBroker:
    """Fake-only read surface with deliberately visible action attempts."""

    execution_authority = "OFF"

    def __init__(
        self,
        *,
        history_count: int = 300,
        latest_offset: timedelta = timedelta(minutes=10),
        history_error: Exception | None = None,
        history_errors_by_epic: dict[str, Exception] | None = None,
        response_diagnostic: dict[str, int | str | None] | None = None,
    ) -> None:
        self.history_count = history_count
        self.latest_offset = latest_offset
        self.history_error = history_error
        self.history_errors_by_epic = history_errors_by_epic or {}
        self.response_diagnostic = response_diagnostic
        self.calls: list[tuple[object, ...]] = []
        self.metadata_by_epic: dict[str, object] = {}
        self.schedule_by_epic: dict[str, object] = {}

    def read_market(self, epic: str) -> object:
        self.calls.append(("read_market", epic))
        return self.metadata_by_epic.get(epic, _metadata(epic))

    def read_historical_prices(self, epic: str, resolution: str, points: int) -> object:
        self.calls.append(("read_historical_prices", epic, resolution, points))
        if epic in self.history_errors_by_epic:
            raise self.history_errors_by_epic[epic]
        if self.history_error is not None:
            raise self.history_error
        if resolution == "DAY" and points == 5:
            return _clock_history(NOW)
        return _history(NOW, self.history_count, self.latest_offset)

    def read_market_schedule_v3(self, epic: str) -> object:
        self.calls.append(("read_market_schedule_v3", epic))
        return self.schedule_by_epic.get(epic, _v3_schedule())

    def latest_response_diagnostic(self) -> dict[str, int | str | None] | None:
        return self.response_diagnostic

    def create_position(self, *_: object, **__: object) -> None:
        raise AssertionError("diagnostics must never attempt execution")


def _registry(
    config: ShadowTournamentConfig,
    *,
    unavailable_symbols: set[str] | None = None,
) -> ShadowMarketRegistry:
    unavailable = unavailable_symbols or set()
    markets: list[MarketSpec] = []
    for configured in config.universe:
        symbol = configured["symbol"]
        asset_class = AssetClass(configured["asset_class"])
        if symbol in unavailable:
            markets.append(
                MarketSpec(
                    symbol=symbol,
                    asset_class=asset_class,
                    epic=None,
                    state=MarketDataState.MARKET_DATA_UNAVAILABLE,
                    reason="TEST_UNAVAILABLE",
                )
            )
        else:
            markets.append(
                MarketSpec(
                    symbol=symbol,
                    asset_class=asset_class,
                    epic=f"TEST.{symbol}",
                    state=MarketDataState.AVAILABLE,
                )
            )
    return ShadowMarketRegistry(
        tuple(markets),
        "a" * 64,
        Path("test-dq03") / "instrument_registry.json",
    )


def _metadata(epic: str) -> dict[str, object]:
    return {
        "instrument": {
            "epic": epic,
            "marketStatus": "TRADEABLE",
            "streamingPricesAvailable": True,
        }
    }


def _live_quotes() -> dict[str, object]:
    return {
        f"TEST.{symbol}": build_ig_price_stream_quote(
            epic=f"TEST.{symbol}",
            symbol=symbol,
            bid_value="1.0000",
            ask_value="1.0002",
            timestamp_milliseconds=int(NOW.timestamp() * 1000),
            market_state="TRADEABLE",
            observed_at=NOW,
            maximum_age_seconds=60,
        )
        for symbol in ("EURUSD", "XAUUSD", "US500", "USTECH100")
    }


def _v3_schedule(
    opening_time: object = "00:00", closing_time: object = "23:59"
) -> dict[str, object]:
    return {
        "instrument": {
            "openingHours": {"marketTimes": [{"openTime": opening_time, "closeTime": closing_time}]}
        }
    }


def _clock_history(now: datetime) -> dict[str, object]:
    """Four completed rows plus an explicit current UTC-midnight DAY row."""

    history = _history(now - timedelta(days=1), 4, timedelta(days=3))
    current = _history(now, 1, now - datetime.combine(now.date(), datetime.min.time(), UTC))
    return {"prices": [*history["prices"], *current["prices"]]}


def _history(now: datetime, count: int, latest_offset: timedelta) -> dict[str, object]:
    prices: list[dict[str, object]] = []
    latest = now - latest_offset
    for index in range(count):
        close = 100.0 + index
        timestamp = latest - timedelta(days=count - index - 1)

        def quote(value: float) -> dict[str, float]:
            return {"bid": value - 0.01, "offer": value + 0.01}

        prices.append(
            {
                "snapshotTimeUTC": timestamp.isoformat(),
                "openPrice": quote(close),
                "highPrice": quote(close + 0.5),
                "lowPrice": quote(close - 0.5),
                "closePrice": quote(close),
            }
        )
    return {"prices": prices}


def test_clock_diagnostic_assesses_fx_metal_and_each_index_without_waiting() -> None:
    config = load_config()
    broker = RecordingReadOnlyBroker()

    report = verify_shadow_session_clock(
        config=config,
        registry=_registry(config),
        broker=broker,
        observed_at_utc=NOW,
        live_quotes=_live_quotes(),
    )

    assert report.execution_authority == "OFF"
    assert report.non_persisting is True
    assert report.requests_used == 8
    assert report.overall_state is ClockDiagnosticState.PASS
    assert [(item.asset_class, item.symbol, item.state) for item in report.assessments] == [
        ("FX", "EURUSD", ClockDiagnosticState.PASS),
        ("METAL", "XAUUSD", ClockDiagnosticState.PASS),
        ("US500", "US500", ClockDiagnosticState.PASS),
        ("USTECH100", "USTECH100", ClockDiagnosticState.PASS),
    ]
    assert all(item.streaming_price_available is True for item in report.assessments)
    assert all(call[0] in {"read_market", "read_historical_prices"} for call in broker.calls)
    assert not any(call[0] == "read_market_schedule_v3" for call in broker.calls)
    assert [call[-1] for call in broker.calls if call[0] == "read_historical_prices"] == [
        5,
        5,
        5,
        5,
    ]


def test_declared_hours_absence_is_advisory_and_does_not_block_a_valid_clock() -> None:
    config = load_config()
    broker = RecordingReadOnlyBroker()
    for symbol in ("EURUSD", "XAUUSD", "US500", "USTECH100"):
        broker.metadata_by_epic[f"TEST.{symbol}"] = {
            "instrument": {
                "epic": f"TEST.{symbol}",
                "marketStatus": "TRADEABLE",
                "streamingPricesAvailable": True,
                "openingHours": None,
            }
        }

    report = verify_shadow_session_clock(
        config=config,
        registry=_registry(config),
        broker=broker,
        observed_at_utc=NOW,
        live_quotes=_live_quotes(),
    )

    assert report.overall_state is ClockDiagnosticState.PASS
    assert all(item.state is ClockDiagnosticState.PASS for item in report.assessments)
    assert all(
        item.opening_hours_state == "DECLARED_HOURS_NOT_PROVIDED" for item in report.assessments
    )
    assert all(
        "SHADOW01_DECLARED_HOURS_NOT_PROVIDED" in item.reason_codes for item in report.assessments
    )
    assert not any(call[0] == "read_market_schedule_v3" for call in broker.calls)


def test_clock_diagnostic_uses_canonical_quote_not_metadata_streaming_flag() -> None:
    config = load_config()
    broker = RecordingReadOnlyBroker()
    for symbol in ("EURUSD", "XAUUSD", "US500", "USTECH100"):
        broker.metadata_by_epic[f"TEST.{symbol}"] = {
            "instrument": {
                "epic": f"TEST.{symbol}",
                "marketStatus": "TRADEABLE",
                "streamingPricesAvailable": False,
                "openingHours": None,
            }
        }

    report = verify_shadow_session_clock(
        config=config,
        registry=_registry(config),
        broker=broker,
        observed_at_utc=NOW,
        live_quotes=_live_quotes(),
    )

    assert report.overall_state is ClockDiagnosticState.PASS
    assert all(item.state is ClockDiagnosticState.PASS for item in report.assessments)
    assert all(item.streaming_price_available is True for item in report.assessments)


@pytest.mark.parametrize("symbol", ("EURUSD", "XAUUSD", "US500", "USTECH100"))
@pytest.mark.parametrize("quote_state", ("missing", "stale"))
def test_clock_diagnostic_fails_closed_per_representative_for_missing_or_stale_quote(
    symbol: str, quote_state: str
) -> None:
    config = load_config()
    quotes = _live_quotes()
    epic = f"TEST.{symbol}"
    if quote_state == "missing":
        del quotes[epic]
    else:
        quotes[epic] = build_ig_price_stream_quote(
            epic=epic,
            symbol=symbol,
            bid_value="1.0000",
            ask_value="1.0002",
            timestamp_milliseconds=int((NOW - timedelta(seconds=61)).timestamp() * 1000),
            market_state="TRADEABLE",
            observed_at=NOW,
            maximum_age_seconds=60,
        )

    report = verify_shadow_session_clock(
        config=config,
        registry=_registry(config),
        broker=RecordingReadOnlyBroker(),
        observed_at_utc=NOW,
        live_quotes=quotes,
    )

    assessments = {item.symbol: item for item in report.assessments}
    assert report.overall_state is ClockDiagnosticState.UNKNOWN
    assert assessments[symbol].state is ClockDiagnosticState.UNKNOWN
    assert (
        "SHADOW01_CLOCK_STREAMING_PRICE_AVAILABILITY_UNPROVEN" in assessments[symbol].reason_codes
    )
    for other_symbol, assessment in assessments.items():
        if other_symbol != symbol:
            assert assessment.state is ClockDiagnosticState.PASS
            assert "SHADOW01_CLOCK_STREAMING_PRICE_AVAILABILITY_UNPROVEN" not in (
                assessment.reason_codes
            )


def test_clock_diagnostic_does_not_borrow_a_quote_between_epics() -> None:
    config = load_config()
    quotes = _live_quotes()
    quotes["TEST.XAUUSD"] = quotes["TEST.EURUSD"]

    report = verify_shadow_session_clock(
        config=config,
        registry=_registry(config),
        broker=RecordingReadOnlyBroker(),
        observed_at_utc=NOW,
        live_quotes=quotes,
    )

    assessments = {item.symbol: item for item in report.assessments}
    assert assessments["XAUUSD"].state is ClockDiagnosticState.UNKNOWN
    assert (
        "SHADOW01_CLOCK_STREAMING_PRICE_AVAILABILITY_UNPROVEN" in assessments["XAUUSD"].reason_codes
    )
    assert all(
        assessments[symbol].state is ClockDiagnosticState.PASS
        for symbol in ("EURUSD", "US500", "USTECH100")
    )


def test_clock_diagnostic_does_not_treat_daily_history_as_a_live_quote() -> None:
    config = load_config()
    broker = RecordingReadOnlyBroker(latest_offset=timedelta(days=4))

    report = verify_shadow_session_clock(
        config=config,
        registry=_registry(config),
        broker=broker,
        observed_at_utc=NOW,
    )

    assert report.overall_state is ClockDiagnosticState.UNKNOWN
    assert all(item.state is ClockDiagnosticState.UNKNOWN for item in report.assessments)
    assert all(
        "SHADOW01_CLOCK_STREAMING_PRICE_AVAILABILITY_UNPROVEN" in item.reason_codes
        for item in report.assessments
    )


def test_unusable_declared_hours_remains_advisory_when_stronger_evidence_passes() -> None:
    config = load_config()
    broker = RecordingReadOnlyBroker()
    broker.metadata_by_epic["TEST.US500"] = {
        "instrument": {
            "epic": "TEST.US500",
            "marketStatus": "TRADEABLE",
            "openingHours": "unsupported-advisory-shape",
        }
    }

    report = verify_shadow_session_clock(
        config=config,
        registry=_registry(config),
        broker=broker,
        observed_at_utc=NOW,
        live_quotes=_live_quotes(),
    )

    us500 = report.assessments[2]
    assert us500.state is ClockDiagnosticState.PASS
    assert us500.opening_hours_state == "DECLARED_HOURS_ADVISORY_UNUSABLE"
    assert us500.proposed_clock_for_human_review is None


def test_v3_schedule_evidence_cannot_override_v4_identity_or_market_status() -> None:
    config = load_config()
    broker = RecordingReadOnlyBroker()
    broker.metadata_by_epic["TEST.EURUSD"] = {
        "instrument": {
            "epic": "TEST.WRONG",
            "marketStatus": "CLOSED",
            "streamingPricesAvailable": True,
        }
    }

    report = verify_shadow_session_clock(
        config=config,
        registry=_registry(config),
        broker=broker,
        observed_at_utc=NOW,
        live_quotes=_live_quotes(),
    )

    fx = report.assessments[0]
    assert fx.state is ClockDiagnosticState.UNKNOWN
    assert fx.opening_hours_state == "DECLARED_HOURS_NOT_PROVIDED"
    assert "SHADOW01_CLOCK_MARKET_IDENTITY_UNVERIFIED" in fx.reason_codes
    assert "SHADOW01_CLOCK_V4_MARKET_STATUS_UNAVAILABLE" in fx.reason_codes


def test_clock_diagnostic_fails_closed_for_unverified_representative_without_broker_read() -> None:
    config = load_config()
    broker = RecordingReadOnlyBroker()

    report = verify_shadow_session_clock(
        config=config,
        registry=_registry(config, unavailable_symbols={"USTECH100"}),
        broker=broker,
        observed_at_utc=NOW,
    )

    ustech = report.assessments[-1]
    assert ustech.state is ClockDiagnosticState.UNKNOWN
    assert ustech.reason_codes == ("SHADOW01_CLOCK_DQ03_MARKET_UNAVAILABLE",)
    assert not any(call[1] == "TEST.USTECH100" for call in broker.calls)


def test_clock_reports_an_instrument_specific_ustech100_history_failure_without_substitution() -> (
    None
):
    config = load_config()
    broker = RecordingReadOnlyBroker(
        history_errors_by_epic={
            "TEST.USTECH100": RuntimeError("upstream-body-must-not-leave-boundary")
        }
    )

    report = verify_shadow_session_clock(
        config=config,
        registry=_registry(config),
        broker=broker,
        observed_at_utc=NOW,
        live_quotes=_live_quotes(),
    )

    assert [item.state for item in report.assessments[:-1]] == [
        ClockDiagnosticState.PASS,
        ClockDiagnosticState.PASS,
        ClockDiagnosticState.PASS,
    ]
    ustech = report.assessments[-1]
    assert ustech.symbol == "USTECH100"
    assert ustech.state is ClockDiagnosticState.UNKNOWN
    assert "SHADOW01_CLOCK_HISTORY_UNAVAILABLE" in ustech.reason_codes
    assert "SHADOW01_CLOCK_LATEST_COMPLETED_HISTORY_UNAVAILABLE" in ustech.reason_codes
    assert [call for call in broker.calls if call[1] == "TEST.USTECH100"] == [
        ("read_market", "TEST.USTECH100"),
        ("read_historical_prices", "TEST.USTECH100", "DAY", 5),
    ]
    assert "upstream-body-must-not-leave-boundary" not in str(report)


def test_diagnostics_require_explicit_off_execution_authority() -> None:
    config = load_config()
    broker = RecordingReadOnlyBroker()
    broker.execution_authority = "ON"

    with pytest.raises(ShadowClockDiagnosticError, match="EXECUTION_AUTHORITY_INVALID"):
        verify_shadow_session_clock(
            config=config,
            registry=_registry(config),
            broker=broker,
            observed_at_utc=NOW,
        )
    with pytest.raises(ShadowWarmupDiagnosticError, match="EXECUTION_AUTHORITY_INVALID"):
        run_shadow_warmup_diagnostic(
            config=config,
            registry=_registry(config),
            broker=broker,
            observed_at_utc=NOW,
        )
    assert broker.calls == []


def test_diagnostics_reject_untrusted_config_or_registry_before_broker_calls() -> None:
    config = load_config()
    broker = RecordingReadOnlyBroker()
    trusted_registry = _registry(config)
    untrusted_config = ShadowTournamentConfig(config.payload, "0" * 64)
    untrusted_registry = ShadowMarketRegistry(
        trusted_registry.markets,
        "not-a-registry-fingerprint",
        None,
    )

    with pytest.raises(ShadowClockDiagnosticError, match="CONFIG_UNVERIFIED"):
        verify_shadow_session_clock(
            config=untrusted_config,
            registry=trusted_registry,
            broker=broker,
            observed_at_utc=NOW,
        )
    with pytest.raises(ShadowWarmupDiagnosticError, match="DQ03_REGISTRY_UNVERIFIED"):
        run_shadow_warmup_diagnostic(
            config=config,
            registry=untrusted_registry,
            broker=broker,
            observed_at_utc=NOW,
        )
    assert broker.calls == []


def test_warmup_diagnostic_is_bounded_and_reports_t1_m1_q1_completed_history() -> None:
    config = load_config()
    broker = RecordingReadOnlyBroker(history_count=300)

    report = run_shadow_warmup_diagnostic(
        config=config,
        registry=_registry(config),
        broker=broker,
        observed_at_utc=NOW,
    )

    assert report.execution_authority == "OFF"
    assert report.non_persisting is True
    assert report.request_budget == 3
    assert report.requests_used == 3
    assert report.history_points_per_request == 300
    assert report.t1_minimum_completed_sessions == 61
    assert report.m1_full_calibration_minimum_sessions == 273
    assert report.q1_minimum_completed_sessions == 61
    assert report.overall_data_quality is WarmupDataQualityState.READY
    assert [item.symbol for item in report.markets] == ["EURUSD", "XAUUSD", "US500"]
    assert all(item.bars_requested == 300 for item in report.markets)
    assert all(item.bars_received == 300 for item in report.markets)
    assert all(item.completed_sessions == 300 for item in report.markets)
    assert all(
        item.t1_history_ready and item.m1_history_ready and item.q1_history_ready
        for item in report.markets
    )
    assert [call[0] for call in broker.calls] == ["read_historical_prices"] * 3


def test_warmup_keeps_299_completed_sessions_when_day_300_is_the_current_candle() -> None:
    class CurrentDayHistoryBroker(RecordingReadOnlyBroker):
        def read_historical_prices(self, epic: str, resolution: str, points: int) -> object:
            self.calls.append(("read_historical_prices", epic, resolution, points))
            current_day = NOW.replace(hour=0, minute=0, second=0, microsecond=0)
            rows: list[dict[str, object]] = []
            for index in range(300):
                timestamp = current_day - timedelta(days=299 - index)
                quote = {"bid": 1.0, "offer": 1.1}
                rows.append(
                    {
                        "snapshotTimeUTC": timestamp.strftime("%Y-%m-%dT%H:%M:%S"),
                        "openPrice": quote,
                        "highPrice": quote,
                        "lowPrice": quote,
                        "closePrice": quote,
                    }
                )
            return {"prices": rows}

    config = load_config()
    broker = CurrentDayHistoryBroker()

    report = run_shadow_warmup_diagnostic(
        config=config,
        registry=_registry(config),
        broker=broker,
        observed_at_utc=NOW,
    )

    assert all(item.bars_received == 300 for item in report.markets)
    assert all(item.completed_sessions == 299 for item in report.markets)
    assert all(
        item.t1_history_ready and item.m1_history_ready and item.q1_history_ready
        for item in report.markets
    )


def test_live_smoke_v2_warmup_has_a_separate_fixed_four_market_budget() -> None:
    config = load_config()
    broker = RecordingReadOnlyBroker(history_count=300)

    report = run_shadow_live_readonly_smoke_warmup_v2(
        config=config,
        registry=_registry(config),
        broker=broker,
        observed_at_utc=NOW,
    )

    assert report.execution_authority == "OFF"
    assert report.non_persisting is True
    assert report.request_budget == 4
    assert report.requests_used == 4
    assert [item.symbol for item in report.markets] == ["EURUSD", "USDJPY", "XAUUSD", "US500"]
    assert [(call[1], call[2], call[3]) for call in broker.calls] == [
        ("TEST.EURUSD", "DAY", 300),
        ("TEST.USDJPY", "DAY", 300),
        ("TEST.XAUUSD", "DAY", 300),
        ("TEST.US500", "DAY", 300),
    ]
    with pytest.raises(TypeError):
        run_shadow_live_readonly_smoke_warmup_v2(
            config=config,
            registry=_registry(config),
            broker=broker,
            observed_at_utc=NOW,
            representative_symbols=("EURUSD",),
        )


def test_warmup_preserves_only_sanitized_403_evidence_for_day_300_reads() -> None:
    config = load_config()
    broker = RecordingReadOnlyBroker(
        history_error=RuntimeError("upstream-body-must-not-leave-boundary"),
        response_diagnostic={
            "status_code": 403,
            "upstream_error_code": "error.public-api.access-denied",
        },
    )

    report = run_shadow_live_readonly_smoke_warmup_v2(
        config=config,
        registry=_registry(config),
        broker=broker,
        observed_at_utc=NOW,
    )

    assert report.requests_used == 4
    assert report.overall_data_quality is WarmupDataQualityState.UNKNOWN
    assert all(item.http_status_code == 403 for item in report.markets)
    assert all(
        item.upstream_error_code == "error.public-api.access-denied" for item in report.markets
    )
    assert all(
        item.reason_codes == ("SHADOW01_WARMUP_HISTORY_UNAVAILABLE",) for item in report.markets
    )
    assert [(call[2], call[3]) for call in broker.calls] == [("DAY", 300)] * 4
    assert "upstream-body-must-not-leave-boundary" not in str(report.document())


def test_warmup_reports_partial_m1_without_creating_decisions_or_writing_database(
    tmp_path: Path,
) -> None:
    config = load_config()
    broker = RecordingReadOnlyBroker(history_count=61)
    database = tmp_path / "safety.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.executescript(
            """
            CREATE TABLE tournament_epoch (id INTEGER PRIMARY KEY);
            CREATE TABLE shadow_decisions (id INTEGER PRIMARY KEY);
            CREATE TABLE outcome_labels (id INTEGER PRIMARY KEY);
            """
        )

    clock_report = verify_shadow_session_clock(
        config=config,
        registry=_registry(config),
        broker=broker,
        observed_at_utc=NOW,
    )
    warmup_report = run_shadow_warmup_diagnostic(
        config=config,
        registry=_registry(config),
        broker=broker,
        observed_at_utc=NOW,
    )

    assert clock_report.non_persisting is True
    assert warmup_report.overall_data_quality is WarmupDataQualityState.WARNING
    assert all(item.t1_history_ready and item.q1_history_ready for item in warmup_report.markets)
    assert all(not item.m1_history_ready for item in warmup_report.markets)
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT count(*) FROM tournament_epoch").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM shadow_decisions").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM outcome_labels").fetchone()[0] == 0


def test_diagnostic_modules_have_no_wait_or_persistence_import_surface() -> None:
    for module in (clock_module, warmup_module):
        source = Path(inspect.getsourcefile(module) or "").read_text(encoding="utf-8")
        tree = ast.parse(source)
        imported_modules = {
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module is not None
        }
        imported_modules.update(
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        )
        calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)]
        assert not any(
            name.startswith(
                (
                    "src.ig_trader.shadow01.runtime",
                    "src.ig_trader.shadow01.storage",
                    "src.ig_trader.shadow01.outcomes",
                    "src.ig_trader.shadow01.policies",
                )
            )
            for name in imported_modules
        )
        assert not any(
            isinstance(call.func, ast.Attribute) and call.func.attr == "sleep" for call in calls
        )
