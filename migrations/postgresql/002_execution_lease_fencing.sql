BEGIN;

ALTER TABLE trading.worker_leases
    ADD COLUMN IF NOT EXISTS heartbeat_at timestamptz;

UPDATE trading.worker_leases
SET heartbeat_at = acquired_at
WHERE heartbeat_at IS NULL;

ALTER TABLE trading.worker_leases
    ALTER COLUMN heartbeat_at SET NOT NULL;

CREATE SEQUENCE IF NOT EXISTS trading.worker_lease_fencing_token_seq AS bigint;

SELECT setval(
    'trading.worker_lease_fencing_token_seq',
    GREATEST(COALESCE(MAX(fencing_token), 0), 1),
    COALESCE(MAX(fencing_token), 0) > 0
)
FROM trading.worker_leases;

CREATE OR REPLACE FUNCTION trading.acquire_execution_lease(
    requested_lease_name text,
    requested_owner_instance text,
    requested_ttl_seconds double precision
)
RETURNS TABLE (
    lease_name text,
    owner_instance text,
    fencing_token bigint,
    acquired_at timestamptz,
    heartbeat_at timestamptz,
    lease_until timestamptz
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, trading
AS $$
BEGIN
    IF requested_lease_name <> 'execution-worker'
       OR btrim(requested_owner_instance) = ''
       OR requested_ttl_seconds < 1
       OR requested_ttl_seconds > 300 THEN
        RAISE EXCEPTION USING
            ERRCODE = '22023',
            MESSAGE = 'invalid execution lease acquisition input';
    END IF;

    RETURN QUERY
    INSERT INTO trading.worker_leases AS current_lease (
        lease_name,
        owner_instance,
        fencing_token,
        acquired_at,
        heartbeat_at,
        lease_until
    ) VALUES (
        requested_lease_name,
        requested_owner_instance,
        nextval('trading.worker_lease_fencing_token_seq'),
        clock_timestamp(),
        clock_timestamp(),
        clock_timestamp() + make_interval(secs => requested_ttl_seconds)
    )
    ON CONFLICT ON CONSTRAINT worker_leases_pkey DO UPDATE
    SET owner_instance = EXCLUDED.owner_instance,
        fencing_token = EXCLUDED.fencing_token,
        acquired_at = EXCLUDED.acquired_at,
        heartbeat_at = EXCLUDED.heartbeat_at,
        lease_until = EXCLUDED.lease_until
    WHERE current_lease.lease_until <= clock_timestamp()
    RETURNING
        current_lease.lease_name,
        current_lease.owner_instance,
        current_lease.fencing_token,
        current_lease.acquired_at,
        current_lease.heartbeat_at,
        current_lease.lease_until;
END;
$$;

REVOKE EXECUTE ON FUNCTION trading.acquire_execution_lease(text, text, double precision)
FROM PUBLIC;

CREATE OR REPLACE FUNCTION trading.renew_execution_lease(
    requested_lease_name text,
    requested_owner_instance text,
    requested_fencing_token bigint,
    requested_ttl_seconds double precision
)
RETURNS TABLE (
    lease_name text,
    owner_instance text,
    fencing_token bigint,
    acquired_at timestamptz,
    heartbeat_at timestamptz,
    lease_until timestamptz
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, trading
AS $$
BEGIN
    IF requested_lease_name <> 'execution-worker'
       OR btrim(requested_owner_instance) = ''
       OR requested_fencing_token <= 0
       OR requested_ttl_seconds < 1
       OR requested_ttl_seconds > 300 THEN
        RAISE EXCEPTION USING
            ERRCODE = '22023',
            MESSAGE = 'invalid execution lease renewal input';
    END IF;

    RETURN QUERY
    UPDATE trading.worker_leases AS current_lease
    SET heartbeat_at = clock_timestamp(),
        lease_until = clock_timestamp() + make_interval(secs => requested_ttl_seconds)
    WHERE current_lease.lease_name = requested_lease_name
      AND current_lease.owner_instance = requested_owner_instance
      AND current_lease.fencing_token = requested_fencing_token
      AND current_lease.lease_until > clock_timestamp()
    RETURNING
        current_lease.lease_name,
        current_lease.owner_instance,
        current_lease.fencing_token,
        current_lease.acquired_at,
        current_lease.heartbeat_at,
        current_lease.lease_until;
END;
$$;

REVOKE EXECUTE ON FUNCTION trading.renew_execution_lease(
    text,
    text,
    bigint,
    double precision
)
FROM PUBLIC;

CREATE OR REPLACE FUNCTION trading.release_execution_lease(
    requested_lease_name text,
    requested_owner_instance text,
    requested_fencing_token bigint
)
RETURNS boolean
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, trading
AS $$
DECLARE
    affected_rows integer;
BEGIN
    UPDATE trading.worker_leases AS current_lease
    SET heartbeat_at = clock_timestamp(),
        lease_until = GREATEST(
            clock_timestamp(),
            current_lease.acquired_at + interval '1 microsecond'
        )
    WHERE current_lease.lease_name = requested_lease_name
      AND current_lease.owner_instance = requested_owner_instance
      AND current_lease.fencing_token = requested_fencing_token;

    GET DIAGNOSTICS affected_rows = ROW_COUNT;
    RETURN affected_rows = 1;
END;
$$;

REVOKE EXECUTE ON FUNCTION trading.release_execution_lease(text, text, bigint)
FROM PUBLIC;

CREATE TABLE IF NOT EXISTS trading.execution_cycle_claims (
    cycle_id uuid PRIMARY KEY,
    lease_name text NOT NULL CHECK (lease_name = 'execution-worker'),
    holder_instance_id text NOT NULL,
    fencing_token bigint NOT NULL CHECK (fencing_token > 0),
    claimed_at timestamptz NOT NULL,
    state text NOT NULL CHECK (state IN ('CLAIMED', 'COMPLETED', 'FAILED_SAFE')),
    CHECK (btrim(holder_instance_id) <> '')
);

CREATE OR REPLACE FUNCTION trading.assert_execution_fence(
    requested_lease_name text,
    requested_owner_instance text,
    requested_fencing_token bigint,
    operation_scope text
)
RETURNS void
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = pg_catalog, trading
AS $$
BEGIN
    IF operation_scope NOT IN (
        'cycle_ownership',
        'trade_intent',
        'broker_submission',
        'reconciliation'
    ) THEN
        RAISE EXCEPTION USING
            ERRCODE = '22023',
            MESSAGE = 'unsupported fenced operation scope';
    END IF;

    PERFORM 1
    FROM trading.worker_leases
    WHERE lease_name = requested_lease_name
      AND owner_instance = requested_owner_instance
      AND fencing_token = requested_fencing_token
      AND lease_until > clock_timestamp()
    FOR UPDATE;

    IF NOT FOUND THEN
        RAISE EXCEPTION USING
            ERRCODE = '55000',
            MESSAGE = 'stale or unavailable execution fencing token';
    END IF;

    PERFORM set_config('ig_trader.execution_lease_name', requested_lease_name, true);
    PERFORM set_config('ig_trader.execution_lease_owner', requested_owner_instance, true);
    PERFORM set_config(
        'ig_trader.execution_fencing_token',
        requested_fencing_token::text,
        true
    );
    PERFORM set_config('ig_trader.execution_operation_scope', operation_scope, true);
END;
$$;

REVOKE EXECUTE ON FUNCTION trading.assert_execution_fence(text, text, bigint, text)
FROM PUBLIC;

CREATE OR REPLACE FUNCTION trading.require_current_execution_fence()
RETURNS trigger
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = pg_catalog, trading
AS $$
DECLARE
    active_lease_name text := current_setting('ig_trader.execution_lease_name', true);
    active_owner text := current_setting('ig_trader.execution_lease_owner', true);
    active_token_text text := current_setting('ig_trader.execution_fencing_token', true);
BEGIN
    IF active_lease_name IS NULL OR active_owner IS NULL OR active_token_text IS NULL THEN
        RAISE EXCEPTION USING
            ERRCODE = '55000',
            MESSAGE = 'state mutation requires an execution fencing token';
    END IF;

    PERFORM 1
    FROM trading.worker_leases
    WHERE lease_name = active_lease_name
      AND owner_instance = active_owner
      AND fencing_token = active_token_text::bigint
      AND lease_until > clock_timestamp()
    FOR UPDATE;

    IF NOT FOUND THEN
        RAISE EXCEPTION USING
            ERRCODE = '55000',
            MESSAGE = 'state mutation rejected for stale execution fencing token';
    END IF;

    IF TG_OP = 'DELETE' THEN
        RETURN OLD;
    END IF;
    RETURN NEW;
END;
$$;

REVOKE EXECUTE ON FUNCTION trading.require_current_execution_fence()
FROM PUBLIC;

DROP TRIGGER IF EXISTS execution_cycle_claims_require_fence ON trading.execution_cycle_claims;
CREATE TRIGGER execution_cycle_claims_require_fence
BEFORE INSERT OR UPDATE OR DELETE ON trading.execution_cycle_claims
FOR EACH ROW EXECUTE FUNCTION trading.require_current_execution_fence();

DROP TRIGGER IF EXISTS trade_intents_require_fence ON trading.trade_intents;
CREATE TRIGGER trade_intents_require_fence
BEFORE INSERT OR UPDATE OR DELETE ON trading.trade_intents
FOR EACH ROW EXECUTE FUNCTION trading.require_current_execution_fence();

DROP TRIGGER IF EXISTS lifecycle_events_require_fence ON trading.lifecycle_events;
CREATE TRIGGER lifecycle_events_require_fence
BEFORE INSERT OR UPDATE OR DELETE ON trading.lifecycle_events
FOR EACH ROW EXECUTE FUNCTION trading.require_current_execution_fence();

DROP TRIGGER IF EXISTS broker_references_require_fence ON trading.broker_references;
CREATE TRIGGER broker_references_require_fence
BEFORE INSERT OR UPDATE OR DELETE ON trading.broker_references
FOR EACH ROW EXECUTE FUNCTION trading.require_current_execution_fence();

DROP TRIGGER IF EXISTS position_state_require_fence ON trading.position_state;
CREATE TRIGGER position_state_require_fence
BEFORE INSERT OR UPDATE OR DELETE ON trading.position_state
FOR EACH ROW EXECUTE FUNCTION trading.require_current_execution_fence();

DROP TRIGGER IF EXISTS reconciliation_state_require_fence ON trading.reconciliation_state;
CREATE TRIGGER reconciliation_state_require_fence
BEFORE INSERT OR UPDATE OR DELETE ON trading.reconciliation_state
FOR EACH ROW EXECUTE FUNCTION trading.require_current_execution_fence();

DROP TRIGGER IF EXISTS evidence_metadata_require_fence ON trading.evidence_metadata;
CREATE TRIGGER evidence_metadata_require_fence
BEFORE INSERT OR UPDATE OR DELETE ON trading.evidence_metadata
FOR EACH ROW EXECUTE FUNCTION trading.require_current_execution_fence();

COMMIT;
