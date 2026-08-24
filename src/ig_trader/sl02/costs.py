"""Fingerprint-bound, deterministic research friction evidence for SL-02."""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from src.ig_trader.sl02.contracts import BrokerEvidence, CostEvidence
from src.ig_trader.strategy_lab.engine import FrictionModel

IG_CASH_MARKET_COMMISSION_SOURCE = "https://deal.ig.com/content/files/CFD_2.pdf"
_RESEARCH_ALLOWED_UTC_HOURS = tuple(range(24))


def generate_research_cost_model(
    evidence: Mapping[str, BrokerEvidence],
    *,
    expected_symbols: Iterable[str],
    output_path: Path,
) -> dict[str, object]:
    """Create a repeatable research model from DQ-03 facts and a stated fee source.

    This document is deliberately not a Demo or Live cost approval. Spread is
    calculated from DQ-03 observed broker rows; slippage is a fixed conservative
    research assumption and is never tuned against strategy performance.
    """

    instruments: list[dict[str, object]] = []
    incomplete: dict[str, list[str]] = {}
    for symbol in expected_symbols:
        broker = evidence.get(symbol)
        reasons = _generation_issues(broker)
        if reasons:
            incomplete[symbol] = reasons
            continue
        assert broker is not None
        assert broker.metadata_fingerprint is not None
        assert broker.pip_or_tick_size is not None
        base_spread = _conservative_spread(broker.observed_spreads, broker.observed_spread)
        slippage = max(broker.pip_or_tick_size, base_spread * Decimal("0.25"))
        instruments.append(
            {
                "symbol": symbol,
                "metadata_fingerprint": broker.metadata_fingerprint,
                "base_spread": base_spread,
                "spread_statistic": "MAX_OF_DQ03_75TH_PERCENTILE_AND_METADATA_SNAPSHOT",
                "observed_spread_count": len(broker.observed_spreads),
                "slippage": slippage,
                "commission_price_equivalent": Decimal("0"),
                "commission_evidence": {
                    "source_type": "AUTHORITATIVE_IG_FEE_DOCUMENTATION",
                    "source_url": IG_CASH_MARKET_COMMISSION_SOURCE,
                    "claim": (
                        "IG's cash-market CFD documentation states that forex, cash stock "
                        "indices, and spot gold and silver use an all-in dealing spread "
                        "rather than a separate commission."
                    ),
                    "applies_when": (
                        "DQ-03 classifies the contract as FX, METAL, or INDEX with expiry '-'."
                    ),
                },
                "allowed_utc_hours": list(_RESEARCH_ALLOWED_UTC_HOURS),
                "evidence_basis": (
                    "Broker fact: DQ-03 BROKER_VALIDATED observed spreads and matching "
                    "metadata fingerprint. Research assumption: slippage is max(one tick, "
                    "25% of base spread), fixed before simulation and not performance-tuned."
                ),
                "review_state": "DETERMINISTIC_RESEARCH_MODEL_NOT_EXECUTION_APPROVED",
            }
        )
    document: dict[str, object] = {
        "schema_version": "strategy-lab-sl02-cost-model/1.0",
        "execution_authority": "OFF",
        "model_status": "RESEARCH_ONLY",
        "generation_policy": {
            "spread": (
                "max(DQ-03 75th-percentile observed close spread, DQ-03 metadata spread snapshot)"
            ),
            "slippage": "max(DQ-03 pip/tick size, 25% of selected base spread)",
            "commission": (
                "zero only for documented DQ-03 cash FX, cash index, and spot metal contracts"
            ),
            "session_hours": "All UTC hours; no session filter is inferred from performance.",
            "stress_scenarios": ["base", "+25% friction", "+50% friction"],
            "commission_documentation": IG_CASH_MARKET_COMMISSION_SOURCE,
        },
        "instruments": instruments,
        "incomplete_instruments": incomplete,
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(document, sort_keys=True, indent=2, default=_json_default) + "\n",
        encoding="utf-8",
    )
    return document


