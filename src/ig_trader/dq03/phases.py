"""Durable, sanitized DQ-03 phase context and verified-resolution loading."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from src.ig_trader.dq03.artifacts import RESOLVER_VERSION, phase_context_matches
from src.ig_trader.dq03.models import (
    CandidateEvidence,
    DataStatus,
    DQ03Resolution,
    DQ03Status,
    MarketMetadata,
)
from src.ig_trader.strategy_lab.models import AssetClass


def phase_context(account_id: str, *, freshness_hours: int = 24) -> dict[str, object]:
    """Create a non-reversible account identity context for later read-only phases."""

    if not account_id.strip():
        raise RuntimeError("IG Demo account identity cannot be proven")
    return {
        "account_identity_fingerprint": hashlib.sha256(account_id.encode("utf-8")).hexdigest(),
        "environment": "DEMO",
        "resolver_version": RESOLVER_VERSION,
        "metadata_freshness_hours": freshness_hours,
    }


def load_phase_one_resolutions(
    output_directory: Path, context: dict[str, object]
) -> tuple[DQ03Resolution, ...]:
    """Load only same-account, fresh-policy Phase 1 artifacts for phases 2 and 3."""

    if not phase_context_matches(output_directory, context):
        raise RuntimeError(
            "DQ-03 Phase 1 artifacts do not match this Demo account or resolver policy"
        )
    path = output_directory / "instrument_registry.json"
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
        values = document["instruments"]
    except (OSError, KeyError, TypeError, json.JSONDecodeError) as error:
        raise RuntimeError("DQ-03 Phase 1 instrument registry is unreadable") from error
    if not isinstance(values, list):
        raise RuntimeError("DQ-03 Phase 1 instrument registry has an invalid shape")
    return tuple(_resolution(item) for item in values if isinstance(item, dict))


def _resolution(value: dict[str, object]) -> DQ03Resolution:
    metadata_value = value.get("metadata")
    metadata = _metadata(metadata_value) if isinstance(metadata_value, dict) else None
    try:
        asset_class = AssetClass(str(value["asset_class"]))
        status = DQ03Status(str(value["classification"]))
        data_status = DataStatus(str(value["data_status"]))
    except (KeyError, ValueError) as error:
        raise RuntimeError("DQ-03 Phase 1 registry has an invalid resolution") from error
    observed = _datetime(value.get("observed_at_utc")) or datetime.now(UTC)
    return DQ03Resolution(
        symbol=str(value.get("canonical_symbol") or ""),
        asset_class=asset_class,
        classification=status,
        selected_epic=_text(value.get("selected_epic")),
        display_name=str(value.get("display_name") or ""),
        selected_alias=_text(value.get("selected_search_alias")),
        candidate_count=int(value.get("candidate_count") or 0),
        selection_score=_integer(value.get("selection_score")),
        selection_reasons=tuple(
            item for item in value.get("selection_reasons", []) if isinstance(item, str)
        )
        if isinstance(value.get("selection_reasons"), list)
        else (),
        candidates=_candidates(value.get("candidates")),
        metadata=metadata,
        data_status=data_status,
        observed_at=observed,
        error=_text(value.get("error")),
        broker_validation_fingerprint=_text(value.get("broker_validation_fingerprint")),
        cost_model_status=DataStatus(str(value.get("cost_model_status", "COST_MODEL_INCOMPLETE"))),
    )


def _metadata(value: dict[str, object]) -> MarketMetadata:
    return MarketMetadata(
        epic=str(value.get("epic") or ""),
        display_name=_text(value.get("display_name")),
        instrument_type=_text(value.get("instrument_type")),
        expiry=_text(value.get("expiry")),
        market_status=_text(value.get("market_status")),
        currency=_text(value.get("currency")),
        minimum_deal_size=_decimal(value.get("minimum_deal_size")),
        minimum_stop_distance=_decimal(value.get("minimum_stop_distance")),
        decimal_places=_integer(value.get("decimal_places")),
        one_pip_means=_decimal(value.get("one_pip_means")),
        value_of_one_pip=_decimal(value.get("value_of_one_pip")),
        streaming_prices_available=value.get("streaming_prices_available")
        if isinstance(value.get("streaming_prices_available"), bool)
        else None,
        bid=_decimal(value.get("bid")),
        offer=_decimal(value.get("offer")),
        controlled_risk_supported=value.get("controlled_risk_supported")
        if isinstance(value.get("controlled_risk_supported"), bool)
        else None,
        observed_at=_datetime(value.get("observed_at_utc")) or datetime.now(UTC),
        minimum_deal_size_unit=_text(value.get("minimum_deal_size_unit")),
        minimum_stop_distance_unit=_text(value.get("minimum_stop_distance_unit")),
        contract_size=_decimal(value.get("contract_size")),
        lot_size=_decimal(value.get("lot_size")),
        scaling_factor=_integer(value.get("scaling_factor")),
    )


def _candidates(value: object) -> tuple[CandidateEvidence, ...]:
    if not isinstance(value, list):
        return ()
    candidates: list[CandidateEvidence] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        metadata_value = item.get("metadata")
        metadata = _metadata(metadata_value) if isinstance(metadata_value, dict) else None
        candidates.append(
            CandidateEvidence(
                epic=_text(item.get("epic")),
                display_name=_text(item.get("display_name")),
                instrument_type=_text(item.get("instrument_type")),
                expiry=_text(item.get("expiry")),
                market_status=_text(item.get("market_status")),
                aliases=tuple(alias for alias in item.get("aliases", []) if isinstance(alias, str))
                if isinstance(item.get("aliases"), list)
                else (),
                score=_integer(item.get("score")),
                selected=bool(item.get("selected")),
                reasons=tuple(
                    reason for reason in item.get("reasons", []) if isinstance(reason, str)
                )
                if isinstance(item.get("reasons"), list)
                else (),
                metadata=metadata,
            )
        )
    return tuple(candidates)


def _decimal(value: object) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _datetime(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)
    except ValueError:
        return None


def _integer(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _text(value: object) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None
