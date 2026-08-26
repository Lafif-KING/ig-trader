"""Local-process bridge for Demo Operator controls.

The Streamlit process never authenticates to IG and never instantiates a broker
transport.  It can only invoke the local controller CLI after the explicit
local-only environment gate passes.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STORE_PATH = ROOT / ".runtime" / "demo_operator" / "demo_execution.sqlite"
_ALLOWED_COMMANDS = frozenset({"start", "pause", "resume", "stop", "kill", "flatten"})


def controls_enabled() -> bool:
    return (
        os.environ.get("DEMO_OPERATOR_LOCAL", "").casefold() == "true"
        and os.environ.get("DASHBOARD_HOSTED", "").casefold() != "true"
    )


def invoke_local_controller(command: str) -> str:
    """Run one fixed local CLI command and return only sanitized status text."""

    if not controls_enabled():
        return "Local Demo controls are disabled."
    if command not in _ALLOWED_COMMANDS:
        return "Requested Demo action is unavailable."
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "src.ig_trader.demo_operator",
            command,
            "--store",
            str(STORE_PATH),
        ],
        cwd=ROOT,
        capture_output=True,
        check=False,
        text=True,
        timeout=30,
    )
    if completed.returncode != 0:
        return "Demo controller rejected the request or safe-stopped. Check Operator Alerts."
    try:
        document = json.loads(completed.stdout)
    except json.JSONDecodeError:
        return "Demo controller returned no safe status."
    if isinstance(document, dict) and isinstance(document.get("message"), str):
        return document["message"]
    if isinstance(document, dict) and isinstance(document.get("closed_positions"), int):
        return (
            f"Closed and reconciled {document['closed_positions']} locally owned Demo position(s)."
        )
    return "Demo controller completed the requested local action."
