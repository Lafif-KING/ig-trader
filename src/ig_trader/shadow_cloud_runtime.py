"""Sanitized Shadow worker health projection with no execution authority."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Protocol

from src.ig_trader.shadow_execution import ExecutionMode


class ShadowHealthPort(Protocol):
    def database_available(self) -> bool: ...
    def lease_authorized(self) -> bool: ...
    def fencing_token_present(self) -> bool: ...
    def stream_connected(self) -> bool: ...
    def quote_age_seconds(self) -> float | None: ...
    def candle_ready(self) -> bool: ...
    def active_position_count(self) -> int | None: ...
    def last_cycle_status(self) -> str | None: ...


@dataclass(frozen=True)
class ShadowReadiness:
    ready: bool
    execution_mode: str
    authorized: bool
    order_authority: bool
    broker_order_call_count: int
    lease_authorized: bool
    fencing_token_present: bool
    stream_connected: bool
    last_quote_age_seconds: float | None
    candle_ready: bool
    active_shadow_position_count: int | None
    last_cycle_status: str | None
    release_sha: str


class ShadowCloudRuntime:
    def __init__(self, health: ShadowHealthPort, *, release_sha: str, quote_grace_seconds: float = 10.0) -> None:
        self._health = health
        self._release_sha = release_sha
        self._quote_grace_seconds = quote_grace_seconds

    def readiness(self) -> ShadowReadiness:
        age = self._health.quote_age_seconds()
        healthy_quote = isinstance(age, int | float) and 0 <= age <= self._quote_grace_seconds
        ready = all(
            (
                self._health.database_available(),
                self._health.lease_authorized(),
                self._health.fencing_token_present(),
                self._health.stream_connected(),
                healthy_quote,
                self._health.candle_ready(),
                self._health.active_position_count() is not None,
            )
        )
        return ShadowReadiness(
            ready=ready,
            execution_mode=ExecutionMode.SHADOW_DEMO.value,
            authorized=False,
            order_authority=False,
            broker_order_call_count=0,
            lease_authorized=self._health.lease_authorized(),
            fencing_token_present=self._health.fencing_token_present(),
            stream_connected=self._health.stream_connected(),
            last_quote_age_seconds=age,
            candle_ready=self._health.candle_ready(),
            active_shadow_position_count=self._health.active_position_count(),
            last_cycle_status=self._health.last_cycle_status(),
            release_sha=self._release_sha,
        )

    def readiness_evidence(self) -> dict[str, object]:
        return asdict(self.readiness())
