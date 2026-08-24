"""Command line entry point for the no-order SL-02 research batch."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from src.ig_trader.sl02.runner import (
    DEFAULT_ARTIFACT_DIRECTORY,
    DEFAULT_CACHE_DIRECTORY,
    DEFAULT_COST_EVIDENCE_PATH,
    DEFAULT_DQ03_DIRECTORY,
    SL02Runner,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="SL-02 broad strategy qualification (research only)")
    parser.add_argument("command", choices=("run",))
    parser.add_argument("--artifact-directory", type=Path, default=DEFAULT_ARTIFACT_DIRECTORY)
    parser.add_argument("--cache-directory", type=Path, default=DEFAULT_CACHE_DIRECTORY)
    parser.add_argument("--dq03-directory", type=Path, default=DEFAULT_DQ03_DIRECTORY)
    parser.add_argument("--cost-evidence-path", type=Path, default=DEFAULT_COST_EVIDENCE_PATH)
    parser.add_argument("--workers", type=int, default=4)
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    run = SL02Runner(
        artifact_directory=arguments.artifact_directory,
        cache_directory=arguments.cache_directory,
        dq03_directory=arguments.dq03_directory,
        cost_evidence_path=arguments.cost_evidence_path,
        max_workers=arguments.workers,
    ).run()
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
                "artifacts": {name: str(path) for name, path in run.artifact_paths.items()},
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
