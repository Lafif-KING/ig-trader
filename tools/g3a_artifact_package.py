"""Create and verify immutable, content-addressed external G3A artifact packages."""

from __future__ import annotations

import argparse
import os
import shutil
import stat
from collections.abc import Mapping, Sequence
from pathlib import Path

from src.ig_trader.g3a_data import fingerprint, sha256_bytes
from tools.g3a_market_data import write_json_create_only

PACKAGE_SCHEMA_VERSION = "g3a-external-artifact-package/1.0.0"


def create_package(
    package_root: Path,
    *,
    artifact_id: str,
    sources: Mapping[str, Path],
) -> dict[str, object]:
    """Copy source trees create-only, validate all hashes, then publish atomically."""

    if package_root.exists() or not artifact_id or not sources:
        raise ValueError("package target must be new and package identity must be complete")
    building = package_root.with_name(package_root.name + ".building")
    if building.exists():
        raise ValueError("package build directory already exists")
    payload_root = building / "payload"
    for label, source in sorted(sources.items()):
        if not _safe_label(label) or not source.is_dir():
            raise ValueError("package source is invalid")
        destination_root = payload_root / label
        for source_file in sorted(path for path in source.rglob("*") if path.is_file()):
            if source_file.is_symlink():
                raise ValueError("package sources cannot contain symlinks")
            relative = source_file.relative_to(source)
            destination = destination_root / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            with source_file.open("rb") as input_stream, destination.open("xb") as output_stream:
                shutil.copyfileobj(input_stream, output_stream)
                output_stream.flush()
                os.fsync(output_stream.fileno())
    entries = _payload_entries(building)
    manifest: dict[str, object] = {
        "schema_version": PACKAGE_SCHEMA_VERSION,
        "artifact_id": artifact_id,
        "file_count": len(entries),
        "total_size_bytes": sum(int(item["size_bytes"]) for item in entries),
        "files": entries,
    }
    manifest["package_fingerprint"] = package_fingerprint(manifest)
    write_json_create_only(building / "package-manifest.json", manifest)
    verification = verify_package(building)
    if verification["status"] != "PASS":
        raise ValueError("package verification failed before publication")
    _make_read_only(building)
    building.replace(package_root)
    return manifest


def verify_package(package_root: Path) -> dict[str, object]:
    manifest_path = package_root / "package-manifest.json"
    if not manifest_path.is_file():
        return {"status": "FAIL", "reason": "MANIFEST_MISSING"}
    import json

    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"status": "FAIL", "reason": "MANIFEST_INVALID"}
    if not isinstance(manifest, dict) or manifest.get("schema_version") != PACKAGE_SCHEMA_VERSION:
        return {"status": "FAIL", "reason": "MANIFEST_SCHEMA_INVALID"}
    expected_files = manifest.get("files")
    if not isinstance(expected_files, list):
        return {"status": "FAIL", "reason": "MANIFEST_FILE_LIST_INVALID"}
    actual = _payload_entries(package_root)
    if actual != expected_files:
        return {
            "status": "FAIL",
            "reason": "PAYLOAD_HASH_OR_FILE_SET_MISMATCH",
            "actual_file_count": len(actual),
        }
    expected_fingerprint = manifest.get("package_fingerprint")
    if expected_fingerprint != package_fingerprint(manifest):
        return {"status": "FAIL", "reason": "PACKAGE_FINGERPRINT_MISMATCH"}
    return {
        "status": "PASS",
        "artifact_id": manifest.get("artifact_id"),
        "file_count": len(actual),
        "total_size_bytes": sum(int(item["size_bytes"]) for item in actual),
        "package_fingerprint": expected_fingerprint,
        "manifest_sha256": sha256_bytes(manifest_path.read_bytes()),
    }


def package_fingerprint(manifest: Mapping[str, object]) -> str:
    return fingerprint(
        {
            "schema_version": manifest.get("schema_version"),
            "artifact_id": manifest.get("artifact_id"),
            "file_count": manifest.get("file_count"),
            "total_size_bytes": manifest.get("total_size_bytes"),
            "files": manifest.get("files"),
        }
    )


def _payload_entries(package_root: Path) -> list[dict[str, object]]:
    payload = package_root / "payload"
    if not payload.is_dir():
        return []
    return [
        {
            "relative_path": path.relative_to(package_root).as_posix(),
            "size_bytes": path.stat().st_size,
            "sha256": sha256_bytes(path.read_bytes()),
        }
        for path in sorted(item for item in payload.rglob("*") if item.is_file())
    ]


def _make_read_only(package_root: Path) -> None:
    for path in sorted((item for item in package_root.rglob("*") if item.is_file()), reverse=True):
        path.chmod(stat.S_IREAD)


def _safe_label(value: str) -> bool:
    return bool(value) and all(character.isalnum() or character in "_.-" for character in value)


def _parse_sources(values: Sequence[str]) -> dict[str, Path]:
    result = {}
    for value in values:
        label, separator, raw_path = value.partition("=")
        if not separator or not _safe_label(label) or label in result or not raw_path:
            raise argparse.ArgumentTypeError("source must be a unique LABEL=PATH")
        result[label] = Path(raw_path)
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="G3A external artifact package manager")
    subparsers = parser.add_subparsers(dest="command", required=True)
    create = subparsers.add_parser("create")
    create.add_argument("--package-root", type=Path, required=True)
    create.add_argument("--artifact-id", required=True)
    create.add_argument("--source", action="append", required=True)
    verify = subparsers.add_parser("verify")
    verify.add_argument("--package-root", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "create":
        result = create_package(
            args.package_root,
            artifact_id=args.artifact_id,
            sources=_parse_sources(args.source),
        )
        print("G3A_ARTIFACT_PACKAGE_CREATED")
        print(f"file_count={result['file_count']}")
        print(f"package_fingerprint={result['package_fingerprint']}")
        return 0
    result = verify_package(args.package_root)
    print("G3A_ARTIFACT_PACKAGE_VERIFICATION")
    for key, value in result.items():
        print(f"{key}={value}")
    return 0 if result["status"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
