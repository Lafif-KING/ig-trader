from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_codex_helpers_are_read_only_and_secret_safe() -> None:
    preflight = (ROOT / "tools/codex/azure-preflight.ps1").read_text()
    assert "get-access-token" not in preflight.lower()
    assert "expose-token" not in preflight.lower()
    assert "'account', 'show'" in preflight.lower()
    assert "'containerapp', 'show'" in preflight.lower()
    assert "'postgres', 'flexible-server', 'list'" in preflight.lower()


def test_project_rules_and_state_have_required_safety_markers() -> None:
    agents = (ROOT / "AGENTS.md").read_text()
    state = (ROOT / "docs/PROJECT_STATE.md").read_text()
    for marker in ("NO_EXECUTION", "Never print", "fencing token", "least privilege"):
        assert marker in agents
    for marker in ("e7f37c143baf0a6ca5819144c2f7780eef72b76d", "missing=0", "NO_EXECUTION"):
        assert marker in state
    forbidden = ("password=", "client_secret", "access_token", "IG_API_KEY")
    combined = (agents + state).lower()
    assert not any(item.lower() in combined for item in forbidden)
