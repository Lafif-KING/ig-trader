# G4B-02B1 PostgreSQL execution lease and fencing foundation

Status: SOURCE / CI / READ-ONLY AZURE ONLY. This change does not migrate the
deployed database, change the running Container App, create a database
principal, enable PostgreSQL connectivity, or grant broker authority.

Base: `e8fc85e756ceab7cf7f82339e38a8e1e3c55fe42`.

## Architecture conclusion

The G4B-02A2 revision restart briefly produced two ready replicas while the
Container App remained configured with `minReplicas=1` and `maxReplicas=1`.
Those settings remain the correct low-cost steady-state size, but they are not
an execution-singleton guarantee. The safety invariant is now:

> At most one process may hold the current ACTIVE EXECUTION LEASE and fencing
> token. Every other process is STANDBY for stateful work.

The implementation reuses `trading.worker_leases` from
`001_execution_state.sql`. Migration 002 adds `heartbeat_at` and a monotonic
fencing sequence to that table; `owner_instance` is the holder instance and
`lease_until` is the expiry time. It does not create a parallel lease store.
The existing TradeIntent, lifecycle, broker-reference, position,
reconciliation, and evidence tables remain the durable state root.

## Atomic lease algorithm

The one accepted lease name is `execution-worker`.

Acquisition uses one PostgreSQL `INSERT ... ON CONFLICT ... DO UPDATE` statement.
The conflict update is permitted only when PostgreSQL's clock proves that the
existing lease expired. The winning statement receives the next value from
`worker_lease_fencing_token_seq`; losing contenders receive no row and become
STANDBY. PostgreSQL row locking serializes simultaneous contenders.

Renewal updates `heartbeat_at` and `lease_until` only when all of lease name,
holder instance, fencing token, and unexpired state still match. Zero returned
rows, token failure, authentication failure, transaction failure, connection
loss, or an ambiguous result immediately removes local authority and changes
the process to STANDBY/FAIL_CLOSED. Clean release expires the row without
deleting its generation history. The next holder must receive a greater token.

No cloud path falls back to SQLite. SQLite remains only the accepted offline
G2/G3B test backend.

## Fencing and state-changing work

`trading.assert_execution_fence` validates the lease name, holder, fencing
token, expiry, and operation scope and holds the lease-row lock through the
caller's transaction. Migration 002 also adds a defensive trigger to each
cloud execution-state table. A write without a current fence, or with a stale
fence, is rejected by PostgreSQL.

The protected scopes are:

- cycle ownership;
- TradeIntent and lifecycle state;
- future broker-submission authority;
- reconciliation authority.

`execution_cycle_claims` records the lease generation that owns a future
stateful cycle. It is a fenced lifecycle record, not a second lease. Broker
submission remains unimplemented and unauthorized. A future adapter must
perform its state transition and fence validation in the same PostgreSQL
transaction and retain the existing idempotency/reconciliation requirements.

## Replica role and observability

Every runtime process exposes one role: `LEADER`, `STANDBY`, or
`NO_EXECUTION`. The accepted cloud entry point still accepts only
`EXECUTION_MODE=NO_EXECUTION`; it does not open PostgreSQL, acquire a lease, or
enable a worker. Its explicit structured values are:

```text
authorized = false
runtime_role = NO_EXECUTION
lease_name = execution-worker
lease_holder = false
lease_state = DISABLED
fencing_token = null
lease_heartbeat_state = DISABLED
```

`replica_instance_id` uses the safe Container Apps replica name when supplied,
then the container hostname, then `local` for offline development. It contains
no secret. Startup logs and health/readiness include all fields above. Tokens
used for database authentication are never logged.

## Managed-identity PostgreSQL authentication

The future connection factory requires the existing user-assigned identity,
`igtrdevfrc-execution-identity`, an Azure PostgreSQL FQDN, database
`ig_trader`, and that identity's client ID. Password environment variables,
database URLs, and SQLite URLs are rejected. Azure Identity obtains the scope
`https://ossrdbms-aad.database.windows.net/.default`; the access token is kept
only in process memory and supplied directly to psycopg as the connection
password over required TLS. The Azure credential object is reused so its token
cache can renew safely for later connections.

The current `NO_EXECUTION` composition never constructs this factory, requests
a token, or opens a database connection.

## Future non-admin principal procedure

This procedure is documentation only. Do not execute it until a separate
database-connectivity work order approves it.

1. The existing human-controlled PostgreSQL Microsoft Entra administrator
   connects specifically to the server's `postgres` database and runs:

   ```sql
   SELECT *
   FROM pg_catalog.pgaadauth_create_principal(
       'igtrdevfrc-execution-identity',
       false,
       false
   );
   ```

   Both Boolean arguments are false, so the managed identity is a regular
   non-admin database role. Stop if the display name is not unique in the
   tenant or the function does not confirm that exact role.

