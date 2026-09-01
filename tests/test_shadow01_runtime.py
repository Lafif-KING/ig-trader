from __future__ import annotations

import ast
import inspect
import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import src.ig_trader.shadow01.runtime as runtime_module
from src.ig_trader.shadow01.__main__ import main
from src.ig_trader.shadow01.config import ShadowTournamentConfig, load_config
from src.ig_trader.shadow01.live_quote import build_ig_price_stream_quote
from src.ig_trader.shadow01.read_only_broker import Shadow01ReadOnlyBroker
from src.ig_trader.shadow01.registry import load_verified_dq03_registry
from src.ig_trader.shadow01.runtime import Shadow01Runtime
from src.ig_trader.shadow01.storage import ShadowTournamentStore
from tests.shadow01_dq03_fixtures import verified_dq03_documents

NOW = datetime(2026, 9, 2, 21, 10, tzinfo=UTC)


class RecordingTransport:
    def __init__(
        self, history: dict[str, object], *, failing_epics: set[str] | None = None
    ) -> None:
        self.history = history
        self.failing_epics = failing_epics or set()
        self.calls: list[tuple[str, str]] = []

    def authorized_request(self, method: str, endpoint: str, **_: object) -> object:
        self.calls.append((method, endpoint))
        epic = endpoint.split("/")[2] if endpoint.count("/") >= 2 else ""
        if epic in self.failing_epics:
            raise RuntimeError("provider unavailable")
        if endpoint.startswith("/markets/"):
            return {
                "snapshot": {"bid": 159.99, "offer": 160.01},
                "instrument": {"instrumentType": "CASH", "expiry": "DFB"},
                "dealingRules": {"minStopOrLimitDistance": {"value": 1.0}},
            }
        if endpoint.startswith("/prices/"):
            return self.history
        return {}


def _history(anchor: datetime = NOW) -> dict[str, object]:
    prices: list[dict[str, object]] = []
    for index in range(61):
        close = 100.0 + index
        timestamp = anchor - timedelta(minutes=10, days=60 - index)

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


def _write_registry_documents(
    directory: Path,
    config: ShadowTournamentConfig,
    symbols: set[str] | None = None,
) -> Path:
    documents = verified_dq03_documents(config)
    if symbols is not None:
        registry = documents["instrument_registry.json"]
        history = documents["history_validation.json"]
        manifest = documents["discovery_manifest.json"]
        registry["instruments"] = [
            item
            for item in registry["instruments"]
            if isinstance(item, dict) and item.get("canonical_symbol") in symbols
        ]
        history["samples"] = [
            item
            for item in history["samples"]
            if isinstance(item, dict) and item.get("symbol") in symbols
        ]
        manifest["instrument_count"] = len(registry["instruments"])
        manifest["classification_counts"] = {"VERIFIED": len(registry["instruments"])}
    for name, document in documents.items():
        (directory / name).write_text(json.dumps(document), encoding="utf-8")
    return directory / "instrument_registry.json"


def _runtime(
    tmp_path: Path,
    *,
    symbols: set[str] | None = None,
    failing_epics: set[str] | None = None,
) -> tuple[Shadow01Runtime, RecordingTransport, ShadowTournamentStore]:
    config = load_config()
    registry_path = _write_registry_documents(tmp_path, config, symbols)
    transport = RecordingTransport(_history(), failing_epics=failing_epics)
    store = ShadowTournamentStore(tmp_path / "shadow.sqlite3")

    def canonical_quote(market, timestamp):
        assert market.epic is not None
        return build_ig_price_stream_quote(
            epic=market.epic,
            symbol=market.symbol,
            bid_value="1.0000",
            ask_value="1.0002",
            timestamp_milliseconds=int(timestamp.timestamp() * 1000),
            market_state="TRADEABLE",
            observed_at=timestamp,
            maximum_age_seconds=60,
        )

    runtime = Shadow01Runtime(
        config=config,
        store=store,
        registry=load_verified_dq03_registry(config, registry_path),
        broker=Shadow01ReadOnlyBroker(transport),
        canonical_quote_provider=canonical_quote,
        history_cache_directory=tmp_path / "history-cache",
        stop_marker_path=tmp_path / "shadow01.stop",
    )
    return runtime, transport, store


