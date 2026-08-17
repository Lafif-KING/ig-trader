BEGIN;

ALTER TABLE trading.trade_intents
    DROP CONSTRAINT IF EXISTS trade_intents_execution_mode_check;

ALTER TABLE trading.trade_intents
    ADD CONSTRAINT trade_intents_execution_mode_check
    CHECK (execution_mode IN ('NO_EXECUTION', 'OFFLINE_PAPER', 'DEMO', 'SHADOW_DEMO'));

CREATE TABLE IF NOT EXISTS trading.shadow_position_state (
    shadow_position_id uuid PRIMARY KEY,
    intent_id uuid NOT NULL UNIQUE REFERENCES trading.trade_intents(intent_id),
    strategy_id text NOT NULL,
    instrument text NOT NULL,
    direction text NOT NULL CHECK (direction IN ('BUY', 'SELL')),
    entry_price numeric NOT NULL CHECK (entry_price > 0),
    stop_price numeric NOT NULL CHECK (stop_price > 0),
    target_price numeric NOT NULL CHECK (target_price > 0),
    opened_at timestamptz NOT NULL,
    closed_at timestamptz,
    status text NOT NULL CHECK (status IN ('OPEN', 'CLOSED', 'RECONCILED', 'FAILED_SAFE')),
    exit_price numeric CHECK (exit_price > 0),
    exit_reason text,
    fencing_token bigint NOT NULL CHECK (fencing_token > 0),
    created_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL,
    CHECK (btrim(strategy_id) <> ''),
    CHECK (btrim(instrument) <> ''),
    CHECK (closed_at IS NULL OR closed_at >= opened_at),
    CHECK (updated_at >= created_at)
);

DROP TRIGGER IF EXISTS shadow_position_state_require_fence ON trading.shadow_position_state;
CREATE TRIGGER shadow_position_state_require_fence
BEFORE INSERT OR UPDATE OR DELETE ON trading.shadow_position_state
FOR EACH ROW EXECUTE FUNCTION trading.require_current_execution_fence();

COMMIT;
