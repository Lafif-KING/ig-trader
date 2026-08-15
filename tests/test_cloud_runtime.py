"""Safety and lifecycle tests for the G4A NO_EXECUTION cloud runtime."""

from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from src.ig_trader.cloud_config import (
    NO_EXECUTION,
    CloudConfig,
    UnsafeCloudConfiguration,
)
from src.ig_trader.cloud_runtime import CloudService, ServiceState
from src.ig_trader.cloud_safety import CloudSafetyMetrics

ROOT = Path(__file__).resolve().parents[1]
COMMIT = "a" * 40


def _config(**overrides: object) -> CloudConfig:
    values: dict[str, object] = {
        "host": "127.0.0.1",
        "port": 0,
        "execution_mode": NO_EXECUTION,
        "commit_sha": COMMIT,
        "version": "0.1.0",
        "image_revision": "test-revision",
        "log_level": "INFO",
        "shutdown_grace_seconds": 2.0,
    }
    values.update(overrides)
    return CloudConfig(**values)  # type: ignore[arg-type]


def _safe_subprocess_environment() -> dict[str, str]:
    environment = os.environ.copy()
    for name in tuple(environment):
        if name.startswith("IG_") or name in {
            "CST",
            "EXECUTION_MODE",
            "PAPER_TRADING",
            "X_SECURITY_TOKEN",
            "X_SECURITY_TOKEN_VALUE",
        }:
            environment.pop(name, None)
    environment["PYTHONPATH"] = str(ROOT)
    return environment


def test_cloud_config_defaults_to_no_execution_without_credentials() -> None:
    config = CloudConfig.from_environment({})

    assert config.execution_mode == NO_EXECUTION
    assert config.port == 8080
    assert config.commit_sha == "unknown"


@pytest.mark.parametrize(
    "environment",
    [
        {"EXECUTION_MODE": "DEMO"},
        {"EXECUTION_MODE": "LIVE"},
        {"PAPER_TRADING": "false"},
        {"IG_DEMO": "false"},
        {"IG_API_KEY": "test-placeholder"},
        {"IG_PASSWORD": "test-placeholder"},
        {"APP_COMMIT_SHA": "not-a-sha"},
    ],
)
def test_cloud_config_rejects_trading_authority_and_ambiguous_identity(
    environment: dict[str, str],
) -> None:
    with pytest.raises(UnsafeCloudConfiguration):
        CloudConfig.from_environment(environment)


@pytest.mark.asyncio
async def test_health_readiness_metadata_and_graceful_drain() -> None:
    metrics = CloudSafetyMetrics()
    service = CloudService(_config(), metrics)
    await service.start()
    try:
        status, ready = await _request(service.port, "/health/ready")
        assert status == 200
        assert ready["status"] == "pass"
        assert ready["release"]["commit_sha"] == COMMIT
        assert ready["execution"] == {
            "mode": "NO_EXECUTION",
            "authorized": False,
            "worker_enabled": False,
            "worker_process_count": 1,
            "replica_policy": {"min_replicas": 1, "max_replicas": 1},
        }
        assert ready["safety"]["credentials_required"] is False
        assert ready["safety"]["network_call_count"] == 0
        assert ready["safety"]["order_endpoint_call_count"] == 0

        live_status, live = await _request(service.port, "/health/live")
        assert live_status == 200
        assert live["check"] == "liveness"

        service.request_shutdown("test")
        assert service.state is ServiceState.DRAINING
        draining_status, draining = service.health_document(readiness=True)
        assert draining_status == 503
        assert draining["status"] == "fail"
        live_status, _ = service.health_document(readiness=False)
        assert live_status == 200
    finally:
        await service.stop()
    assert service.state is ServiceState.STOPPED


async def _request(port: int, path: str) -> tuple[int, dict[str, object]]:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(f"GET {path} HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n".encode())
    await writer.drain()
    response = await reader.read()
    writer.close()
    await writer.wait_closed()
    headers, body = response.split(b"\r\n\r\n", 1)
    status = int(headers.split(b" ", 2)[1])
    return status, json.loads(body)


