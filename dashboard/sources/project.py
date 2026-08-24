"""Loading and minimal validation for reviewed project gate data."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import SchemaError

from dashboard.models import ProjectGate, ProjectStatus, ProjectSummary

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_GATES_PATH = ROOT / "project" / "gates.json"
DEFAULT_SCHEMA_PATH = ROOT / "project" / "gates.schema.json"
GATE_REQUIRED_FIELDS = {
    "gate_id",
    "display_name",
    "group",
    "plain_english_description",
    "governance_status",
    "technical_status",
    "weight",
    "owner",
    "completed_sha",
    "related_pr",
    "blocker",
    "next_action",
    "evidence",
    "last_verified_at",
}
SUMMARY_REQUIRED_FIELDS = {
    "current_phase_gate_id",
    "current_phase",
    "current_status",
    "current_blocker",
    "next_action",
    "execution_mode",
    "broker_order_authority",
    "demo_execution",
    "live_execution",
    "real_database_state",
    "real_database_governance",
    "last_verified_at",
}
ALLOWED_STATUSES = {
    "PASS",
    "PASS_WITH_KNOWN_GAP",
    "ENGINEERING_CLOSED",
    "IN_PROGRESS",
    "HOLD",
    "BLOCKED",
    "NOT_STARTED",
    "DISABLED",
    "NOT_AUTHORIZED",
    "UNKNOWN",
}
ALLOWED_GROUPS = {"FOUNDATION", "SHADOW", "DEMO", "LIVE", "RESEARCH", "ENHANCEMENTS"}
ALLOWED_EXECUTION_MODES = {"NO_EXECUTION", "SHADOW_DEMO", "DEMO_EXECUTION", "LIVE_EXECUTION"}
ALLOWED_BROKER_AUTHORITIES = {"OFF"}
ALLOWED_DATABASE_GOVERNANCE = {"RECOVERY_HOLD"}


class ProjectGateValidationError(ValueError):
    """The committed roadmap cannot safely be rendered as a reviewed gate set."""


def _parse_gate(item: Any) -> ProjectGate:
    if not isinstance(item, dict):
        raise ProjectGateValidationError("Each gate must be a JSON object.")
    missing = GATE_REQUIRED_FIELDS.difference(item)
    if missing:
        raise ProjectGateValidationError(f"Gate is missing required fields: {sorted(missing)}")
    if not isinstance(item["weight"], int) or item["weight"] < 0:
        raise ProjectGateValidationError("Gate weight must be a non-negative integer.")
    if item["group"] not in ALLOWED_GROUPS:
        raise ProjectGateValidationError("Gate group is not defined by the reviewed schema.")
    if (
        item["governance_status"] not in ALLOWED_STATUSES
        or item["technical_status"] not in ALLOWED_STATUSES
    ):
        raise ProjectGateValidationError("Gate status is not defined by the reviewed schema.")
    if not isinstance(item["evidence"], list) or not all(
        isinstance(value, str) for value in item["evidence"]
    ):
        raise ProjectGateValidationError("Gate evidence must be a list of safe text strings.")
    try:
        verified_at = datetime.fromisoformat(item["last_verified_at"].replace("Z", "+00:00"))
    except (AttributeError, TypeError, ValueError) as error:
        raise ProjectGateValidationError(
            "last_verified_at must be an ISO-8601 timestamp."
        ) from error
    return ProjectGate(
        gate_id=item["gate_id"],
        display_name=item["display_name"],
        group=item["group"],
        plain_english_description=item["plain_english_description"],
        governance_status=item["governance_status"],
        technical_status=item["technical_status"],
        weight=item["weight"],
        owner=item["owner"],
        completed_sha=item["completed_sha"],
        related_pr=item["related_pr"],
        blocker=item["blocker"],
        next_action=item["next_action"],
        evidence=tuple(item["evidence"]),
        last_verified_at=verified_at,
    )


def _parse_summary(item: Any) -> ProjectSummary:
    if not isinstance(item, dict):
        raise ProjectGateValidationError("Project summary must be a JSON object.")
    missing = SUMMARY_REQUIRED_FIELDS.difference(item)
    if missing:
        raise ProjectGateValidationError(
            f"Project summary is missing required fields: {sorted(missing)}"
        )
    text_fields = SUMMARY_REQUIRED_FIELDS.difference({"last_verified_at"})
    if not all(isinstance(item[field], str) and item[field] for field in text_fields):
        raise ProjectGateValidationError("Project summary fields must be non-empty text.")
    if item["current_status"] not in ALLOWED_STATUSES:
        raise ProjectGateValidationError(
            "Project summary status is not defined by the reviewed schema."
        )
    if item["execution_mode"] not in ALLOWED_EXECUTION_MODES:
        raise ProjectGateValidationError("Project summary execution mode is not allowed.")
    if item["broker_order_authority"] not in ALLOWED_BROKER_AUTHORITIES:
        raise ProjectGateValidationError("Project summary broker authority is not allowed.")
    if (
        item["demo_execution"] not in ALLOWED_STATUSES
        or item["live_execution"] not in ALLOWED_STATUSES
    ):
        raise ProjectGateValidationError("Project summary execution governance is not allowed.")
    if item["real_database_state"] not in ALLOWED_STATUSES:
        raise ProjectGateValidationError("Project summary database state is not allowed.")
    if item["real_database_governance"] not in ALLOWED_DATABASE_GOVERNANCE:
        raise ProjectGateValidationError("Project summary database governance is not allowed.")
    try:
        verified_at = datetime.fromisoformat(item["last_verified_at"].replace("Z", "+00:00"))
    except (AttributeError, TypeError, ValueError) as error:
        raise ProjectGateValidationError(
            "Project summary last_verified_at must be an ISO-8601 timestamp."
        ) from error
    return ProjectSummary(
        current_phase_gate_id=item["current_phase_gate_id"],
        current_phase=item["current_phase"],
        current_status=item["current_status"],
        current_blocker=item["current_blocker"],
        next_action=item["next_action"],
        execution_mode=item["execution_mode"],
        broker_order_authority=item["broker_order_authority"],
        demo_execution=item["demo_execution"],
        live_execution=item["live_execution"],
        real_database_state=item["real_database_state"],
        real_database_governance=item["real_database_governance"],
        last_verified_at=verified_at,
    )


def _load_schema_document(schema_path: Path = DEFAULT_SCHEMA_PATH) -> dict[str, Any]:
    """Load the committed Draft 2020-12 schema before validating gate data."""

    try:
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        Draft202012Validator.check_schema(schema)
    except (KeyError, OSError, SchemaError, TypeError, json.JSONDecodeError) as error:
        raise ProjectGateValidationError("Unable to read the project gate JSON schema.") from error
    if not isinstance(schema, dict):
        raise ProjectGateValidationError("Project gate JSON schema must be a JSON object.")
    return schema


def load_project_status(path: Path = DEFAULT_GATES_PATH) -> ProjectStatus:
    """Read reviewed summary and gates from disk without a network or database path."""

    schema = _load_schema_document(path.parent / "gates.schema.json")
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ProjectGateValidationError(
            "Unable to read the reviewed project gate file."
        ) from error
    if not isinstance(document, dict) or not isinstance(document.get("gates"), list):
        raise ProjectGateValidationError("Project gate file must contain a gates list.")
    errors = sorted(
        Draft202012Validator(schema, format_checker=FormatChecker()).iter_errors(document),
        key=lambda error: list(error.absolute_path),
    )
    if errors:
        raise ProjectGateValidationError("Project gate data does not match its JSON schema.")
    summary = _parse_summary(document.get("project_summary"))
    gates = tuple(_parse_gate(item) for item in document["gates"])
    if not gates:
        raise ProjectGateValidationError("Project gate file cannot be empty.")
    if len({gate.gate_id for gate in gates}) != len(gates):
        raise ProjectGateValidationError("Project gate IDs must be unique.")
    if summary.current_phase_gate_id not in {gate.gate_id for gate in gates}:
        raise ProjectGateValidationError("Project summary phase gate must exist in reviewed gates.")
    return ProjectStatus(summary=summary, gates=gates)


def load_project_gates(path: Path = DEFAULT_GATES_PATH) -> tuple[ProjectGate, ...]:
    """Backward-compatible gate accessor for calculations that need no summary."""

    return load_project_status(path).gates
