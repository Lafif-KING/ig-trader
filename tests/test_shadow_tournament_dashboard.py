"""Focused safety tests for the isolated Shadow Tournament dashboard boundary."""

from __future__ import annotations

import ast
import sys
from pathlib import Path

from dashboard import shadow_tournament_control as controller
from dashboard.shadow_tournament_control import (
    controls_for,
    invoke_shadow_tournament_controller,
)
from dashboard.sources.shadow_tournament import (
    ShadowTournamentDashboard,
    load_shadow_tournament_dashboard,
)
from src.ig_trader.shadow01.config import load_config
from tests.shadow01_dq03_fixtures import write_verified_dq03_documents

ROOT = Path(__file__).resolve().parents[1]


def test_dashboard_boundary_does_not_import_demo_or_trading_components() -> None:
    paths = (
        ROOT / "dashboard" / "sources" / "shadow_tournament.py",
        ROOT / "dashboard" / "pages" / "shadow_tournament.py",
        ROOT / "dashboard" / "shadow_tournament_control.py",
    )
    imports: set[str] = set()
    for path in paths:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        imports.update(
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module is not None
        )
        imports.update(
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        )

    forbidden = (
        "dashboard.operator_control",
        "dashboard.sources.demo_operator",
        "src.ig_trader.main",
        "src.ig_trader.execution",
        "src.ig_trader.session",
    )
    assert not any(module.startswith(forbidden) for module in imports)


def test_market_snapshot_operator_labels_separate_metadata_from_live_price_feed() -> None:
    source = (ROOT / "dashboard" / "pages" / "shadow_tournament.py").read_text(encoding="utf-8")

    assert '"Metadata health"' in source
    assert '"Live price feed"' in source
    assert '"Last quote age (seconds)"' in source
    assert '"Live price feed status"' in source
    assert "BIDPRICE1" not in source
    assert "ASKPRICE1" not in source


def _dashboard(*, available: bool = True, epoch_created: bool = False) -> ShadowTournamentDashboard:
    return ShadowTournamentDashboard(
        available=available,
        reason="SHADOW01_STORAGE_NOT_CREATED" if not available else "SHADOW01_DASHBOARD_READY",
        tournament_version="SHADOW01-V1",
        config_fingerprint="f" * 64,
        execution_authority="OFF",
        epoch_utc="2026-08-29T00:00:00+00:00" if epoch_created else None,
        epoch_created=epoch_created,
        market_matrix=(),
        provider_health=(),
        epoch_readiness=(),
        market_snapshots=(),
        engine_insights=(),
        latest_decisions=(),
        resolved_outcomes=(),
        leaderboard=(),
        factor_audit=(),
    )


def test_missing_shadow_store_is_explicit_and_never_created(tmp_path: Path) -> None:
    database_path = tmp_path / "runtime" / "shadow_tournament.sqlite3"

    dashboard = load_shadow_tournament_dashboard(
        config_path=ROOT / "shadow01_strategy_config.json",
        database_path=database_path,
        registry_path=tmp_path / "missing-dq03-registry.json",
    )

    assert not dashboard.available
    assert dashboard.reason == "SHADOW01_STORAGE_NOT_CREATED"
    assert len(dashboard.market_matrix) == 20
    assert {item["epic"] for item in dashboard.market_matrix} == {None}
    assert {item["state"] for item in dashboard.market_matrix} == {"MARKET_DATA_UNAVAILABLE"}
    assert not database_path.exists()


def test_missing_config_fails_closed_without_creating_a_store(tmp_path: Path) -> None:
    database_path = tmp_path / "shadow_tournament.sqlite3"

    dashboard = load_shadow_tournament_dashboard(
        config_path=tmp_path / "missing-shadow01-config.json",
        database_path=database_path,
    )

    assert not dashboard.available
    assert dashboard.reason == "SHADOW01_CONFIG_UNAVAILABLE"
    assert not database_path.exists()


def test_market_matrix_uses_only_epics_proven_by_the_dq03_registry(tmp_path: Path) -> None:
    config = load_config(ROOT / "shadow01_strategy_config.json")
    registry_path = tmp_path / "instrument_registry.json"
    write_verified_dq03_documents(tmp_path, config)
    database_path = tmp_path / "shadow_tournament.sqlite3"

    dashboard = load_shadow_tournament_dashboard(
        config_path=ROOT / "shadow01_strategy_config.json",
        database_path=database_path,
        registry_path=registry_path,
    )

    assert len(dashboard.market_matrix) == 20
    assert dashboard.market_matrix[0]["epic"] == "TEST.EURUSD"
    assert {item["state"] for item in dashboard.market_matrix} == {"AVAILABLE"}
    assert not database_path.exists()


