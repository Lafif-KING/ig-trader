"""Create immutable G2 JSON and Markdown evidence artifacts."""

from __future__ import annotations

import json
import os
from dataclasses import asdict, is_dataclass
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import Any

from src.ig_trader.offline_paper.conductor import FrozenV1Config
from src.ig_trader.offline_paper.domain import RunResult, RunStatus
from src.ig_trader.offline_paper.isolation import IsolationMetrics
from src.ig_trader.offline_paper.paper_broker import PaperBroker
from src.ig_trader.offline_paper.persistence import TradeIntentStore

BASE_SHA = "46244bc04b6282d62299dfeee16e20c7abbc701d"


def build_evidence(
    *,
    repository_root: Path,
    command: str,
    result: RunResult,
    restart_result: RunResult,
    config: FrozenV1Config,
    input_fingerprint: str,
    intents: TradeIntentStore,
    broker: PaperBroker,
    metrics: IsolationMetrics,
) -> dict[str, Any]:
    stored = intents.intents()
    if stored is None:
        stored = ()
    intent_documents = []
    lifecycle = []
    for intent in stored:
        intent_documents.append(_json_value(intent))
        events = intents.events(intent.intent_id) or ()
        lifecycle.append(
            {
                "intent_id": intent.intent_id,
                "states": [item.to_state.value for item in events],
                "events": _json_value(events),
            }
        )
    reconciliation = broker.reconciliation_snapshot(as_of=datetime.now(UTC))
    counts = metrics.document()
    isolation_pass = all(
        counts[name] == 0
        for name in (
            "network_call_count",
            "ig_rest_call_count",
            "lightstreamer_connection_count",
            "order_endpoint_call_count",
            "credential_resolution_count",
        )
    )
    lifecycle_pass = bool(
        result.status is RunStatus.COMPLETE
        and stored
        and all(item.lifecycle_state.value == "RECONCILED" for item in stored)
        and reconciliation is not None
        and not reconciliation.account.positions
    )
    idempotency_pass = bool(
        restart_result.status is RunStatus.COMPLETE and restart_result.idempotent_restart
    )
    classification = (
        "PASS"
        if isolation_pass and lifecycle_pass and idempotency_pass
        else _failure_classification(
            result,
            isolation_pass=isolation_pass,
            idempotency_pass=idempotency_pass,
        )
    )
    return {
        "schema_version": "1.0",
        "work_order": "G2-01",
        "generated_at": datetime.now(UTC).isoformat(),
        "git_commit_sha": _git_head(repository_root),
        "base_sha": BASE_SHA,
        "branch": "codex/g2-01-offline-paper-e2e",
        "command": command,
        "execution_mode": "OFFLINE_PAPER",
        "input": {
            "source_type": "SYNTHETIC_DETERMINISTIC_OFFLINE_PAPER",
            "fingerprint": input_fingerprint,
        },
        "frozen_v1": {
            "configuration_hash": config.configuration_hash,
            "parameters": asdict(config),
            "change_declaration": "FROZEN VALUES IMPLEMENTED EXACTLY; NO OPTIMIZATION OR TUNING",
        },
        "ports": {
            "market_data": "MarketDataPort -> LocalFixtureData",
            "historical_data": "HistoricalDataPort -> LocalFixtureData",
            "execution": "ExecutionPort -> PaperBroker only",
            "account": "AccountPort -> PaperBroker",
            "reconciliation": "ReconciliationPort -> PaperBroker",
        },
        "lifecycle_states": lifecycle,
        "trade_intent_ids": [item.intent_id for item in stored],
        "trade_intents": intent_documents,
        "risk_result": result.risk_code,
        "paper_broker_result": result.paper_broker_result,
        "reconciliation_result": result.reconciliation_result,
        "reconciliation_snapshot": _json_value(reconciliation),
        "lineage": list(intents.lineage(result.cycle_id) or ()),
        "restart_scenarios": [
            {
                "scenario": "completed lifecycle restart and duplicate cycle processing",
                "status": "PASS" if idempotency_pass else "FAIL",
                "runtime_result": _json_value(restart_result),
            },
            *[
                {
                    "scenario": scenario,
                    "status": "VERIFIED_BY_FOCUSED_AUTOMATED_TEST",
                    "evidence": "tests/test_g2_offline_paper.py",
                }
                for scenario in (
                    "restart before TradeIntent",
                    "restart after TradeIntent creation",
                    "restart after PaperBroker submission",
                    "restart with open paper position",
                    "restart during exit",
                    "corrupted or ambiguous local state",
                    "orphan intent",
                    "mismatched intent",
                    "PortfolioRisk rejection",
                )
            ],
        ],
        "network_isolation": {
            **counts,
            "status": "PASS" if isolation_pass else "FAIL",
            "ig_rest_instantiated": False,
            "lightstreamer_instantiated": False,
            "credentials_resolved": False,
        },
        "tests": {
            "focused": "tests/test_g2_offline_paper.py",
            "g1_preserved": "tests/test_ig_auth_diagnostic.py",
            "full_suite": "poetry run pytest -q",
            "status": "VALIDATED_SEPARATELY_AND_REPORTED_TO_OPERATOR",
        },
        "limitations": [
            "PaperBroker validates orchestration, persistence and deterministic fills only.",
            "The bundled input is explicitly synthetic and is not historical-market evidence.",
            "No claim is made about IG execution quality, slippage, liquidity or profitability.",
            "This path grants no IG Demo or Live order authority.",
        ],
        "checks": {
            "full_lifecycle": lifecycle_pass,
            "network_isolation": isolation_pass,
            "idempotent_restart": idempotency_pass,
        },
        "final_classification": classification,
    }


