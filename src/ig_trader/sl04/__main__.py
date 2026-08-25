"""Run the offline-local SL-04 deep-history replay."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from src.ig_trader.sl03.runner import SL03BrokerEvidenceRequired

from .runner import SL04Runner


def main() -> int:
    parser = argparse.ArgumentParser(description="Run offline-local SL-04 deep structured history.")
    parser.add_argument("--artifacts", type=Path, required=True)
    parser.add_argument("--dq03", type=Path, required=True)
    parser.add_argument("--yahoo-cache", type=Path, required=True)
    parser.add_argument("--local-dukascopy", type=Path, required=True)
    parser.add_argument("--dukascopy-export", type=Path, required=True)
    parser.add_argument("--previous-artifacts", type=Path, required=True)
    arguments = parser.parse_args()
    try:
        result = SL04Runner(
            artifact_directory=arguments.artifacts,
            dq03_directory=arguments.dq03,
            yahoo_cache_directory=arguments.yahoo_cache,
            local_data_directory=arguments.local_dukascopy,
            export_directory=arguments.dukascopy_export,
            previous_artifact_directory=arguments.previous_artifacts,
        ).run()
    except SL03BrokerEvidenceRequired as error:
        print(json.dumps({"classification": str(error), "execution_authority": "OFF"}))
        return 3
    print(
        json.dumps(
            {
                "classification": "SL04_DEEP_STRUCTURED_HISTORY_REPLAY_COMPLETE",
                "combinations_scheduled": result.combinations_scheduled,
                "combinations_simulated": result.combinations_simulated,
                "dataset_count": result.dataset_count,
                "runtime_seconds": result.runtime_seconds,
                "mode": "OFFLINE_LOCAL_ONLY",
                "network_acquisition_calls": 0,
                "local_files_accepted": result.local_files_accepted,
                "local_files_rejected": result.local_files_rejected,
                "execution_authority": "OFF",
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
