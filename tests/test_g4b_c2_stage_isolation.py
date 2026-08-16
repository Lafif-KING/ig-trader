"""Contracts for the isolated G4B-01C2 Azure deployment stage."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "infra/azure/dev-shadow-c2.bicep"
PARAMETERS = ROOT / "infra/azure/dev-shadow-c2.parameters.bicepparam"

APPROVED_COMMIT = "903dff5d07af03da593d3afff8b53c427704bd21"
APPROVED_DIGEST = (
    "igtrdevfrcbzkxc6c6acr.azurecr.io/ig-trader@sha256:"
    "cf90c62dbe81166414a864435bff8de2ab2adfd566dd22793571eb9d8accaf45"
)

RESOURCE_PATTERN = re.compile(
    r"resource\s+\w+\s+'(?P<type>[^'@]+)@[^']+'\s+(?P<existing>existing\s+)?=",
)


def _template() -> str:
    return TEMPLATE.read_text(encoding="utf-8")


def _parameters() -> str:
    return PARAMETERS.read_text(encoding="utf-8")


def _resources(source: str, *, existing: bool) -> list[str]:
    return [
        match.group("type")
        for match in RESOURCE_PATTERN.finditer(source)
        if bool(match.group("existing")) is existing
    ]


def test_c2_owns_exactly_three_resource_types() -> None:
    source = _template()

    assert _resources(source, existing=False) == [
        "Microsoft.App/managedEnvironments",
        "Microsoft.Insights/diagnosticSettings",
        "Microsoft.App/containerApps",
    ]


def test_c2_dependencies_are_existing_and_read_only() -> None:
    source = _template()

    assert _resources(source, existing=True) == [
        "Microsoft.ContainerRegistry/registries",
        "Microsoft.ManagedIdentity/userAssignedIdentities",
        "Microsoft.Network/virtualNetworks",
        "Microsoft.Network/virtualNetworks/subnets",
        "Microsoft.OperationalInsights/workspaces",
    ]
    for forbidden_configuration in (
        "adminUserEnabled",
        "anonymousPullEnabled",
        "addressPrefixes",
        "delegations:",
        "retentionInDays",
    ):
        assert forbidden_configuration not in source


def test_c2_excludes_postgresql_roles_key_vault_and_new_networking() -> None:
    source = _template()

    for forbidden_resource in (
        "Microsoft.Authorization/roleAssignments",
        "Microsoft.DBforPostgreSQL",
        "Microsoft.KeyVault",
        "Microsoft.Network/privateEndpoints",
        "Microsoft.Network/publicIPAddresses",
        "Microsoft.Network/natGateways",
        "Microsoft.Network/azureFirewalls",
    ):
        assert forbidden_resource not in source


def test_c2_preserves_no_execution_runtime_contract() -> None:
    source = _template()

    for expected in (
        "activeRevisionsMode: 'Single'",
        "external: false",
        "secrets: []",
        "value: 'NO_EXECUTION'",
        "cpu: json('0.5')",
        "memory: '1Gi'",
        "minReplicas: 1",
        "maxReplicas: 1",
        "internal: true",
        "zoneRedundant: false",
    ):
        assert expected in source
    for broker_setting in (
        "IG_ACCOUNT_ID",
        "IG_API_KEY",
        "IG_IDENTIFIER",
        "IG_PASSWORD",
        "keyVaultUrl",
        "secretRef:",
    ):
        assert broker_setting not in source


def test_c2_parameters_pin_exact_image_and_existing_resource_names() -> None:
    source = _template()
    parameters = _parameters()

    assert APPROVED_DIGEST in source
    assert APPROVED_DIGEST in parameters
    assert APPROVED_COMMIT in source
    assert APPROVED_COMMIT in parameters
    for existing_name in (
        "igtrdevfrcbzkxc6c6acr",
        "igtrdevfrc-execution-identity",
        "igtrdevfrc-vnet",
        "container-apps",
        "igtrdevfrc-logs",
    ):
        assert existing_name in parameters
    assert "@sha256:" in parameters
    assert ":latest" not in parameters


def test_ci_lints_and_compiles_c2_template_and_parameters() -> None:
    workflow = (ROOT / ".github/workflows/ci.yaml").read_text(encoding="utf-8")

    assert "tests/test_g4b_c2_stage_isolation.py" in workflow
    assert "az bicep lint --file infra/azure/dev-shadow-c2.bicep" in workflow
    assert "az bicep build --file infra/azure/dev-shadow-c2.bicep" in workflow
    assert "--file infra/azure/dev-shadow-c2.parameters.bicepparam" in workflow