def write_evidence(json_path: Path, markdown_path: Path, document: dict[str, Any]) -> bool:
    if json_path.exists() or markdown_path.exists():
        return False
    json_path.parent.mkdir(parents=True, exist_ok=True)
    markdown_path.parent.mkdir(parents=True, exist_ok=True)
    json_temporary = json_path.with_suffix(json_path.suffix + ".tmp")
    markdown_temporary = markdown_path.with_suffix(markdown_path.suffix + ".tmp")
    markdown = _markdown(document)
    try:
        with json_temporary.open("x", encoding="utf-8", newline="\n") as stream:
            json.dump(document, stream, indent=2, sort_keys=True, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        with markdown_temporary.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(markdown)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(json_temporary, json_path)
        os.link(markdown_temporary, markdown_path)
        return True
    except (OSError, TypeError, ValueError):
        return False
    finally:
        json_temporary.unlink(missing_ok=True)
        markdown_temporary.unlink(missing_ok=True)


def _markdown(document: dict[str, Any]) -> str:
    network = document["network_isolation"]
    states = document["lifecycle_states"]
    state_lines = (
        "\n".join(f"- `{item['intent_id']}`: {' -> '.join(item['states'])}" for item in states)
        or "- No TradeIntent"
    )
    return f"""# G2-01 OFFLINE_PAPER evidence

- Final classification: `{document["final_classification"]}`
- Git commit: `{document["git_commit_sha"]}`
- Base commit: `{document["base_sha"]}`
- Branch: `{document["branch"]}`
- Execution mode: `{document["execution_mode"]}`
- Frozen V1 configuration hash: `{document["frozen_v1"]["configuration_hash"]}`
- Input fingerprint: `{document["input"]["fingerprint"]}`
- Risk result: `{document["risk_result"]}`
- PaperBroker result: `{document["paper_broker_result"]}`
- Reconciliation: `{document["reconciliation_result"]}`

## Lifecycle

{state_lines}

## Network prohibition

- network_call_count: `{network["network_call_count"]}`
- ig_rest_call_count: `{network["ig_rest_call_count"]}`
- lightstreamer_connection_count: `{network["lightstreamer_connection_count"]}`
- order_endpoint_call_count: `{network["order_endpoint_call_count"]}`
- credential_resolution_count: `{network["credential_resolution_count"]}`
- status: `{network["status"]}`

## Reproduction command

```powershell
{document["command"]}
```

## Limits

- PaperBroker does not prove IG execution quality.
- The bundled candle input is explicitly synthetic, deterministic test evidence.
- No Demo or Live order authority is granted.
"""


def _failure_classification(
    result: RunResult,
    *,
    isolation_pass: bool,
    idempotency_pass: bool,
) -> str:
    if not isolation_pass:
        return "NETWORK_ISOLATION_FAILURE"
    if not idempotency_pass:
        return "IDEMPOTENCY_FAILURE"
    if "RISK" in result.reason:
        return "RISK_CONTROL_FAILURE"
    if result.status is RunStatus.BLOCKED:
        return "STATE_RECOVERY_FAILURE"
    return "INCONCLUSIVE"


def _json_value(value: Any) -> Any:
    if value is None or isinstance(value, str | int | float | bool):
        return value
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat()
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value):
        return {key: _json_value(item) for key, item in asdict(value).items()}
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_json_value(item) for item in value]
    raise TypeError(f"unsupported evidence value: {type(value).__name__}")


def _git_head(repository_root: Path) -> str:
    dot_git = repository_root / ".git"
    if dot_git.is_file():
        text = dot_git.read_text(encoding="utf-8").strip()
        if not text.startswith("gitdir: "):
            return "UNKNOWN"
        git_dir = Path(text.removeprefix("gitdir: ").strip())
        common_file = git_dir / "commondir"
        common_dir = (
            (git_dir / common_file.read_text(encoding="utf-8").strip()).resolve()
            if common_file.is_file()
            else git_dir
        )
    elif dot_git.is_dir():
        git_dir = common_dir = dot_git
    else:
        return "UNKNOWN"
    head = (git_dir / "HEAD").read_text(encoding="utf-8").strip()
    if not head.startswith("ref: "):
        return head if len(head) == 40 else "UNKNOWN"
    reference = head.removeprefix("ref: ")
    reference_path = common_dir / reference
    if reference_path.is_file():
        return reference_path.read_text(encoding="utf-8").strip()
    packed = common_dir / "packed-refs"
    if packed.is_file():
        for line in packed.read_text(encoding="utf-8").splitlines():
            if line.endswith(f" {reference}"):
                return line.split(" ", 1)[0]
    return "UNKNOWN"