def test_process_guard_blocks_broker_import_and_outbound_connect() -> None:
    script = """
import json
import socket
from src.ig_trader.cloud_safety import CloudIsolationError, activate
metrics = activate('NO_EXECUTION')
try:
    __import__('src.ig_trader.execution', fromlist=['ExecutionEngine'])
except CloudIsolationError:
    pass
else:
    raise AssertionError('execution import was not blocked')
client = socket.socket()
client.setblocking(False)
try:
    client.connect(('192.0.2.1', 9))
except CloudIsolationError:
    pass
except BlockingIOError as error:
    raise AssertionError('outbound connection reached the socket layer') from error
else:
    raise AssertionError('outbound connection was not blocked')
finally:
    client.close()
print(json.dumps(metrics.document(), sort_keys=True))
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=ROOT,
        env=_safe_subprocess_environment(),
        text=True,
        capture_output=True,
        check=False,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr
    metrics = json.loads(result.stdout)
    assert metrics["blocked_order_import_attempt_count"] == 1
    assert metrics["blocked_network_attempt_count"] == 1
    assert metrics["network_call_count"] == 0
    assert metrics["ig_rest_call_count"] == 0
    assert metrics["order_endpoint_call_count"] == 0


def test_launcher_rejects_demo_before_server_or_network() -> None:
    environment = _safe_subprocess_environment()
    environment["EXECUTION_MODE"] = "DEMO"
    result = subprocess.run(
        [sys.executable, "-m", "src.ig_trader.cloud_runtime"],
        cwd=ROOT,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
        timeout=15,
    )
    assert result.returncode == 2
    assert "unsafe_configuration" in result.stderr
    assert "DEMO" not in result.stderr


def test_real_launcher_starts_without_credentials_and_stops_gracefully() -> None:
    port = _reserve_port()
    environment = _safe_subprocess_environment()
    environment.update(
        {
            "APP_COMMIT_SHA": COMMIT,
            "APP_HOST": "127.0.0.1",
            "APP_PORT": str(port),
            "EXECUTION_MODE": "NO_EXECUTION",
        }
    )
    creation_flags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
    process = subprocess.Popen(
        [sys.executable, "-m", "src.ig_trader.cloud_runtime"],
        cwd=ROOT,
        env=environment,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        creationflags=creation_flags,
    )
    try:
        health = _wait_for_real_health(port, process)
        assert health["release"]["commit_sha"] == COMMIT
        assert health["execution"]["mode"] == "NO_EXECUTION"
        assert health["safety"]["network_call_count"] == 0
        assert health["safety"]["order_endpoint_call_count"] == 0
        assert health["safety"]["credentials_required"] is False
        assert health["safety"]["broker_modules_loaded"] is False

        if os.name == "nt":
            process.send_signal(signal.CTRL_BREAK_EVENT)
        else:
            process.terminate()
        stdout, stderr = process.communicate(timeout=10)
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=5)

    assert process.returncode == 0, stderr
    events = [json.loads(line) for line in stdout.splitlines()]
    event_names = {event["event"] for event in events}
    assert "cloud_service_started" in event_names
    assert "cloud_shutdown_requested" in event_names
    assert "cloud_service_stopped" in event_names
    stopped = next(event for event in events if event["event"] == "cloud_service_stopped")
    assert stopped["safety"]["network_call_count"] == 0
    assert stopped["safety"]["order_endpoint_call_count"] == 0
    assert stopped["safety"]["broker_modules_loaded"] is False


def _reserve_port() -> int:
    import socket

    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _wait_for_real_health(
    port: int,
    process: subprocess.Popen[str],
) -> dict[str, object]:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if process.poll() is not None:
            stdout, stderr = process.communicate()
            raise AssertionError(f"cloud process exited early: {stdout} {stderr}")
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/health/ready",
                timeout=1,
            ) as response:
                return json.loads(response.read())
        except (OSError, urllib.error.URLError):
            time.sleep(0.1)
    raise AssertionError("cloud process did not become ready")
