"""Process-level network and broker-client prohibition for OFFLINE_PAPER."""

from __future__ import annotations

import os
import sys
from dataclasses import asdict, dataclass
from importlib.abc import MetaPathFinder
from importlib.machinery import ModuleSpec
from threading import Lock
from typing import Any


class OfflineIsolationError(RuntimeError):
    """Raised locally before a prohibited capability can be used."""


@dataclass
class IsolationMetrics:
    network_call_count: int = 0
    ig_rest_call_count: int = 0
    lightstreamer_connection_count: int = 0
    order_endpoint_call_count: int = 0
    credential_resolution_count: int = 0
    blocked_network_attempt_count: int = 0
    blocked_process_attempt_count: int = 0
    blocked_ig_import_attempt_count: int = 0
    blocked_lightstreamer_import_attempt_count: int = 0
    blocked_credential_import_attempt_count: int = 0
    blocked_order_import_attempt_count: int = 0

    def document(self) -> dict[str, int]:
        return asdict(self)


_BLOCKED_MODULES = {
    "src.ig_trader.config": "credential",
    "src.ig_trader.session": "ig",
    "src.ig_trader.http_client": "ig",
    "src.ig_trader.streaming": "lightstreamer",
    "src.ig_trader.execution": "order",
    "src.ig_trader.market_data": "ig",
}


class _BrokerModuleBlocker(MetaPathFinder):
    def __init__(self, metrics: IsolationMetrics) -> None:
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
        raise OfflineIsolationError(f"OFFLINE_PAPER prohibits importing {fullname}")


_activation_lock = Lock()
_active_metrics: IsolationMetrics | None = None


def activate(mode: str) -> IsolationMetrics:
    """Install irreversible guards before any trading module is loaded."""

    global _active_metrics
    if mode != "OFFLINE_PAPER":
        raise OfflineIsolationError("only OFFLINE_PAPER mode is accepted")
    unsafe_environment = {
        "PAPER_TRADING": {"0", "false", "no", "off"},
        "EXECUTION_MODE": {"live", "demo", "ig"},
        "IG_ENVIRONMENT": {"live", "demo"},
    }
    for name, rejected in unsafe_environment.items():
        value = os.environ.get(name)
        if value is not None and value.strip().casefold() in rejected:
            raise OfflineIsolationError(f"unsafe process mode in {name}")
    already_loaded = sorted(name for name in _BLOCKED_MODULES if name in sys.modules)
    if already_loaded:
        raise OfflineIsolationError("broker or credential module loaded before isolation")
    with _activation_lock:
        if _active_metrics is not None:
            return _active_metrics
        metrics = IsolationMetrics()

        def audit(event: str, args: tuple[Any, ...]) -> None:
            del args
            if event in {
                "socket.__new__",
                "socket.connect",
                "socket.connect_ex",
                "socket.getaddrinfo",
                "socket.gethostbyaddr",
                "socket.gethostbyname",
            }:
                metrics.blocked_network_attempt_count += 1
                raise OfflineIsolationError("OFFLINE_PAPER prohibits sockets")
            if event in {"subprocess.Popen", "os.system", "os.posix_spawn"}:
                metrics.blocked_process_attempt_count += 1
                raise OfflineIsolationError("OFFLINE_PAPER prohibits child processes")

        sys.addaudithook(audit)
        sys.meta_path.insert(0, _BrokerModuleBlocker(metrics))
        _active_metrics = metrics
        return metrics


def active_metrics() -> IsolationMetrics:
    if _active_metrics is None:
        raise OfflineIsolationError("OFFLINE_PAPER isolation is not active")
    return _active_metrics
