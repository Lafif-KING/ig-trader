"""Loading and minimal validation for reviewed project gate data."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from dashboard.models import ProjectGate

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_GATES_PATH = ROOT / "project" / "gates.json"
DEFAULT_SCHEMA_PATH = ROOT / "project" / "gates.schema.json"
REQUIRED_FIELDS = {
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
ALLOWED_GROUPS = {"FOUNDATION", "SHADOW", "DEMO", "LIVE", "ENHANCEMENTS"}


class ProjectGateValidationError(ValueError):
    """The committed roadmap cannot safely be rendered as a reviewed gate set."""


def _parse_gate(item: Any) -> ProjectGate:
    if not isinstance(item, dict):
        raise ProjectGateValidationError("Each gate must be a JSON object.")
    missing = REQUIRED_FIELDS.difference(item)
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


def _validate_schema_document(schema_path: Path = DEFAULT_SCHEMA_PATH) -> None:
    """Verify that the committed schema retains the dashboard's reviewed contract."""

    try:
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        required = set(schema["properties"]["gates"]["items"]["required"])
    except (KeyError, OSError, TypeError, json.JSONDecodeError) as error:
        raise ProjectGateValidationError("Unable to read the project gate JSON schema.") from error
    if required != REQUIRED_FIELDS:
        raise ProjectGateValidationError(
            "Project gate JSON schema does not match the reviewed contract."
        )


def load_project_gates(path: Path = DEFAULT_GATES_PATH) -> tuple[ProjectGate, ...]:
    """Read reviewed data from disk; this source has no network or database path."""

    _validate_schema_document(path.parent / "gates.schema.json")
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ProjectGateValidationError(
            "Unable to read the reviewed project gate file."
        ) from error
    if not isinstance(document, dict) or not isinstance(document.get("gates"), list):
        raise ProjectGateValidationError("Project gate file must contain a gates list.")
    gates = tuple(_parse_gate(item) for item in document["gates"])
    if not gates:
        raise ProjectGateValidationError("Project gate file cannot be empty.")
    if len({gate.gate_id for gate in gates}) != len(gates):
        raise ProjectGateValidationError("Project gate IDs must be unique.")
    return gates
