"""Install irreversible offline guards before importing replay dependencies."""

from __future__ import annotations

import sys
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    from src.ig_trader.offline_paper.isolation import OfflineIsolationError, activate

    try:
        metrics = activate("OFFLINE_PAPER")
    except OfflineIsolationError as error:
        print("G3B_EXACT_REPLAY_FAILED", file=sys.stderr)
        print(f"reason={type(error).__name__}", file=sys.stderr)
        return 2
    from src.ig_trader.g3b_replay.runner import cli_main

    return cli_main(
        list(sys.argv[1:] if argv is None else argv),
        metrics=metrics,
        repository_root=Path(__file__).resolve().parents[3],
    )


if __name__ == "__main__":
    raise SystemExit(main())
