from __future__ import annotations

import ast
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _dashboard_sources() -> tuple[Path, ...]:
    return tuple((ROOT / "dashboard").rglob("*.py"))


def test_dashboard_has_no_broker_database_or_azure_imports() -> None:
    prohibited = {
        "azure",
        "ig_trader",
        "lightstreamer",
        "lightstreamer_client_lib",
        "psycopg",
        "sqlalchemy",
        "ta",
    }
    for path in _dashboard_sources():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        imports = [
            node for node in ast.walk(tree) if isinstance(node, (ast.Import, ast.ImportFrom))
        ]
        names = {
            name.name.split(".")[0]
            for node in imports
            for name in (node.names if isinstance(node, ast.Import) else ())
        }
        names.update(
            node.module.split(".")[0]
            for node in imports
            if isinstance(node, ast.ImportFrom) and node.module
        )
        assert not names.intersection(prohibited), path


def test_dashboard_source_has_no_mutating_http_method_calls() -> None:
    source = (ROOT / "dashboard" / "sources" / "github.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    methods = {
        node.func.attr.lower()
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    assert not methods.intersection({"post", "put", "patch", "delete"})
    assert "get" in methods


def test_dashboard_has_only_a_non_authoritative_refresh_button() -> None:
    app_source = (ROOT / "dashboard" / "app.py").read_text(encoding="utf-8")
    assert 'st.sidebar.button("Refresh public GitHub status")' in app_source
    assert "enable_demo" not in app_source.casefold()
    assert "enable_live" not in app_source.casefold()


def test_no_streamlit_secret_file_is_tracked() -> None:
    tracked = subprocess.run(
        ["git", "ls-files"], cwd=ROOT, check=True, text=True, capture_output=True
    ).stdout.splitlines()
    assert ".streamlit/secrets.toml" not in tracked


def test_dashboard_image_is_separate_non_root_and_does_not_copy_trading_source() -> None:
    dockerfile = (ROOT / "Dockerfile.dashboard").read_text(encoding="utf-8")
    assert "USER 10002:10002" in dockerfile
    assert "COPY src" not in dockerfile
    assert "poetry sync --only dashboard --no-root --no-ansi" in dockerfile
    assert "--with dashboard" not in dockerfile
    assert '"streamlit", "run", "dashboard/app.py"' in dockerfile


def test_dashboard_dependency_group_and_ci_proof_are_isolated() -> None:
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    ci = (ROOT / ".github" / "workflows" / "ci.yaml").read_text(encoding="utf-8")
    assert "[tool.poetry.group.dashboard]" in pyproject
    assert "optional = true" in pyproject
    assert "[tool.poetry.group.dashboard.dependencies]" in pyproject
    for dependency in ('streamlit = "1.61.1"', 'httpx = "^0.28.1"', 'jsonschema = "^4.26.0"'):
        assert dependency in pyproject
    assert "poetry sync --with dashboard --no-interaction --no-ansi" in ci
    assert "Verify dashboard image has only dashboard dependencies" in ci
    assert '"codex/pi1-project-dashboard-mvp"' not in ci
    for dependency in (
        "streamlit",
        "httpx",
        "jsonschema",
        "psycopg",
        "sqlalchemy",
        "azure",
        "lightstreamer_client_lib",
        "ta",
    ):
        assert dependency in ci
