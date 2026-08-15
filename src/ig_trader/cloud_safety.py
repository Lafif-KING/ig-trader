"""Irreversible process guards for the cloud NO_EXECUTION composition."""

from __future__ import annotations

import socket
import sys
from dataclasses import asdict, dataclass
from importlib.abc import MetaPathFinder
from importlib.machinery import ModuleSpec
from threading import Lock
from typing import Any

from src.ig_trader.cloud_config import NO_EXECUTION


class CloudIsolationError(RuntimeError):
    """Raised locally before a prohibited cloud capability can be used."""


@dataclass
class CloudSafetyMetrics:
    """Successful-call counters and separate blocked-attempt counters."""

    network_call_count: int = 0
    ig_rest_call_count: int = 0
    lightstreamer_connection_count: int = 0
    order_endpoint_call_count: int = 0
    credential_resolution_count: int = 0
    loopback_connect_count: int = 0
    blocked_network_attempt_count: int = 0
    blocked_ig_import_attempt_count: int = 0
    blocked_lightstreamer_import_attempt_count: int = 0
    blocked_credential_import_attempt_count: int = 0
    blocked_order_import_attempt_count: int = 0

    def document(self) -> dict[str, int]:
        return asdict(self)


_BLOCKED_MODULES = {
    "src.ig_trader.config": "credential",
    "src.ig_trader.execution": "order",
    "src.ig_trader.http_client": "ig",
    "src.ig_trader.market_data": "ig",
    "src.ig_trader.session": "ig",
    "src.ig_trader.streaming": "lightstreamer",
}


class _BrokerModuleBlocker(MetaPathFinder):
    def __init__(self, metrics: CloudSafetyMetrics) -> None:
        self.metrics = metrics

    def find_spec(
        self,
        fullname: str,
        path: object = None,
        target: object = None,
    ) -> ModuleSpec | None:
        del path, target
        category = _BLOCKED_MODULES.get(fullname)
        if category is None:
            return None
        if category == "credential":
            self.metrics.blocked_credential_import_attempt_count += 1
        elif category == "lightstreamer":
            self.metrics.blocked_lightstreamer_import_attempt_count += 1
        elif category == "order":
            self.metrics.blocked_order_import_attempt_count += 1
        else:
            self.metrics.blocked_ig_import_attempt_count += 1
        raise CloudIsolationError(f"NO_EXECUTION prohibits importing {fullname}")


_activation_lock = Lock()
_active_metrics: CloudSafetyMetrics | None = None


def activate(mode: str) -> CloudSafetyMetrics:
    """Install guards before the health server starts."""

    global _active_metrics
    if mode != NO_EXECUTION:
        raise CloudIsolationError("only NO_EXECUTION is accepted")
    already_loaded = sorted(name for name in _BLOCKED_MODULES if name in sys.modules)
    if already_loaded:
        raise CloudIsolationError("broker or credential module loaded before isolation")

    with _activation_lock:
        if _active_metrics is not None:
            return _active_metrics
        metrics = CloudSafetyMetrics()
        original_socket = socket.socket

        class GuardedSocket(original_socket):
            def connect(self, address: Any) -> None:
                if not _is_loopback_address(address):
                    metrics.blocked_network_attempt_count += 1
                    raise CloudIsolationError("NO_EXECUTION prohibits outbound connections")
                metrics.loopback_connect_count += 1
                super().connect(address)

            def connect_ex(self, address: Any) -> int:
                if not _is_loopback_address(address):
                    metrics.blocked_network_attempt_count += 1
                    raise CloudIsolationError("NO_EXECUTION prohibits outbound connections")
                metrics.loopback_connect_count += 1
                return super().connect_ex(address)

        def audit(event: str, args: tuple[Any, ...]) -> None:
            if event in {
                "http.client.connect",
                "socket.connect",
                "socket.connect_ex",
            }:
                if _is_loopback_connection(event, args):
                    metrics.loopback_connect_count += 1
                    return
                metrics.blocked_network_attempt_count += 1
                raise CloudIsolationError("NO_EXECUTION prohibits outbound connections")

        sys.addaudithook(audit)
        socket.socket = GuardedSocket
        sys.meta_path.insert(0, _BrokerModuleBlocker(metrics))
        _active_metrics = metrics
        return metrics


def broker_modules_loaded() -> bool:
    """Return whether a prohibited broker module entered this process."""

    return any(name in sys.modules for name in _BLOCKED_MODULES)


def _is_loopback_connection(event: str, args: tuple[Any, ...]) -> bool:
    if len(args) >= 2 and (event.startswith("socket.") or event == "http.client.connect"):
        address = args[1]
    else:
        return False
    return _is_loopback_address(address)


def _is_loopback_address(address: Any) -> bool:
    host = address[0] if isinstance(address, tuple) and address else address
    return host in {"127.0.0.1", "::1", "localhost"}
