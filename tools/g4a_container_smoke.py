"""Run the G4A safe-container acceptance test and write machine-readable evidence."""

from __future__ import annotations

import argparse
import json
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from uuid import uuid4


class ContainerSmokeError(RuntimeError):
    """Raised when the container violates a G4A acceptance condition."""


def _run(*arguments: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        list(arguments),
        text=True,
        capture_output=True,
        check=False,
        timeout=120,
    )
    if check and result.returncode != 0:
        raise ContainerSmokeError(f"command failed: {arguments[0]} {arguments[1]}")
    return result


def _inspect(name: str) -> dict[str, object]:
    output = _run("docker", "inspect", name).stdout
    documents = json.loads(output)
    if len(documents) != 1:
        raise ContainerSmokeError("container inspection was ambiguous")
    return documents[0]


def _mapped_port(name: str) -> int:
    inspection = _inspect(name)
    try:
        bindings = inspection["NetworkSettings"]["Ports"]["8080/tcp"]  # type: ignore[index]
        return int(bindings[0]["HostPort"])
    except (KeyError, IndexError, TypeError, ValueError) as error:
        raise ContainerSmokeError("health port mapping is unavailable") from error


def _health(port: int, path: str) -> dict[str, object]:
    with urllib.request.urlopen(
        f"http://127.0.0.1:{port}{path}",
        timeout=2,
    ) as response:
        if response.status != 200:
            raise ContainerSmokeError(f"{path} returned {response.status}")
        return json.loads(response.read())


def _wait_for_health(port: int) -> tuple[dict[str, object], dict[str, object]]:
    deadline = time.monotonic() + 30
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            return _health(port, "/health/live"), _health(port, "/health/ready")
        except (OSError, urllib.error.URLError, ContainerSmokeError) as error:
            last_error = error
            time.sleep(0.25)
    raise ContainerSmokeError("container did not become healthy") from last_error


def _assert_health(document: dict[str, object], expected_commit: str) -> None:
    release = document.get("release")
    execution = document.get("execution")
    safety = document.get("safety")
    if not isinstance(release, dict) or release.get("commit_sha") != expected_commit:
        raise ContainerSmokeError("health commit identity does not match the image build")
    if not isinstance(execution, dict) or execution.get("mode") != "NO_EXECUTION":
        raise ContainerSmokeError("container did not default to NO_EXECUTION")
    if execution.get("authorized") is not False or execution.get("worker_enabled") is not False:
        raise ContainerSmokeError("execution unexpectedly has authority")
    if execution.get("replica_policy") != {"min_replicas": 1, "max_replicas": 1}:
        raise ContainerSmokeError("singleton replica policy is missing")
    if not isinstance(safety, dict):
        raise ContainerSmokeError("safety metadata is missing")
    for name in (
        "network_call_count",
        "ig_rest_call_count",
        "lightstreamer_connection_count",
        "order_endpoint_call_count",
        "credential_resolution_count",
        "blocked_network_attempt_count",
    ):
        if safety.get(name) != 0:
            raise ContainerSmokeError(f"nonzero safety counter: {name}")
    if safety.get("credentials_required") is not False:
        raise ContainerSmokeError("safe startup unexpectedly requires credentials")
    if safety.get("broker_modules_loaded") is not False:
        raise ContainerSmokeError("broker module loaded in safe container")


def run(image: str, expected_commit: str, evidence_path: Path) -> None:
    name = f"ig-trader-g4a-{uuid4().hex[:12]}"
    started = False
    try:
        result = _run(
            "docker",
            "run",
            "--detach",
            "--name",
            name,
            "--label",
            "ig-trader.g4a-smoke=true",
            "--publish",
            "127.0.0.1::8080",
            image,
        )
        if not result.stdout.strip():
            raise ContainerSmokeError("docker did not return a container identity")
        started = True
        port = _mapped_port(name)
        liveness, readiness = _wait_for_health(port)
        _assert_health(liveness, expected_commit)
        _assert_health(readiness, expected_commit)

        stop_result = _run("docker", "stop", "--time", "15", name, check=False)
        if stop_result.returncode != 0:
            raise ContainerSmokeError("container did not accept graceful stop")
        inspection = _inspect(name)
        state = inspection.get("State")
        if not isinstance(state, dict) or state.get("ExitCode") != 0:
            raise ContainerSmokeError("container did not exit cleanly after SIGTERM")
        logs = _run("docker", "logs", name).stdout.splitlines()
        events: list[dict[str, object]] = []
        for line in logs:
            try:
                value = json.loads(line)
            except json.JSONDecodeError as error:
                raise ContainerSmokeError("container emitted a non-JSON log line") from error
            if not isinstance(value, dict):
                raise ContainerSmokeError("container emitted a non-object JSON log")
            events.append(value)
        event_names = {event.get("event") for event in events}
        if not {"cloud_shutdown_requested", "cloud_service_stopped"} <= event_names:
            raise ContainerSmokeError("graceful shutdown events are missing")

        evidence = {
            "classification": "PASS_CONTAINER_SMOKE",
            "image": image,
            "expected_commit_sha": expected_commit,
            "liveness": liveness,
            "readiness": readiness,
            "graceful_shutdown": {
                "exit_code": 0,
                "shutdown_requested_logged": True,
                "service_stopped_logged": True,
            },
            "order_endpoint_call_count": readiness["safety"][  # type: ignore[index]
                "order_endpoint_call_count"
            ],
        }
        evidence_path.parent.mkdir(parents=True, exist_ok=True)
        evidence_path.write_text(
            json.dumps(evidence, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        print(json.dumps(evidence, sort_keys=True))
    finally:
        if started:
            inspection_result = _run("docker", "inspect", name, check=False)
            if inspection_result.returncode == 0:
                inspection = json.loads(inspection_result.stdout)[0]
                labels = inspection.get("Config", {}).get("Labels", {})
                if labels.get("ig-trader.g4a-smoke") == "true":
                    _run("docker", "rm", "--force", name, check=False)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--image", required=True)
    parser.add_argument("--expected-commit", required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    arguments = parser.parse_args()
    run(arguments.image, arguments.expected_commit, arguments.evidence)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
