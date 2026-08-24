"""Run the cache-first SL-03 research batch; no broker network path exists here."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from src.ig_trader.sl03.runner import SL03BrokerEvidenceRequired, SL03Runner


def main() -> int:
    parser = argparse.ArgumentParser(description="Run offline SL-03 research evidence.")
    parser.add_argument("--artifacts", type=Path, required=True)
    parser.add_argument("--dq03", type=Path, required=True)
    parser.add_argument("--yahoo-cache", type=Path, required=True)
    parser.add_argument("--dukascopy-cache", type=Path, required=True)
    arguments = parser.parse_args()
    try:
        result = SL03Runner(
            artifact_directory=arguments.artifacts,
            dq03_directory=arguments.dq03,
            yahoo_cache_directory=arguments.yahoo_cache,
            dukascopy_cache_directory=arguments.dukascopy_cache,
        ).run()
    except SL03BrokerEvidenceRequired as error:
        print(json.dumps({"classification": str(error), "execution_authority": "OFF"}))
        return 2
    print(
        json.dumps(
            {
                "classification": "SL03_RESEARCH_COMPLETE",
                "combinations_scheduled": result.combinations_scheduled,
                "combinations_simulated": result.combinations_simulated,
                "parameter_sets_evaluated": result.parameter_sets_evaluated,
                "dataset_count": result.dataset_count,
                "runtime_seconds": result.runtime_seconds,
                "execution_authority": "OFF",
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