def test_pre_epoch_probe_records_read_only_clock_health_and_no_decisions(tmp_path: Path) -> None:
    runtime, transport, store = _runtime(tmp_path)

    result = runtime.pre_epoch_provider_probe(observed_at=NOW)

    assert result.status == "SHADOW01_PRE_EPOCH_READINESS_RECORDED"
    assert {endpoint.split("/")[2] for _, endpoint in transport.calls} >= {
        "TEST.EURUSD",
        "TEST.XAUUSD",
        "TEST.US500",
    }
    with sqlite3.connect(store.path) as connection:
        assert connection.execute("SELECT count(*) FROM shadow_decisions").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM market_snapshots").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM engine_insights").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM epoch_readiness").fetchone()[0] == 1
        readiness_snapshot = json.loads(
            connection.execute("SELECT snapshot_json FROM epoch_readiness").fetchone()[0]
        )
        assert readiness_snapshot["metadata"]["readiness_summary"]["causal_snapshot_count"] == 20
        assert readiness_snapshot["metadata"]["readiness_summary"]["causal_snapshot_fingerprint"]
        marker = connection.execute(
            """
            SELECT provider, status, detail, data_json
            FROM provider_health
            WHERE provider = 'IG_READ_ONLY'
              AND status = 'HEALTHY'
              AND detail = 'SHADOW01_READ_ONLY_CLOCK_PROBE_OK'
            ORDER BY id DESC LIMIT 1
            """
        ).fetchone()
        assert marker is not None
        assert json.loads(marker[3])["asset_classes"] == ["FX", "METAL", "INDEX"]


def test_pre_epoch_probe_requires_a_proven_common_clock_for_each_asset_class(
    tmp_path: Path,
) -> None:
    runtime, _, store = _runtime(tmp_path, symbols={"EURUSD"})

    result = runtime.pre_epoch_provider_probe(observed_at=NOW)

    assert result.status == "SHADOW01_SESSION_CLOCK_HUMAN_GATE_REQUIRED"
    with sqlite3.connect(store.path) as connection:
        assert connection.execute("SELECT count(*) FROM shadow_decisions").fetchone()[0] == 0


def test_pre_epoch_probe_human_gates_when_a_verified_asset_class_read_fails(tmp_path: Path) -> None:
    runtime, _, store = _runtime(tmp_path, failing_epics={"TEST.XAUUSD"})

    result = runtime.pre_epoch_provider_probe(observed_at=NOW)

    assert result.status == "SHADOW01_SESSION_CLOCK_HUMAN_GATE_REQUIRED"
    assert any(item.instrument == "XAUUSD" for item in result.market_results)
    with sqlite3.connect(store.path) as connection:
        assert connection.execute("SELECT count(*) FROM shadow_decisions").fetchone()[0] == 0


def test_cycle_requires_epoch_and_anchor_then_records_shared_timestamp_decisions(
    tmp_path: Path,
) -> None:
    runtime, transport, store = _runtime(tmp_path)

    no_epoch = runtime.run_observation_cycle(observed_at=NOW)
    wrong_anchor = runtime.run_observation_cycle(observed_at=NOW + timedelta(minutes=1))

    assert no_epoch.status == "SHADOW01_EPOCH_NOT_CREATED"
    assert wrong_anchor.status == "SHADOW01_DECISION_TIMESTAMP_IS_NOT_THE_FROZEN_ANCHOR"
    assert transport.calls == []

    readiness = runtime.pre_epoch_provider_probe(observed_at=NOW)
    assert readiness.status == "SHADOW01_PRE_EPOCH_READINESS_RECORDED"
    store.create_epoch(
        runtime.config,
        epoch_utc=NOW,
        authorization_phrase="START SHADOW01-V1 EPOCH",
    )
    recorded = runtime.run_observation_cycle(observed_at=NOW)

    assert recorded.status == "SHADOW01_OBSERVATION_RECORDED"
    assert recorded.decisions_recorded == 80
    assert len(recorded.market_results) == 20
    assert all(
        item.status == "SHADOW01_MARKET_OBSERVATION_RECORDED" for item in recorded.market_results
    )
    assert transport.calls.count(("GET", "/prices/TEST.EURUSD/DAY/300")) == 1
    with sqlite3.connect(store.path) as connection:
        rows = connection.execute(
            "SELECT decision_timestamp_utc FROM shadow_decisions ORDER BY policy_id"
        ).fetchall()
        assert len(rows) == 80
        assert {row[0] for row in rows} == {NOW.isoformat()}
        assert connection.execute("SELECT count(*) FROM engine_insights").fetchone()[0] == 140

    calls_before_repeat = tuple(transport.calls)
    repeated = runtime.run_observation_cycle(observed_at=NOW)

    assert repeated.status == "SHADOW01_OBSERVATION_ALREADY_RECORDED"
    assert repeated.decisions_recorded == 0
    assert all(
        item.status == "SHADOW01_MARKET_OBSERVATION_ALREADY_RECORDED"
        for item in repeated.market_results
    )
    assert tuple(transport.calls) == calls_before_repeat


