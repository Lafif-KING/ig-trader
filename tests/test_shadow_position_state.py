"""Contract checks for the dedicated broker-neutral Shadow position model."""

from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest

from src.ig_trader.shadow_position_state import (
    ShadowPosition,
    ShadowPositionState,
    ShadowPositionStateError,
)

ROOT = Path(__file__).resolve().parents[1]


def _position() -> ShadowPosition:
    now = datetime(2026, 8, 17, tzinfo=UTC)
    return ShadowPosition(uuid4(), uuid4(), "S0", "EURGBP", "BUY", 0.85, 0.8496,
                          0.8506, now, 1, now, now)


def test_shadow_position_is_broker_neutral_and_uses_no_deal_or_order_identifier() -> None:
    position = _position()
    assert position.status is ShadowPositionState.OPEN
    assert "deal_id" not in position.__dataclass_fields__
    assert "order_id" not in position.__dataclass_fields__
    assert "working_order_id" not in position.__dataclass_fields__


def test_migration_keeps_broker_position_deal_id_required_and_shadow_broker_neutral() -> None:
    broker = (ROOT / "migrations/postgresql/001_execution_state.sql").read_text(encoding="utf-8")
    shadow = (ROOT / "migrations/postgresql/003_shadow_position_state.sql").read_text(
        encoding="utf-8"
    )
    assert "deal_id text NOT NULL UNIQUE" in broker
    assert "deal_id" not in shadow
    assert "order_id" not in shadow
    assert "working_order_id" not in shadow
    assert "UNIQUE REFERENCES trading.trade_intents(intent_id)" in shadow
    assert "shadow_position_state_require_fence" in shadow


def test_shadow_position_requires_open_broker_neutral_state() -> None:
    invalid = _position()
    with pytest.raises(ShadowPositionStateError):
        from src.ig_trader.shadow_position_state import _validate

        _validate(ShadowPosition(**{**invalid.__dict__, "status": ShadowPositionState.CLOSED}))
