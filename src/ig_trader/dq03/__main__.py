"""Command line entry points for separately resumable, read-only DQ-03 phases."""

# ruff: noqa: E501

from __future__ import annotations

import argparse
import json
import time
from datetime import timedelta
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
from src.ig_trader.dq03.models import DQ03Resolution, RequestCounters
from src.ig_trader.dq03.phases import load_phase_one_resolutions, phase_context
from src.ig_trader.dq03.rate_limit import DQ03RateLimiter
from src.ig_trader.dq03.resolver import DQ03InstrumentResolver
from src.ig_trader.session import SessionManager
from src.ig_trader.strategy_lab.models import INITIAL_INSTRUMENTS

ROOT = Path(__file__).resolve().parents[3]
DEFAULT_ARTIFACT_DIRECTORY = ROOT / "artifacts" / "dq03"


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
            results, samples = DQ03HistoryAcquirer(transport, counters).validate_verified(
                results, points=arguments.points
            )
            phase = "PHASE_2"
        else:
            streaming = _streaming_smoke(transport, results, counters)
            phase = "PHASE_3"

    paths = write_dq03_artifacts(
        arguments.output_directory,
        results,
        counters,
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
    """Use one bounded Lightstreamer session for at most three representative contracts."""

    targets = {item.symbol: item for item in results}
    selected = [targets[name] for name in ("EURUSD", "GER40", "XAUUSD") if name in targets]
    epics = [
        item.selected_epic
        for item in selected
        if item.selected_epic and item.metadata and item.metadata.streaming_prices_available
    ]
    if not epics:
        return {"status": "NOT_RUN", "reason": "No representative verified streaming contract."}
    session = transport._session  # noqa: SLF001 - authenticated transport-owned session.
    endpoint = getattr(session, "lightstreamer_endpoint", None)
    if not isinstance(endpoint, str) or not endpoint:
        return {"status": "NOT_RUN", "reason": "IG session did not return a streaming endpoint."}
    stream = DemoPriceStream(
        endpoint=endpoint,
        api_key=settings.ig_api_key,
        session=session,
        rest_demo_proven=True,
    )
    try:
        stream.connect()
        stream.subscribe_prices(epics)
        counters.streaming_subscription_count += 1
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            fresh = [stream.quote(epic, maximum_age=timedelta(seconds=5)) for epic in epics]
            if all(fresh):
                return {
                    "status": "PASS",
                    "epics": epics,
                    "quotes": [
                        {
                            "epic": quote.epic,
                            "bid": str(quote.bid),
                            "offer": str(quote.offer),
                            "timestamp": quote.observed_at.isoformat(),
                        }
                        for quote in fresh
                        if quote is not None
                    ],
                }
            time.sleep(0.2)
        return {
            "status": "FAIL",
            "reason": "No fresh BID/OFFER within ten seconds.",
            "epics": epics,
        }
    except Exception as error:  # Read-only smoke evidence must not be hidden.
        return {"status": "FAIL", "reason": str(error)[:180], "epics": epics}
    finally:
        stream.disconnect()


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