def test_missing_or_stale_canonical_quote_records_no_decision_and_no_history_read(
    tmp_path: Path,
) -> None:
    runtime, transport, store = _runtime(tmp_path)
    assert (
        runtime.pre_epoch_provider_probe(observed_at=NOW).status
        == "SHADOW01_PRE_EPOCH_READINESS_RECORDED"
    )
    store.create_epoch(
        runtime.config,
        epoch_utc=NOW,
        authorization_phrase="START SHADOW01-V1 EPOCH",
    )
    calls_before = tuple(transport.calls)
    runtime.canonical_quote_provider = lambda _market, _timestamp: None

    missing = runtime.run_observation_cycle(observed_at=NOW)

    assert missing.status == "SHADOW01_OBSERVATION_NO_MARKETS_RECORDED"
    assert all(item.status == "SHADOW01_MARKET_NO_DECISION" for item in missing.market_results)
    assert all(
        item.reason_codes == ("SHADOW01_CANONICAL_STREAM_QUOTE_UNAVAILABLE",)
        for item in missing.market_results
    )
    assert tuple(transport.calls) == calls_before
    with sqlite3.connect(store.path) as connection:
        assert connection.execute("SELECT count(*) FROM shadow_decisions").fetchone()[0] == 0

    def stale_quote(market, timestamp):
        assert market.epic is not None
        return build_ig_price_stream_quote(
            epic=market.epic,
            symbol=market.symbol,
            bid_value="1.0000",
            ask_value="1.0002",
            timestamp_milliseconds=int((timestamp - timedelta(seconds=61)).timestamp() * 1000),
            market_state="TRADEABLE",
            observed_at=timestamp,
            maximum_age_seconds=60,
        )

    runtime.canonical_quote_provider = stale_quote
    stale = runtime.run_observation_cycle(observed_at=NOW)

    assert stale.status == "SHADOW01_OBSERVATION_NO_MARKETS_RECORDED"
    assert all(item.status == "SHADOW01_MARKET_NO_DECISION" for item in stale.market_results)
    assert tuple(transport.calls) == calls_before


def test_cycle_resolves_only_due_outcomes_from_later_completed_sessions(tmp_path: Path) -> None:
    runtime, transport, store = _runtime(tmp_path)
    assert (
        runtime.pre_epoch_provider_probe(observed_at=NOW).status
        == "SHADOW01_PRE_EPOCH_READINESS_RECORDED"
    )
    store.create_epoch(
        runtime.config,
        epoch_utc=NOW,
        authorization_phrase="START SHADOW01-V1 EPOCH",
    )

    first = runtime.run_observation_cycle(observed_at=NOW)
    assert first.outcomes_resolved == 0

    one_session_later = NOW + timedelta(days=1)
    transport.history = _history(one_session_later)
    second = runtime.run_observation_cycle(observed_at=one_session_later)
    assert second.outcomes_resolved > 0
    with sqlite3.connect(store.path) as connection:
        first_horizon = connection.execute(
            """
            SELECT o.horizon_sessions, o.quality
            FROM outcome_labels o
            JOIN shadow_decisions d ON d.decision_id = o.decision_id
            WHERE d.decision_timestamp_utc = ?
              AND d.instrument = 'EURUSD'
              AND d.policy_id = 'P0_TECHNICAL_TREND_ONLY'
            ORDER BY o.horizon_sessions
            """,
            (NOW.isoformat(),),
        ).fetchall()
    assert first_horizon == [(1, "NORMAL")]

    twenty_sessions_later = NOW + timedelta(days=20)
    transport.history = _history(twenty_sessions_later)
    third = runtime.run_observation_cycle(observed_at=twenty_sessions_later)
    assert third.outcomes_resolved > 0
    with sqlite3.connect(store.path) as connection:
        horizons = connection.execute(
            """
            SELECT o.horizon_sessions, o.quality, o.cost_adjusted_result
            FROM outcome_labels o
            JOIN shadow_decisions d ON d.decision_id = o.decision_id
            WHERE d.decision_timestamp_utc = ?
              AND d.instrument = 'EURUSD'
              AND d.policy_id = 'P0_TECHNICAL_TREND_ONLY'
            ORDER BY o.horizon_sessions
            """,
            (NOW.isoformat(),),
        ).fetchall()
    assert horizons == [
        (1, "NORMAL", None),
        (3, "NORMAL", None),
        (5, "NORMAL", None),
        (10, "NORMAL", None),
        (20, "NORMAL", None),
    ]


