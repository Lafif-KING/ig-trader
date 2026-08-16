"""Contracts for the source-only G4B-02A1 Azure monitoring stage."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "infra/azure/dev-shadow-ops.bicep"
PARAMETERS = ROOT / "infra/azure/dev-shadow-ops.parameters.bicepparam"
RUNBOOK = ROOT / "docs/G4B-02A1-OPERATIONAL-ALERTS.md"

RESOURCE_PATTERN = re.compile(
    r"resource\s+\w+\s+'(?P<type>[^'@]+)@[^']+'\s+(?P<existing>existing\s+)?=",
)


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _resources(source: str, *, existing: bool) -> list[str]:
    return [
        match.group("type")
        for match in RESOURCE_PATTERN.finditer(source)
        if bool(match.group("existing")) is existing
    ]


def _resource_block(source: str, symbolic_name: str, next_name: str) -> str:
    start = source.index(f"resource {symbolic_name} ")
    end = source.index(f"resource {next_name} ", start)
    return source[start:end]


def test_ops_stage_owns_only_approved_monitoring_resources() -> None:
    source = _read(TEMPLATE)

    assert _resources(source, existing=False) == [
        "Microsoft.Insights/actionGroups",
        "Microsoft.Insights/metricAlerts",
        "Microsoft.Insights/metricAlerts",
        "Microsoft.Insights/metricAlerts",
        "Microsoft.Insights/scheduledQueryRules",
    ]
    assert _resources(source, existing=True) == [
        "Microsoft.App/containerApps",
        "Microsoft.OperationalInsights/workspaces",
    ]


def test_ops_stage_cannot_modify_app_data_network_identity_or_budget() -> None:
    source = _read(TEMPLATE)

    for forbidden in (
        "Microsoft.App/managedEnvironments",
        "Microsoft.Authorization/roleAssignments",
        "Microsoft.Consumption/budgets",
        "Microsoft.ContainerRegistry/registries",
        "Microsoft.DBforPostgreSQL",
        "Microsoft.KeyVault",
        "Microsoft.ManagedIdentity",
        "Microsoft.Network",
        "privateDns",
        "subnets",
    ):
        assert forbidden not in source
    assert "resource executionWorker 'Microsoft.App/containerApps@2025-01-01' existing" in source
    assert (
        "resource logAnalyticsWorkspace "
        "'Microsoft.OperationalInsights/workspaces@2023-09-01' existing"
    ) in source


def test_action_group_uses_only_parameterized_operator_email() -> None:
    source = _read(TEMPLATE)
    parameters = _read(PARAMETERS)

    assert "@secure()\nparam operatorEmail string" in source
    assert "emailAddress: operatorEmail" in source
    assert "useCommonAlertSchema: true" in source
    assert "readEnvironmentVariable('AZURE_ALERT_EMAIL')" in parameters
    assert "@" not in parameters
    for unapproved_receiver in (
        "smsReceivers",
        "voiceReceivers",
        "webhookReceivers",
        "automationRunbookReceivers",
    ):
        assert unapproved_receiver not in source


def test_replica_below_one_alert_is_critical_and_bounded() -> None:
    source = _read(TEMPLATE)
    block = _resource_block(source, "replicaBelowOneAlert", "replicaAboveOneAlert")

    for expected in (
        "metricName: 'Replicas'",
        "metricNamespace: 'Microsoft.App/containerApps'",
        "operator: 'LessThan'",
        "threshold: 1",
        "timeAggregation: 'Maximum'",
        "severity: 0",
        "evaluationFrequency: metricEvaluationFrequency",
        "windowSize: metricWindowSize",
        "actionGroupId: operatorActionGroup.id",
        "executionWorker.id",
    ):
        assert expected in block


def test_replica_above_one_alert_is_critical_singleton_guard() -> None:
    source = _read(TEMPLATE)
    block = _resource_block(source, "replicaAboveOneAlert", "restartAlert")

    for expected in (
        "metricName: 'Replicas'",
        "operator: 'GreaterThan'",
        "threshold: 1",
        "timeAggregation: 'Maximum'",
        "severity: 0",
        "actionGroupId: operatorActionGroup.id",
        "executionWorker.id",
    ):
        assert expected in block


def test_restart_alert_is_warning_on_supported_restart_metric() -> None:
    source = _read(TEMPLATE)
    block = _resource_block(source, "restartAlert", "unsafeRuntimeAlert")

    for expected in (
        "metricName: 'RestartCount'",
        "operator: 'GreaterThan'",
        "threshold: 0",
        "timeAggregation: 'Maximum'",
        "severity: 2",
        "actionGroupId: operatorActionGroup.id",
        "executionWorker.id",
    ):
        assert expected in block


def test_log_alert_uses_the_observed_structured_console_schema() -> None:
    source = _read(TEMPLATE)

    for expected in (
        "ContainerAppConsoleLogs",
        "ContainerAppName ==",
        "parse_json(Log)",
        "payload.event",
        "cloud_service_started",
        "payload.execution_mode",
        "payload.worker_enabled",
        "payload.commit_sha",
        "NO_EXECUTION",
        "expectedApplicationSourceSha",
        "evaluationFrequency: 'PT5M'",
        "skipQueryValidation: false",
        "severity: 0",
        "operator: 'GreaterThan'",
        "threshold: 0",
        "logAnalyticsWorkspace.id",
        "operatorActionGroup.id",
    ):
        assert expected in source
    assert "ContainerAppName_s" not in source
    assert "Log_s" not in source


def test_ops_stage_has_no_broker_or_execution_authority_surface() -> None:
    source = _read(TEMPLATE)
    parameters = _read(PARAMETERS)
    combined = source + parameters

    assert "executionAuthority: 'none'" in source
    assert "NO_EXECUTION" in source
    for forbidden in (
        "IG_ACCOUNT_ID",
        "IG_API_KEY",
        "IG_IDENTIFIER",
        "IG_PASSWORD",
        "SHADOW_DEMO",
        "LIVE",
        "secretRef",
        "keyVaultUrl",
    ):
        assert forbidden not in combined


def test_ci_compiles_ops_stage_and_preserves_dedicated_junit_evidence() -> None:
    workflow = _read(ROOT / ".github/workflows/ci.yaml")
    evidence = _read(ROOT / "tools/g4a_ci_evidence.py")

    assert "tests/test_g4b_ops_monitoring.py" in workflow
    assert "tests-g4b-ops.xml" in workflow
    assert "az bicep lint --file infra/azure/dev-shadow-ops.bicep" in workflow
    assert "az bicep build --file infra/azure/dev-shadow-ops.bicep" in workflow
    assert "--file infra/azure/dev-shadow-ops.parameters.bicepparam" in workflow
    assert '"g4b_ops": _junit(directory / "tests-g4b-ops.xml")' in evidence


def test_restart_runbook_is_documentation_only_and_preserves_safety() -> None:
    runbook = _read(RUNBOOK)

    for expected in (
        "DO NOT EXECUTE DURING G4B-02A1",
        "az containerapp revision restart",
        "exactly one replica",
        "EXECUTION_MODE=NO_EXECUTION",
        "restart alert",
        "network_call_count = 0",
        "order_endpoint_call_count = 0",
        "Do not retry the restart",
    ):
        assert expected in runbook
