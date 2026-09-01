from __future__ import annotations

from pathlib import Path

import streamlit as st
from streamlit.testing.v1 import AppTest

from dashboard import shadow_tournament_control
from dashboard.app import PAGES
from dashboard.pages import demo_operator as demo_operator_page
from src.ig_trader.shadow01.local_demo_read_only import Shadow01LocalDemoReadOnlyFactory

ROOT = Path(__file__).resolve().parents[1]


def _rendered(app_test: AppTest) -> str:
    elements = (
        *app_test.title,
        *app_test.header,
        *app_test.subheader,
        *app_test.markdown,
        *app_test.caption,
        *app_test.error,
        *app_test.warning,
        *app_test.success,
        *app_test.info,
        *app_test.metric,
    )
    return "\n".join(str(element.value) for element in elements)


def _run_app(monkeypatch, page: str = "COCKPIT") -> AppTest:
    st.cache_data.clear()
    monkeypatch.setenv("DASHBOARD_GITHUB_OFFLINE", "1")
    app_test = AppTest.from_file(ROOT / "dashboard" / "app.py").run(timeout=15)
    if page != "COCKPIT":
        app_test.sidebar.radio[0].set_value(page).run(timeout=15)
    assert not app_test.exception
    return app_test


def test_mvp_navigation_includes_the_separate_shadow_tournament_page() -> None:
    assert PAGES == (
        "COCKPIT",
        "MARKET SCANNER",
        "STRATEGY CENTER",
        "POSITIONS",
        "PERFORMANCE",
        "DECISION EXPLORER",
        "RISK & HEALTH",
        "DEMO OPERATOR",
        "SHADOW TOURNAMENT",
    )


def test_every_mvp_page_is_renderable_without_starting_a_robot(monkeypatch) -> None:
    for page in PAGES:
        app_test = _run_app(monkeypatch, page)
        assert not app_test.exception


def test_app_starts_in_demo_cockpit_with_no_trade_authority(monkeypatch) -> None:
    app_test = _run_app(monkeypatch)
    rendered = _rendered(app_test)

    assert "IG Trader Control Center" in rendered
    assert "IG DEMO MODE" in rendered
    assert "ROBOT CANNOT START NEW TRADES" in rendered
    assert "No strategies are currently approved for Demo execution." in rendered


def test_operator_pages_render_unknowns_without_fabricating_data(monkeypatch) -> None:
    scanner = _run_app(monkeypatch, "MARKET SCANNER")
    positions = _run_app(monkeypatch, "POSITIONS")
    performance = _run_app(monkeypatch, "PERFORMANCE")
    decisions = _run_app(monkeypatch, "DECISION EXPLORER")

    assert "Verified research universe" in _rendered(scanner)
    assert "No robot-owned positions." in _rendered(positions)
    assert "DEMO EVIDENCE NOT AVAILABLE YET" in _rendered(performance)
    assert "No robot decision source is available" in _rendered(decisions)


def test_strategy_center_separates_research_from_execution_approval(monkeypatch) -> None:
    app_test = _run_app(monkeypatch, "STRATEGY CENTER")
    rendered = _rendered(app_test)

    assert "DEMO APPROVED STRATEGY COUNT = 0" in rendered
    assert "Only the integrated approved Demo execution registry" in rendered
    assert [button.label.casefold() for button in app_test.button] == [
        "refresh public github status"
    ]


def test_mock_mode_is_visibly_separate_from_demo_state(monkeypatch) -> None:
    monkeypatch.setenv("CONTROL_CENTER_MODE", "MOCK")
    app_test = _run_app(monkeypatch, "POSITIONS")

    assert "SIMULATED UI DATA" in _rendered(app_test)
    assert app_test.dataframe


def test_raw_github_environment_value_is_never_rendered(monkeypatch) -> None:
    raw_value = "test-dashboard-value-must-not-render"
    monkeypatch.setenv("GITHUB_TOKEN", raw_value)
    app_test = _run_app(monkeypatch)

    assert raw_value not in _rendered(app_test)


def test_shadow_tournament_is_zero_order_and_epoch_gated(monkeypatch) -> None:
    monkeypatch.setenv("SHADOW_TOURNAMENT_LOCAL", "true")
    app_test = _run_app(monkeypatch, "SHADOW TOURNAMENT")
    rendered = _rendered(app_test)

    assert "SHADOW ONLY—ZERO ORDERS" in rendered
    assert "Start is disabled until a separately created Shadow Tournament epoch exists." in (
        rendered
    )
    start = next(button for button in app_test.button if button.label == "START SHADOW MONITOR")
    assert start.disabled


def test_opening_shadow_control_center_starts_no_monitor_or_local_demo(monkeypatch) -> None:
    """The full Streamlit page-open path must not invoke either worker bridge."""

    shadow_monitor_popen_calls: list[object] = []
    demo_controller_calls: list[str] = []
    local_demo_build_calls: list[object] = []

    def unexpected_shadow_monitor_popen(*args: object, **kwargs: object) -> None:
        shadow_monitor_popen_calls.append((args, kwargs))
        raise AssertionError("opening the Shadow control center must not call Popen")

    def unexpected_demo_controller(command: str) -> str:
        demo_controller_calls.append(command)
        raise AssertionError("opening the Shadow control center must not start the Demo controller")

    def unexpected_local_demo_build(
        factory: Shadow01LocalDemoReadOnlyFactory,
    ) -> object:
        local_demo_build_calls.append(factory)
        raise AssertionError(
            "opening the Shadow control center must not build a local Demo adapter"
        )

    monkeypatch.setattr(
        shadow_tournament_control.subprocess,
        "Popen",
        unexpected_shadow_monitor_popen,
    )
    monkeypatch.setattr(demo_operator_page, "invoke_local_controller", unexpected_demo_controller)
    monkeypatch.setattr(Shadow01LocalDemoReadOnlyFactory, "build", unexpected_local_demo_build)
    monkeypatch.setenv("SHADOW_TOURNAMENT_LOCAL", "true")
    monkeypatch.setenv("DEMO_OPERATOR_LOCAL", "true")

    app_test = _run_app(monkeypatch, "SHADOW TOURNAMENT")

    assert not app_test.exception
    assert shadow_monitor_popen_calls == []
    assert demo_controller_calls == []
    assert local_demo_build_calls == []
