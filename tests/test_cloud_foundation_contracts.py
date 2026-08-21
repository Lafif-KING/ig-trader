"""Static contracts for the G4A image, CI, Azure and PostgreSQL definitions."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from tools.validate_immutable_image import validate_immutable_image_reference

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
        "- name: G4B operations focused tests",
        "- name: G4B execution lease unit tests",
        "- name: G4B PostgreSQL fencing proof",
        "- name: G4B PostgreSQL two-process handoff proof",
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
    assert "az bicep build --file infra/azure/dev-shadow-app.bicep" in workflow
    assert "az bicep build --file infra/azure/dev-shadow-c2.bicep" in workflow
    assert "az bicep build --file infra/azure/dev-shadow-foundation.bicep" in workflow
    assert "az bicep build-params" in workflow
    assert "pre-commit run --all-files" in workflow
    assert "tests/test_ig_auth_diagnostic.py" in workflow
    assert "tests/test_g2_offline_paper.py" in workflow
    assert "tests/test_g3a_market_data.py" in workflow
    assert "tests/test_cloud_runtime.py" in workflow
    assert "tests/test_execution_lease.py" in workflow
    assert "tests-g4b-lease-unit.xml" in workflow
    assert "tests-g4b-lease-fencing.xml" in workflow
    assert "tests-g4b-lease-concurrency.xml" in workflow
    assert 'RUN_POSTGRES_INTEGRATION: "1"' in workflow
    assert "timeout --signal=TERM --kill-after=10s 60s" in workflow
    assert "postgres:16.10-bookworm@sha256:" in workflow
    assert "POSTGRES_HOST_AUTH_METHOD: trust" in workflow
    assert "order_endpoint_call_count" in _read("tools/g4a_container_smoke.py")
    assert "NO_EXECUTION lease or fencing metadata is unsafe" in _read(
        "tools/g4a_container_smoke.py"
    )
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


def test_low_cost_dev_shadow_profile_preserves_execution_safety() -> None:
    foundation = _read("infra/azure/dev-shadow-foundation.bicep")
    application = _read("infra/azure/dev-shadow-app.bicep")

    assert "name: 'Basic'" in foundation
    assert "adminUserEnabled: false" in foundation
    assert "publicNetworkAccess: 'Enabled'" in foundation
    assert "name: 'Standard_B1ms'" in foundation
    assert "tier: 'Burstable'" in foundation
    assert "backupRetentionDays: 7" in foundation
    assert "mode: 'Disabled'" in foundation
    assert "storageSizeGB: 32" in foundation
    assert "version: '16'" in foundation
    assert "passwordAuth: 'Disabled'" in foundation
    assert "activeDirectoryAuth: 'Enabled'" in foundation
    assert "privateDnsZoneArmResourceId" in foundation
    assert "retentionInDays: 30" in foundation
    assert "Microsoft.KeyVault" not in foundation
    assert "Microsoft.Network/privateEndpoints" not in foundation

    assert "activeRevisionsMode: 'Single'" in application
    assert "minReplicas: 1" in application
    assert "maxReplicas: 1" in application
    assert "value: 'NO_EXECUTION'" in application
    assert "secrets: []" in application
    assert "The input must use repository@sha256:DIGEST form" in application
    assert "@sha256:" in application
    assert "IG_API_KEY" not in application
    assert "IG_IDENTIFIER" not in application
    assert "IG_PASSWORD" not in application
    assert "keyVaultUrl" not in application


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


def test_immutable_image_reference_rejects_incomplete_or_invalid_digests() -> None:
    valid = "registry.example/ig-trader@sha256:" + "a" * 64
    assert validate_immutable_image_reference(valid) == valid
    for image in (
        "registry.example/ig-trader@sha256",
        "registry.example/ig-trader@sha256:",
        "registry.example/ig-trader@sha256:null",
        "registry.example/ig-trader@sha256:" + "a" * 63,
        "registry.example/ig-trader@sha256:" + "g" * 64,
        "registry.example/ig-trader:latest",
    ):
        with pytest.raises(ValueError):
            validate_immutable_image_reference(image)


def test_db_bootstrap_deployment_validates_image_before_azure_mutation() -> None:
    deployment = _read("tools/codex/deploy-db-bootstrap.ps1")

    validation = deployment.index("tools/validate_immutable_image.py")
    azure_mutation = deployment.index("az deployment group create")
    assert validation < azure_mutation
    assert "if ($LASTEXITCODE -ne 0)" in deployment[validation:azure_mutation]
    assert "ValidateOnly" in deployment
