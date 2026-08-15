"""Immutable G3A package verification and point-in-time replay data access."""

from __future__ import annotations

import json
import stat
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from src.ig_trader.g3a_data import (
    RESOLUTION_MINUTES,
    CanonicalCandle,
    canonical_candle_from_document,
    fingerprint,
    sha256_bytes,
)

PACKAGE_SCHEMA_VERSION = "g3a-external-artifact-package/1.0.0"
EXPECTED_ARTIFACT_ID = "g3a-02-20260815"
EXPECTED_PACKAGE_FINGERPRINT = "61442f9cf91260ed32098767a206ef45e9d45ea3dc17c93572cba3111eff3780"
EXPECTED_MANIFEST_SHA256 = "c6469e45f743204dafd05361e0522c965398529466015568dad9fabd8a98b6d4"
EXPECTED_DATASET_FINGERPRINT = "36fc24a739a7db238d96131be1d6c42a48895eeca1228697771b1736293ecbea"
FINAL_RUN_ID = "g3a-01-20260815-v4"
FINAL_CANONICAL_SCHEMA_VERSION = "g3a-canonical-candle/1.0.0"
FINAL_NORMALIZATION_VERSION = "g3a-normalizer/1.0.0"
AUTHORITATIVE_GAP_EPIC = "CS.D.EURGBP.MINI.IP"
AUTHORITATIVE_GAP_RESOLUTION = "MINUTE"
AUTHORITATIVE_GAP_TIMESTAMP = datetime(2026, 8, 14, 19, 3, tzinfo=UTC)

FROZEN_REPLAY_INSTRUMENTS = (
    ("EURGBP", "EUR/GBP Mini", "CS.D.EURGBP.MINI.IP"),
    ("EURUSD", "EUR/USD Mini", "CS.D.EURUSD.CEFM.IP"),
    ("GBPUSD", "GBP/USD Mini", "CS.D.GBPUSD.MINI.IP"),
)
FROZEN_RESOLUTIONS = ("HOUR", "MINUTE_15", "MINUTE_5", "MINUTE")
EXPECTED_INVENTORY = frozenset(
    (epic, resolution)
    for _, _, epic in FROZEN_REPLAY_INSTRUMENTS
    for resolution in FROZEN_RESOLUTIONS
)


class ArtifactIntegrityError(ValueError):
    """Raised before replay when immutable G3A evidence differs."""


@dataclass(frozen=True)
class SeriesEvidence:
    symbol: str
    instrument_name: str
    epic: str
    resolution: str
    resolution_label: str
    candle_count: int
    start_utc: datetime
    end_utc: datetime
    normalized_relative_path: str
    normalized_sha256: str

    def document(self) -> dict[str, object]:
        return {
            "symbol": self.symbol,
            "instrument_name": self.instrument_name,
            "epic": self.epic,
            "resolution": self.resolution,
            "resolution_label": self.resolution_label,
            "candle_count": self.candle_count,
            "start_utc": self.start_utc.isoformat(),
            "end_utc": self.end_utc.isoformat(),
            "normalized_relative_path": self.normalized_relative_path,
            "normalized_sha256": self.normalized_sha256,
        }


@dataclass(frozen=True)
class InstrumentRules:
    symbol: str
    instrument_name: str
    epic: str
    pip_size: float
    value_of_one_pip: float
    pip_currency: str
    minimum_size: float
    minimum_stop_pips: float

    def document(self) -> dict[str, object]:
        return {
            "symbol": self.symbol,
            "instrument_name": self.instrument_name,
            "epic": self.epic,
            "pip_size": self.pip_size,
            "value_of_one_pip": self.value_of_one_pip,
            "pip_currency": self.pip_currency,
            "pip_value_account_currency": "NOT_ESTABLISHED",
            "minimum_size": self.minimum_size,
            "minimum_stop_pips": self.minimum_stop_pips,
            "source": "ACCEPTED_G3A_MARKET_METADATA",
        }


