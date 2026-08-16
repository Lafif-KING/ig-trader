# G4B-02B2A database bootstrap foundation

This work order builds and validates tooling only. It does not deploy a job,
change an Azure PostgreSQL administrator, execute SQL in Azure, publish an
image, restart the application, or enable broker execution.

## Reviewed inputs and finite commands

The dedicated `Dockerfile.db-bootstrap` uses the same pinned Python base and
locked production dependency set as the application image. Its project inputs
are limited to `db_bootstrap.py`, `execution_lease.py`, the package marker, and
the two reviewed migrations. It runs as UID/GID 10001 and has these two modes:

- `python -m ig_trader.db_bootstrap bootstrap-admin` performs the one-time
  administrative bootstrap.
- `python -m ig_trader.db_bootstrap runtime-probe` proves the permanent runtime
  identity's non-admin access, lease lifecycle, and stale-fence rejection.

The reviewed migration identities are immutable inputs:

| Migration | SHA-256 |
| --- | --- |
| `001_execution_state.sql` | `42dcbe2b47c5fed8223a4831d8c594e78c3180f454b71e15358819a9039c8800` |
| `002_execution_lease_fencing.sql` | `731b918b573ee232aab3fa709e7a41b5ac03e11f4f81d08458f8fcefcb16599c` |

The module refuses to open the database when either file differs. It inventories
the `trading` schema's relations, sequences, functions, triggers, required
column constraint, and migration ledger. Blank means apply 001 and then 002;
complete 001 plus absent 002 means apply 002; both complete means verify only.
Any partial object set, unknown object, unknown ledger entry, missing ledger
entry, or checksum mismatch fails closed as `DATABASE_SCHEMA_DRIFT`.
Each migration body and its reviewed ledger row commit atomically, so a process
failure cannot leave a silently accepted schema without its checksum record.

## Identity boundary

The permanent database role name is
`igtrdevfrc-execution-identity`. The future deployment resolves its object ID
from the existing runtime UAMI's `principalId`; no object ID is stored in Git.
The temporary identity is `igtrdevfrc-db-bootstrap-identity`. It is distinct
from the runtime and GitHub publisher identities and receives only a temporary
ACR `AcrPull` assignment and temporary PostgreSQL Entra administrator child.

The bootstrap connects as that exact temporary service principal. Entra role
creation runs only against PostgreSQL's required `postgres` system database;
schema inspection, migrations, grants, and privilege verification run against
`ig_trader`. It creates or verifies the runtime mapping with parameterized SQL
equivalent to:

```sql
SELECT *
FROM pg_catalog.pgaadauth_create_principal_with_oid(
    'igtrdevfrc-execution-identity',
    runtime_object_id_resolved_from_existing_uami,
    'service',
    false,
    false
);
```

The postcondition is exact object-ID equality, principal type `service`, and no
admin, superuser, `CREATEROLE`, `CREATEDB`, `azure_pg_admin`, or owned trading
objects. An existing role with a different object ID fails closed.

## Exact runtime authorization

The bootstrap first revokes direct privileges and then grants only:

- database `CONNECT`; trading schema `USAGE`;
- `SELECT` on `schema_migrations` and `worker_leases`;
- `SELECT, INSERT, UPDATE` on `execution_cycle_claims`, `trade_intents`,
  `position_state`, and `reconciliation_state`;
- `SELECT, INSERT` on `lifecycle_events`, `broker_references`, and
  `evidence_metadata`;
- `USAGE, SELECT` on `lifecycle_events_sequence_seq`;
- `EXECUTE` on `acquire_execution_lease`, `renew_execution_lease`,
  `release_execution_lease`, `assert_execution_fence`, and
  `require_current_execution_fence` with their reviewed signatures.

The role receives no database or schema `CREATE`, table `DELETE`, direct
`INSERT/UPDATE/DELETE` on `worker_leases`, migration authority, arbitrary DDL,
ownership, or role-administration authority. The job reads back effective
privileges and fails if a required privilege is missing or a prohibited one is
present.

## Token and networking boundary

Both jobs reference the existing `igtrdevfrc-aca-env` so they inherit its
private network path. PostgreSQL remains private: no public access, firewall,
public IP, VNet, subnet, or managed environment is created. Each mode selects
its exact user-assigned identity, requests the Azure PostgreSQL scope
`https://ossrdbms-aad.database.windows.net/.default`, holds the access token in
memory only, and uses TLS with a bounded connect and statement timeout.
Passwords and DSNs in environment variables are rejected. Logs and evidence
contain only `token_acquired=true/false`; tokens are never serialized.

The runtime probe connects as the permanent non-admin role, verifies the exact
authorization set, acquires and renews the `execution-worker` lease, validates
a current fencing token, releases it, acquires a newer generation, proves the
old token is rejected, and releases the successor. It imports no broker client
and grants no execution authority.

## Future approved deployment and teardown order

These actions require a separate work order and must not be run during
G4B-02B2A:

1. Publish and approve an immutable bootstrap image digest.
2. Run the Bicep identity-only phase to create only the temporary bootstrap
   UAMI, then read and verify its generated `principalId`.
3. Supply that exact object ID to the full phase; ARM requires the PostgreSQL
   administrator child name before deployment begins. Add its ACR-scope
   `AcrPull` assignment.
4. Add the temporary UAMI as the PostgreSQL Entra administrator child.
5. Create both finite manual Container Apps Jobs in the existing environment.
6. Start the bootstrap job once and accept only sanitized passing evidence.
7. Start the runtime-probe job once and accept only sanitized passing evidence.
8. Delete the runtime-probe and bootstrap Jobs.
9. Delete the PostgreSQL temporary administrator child.
10. Delete the temporary ACR role assignment.
11. Delete the temporary bootstrap UAMI.
12. Verify the runtime role remains non-admin and the application remains
    `NO_EXECUTION` with one ready replica.

If either job fails, do not retry automatically and do not remove evidence.
Stop for schema, identity, privilege, or networking review. The permanent
runtime UAMI must never be made PostgreSQL administrator.

## IaC ownership, cost, and safety

`dev-shadow-db-bootstrap.bicep` owns exactly five temporary ARM resources: the
bootstrap UAMI, its ACR role assignment, its PostgreSQL administrator child,
the bootstrap Job, and the runtime-probe Job. The Container Apps environment,
ACR, PostgreSQL server, and runtime UAMI are existing/reference-only. A
read-only What-If must show no unrelated modification or deletion.

At the configured ceilings, one ten-minute bootstrap and one five-minute probe
consume at most 450 vCPU-seconds and 900 GiB-seconds of Container Apps job
compute in total. The incremental charge should be negligible or small,
depending on the then-current regional meter and free grant; this is planning
guidance, not a guaranteed Azure charge. The existing EUR 60 monthly DEV budget
is unchanged.

Throughout this foundation: Azure creates/modifies/deletes are zero, the
running revision is unchanged and `NO_EXECUTION`, IG credentials and connections
are zero, and Demo/Live/order/position/working-order activity is zero.
