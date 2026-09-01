"""Non-persisting, supplied-facts diagnostic for the Shadow01 policy surface.

This module is intentionally narrower than :mod:`shadow01.runtime`: it does
not acquire broker data, open a database, create an epoch, materialize an
append-only record, resolve an outcome, or run a monitor.  A caller supplies
the already-read-only, completed information set and receives in-memory policy
recommendations explicitly marked as non-prospective diagnostic output.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from math import isfinite

from src.ig_trader.shadow01.config import ShadowTournamentConfig
from src.ig_trader.shadow01.engines import (
    CostInputs,
    CrossAssetInput,
    FundamentalInputs,
    assess_c1_cost,
    assess_q1_quality,
    build_f1_context,
    compute_technical_state,
    evaluate_m1_reversion,
    evaluate_t1_trend,
    evaluate_x1_context,
)
from src.ig_trader.shadow01.live_quote import ShadowLiveQuote
from src.ig_trader.shadow01.models import (
    CostContext,
    CrossAssetOpinion,
    DailyBar,
    FundamentalContext,
    MarketSpec,
    PolicyId,
    QualityAssessment,
    ReversionOpinion,
    TechnicalOpinion,
    TechnicalState,
    fingerprint,
    require_utc,
)
from src.ig_trader.shadow01.policies import PolicyRecommendation, evaluate_policies
from src.ig_trader.shadow01.read_only_broker import (
    ReadOnlyBrokerRequestCounters,
    Shadow01ReadOnlyBroker,
)
from src.ig_trader.shadow01.registry import (
    ShadowMarketRegistry,
    ShadowRegistryError,
    require_exact_twenty,
)

_DRY_STATUS = "DRY_RUN_NON_PROSPECTIVE"
_FINGERPRINT_CHARACTERS = frozenset("0123456789abcdef")


class ShadowDrySnapshotError(RuntimeError):
    """A supplied diagnostic fact cannot safely enter the dry policy surface."""


@dataclass(frozen=True)
class DrySnapshotMarketInput:
    """One immutable, already-read-only completed information set.

    ``input_data_fingerprint`` is the canonical identity of the supplied raw
    history or equivalent upstream evidence.  The diagnostic never writes it
    to a cache or database and never creates a replacement fingerprint from
    inferred data.
    """

    instrument: str
    epic: str
    completed_bars: tuple[DailyBar, ...]
    input_data_fingerprint: str
    cost_inputs: CostInputs | None = None
    live_quote: ShadowLiveQuote | None = None
    cross_asset_inputs: tuple[CrossAssetInput, ...] | None = None
    fundamental_inputs: FundamentalInputs | None = None

    def __post_init__(self) -> None:
        if not _nonempty_text(self.instrument) or not _nonempty_text(self.epic):
            raise ShadowDrySnapshotError("SHADOW01_DRY_SNAPSHOT_MARKET_IDENTITY_INVALID")
        if not _valid_fingerprint(self.input_data_fingerprint):
            raise ShadowDrySnapshotError("SHADOW01_DRY_SNAPSHOT_EVIDENCE_FINGERPRINT_INVALID")
        try:
            bars = tuple(self.completed_bars)
        except TypeError:
            raise ShadowDrySnapshotError("SHADOW01_DRY_SNAPSHOT_COMPLETED_BARS_INVALID") from None
        if not bars or not all(isinstance(item, DailyBar) for item in bars):
            raise ShadowDrySnapshotError("SHADOW01_DRY_SNAPSHOT_COMPLETED_BARS_INVALID")
        object.__setattr__(self, "completed_bars", bars)
        if self.cost_inputs is not None and not isinstance(self.cost_inputs, CostInputs):
            raise ShadowDrySnapshotError("SHADOW01_DRY_SNAPSHOT_COST_INPUTS_INVALID")
        if self.live_quote is not None and (
            not isinstance(self.live_quote, ShadowLiveQuote)
            or self.live_quote.quality != "VALID_QUOTE"
            or self.live_quote.epic != self.epic
            or self.live_quote.symbol != self.instrument
        ):
            raise ShadowDrySnapshotError("SHADOW01_DRY_SNAPSHOT_LIVE_QUOTE_INVALID")
        if self.cross_asset_inputs is not None:
            try:
                context_inputs = tuple(self.cross_asset_inputs)
            except TypeError:
                raise ShadowDrySnapshotError("SHADOW01_DRY_SNAPSHOT_X1_INPUTS_INVALID") from None
            if not all(isinstance(item, CrossAssetInput) for item in context_inputs):
                raise ShadowDrySnapshotError("SHADOW01_DRY_SNAPSHOT_X1_INPUTS_INVALID")
            object.__setattr__(self, "cross_asset_inputs", context_inputs)
        if self.fundamental_inputs is not None and not isinstance(
            self.fundamental_inputs, FundamentalInputs
        ):
            raise ShadowDrySnapshotError("SHADOW01_DRY_SNAPSHOT_F1_INPUTS_INVALID")


@dataclass(frozen=True)
class DrySnapshotContext:
    """The one canonical timestamp and health facts shared by every policy."""

    observed_at_utc: datetime
    markets: tuple[DrySnapshotMarketInput, ...]
    # Diagnostics must supply every health fact explicitly.  Missing evidence
    # is UNKNOWN, never an inferred healthy provider or completed session.
    provider_healthy: bool | None = None
    stream_healthy: bool | None = None
    session_complete: bool | None = None

    def __post_init__(self) -> None:
        try:
            timestamp = require_utc(self.observed_at_utc)
        except ValueError:
            raise ShadowDrySnapshotError("SHADOW01_DRY_SNAPSHOT_TIMESTAMP_INVALID") from None
        object.__setattr__(self, "observed_at_utc", timestamp)
        try:
            markets = tuple(self.markets)
        except TypeError:
            raise ShadowDrySnapshotError("SHADOW01_DRY_SNAPSHOT_MARKETS_INVALID") from None
        if not markets or not all(isinstance(item, DrySnapshotMarketInput) for item in markets):
            raise ShadowDrySnapshotError("SHADOW01_DRY_SNAPSHOT_MARKETS_INVALID")
        if len({item.instrument for item in markets}) != len(markets):
            raise ShadowDrySnapshotError("SHADOW01_DRY_SNAPSHOT_MARKETS_DUPLICATE")
        object.__setattr__(self, "markets", markets)
        for value in (self.provider_healthy, self.stream_healthy, self.session_complete):
            if value is not None and not isinstance(value, bool):
                raise ShadowDrySnapshotError("SHADOW01_DRY_SNAPSHOT_HEALTH_INPUT_INVALID")


@dataclass(frozen=True)
class DrySnapshotPolicyResult:
    """One policy recommendation bound to the shared diagnostic timestamp."""

    recommendation: PolicyRecommendation
    diagnostic_timestamp_utc: datetime

    def __post_init__(self) -> None:
        if not isinstance(self.recommendation, PolicyRecommendation):
            raise ValueError("Shadow dry snapshot policy recommendation is invalid")
        object.__setattr__(
            self,
            "diagnostic_timestamp_utc",
            require_utc(self.diagnostic_timestamp_utc),
        )


@dataclass(frozen=True)
class DrySnapshotMarketResult:
    """In-memory T1/M1/X1/F1/Q1/C1 and P0--P3 diagnostic output for one market."""

    status: str
    instrument: str
    epic: str
    diagnostic_timestamp_utc: datetime
    input_data_fingerprint: str
    technical_state: TechnicalState
    trend: TechnicalOpinion
    reversion: ReversionOpinion
    cross_asset: CrossAssetOpinion
    fundamental: FundamentalContext
    quality: QualityAssessment
    cost: CostContext
    policies: tuple[DrySnapshotPolicyResult, ...]

    def __post_init__(self) -> None:
        if self.status != _DRY_STATUS:
            raise ValueError("Shadow dry snapshot result status is invalid")
        object.__setattr__(
            self,
            "diagnostic_timestamp_utc",
            require_utc(self.diagnostic_timestamp_utc),
        )
        if tuple(item.recommendation.policy_id for item in self.policies) != tuple(PolicyId) or any(
            item.diagnostic_timestamp_utc != self.diagnostic_timestamp_utc for item in self.policies
        ):
            raise ValueError("Shadow dry snapshot requires all four policies")


@dataclass(frozen=True)
class DrySnapshotResult:
    """Formally non-prospective diagnostic result with no persistence side effects."""

    status: str
    observed_at_utc: datetime
    execution_authority: str
    markets: tuple[DrySnapshotMarketResult, ...]
    evidence_fingerprint: str
    broker_counters_before: ReadOnlyBrokerRequestCounters
    broker_counters_after: ReadOnlyBrokerRequestCounters
    epoch_created: bool = False
    prospective_decisions_created: int = 0
    outcomes_created: int = 0
    demo_robot_starts: int = 0

    def __post_init__(self) -> None:
        if self.status != _DRY_STATUS or self.execution_authority != "OFF":
            raise ValueError("Shadow dry snapshot safety contract is invalid")
        object.__setattr__(self, "observed_at_utc", require_utc(self.observed_at_utc))
        if not self.markets or not all(item.status == _DRY_STATUS for item in self.markets):
            raise ValueError("Shadow dry snapshot market results are invalid")
        if not _valid_fingerprint(self.evidence_fingerprint):
            raise ValueError("Shadow dry snapshot evidence fingerprint is invalid")
        if not (
            isinstance(self.broker_counters_before, ReadOnlyBrokerRequestCounters)
            and isinstance(self.broker_counters_after, ReadOnlyBrokerRequestCounters)
        ):
            raise ValueError("Shadow dry snapshot broker counters are invalid")
        if (
            self.epoch_created,
            self.prospective_decisions_created,
            self.outcomes_created,
            self.demo_robot_starts,
        ) != (False, 0, 0, 0):
            raise ValueError("Shadow dry snapshot must remain non-prospective")

    @property
    def dry_run_non_prospective(self) -> bool:
        """Expose the required explicit label without a mutable mode switch."""

        return True


class ShadowDrySnapshotService:
    """Evaluate supplied completed facts without acquiring or retaining data."""

    execution_authority = "OFF"

    def __init__(
        self,
        *,
        config: ShadowTournamentConfig,
        registry: ShadowMarketRegistry,
        broker: Shadow01ReadOnlyBroker,
    ) -> None:
        _require_safe_dependencies(config=config, registry=registry, broker=broker)
        self._config = config
        self._registry = registry
        self._broker = broker

    def run(self, context: DrySnapshotContext) -> DrySnapshotResult:
        """Run the six engines and four policies from supplied completed facts only.

        The broker is deliberately never called.  Its immutable before/after
        counters are carried in the result so callers and tests can prove this
        diagnostic neither acquired data nor changed broker authority.
        """

        if not isinstance(context, DrySnapshotContext):
            raise ShadowDrySnapshotError("SHADOW01_DRY_SNAPSHOT_CONTEXT_REQUIRED")
        _require_safe_dependencies(
            config=self._config,
            registry=self._registry,
            broker=self._broker,
        )
        counters_before = self._broker.request_counters
        timestamp = context.observed_at_utc
        verified = _verified_market_map(self._registry)
        results = tuple(
            _evaluate_market(
                config=self._config,
                market=market,
                verified=verified,
                context=context,
                timestamp=timestamp,
            )
            for market in context.markets
        )
        counters_after = self._broker.request_counters
        if counters_after != counters_before:
            raise ShadowDrySnapshotError("SHADOW01_DRY_SNAPSHOT_BROKER_ACTIVITY_DETECTED")
        return DrySnapshotResult(
            status=_DRY_STATUS,
            observed_at_utc=timestamp,
            execution_authority=self.execution_authority,
            markets=results,
            evidence_fingerprint=fingerprint(
                {
                    "status": _DRY_STATUS,
                    "timestamp": timestamp,
                    "config_fingerprint": self._config.fingerprint,
                    "registry_fingerprint": self._registry.source_fingerprint,
                    "markets": [
                        {
                            "instrument": item.instrument,
                            "epic": item.epic,
                            "input_data_fingerprint": item.input_data_fingerprint,
                        }
                        for item in results
                    ],
                }
            ),
            broker_counters_before=counters_before,
            broker_counters_after=counters_after,
        )


def run_shadow_dry_snapshot(
    *,
    config: ShadowTournamentConfig,
    registry: ShadowMarketRegistry,
    broker: Shadow01ReadOnlyBroker,
    context: DrySnapshotContext,
) -> DrySnapshotResult:
    """Convenience function for the reviewed non-persisting diagnostic surface."""

    return ShadowDrySnapshotService(config=config, registry=registry, broker=broker).run(context)


def _evaluate_market(
    *,
    config: ShadowTournamentConfig,
    market: DrySnapshotMarketInput,
    verified: dict[str, MarketSpec],
    context: DrySnapshotContext,
    timestamp: datetime,
) -> DrySnapshotMarketResult:
    spec = verified.get(market.instrument)
    if spec is None or getattr(spec, "epic", None) != market.epic:
        raise ShadowDrySnapshotError("SHADOW01_DRY_SNAPSHOT_UNVERIFIED_EPIC")
    history = config.payload.get("history")
    if not isinstance(history, dict):
        raise ShadowDrySnapshotError("SHADOW01_DRY_SNAPSHOT_HISTORY_CONFIG_INVALID")
    minimum = history.get("minimum_completed_observations")
    if isinstance(minimum, bool) or not isinstance(minimum, int) or minimum < 1:
        raise ShadowDrySnapshotError("SHADOW01_DRY_SNAPSHOT_HISTORY_CONFIG_INVALID")
    quality = assess_q1_quality(
        market.completed_bars,
        config,
        decision_timestamp_utc=timestamp,
        provider_healthy=context.provider_healthy,
        stream_healthy=context.stream_healthy,
        session_complete=context.session_complete,
        feature_available=len(market.completed_bars) >= minimum,
    )
    try:
        technical = compute_technical_state(
            market.completed_bars,
            config,
            decision_timestamp_utc=timestamp,
        )
        trend = evaluate_t1_trend(technical)
        reversion = evaluate_m1_reversion(
            market.completed_bars,
            config,
            decision_timestamp_utc=timestamp,
        )
    except ValueError as error:
        raise ShadowDrySnapshotError(str(error)) from None
    cross_asset = evaluate_x1_context(
        market.cross_asset_inputs,
        config,
        decision_timestamp_utc=timestamp,
    )
    fundamental = build_f1_context(
        market.fundamental_inputs,
        decision_timestamp_utc=timestamp,
    )
    cost = assess_c1_cost(_cost_inputs_for_market(market), technical, config)
    recommendations = evaluate_policies(
        config,
        trend=trend,
        reversion=reversion,
        cross_asset=cross_asset,
        fundamental=fundamental,
        quality=quality,
        cost=cost,
    )
    return DrySnapshotMarketResult(
        status=_DRY_STATUS,
        instrument=market.instrument,
        epic=market.epic,
        diagnostic_timestamp_utc=timestamp,
        input_data_fingerprint=market.input_data_fingerprint,
        technical_state=technical,
        trend=trend,
        reversion=reversion,
        cross_asset=cross_asset,
        fundamental=fundamental,
        quality=quality,
        cost=cost,
        policies=tuple(
            DrySnapshotPolicyResult(
                recommendation=recommendation,
                diagnostic_timestamp_utc=timestamp,
            )
            for recommendation in recommendations
        ),
    )


def _cost_inputs_for_market(market: DrySnapshotMarketInput) -> CostInputs | None:
    """Use one accepted canonical stream quote for C1 without inventing costs."""

    quote = market.live_quote
    if quote is None:
        return market.cost_inputs
    assert quote.bid is not None and quote.ask is not None
    existing = market.cost_inputs
    try:
        reference_price = float(quote.ask)
        spread = float(quote.ask - quote.bid)
    except (OverflowError, ValueError):
        reference_price = None
        spread = None
    if reference_price is not None and not isfinite(reference_price):
        reference_price = None
    if spread is not None and not isfinite(spread):
        spread = None
    return CostInputs(
        reference_price=reference_price,
        spread=spread,
        minimum_stop_distance=existing.minimum_stop_distance if existing is not None else None,
        product_type=existing.product_type if existing is not None else None,
        funding_metadata=existing.funding_metadata if existing is not None else None,
    )


def _require_safe_dependencies(
    *,
    config: ShadowTournamentConfig,
    registry: ShadowMarketRegistry,
    broker: Shadow01ReadOnlyBroker,
) -> None:
    if (
        not isinstance(config, ShadowTournamentConfig)
        or not config.fingerprint_is_valid
        or config.payload.get("execution_authority") != "OFF"
    ):
        raise ShadowDrySnapshotError("SHADOW01_DRY_SNAPSHOT_CONFIG_UNSAFE")
    if not isinstance(registry, ShadowMarketRegistry):
        raise ShadowDrySnapshotError("SHADOW01_DRY_SNAPSHOT_REGISTRY_REQUIRED")
    if not isinstance(broker, Shadow01ReadOnlyBroker) or broker.execution_authority != "OFF":
        raise ShadowDrySnapshotError("SHADOW01_DRY_SNAPSHOT_READ_ONLY_BROKER_REQUIRED")
    try:
        proven = require_exact_twenty(registry)
    except ShadowRegistryError:
        raise ShadowDrySnapshotError("SHADOW01_DRY_SNAPSHOT_REGISTRY_SCOPE_INVALID") from None
    if (
        registry.verified_count != len(proven)
        or registry.source_path is None
        or not _valid_fingerprint(registry.source_fingerprint)
        or any(item.epic is None for item in proven)
    ):
        raise ShadowDrySnapshotError("SHADOW01_DRY_SNAPSHOT_DQ03_PROVENANCE_REQUIRED")


def _verified_market_map(registry: ShadowMarketRegistry) -> dict[str, MarketSpec]:
    """Return only full DQ-03 identities after dependency validation has passed."""

    return {item.symbol: item for item in registry.markets if item.epic is not None}


def _nonempty_text(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip()) and value == value.strip()


def _valid_fingerprint(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in _FINGERPRINT_CHARACTERS for character in value)
    )


__all__ = (
    "DrySnapshotContext",
    "DrySnapshotMarketInput",
    "DrySnapshotMarketResult",
    "DrySnapshotPolicyResult",
    "DrySnapshotResult",
    "ShadowDrySnapshotError",
    "ShadowDrySnapshotService",
    "run_shadow_dry_snapshot",
)
