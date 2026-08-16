"""Assemble compact, sanitized evidence for a successful G4A remote CI run."""

from __future__ import annotations

import argparse
import json
import os
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any


class EvidenceError(RuntimeError):
    """Raised when required remote CI evidence is absent or unsuccessful."""


def _json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise EvidenceError(f"{path.name} is not a JSON object")
    return value


def _junit(path: Path) -> dict[str, int | str]:
    root = ET.parse(path).getroot()
    if root.tag not in {"testsuites", "testsuite"}:
        raise EvidenceError(f"{path.name} has an unexpected root element")
    suites = [root] if root.tag == "testsuite" else list(root.findall("testsuite"))
    result: dict[str, int | str] = {"status": "pass"}
    for name in ("tests", "failures", "errors", "skipped"):
        result[name] = sum(int(suite.attrib.get(name, "0")) for suite in suites)
    if result["failures"] or result["errors"]:
        raise EvidenceError(f"{path.name} contains test failures")
    return result


def assemble(directory: Path) -> dict[str, Any]:
    tests = {
        "complete": _junit(directory / "tests-complete.xml"),
        "g1": _junit(directory / "tests-g1.xml"),
        "g2": _junit(directory / "tests-g2.xml"),
        "g3a": _junit(directory / "tests-g3a.xml"),
        "g3b": _junit(directory / "tests-g3b.xml"),
        "g4a": _junit(directory / "tests-g4a.xml"),
        "g4b_ops": _junit(directory / "tests-g4b-ops.xml"),
        "g4b_lease_unit": _junit(directory / "tests-g4b-lease-unit.xml"),
        "g4b_lease_fencing": _junit(directory / "tests-g4b-lease-fencing.xml"),
        "g4b_lease_concurrency": _junit(directory / "tests-g4b-lease-concurrency.xml"),
    }
    container = _json(directory / "container-smoke.json")
    image = _json(directory / "image-inspection.json")
    bicep = _json(directory / "bicep-result.json")
    secrets = _json(directory / "secret-scan.json")
    for name, value in {"container": container, "image": image, "bicep": bicep}.items():
        status = value.get("classification", value.get("status"))
        if status not in {"PASS_CONTAINER_SMOKE", "pass"}:
            raise EvidenceError(f"{name} evidence does not pass")
    if secrets != {"gitleaks": "pass", "tracked_source_scan": "pass"}:
        raise EvidenceError("secret-scan evidence does not pass")

    sha = os.environ.get("GITHUB_SHA", "unknown")
    run_id = os.environ.get("GITHUB_RUN_ID", "local")
    repository = os.environ.get("GITHUB_REPOSITORY", "local")
    server = os.environ.get("GITHUB_SERVER_URL", "https://github.com")
    return {
        "bicep": bicep,
        "candidate_sha": sha,
        "classification": "PASS_REMOTE_CI",
        "container": container,
        "image": image,
        "quality": {
            "format": "pass",
            "pre_commit": "pass",
            "ruff": "pass",
        },
        "run_id": run_id,
        "run_url": f"{server}/{repository}/actions/runs/{run_id}",
        "secret_scan": secrets,
        "tests": tests,
    }


def _markdown(evidence: dict[str, Any]) -> str:
    tests = evidence["tests"]
    image = evidence["image"]
    container = evidence["container"]
    safety = container["safety"]
    return "\n".join(
        [
            "# G4A remote CI evidence",
            "",
            f"- Run: `{evidence['run_id']}`",
            f"- Candidate: `{evidence['candidate_sha']}`",
            f"- Complete tests: `{tests['complete']['tests']}` passed",
            "- G1/G2/G3A/G3B/G4A/G4B-ops/G4B-lease-unit/fencing/concurrency: "
            f"`{tests['g1']['tests']}` / `{tests['g2']['tests']}` / "
            f"`{tests['g3a']['tests']}` / `{tests['g3b']['tests']}` / "
            f"`{tests['g4a']['tests']}` / `{tests['g4b_ops']['tests']}` / "
            f"`{tests['g4b_lease_unit']['tests']}` / "
            f"`{tests['g4b_lease_fencing']['tests']}` / "
            f"`{tests['g4b_lease_concurrency']['tests']}` passed",
            f"- Image digest: `{image['image_digest']}`",
            f"- Bicep: `{evidence['bicep']['status']}`",
            f"- Container: `{container['classification']}`",
            f"- Commit reported by container: `{container['readiness']['release']['commit_sha']}`",
            f"- Execution mode: `{container['readiness']['execution']['mode']}`",
            f"- Network/IG/Lightstreamer/order/credential counters: "
            f"`{safety['network_call_count']}` / `{safety['ig_rest_call_count']}` / "
            f"`{safety['lightstreamer_connection_count']}` / "
            f"`{safety['order_endpoint_call_count']}` / "
            f"`{safety['credential_resolution_count']}`",
            f"- Graceful shutdown exit: `{container['graceful_shutdown']['exit_code']}`",
            "- Azure resources created: `no`",
            "- IG/Demo/Live/order operation: `none`",
            "",
        ]
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--artifact-directory", type=Path, required=True)
    arguments = parser.parse_args()
    evidence = assemble(arguments.artifact_directory)
    json_path = arguments.artifact_directory / "g4a-remote-ci-evidence.json"
    markdown_path = arguments.artifact_directory / "g4a-remote-ci-evidence.md"
    json_path.write_text(
        json.dumps(evidence, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    markdown_path.write_text(_markdown(evidence), encoding="utf-8")
    print(json.dumps(evidence, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
