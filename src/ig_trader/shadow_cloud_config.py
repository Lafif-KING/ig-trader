"""Fail-closed configuration contract for the broker-read-only Shadow worker."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from src.ig_trader.shadow_execution import ExecutionMode

FROZEN_SHADOW_EPICS = frozenset(
    {
        "CS.D.EURGBP.MINI.IP",
        "CS.D.EURUSD.CEFM.IP",
        "CS.D.GBPUSD.MINI.IP",
    }
)


class ShadowCloudConfigurationError(ValueError):
    """Configuration cannot safely start the Shadow process."""


@dataclass(frozen=True)
class ShadowCloudConfig:
    execution_mode: ExecutionMode
    ig_base_url: str
    instrument_epics: tuple[str, ...]
    postgres_managed_identity_client_id: str
    ig_secret_reference: str
    replica_count: int

    @classmethod
    def from_environment(cls, environment: Mapping[str, str]) -> ShadowCloudConfig:
        mode = environment.get("EXECUTION_MODE", "").strip()
        base_url = environment.get("IG_BASE_URL", "").strip()
        instruments = tuple(
            value.strip()
            for value in environment.get("SHADOW_INSTRUMENT_EPICS", "").split(",")
            if value.strip()
        )
        identity = environment.get("POSTGRES_MANAGED_IDENTITY_CLIENT_ID", "").strip()
        secret_reference = environment.get("IG_DEMO_SECRET_REFERENCE", "").strip()
        replicas = environment.get("SHADOW_REPLICA_COUNT", "").strip()
        if mode != ExecutionMode.SHADOW_DEMO.value:
            raise ShadowCloudConfigurationError("Shadow worker accepts SHADOW_DEMO only")
        if not base_url.startswith("https://") or "demo" not in base_url.casefold() or "live" in base_url.casefold():
            raise ShadowCloudConfigurationError("Shadow worker requires an IG Demo HTTPS endpoint")
        if not instruments or set(instruments) != FROZEN_SHADOW_EPICS:
            raise ShadowCloudConfigurationError("Shadow worker requires the frozen instrument registry")
        if not identity or not secret_reference or "=" in secret_reference:
            raise ShadowCloudConfigurationError("managed identity and secret reference are required")
        if replicas != "1":
            raise ShadowCloudConfigurationError("Shadow worker requires exactly one replica")
        return cls(
            execution_mode=ExecutionMode.SHADOW_DEMO,
            ig_base_url=base_url,
            instrument_epics=instruments,
            postgres_managed_identity_client_id=identity,
            ig_secret_reference=secret_reference,
            replica_count=1,
        )