def cost_evidence_preflight(
    path: Path, broker_evidence: Mapping[str, BrokerEvidence], *, expected_symbols: Iterable[str]
) -> dict[str, object]:
    """Describe missing and mismatched cost facts without silently defaulting them."""

    expected = tuple(expected_symbols)
    document = _read_document(path)
    rows = document.get("instruments") if document else None
    indexed = (
        {
            row.get("symbol"): row
            for row in rows
            if isinstance(row, dict) and isinstance(row.get("symbol"), str)
        }
        if isinstance(rows, list)
        else {}
    )
    missing: dict[str, list[str]] = {}
    mismatches: list[str] = []
    valid_count = 0
    for symbol in expected:
        row = indexed.get(symbol)
        broker = broker_evidence.get(symbol)
        parsed = _parse(row)
        if row is None:
            missing[symbol] = ["COST_EVIDENCE_ENTRY_MISSING"]
        elif parsed is None:
            missing[symbol] = ["COST_EVIDENCE_ENTRY_INVALID_OR_UNDOCUMENTED"]
        elif broker is None or parsed.metadata_fingerprint != broker.metadata_fingerprint:
            mismatches.append(symbol)
        else:
            valid_count += 1
    return {
        "cost_evidence_path": str(path),
        "cost_document_loaded": document is not None,
        "cost_entries_valid": valid_count,
        "fingerprint_mismatches": sorted(mismatches),
        "missing_cost_inputs": missing,
        "status": "COST_EVIDENCE_COMPLETE"
        if valid_count == len(expected)
        else "COST_MODEL_INCOMPLETE",
    }


def load_cost_evidence(path: Path) -> dict[str, CostEvidence]:
    """Return only complete, fingerprint-bound cost evidence from a local artifact."""

    document = _read_document(path)
    rows = document.get("instruments") if document else None
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


def _generation_issues(broker: BrokerEvidence | None) -> list[str]:
    if broker is None:
        return ["BROKER_EVIDENCE_MISSING"]
    issues: list[str] = []
    if not broker.metadata_fingerprint:
        issues.append("METADATA_FINGERPRINT_MISSING")
    if broker.data_status != "BROKER_VALIDATED":
        issues.append("BROKER_VALIDATION_REQUIRED")
    if broker.pip_or_tick_size is None or broker.pip_or_tick_size <= 0:
        issues.append("PIP_OR_TICK_SIZE_MISSING")
    if not broker.observed_spreads or any(value <= 0 for value in broker.observed_spreads):
        issues.append("OBSERVED_SPREAD_ROWS_MISSING")
    if broker.asset_class not in {"FX", "METAL", "INDEX"} or broker.expiry != "-":
        issues.append("COMMISSION_DOCUMENTATION_NOT_APPLICABLE")
    return issues


def _conservative_spread(values: tuple[Decimal, ...], snapshot: Decimal | None) -> Decimal:
    ordered = sorted(values)
    percentile_75 = ordered[max(0, (len(ordered) * 3 + 3) // 4 - 1)]
    return max(percentile_75, snapshot or Decimal("0"))


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
        or not _valid_fingerprint(fingerprint)
        or not isinstance(basis, str)
        or not basis.strip()
        or not isinstance(hours, list)
        or not all(
            isinstance(hour, int) and not isinstance(hour, bool) and 0 <= hour <= 23
            for hour in hours
        )
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
    if commission == 0 and not _valid_commission_evidence(value.get("commission_evidence")):
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


def _read_document(path: Path) -> dict[str, Any] | None:
    try:
        value: Any = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _valid_commission_evidence(value: object) -> bool:
    if not isinstance(value, dict):
        return False
    source_url = value.get("source_url")
    return (
        value.get("source_type") == "AUTHORITATIVE_IG_FEE_DOCUMENTATION"
        and isinstance(source_url, str)
        and source_url.startswith(("https://www.ig.com/", "https://deal.ig.com/"))
        and isinstance(value.get("claim"), str)
        and bool(value["claim"].strip())
    )


def _valid_fingerprint(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(char in "0123456789abcdef" for char in value.lower())
    )


def _decimal(value: object) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _json_default(value: object) -> object:
    if isinstance(value, Decimal):
        return str(value)
    raise TypeError(f"not JSON serializable: {type(value)!r}")
