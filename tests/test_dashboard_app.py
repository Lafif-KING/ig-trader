from __future__ import annotations

from pathlib import Path

import streamlit as st
from streamlit.testing.v1 import AppTest

from dashboard.models import GitHubStatus, WorkflowRun
from dashboard.sources import github as github_source

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


def _run_app(monkeypatch, page: str = "Overview", status: GitHubStatus | None = None) -> AppTest:
    st.cache_data.clear()
    monkeypatch.setenv("DASHBOARD_GITHUB_OFFLINE", "1")
    if status is not None:
        monkeypatch.delenv("DASHBOARD_GITHUB_OFFLINE", raising=False)
        monkeypatch.setattr(github_source, "fetch_github_status", lambda: status)
    app_test = AppTest.from_file(ROOT / "dashboard" / "app.py").run(timeout=15)
    if page != "Overview":
        app_test.sidebar.radio[0].set_value(page).run(timeout=15)
    assert not app_test.exception
    return app_test


def test_app_starts_and_overview_shows_safety_banner(monkeypatch) -> None:
    app_test = _run_app(monkeypatch)
    rendered = _rendered(app_test)
    assert "IG Trader Control Center" in rendered
    assert "EXECUTION MODE: NO_EXECUTION" in rendered
    assert "BROKER ORDER AUTHORITY: OFF" in rendered
    assert "Real Azure Database Recovery — HOLD" in rendered
    assert "The real migration 003 database state is unknown." in rendered
    assert "Do not retry bootstrap-admin." in rendered
    assert "RECOVERY HOLD" in rendered


def test_locked_pages_and_no_data_shadow_state(monkeypatch) -> None:
    demo = _run_app(monkeypatch, "Demo Results")
    live = _run_app(monkeypatch, "Live Results")
    shadow = _run_app(monkeypatch, "Shadow Results")
    assert "DEMO_EXECUTION DISABLED" in _rendered(demo)
    assert "LIVE_EXECUTION DISABLED" in _rendered(live)
    assert "SHADOW CLOUD DATA NOT AVAILABLE YET" in _rendered(shadow)
    assert "Historical offline replay — not live Shadow results." in _rendered(shadow)
    assert "0 wins" in _rendered(shadow)


def test_github_unavailable_mode_does_not_crash(monkeypatch) -> None:
    app_test = _run_app(monkeypatch, "Tests & GitHub CI")
    assert "GITHUB DATA TEMPORARILY UNAVAILABLE" in _rendered(app_test)


def test_strategy_lab_page_is_read_only_with_or_without_local_artifacts(monkeypatch) -> None:
    app_test = _run_app(monkeypatch, "Strategy Lab")
    rendered = _rendered(app_test)
    assert "STRATEGY LAB ARTIFACTS NOT AVAILABLE" in rendered or "Leaderboard" in rendered
    labels = [button.label.casefold() for button in app_test.button]
    assert labels == ["refresh public github status"]


def test_mocked_green_ci_and_main_sha_render(monkeypatch) -> None:
    workflow = WorkflowRun(
        "CI", 9, "completed", "success", "c" * 40, "main", "https://example.test", None, None
    )
    app_test = _run_app(
        monkeypatch, "Tests & GitHub CI", GitHubStatus(True, "d" * 40, latest_workflow=workflow)
    )
    rendered = _rendered(app_test)
    assert "Latest workflow: PASS — MAIN" in rendered
    assert "d" * 40 in rendered


def test_mocked_failed_ci_renders_first_failed_step(monkeypatch) -> None:
    workflow = WorkflowRun(
        "CI",
        10,
        "completed",
        "failure",
        "e" * 40,
        "main",
        "https://example.test",
        None,
        None,
        first_failed_step="Dashboard tests",
    )
    app_test = _run_app(
        monkeypatch, "Tests & GitHub CI", GitHubStatus(True, "f" * 40, latest_workflow=workflow)
    )
    assert "Dashboard tests" in _rendered(app_test)


def test_mocked_active_pr_ci_context_renders(monkeypatch) -> None:
    workflow = WorkflowRun(
        "CI",
        11,
        "in_progress",
        None,
        "e" * 40,
        "codex/dashboard",
        "https://example.test",
        None,
        None,
        pull_request=7,
    )
    app_test = _run_app(
        monkeypatch,
        "Tests & GitHub CI",
        GitHubStatus(
            True,
            "f" * 40,
            latest_workflow=workflow,
            workflow_context="ACTIVE PR #7",
        ),
    )
    rendered = _rendered(app_test)
    assert "Latest workflow: IN PROGRESS — ACTIVE PR #7" in rendered


def test_active_pr_without_ci_is_clearly_unreported(monkeypatch) -> None:
    app_test = _run_app(
        monkeypatch,
        "Tests & GitHub CI",
        GitHubStatus(
            True,
            "f" * 40,
            workflow_context="ACTIVE PR #7 — CI NOT YET REPORTED",
        ),
    )
    assert "ACTIVE PR #7 — CI NOT YET REPORTED: GitHub did not report a workflow run." in _rendered(
        app_test
    )


def test_roadmap_renders_reviewed_gate_and_no_activation_button(monkeypatch) -> None:
    app_test = _run_app(monkeypatch, "Project Roadmap")
    rendered = _rendered(app_test)
    assert any("IG Demo Authentication" in expander.label for expander in app_test.expander)
    assert "**Governance status:** PASS" in rendered
    labels = [button.label.casefold() for button in app_test.button]
    assert labels == ["refresh public github status"]


def test_raw_github_environment_value_is_never_rendered(monkeypatch) -> None:
    raw_value = "test-dashboard-value-must-not-render"
    monkeypatch.setenv("GITHUB_TOKEN", raw_value)
    app_test = _run_app(monkeypatch)
    assert raw_value not in _rendered(app_test)
