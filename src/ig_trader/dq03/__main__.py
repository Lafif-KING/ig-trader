"""Command line entry points for separately resumable, read-only DQ-03 phases."""

# ruff: noqa: E501

from __future__ import annotations

import argparse
import json
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

from src.ig_trader.config import settings
from src.ig_trader.demo_stream import DemoPriceStream
from src.ig_trader.demo_transport import (
    IG_DEMO_BASE_URL,
    IGDemoRESTTransport,
    classify_ig_demo_403,
    validate_ig_demo_endpoint,
)
from src.ig_trader.dq03.acquisition import DQ03HistoryAcquirer
from src.ig_trader.dq03.artifacts import write_dq03_artifacts
from src.ig_trader.dq03.models import DataStatus, DQ03Resolution, DQ03Status, RequestCounters
from src.ig_trader.dq03.phases import load_phase_one_resolutions, phase_context
from src.ig_trader.dq03.rate_limit import DQ03RateLimiter
from src.ig_trader.dq03.resolver import DQ03InstrumentResolver
from src.ig_trader.session import SessionManager
from src.ig_trader.strategy_lab.models import INITIAL_INSTRUMENTS

ROOT = Path(__file__).resolve().parents[3]
DEFAULT_ARTIFACT_DIRECTORY = ROOT / "artifacts" / "dq03"
STREAMING_SMOKE_TIMEOUT_SECONDS = 30


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description="DQ-03 read-only IG Demo resolver")
    commands = root.add_subparsers(dest="command", required=True)
    for command in ("resolve", "resolve-universe"):
        resolve = commands.add_parser(command, help="PHASE 1: discovery and metadata only")
        resolve.add_argument("--symbol", choices=[item.symbol for item in INITIAL_INSTRUMENTS])
        _artifact_arguments(resolve)
    history = commands.add_parser(
        "history", help="PHASE 2: bounded history for prior VERIFIED rows"
    )
    _artifact_arguments(history)
    history.add_argument("--points", type=int, default=20)
    stream = commands.add_parser("streaming-smoke", help="PHASE 3: bounded streaming smoke")
    _artifact_arguments(stream)
    return root


def _artifact_arguments(command: argparse.ArgumentParser) -> None:
    command.add_argument("--output-directory", type=Path, default=DEFAULT_ARTIFACT_DIRECTORY)


def main(argv: list[str] | None = None) -> int:
    arguments = parser().parse_args(argv)
    counters = RequestCounters()
    limiter = DQ03RateLimiter(counters)
    transport, account_id = _read_only_preflight(counters, limiter)
    context = phase_context(account_id)
    samples = ()
    streaming = None

    if arguments.command in {"resolve", "resolve-universe"}:
        instruments = (
            tuple(item for item in INITIAL_INSTRUMENTS if item.symbol == arguments.symbol)
            if arguments.symbol
            else INITIAL_INSTRUMENTS
        )
        results = DQ03InstrumentResolver(transport, counters=counters).resolve_universe(instruments)
        phase = "PHASE_1"
    else:
        results = load_phase_one_resolutions(arguments.output_directory, context)
        if arguments.command == "history":
            results, samples = DQ03HistoryAcquirer(
                transport,
                counters,
                snapshot_time_utc_offset_hours=transport.session_timezone_offset_hours,
            ).validate_verified(results, points=arguments.points)
            phase = "PHASE_2"
        else:
            streaming = _streaming_smoke(transport, results, counters)
            phase = "PHASE_3"

    paths = write_dq03_artifacts(
        arguments.output_directory,
        results,
        counters,
        history_samples=samples,
        streaming_result=streaming,
        phase=phase,
        run_context=context,
    )
    _print_summary(results)
    print(
        json.dumps(
            {
                "classification": "DQ03_READ_ONLY_COMPLETE",
                "phase": phase,
                "artifacts": {name: str(path) for name, path in paths.items()},
                "history_samples": [sample.document() for sample in samples],
                "request_counts": counters.document(),
                "demo_create_calls": 0,
                "demo_close_calls": 0,
                "execution_authority": "OFF",
            },
            sort_keys=True,
        )
    )
    return 0


