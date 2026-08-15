"""Verify the built G4A image identity, base layers, and application contents."""

from __future__ import annotations

import argparse
import json
import subprocess
import tarfile
from pathlib import Path, PurePosixPath
from uuid import uuid4


class ImageInspectionError(RuntimeError):
    """Raised when the image violates the G4A build contract."""


def _docker_json(*arguments: str) -> dict[str, object]:
    result = subprocess.run(
        ["docker", *arguments],
        check=False,
        capture_output=True,
        text=True,
        timeout=120,
    )
    if result.returncode != 0:
        raise ImageInspectionError(f"docker {arguments[0]} failed")
    values = json.loads(result.stdout)
    if not isinstance(values, list) or len(values) != 1 or not isinstance(values[0], dict):
        raise ImageInspectionError("docker inspection result was ambiguous")
    return values[0]


def _rootfs_layers(document: dict[str, object]) -> list[str]:
    rootfs = document.get("RootFS")
    if not isinstance(rootfs, dict) or not isinstance(rootfs.get("Layers"), list):
        raise ImageInspectionError("image rootfs layers are unavailable")
    return [str(value) for value in rootfs["Layers"]]


def _application_paths(image: str) -> list[str]:
    name = f"ig-trader-g4a-inspect-{uuid4().hex[:12]}"
    create = subprocess.run(
        ["docker", "create", "--name", name, image],
        check=False,
        capture_output=True,
        text=True,
        timeout=120,
    )
    if create.returncode != 0:
        raise ImageInspectionError("unable to create inspection container")
    try:
        export = subprocess.Popen(
            ["docker", "export", name],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        if export.stdout is None:
            raise ImageInspectionError("docker export stream is unavailable")
        paths: list[str] = []
        with tarfile.open(fileobj=export.stdout, mode="r|") as archive:
            for member in archive:
                path = PurePosixPath(member.name)
                if path.parts and path.parts[0] == "app":
                    paths.append(path.as_posix())
        stderr = export.stderr.read().decode("utf-8", errors="replace") if export.stderr else ""
        if export.wait(timeout=120) != 0:
            raise ImageInspectionError(f"docker export failed: {stderr[:160]}")
        return paths
    finally:
        subprocess.run(
            ["docker", "rm", "--force", name],
            check=False,
            capture_output=True,
            timeout=120,
        )


def inspect_image(
    image: str,
    expected_base: str,
    expected_commit: str,
) -> dict[str, object]:
    image_document = _docker_json("image", "inspect", image)
    base_document = _docker_json("image", "inspect", expected_base)
    image_layers = _rootfs_layers(image_document)
    base_layers = _rootfs_layers(base_document)
    if image_layers[: len(base_layers)] != base_layers:
        raise ImageInspectionError("runtime rootfs does not extend the expected base image")

    config = image_document.get("Config")
    if not isinstance(config, dict):
        raise ImageInspectionError("image configuration is unavailable")
    if config.get("User") != "10001:10001":
        raise ImageInspectionError("image runtime user is not the expected non-root user")
    expected_entrypoint = ["python", "-m", "src.ig_trader.cloud_runtime"]
    if config.get("Entrypoint") != expected_entrypoint:
        raise ImageInspectionError("image entrypoint is unexpected")

    labels = config.get("Labels")
    if (
        not isinstance(labels, dict)
        or labels.get("org.opencontainers.image.revision") != expected_commit
    ):
        raise ImageInspectionError("OCI revision label does not match the candidate")
    environment = config.get("Env")
    if not isinstance(environment, list):
        raise ImageInspectionError("image environment is unavailable")
    environment_names = {str(item).partition("=")[0] for item in environment}
    forbidden_environment = {
        "CST",
        "IG_ACCOUNT_ID",
        "IG_API_KEY",
        "IG_IDENTIFIER",
        "IG_PASSWORD",
        "X_SECURITY_TOKEN",
    }
    if environment_names & forbidden_environment:
        raise ImageInspectionError("image contains a broker credential environment variable")
    if "EXECUTION_MODE=NO_EXECUTION" not in environment:
        raise ImageInspectionError("image does not default to NO_EXECUTION")

    paths = _application_paths(image)
    parts = [PurePosixPath(path).parts for path in paths]
    if any(".git" in value or ".env" in value for value in parts):
        raise ImageInspectionError("image contains source-control or environment metadata")
    forbidden_project_suffixes = {".csv", ".db", ".feather", ".parquet", ".sqlite"}
    source_files = [PurePosixPath(path) for path in paths if path.startswith("app/src/")]
    if any(path.suffix.casefold() in forbidden_project_suffixes for path in source_files):
        raise ImageInspectionError("image contains a historical or database data package")
    application_roots = sorted({value[1] for value in parts if len(value) > 1})
    if application_roots != [".venv", "src"]:
        raise ImageInspectionError("image contains an unexpected application root")

    image_id = image_document.get("Id")
    if not isinstance(image_id, str) or not image_id.startswith("sha256:"):
        raise ImageInspectionError("image content identity is unavailable")
    return {
        "application_roots": application_roots,
        "base_image": expected_base,
        "base_layer_count": len(base_layers),
        "entrypoint": expected_entrypoint,
        "environment_file_present": False,
        "forbidden_credential_environment_present": False,
        "historical_bulk_data_present": False,
        "image_digest": image_id,
        "non_root_user": config["User"],
        "oci_labels": labels,
        "project_file_count": len(source_files),
        "project_secret_material_present": False,
        "source_control_metadata_present": False,
        "status": "pass",
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--image", required=True)
    parser.add_argument("--expected-base", required=True)
    parser.add_argument("--expected-commit", required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    arguments = parser.parse_args()
    evidence = inspect_image(
        arguments.image,
        arguments.expected_base,
        arguments.expected_commit,
    )
    arguments.evidence.parent.mkdir(parents=True, exist_ok=True)
    arguments.evidence.write_text(
        json.dumps(evidence, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(evidence, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
