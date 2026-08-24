"""Versioned SL-03 research challengers; the frozen S0 benchmark is untouched."""

from __future__ import annotations

from dataclasses import replace

from src.ig_trader.strategy_lab.strategies import RuleStrategy, strategy_registry

_SL03_PARAMETERS: dict[str, tuple[tuple[tuple[str, str], ...], ...]] = {
    "S1": (
        (("fast", "5"), ("slow", "13")),
        (("fast", "13"), ("slow", "55")),
    ),
    "S2": ((("lookback", "10"),), (("lookback", "40"),)),
    "S3": (
        (("lookback", "14"), ("deviation", "1.75")),
        (("lookback", "30"), ("deviation", "1.25")),
    ),
    "S4": ((("lookback", "6"),), (("lookback", "20"),)),
    "S5": (
        (("normal_period", "12"), ("expansion", "1.35")),
        (("normal_period", "40"), ("expansion", "1.15")),
    ),
    "S6": (
        (("lookback", "6"), ("displacement", "1.2")),
        (("lookback", "20"), ("displacement", "0.8")),
    ),
    "S7": (
        (("context", "13"), ("trigger", "3")),
        (("context", "55"), ("trigger", "13")),
    ),
}

_HYPOTHESES = {
    "S1": "Compare fast and slow trend horizons to test opportunity frequency versus persistence.",
    "S2": "Compare short session-range and long rolling-range breakouts without threshold tuning.",
    "S3": "Test shorter and longer volatility-normalised mean-reversion contexts.",
    "S4": "Test short and long liquidity-sweep context windows with the same reversal rule.",
    "S5": "Test abrupt and persistent volatility-regime transitions with fixed expansion bounds.",
    "S6": "Test short and long structure windows with reciprocal displacement thresholds.",
    "S7": "Test fast and slow context/trigger pairs for cross-horizon trend agreement.",
}


def sl03_challenger_variants(strategy_id: str) -> tuple[RuleStrategy, ...]:
    """Return two coarse, documented variants per S1-S7 family.

    These are deliberately not a fine grid around an observed historical
    optimum.  S0 is returned exactly as frozen by the base registry.
    """

    base = strategy_registry()[strategy_id]
    if base.definition.baseline_only:
        return (base,)
    values = _SL03_PARAMETERS.get(strategy_id)
    if values is None:
        raise ValueError(f"No reviewed SL-03 challenger definition for {strategy_id}")
    inherited = dict(base.definition.parameters)
    return tuple(
        RuleStrategy(
            replace(
                base.definition,
                version=f"1.1.0-sl03-{strategy_id.lower()}-{number}",
                parent_version=base.definition.version,
                parameters=tuple(sorted({**inherited, **dict(parameters)}.items())),
                change_reason=(
                    f"SL-03 versioned challenger. {_HYPOTHESES[strategy_id]} "
                    "Bounds are fixed before evaluation; no result-dependent relaxation."
                ),
            ),
            base._rule,
        )
        for number, parameters in enumerate(values, start=1)
    )


def sl03_hypothesis(strategy_id: str) -> str:
    return _HYPOTHESES.get(strategy_id, "Frozen benchmark; no optimisation is permitted.")
