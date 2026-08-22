from datetime import UTC, datetime, timedelta

import pytest

from src.ig_trader.shadow_ig_readonly import ShadowReadonlyStream, StreamHealth


NOW = datetime(2026, 8, 21, 12, tzinfo=UTC)


def test_stream_uses_one_market_item_and_restores_it_after_reconnect() -> None:
    stream = ShadowReadonlyStream("CS.D.EURGBP.MINI.IP")
    assert stream.item == "MARKET:CS.D.EURGBP.MINI.IP"
    assert stream.connected().subscriptions == (stream.item,)
    stream.update_quote(bid=0.8499, offer=0.8500, as_of=NOW)
    assert stream.fresh_quote(now=NOW, max_age=timedelta(seconds=10)) is not None
    retry = stream.disconnected(now=NOW)
    assert retry.health is StreamHealth.BACKOFF
    assert retry.reconnect_after == NOW + timedelta(seconds=1)
    assert stream.connected().subscriptions == (stream.item,)


def test_stream_rejects_updates_while_disconnected_and_stale_quotes() -> None:
    stream = ShadowReadonlyStream("CS.D.EURGBP.MINI.IP")
    with pytest.raises(RuntimeError):
        stream.update_quote(bid=0.8499, offer=0.8500, as_of=NOW)
    stream.connected()
    stream.update_quote(bid=0.8499, offer=0.8500, as_of=NOW)
    assert stream.fresh_quote(now=NOW + timedelta(seconds=11), max_age=timedelta(seconds=10)) is None