@dataclass(frozen=True)
class ArtifactVerification:
    artifact_id: str
    package_fingerprint: str
    manifest_sha256: str
    dataset_fingerprint: str
    file_count: int
    total_size_bytes: int
    read_only_file_count: int
    series: tuple[SeriesEvidence, ...]
    instrument_rules: tuple[InstrumentRules, ...]

    def document(self) -> dict[str, object]:
        return {
            "status": "PASS",
            "artifact_id": self.artifact_id,
            "package_fingerprint": self.package_fingerprint,
            "manifest_sha256": self.manifest_sha256,
            "dataset_fingerprint": self.dataset_fingerprint,
            "file_count": self.file_count,
            "total_size_bytes": self.total_size_bytes,
            "read_only_file_count": self.read_only_file_count,
            "manifest_read_only": True,
            "normalized_hash_status": "PASS",
            "inventory_status": "PASS",
            "modified_after_g3a_status": "NO_CONTENT_CHANGE_DETECTED",
            "series": [item.document() for item in self.series],
            "instrument_rules": [item.document() for item in self.instrument_rules],
        }


@dataclass(frozen=True)
class ReplayDataset:
    verification: ArtifactVerification
    candles: dict[tuple[str, str], tuple[CanonicalCandle, ...]]
    instrument_rules: dict[str, InstrumentRules]

    def series(self, epic: str, resolution: str) -> tuple[CanonicalCandle, ...]:
        return self.candles[(epic, resolution)]

    def decision_starts(self, epic: str) -> tuple[datetime, ...]:
        minute = self.series(epic, "MINUTE")
        start = minute[0].timestamp_utc
        end = minute[-1].timestamp_utc
        result: list[datetime] = []
        current = start
        while current <= end:
            result.append(current)
            current += timedelta(minutes=1)
        return tuple(result)

    def missing_minute_starts(self, epic: str) -> tuple[datetime, ...]:
        observed = {item.timestamp_utc for item in self.series(epic, "MINUTE")}
        return tuple(value for value in self.decision_starts(epic) if value not in observed)

    def candle_at(
        self,
        epic: str,
        resolution: str,
        timestamp: datetime,
    ) -> CanonicalCandle | None:
        return next(
            (
                item
                for item in self.series(epic, resolution)
                if item.timestamp_utc == timestamp.astimezone(UTC)
            ),
            None,
        )

    def closed_window(
        self,
        epic: str,
        resolution: str,
        *,
        decision_time: datetime,
        count: int,
        not_before: datetime | None = None,
    ) -> tuple[CanonicalCandle, ...]:
        if count < 1 or resolution not in RESOLUTION_MINUTES:
            raise ValueError("closed-window request is invalid")
        duration = timedelta(minutes=RESOLUTION_MINUTES[resolution])
        decision = decision_time.astimezone(UTC)
        minimum = not_before.astimezone(UTC) if not_before is not None else None
        eligible = tuple(
            item
            for item in self.series(epic, resolution)
            if item.timestamp_utc + duration <= decision
            and (minimum is None or item.timestamp_utc >= minimum)
        )
        result = eligible[-count:]
        if any(item.timestamp_utc + duration > decision for item in result):
            raise ArtifactIntegrityError("future candle leakage detected")
        return result


