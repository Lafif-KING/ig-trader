"""Dependency-free validation for the committed dashboard gate file."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dashboard.sources.project import ProjectGateValidationError, load_project_gates


def main() -> int:
    try:
        gates = load_project_gates()
    except ProjectGateValidationError as error:
        print(f"project gates validation failed: {error}")
        return 1
    print(f"project gates validation passed; gates={len(gates)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
