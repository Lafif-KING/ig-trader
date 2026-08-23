"""Immutable, sanitized DQ-03 resolution evidence contracts."""

# ruff: noqa: E501

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum

from src.ig_trader.strategy_lab.models import AssetClass


class DQ03Status(StrEnum):
    """Conservative outcomes for a canonical research instrument."""

    VERIFIED = "VERIFIED"
    AMBIGUOUS = "AMBIGUOUS"
    NOT_FOUND = "NOT_FOUND"
    UNTRADEABLE = "UNTRADEABLE"
    METADATA_INCOMPLETE = "METADATA_INCOMPLETE"
    UNSUPPORTED_PRODUCT = "UNSUPPORTED_PRODUCT"


class DataStatus(StrEnum):
    """Data is unavailable until a bounded read proves its contract."""

    DATA_NOT_AVAILABLE = "DATA_NOT_AVAILABLE"
    BROKER_VALIDATION_PENDING = "BROKER_VALIDATION_PENDING"
    BROKER_VALIDATED = "BROKER_VALIDATED"
    DATA_QUALITY_FAIL = "DATA_QUALITY_FAIL"
    COST_MODEL_INCOMPLETE = "COST_MODEL_INCOMPLETE"


@dataclass
class RequestCounters:
    """Per-run request accounting; no counter is inferred from a cache hit."""

    preflight_request_count: int = 0
    search_request_count: int = 0
    metadata_request_count: int = 0
    history_request_count: int = 0
    history_points_consumed: int = 0
    streaming_subscription_count: int = 0
    demo_create_calls: int = 0
    demo_close_calls: int = 0

    @property
    def total_ig_requests(self) -> int:
        return (
            self.preflight_request_count
            + self.search_request_count
            + self.metadata_request_count
            + self.history_request_count
        )

    def document(self) -> dict[str, int]:
        return {
            "preflight_request_count": self.preflight_request_count,
            "search_request_count": self.search_request_count,
            "metadata_request_count": self.metadata_request_count,
            "history_request_count": self.history_request_count,
            "history_points_consumed": self.history_points_consumed,
            "streaming_subscription_count": self.streaming_subscription_count,
            "total_IG_requests": self.total_ig_requests,
            "demo_create_calls": self.demo_create_calls,
            "demo_close_calls": self.demo_close_calls,
        }


@dataclass(frozen=True)
class MarketMetadata:
    """Only values directly returned by IG's market metadata response."""

    epic: str
    display_name: str | None
    instrument_type: str | None
    expiry: str | None
    market_status: str | None
    currency: str | None
    minimum_deal_size: Decimal | None
    minimum_stop_distance: Decimal | None
    decimal_places: int | None
    one_pip_means: Decimal | None
    value_of_one_pip: Decimal | None
    streaming_prices_available: bool | None
    bid: Decimal | None
    offer: Decimal | None
    controlled_risk_supported: bool | None
    observed_at: datetime

    @property
    def spread(self) -> Decimal | None:
        if self.bid is None or self.offer is None or self.bid < 0 or self.offer < self.bid:
            return None
        return self.offer - self.bid

    @property
    def complete(self) -> bool:
        return all(
            value is not None
            for value in (
                self.display_name,
                self.instrument_type,
                self.expiry,
                self.market_status,
                self.currency,
                self.minimum_deal_size,
                self.minimum_stop_distance,
                self.decimal_places,
                self.one_pip_means,
                self.value_of_one_pip,
                self.streaming_prices_available,
                self.bid,
                self.offer,
            )
        ) and bool(
            self.minimum_deal_size
            and self.minimum_deal_size > 0
            and self.minimum_stop_distance
            and self.minimum_stop_distance > 0
            and self.one_pip_means
            and self.one_pip_means > 0
            and self.value_of_one_pip
            and self.value_of_one_pip > 0
            and self.bid
            and self.bid > 0
            and self.offer
            and self.offer > 0
            and self.spread is not None
        )

    @property
    def fingerprint(self) -> str:
        return _fingerprint(self.document())

    def document(self) -> dict[str, object]:
        return {
            "epic": self.epic,
            "display_name": self.display_name,
            "instrument_type": self.instrument_type,
            "expiry": self.expiry,
            "market_status": self.market_status,
            "currency": self.currency,
            "minimum_deal_size": _decimal_document(self.minimum_deal_size),
            "minimum_stop_distance": _decimal_document(self.minimum_stop_distance),
            "decimal_places": self.decimal_places,
            "one_pip_means": _decimal_document(self.one_pip_means),
            "value_of_one_pip": _decimal_document(self.value_of_one_pip),
            "streaming_prices_available": self.streaming_prices_available,
            "bid": _decimal_document(self.bid),
            "offer": _decimal_document(self.offer),
            "spread": _decimal_document(self.spread),
            "controlled_risk_supported": self.controlled_risk_supported,
            "observed_at_utc": self.observed_at.astimezone(UTC).isoformat(),
        }


