"""Verify the finite database-job image identity and deliberately small contents."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import tarfile
from pathlib import Path, PurePosixPath
from uuid import uuid4


class ImageInspectionError(RuntimeError):
    """Raised when the database-job image violates its reviewed contract."""


EXPECTED_PROJECT_FILES = {
    "app/migrations/postgresql/001_execution_state.sql",
    "app/migrations/postgresql/002_execution_lease_fencing.sql",
    "app/src/ig_trader/__init__.py",
    "app/src/ig_trader/db_bootstrap.py",
    "app/src/ig_trader/execution_lease.py",
}


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


def _application_files(image: str) -> dict[str, bytes]:
    name = f"ig-trader-db-bootstrap-inspect-{uuid4().hex[:12]}"
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
        files: dict[str, bytes] = {}
        with tarfile.open(fileobj=export.stdout, mode="r|") as archive:
            for member in archive:
                path = PurePosixPath(member.name).as_posix()
                if not member.isfile() or not path.startswith("app/"):
                    continue
                if path.startswith(("app/src/", "app/migrations/")):
                    extracted = archive.extractfile(member)
                    if extracted is None:
                        raise ImageInspectionError("image file could not be inspected")
                    files[path] = extracted.read()
        stderr = export.stderr.read().decode("utf-8", errors="replace") if export.stderr else ""
        if export.wait(timeout=120) != 0:
            raise ImageInspectionError(f"docker export failed: {stderr[:160]}")
        return files
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
        raise ImageInspectionError("runtime rootfs does not extend the pinned base image")

    config = image_document.get("Config")
    if not isinstance(config, dict):
        raise ImageInspectionError("image configuration is unavailable")
    expected_entrypoint = ["python", "-m", "ig_trader.db_bootstrap"]
    if config.get("User") != "10001:10001":
        raise ImageInspectionError("image runtime user is not the reviewed non-root user")
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
        "DATABASE_URL",
        "IG_ACCOUNT_ID",
        "IG_API_KEY",
        "IG_IDENTIFIER",
        "IG_PASSWORD",
        "PGPASSWORD",
        "POSTGRES_PASSWORD",
        "X_SECURITY_TOKEN",
    }
    if environment_names & forbidden_environment:
        raise ImageInspectionError("image contains a credential environment variable")

    files = _application_files(image)
    if set(files) != EXPECTED_PROJECT_FILES:
        raise ImageInspectionError("image project inputs differ from the reviewed finite set")
    if any(part in {".env", ".git"} for path in files for part in PurePosixPath(path).parts):
        raise ImageInspectionError("image contains environment or source-control metadata")
    forbidden_suffixes = {".csv", ".db", ".feather", ".parquet", ".sqlite"}
    if any(PurePosixPath(path).suffix.casefold() in forbidden_suffixes for path in files):
        raise ImageInspectionError("image contains a historical or database data package")

    migration_hashes = {
        PurePosixPath(path).name: hashlib.sha256(content).hexdigest()
        for path, content in files.items()
        if path.startswith("app/migrations/")
    }
    expected_hashes = {
        "001_execution_state.sql": (
            "42dcbe2b47c5fed8223a4831d8c594e78c3180f454b71e15358819a9039c8800"
        ),
        "002_execution_lease_fencing.sql": (
            "731b918b573ee232aab3fa709e7a41b5ac03e11f4f81d08458f8fcefcb16599c"
        ),
    }
    if migration_hashes != expected_hashes:
        raise ImageInspectionError("image migration hashes differ from review")

    image_id = image_document.get("Id")
    if not isinstance(image_id, str) or not image_id.startswith("sha256:"):
        raise ImageInspectionError("image content identity is unavailable")
    return {
        "base_image": expected_base,
        "base_layer_count": len(base_layers),
        "entrypoint": expected_entrypoint,
        "forbidden_credential_environment_present": False,
        "image_digest": image_id,
        "migration_hashes": migration_hashes,
        "networked_acceptance_test_required": False,
        "non_root_user": config["User"],
        "oci_labels": labels,
        "project_files": sorted(files),
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