def test_monitor_refuses_missing_provider_and_stop_marker_is_shadow_only(tmp_path: Path) -> None:
    runtime, _, store = _runtime(tmp_path)
    assert (
        runtime.pre_epoch_provider_probe(observed_at=NOW).status
        == "SHADOW01_PRE_EPOCH_READINESS_RECORDED"
    )
    store.create_epoch(
        runtime.config,
        epoch_utc=NOW,
        authorization_phrase="START SHADOW01-V1 EPOCH",
    )
    runtime.broker = None

    missing_provider = next(runtime.monitor(now=lambda: NOW, sleep=lambda _: None))
    assert missing_provider.status == "SHADOW01_PROVIDER_REQUIRED"

    stopped = runtime.request_stop(requested_at=NOW)
    monitor = next(runtime.monitor(now=lambda: NOW, sleep=lambda _: None))
    assert stopped.status == "SHADOW01_MONITOR_STOP_REQUESTED"
    assert monitor.status == "SHADOW01_MONITOR_STOP_REQUESTED"
    assert runtime.stop_marker_path.name == "shadow01.stop"
    assert (
        json.loads(runtime.stop_marker_path.read_text(encoding="utf-8"))["scope"]
        == "SHADOW01_MONITOR_ONLY"
    )


def test_monitor_waits_for_the_next_anchor_without_off_clock_provider_reads(tmp_path: Path) -> None:
    runtime, transport, store = _runtime(tmp_path)
    assert (
        runtime.pre_epoch_provider_probe(observed_at=NOW).status
        == "SHADOW01_PRE_EPOCH_READINESS_RECORDED"
    )
    store.create_epoch(
        runtime.config,
        epoch_utc=NOW,
        authorization_phrase="START SHADOW01-V1 EPOCH",
    )
    calls_before_monitor = len(transport.calls)
    moments = iter((NOW - timedelta(milliseconds=100), NOW + timedelta(milliseconds=100)))
    sleeps: list[float] = []
    monitor = runtime.monitor(
        poll_interval_seconds=60.0,
        now=lambda: next(moments),
        sleep=sleeps.append,
    )

    result = next(monitor)

    assert result.status == "SHADOW01_OBSERVATION_RECORDED"
    assert result.observed_at_utc == NOW
    assert sleeps == [0.1]
    post_monitor_calls = transport.calls[calls_before_monitor:]
    assert len(post_monitor_calls) == 20
    assert all(endpoint.startswith("/markets/") for _, endpoint in post_monitor_calls)
    runtime.request_stop(requested_at=NOW)
    assert next(monitor).status == "SHADOW01_MONITOR_STOP_REQUESTED"


def test_monitor_reports_a_late_unrecorded_anchor_without_provider_reads(tmp_path: Path) -> None:
    runtime, transport, store = _runtime(tmp_path)
    assert (
        runtime.pre_epoch_provider_probe(observed_at=NOW).status
        == "SHADOW01_PRE_EPOCH_READINESS_RECORDED"
    )
    store.create_epoch(
        runtime.config,
        epoch_utc=NOW,
        authorization_phrase="START SHADOW01-V1 EPOCH",
    )
    calls_before_monitor = tuple(transport.calls)

    monitor = runtime.monitor(
        now=lambda: NOW + timedelta(seconds=2),
        sleep=lambda _: None,
    )
    result = next(monitor)

    assert result.status == "SHADOW01_MONITOR_ANCHOR_MISSED"
    assert result.observed_at_utc == NOW
    assert result.detail == "SHADOW01_MONITOR_WAKE_LATE"
    assert tuple(transport.calls) == calls_before_monitor