@dataclass(frozen=True)
class CandidateEvidence:
    """An observed search candidate and deterministic accept/reject evidence."""

    epic: str | None
    display_name: str | None
    instrument_type: str | None
    expiry: str | None
    market_status: str | None
    aliases: tuple[str, ...]
    score: int | None
    selected: bool
    reasons: tuple[str, ...]
    metadata: MarketMetadata | None = None

    def document(self) -> dict[str, object]:
        return {
            "epic": self.epic,
            "display_name": self.display_name,
            "instrument_type": self.instrument_type,
            "expiry": self.expiry,
            "market_status": self.market_status,
            "aliases": list(self.aliases),
            "score": self.score,
            "selected": self.selected,
            "reasons": list(self.reasons),
            "metadata": self.metadata.document() if self.metadata else None,
        }


@dataclass(frozen=True)
class DQ03Resolution:
    """Authoritative per-run evidence for one research symbol, never an order permit."""

    symbol: str
    asset_class: AssetClass
    classification: DQ03Status
    selected_epic: str | None
    display_name: str
    selected_alias: str | None
    candidate_count: int
    selection_score: int | None
    selection_reasons: tuple[str, ...]
    candidates: tuple[CandidateEvidence, ...]
    metadata: MarketMetadata | None
    data_status: DataStatus
    observed_at: datetime
    error: str | None = None
    broker_validation_fingerprint: str | None = None
    cost_model_status: DataStatus = DataStatus.COST_MODEL_INCOMPLETE

    @property
    def metadata_fingerprint(self) -> str | None:
        return self.metadata.fingerprint if self.metadata else None

    def document(self) -> dict[str, object]:
        return {
            "canonical_symbol": self.symbol,
            "asset_class": self.asset_class.value,
            "classification": self.classification.value,
            "selected_epic": self.selected_epic,
            "display_name": self.display_name,
            "selected_search_alias": self.selected_alias,
            "candidate_count": self.candidate_count,
            "selection_score": self.selection_score,
            "selection_reasons": list(self.selection_reasons),
            "rejected_candidates": [
                item.document() for item in self.candidates if not item.selected
            ],
            "candidates": [item.document() for item in self.candidates],
            "metadata": self.metadata.document() if self.metadata else None,
            "metadata_fingerprint": self.metadata_fingerprint,
            "data_status": self.data_status.value,
            "cost_model_status": self.cost_model_status.value,
            "broker_validation_fingerprint": self.broker_validation_fingerprint,
            "observed_at_utc": self.observed_at.astimezone(UTC).isoformat(),
            "error": self.error,
            "execution_authority": "OFF",
        }

    def with_broker_validation(self, status: DataStatus, fingerprint: str | None) -> DQ03Resolution:
        return DQ03Resolution(
            **{
                **self.__dict__,
                "data_status": status,
                "broker_validation_fingerprint": fingerprint,
            }
        )


def metadata_from_transport(
    value: object, *, observed_at: datetime | None = None
) -> MarketMetadata:
    """Project the existing strict Demo transport model without guessing new fields."""

    return MarketMetadata(
        epic=str(value.epic),
        display_name=_optional_text(getattr(value, "display_name", None)),
        instrument_type=_optional_text(getattr(value, "asset_class", None)),
        expiry=_optional_text(getattr(value, "expiry", None)),
        market_status=_optional_text(getattr(value, "market_status", None)),
        currency=_optional_text(getattr(value, "currency", None)),
        minimum_deal_size=_decimal(getattr(value, "minimum_deal_size", None)),
        minimum_stop_distance=_decimal(getattr(value, "minimum_stop_distance", None)),
        decimal_places=_integer(getattr(value, "decimal_places", None)),
        one_pip_means=_decimal(getattr(value, "pip_or_tick_size", None)),
        value_of_one_pip=_decimal(getattr(value, "value_of_one_pip", None)),
        streaming_prices_available=_boolean(getattr(value, "streaming_available", None)),
        bid=_decimal(getattr(value, "bid", None)),
        offer=_decimal(getattr(value, "offer", None)),
        controlled_risk_supported=_boolean(getattr(value, "controlled_risk_supported", None)),
        observed_at=(observed_at or getattr(value, "observed_at", None) or datetime.now(UTC)),
    )


def _fingerprint(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    ).hexdigest()


def _decimal_document(value: Decimal | None) -> str | None:
    return str(value) if value is not None else None


def _decimal(value: object) -> Decimal | None:
    return value if isinstance(value, Decimal) else None


def _integer(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _boolean(value: object) -> bool | None:
    return value if isinstance(value, bool) else None


def _optional_text(value: object) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None
