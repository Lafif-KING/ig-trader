"""Static regression checks for the Shadow01 zero-execution boundary."""

from __future__ import annotations

import ast
import inspect
from collections.abc import Callable
from pathlib import Path

import pytest

import src.ig_trader.shadow01.runtime as runtime_module
from dashboard.sources.shadow_tournament import load_shadow_tournament_dashboard
from src.ig_trader.shadow01.config import ShadowConfigError, load_config
from src.ig_trader.shadow01.read_only_broker import ReadOnlyBrokerError, Shadow01ReadOnlyBroker
from src.ig_trader.shadow01.runtime import Shadow01Runtime


class _RecordingTransport:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def authorized_request(self, method: str, endpoint: str, **_: object) -> object:
        self.calls.append((method, endpoint))
        return {}


def test_shadow_runtime_has_no_execution_import_or_public_execution_surface() -> None:
    source_path = Path(inspect.getsourcefile(runtime_module) or "")
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
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
    public_methods = {
        name
        for name, value in vars(Shadow01Runtime).items()
        if isinstance(value, Callable) and not name.startswith("_")
    }

    assert not any(
        module.startswith(
            (
                "src.ig_trader.execution",
                "src.ig_trader.demo",
                "src.ig_trader.main",
                "src.ig_trader.shadow_execution",
            )
        )
        for module in imports
    )
    assert (
        not {
            "create_position",
            "close_position",
            "create_working_order",
            "modify_working_order",
            "enable_execution_authority",
        }
        & public_methods
    )
    assert Shadow01Runtime.execution_authority == "OFF"


@pytest.mark.parametrize(
    "endpoint",
    ("/positions/otc", "/workingorders/otc", "/confirms/test"),
)
def test_forbidden_broker_routes_never_reach_transport(endpoint: str) -> None:
    transport = _RecordingTransport()
    broker = Shadow01ReadOnlyBroker(transport)

    with pytest.raises(ReadOnlyBrokerError):
        broker._request("POST", endpoint)

    assert transport.calls == []
    assert broker.execution_authority == "OFF"


def test_config_refuses_an_execution_authority_change(tmp_path: Path) -> None:
    path = tmp_path / "shadow01-config.json"
    path.write_text(
        (Path(__file__).parents[1] / "shadow01_strategy_config.json")
        .read_text(encoding="utf-8")
        .replace('"execution_authority": "OFF"', '"execution_authority": "ON"'),
        encoding="utf-8",
    )

    with pytest.raises(ShadowConfigError, match="EXECUTION_AUTHORITY_MUST_REMAIN_OFF"):
        load_config(path)


def test_opening_shadow_dashboard_cannot_create_a_worker_or_store(tmp_path: Path) -> None:
    database = tmp_path / "runtime" / "shadow_tournament.sqlite3"

    dashboard = load_shadow_tournament_dashboard(
        config_path=Path(__file__).parents[1] / "shadow01_strategy_config.json",
        database_path=database,
    )

    assert not dashboard.epoch_created
    assert not database.exists()
