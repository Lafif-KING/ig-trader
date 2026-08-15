BEGIN;

CREATE SCHEMA IF NOT EXISTS trading;

CREATE TABLE IF NOT EXISTS trading.schema_migrations (
    version text PRIMARY KEY,
    checksum_sha256 text NOT NULL CHECK (checksum_sha256 ~ '^[0-9a-f]{64}$'),
    applied_at timestamptz NOT NULL DEFAULT now(),
    applied_by text NOT NULL
);

CREATE TABLE IF NOT EXISTS trading.trade_intents (
    intent_id uuid PRIMARY KEY,
    idempotency_key text NOT NULL UNIQUE,
    strategy_name text NOT NULL,
    epic text NOT NULL,
    execution_mode text NOT NULL CHECK (execution_mode IN ('NO_EXECUTION', 'OFFLINE_PAPER', 'DEMO')),
    lifecycle_state text NOT NULL,
    expected_version bigint NOT NULL DEFAULT 0 CHECK (expected_version >= 0),
    intent_payload jsonb NOT NULL,
    input_fingerprint_sha256 text NOT NULL CHECK (input_fingerprint_sha256 ~ '^[0-9a-f]{64}$'),
    created_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL,
    CHECK (btrim(strategy_name) <> ''),
    CHECK (btrim(epic) <> ''),
    CHECK (updated_at >= created_at)
);

CREATE TABLE IF NOT EXISTS trading.lifecycle_events (
    sequence bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    intent_id uuid NOT NULL REFERENCES trading.trade_intents(intent_id),
    from_state text,
    to_state text NOT NULL,
    reason_code text NOT NULL,
    evidence jsonb NOT NULL,
    occurred_at timestamptz NOT NULL,
    UNIQUE (intent_id, sequence),
    CHECK (btrim(to_state) <> ''),
    CHECK (btrim(reason_code) <> '')
);

CREATE TABLE IF NOT EXISTS trading.broker_references (
    intent_id uuid PRIMARY KEY REFERENCES trading.trade_intents(intent_id),
    deal_reference text NOT NULL UNIQUE,
    deal_id text NOT NULL UNIQUE,
    confirmation_status text NOT NULL CHECK (confirmation_status = 'ACCEPTED'),
    confirmed_at timestamptz NOT NULL,
    confirmation_evidence jsonb NOT NULL,
    CHECK (btrim(deal_reference) <> ''),
    CHECK (btrim(deal_id) <> '')
);

CREATE TABLE IF NOT EXISTS trading.position_state (
    position_id uuid PRIMARY KEY,
    intent_id uuid NOT NULL UNIQUE REFERENCES trading.trade_intents(intent_id),
    deal_id text NOT NULL UNIQUE,
    strategy_name text NOT NULL,
    state text NOT NULL CHECK (
        state IN ('OPEN', 'CLOSED', 'UNKNOWN', 'RECONCILIATION_REQUIRED')
    ),
    state_version bigint NOT NULL DEFAULT 0 CHECK (state_version >= 0),
    broker_snapshot jsonb NOT NULL,
    observed_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL,
    CHECK (updated_at >= observed_at)
);

CREATE TABLE IF NOT EXISTS trading.reconciliation_state (
    scope text PRIMARY KEY,
    status text NOT NULL CHECK (status IN ('KNOWN', 'UNKNOWN', 'BLOCKED')),
    last_broker_snapshot_fingerprint_sha256 text CHECK (
        last_broker_snapshot_fingerprint_sha256 IS NULL
        OR last_broker_snapshot_fingerprint_sha256 ~ '^[0-9a-f]{64}$'
    ),
    checkpoint jsonb NOT NULL,
    reconciled_at timestamptz,
    updated_at timestamptz NOT NULL,
    CHECK (btrim(scope) <> '')
);

CREATE TABLE IF NOT EXISTS trading.evidence_metadata (
    evidence_id uuid PRIMARY KEY,
    intent_id uuid REFERENCES trading.trade_intents(intent_id),
    evidence_type text NOT NULL,
    object_uri text NOT NULL,
    content_sha256 text NOT NULL CHECK (content_sha256 ~ '^[0-9a-f]{64}$'),
    metadata jsonb NOT NULL,
    created_at timestamptz NOT NULL,
    UNIQUE (object_uri, content_sha256),
    CHECK (btrim(evidence_type) <> ''),
    CHECK (btrim(object_uri) <> '')
);

CREATE TABLE IF NOT EXISTS trading.worker_leases (
    lease_name text PRIMARY KEY CHECK (lease_name = 'execution-worker'),
    owner_instance text NOT NULL,
    fencing_token bigint NOT NULL CHECK (fencing_token > 0),
    acquired_at timestamptz NOT NULL,
    lease_until timestamptz NOT NULL,
    CHECK (lease_until > acquired_at),
    CHECK (btrim(owner_instance) <> '')
);

CREATE OR REPLACE FUNCTION trading.reject_append_only_mutation()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION 'append-only table mutation is prohibited';
END;
$$;

DROP TRIGGER IF EXISTS lifecycle_events_append_only ON trading.lifecycle_events;
CREATE TRIGGER lifecycle_events_append_only
BEFORE UPDATE OR DELETE ON trading.lifecycle_events
FOR EACH ROW EXECUTE FUNCTION trading.reject_append_only_mutation();

DROP TRIGGER IF EXISTS evidence_metadata_append_only ON trading.evidence_metadata;
CREATE TRIGGER evidence_metadata_append_only
BEFORE UPDATE OR DELETE ON trading.evidence_metadata
FOR EACH ROW EXECUTE FUNCTION trading.reject_append_only_mutation();

COMMIT;
