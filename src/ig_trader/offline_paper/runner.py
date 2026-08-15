"""CLI composition root for broker-isolated OFFLINE_PAPER."""

from __future__ import annotations

import argparse
import shlex
import sys
from pathlib import Path

from src.ig_trader.offline_paper.conductor import FrozenV1Config, OfflinePaperConductor
from src.ig_trader.offline_paper.domain import RunStatus
from src.ig_trader.offline_paper.fixture import LocalFixtureData
from src.ig_trader.offline_paper.isolation import IsolationMetrics
from src.ig_trader.offline_paper.paper_broker import PaperBroker
from src.ig_trader.offline_paper.persistence import StateStoreError, TradeIntentStore
from src.ig_trader.offline_paper.reporting import build_evidence, write_evidence


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run broker-isolated exact Scalper paper E2E")
    parser.add_argument("--mode", required=True, choices=("OFFLINE_PAPER",))
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--state-directory", required=True, type=Path)
    parser.add_argument("--evidence-json", required=True, type=Path)
    parser.add_argument("--evidence-markdown", required=True, type=Path)
    return parser


def cli_main(
    argv: list[str],
    *,
    metrics: IsolationMetrics,
    repository_root: Path,
) -> int:
    args = build_parser().parse_args(argv)
    if args.evidence_json.exists() or args.evidence_markdown.exists():
        return _failure("EVIDENCE_OUTPUT_ALREADY_EXISTS", 2)
    try:
        source = LocalFixtureData(args.input)
        config = FrozenV1Config()
        intents = TradeIntentStore(args.state_directory / "trade-intents.db")
        broker = PaperBroker(
            args.state_directory / "paper-broker.db",
            account_id=source.account.account_id,
            currency=source.account.currency,
            starting_balance=source.account.starting_balance,
        )
        conductor = OfflinePaperConductor(
            market_data=source,
            historical_data=source,
            source=source,
            execution=broker,
            account=broker,
            reconciliation=broker,
            intents=intents,
            config=config,
        )
        result = conductor.run()
        if result.status is not RunStatus.COMPLETE:
            return _failure(result.reason, 4)
        restarted = OfflinePaperConductor(
            market_data=source,
            historical_data=source,
            source=source,
            execution=broker,
            account=broker,
            reconciliation=broker,
            intents=intents,
            config=config,
        ).run()
        if restarted.status is not RunStatus.COMPLETE or not restarted.idempotent_restart:
            return _failure("IDEMPOTENT_RESTART_FAILED", 5)
        command = _command(argv)
        evidence = build_evidence(
            repository_root=repository_root,
            command=command,
            result=result,
            restart_result=restarted,
            config=config,
            input_fingerprint=source.document_fingerprint,
            intents=intents,
            broker=broker,
            metrics=metrics,
        )
        if evidence["final_classification"] != "PASS":
            return _failure(str(evidence["final_classification"]), 6)
        if not write_evidence(args.evidence_json, args.evidence_markdown, evidence):
            return _failure("EVIDENCE_WRITE_FAILED", 7)
    except (OSError, ValueError, StateStoreError) as error:
        return _failure(type(error).__name__, 3)
    print("G2_OFFLINE_PAPER_COMPLETE")
    print("classification=PASS")
    print(f"intent_count={len(result.intent_ids)}")
    print("network_call_count=0")
    print("ig_rest_call_count=0")
    print("lightstreamer_connection_count=0")
    print("order_endpoint_call_count=0")
    return 0


def _command(argv: list[str]) -> str:
    rendered = " ".join(shlex.quote(item) for item in argv)
    return f"poetry run python -m src.ig_trader.offline_paper {rendered}"


def _failure(reason: str, code: int) -> int:
    print("G2_OFFLINE_PAPER_FAILED", file=sys.stderr)
    print(f"reason={reason}", file=sys.stderr)
    return code