def verify_and_load_package(package_root: Path) -> ReplayDataset:
    """Fail closed unless the complete accepted package and dataset are exact."""

    root = package_root.resolve()
    verification, manifest_files = _verify_package(root, require_read_only=True)
    manifest_root = (
        root / "payload" / "g3a-01-runtime" / "g3a" / "data" / "manifests" / FINAL_RUN_ID
    )
    run_manifest = _read_object(manifest_root / "run.manifest.json")
    if run_manifest.get("dataset_fingerprint") != EXPECTED_DATASET_FINGERPRINT:
        raise ArtifactIntegrityError("dataset fingerprint record differs")
    series_references = run_manifest.get("series_manifests")
    if not isinstance(series_references, list) or len(series_references) != 12:
        raise ArtifactIntegrityError("final series manifest inventory differs")

    candles: dict[tuple[str, str], tuple[CanonicalCandle, ...]] = {}
    series_evidence: list[SeriesEvidence] = []
    normalized_hashes: list[tuple[str, str, str]] = []
    observed_inventory: set[tuple[str, str]] = set()
    observed_canonical_versions: set[str] = set()
    observed_normalization_versions: set[str] = set()
    instrument_names = {epic: (symbol, name) for symbol, name, epic in FROZEN_REPLAY_INSTRUMENTS}
    rules = {
        epic: _load_instrument_rules(root, symbol=symbol, instrument_name=name, epic=epic)
        for symbol, name, epic in FROZEN_REPLAY_INSTRUMENTS
    }
    for reference in series_references:
        if not isinstance(reference, dict) or not isinstance(reference.get("relative_path"), str):
            raise ArtifactIntegrityError("series manifest reference is invalid")
        series_manifest_path = manifest_root.parent.parent / str(reference["relative_path"])
        if sha256_bytes(series_manifest_path.read_bytes()) != reference.get("sha256"):
            raise ArtifactIntegrityError("series manifest hash differs")
        series_manifest = _read_object(series_manifest_path)
        observed_canonical_versions.add(str(series_manifest.get("canonical_schema_version")))
        observed_normalization_versions.add(str(series_manifest.get("normalization_version")))
        epic = series_manifest.get("epic")
        resolution = series_manifest.get("resolution")
        if not isinstance(epic, str) or not isinstance(resolution, str):
            raise ArtifactIntegrityError("series manifest identity is invalid")
        identity = (epic, resolution)
        if identity not in EXPECTED_INVENTORY or identity in observed_inventory:
            raise ArtifactIntegrityError("EPIC/resolution inventory differs")
        observed_inventory.add(identity)
        normalized = series_manifest.get("normalized_data")
        if not isinstance(normalized, dict):
            raise ArtifactIntegrityError("normalized data evidence is missing")
        relative_path = normalized.get("relative_path")
        expected_hash = normalized.get("sha256")
        if not isinstance(relative_path, str) or not isinstance(expected_hash, str):
            raise ArtifactIntegrityError("normalized data evidence is invalid")
        normalized_path = manifest_root.parent.parent / relative_path
        package_relative = normalized_path.relative_to(root).as_posix()
        entry = manifest_files.get(package_relative)
        if entry is None or entry.get("sha256") != expected_hash:
            raise ArtifactIntegrityError("normalized file is not package-authenticated")
        loaded = _load_candles(normalized_path, epic=epic, resolution=resolution)
        if len(loaded) != series_manifest.get("candle_count"):
            raise ArtifactIntegrityError("normalized candle count differs")
        candles[identity] = loaded
        normalized_hashes.append((epic, resolution, expected_hash))
        symbol, instrument_name = instrument_names[epic]
        series_evidence.append(
            SeriesEvidence(
                symbol=symbol,
                instrument_name=instrument_name,
                epic=epic,
                resolution=resolution,
                resolution_label=str(series_manifest.get("resolution_label")),
                candle_count=len(loaded),
                start_utc=loaded[0].timestamp_utc,
                end_utc=loaded[-1].timestamp_utc,
                normalized_relative_path=package_relative,
                normalized_sha256=expected_hash,
            )
        )
    if observed_inventory != EXPECTED_INVENTORY:
        raise ArtifactIntegrityError("EPIC/resolution inventory is incomplete")
    if observed_canonical_versions != {FINAL_CANONICAL_SCHEMA_VERSION} or (
        observed_normalization_versions != {FINAL_NORMALIZATION_VERSION}
    ):
        raise ArtifactIntegrityError("final normalization version differs")
    dataset_fingerprint = fingerprint(
        {
            "schema_version": FINAL_CANONICAL_SCHEMA_VERSION,
            "normalization_version": FINAL_NORMALIZATION_VERSION,
            "series": sorted(normalized_hashes),
        }
    )
    if dataset_fingerprint != EXPECTED_DATASET_FINGERPRINT:
        raise ArtifactIntegrityError("computed dataset fingerprint differs")
    dataset = ReplayDataset(
        ArtifactVerification(
            artifact_id=verification.artifact_id,
            package_fingerprint=verification.package_fingerprint,
            manifest_sha256=verification.manifest_sha256,
            dataset_fingerprint=dataset_fingerprint,
            file_count=verification.file_count,
            total_size_bytes=verification.total_size_bytes,
            read_only_file_count=verification.read_only_file_count,
            series=tuple(sorted(series_evidence, key=lambda item: (item.symbol, item.resolution))),
            instrument_rules=tuple(rules[epic] for _, _, epic in FROZEN_REPLAY_INSTRUMENTS),
        ),
        candles,
        rules,
    )
    expected_gaps = {
        AUTHORITATIVE_GAP_EPIC: (AUTHORITATIVE_GAP_TIMESTAMP,),
        "CS.D.EURUSD.CEFM.IP": (),
        "CS.D.GBPUSD.MINI.IP": (),
    }
    for epic, expected in expected_gaps.items():
        if dataset.missing_minute_starts(epic) != expected:
            raise ArtifactIntegrityError("authoritative minute-gap inventory differs")
    return dataset


