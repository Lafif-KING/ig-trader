from src.ig_trader.shadow_cloud_runtime import ShadowCloudRuntime


class Health:
    def __init__(self, *, database: bool = True, quote_age: float | None = 1.0) -> None:
        self.database = database
        self.quote_age = quote_age

    def database_available(self) -> bool:
        return self.database

    def lease_authorized(self) -> bool:
        return True

    def fencing_token_present(self) -> bool:
        return True

    def stream_connected(self) -> bool:
        return True

    def quote_age_seconds(self) -> float | None:
        return self.quote_age

    def candle_ready(self) -> bool:
        return True

    def active_position_count(self) -> int | None:
        return 0

    def last_cycle_status(self) -> str | None:
        return "NO_TRADE"


def test_readiness_is_broker_inert_and_requires_all_dependencies() -> None:
    evidence = ShadowCloudRuntime(Health(), release_sha="a" * 40).readiness_evidence()
    assert evidence["ready"] is True
    assert evidence["execution_mode"] == "SHADOW_DEMO"
    assert evidence["authorized"] is False
    assert evidence["order_authority"] is False
    assert evidence["broker_order_call_count"] == 0


def test_unavailable_database_or_stale_quote_blocks_readiness() -> None:
    assert ShadowCloudRuntime(Health(database=False), release_sha="a" * 40).readiness().ready is False
    assert ShadowCloudRuntime(Health(quote_age=11.0), release_sha="a" * 40).readiness().ready is False
