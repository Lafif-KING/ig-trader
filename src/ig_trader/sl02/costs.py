"""Explicit, reviewed SL-02 cost-evidence loading; no generic defaults exist."""

from __future__ import annotations

import json
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from src.ig_trader.sl02.contracts import BrokerEvidence, CostEvidence
from src.ig_trader.strategy_lab.engine import FrictionModel


def load_cost_evidence(path: Path) -> dict[str, CostEvidence]:
    """Return only complete, fingerprint-bound cost evidence from a local artifact."""

    try:
        document: Any = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    rows = document.get("instruments") if isinstance(document, dict) else None
    if not isinstance(rows, list):
        return {}
    result: dict[str, CostEvidence] = {}
    for row in rows:
        evidence = _parse(row)
        if evidence is not None:
            result[evidence.symbol] = evidence
    return result


def friction_model(
    broker: BrokerEvidence | None, cost: CostEvidence | None, *, stress_multiplier: Decimal
) -> FrictionModel | None:
    """Build friction only from matching broker facts and reviewed cost evidence."""

    if (
        broker is None
        or cost is None
        or not broker.metadata_fingerprint
        or broker.metadata_fingerprint != cost.metadata_fingerprint
        or broker.pip_or_tick_size is None
        or broker.minimum_stop_distance is None
        or broker.minimum_deal_size is None
        or broker.data_status != "BROKER_VALIDATED"
    ):
        return None
    return FrictionModel(
        tick_size=broker.pip_or_tick_size,
        typical_spread=cost.base_spread * stress_multiplier,
        slippage=cost.slippage * stress_multiplier,
        commission_price_equivalent=cost.commission_price_equivalent,
        minimum_stop_distance=broker.minimum_stop_distance,
        minimum_size=broker.minimum_deal_size,
        allowed_utc_hours=cost.allowed_utc_hours,
    )


def _parse(value: object) -> CostEvidence | None:
    if not isinstance(value, dict):
        return None
    symbol = value.get("symbol")
    fingerprint = value.get("metadata_fingerprint")
    basis = value.get("evidence_basis")
    hours = value.get("allowed_utc_hours")
    if (
        not isinstance(symbol, str)
        or not symbol.isupper()
        or not isinstance(fingerprint, str)
        or len(fingerprint) != 64
        or not isinstance(basis, str)
        or not basis.strip()
        or not isinstance(hours, list)
        or not all(isinstance(hour, int) and not isinstance(hour, bool) and 0 <= hour <= 23 for hour in hours)
        or not hours
    ):
        return None
    base_spread = _decimal(value.get("base_spread"))
    slippage = _decimal(value.get("slippage"))
    commission = _decimal(value.get("commission_price_equivalent"))
    if base_spread is None or slippage is None or commission is None:
        return None
    if base_spread < 0 or slippage < 0 or commission < 0:
        return None
    return CostEvidence(
        symbol,
        fingerprint,
        base_spread,
        slippage,
        commission,
        frozenset(hours),
        basis.strip(),
    )


def _decimal(value: object) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
