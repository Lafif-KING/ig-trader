"""Broker-neutral placeholder for future durable Shadow evidence."""

from __future__ import annotations

from dashboard.models import ShadowDataStatus


def load_shadow_data() -> ShadowDataStatus:
    """Return explicit no-data state; never turn absent evidence into zeroes."""

    return ShadowDataStatus(
        status="DATA_NOT_AVAILABLE",
        reason=(
            "Azure database recovery is still on HOLD; durable Shadow evidence is not "
            "deployed; and the Shadow worker is not running."
        ),
    )
