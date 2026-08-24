"""Central, conservative rolling allowance control for DQ-03 read-only REST calls."""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
from time import monotonic, sleep

from src.ig_trader.dq03.models import RequestCounters


class DQ03RateLimiter:
    """Keep DQ-03 below 25 non-trading REST calls in every rolling minute.

    The observer is intentionally installed beneath the resolver, at the session
    boundary, so login and preflight reads are counted as well as DQ-03 phases.
    It waits once for the earliest expiring slot and never retries a response.
    """

    def __init__(
        self,
        counters: RequestCounters,
        *,
        maximum_requests: int = 25,
        window_seconds: float = 60.0,
        clock: Callable[[], float] = monotonic,
        sleeper: Callable[[float], None] = sleep,
    ) -> None:
        if maximum_requests < 1 or window_seconds <= 0:
            raise ValueError("DQ-03 rolling request policy must be positive")
        self._counters = counters
        self.maximum_requests = maximum_requests
        self.window_seconds = window_seconds
        self._clock = clock
        self._sleeper = sleeper
        self._timestamps: deque[float] = deque()

    def before_request(self, _method: str, _endpoint: str) -> None:
        """Wait for a slot before one physical, non-trading REST request."""

        now = self._clock()
        self._discard_expired(now)
        if len(self._timestamps) >= self.maximum_requests:
            wait_seconds = max(0.0, self._timestamps[0] + self.window_seconds - now)
            if wait_seconds > 0:
                self._sleeper(wait_seconds)
                self._counters.rate_limit_wait_count += 1
                self._counters.rate_limit_wait_seconds += wait_seconds
            now = self._clock()
            self._discard_expired(now)
        if len(self._timestamps) >= self.maximum_requests:
            # A non-advancing injected test clock is not permission to burst.
            raise RuntimeError("DQ-03 rolling rate-limit slot did not become available")
        self._timestamps.append(now)
        self._counters.observed_non_trading_request_count += 1

    def _discard_expired(self, now: float) -> None:
        while self._timestamps and now - self._timestamps[0] >= self.window_seconds:
            self._timestamps.popleft()
