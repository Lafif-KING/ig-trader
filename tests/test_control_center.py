"""Safety and state tests for the UI-MVP Control Center source layer."""

from __future__ import annotations

import json

from dashboard.sources.control_center import load_control_center_state
from dashboard.sources.demo_operator import DemoOperatorSnapshot
from dashboard.sources.project import load_project_status


def _state(fields: dict[str, object] | None = None):
    return load_control_center_state(
        load_project_status(), DemoOperatorSnapshot(bool(fields), fields or {})
    )


def test_start_is_disabled_when_no_demo_strategies_are_approved(monkeypatch) -> None:
    monkeypatch.setenv("DEMO_OPERATOR_LOCAL", "true")
    state = _state(
        {
            "environment": "IG_DEMO",
            "rest_status": "CONNECTED",
            "account": "Demo account ••••1234",
            "robot_state": "STOPPED",
            "kill_switch_state": "RELEASED",
            "execution_authority": "ON",
            "approved_demo_epic_count": 1,
            "approved_demo_strategy_count": 0,
            "risk_configuration_status": "VALID",
            "reconciliation_status": "NORMAL",
        }
    )

    assert not state.start_gate.enabled
    assert "No strategies are currently approved for Demo execution." in state.start_gate.blockers


def test_start_gate_keeps_kill_switch_and_execution_authority_blockers(monkeypatch) -> None:
    monkeypatch.setenv("DEMO_OPERATOR_LOCAL", "true")
    state = _state(
        {
            "environment": "IG_DEMO",
            "rest_status": "CONNECTED",
            "account": "Demo account ••••1234",
            "robot_state": "STOPPED",
            "kill_switch_state": "BLOCKING",
            "execution_authority": "OFF",
            "approved_demo_epic_count": 1,
            "approved_demo_strategy_count": 1,
            "risk_configuration_status": "VALID",
            "reconciliation_status": "NORMAL",
        }
    )

    assert "Kill switch is active or not proven released." in state.start_gate.blockers
    assert "Execution authority is OFF." in state.start_gate.blockers


def test_start_gate_requires_local_demo_operator_context(monkeypatch) -> None:
    monkeypatch.delenv("DEMO_OPERATOR_LOCAL", raising=False)
    monkeypatch.delenv("DASHBOARD_HOSTED", raising=False)
    state = _state()

    assert not state.start_gate.enabled
    assert any("DEMO_OPERATOR_LOCAL=true" in item for item in state.start_gate.blockers)


def test_live_environment_is_read_only_and_cannot_start(monkeypatch) -> None:
    monkeypatch.setenv("DEMO_OPERATOR_LOCAL", "true")
    state = _state({"environment": "IG_LIVE"})

    assert state.robot.environment == "IG LIVE"
    assert not state.start_gate.enabled
    assert "The configured environment is not IG Demo." in state.start_gate.blockers


def test_position_marks_buy_at_bid_and_sell_at_offer() -> None:
    state = _state(
        {
            "positions": [
                {
                    "ownership": "RECONCILED",
                    "instrument": "EURGBP",
                    "epic": "CS.D.EURGBP.MINI.IP",
                    "direction": "BUY",
                    "deal_id": "DEAL-1234567890",
                    "bid": "0.85010",
                    "offer": "0.85013",
                },
                {
                    "ownership": "RECONCILED",
                    "instrument": "EURUSD",
                    "epic": "CS.D.EURUSD.CFD.IP",
                    "direction": "SELL",
                    "bid": "1.17010",
                    "offer": "1.17014",
                },
            ]
        }
    )

    assert [item.executable_mark for item in state.positions] == ["0.85010", "1.17014"]
    assert state.positions[0].deal_id == "DEAL…7890"


def test_mock_mode_is_explicit_and_never_enables_demo_controls(monkeypatch) -> None:
    monkeypatch.setenv("CONTROL_CENTER_MODE", "MOCK")
    state = _state({"environment": "IG_DEMO", "execution_authority": "ON"})

    assert state.simulated
    assert state.source_label == "MOCK / REPLAY SOURCE"
    assert not state.start_gate.enabled
    assert "SIMULATED UI DATA cannot start the Demo robot." in state.start_gate.blockers


def test_external_research_summary_cannot_grant_execution_approval(tmp_path, monkeypatch) -> None:
    path = tmp_path / "alpha_status.json"
    path.write_text(
        json.dumps(
            {
                "research_id": "ALPHA-02",
                "status": "REJECTED",
                "tested": 16,
                "qualified": 99,
                "execution_authority": "ON",
                "approved_demo_strategy_count": 99,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("CONTROL_CENTER_RESEARCH_SUMMARIES", str(path))
    state = _state()

    alpha = next(item for item in state.research if item.research_id == "ALPHA-02")
    assert alpha.status == "REJECTED"
    assert alpha.qualified == "99"
    assert state.health.approved_strategy_count == 0
    assert not state.start_gate.enabled
