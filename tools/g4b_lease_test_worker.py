"""Isolated PostgreSQL lease contender used only by the bounded CI proof."""

from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path

import psycopg

from src.ig_trader.execution_lease import (
    EXECUTION_LEASE_NAME,
    LeaseError,
    PostgresExecutionLeaseStore,
)

_POSTGRES_DSN_ENV = "TEST_POSTGRES_DSN"
_POSTGRES_TEST_ROLE = "g4b02b1_runtime_test"


def _connection(dsn: str) -> psycopg.Connection[tuple[object, ...]]:
    return psycopg.connect(
        psycopg.conninfo.make_conninfo(
            dsn,
            user=_POSTGRES_TEST_ROLE,
            connect_timeout=5,
            options="-c statement_timeout=5000 -c lock_timeout=3000",
        )
    )


def _write_result(path: Path, document: dict[str, str | int | None]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(document, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--instance-id", required=True)
    parser.add_argument("--ttl-seconds", required=True, type=float)
    parser.add_argument("--result-file", required=True, type=Path)
    parser.add_argument("--release-file", required=True, type=Path)
    arguments = parser.parse_args()
    dsn = os.environ.get(_POSTGRES_DSN_ENV, "").strip()
    if not dsn:
        _write_result(arguments.result_file, {"status": "DATABASE_UNAVAILABLE"})
        return 2

    store = PostgresExecutionLeaseStore(lambda: _connection(dsn))
    try:
        lease = store.acquire(
            EXECUTION_LEASE_NAME,
            arguments.instance_id,
            arguments.ttl_seconds,
        )
    except LeaseError:
        _write_result(arguments.result_file, {"status": "DATABASE_UNAVAILABLE"})
        return 2

    _write_result(
        arguments.result_file,
        {
            "instance": arguments.instance_id,
            "role": "LEADER" if lease else "STANDBY",
            "status": "PASS",
            "token": lease.fencing_token if lease else None,
        },
    )
    if lease is not None:
        deadline = time.monotonic() + 30
        while not arguments.release_file.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
