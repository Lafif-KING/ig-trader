"""Offline Strategy Lab CLI."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from src.ig_trader.strategy_lab.artifacts import load_leaderboard
from src.ig_trader.strategy_lab.models import INITIAL_INSTRUMENTS, Timeframe
from src.ig_trader.strategy_lab.runner import DEFAULT_ARTIFACT_DIRECTORY, StrategyLabRunner


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Offline, broker-neutral Strategy Lab")
    subcommands = parser.add_subparsers(dest="command", required=True)
    subcommands.add_parser("list-instruments")
    run = subcommands.add_parser("run")
    run.add_argument("--instrument", required=True)
    run.add_argument("--strategy", required=True)
    run.add_argument("--timeframe", required=True, choices=[item.value for item in Timeframe])
    batch = subcommands.add_parser("batch")
    batch.add_argument("--universe", required=True, choices=["initial"])
    leaderboard = subcommands.add_parser("leaderboard")
    leaderboard.add_argument("--artifact-directory", type=Path, default=DEFAULT_ARTIFACT_DIRECTORY)
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    runner = StrategyLabRunner()
    if arguments.command == "list-instruments":
        print(
            json.dumps(
                [
                    {
                        "symbol": item.symbol,
                        "asset_class": item.asset_class.value,
                        "display_name": item.display_name,
                        "ig_epic": item.ig_epic,
                        "execution_status": item.execution_status.value,
                    }
                    for item in INITIAL_INSTRUMENTS
                ],
                indent=2,
                sort_keys=True,
            )
        )
        return 0
    if arguments.command == "leaderboard":
        leaderboard_path = arguments.artifact_directory / "leaderboard.json"
        print(json.dumps(load_leaderboard(leaderboard_path), indent=2))
        return 0
    if arguments.command == "run":
        timeframe = Timeframe(arguments.timeframe)
        run = runner.run_one(arguments.instrument, arguments.strategy, timeframe)
    else:
        run = runner.batch_initial()
    paths = runner.write(run)
    print(
        json.dumps(
            {
                "classification": "STRATEGY_LAB_OFFLINE_COMPLETE",
                "entries": len(run.entries),
                "broker_order_calls": 0,
                "network_calls": 0,
                "artifacts": {name: str(path) for name, path in paths.items()},
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
