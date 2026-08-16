"""Single-process cloud health runtime with no trading authority."""

from __future__ import annotations

import asyncio
import json
import signal
import sys
from contextlib import suppress
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from urllib.parse import urlsplit

import structlog

from src.ig_trader.cloud_config import CloudConfig, UnsafeCloudConfiguration
from src.ig_trader.cloud_safety import (
    CloudIsolationError,
    CloudSafetyMetrics,
    activate,
    broker_modules_loaded,
)
from src.ig_trader.execution_lease import no_execution_lease_status

logger = structlog.get_logger(__name__)

_MAX_REQUEST_BYTES = 8192
_REQUEST_TIMEOUT_SECONDS = 2.0


class ServiceState(StrEnum):
    STARTING = "STARTING"
    READY = "READY"
    DRAINING = "DRAINING"
    STOPPED = "STOPPED"


class CloudService:
    """Minimal health server for the fenced future execution worker."""

    def __init__(self, config: CloudConfig, metrics: CloudSafetyMetrics) -> None:
        self.config = config
        self.metrics = metrics
        self.lease_status = no_execution_lease_status(config.replica_instance_id)
        self.state = ServiceState.STARTING
        self.shutdown_reason = "not_requested"
        self._shutdown = asyncio.Event()
        self._server: asyncio.Server | None = None
        self._client_tasks: set[asyncio.Task[None]] = set()

    @property
    def port(self) -> int:
        if self._server is None or not self._server.sockets:
            return self.config.port
        return int(self._server.sockets[0].getsockname()[1])

    async def start(self) -> None:
        if self.state is not ServiceState.STARTING:
            raise RuntimeError("service can only be started once")
        self._server = await asyncio.start_server(
            self._handle_client,
            host=self.config.host,
            port=self.config.port,
            limit=_MAX_REQUEST_BYTES,
        )
        self.state = ServiceState.READY
        logger.info(
            "cloud_service_started",
            commit_sha=self.config.commit_sha,
            execution_mode=self.config.execution_mode,
            host=self.config.host,
            port=self.port,
            worker_enabled=False,
            worker_process_count=1,
            **self.lease_status.document(),
        )

    def request_shutdown(self, reason: str) -> None:
        if self.state in {ServiceState.DRAINING, ServiceState.STOPPED}:
            return
        self.shutdown_reason = reason
        self.state = ServiceState.DRAINING
        logger.info("cloud_shutdown_requested", reason=reason)
        self._shutdown.set()

    async def wait_for_shutdown(self) -> None:
        await self._shutdown.wait()

    async def stop(self) -> None:
        if self.state is ServiceState.STOPPED:
            return
        self.state = ServiceState.DRAINING
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

        current = asyncio.current_task()
        pending = [task for task in self._client_tasks if task is not current and not task.done()]
        if pending:
            done, still_pending = await asyncio.wait(
                pending,
                timeout=self.config.shutdown_grace_seconds,
            )
            del done
            for task in still_pending:
                task.cancel()
            if still_pending:
                await asyncio.gather(*still_pending, return_exceptions=True)

        self.state = ServiceState.STOPPED
        logger.info(
            "cloud_service_stopped",
            reason=self.shutdown_reason,
            safety={
                **self.metrics.document(),
                "broker_modules_loaded": broker_modules_loaded(),
            },
        )

    def health_document(self, *, readiness: bool) -> tuple[int, dict[str, Any]]:
        ready = self.state is ServiceState.READY
        passed = ready if readiness else self.state is not ServiceState.STOPPED
        document = {
            "service": "ig-trader",
            "check": "readiness" if readiness else "liveness",
            "status": "pass" if passed else "fail",
            "state": self.state.value,
            "timestamp": datetime.now(UTC).isoformat(),
            "release": {
                "commit_sha": self.config.commit_sha,
                "version": self.config.version,
                "image_revision": self.config.image_revision,
            },
            "execution": {
                "mode": self.config.execution_mode,
                "worker_enabled": False,
                "worker_process_count": 1,
                "replica_policy": {"min_replicas": 1, "max_replicas": 1},
                **self.lease_status.document(),
            },
            "safety": {
                **self.metrics.document(),
                "broker_modules_loaded": broker_modules_loaded(),
                "credentials_required": False,
            },
        }
        return (200 if passed else 503), document

    async def _handle_client(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        task = asyncio.current_task()
        if task is not None:
            self._client_tasks.add(task)
        try:
            try:
                raw_request = await asyncio.wait_for(
                    reader.readuntil(b"\r\n\r\n"),
                    timeout=_REQUEST_TIMEOUT_SECONDS,
                )
            except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, TimeoutError):
                await self._write_response(writer, 400, {"status": "bad_request"})
                return

            request_line = raw_request.split(b"\r\n", 1)[0]
            try:
                method_bytes, target_bytes, protocol = request_line.split(b" ", 2)
                method = method_bytes.decode("ascii")
                target = target_bytes.decode("ascii")
            except (UnicodeDecodeError, ValueError):
                await self._write_response(writer, 400, {"status": "bad_request"})
                return

            if protocol not in {b"HTTP/1.0", b"HTTP/1.1"}:
                await self._write_response(writer, 400, {"status": "bad_request"})
                return
            if method not in {"GET", "HEAD"}:
                await self._write_response(writer, 405, {"status": "method_not_allowed"})
                return

            path = urlsplit(target).path
            if path == "/health/live":
                status, document = self.health_document(readiness=False)
            elif path in {"/health", "/health/ready"}:
                status, document = self.health_document(readiness=True)
            else:
                status, document = 404, {"status": "not_found"}
            await self._write_response(writer, status, document, include_body=method == "GET")
        except (ConnectionError, BrokenPipeError):
            logger.debug("cloud_health_client_disconnected")
        finally:
            writer.close()
            with suppress(ConnectionError, BrokenPipeError):
                await writer.wait_closed()
            if task is not None:
                self._client_tasks.discard(task)

    @staticmethod
    async def _write_response(
        writer: asyncio.StreamWriter,
        status: int,
        document: dict[str, Any],
        *,
        include_body: bool = True,
    ) -> None:
        body = json.dumps(document, sort_keys=True, separators=(",", ":")).encode("utf-8")
        reason = {
            200: "OK",
            400: "Bad Request",
            404: "Not Found",
            405: "Method Not Allowed",
            503: "Service Unavailable",
        }[status]
        response_body = body if include_body else b""
        headers = (
            f"HTTP/1.1 {status} {reason}\r\n"
            "Content-Type: application/json; charset=utf-8\r\n"
            f"Content-Length: {len(body)}\r\n"
            "Cache-Control: no-store\r\n"
            "Connection: close\r\n"
            "\r\n"
        ).encode("ascii")
        writer.write(headers + response_body)
        await writer.drain()


