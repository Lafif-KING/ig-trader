"""Deterministic no-network coverage for the DQ-01 harness."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from src.ig_trader.demo_qualification import run_offline_qualification

ROOT = Path(__file__).resolve().parents[1]


def test_offline_harness_reports_engineering_pass_and_zero_real_broker_calls() -> None:
    result = run_offline_qualification()

    assert result["DQ_GATE"] == "DQ01"
    assert result["classification"] == "DQ01_ENGINEERING_PASS"
    assert result["duplicate_suppressions"] == 1
    assert result["ambiguous_result_recoveries"] == 1
    assert result["legacy_execution_blocked"] is True
    assert result["real_broker_network_calls"] == 0
    assert result["real_broker_order_calls"] == 0


def test_offline_cli_does_not_need_credentials_or_network_configuration() -> None:
    environment = {
        name: value
        for name, value in os.environ.items()
        if not name.startswith("IG_") and name not in {"CST", "X_SECURITY_TOKEN"}
    }
    environment["PYTHONPATH"] = str(ROOT)
    result = subprocess.run(
        [sys.executable, "-m", "src.ig_trader.demo_qualification", "--offline"],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=15,
    )

    assert result.returncode == 0, result.stderr
    document = json.loads(result.stdout)
    assert document["classification"] == "DQ01_ENGINEERING_PASS"
    assert document["real_broker_network_calls"] == 0
    assert document["real_broker_order_calls"] == 0