def _read_only_preflight(
    counters: RequestCounters, limiter: DQ03RateLimiter
) -> tuple[IGDemoRESTTransport, str]:
    """Prove Demo identity and empty positions before any DQ-03 market reads."""

    base_url = validate_ig_demo_endpoint(settings.ig_base_url or IG_DEMO_BASE_URL)
    expected = settings.ig_expected_demo_account_id.strip()
    if not expected:
        raise RuntimeError("IG_EXPECTED_DEMO_ACCOUNT_ID is required for a real DQ-03 run")
    session = SessionManager(
        request_observer=limiter.before_request,
        response_error_observer=lambda response: counters.record_403(
            classify_ig_demo_403(response)
        ),
    )
    transport = IGDemoRESTTransport(
        session=session,
        base_url=base_url,
        error_observer=lambda error: counters.record_403(error.classification),
    )
    account = transport.get_account()
    if account.account_id != expected:
        raise RuntimeError("IG Demo account identity cannot be proven")
    positions = transport.list_position_details()
    if positions:
        raise RuntimeError("IG Demo account has open positions; DQ-03 resolution is blocked")
    counters.preflight_request_count = counters.observed_non_trading_request_count
    return transport, account.account_id


def _streaming_smoke(
    transport: IGDemoRESTTransport,
    results: tuple[DQ03Resolution, ...],
    counters: RequestCounters,
) -> dict[str, object]:
    """Use one bounded Lightstreamer session for the two verified smoke contracts."""

    targets = {item.symbol: item for item in results}
    selected = [
        targets[name]
        for name in ("EURUSD", "XAUUSD")
        if name in targets
        and targets[name].classification is DQ03Status.VERIFIED
        and targets[name].data_status is DataStatus.BROKER_VALIDATED
    ]
    missing_targets = sorted({"EURUSD", "XAUUSD"} - {item.symbol for item in selected})
    if missing_targets:
        return {
            "status": "NOT_RUN",
            "reason": "Required broker-validated smoke target is unavailable.",
            "missing_symbols": missing_targets,
            "server_endpoint_present": False,
            "connect_status": "NOT_ATTEMPTED",
            "subscription_status": "NOT_ATTEMPTED",
            "fresh_quote_count": 0,
            "disconnect_status": "NOT_ATTEMPTED",
        }
    epics = [
        item.selected_epic
        for item in selected
        if item.selected_epic and item.metadata and item.metadata.streaming_prices_available
    ]
    if len(epics) != 2:
        return {
            "status": "NOT_RUN",
            "reason": "Required target has no proven streaming metadata.",
            "server_endpoint_present": False,
            "connect_status": "NOT_ATTEMPTED",
            "subscription_status": "NOT_ATTEMPTED",
            "fresh_quote_count": 0,
            "disconnect_status": "NOT_ATTEMPTED",
        }
    session = transport._session  # noqa: SLF001 - authenticated transport-owned session.
    endpoint = getattr(session, "lightstreamer_endpoint", None)
    evidence: dict[str, object] = {
        "status": "FAIL",
        "server_endpoint_present": isinstance(endpoint, str) and bool(endpoint),
        "connect_status": "NOT_ATTEMPTED",
        "subscription_status": "NOT_ATTEMPTED",
        "subscribed_epics": epics,
        "fresh_quote_count": 0,
        "quotes": [],
        "timeout_seconds": STREAMING_SMOKE_TIMEOUT_SECONDS,
        "disconnect_status": "NOT_ATTEMPTED",
    }
    if not isinstance(endpoint, str) or not endpoint:
        evidence.update(status="NOT_RUN", reason="IG session did not return a streaming endpoint.")
        return evidence
    stream: DemoPriceStream | None = None
    try:
        stream = DemoPriceStream(
            endpoint=endpoint,
            session=session,
            rest_demo_proven=True,
        )
        stream.connect()
        evidence["connect_status"] = "REQUESTED"
        stream.subscribe_prices(epics)
        counters.streaming_subscription_count += 1
        evidence["subscription_status"] = "REQUESTED"
        deadline = time.monotonic() + STREAMING_SMOKE_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            evidence["connect_status"] = stream.connection_status
            if stream.connection_error is not None:
                evidence["reason"] = (
                    f"Lightstreamer server connection failed (code {stream.connection_error})."
                )
                break
            if stream.subscription_error is not None:
                evidence.update(
                    subscription_status="SERVER_REJECTED",
                    reason=f"Lightstreamer rejected the subscription (code {stream.subscription_error}).",
                )
                break
            fresh = [stream.quote(epic, maximum_age=timedelta(seconds=5)) for epic in epics]
            if stream.connection_confirmed and stream.subscription_confirmed and all(fresh):
                quotes = [quote for quote in fresh if quote is not None]
                evidence.update(
                    status="PASS",
                    subscription_status="CONFIRMED",
                    fresh_quote_count=len(quotes),
                    quotes=[
                        {
                            "epic": quote.epic,
                            "bid": str(quote.bid),
                            "offer": str(quote.offer),
                            "timestamp_utc": _format_utc_timestamp(quote.observed_at),
                            "age_seconds": round(
                                max(0.0, (datetime.now(UTC) - quote.observed_at).total_seconds()), 3
                            ),
                        }
                        for quote in quotes
                    ],
                )
                break
            time.sleep(0.2)
        if evidence["status"] != "PASS":
            evidence["connect_status"] = stream.connection_status
            if not stream.connection_confirmed and stream.connection_error is None:
                evidence["reason"] = (
                    "Lightstreamer did not confirm a server connection within "
                    f"{STREAMING_SMOKE_TIMEOUT_SECONDS} seconds."
                )
            elif stream.subscription_confirmed:
                evidence["subscription_status"] = "CONFIRMED"
                evidence.setdefault(
                    "reason",
                    f"No fresh BID/OFFER within {STREAMING_SMOKE_TIMEOUT_SECONDS} seconds.",
                )
            elif stream.subscription_error is None:
                evidence["reason"] = (
                    "Lightstreamer did not confirm the subscription within "
                    f"{STREAMING_SMOKE_TIMEOUT_SECONDS} seconds."
                )
    except Exception as error:  # Read-only smoke evidence must not be hidden.
        evidence["reason"] = str(error)[:180]
    finally:
        if stream is not None:
            try:
                stream.disconnect()
                evidence["disconnect_status"] = "DISCONNECTED"
            except Exception as error:  # The smoke result must retain disconnect failure evidence.
                evidence["disconnect_status"] = "FAIL"
                evidence.setdefault("reason", str(error)[:180])
    return evidence


