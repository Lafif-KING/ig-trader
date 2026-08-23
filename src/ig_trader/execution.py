"""Disabled compatibility boundary for the pre-DQ-01 execution engine.

The former implementation inferred market economics and directly posted an IG
order.  It is deliberately unavailable: Demo qualification must use the
separate request-only, confirmation-first domain in ``demo_execution``.
"""

from __future__ import annotations

from typing import NoReturn


class LegacyExecutionDisabled(RuntimeError):
    """A caller attempted to use the unsafe pre-qualification order path."""


class ExecutionEngine:
    """Legacy API shape retained only to fail closed before any HTTP operation."""

    def __init__(self, session: object) -> None:
        self._session = session

    def execute_trade(
        self,
        signal: object,
        size: float,
        sl_pips: int,
        tp_pips: int,
        pip_value: float | None = None,
    ) -> NoReturn:
        """Reject legacy execution without accessing the session or broker."""

        del signal, size, sl_pips, tp_pips, pip_value
        raise LegacyExecutionDisabled(
            "legacy ExecutionEngine is disabled; DQ-01 requires the demo qualification domain"
        )
