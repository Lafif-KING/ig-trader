"""CLI composition for the broker-isolated exact replay."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from src.ig_trader.g3b_replay.account_state import (
    G2QualificationState,
    QualificationAccountStateGap,
)
from src.ig_trader.g3b_replay.data import (
    ArtifactIntegrityError,
    verify_and_load_package,
)
from src.ig_trader.g3b_replay.engine import ExactReplayEngine
from src.ig_trader.g3b_replay.reporting import canonical_bytes, write_evidence
from src.ig_trader.offline_paper.isolation import IsolationMetrics


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run exact frozen Scalper replay offline")
    parser.add_argument("--mode", required=True, choices=("OFFLINE_REPLAY",))
    parser.add_argument("--package-root", required=True, type=Path)
    parser.add_argument("--qualification-fixture", required=True, type=Path)
    parser.add_argument("--state-root", required=True, type=Path)
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
    if args.state_root.exists():
        return _failure("PAPER_STATE_OUTPUT_ALREADY_EXISTS", 2)
    try:
        dataset = verify_and_load_package(args.package_root)
        qualification_state = G2QualificationState.load(args.qualification_fixture)
        commit_sha = git_head(repository_root)
        first_broker = qualification_state.create_paper_broker(
            args.state_root / "first" / "paper-broker.db"
        )
        first = ExactReplayEngine(
            dataset,
            commit_sha=commit_sha,
            network_metrics=metrics.document(),
            qualification_state=qualification_state,
            paper_broker=first_broker,
        ).run()
        second_broker = qualification_state.create_paper_broker(
            args.state_root / "second" / "paper-broker.db"
        )
        second = ExactReplayEngine(
            dataset,
            commit_sha=commit_sha,
            network_metrics=metrics.document(),
            qualification_state=qualification_state,
            paper_broker=second_broker,
        ).run()
        if canonical_bytes(first) != canonical_bytes(second):
            return _failure("NON_DETERMINISTIC", 5)
        safety_names = (
            "network_call_count",
            "ig_rest_call_count",
            "lightstreamer_connection_count",
            "order_endpoint_call_count",
            "credential_resolution_count",
        )
        if any(metrics.document()[name] != 0 for name in safety_names):
            return _failure("NETWORK_OR_BROKER_ISOLATION_FAILURE", 6)
        if not write_evidence(args.evidence_json, args.evidence_markdown, first):
            return _failure("EVIDENCE_WRITE_FAILED", 7)
    except ArtifactIntegrityError:
        return _failure("ARTIFACT_INTEGRITY_FAILURE", 3)
    except QualificationAccountStateGap:
        return _failure("QUALIFICATION_ACCOUNT_STATE_GAP", 8)
    except (OSError, TypeError, ValueError):
        return _failure("REPLAY_INTEGRITY_FAILURE", 4)
    print("G3B_ACCOUNT_STATE_REPLAY_COMPLETE")
    print(f"engineering_classification={first['engineering_replay_classification']}")
    print(f"performance_classification={first['performance_evidence_classification']}")
    print(f"final_recommendation={first['final_recommendation']}")
    print(f"replay_run_fingerprint={first['replay_run_fingerprint']}")
    for name in (
        "network_call_count",
        "ig_rest_call_count",
        "lightstreamer_connection_count",
        "order_endpoint_call_count",
        "credential_resolution_count",
    ):
        print(f"{name}={metrics.document()[name]}")
    return 0


def git_head(repository_root: Path) -> str:
    """Read the worktree HEAD without spawning a prohibited child process."""

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


def _failure(reason: str, code: int) -> int:
    print("G3B_EXACT_REPLAY_FAILED", file=sys.stderr)
    print(f"reason={reason}", file=sys.stderr)
    return code