def _format_utc_timestamp(value: datetime) -> str:
    """Render a broker event timestamp as UTC without depending on callback-local tzinfo."""

    seconds = value.timestamp()
    whole_seconds = int(seconds)
    microseconds = round((seconds - whole_seconds) * 1_000_000)
    if microseconds == 1_000_000:
        whole_seconds += 1
        microseconds = 0
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(whole_seconds)) + (
        f".{microseconds:06d}Z"
    )


def _print_summary(results: tuple[DQ03Resolution, ...]) -> None:
    print(
        "symbol\tclassification\tepic\tmissing_fields\tcurrency\tminimum_size\tminimum_stop\tmarket_status\tstreaming"
    )
    for item in results:
        metadata = item.metadata
        print(
            "\t".join(
                (
                    item.symbol,
                    item.classification.value,
                    item.selected_epic or "—",
                    ",".join(item.missing_fields) or "—",
                    metadata.currency if metadata and metadata.currency else "—",
                    str(metadata.minimum_deal_size)
                    if metadata and metadata.minimum_deal_size
                    else "—",
                    str(metadata.minimum_stop_distance)
                    if metadata and metadata.minimum_stop_distance
                    else "—",
                    metadata.market_status if metadata and metadata.market_status else "—",
                    str(metadata.streaming_prices_available) if metadata else "—",
                )
            )
        )


if __name__ == "__main__":
    raise SystemExit(main())
