"""Command line entry point for the no-order SL-02 research batch."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from src.ig_trader.sl02.costs import cost_evidence_preflight, generate_research_cost_model
from src.ig_trader.sl02.evidence import preflight_dq03_evidence, write_preflight_report
from src.ig_trader.sl02.runner import (
    DEFAULT_ARTIFACT_DIRECTORY,
    DEFAULT_CACHE_DIRECTORY,
    DEFAULT_COST_EVIDENCE_PATH,
    DEFAULT_DQ03_DIRECTORY,
    SL02BrokerEvidenceRequired,
    SL02Runner,
    SL02_VERIFIED_SYMBOLS,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="SL-02 broad strategy qualification (research only)")
    parser.add_argument("command", choices=("evidence-preflight", "generate-cost-model", "run"))
    parser.add_argument("--artifact-directory", type=Path, default=DEFAULT_ARTIFACT_DIRECTORY)
    parser.add_argument("--cache-directory", type=Path, default=DEFAULT_CACHE_DIRECTORY)
    parser.add_argument("--dq03-directory", type=Path, default=DEFAULT_DQ03_DIRECTORY)
    parser.add_argument("--cost-evidence-path", type=Path, default=DEFAULT_COST_EVIDENCE_PATH)
    parser.add_argument("--workers", type=int, default=4)
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    preflight = preflight_dq03_evidence(
        arguments.dq03_directory, expected_symbols=SL02_VERIFIED_SYMBOLS
    )
    cost_preflight = cost_evidence_preflight(
        arguments.cost_evidence_path,
        preflight.evidence,
        expected_symbols=SL02_VERIFIED_SYMBOLS,
    )
    report = {**preflight.document(), "cost_evidence": cost_preflight}
    report_path = write_preflight_report(
        arguments.artifact_directory / "sl02_evidence_preflight.json", report
    )
    if arguments.command == "evidence-preflight":
        print(json.dumps({"classification": report["status"], "report_path": str(report_path)}, sort_keys=True))
        return 0 if preflight.broker_ready else 2
    if arguments.command == "generate-cost-model":
        if not preflight.broker_ready:
            print(
                json.dumps(
                    {"classification": "SL02_BROKER_EVIDENCE_REQUIRED", "report_path": str(report_path)},
                    sort_keys=True,
                )
            )
            return 2
        model = generate_research_cost_model(
            preflight.evidence,
            expected_symbols=SL02_VERIFIED_SYMBOLS,
            output_path=arguments.cost_evidence_path,
        )
        cost_report = cost_evidence_preflight(
            arguments.cost_evidence_path,
            preflight.evidence,
            expected_symbols=SL02_VERIFIED_SYMBOLS,
        )
        report = {**preflight.document(), "cost_evidence": cost_report}
        report_path = write_preflight_report(
            arguments.artifact_directory / "sl02_evidence_preflight.json", report
        )
        print(
            json.dumps(
                {
                    "classification": "SL02_RESEARCH_COST_MODEL_GENERATED",
                    "cost_model_path": str(arguments.cost_evidence_path),
                    "cost_entries": len(model["instruments"]),
                    "incomplete_entries": len(model["incomplete_instruments"]),
                    "report_path": str(report_path),
                    "execution_authority": "OFF",
                },
                sort_keys=True,
            )
        )
        return 0
    try:
        run = SL02Runner(
            artifact_directory=arguments.artifact_directory,
            cache_directory=arguments.cache_directory,
            dq03_directory=arguments.dq03_directory,
            cost_evidence_path=arguments.cost_evidence_path,
            max_workers=arguments.workers,
        ).run()
    except SL02BrokerEvidenceRequired as error:
        print(
            json.dumps(
                {"classification": "SL02_BROKER_EVIDENCE_REQUIRED", "report_path": str(error.report_path)},
                sort_keys=True,
            )
        )
        return 2
    print(
        json.dumps(
            {
                "classification": "SL02_RESEARCH_COMPLETE",
                "execution_authority": "OFF",
                "ig_create_calls": 0,
                "ig_close_calls": 0,
                "live_calls": 0,
                "combinations_scheduled": run.combinations_scheduled,
                "combinations_simulated": run.combinations_simulated,
                "parameter_sets_evaluated": run.parameter_sets_evaluated,
                "dataset_count": run.dataset_count,
                "runtime_seconds": run.runtime_seconds,
                "evidence_preflight": str(run.evidence_preflight_path),
                "artifacts": {name: str(path) for name, path in run.artifact_paths.items()},
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
