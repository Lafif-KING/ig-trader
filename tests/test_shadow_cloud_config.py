import pytest

from src.ig_trader.shadow_cloud_config import ShadowCloudConfig, ShadowCloudConfigurationError


def environment(**overrides: str) -> dict[str, str]:
    values = {
        "EXECUTION_MODE": "SHADOW_DEMO",
        "IG_BASE_URL": "https://demo-api.ig.com",
        "SHADOW_INSTRUMENT_EPICS": "CS.D.EURGBP.MINI.IP,CS.D.EURUSD.CEFM.IP,CS.D.GBPUSD.MINI.IP",
        "POSTGRES_MANAGED_IDENTITY_CLIENT_ID": "managed-identity-client-id",
        "IG_DEMO_SECRET_REFERENCE": "keyvault-reference-name",
        "SHADOW_REPLICA_COUNT": "1",
    }
    values.update(overrides)
    return values


def test_only_complete_singleton_shadow_demo_configuration_is_accepted() -> None:
    config = ShadowCloudConfig.from_environment(environment())
    assert config.execution_mode.value == "SHADOW_DEMO"
    assert config.replica_count == 1


@pytest.mark.parametrize(
    "overrides",
    [
        {"EXECUTION_MODE": "NO_EXECUTION"},
        {"EXECUTION_MODE": "DEMO_EXECUTION"},
        {"EXECUTION_MODE": "LIVE_EXECUTION"},
        {"IG_BASE_URL": "https://api.ig.com"},
        {"IG_BASE_URL": "https://live-api.ig.com"},
        {"SHADOW_INSTRUMENT_EPICS": "CS.D.EURGBP.MINI.IP"},
        {"SHADOW_REPLICA_COUNT": "2"},
        {"IG_DEMO_SECRET_REFERENCE": "password=plaintext"},
    ],
)
def test_unsafe_shadow_configuration_is_rejected(overrides: dict[str, str]) -> None:
    with pytest.raises(ShadowCloudConfigurationError):
        ShadowCloudConfig.from_environment(environment(**overrides))