2. The same administrator connects to database `ig_trader` and applies only
   these runtime privileges after migrations 001 and 002 have been applied by
   the separate migration authority:

   ```sql
   REVOKE ALL ON DATABASE ig_trader FROM "igtrdevfrc-execution-identity";
   GRANT CONNECT ON DATABASE ig_trader TO "igtrdevfrc-execution-identity";

   REVOKE CREATE ON SCHEMA public FROM "igtrdevfrc-execution-identity";
   GRANT USAGE ON SCHEMA trading TO "igtrdevfrc-execution-identity";

   GRANT SELECT ON
       trading.schema_migrations,
       trading.worker_leases,
       trading.execution_cycle_claims,
       trading.trade_intents,
       trading.lifecycle_events,
       trading.broker_references,
       trading.position_state,
       trading.reconciliation_state,
       trading.evidence_metadata
   TO "igtrdevfrc-execution-identity";

   GRANT INSERT, UPDATE ON trading.execution_cycle_claims
   TO "igtrdevfrc-execution-identity";
   GRANT INSERT, UPDATE ON trading.trade_intents
   TO "igtrdevfrc-execution-identity";
   GRANT INSERT ON trading.lifecycle_events
   TO "igtrdevfrc-execution-identity";
   GRANT INSERT ON trading.broker_references
   TO "igtrdevfrc-execution-identity";
   GRANT INSERT, UPDATE ON trading.position_state
   TO "igtrdevfrc-execution-identity";
   GRANT INSERT, UPDATE ON trading.reconciliation_state
   TO "igtrdevfrc-execution-identity";
   GRANT INSERT ON trading.evidence_metadata
   TO "igtrdevfrc-execution-identity";

   GRANT USAGE, SELECT ON SEQUENCE
       trading.lifecycle_events_sequence_seq
   TO "igtrdevfrc-execution-identity";

   GRANT EXECUTE ON FUNCTION trading.acquire_execution_lease(text, text, double precision)
   TO "igtrdevfrc-execution-identity";
   GRANT EXECUTE ON FUNCTION trading.renew_execution_lease(text, text, bigint, double precision)
   TO "igtrdevfrc-execution-identity";
   GRANT EXECUTE ON FUNCTION trading.release_execution_lease(text, text, bigint)
   TO "igtrdevfrc-execution-identity";
   GRANT EXECUTE ON FUNCTION trading.assert_execution_fence(text, text, bigint, text)
   TO "igtrdevfrc-execution-identity";
   GRANT EXECUTE ON FUNCTION trading.require_current_execution_fence()
   TO "igtrdevfrc-execution-identity";
   ```

The runtime role receives no `CREATE`, `CREATEROLE`, `CREATEDB`, migration
write, schema ownership, table deletion, or PostgreSQL administration rights.

## Revised alert semantics

The deployed `Replicas > 1` alert remains useful, but its meaning is now
`PLATFORM_OVERLAP_OR_SCALE_DRIFT`; it is not proof of two active traders. The
future execution-safety alerts use structured lease events.

Active lease-holder overlap:

```kusto
ContainerAppConsoleLogs
| where ContainerAppName == 'igtrdevfrc-execution-worker'
| extend payload = parse_json(Log)
| where tostring(payload.event) in ('execution_lease_acquired', 'execution_lease_renewed')
| extend replica_instance_id = tostring(payload.replica_instance_id), lease_holder = tobool(payload.lease_holder), lease_state = tostring(payload.lease_state), fencing_token = tolong(payload.fencing_token), heartbeat = tostring(payload.lease_heartbeat_state)
| where lease_holder == true and lease_state == 'ACQUIRED' and heartbeat == 'HEALTHY'
| summarize LastHeartbeat=max(TimeGenerated), ActiveTokens=dcount(fencing_token) by replica_instance_id, bin(TimeGenerated, 1m)
| where LastHeartbeat > ago(30s)
| summarize ActiveLeaseHolders=count(), ActiveTokens=sum(ActiveTokens) by bin(TimeGenerated, 1m)
| where ActiveLeaseHolders > 1 or ActiveTokens > 1
```

Lease loss or renewal failure:

```kusto
ContainerAppConsoleLogs
| where ContainerAppName == 'igtrdevfrc-execution-worker'
| extend payload = parse_json(Log)
| where tostring(payload.event) == 'execution_lease_fail_closed'
| where tostring(payload.lease_state) in ('LOST', 'DATABASE_UNAVAILABLE') or tostring(payload.lease_heartbeat_state) == 'FAILED'
```

Stale fence rejection:

```kusto
ContainerAppConsoleLogs
| where ContainerAppName == 'igtrdevfrc-execution-worker'
| extend payload = parse_json(Log)
| where tostring(payload.event) == 'stale_fencing_token_rejected'
```

These queries are designs only. No Azure alert was changed or deployed by
G4B-02B1.

## Test boundary

Unit tests cover role transitions, renewal, expiry, clean handoff, database
failure, ambiguous state, every fenced operation, distinct replica identities,
explicit `authorized=false`, managed-identity connection shape, and absence of
SQLite fallback. Remote CI supplies a pinned PostgreSQL 16 service and runs two
independent Python processes. Exactly one must acquire the lease; after that
leader process is terminated and its lease expires, the former standby must
acquire a strictly newer fencing token. A separate PostgreSQL test proves that
unfenced and stale writes are rejected.
