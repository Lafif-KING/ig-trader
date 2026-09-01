"""Local-process bridge for the Shadow Tournament observer only.

The bridge accepts two fixed commands: an isolated Shadow observer monitor and
its Shadow-only stop request.  It never imports or calls the Demo controller,
broker client, trading bot, or order pipeline.
"""

from __future__ import annotations

import contextlib
import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from dashboard.sources.shadow_tournament import (
    DEFAULT_DATABASE_PATH,
    ShadowTournamentDashboard,
)

ROOT = Path(__file__).resolve().parents[1]
PID_RECORD_PATH = ROOT / "runtime" / "shadow_tournament_monitor.json"
_ALLOWED_COMMANDS = frozenset({"start", "stop"})


@dataclass(frozen=True)
class ShadowTournamentControls:
    """The separately derived, fail-closed local process-control state."""

    start_enabled: bool
    stop_enabled: bool
    monitor_running: bool
    reason: str


def local_controls_enabled() -> bool:
    """Require an explicit local-only environment gate for observer control."""

    return (
        os.environ.get("SHADOW_TOURNAMENT_LOCAL", "").casefold() == "true"
        and os.environ.get("DASHBOARD_HOSTED", "").casefold() != "true"
    )


def controls_for(
    dashboard: ShadowTournamentDashboard,
    *,
    pid_record_path: Path = PID_RECORD_PATH,
) -> ShadowTournamentControls:
    """Compute command availability without reading or writing the tournament store."""

    running = _monitor_running(pid_record_path)
    if not local_controls_enabled():
        return ShadowTournamentControls(False, False, running, "SHADOW01_LOCAL_CONTROL_DISABLED")
    if dashboard.execution_authority != "OFF":
        return ShadowTournamentControls(False, running, running, "SHADOW01_AUTHORITY_NOT_OFF")
    if running:
        return ShadowTournamentControls(False, True, True, "SHADOW01_MONITOR_RUNNING")
    if not dashboard.available:
        return ShadowTournamentControls(False, False, False, dashboard.reason)
    if not dashboard.epoch_created:
        return ShadowTournamentControls(False, False, False, "SHADOW01_EPOCH_NOT_CREATED")
    return ShadowTournamentControls(True, False, False, "SHADOW01_READY_TO_MONITOR")


def invoke_shadow_tournament_controller(
    command: str,
    dashboard: ShadowTournamentDashboard,
    *,
    database_path: Path = DEFAULT_DATABASE_PATH,
    pid_record_path: Path = PID_RECORD_PATH,
) -> str:
    """Invoke a fixed local Shadow-only command after fail-closed gating."""

    if command not in _ALLOWED_COMMANDS:
        return "SHADOW01_COMMAND_UNAVAILABLE"
    if database_path.resolve() != DEFAULT_DATABASE_PATH.resolve():
        return "SHADOW01_DATABASE_PATH_REJECTED"
    controls = controls_for(dashboard, pid_record_path=pid_record_path)
    if command == "start":
        if not controls.start_enabled:
            return controls.reason
        return _start_monitor(database_path, pid_record_path)
    if not controls.stop_enabled:
        return controls.reason
    return _request_stop(database_path, pid_record_path)


def _start_monitor(database_path: Path, pid_record_path: Path) -> str:
    if not _reserve_record(pid_record_path):
        return "SHADOW01_MONITOR_ALREADY_RECORDED"
    command = _command("monitor", database_path)
    try:
        process = subprocess.Popen(
            command,
            cwd=ROOT,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            shell=False,
        )
    except OSError:
        _remove_record(pid_record_path)
        return "SHADOW01_MONITOR_START_FAILED"
    try:
        _write_record(pid_record_path, process.pid, database_path)
    except OSError:
        process.terminate()
        with contextlib.suppress(subprocess.TimeoutExpired):
            process.wait(timeout=5)
        _remove_record(pid_record_path)
        return "SHADOW01_MONITOR_RECORD_FAILED"
    return "SHADOW01_MONITOR_STARTED"


def _request_stop(database_path: Path, pid_record_path: Path) -> str:
    try:
        result = subprocess.run(
            _command("stop", database_path),
            cwd=ROOT,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            shell=False,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return "SHADOW01_MONITOR_STOP_REQUEST_FAILED"
    if result.returncode != 0:
        return "SHADOW01_MONITOR_STOP_REJECTED"
    if not _monitor_running(pid_record_path):
        _remove_record(pid_record_path)
    return "SHADOW01_MONITOR_STOP_REQUESTED"


def _command(action: str, database_path: Path) -> list[str]:
    command = [
        sys.executable,
        "-m",
        "src.ig_trader.shadow01",
        action,
        "--database",
        str(database_path),
    ]
    if action == "monitor":
        # The dashboard click is the operator's explicit local monitor action.
        # The CLI still refuses construction if the Demo-only read adapter's
        # own credential and endpoint gates are not satisfied.
        command.append("--use-local-demo-read-only")
    return command


def _reserve_record(path: Path) -> bool:
    if _monitor_running(path):
        return False
    _remove_record(path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except OSError:
        return False
    with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
        json.dump({"schema_version": "shadow01-monitor/1.0", "state": "STARTING"}, stream)
        stream.write("\n")
    return True


def _write_record(path: Path, pid: int, database_path: Path) -> None:
    document = {
        "schema_version": "shadow01-monitor/1.0",
        "pid": pid,
        "database_path": str(database_path.resolve()),
        "module": "src.ig_trader.shadow01",
        "command": "monitor",
    }
    path.write_text(json.dumps(document, sort_keys=True) + "\n", encoding="utf-8")


def _monitor_running(path: Path) -> bool:
    document = _record(path)
    if document is None:
        return False
    pid = document.get("pid")
    if not isinstance(pid, int) or pid <= 0:
        return False
    return _pid_running(pid)


def _record(path: Path) -> dict[str, object] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(value, dict):
        return None
    if value.get("schema_version") != "shadow01-monitor/1.0":
        return None
    if value.get("module") != "src.ig_trader.shadow01" or value.get("command") != "monitor":
        return None
    if value.get("database_path") != str(DEFAULT_DATABASE_PATH.resolve()):
        return None
    return value


def _pid_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _remove_record(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        return
