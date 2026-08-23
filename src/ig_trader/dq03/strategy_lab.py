"""Read-only bridge from DQ-03 evidence to the broker-neutral Strategy Lab."""

from __future__ import annotations

from decimal import Decimal

from src.ig_trader.dq03.models import DQ03Resolution, DQ03Status
from src.ig_trader.strategy_lab.models import (
    INITIAL_INSTRUMENT_REGISTRY,
    DataAvailability,
    InstrumentSpec,
    SpreadStatistics,
)


def build_strategy_lab_context(resolutions: tuple[DQ03Resolution, ...]) -> dict[str, object]:
    """Expose verified broker facts without representing samples as research history.

    A bounded broker validation sample proves the response shape and broker
    source fingerprint only.  It is intentionally not presented as a broad
    research dataset, so every record remains ``DATA_NOT_AVAILABLE`` until an
    explicit local or external dataset is supplied through Strategy Lab's
    existing source interfaces.
    """

    return {
        "schema_version": "dq03-strategy-lab-context/1.0",
        "execution_authority": "OFF",
        "instruments": [
            {
                "symbol": item.symbol,
                "resolution_status": item.classification.value,
                "ig_epic": item.selected_epic,
                "metadata_fingerprint": item.metadata_fingerprint,
                "broker_validation_fingerprint": item.broker_validation_fingerprint,
                "data_status": "DATA_NOT_AVAILABLE",
                "data_reason": "No broad Strategy Lab dataset was supplied.",
                "cost_model_status": item.cost_model_status.value,
                "cost_model_inputs": _cost_model_inputs(item),
                "source_attribution": "IG_DEMO_READ_ONLY_METADATA"
                if item.classification is DQ03Status.VERIFIED
                else None,
            }
            for item in resolutions
        ],
    }


def registry_from_resolutions(
    resolutions: tuple[DQ03Resolution, ...],
) -> dict[str, InstrumentSpec]:
    """Create a local Lab registry projection without creating execution authority."""

    registry = dict(INITIAL_INSTRUMENT_REGISTRY)
    for item in resolutions:
        if item.classification is not DQ03Status.VERIFIED or item.metadata is None:
            continue
        metadata = item.metadata
        registry[item.symbol] = InstrumentSpec(
            symbol=item.symbol,
            asset_class=item.asset_class,
            display_name=metadata.display_name or item.display_name,
            ig_epic=item.selected_epic,
            expiry=metadata.expiry,
            currency=metadata.currency,
            pip_or_tick_size=metadata.one_pip_means,
            decimal_places=metadata.decimal_places,
            minimum_deal_size=metadata.minimum_deal_size,
            minimum_stop_distance=metadata.minimum_stop_distance,
            spread_statistics=SpreadStatistics(
                median=metadata.spread,
                percentile_95=None,
                sample_count=1 if metadata.spread is not None else 0,
                source_fingerprint=metadata.fingerprint,
            ),
            data_availability=DataAvailability.NOT_AVAILABLE,
        )
    return registry


def _cost_model_inputs(resolution: DQ03Resolution) -> dict[str, str | None]:
    metadata = resolution.metadata
    if metadata is None:
        return {
            "spread": None,
            "pip_or_tick_size": None,
            "value_of_one_pip": None,
            "minimum_deal_size": None,
            "minimum_stop_distance": None,
            "currency": None,
        }
    return {
        "spread": _decimal(metadata.spread),
        "pip_or_tick_size": _decimal(metadata.one_pip_means),
        "value_of_one_pip": _decimal(metadata.value_of_one_pip),
        "minimum_deal_size": _decimal(metadata.minimum_deal_size),
        "minimum_stop_distance": _decimal(metadata.minimum_stop_distance),
        "currency": metadata.currency,
    }


def _decimal(value: Decimal | None) -> str | None:
    return str(value) if value is not None else None
