"""Deterministic create-only JSON and Markdown evidence for G3B."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


def canonical_bytes(document: dict[str, Any]) -> bytes:
    return json.dumps(
        document,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
        ensure_ascii=False,
    ).encode("utf-8")


def write_evidence(
    json_path: Path,
    markdown_path: Path,
    document: dict[str, Any],
) -> bool:
    """Publish both artifacts create-only so an earlier run is never overwritten."""

    if json_path.exists() or markdown_path.exists():
        return False
    json_path.parent.mkdir(parents=True, exist_ok=True)
    markdown_path.parent.mkdir(parents=True, exist_ok=True)
    json_temporary = json_path.with_suffix(json_path.suffix + ".tmp")
    markdown_temporary = markdown_path.with_suffix(markdown_path.suffix + ".tmp")
    try:
        with json_temporary.open("x", encoding="utf-8", newline="\n") as stream:
            json.dump(document, stream, indent=2, sort_keys=True, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        with markdown_temporary.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(markdown(document))
            stream.flush()
            os.fsync(stream.fileno())
        os.link(json_temporary, json_path)
        os.link(markdown_temporary, markdown_path)
        return True
    except (OSError, TypeError, ValueError):
        return False
    finally:
        json_temporary.unlink(missing_ok=True)
        markdown_temporary.unlink(missing_ok=True)


def markdown(document: dict[str, Any]) -> str:
    metrics = document["metrics"]
    gap = document["gap_policy"]
    network = document["network_isolation"]
    rows = []
    for item in document["per_instrument_metrics"]:
        rows.append(
            "| {symbol} | {valid} | {invalid} | {buy} | {sell} | {candidates} | "
            "{risk_rejections} | {spread_rejections} | {trades} | {net:.4f} |".format(
                symbol=item["symbol"],
                valid=item["valid_decision_timestamps"],
                invalid=item["invalid_timestamps"],
                buy=item["signals"]["BUY"],
                sell=item["signals"]["SELL"],
                candidates=item["candidates"],
                risk_rejections=item["risk_rejections"],
                spread_rejections=item["spread_rejections"],
                trades=item["executed_paper_trades"],
                net=item["net_spread_adjusted_result_pips"],
            )
        )
    range_rows = []
    for item in document["artifact_verification"]["series"]:
        range_rows.append(
            f"| {item['symbol']} | {item['resolution_label']} | {item['candle_count']} | "
            f"{item['start_utc']} | {item['end_utc']} |"
        )
    reasons = metrics["rejection_reasons"]
    rejection_lines = (
        "\n".join(f"- `{key}`: `{value}`" for key, value in reasons.items()) or "- None"
    )
    limitations = "\n".join(f"- {value}" for value in document["limitations"])
    instrument_header = (
        "| Instrument | Valid | Invalid | BUY | SELL | Candidates | Risk rejects | "
        "Spread rejects | Trades | Net pips |"
    )
    return f"""# G3B-01 Exact Frozen Scalper Replay

- Engineering classification: `{document["engineering_replay_classification"]}`
- Performance-evidence classification: `{document["performance_evidence_classification"]}`
- Final strategy decision: `{document["final_strategy_decision"]}`
- Git commit: `{document["git_commit_sha"]}`
- Replay engine: `{document["replay_engine_version"]}`
- Replay run fingerprint: `{document["replay_run_fingerprint"]}`
- Frozen V1 configuration hash: `{document["frozen_v1"]["configuration_hash"]}`
- External package fingerprint: `{document["artifact_verification"]["package_fingerprint"]}`
- Dataset fingerprint: `{document["artifact_verification"]["dataset_fingerprint"]}`

## Replay result

| Metric | Value |
|---|---:|
| Decision timestamps | {metrics["decision_timestamps"]} |
| Valid decisions | {metrics["valid_decision_timestamps"]} |
| Invalid decisions | {metrics["invalid_timestamps"]} |
| Gap-invalidated | {metrics["gap_invalidated_timestamps"]} |
| Warm-up-invalidated | {metrics["warmup_invalidated_timestamps"]} |
| BUY signals | {metrics["signals"]["BUY"]} |
| SELL signals | {metrics["signals"]["SELL"]} |
| NO_TRADE signals | {metrics["signals"]["NO_TRADE"]} |
| Candidates | {metrics["candidates"]} |
| Risk rejections | {metrics["risk_rejections"]} |
| Spread rejections | {metrics["spread_rejections"]} |
| Accepted TradeIntents | {metrics["accepted_trade_intents"]} |
| Executed paper trades | {metrics["executed_paper_trades"]} |
| Wins / losses / breakeven | {metrics["wins"]} / {metrics["losses"]} / {metrics["breakeven"]} |
| Net spread-adjusted result (pips) | {metrics["net_spread_adjusted_result_pips"]:.4f} |
| Result (R) | {metrics["result_r_multiples"]:.4f} |
| Maximum drawdown (pips) | {metrics["maximum_drawdown_pips"]:.4f} |
| Maximum consecutive losses | {metrics["maximum_consecutive_losses"]} |
| Ambiguous intrabar events | {metrics["ambiguous_intrabar_events"]} |

## Per instrument

{instrument_header}
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
{chr(10).join(rows)}

## Exact accepted ranges

| Instrument | Timeframe | Candles | First candle start (UTC) | Last candle start (UTC) |
|---|---|---:|---|---|
{chr(10).join(range_rows)}

## Rejections

{rejection_lines}

## Authoritative gap

- Policy: `{gap["policy_id"]}`
- Decisions prevented: `{gap["decisions_prevented"]}`
- Executed trades prevented by policy: `{gap["executed_trades_prevented_by_policy"]}`
- Counterfactual trade count: `{gap["counterfactual_trade_count"]}`

## Offline safety counters

- network_call_count: `{network["network_call_count"]}`
- ig_rest_call_count: `{network["ig_rest_call_count"]}`
- lightstreamer_connection_count: `{network["lightstreamer_connection_count"]}`
- order_endpoint_call_count: `{network["order_endpoint_call_count"]}`
- credential_resolution_count: `{network["credential_resolution_count"]}`

## Interpretation

{document["performance_evidence_reason"]}

## Limitations

{limitations}
"""
