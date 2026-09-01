"""Load only explicit, sanitized DQ-03 market evidence for SHADOW01."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from src.ig_trader.dq03.artifacts import ARTIFACT_SCHEMA, RESOLVER_VERSION
from src.ig_trader.shadow01.config import ShadowTournamentConfig
from src.ig_trader.shadow01.models import AssetClass, MarketDataState, MarketSpec, fingerprint

_DQ03_HISTORY_AUGMENTATION_PHASES = frozenset(("PHASE_2", "PHASE_3"))
_DQ03_METADATA_KEYS = frozenset(
    (
        "epic",
        "display_name",
        "instrument_type",
        "expiry",
        "market_status",
        "currency",
        "minimum_deal_size",
        "minimum_stop_distance",
        "decimal_places",
        "one_pip_means",
        "value_of_one_pip",
        "streaming_prices_available",
        "bid",
        "offer",
        "spread",
        "controlled_risk_supported",
        "minimum_deal_size_unit",
        "minimum_stop_distance_unit",
        "contract_size",
        "lot_size",
        "scaling_factor",
        "missing_fields",
        "observed_at_utc",
    )
)


class ShadowRegistryError(ValueError):
    """The supplied DQ-03 evidence cannot safely identify the requested market scope."""


@dataclass(frozen=True)
class ShadowMarketRegistry:
    """Twenty fixed symbols, each either proven or explicitly unavailable."""

    markets: tuple[MarketSpec, ...]
    source_fingerprint: str | None
    source_path: Path | None

    @property
    def verified_count(self) -> int:
        return sum(item.state is MarketDataState.AVAILABLE for item in self.markets)

    @property
    def unavailable_count(self) -> int:
        return len(self.markets) - self.verified_count

    def by_symbol(self, symbol: str) -> MarketSpec:
        for item in self.markets:
            if item.symbol == symbol:
                return item
        raise KeyError(symbol)

    def document(self) -> dict[str, object]:
        return {
            "scope": "SHADOW01_FROZEN_20_MARKETS",
            "source_path": str(self.source_path) if self.source_path else None,
            "source_fingerprint": self.source_fingerprint,
            "verified_count": self.verified_count,
            "unavailable_count": self.unavailable_count,
            "markets": [
                {
                    "symbol": item.symbol,
                    "asset_class": item.asset_class.value,
                    "epic": item.epic,
                    "state": item.state.value,
                    "reason": item.reason,
                    "metadata": item.metadata,
                }
                for item in self.markets
            ],
        }


def load_verified_dq03_registry(
    config: ShadowTournamentConfig, path: Path | None
) -> ShadowMarketRegistry:
    """Read a DQ-03 artifact without guessing any absent EPIC.

    A missing or malformed registry remains an explicit 20-row unavailable
    result.  This lets the dashboard explain the blocker while preventing a
    caller from silently substituting a broker contract.
    """

    if path is None:
        return _all_unavailable(config, None, None, "DQ03_REGISTRY_NOT_SUPPLIED")
    try:
        raw: Any = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return _all_unavailable(config, path, None, "DQ03_REGISTRY_UNAVAILABLE")
    if not isinstance(raw, dict):
        return _all_unavailable(config, path, None, "DQ03_REGISTRY_INVALID")
    source_fingerprint = fingerprint(raw)
    if raw.get("schema_version") != ARTIFACT_SCHEMA:
        return _all_unavailable(config, path, source_fingerprint, "DQ03_REGISTRY_SCHEMA_INVALID")
    if raw.get("execution_authority") != "OFF":
        return _all_unavailable(
            config, path, source_fingerprint, "DQ03_REGISTRY_EXECUTION_AUTHORITY_INVALID"
        )
    entries = raw.get("instruments")
    if not isinstance(entries, list):
        return _all_unavailable(config, path, source_fingerprint, "DQ03_REGISTRY_ROWS_INVALID")
    history_by_symbol, provenance_error = _load_dq03_provenance(path, raw, len(entries))
    if provenance_error is not None:
        return _all_unavailable(config, path, source_fingerprint, provenance_error)
    indexed = _index_entries(entries)
    markets = tuple(
        _market_from_entry(
            item,
            indexed.get(str(item["symbol"])),
            history_by_symbol.get(str(item["symbol"])),
        )
        for item in config.universe
    )
    return ShadowMarketRegistry(markets, source_fingerprint, path)


def require_exact_twenty(registry: ShadowMarketRegistry) -> tuple[MarketSpec, ...]:
    """Defensive assertion for worker entry points; never shrink the universe."""

    if len(registry.markets) != 20:
        raise ShadowRegistryError("SHADOW01_REGISTRY_SCOPE_INVALID")
    return registry.markets


def _all_unavailable(
    config: ShadowTournamentConfig,
    path: Path | None,
    source_fingerprint: str | None,
    reason: str,
) -> ShadowMarketRegistry:
    markets = tuple(
        MarketSpec(
            symbol=str(item["symbol"]),
            asset_class=AssetClass(str(item["asset_class"])),
            epic=None,
            state=MarketDataState.MARKET_DATA_UNAVAILABLE,
            reason=reason,
        )
        for item in config.universe
    )
    return ShadowMarketRegistry(markets, source_fingerprint, path)


def _index_entries(values: list[object]) -> dict[str, dict[str, object] | None]:
    indexed: dict[str, dict[str, object] | None] = {}
    for value in values:
        if not isinstance(value, dict) or not isinstance(value.get("canonical_symbol"), str):
            continue
        symbol = value["canonical_symbol"].upper()
        if symbol in indexed:
            indexed[symbol] = None
        else:
            indexed[symbol] = value
    return indexed


def _load_dq03_provenance(
    registry_path: Path,
    registry: dict[str, object],
    entry_count: int,
) -> tuple[dict[str, dict[str, object] | None], str | None]:
    """Require the immutable DQ-03 handoff chain, not a standalone EPIC claim.

    DQ-03 does not publish signed artifacts, so a local reader cannot prove a
    file's origin cryptographically.  It can, however, require every
    cross-linked document that ``write_dq03_artifacts`` emits after bounded
    broker history validation.  A bare registry document therefore never
    unlocks a Shadow market.
    """

    context = registry.get("run_context")
    phase = registry.get("latest_augmentation_phase")
    if (
        registry.get("phase") != "PHASE_1"
        or phase not in _DQ03_HISTORY_AUGMENTATION_PHASES
        or not _valid_dq03_context(context)
    ):
        return {}, "DQ03_REGISTRY_PROVENANCE_INVALID"
    assert isinstance(context, dict)

    manifest = _read_object(registry_path.parent / "discovery_manifest.json")
    if manifest is None:
        return {}, "DQ03_PROVENANCE_MANIFEST_UNAVAILABLE"
    if not _valid_manifest(manifest, context, phase, entry_count):
        return {}, "DQ03_PROVENANCE_MANIFEST_INVALID"

    history = _read_object(registry_path.parent / "history_validation.json")
    if history is None:
        return {}, "DQ03_PROVENANCE_HISTORY_UNAVAILABLE"
    if (
        history.get("schema_version") != ARTIFACT_SCHEMA
        or history.get("execution_authority") != "OFF"
        or history.get("phase") != "PHASE_2"
    ):
        return {}, "DQ03_PROVENANCE_HISTORY_INVALID"
    samples = history.get("samples")
    if not isinstance(samples, list):
        return {}, "DQ03_PROVENANCE_HISTORY_INVALID"
    return _index_history_samples(samples), None


def _valid_dq03_context(value: object) -> bool:
    if not isinstance(value, dict):
        return False
    freshness = value.get("metadata_freshness_hours")
    return (
        value.get("environment") == "DEMO"
        and value.get("resolver_version") == RESOLVER_VERSION
        and _valid_fingerprint(value.get("account_identity_fingerprint"))
        and isinstance(freshness, int)
        and not isinstance(freshness, bool)
        and freshness > 0
    )


def _valid_manifest(
    manifest: dict[str, object],
    context: dict[str, object],
    latest_augmentation_phase: object,
    entry_count: int,
) -> bool:
    request_counts = manifest.get("request_counts")
    return (
        manifest.get("schema_version") == ARTIFACT_SCHEMA
        and manifest.get("execution_authority") == "OFF"
        and manifest.get("phase") == latest_augmentation_phase
        and manifest.get("run_context") == context
        and manifest.get("instrument_count") == entry_count
        and manifest.get("history_validation_artifact_present") is True
        and manifest.get("demo_create_calls") == 0
        and manifest.get("demo_close_calls") == 0
        and isinstance(manifest.get("generated_at_utc"), str)
        and bool(manifest.get("generated_at_utc"))
        and isinstance(request_counts, dict)
        and request_counts.get("demo_create_calls") == 0
        and request_counts.get("demo_close_calls") == 0
    )


def _read_object(path: Path) -> dict[str, object] | None:
    try:
        value: Any = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _index_history_samples(values: list[object]) -> dict[str, dict[str, object] | None]:
    indexed: dict[str, dict[str, object] | None] = {}
    for value in values:
        if not isinstance(value, dict) or not isinstance(value.get("symbol"), str):
            continue
        symbol = value["symbol"]
        if symbol != symbol.upper():
            continue
        if symbol in indexed:
            indexed[symbol] = None
        else:
            indexed[symbol] = value
    return indexed


def _market_from_entry(
    configured: dict[str, str],
    entry: dict[str, object] | None,
    history_sample: dict[str, object] | None,
) -> MarketSpec:
    symbol = configured["symbol"]
    asset_class = AssetClass(configured["asset_class"])
    if entry is None:
        return _unavailable(symbol, asset_class, "DQ03_REGISTRY_ENTRY_MISSING_OR_DUPLICATE")
    if entry.get("asset_class") != asset_class.value:
        return _unavailable(symbol, asset_class, "DQ03_ASSET_CLASS_MISMATCH")
    if entry.get("classification") != "VERIFIED":
        return _unavailable(symbol, asset_class, "DQ03_MARKET_NOT_VERIFIED")
    if entry.get("execution_authority") != "OFF":
        return _unavailable(symbol, asset_class, "DQ03_ENTRY_EXECUTION_AUTHORITY_INVALID")
    if entry.get("data_status") != "BROKER_VALIDATED":
        return _unavailable(symbol, asset_class, "DQ03_BROKER_VALIDATION_REQUIRED")
    epic = entry.get("selected_epic")
    if not isinstance(epic, str) or not epic.strip():
        return _unavailable(symbol, asset_class, "DQ03_VERIFIED_EPIC_MISSING")
    epic = epic.strip()
    if not _valid_selection_evidence(entry, epic, history_sample):
        return _unavailable(symbol, asset_class, "DQ03_SELECTION_PROVENANCE_INVALID")
    metadata = entry.get("metadata")
    if not isinstance(metadata, dict) or not _valid_metadata_evidence(entry, metadata, epic):
        return _unavailable(symbol, asset_class, "DQ03_METADATA_PROVENANCE_INVALID")
    if not _valid_history_evidence(history_sample, symbol, epic, entry):
        return _unavailable(symbol, asset_class, "DQ03_HISTORY_PROVENANCE_INVALID")
    return MarketSpec(symbol, asset_class, epic, MarketDataState.AVAILABLE, metadata=metadata)


def _valid_selection_evidence(
    entry: dict[str, object],
    epic: str,
    history_sample: dict[str, object] | None,
) -> bool:
    """Validate either complete candidate evidence or DQ-03's redacted handoff form.

    The authoritative DQ-03 handoff may intentionally omit all candidate
    records after Phase 3.  That representation is accepted only when the
    chosen market is still bound to the selected metadata and to the exact
    independently stored broker-history sample.  An empty candidate list is
    therefore not, by itself, selection evidence.
    """

    candidates = entry.get("candidates")
    candidate_count = entry.get("candidate_count")
    score = entry.get("selection_score")
    reasons = entry.get("selection_reasons")
    if (
        entry.get("selected_candidate_epic") != epic
        or not isinstance(entry.get("selected_search_alias"), str)
        or not entry["selected_search_alias"].strip()
        or not isinstance(candidate_count, int)
        or isinstance(candidate_count, bool)
        or candidate_count < 1
        or not isinstance(score, int)
        or isinstance(score, bool)
        or not isinstance(reasons, list)
        or not reasons
        or not all(isinstance(reason, str) and reason.strip() for reason in reasons)
        or not isinstance(candidates, list)
    ):
        return False
    if not candidates:
        return _valid_redacted_selection_evidence(entry, epic, history_sample)
    if len(candidates) != candidate_count:
        return False
    return any(
        isinstance(candidate, dict)
        and candidate.get("selected") is True
        and candidate.get("epic") == epic
        and candidate.get("metadata") == entry.get("metadata")
        for candidate in candidates
    )


def _valid_redacted_selection_evidence(
    entry: dict[str, object],
    epic: str,
    history_sample: dict[str, object] | None,
) -> bool:
    """Accept the narrow Phase-3 representation with candidate rows redacted.

    The source artifact retains an empty ``candidates`` and
    ``rejected_candidates`` list, but preserves the winning identity,
    metadata, score, rationale, and complete broker-validation sample.  The
    embedded sample must exactly match the cross-linked Phase-2 history file;
    otherwise no market is unlocked.
    """

    metadata = entry.get("metadata")
    embedded_history = entry.get("broker_validation")
    selected_name = entry.get("selected_candidate_name")
    display_name = entry.get("display_name")
    candidate_score = entry.get("candidate_score")
    return (
        entry.get("rejected_candidates") == []
        and isinstance(selected_name, str)
        and bool(selected_name.strip())
        and display_name == selected_name
        and isinstance(metadata, dict)
        and metadata.get("epic") == epic
        and metadata.get("display_name") == selected_name
        and isinstance(candidate_score, int)
        and not isinstance(candidate_score, bool)
        and candidate_score == entry.get("selection_score")
        and isinstance(embedded_history, dict)
        and history_sample is not None
        and embedded_history == history_sample
        and embedded_history.get("symbol") == entry.get("canonical_symbol")
        and embedded_history.get("epic") == epic
        and embedded_history.get("status") == "BROKER_VALIDATED"
        and embedded_history.get("source_fingerprint") == entry.get("broker_validation_fingerprint")
    )


def _valid_metadata_evidence(
    entry: dict[str, object], metadata: dict[str, object], epic: str
) -> bool:
    value = entry.get("metadata_fingerprint")
    return (
        _DQ03_METADATA_KEYS.issubset(metadata)
        and metadata.get("epic") == epic
        and metadata.get("missing_fields") == []
        and isinstance(metadata.get("observed_at_utc"), str)
        and bool(metadata.get("observed_at_utc"))
        and isinstance(metadata.get("streaming_prices_available"), bool)
        and _valid_fingerprint(value)
        and value == fingerprint(metadata)
    )


def _valid_history_evidence(
    sample: dict[str, object] | None,
    symbol: str,
    epic: str,
    entry: dict[str, object],
) -> bool:
    if sample is None:
        return False
    rows = sample.get("rows")
    source_fingerprint = sample.get("source_fingerprint")
    requested_points = sample.get("requested_points")
    returned_points = sample.get("returned_points")
    row_count = sample.get("row_count")
    if (
        sample.get("symbol") != symbol
        or sample.get("epic") != epic
        or sample.get("status") != "BROKER_VALIDATED"
        or not isinstance(sample.get("resolution"), str)
        or not sample["resolution"].strip()
        or not isinstance(rows, list)
        or not rows
        or not all(isinstance(row, dict) for row in rows)
        or not isinstance(requested_points, int)
        or isinstance(requested_points, bool)
        or requested_points < 2
        or returned_points != requested_points
        or row_count != len(rows)
        or row_count != returned_points
        or sample.get("timestamp_shape_valid") is not True
        or sample.get("ohlc_shape_valid") is not True
        or sample.get("timestamps_monotonic") is not True
        or sample.get("resolution_ordering_valid") is not True
        or sample.get("duplicate_timestamp_count") != 0
        or sample.get("invalid_row_count") != 0
        or not _valid_fingerprint(source_fingerprint)
        or source_fingerprint != entry.get("broker_validation_fingerprint")
    ):
        return False
    return source_fingerprint == fingerprint(
        {"symbol": symbol, "epic": epic, "resolution": sample["resolution"], "rows": rows}
    )


def _valid_fingerprint(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value.lower())
    )


def _unavailable(symbol: str, asset_class: AssetClass, reason: str) -> MarketSpec:
    return MarketSpec(
        symbol=symbol,
        asset_class=asset_class,
        epic=None,
        state=MarketDataState.MARKET_DATA_UNAVAILABLE,
        reason=reason,
    )