def test_start_is_disabled_without_a_created_epoch(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("SHADOW_TOURNAMENT_LOCAL", "true")
    monkeypatch.delenv("DASHBOARD_HOSTED", raising=False)

    controls = controls_for(
        _dashboard(epoch_created=False),
        pid_record_path=tmp_path / "monitor.json",
    )

    assert not controls.start_enabled
    assert not controls.stop_enabled
    assert controls.reason == "SHADOW01_EPOCH_NOT_CREATED"


def test_start_gate_never_launches_a_process_without_epoch(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("SHADOW_TOURNAMENT_LOCAL", "true")
    monkeypatch.delenv("DASHBOARD_HOSTED", raising=False)

    def unexpected_popen(*args: object, **kwargs: object) -> None:
        raise AssertionError("a missing epoch must block before Popen")

    monkeypatch.setattr(controller.subprocess, "Popen", unexpected_popen)

    result = invoke_shadow_tournament_controller(
        "start",
        _dashboard(epoch_created=False),
        pid_record_path=tmp_path / "monitor.json",
    )

    assert result == "SHADOW01_EPOCH_NOT_CREATED"


def test_start_uses_only_the_fixed_shadow_monitor_command(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("SHADOW_TOURNAMENT_LOCAL", "true")
    monkeypatch.delenv("DASHBOARD_HOSTED", raising=False)
    observed: dict[str, object] = {}

    class FakeProcess:
        pid = 24680

        def terminate(self) -> None:
            raise AssertionError("the monitor record write should succeed")

        def wait(self, timeout: float) -> None:
            raise AssertionError("the monitor record write should succeed")

    def fake_popen(command: list[str], **kwargs: object) -> FakeProcess:
        observed["command"] = command
        observed["kwargs"] = kwargs
        return FakeProcess()

    monkeypatch.setattr(controller.subprocess, "Popen", fake_popen)
    pid_record_path = tmp_path / "monitor.json"

    result = invoke_shadow_tournament_controller(
        "start",
        _dashboard(epoch_created=True),
        pid_record_path=pid_record_path,
    )

    assert result == "SHADOW01_MONITOR_STARTED"
    assert observed["command"] == [
        sys.executable,
        "-m",
        "src.ig_trader.shadow01",
        "monitor",
        "--database",
        str(controller.DEFAULT_DATABASE_PATH),
        "--use-local-demo-read-only",
    ]
    assert observed["kwargs"] == {
        "cwd": ROOT,
        "stdin": controller.subprocess.DEVNULL,
        "stdout": controller.subprocess.DEVNULL,
        "stderr": controller.subprocess.DEVNULL,
        "shell": False,
    }


def test_stop_uses_only_the_fixed_shadow_stop_command(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("SHADOW_TOURNAMENT_LOCAL", "true")
    monkeypatch.delenv("DASHBOARD_HOSTED", raising=False)
    monkeypatch.setattr(controller, "_monitor_running", lambda _path: True)
    observed: dict[str, object] = {}

    class Completed:
        returncode = 0

    def fake_run(command: list[str], **kwargs: object) -> Completed:
        observed["command"] = command
        observed["kwargs"] = kwargs
        return Completed()

    monkeypatch.setattr(controller.subprocess, "run", fake_run)

    result = invoke_shadow_tournament_controller(
        "stop",
        _dashboard(epoch_created=True),
        pid_record_path=tmp_path / "monitor.json",
    )

    assert result == "SHADOW01_MONITOR_STOP_REQUESTED"
    assert observed["command"] == [
        sys.executable,
        "-m",
        "src.ig_trader.shadow01",
        "stop",
        "--database",
        str(controller.DEFAULT_DATABASE_PATH),
    ]
    assert observed["kwargs"] == {
        "cwd": ROOT,
        "stdin": controller.subprocess.DEVNULL,
        "stdout": controller.subprocess.DEVNULL,
        "stderr": controller.subprocess.DEVNULL,
        "check": False,
        "shell": False,
        "timeout": 30,
    }
