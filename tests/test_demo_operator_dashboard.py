"""UI boundary tests for the local-only Demo Operator page."""

from __future__ import annotations

import json
from pathlib import Path

import streamlit as st
from streamlit.testing.v1 import AppTest

from dashboard.pages import demo_operator as demo_operator_page
from dashboard.sources.demo_operator import load_demo_operator_snapshot, research_instrument_rows

ROOT = Path(__file__).resolve().parents[1]


def _rendered(app_test: AppTest) -> str:
    elements = (
        *app_test.header,
        *app_test.subheader,
        *app_test.caption,
        *app_test.info,
        *app_test.warning,
    )
    return "\n".join(str(item.value) for item in elements)


def _app(monkeypatch, *, local_controls: bool) -> AppTest:
    st.cache_data.clear()
    monkeypatch.setenv("DASHBOARD_GITHUB_OFFLINE", "1")
    if local_controls:
        monkeypatch.setenv("DEMO_OPERATOR_LOCAL", "true")
        monkeypatch.delenv("DASHBOARD_HOSTED", raising=False)
    else:
        monkeypatch.delenv("DEMO_OPERATOR_LOCAL", raising=False)
        monkeypatch.delenv("DASHBOARD_HOSTED", raising=False)
    app_test = AppTest.from_file(ROOT / "dashboard" / "app.py").run(timeout=15)
    app_test.sidebar.radio[0].set_value("DEMO OPERATOR").run(timeout=15)
    assert not app_test.exception
    return app_test


def test_demo_operator_is_read_only_by_default_and_live_never_appears(monkeypatch) -> None:
    app_test = _app(monkeypatch, local_controls=False)
    labels = [button.label for button in app_test.button]

    assert labels == ["Refresh public GitHub status"]
    assert "READ ONLY" in _rendered(app_test)
    assert "LIVE_EXECUTION" not in "\n".join(labels)


def test_demo_operator_controls_render_only_in_explicit_local_mode(monkeypatch) -> None:
    app_test = _app(monkeypatch, local_controls=True)
    labels = {button.label for button in app_test.button}

    assert {
        "START DEMO ROBOT",
        "PAUSE NEW ENTRIES",
        "RESUME",
        "STOP",
        "EMERGENCY KILL",
        "FLATTEN ROBOT POSITIONS",
    } <= labels
    assert "START LIVE" not in labels
    assert next(button for button in app_test.button if button.label == "START DEMO ROBOT").disabled
    flatten = next(
        button for button in app_test.button if button.label == "FLATTEN ROBOT POSITIONS"
    )
    assert flatten.disabled


def test_local_kill_button_uses_controller_bridge_not_dashboard_broker_code(monkeypatch) -> None:
    monkeypatch.setattr(
        demo_operator_page,
        "invoke_local_controller",
        lambda command: f"controller bridge received {command}",
    )
    app_test = _app(monkeypatch, local_controls=True)

    next(button for button in app_test.button if button.label == "EMERGENCY KILL").click().run(
        timeout=15
    )

    assert "controller bridge received kill" in _rendered(app_test)


def test_flatten_requires_explicit_confirmation_before_using_controller_bridge(monkeypatch) -> None:
    monkeypatch.setattr(
        demo_operator_page,
        "invoke_local_controller",
        lambda command: f"controller bridge received {command}",
    )
    app_test = _app(monkeypatch, local_controls=True)
    flatten = next(
        button for button in app_test.button if button.label == "FLATTEN ROBOT POSITIONS"
    )
    assert flatten.disabled

    app_test.checkbox[0].set_value(True).run(timeout=15)
    next(
        button for button in app_test.button if button.label == "FLATTEN ROBOT POSITIONS"
    ).click().run(timeout=15)

    assert "controller bridge received flatten" in _rendered(app_test)


def test_operator_snapshot_is_file_only_and_filters_unknown_fields(tmp_path: Path) -> None:
    path = tmp_path / "operator_snapshot.json"
    path.write_text(
        json.dumps(
            {
                "environment": "IG_DEMO",
                "robot_state": "RUNNING",
                "positions": [{"epic": "CS.TEST", "deal_id": "D-1"}],
                "cst": "must-not-reach-ui",
            }
        ),
        encoding="utf-8",
    )

    snapshot = load_demo_operator_snapshot(path)

    assert snapshot.available
    assert snapshot.fields["environment"] == "IG_DEMO"
    assert "cst" not in snapshot.fields
    assert len(research_instrument_rows()) == 26