async def run_cloud_service(config: CloudConfig, metrics: CloudSafetyMetrics) -> None:
    service = CloudService(config, metrics)
    loop = asyncio.get_running_loop()
    _install_signal_handlers(loop, service)
    await service.start()
    try:
        await service.wait_for_shutdown()
    finally:
        await service.stop()


def _install_signal_handlers(
    loop: asyncio.AbstractEventLoop,
    service: CloudService,
) -> None:
    signal_values = [signal.SIGTERM, signal.SIGINT]
    if hasattr(signal, "SIGBREAK"):
        signal_values.append(signal.SIGBREAK)
    for signal_value in signal_values:
        signal_name = signal.Signals(signal_value).name
        try:
            loop.add_signal_handler(
                signal_value,
                service.request_shutdown,
                signal_name,
            )
        except (NotImplementedError, RuntimeError):

            def handle_signal(
                _signum: int,
                _frame: object,
                reason: str = signal_name,
            ) -> None:
                loop.call_soon_threadsafe(service.request_shutdown, reason)

            signal.signal(
                signal_value,
                handle_signal,
            )


def main() -> int:
    try:
        config = CloudConfig.from_environment()
        metrics = activate(config.execution_mode)
    except (UnsafeCloudConfiguration, CloudIsolationError):
        print(
            json.dumps(
                {"event": "cloud_start_rejected", "reason": "unsafe_configuration"},
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2

    from src.ig_trader.logging_config import configure_logging

    configure_logging(config.log_level)
    try:
        asyncio.run(run_cloud_service(config, metrics))
    except KeyboardInterrupt:
        return 0
    except Exception:
        logger.exception("cloud_service_failed")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