def _verify_package(
    package_root: Path,
    *,
    require_read_only: bool,
) -> tuple[ArtifactVerification, dict[str, dict[str, Any]]]:
    manifest_path = package_root / "package-manifest.json"
    manifest = _read_object(manifest_path)
    if (
        manifest.get("schema_version") != PACKAGE_SCHEMA_VERSION
        or manifest.get("artifact_id") != EXPECTED_ARTIFACT_ID
    ):
        raise ArtifactIntegrityError("package identity or schema differs")
    expected_files = manifest.get("files")
    if not isinstance(expected_files, list):
        raise ArtifactIntegrityError("package file manifest is invalid")
    actual_files: list[dict[str, Any]] = []
    read_only_count = 0
    manifest_read_only = not manifest_path.stat().st_mode & stat.S_IWRITE
    payload = package_root / "payload"
    for path in sorted(item for item in payload.rglob("*") if item.is_file()):
        if path.is_symlink():
            raise ArtifactIntegrityError("package payload contains a symlink")
        mode = path.stat().st_mode
        if not mode & stat.S_IWRITE:
            read_only_count += 1
        actual_files.append(
            {
                "relative_path": path.relative_to(package_root).as_posix(),
                "size_bytes": path.stat().st_size,
                "sha256": sha256_bytes(path.read_bytes()),
            }
        )
    if actual_files != expected_files:
        raise ArtifactIntegrityError("payload hash, size, or file set differs")
    package_fingerprint = fingerprint(
        {
            "schema_version": manifest.get("schema_version"),
            "artifact_id": manifest.get("artifact_id"),
            "file_count": manifest.get("file_count"),
            "total_size_bytes": manifest.get("total_size_bytes"),
            "files": manifest.get("files"),
        }
    )
    manifest_sha256 = sha256_bytes(manifest_path.read_bytes())
    if (
        package_fingerprint != EXPECTED_PACKAGE_FINGERPRINT
        or manifest.get("package_fingerprint") != package_fingerprint
        or manifest_sha256 != EXPECTED_MANIFEST_SHA256
    ):
        raise ArtifactIntegrityError("package or manifest fingerprint differs")
    if manifest.get("file_count") != len(actual_files) or manifest.get("total_size_bytes") != sum(
        int(item["size_bytes"]) for item in actual_files
    ):
        raise ArtifactIntegrityError("package totals differ")
    if require_read_only and (read_only_count != len(actual_files) or not manifest_read_only):
        raise ArtifactIntegrityError("package payload is not immutable/read-only")
    file_map = {str(item["relative_path"]): item for item in actual_files}
    return (
        ArtifactVerification(
            artifact_id=EXPECTED_ARTIFACT_ID,
            package_fingerprint=package_fingerprint,
            manifest_sha256=manifest_sha256,
            dataset_fingerprint=EXPECTED_DATASET_FINGERPRINT,
            file_count=len(actual_files),
            total_size_bytes=sum(int(item["size_bytes"]) for item in actual_files),
            read_only_file_count=read_only_count,
            series=(),
            instrument_rules=(),
        ),
        file_map,
    )


