"""Read-only dashboard source tests for Strategy Lab artifacts."""

from __future__ import annotations

import json
from pathlib import Path

from dashboard.sources.strategy_lab import load_strategy_lab_snapshot


def test_strategy_lab_snapshot_is_safe_when_runtime_artifacts_are_absent(tmp_path: Path) -> None:
    snapshot = load_strategy_lab_snapshot(tmp_path)
    assert not snapshot.available
    assert not snapshot.entries


def test_strategy_lab_snapshot_reads_only_expected_local_evidence(tmp_path: Path) -> None:
    (tmp_path / "leaderboard.json").write_text(
        json.dumps(
            {
                "entries": [
                    {
                        "instrument": "EURUSD",
                        "asset_class": "FX",
                        "strategy": "S2",
                        "version": "1.0.0",
                        "timeframe": "5M",
                        "trades": 0,
                        "status": "COST_MODEL_INCOMPLETE",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "instrument_summary.json").write_text(
        json.dumps({"instrument_count": 1}), encoding="utf-8"
    )
    (tmp_path / "strategy_summary.json").write_text(
        json.dumps({"strategies_tested": 1}), encoding="utf-8"
    )
    snapshot = load_strategy_lab_snapshot(tmp_path)
    assert snapshot.available
    assert snapshot.entries[0]["instrument"] == "EURUSD"
    assert snapshot.instrument_summary == {"instrument_count": 1}
