"""Safe local CLI for the observation-only Shadow01 monitor.

The CLI deliberately has only ``status``, explicit read-only ``probe``,
``monitor``, and ``stop``.  It has no command that creates an epoch, starts a
Demo worker, or submits broker actions.  Authentication is possible only when
the operator explicitly adds the local-Demo read-only flag to ``probe`` or
``monitor``; status and stop never construct a broker client.
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from src.ig_trader.shadow01.config import (
    DEFAULT_CONFIG_PATH,
    ShadowConfigError,
    load_config,
)
from src.ig_trader.shadow01.registry import load_verified_dq03_registry
from src.ig_trader.shadow01.runtime import Shadow01Runtime
from src.ig_trader.shadow01.storage import DEFAULT_DATABASE_PATH, ShadowTournamentStore

DEFAULT_REGISTRY_PATH = (
    DEFAULT_CONFIG_PATH.parent / "artifacts" / "dq03" / "instrument_registry.json"
)


def parser() -> argparse.ArgumentParser:
    """Build the deliberately small, local-only command surface."""

    root = argparse.ArgumentParser(description="Shadow01 observation-only local monitor")
    commands = root.add_subparsers(dest="command", required=True)
    for name in ("status", "probe", "monitor", "stop"):
        command = commands.add_parser(name)
        command.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
        command.add_argument("--database", type=Path, default=DEFAULT_DATABASE_PATH)
        command.add_argument("--registry", type=Path, default=DEFAULT_REGISTRY_PATH)
        command.add_argument("--history-cache", type=Path, default=None)
        command.add_argument("--stop-marker", type=Path, default=None)
    commands.choices["monitor"].add_argument("--poll-seconds", type=_positive_seconds, default=60.0)
    for name in ("probe", "monitor"):
        commands.choices[name].add_argument(
            "--use-local-demo-read-only",
            action="store_true",
            help=(
                "Explicitly construct the local Demo-only read adapter. "
                "Without this flag, no broker client or authentication is possible."
            ),
        )
    return root


def main(argv: Sequence[str] | None = None) -> int:
    """Run a non-authorizing local Shadow01 command."""

    args = parser().parse_args(argv)
    try:
        config = load_config(args.config)
    except ShadowConfigError as error:
        _write({"status": str(error), "execution_authority": "OFF"})
        return 2
    registry = load_verified_dq03_registry(config, args.registry)
    broker, broker_error = _explicit_local_demo_broker(
        args,
        registry_verified=registry.verified_count,
    )
    if broker_error is not None:
        _write({"status": broker_error, "execution_authority": "OFF"})
        return 2
    runtime = Shadow01Runtime(
        config=config,
        store=ShadowTournamentStore(args.database),
        registry=registry,
        broker=broker,
        history_cache_directory=args.history_cache,
        stop_marker_path=args.stop_marker,
    )
    if args.command == "status":
        _write(runtime.status())
        return 0
    if args.command == "stop":
        _write(runtime.request_stop().document())
        return 0
    if args.command == "probe":
        if broker is None:
            _write(
                {
                    "status": "SHADOW01_LOCAL_DEMO_READ_ONLY_FLAG_REQUIRED",
                    "execution_authority": "OFF",
                }
            )
            return 2
        result = runtime.pre_epoch_provider_probe(observed_at=datetime.now(UTC))
        _write(result.document())
        return 0 if result.status == "SHADOW01_PRE_EPOCH_READINESS_RECORDED" else 2
    last_status: str | None = None
    try:
        for result in runtime.monitor(poll_interval_seconds=args.poll_seconds):
            _write(result.document())
            last_status = result.status
    except KeyboardInterrupt:
        _write({"status": "SHADOW01_MONITOR_INTERRUPTED", "execution_authority": "OFF"})
        return 130
    return 0 if last_status == "SHADOW01_MONITOR_STOP_REQUESTED" else 2


def _positive_seconds(value: str) -> float:
    try:
        parsed = float(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("poll seconds must be a positive number") from error
    if parsed <= 0:
        raise argparse.ArgumentTypeError("poll seconds must be a positive number")
    return parsed


def _explicit_local_demo_broker(
    args: argparse.Namespace,
    *,
    registry_verified: int,
) -> tuple[Any | None, str | None]:
    """Build the local Demo adapter only after the explicit monitor/probe flag.

    ``status`` and ``stop`` never import the local factory.  A failed gate is
    represented only by a stable code, never a credential, endpoint response,
    or transport exception.
    """

    if getattr(args, "command", None) not in {"probe", "monitor"}:
        return None, None
    if getattr(args, "use_local_demo_read_only", False) is not True:
        return None, None
    if registry_verified != 20:
        return None, "SHADOW01_DQ03_20_PROVEN_MARKETS_REQUIRED"
    try:
        from src.ig_trader.shadow01.local_demo_read_only import (
            Shadow01LocalDemoReadOnlyError,
            Shadow01LocalDemoReadOnlyFactory,
        )

        factory = Shadow01LocalDemoReadOnlyFactory()
        readiness = factory.status()
        if not readiness.ready:
            return None, readiness.reason_code
        return factory.build(), None
    except Shadow01LocalDemoReadOnlyError:
        return None, "SHADOW01_LOCAL_DEMO_READ_ONLY_CONSTRUCTION_FAILED"
    except Exception:
        return None, "SHADOW01_LOCAL_DEMO_READ_ONLY_UNAVAILABLE"


def _write(value: object) -> None:
    print(json.dumps(value, sort_keys=True, separators=(",", ":"), default=str))


if __name__ == "__main__":
    raise SystemExit(main())