def _load_candles(path: Path, *, epic: str, resolution: str) -> tuple[CanonicalCandle, ...]:
    result = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        try:
            candle = canonical_candle_from_document(json.loads(line))
        except (json.JSONDecodeError, ValueError) as error:
            raise ArtifactIntegrityError(f"invalid normalized row at line {line_number}") from error
        if candle.epic != epic or candle.resolution != resolution:
            raise ArtifactIntegrityError("normalized row identity differs")
        result.append(candle)
    if not result:
        raise ArtifactIntegrityError("normalized series is empty")
    timestamps = [item.timestamp_utc for item in result]
    if timestamps != sorted(timestamps) or len(timestamps) != len(set(timestamps)):
        raise ArtifactIntegrityError("normalized timestamps are non-monotonic or duplicate")
    return tuple(result)


def _load_instrument_rules(
    package_root: Path,
    *,
    symbol: str,
    instrument_name: str,
    epic: str,
) -> InstrumentRules:
    path = (
        package_root
        / "payload"
        / "g3a-01-runtime"
        / "g3a"
        / "data"
        / "raw"
        / "g3a-01-20260815"
        / f"market-{symbol}.json"
    )
    document = _read_object(path)
    instrument = document.get("instrument")
    rules = document.get("dealingRules")
    if not isinstance(instrument, dict) or not isinstance(rules, dict):
        raise ArtifactIntegrityError("market metadata is incomplete")
    if instrument.get("epic") != epic or instrument.get("name") != instrument_name:
        raise ArtifactIntegrityError("market metadata identity differs")
    one_pip_means = instrument.get("onePipMeans")
    value_of_one_pip = instrument.get("valueOfOnePip")
    if not isinstance(one_pip_means, str) or not isinstance(value_of_one_pip, str):
        raise ArtifactIntegrityError("pip metadata is missing")
    parts = one_pip_means.split()
    if len(parts) != 2 or "/" not in parts[1]:
        raise ArtifactIntegrityError("pip metadata format differs")
    try:
        pip_size = float(Decimal(parts[0]))
        pip_value = float(Decimal(value_of_one_pip))
    except (InvalidOperation, ValueError) as error:
        raise ArtifactIntegrityError("pip metadata is invalid") from error
    pip_currency = parts[1].split("/", 1)[0]
    minimum_size = _point_rule(rules, "minDealSize")
    minimum_stop = _point_rule(rules, "minNormalStopOrLimitDistance")
    if pip_size <= 0 or pip_value <= 0 or len(pip_currency) != 3 or not pip_currency.isalpha():
        raise ArtifactIntegrityError("pip metadata value is invalid")
    return InstrumentRules(
        symbol=symbol,
        instrument_name=instrument_name,
        epic=epic,
        pip_size=pip_size,
        value_of_one_pip=pip_value,
        pip_currency=pip_currency,
        minimum_size=minimum_size,
        minimum_stop_pips=minimum_stop,
    )


def _point_rule(rules: dict[str, Any], name: str) -> float:
    value = rules.get(name)
    if not isinstance(value, dict) or value.get("unit") != "POINTS":
        raise ArtifactIntegrityError(f"{name} rule is missing or not point-denominated")
    number = value.get("value")
    if isinstance(number, bool) or not isinstance(number, int | float) or float(number) <= 0:
        raise ArtifactIntegrityError(f"{name} rule is invalid")
    return float(number)


def _read_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ArtifactIntegrityError(f"invalid JSON evidence: {path.name}") from error
    if not isinstance(value, dict):
        raise ArtifactIntegrityError(f"JSON evidence is not an object: {path.name}")
    return value
