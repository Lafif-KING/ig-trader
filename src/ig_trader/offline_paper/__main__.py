"""Safe launcher that activates isolation before importing the conductor."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from src.ig_trader.offline_paper.isolation import OfflineIsolationError, activate


def _bootstrap_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--mode", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    try:
        bootstrap, _ = _bootstrap_parser().parse_known_args(arguments)
        metrics = activate(bootstrap.mode)
    except (OfflineIsolationError, SystemExit) as error:
        print("G2_OFFLINE_PAPER_FAILED", file=sys.stderr)
        print(f"reason={type(error).__name__}", file=sys.stderr)
        return 2

    from src.ig_trader.offline_paper.runner import cli_main

    return cli_main(arguments, metrics=metrics, repository_root=Path.cwd())


if __name__ == "__main__":
    raise SystemExit(main())