def test_monitor_skips_a_scheduled_anchor_when_its_wake_is_late(tmp_path: Path) -> None:
    runtime, transport, store = _runtime(tmp_path)
    assert (
        runtime.pre_epoch_provider_probe(observed_at=NOW).status
        == "SHADOW01_PRE_EPOCH_READINESS_RECORDED"
    )
    store.create_epoch(
        runtime.config,
        epoch_utc=NOW,
        authorization_phrase="START SHADOW01-V1 EPOCH",
    )
    calls_before_monitor = tuple(transport.calls)
    moments = iter((NOW - timedelta(milliseconds=100), NOW + timedelta(seconds=2)))
    sleeps: list[float] = []

    monitor = runtime.monitor(
        now=lambda: next(moments),
        sleep=sleeps.append,
    )
    result = next(monitor)

    assert result.status == "SHADOW01_MONITOR_ANCHOR_MISSED"
    assert result.observed_at_utc == NOW
    assert sleeps == [0.1]
    assert tuple(transport.calls) == calls_before_monitor


def test_late_restart_reports_complete_anchor_without_provider_reads(tmp_path: Path) -> None:
    runtime, transport, store = _runtime(tmp_path)
    assert (
        runtime.pre_epoch_provider_probe(observed_at=NOW).status
        == "SHADOW01_PRE_EPOCH_READINESS_RECORDED"
    )
    store.create_epoch(
        runtime.config,
        epoch_utc=NOW,
        authorization_phrase="START SHADOW01-V1 EPOCH",
    )
    assert runtime.run_observation_cycle(observed_at=NOW).status == "SHADOW01_OBSERVATION_RECORDED"
    calls_before_restart = tuple(transport.calls)
    runtime.store = ShadowTournamentStore(store.path)

    monitor = runtime.monitor(
        now=lambda: NOW + timedelta(seconds=2),
        sleep=lambda _: None,
    )
    result = next(monitor)

    assert result.status == "SHADOW01_OBSERVATION_ALREADY_RECORDED"
    assert result.observed_at_utc == NOW
    assert tuple(transport.calls) == calls_before_restart


def test_cli_requires_an_explicit_local_flag_before_any_read_only_adapter_build(
    tmp_path: Path, capsys
) -> None:
    config = load_config()
    registry_path = _write_registry_documents(tmp_path, config, {"EURUSD"})
    database = tmp_path / "shadow.sqlite3"
    marker = tmp_path / "shadow-stop.marker"

    assert main(["status", "--database", str(database), "--registry", str(registry_path)]) == 0
    assert main(["probe", "--database", str(database), "--registry", str(registry_path)]) == 2
    assert main(["monitor", "--database", str(database), "--registry", str(registry_path)]) == 2
    assert (
        main(
            [
                "monitor",
                "--database",
                str(database),
                "--registry",
                str(registry_path),
                "--use-local-demo-read-only",
            ]
        )
        == 2
    )
    assert main(["stop", "--database", str(database), "--stop-marker", str(marker)]) == 0
    assert main(["monitor", "--database", str(database), "--stop-marker", str(marker)]) == 0
    output = capsys.readouterr().out

    assert "execution_authority" in output
    assert "SHADOW01_LOCAL_DEMO_READ_ONLY_FLAG_REQUIRED" in output
    assert "SHADOW01_DQ03_20_PROVEN_MARKETS_REQUIRED" in output
    assert "SHADOW01_MONITOR_STOP_REQUESTED" in output


def test_status_exposes_gate01_zeroes_before_a_broker_is_constructed(tmp_path: Path) -> None:
    config = load_config()
    registry_path = _write_registry_documents(tmp_path, config, {"EURUSD"})
    runtime = Shadow01Runtime(
        config=config,
        store=ShadowTournamentStore(tmp_path / "shadow.sqlite3"),
        registry=load_verified_dq03_registry(config, registry_path),
        broker=None,
    )

    status = runtime.status()

    assert status["execution_authority"] == "OFF"
    assert status["broker_constructed"] is False
    assert status["execution_safety_counters"] == {
        "create": 0,
        "close": 0,
        "working_orders": 0,
        "demo_starts": 0,
    }
    assert (
        status["broker_request_counters"]["execution_safety_counters"]
        == status["execution_safety_counters"]
    )


def test_runtime_has_no_demo_or_execution_import_surface() -> None:
    source = Path(inspect.getsourcefile(runtime_module) or "").read_text(encoding="utf-8")
    tree = ast.parse(source)
    imports = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module is not None
    }
    imports.update(
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    )

    assert not any(
        name.startswith(
            (
                "src.ig_trader.demo",
                "src.ig_trader.execution",
                "src.ig_trader.shadow_execution",
            )
        )
        for name in imports
    )
    assert Shadow01Runtime.execution_authority == "OFF"
