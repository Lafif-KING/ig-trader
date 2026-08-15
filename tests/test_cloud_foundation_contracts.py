"""Static contracts for the G4A image, CI, Azure and PostgreSQL definitions."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _read(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def test_dockerfile_is_pinned_non_root_and_safe_by_default() -> None:
    dockerfile = _read("Dockerfile")

    assert re.search(r"python:3\.13\.14-slim-bookworm@sha256:[0-9a-f]{64}", dockerfile)
    assert "poetry sync --only main --no-root" in dockerfile
    assert "USER 10001:10001" in dockerfile
    assert "EXECUTION_MODE=NO_EXECUTION" in dockerfile
    assert 'ENTRYPOINT ["python", "-m", "src.ig_trader.cloud_runtime"]' in dockerfile
    assert "STOPSIGNAL SIGTERM" in dockerfile
    assert "/health/live" in dockerfile
    assert "COPY . ." not in dockerfile


def test_ci_contains_every_required_gate_and_sha_pins_actions() -> None:
    workflow = _read(".github/workflows/ci.yaml")
    ordered = [
        "- name: Collect and run complete clean suite",
        "- name: G1 focused tests",
        "- name: G2 focused tests",
        "- name: G3A focused tests",
        "- name: G4A focused tests",
        "- name: Ruff",
        "- name: Formatting",
        "- name: Repository pre-commit hooks",
        "- name: Validate Azure Bicep",
        "- name: Secret scan",
        "- name: Build commit-addressed image",
        "- name: Safe container acceptance test",
        "- name: Inspect image identity and contents",
        "- name: Build compact remote CI evidence",
    ]
    positions = [workflow.index(value) for value in ordered]
    assert positions == sorted(positions)
    assert "poetry check --lock" in workflow
    assert "az bicep install --version v0.45.15" in workflow
    assert "az bicep build --file infra/azure/app.bicep" in workflow
    assert "az bicep build-params" in workflow
    assert "pre-commit run --all-files" in workflow
    assert "tests/test_ig_auth_diagnostic.py" in workflow
    assert "tests/test_g2_offline_paper.py" in workflow
    assert "tests/test_g3a_market_data.py" in workflow
    assert "tests/test_cloud_runtime.py" in workflow
    assert "order_endpoint_call_count" in _read("tools/g4a_container_smoke.py")
    assert "tools/g4a_image_inspect.py" in workflow
    assert "tools/g4a_ci_evidence.py" in workflow
    for reference in re.findall(r"uses:\s+[^@\s]+@([^\s#]+)", workflow):
        assert re.fullmatch(r"[0-9a-f]{40}", reference)


def test_azure_app_is_a_single_safe_execution_worker() -> None:
    application = _read("infra/azure/app.bicep")

    assert "activeRevisionsMode: 'Single'" in application
    assert "minReplicas: 1" in application
    assert "maxReplicas: 1" in application
    assert "value: 'NO_EXECUTION'" in application
    assert "param enableBrokerSecretReferences bool = false" in application
    assert "external: false" in application
    assert "secretRef: 'ig-api-key'" in application
    assert "keyVaultUrl:" in application
    assert "passwordSecretRef" not in application


def test_azure_foundation_has_private_identity_based_persistence() -> None:
    foundation = _read("infra/azure/foundation.bicep")

    assert "publicNetworkAccess: 'Disabled'" in foundation
    assert "passwordAuth: 'Disabled'" in foundation
    assert "activeDirectoryAuth: 'Enabled'" in foundation
    assert "enableRbacAuthorization: true" in foundation
    assert "adminUserEnabled: false" in foundation
    assert "destination: 'azure-monitor'" in foundation
    assert "privateDnsZoneArmResourceId" in foundation


def test_postgresql_migration_covers_durable_trading_state() -> None:
    migration = _read("migrations/postgresql/001_execution_state.sql")

    for table in (
        "trade_intents",
        "lifecycle_events",
        "broker_references",
        "position_state",
        "reconciliation_state",
        "evidence_metadata",
        "worker_leases",
    ):
        assert f"trading.{table}" in migration
    assert "confirmation_status = 'ACCEPTED'" in migration
    assert "append-only table mutation is prohibited" in migration
    assert "lease_name = 'execution-worker'" in migration


def test_offline_sqlite_paths_are_not_replaced() -> None:
    assert "sqlite3" in _read("src/ig_trader/offline_paper/persistence.py")
    assert 'DATABASE_URL = "sqlite:///./trading.db"' in _read("src/ig_trader/database.py")
