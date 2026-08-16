# G4A-01 cloud foundation

Status: IMPLEMENTED AS CODE; NO EXECUTION AUTHORITY; CLOUD ACCESS REQUIRED

Base: `074a72351af385a1516695b04f8674138ebfb7c1`

This work order creates a separate cloud composition for the accepted G1/G2/G3A
repository. It does not change the legacy broker-capable `main.py`, does not
authenticate to IG, and does not grant Demo or Live order authority.

## Safe container architecture

The image has two build stages. The builder uses Poetry `2.4.1` and
`poetry.lock`; the runtime receives only the virtual environment and application
source. Both stages use the same exact Python `3.13.14` bookworm image manifest
digest. The runtime is UID/GID `10001`, writes logs to stdout as JSON, exposes
port `8080`, includes a Docker health check, and receives `SIGTERM` directly as
the Python process.

The cloud entry point is `python -m src.ig_trader.cloud_runtime`.

It accepts only `EXECUTION_MODE=NO_EXECUTION`. If the variable is absent it
still chooses `NO_EXECUTION`. Demo, Live, `PAPER_TRADING=false`, Live IG
configuration, or any supplied broker credential causes startup to fail before
the listener opens. The process installs irreversible guards that block imports
of credential, IG session, REST, streaming, market-data and execution modules,
and block every outbound socket connection. Incoming health requests remain
available.

The execution worker has one-replica steady-state sizing:

```text
process workers = 1
Container Apps min replicas = 1
Container Apps max replicas = 1
active revisions mode = Single
```

Azure can temporarily overlap replicas during platform operations even with
that sizing. G4A keeps every process disabled and unauthorized. A later
controlled execution composition must use the PostgreSQL execution lease and
fencing token documented in `G4B-02B1-EXECUTION-LEASE.md`; changing an
environment variable cannot activate this image.

## Health contract

- `GET /health/live`: process/event-loop liveness.
- `GET /health/ready`: readiness; changes to HTTP 503 while draining.
- `GET /health`: readiness alias.

Every successful health document includes the Git commit, application version,
container revision, explicit execution mode, authorization state, steady-state
replica policy, credential requirement, broker-module state, and safety
counters. `order_endpoint_call_count`, `ig_rest_call_count`, and
`network_call_count` must remain zero.

On `SIGTERM` or `SIGINT`, readiness fails first, the listener closes, in-flight
health requests receive up to the configured grace period, and the process logs
`cloud_shutdown_requested` followed by `cloud_service_stopped` before exiting
zero.

## CI and image identity

`.github/workflows/ci.yaml` is read-only with respect to the repository and
runs these gates in order:

1. exact Python and Poetry setup;
2. lock validation and synchronized install;
3. complete pytest suite;
4. Ruff lint;
5. Ruff formatting;
6. exact-version Bicep lint and compilation;
7. dependency-free redacting secret-pattern scan plus checksum-verified
   Gitleaks `8.30.1` directory scan;
8. commit-addressed image build;
9. container liveness/readiness/start/stop and zero-broker-call acceptance;
10. image inspection and build-input SHA-256 evidence upload.

All reusable GitHub Actions are pinned to full commit SHAs. The image embeds
`GITHUB_SHA` in both an OCI revision label and health metadata. The uploaded
artifact is named `cloud-foundation-COMMIT_SHA` and contains the image inspect
document, container-smoke evidence, and Dockerfile/lockfile hashes. The workflow
does not push or deploy an image and receives no cloud or broker credential.

## Azure design

The target is an internal Azure Container Apps environment in a delegated VNet.
`infra/azure/foundation.bicep` provisions:

- internal, zone-redundant Container Apps environment;
- Premium Azure Container Registry with admin login and public access disabled;
- user-assigned managed identity with only `AcrPull` and Key Vault Secrets User;
- RBAC Key Vault with purge protection, public access disabled and private endpoint;
- Log Analytics with resource-specific Container Apps diagnostic logs;
- Azure Database for PostgreSQL Flexible Server 16 with private delegated subnet,
  private DNS, zone-redundant HA, 14-day backups, Entra-only authentication and
  no public endpoint.

`infra/azure/app.bicep` deploys a digest-addressed image to the single-revision
Container App. It sets `NO_EXECUTION`, configures startup/liveness/readiness
probes and provides a 30-second platform termination grace period. The current
broker-secret-reference switch defaults false and must remain false for G4A.
If it is changed, this image still rejects the credential-bearing process, so
the switch does not create trading authority.

The templates are split because infrastructure must exist before an image can
be built in the private registry. A secure future flow is:

1. deploy or update the foundation with Azure Resource Manager;
2. build the reviewed commit through an identity-authenticated ACR build or an
   approved VNet-connected builder;
3. record the resulting `repository@sha256:DIGEST` identity;
4. deploy `app.bicep` using that digest and matching full commit SHA;
5. require startup and readiness success before accepting the new revision.

The design follows Microsoft's documented Container Apps Key Vault secret
reference and managed-registry identity properties, private PostgreSQL delegated
subnet/private DNS requirements, and single-revision readiness behavior:

- https://learn.microsoft.com/azure/templates/microsoft.app/containerapps
- https://learn.microsoft.com/azure/container-apps/revisions
- https://learn.microsoft.com/azure/postgresql/network/concepts-networking-private
- https://learn.microsoft.com/azure/postgresql/security/security-connect-with-managed-identity

## Secret architecture

No secret value exists in Git, Bicep parameters, image layers, health output, or
documentation. The G4A Container App receives no broker secret at all. The
reserved future design uses versionless Key Vault URIs, the user-assigned
managed identity, and Container Apps `secretRef` environment bindings. Registry
pull uses the same identity and no registry password.

PostgreSQL uses Microsoft Entra authentication. A platform administrator must
create a least-privilege database principal representing the execution managed
identity before a future persistence adapter can connect. The token is obtained
at runtime and never stored. Do not paste a token, password, key, account ID,
private address, or raw secret into chat, a parameter file, or a command log.

## Persistence architecture

SQLite remains unchanged for deterministic offline G2 tests and the existing
local candle store. Cloud execution persistence is a separate backend and is
not opened by `NO_EXECUTION` startup.

`migrations/postgresql/001_execution_state.sql` defines durable PostgreSQL
records for:

- `trade_intents` with idempotency, input hash, mode, payload and optimistic version;
- append-only `lifecycle_events`;
- broker references that can only represent `ACCEPTED` confirmation;
- strategy-owned `position_state`, including unknown/reconciliation-required states;
- `reconciliation_state` checkpoints that distinguish known, unknown and blocked;
- append-only evidence metadata with content SHA-256;
- a single named worker lease with fencing token as defense in depth.

The future adapter must transact intent state, lifecycle event and version
change together; refuse missing or corrupt state; use IG as broker-position
truth; use the strategy registry as ownership truth; and acquire both the
steady-state Container Apps topology and database fencing lease before any order.
The schema does not replace or migrate offline SQLite data.

Migrations use expand-then-migrate-then-contract discipline. They run as a
separate, identity-controlled deployment job, never automatically in every
worker replica. The SQL is prepared but intentionally not applied because this
environment has no Azure authentication.

## Logging and alerts

Application logs are JSON with event name, timestamp, level and safe operational
metadata. Credential fields, headers, broker tokens, account identifiers,
request bodies, database tokens and Key Vault values are forbidden. Azure
diagnostic settings route console/system logs and metrics to a 90-day Log
Analytics workspace without embedding the workspace shared key in the template.

Required alerts for the deployment gate are:

- critical: any `cloud_start_rejected` or `cloud_service_failed` event;
- critical: any reported order, IG REST, or successful outbound network counter above zero;
- critical: steady-state replica drift, with overlap classified as
  `PLATFORM_OVERLAP_OR_SCALE_DRIFT` rather than proof of multiple active workers;
- critical: more than one active execution lease holder or fencing generation;
- high: lease lost, lease renewal failure, or stale fencing token rejection;
- high: readiness remains failed or revision restarts repeatedly for five minutes;
- high: PostgreSQL unavailable, storage above 80%, HA unhealthy, or backup failure;
- medium: no `cloud_service_started`/health telemetry in the expected interval.

Alert action groups must target an operator-controlled channel and must not
contain a broker credential. Paging does not authorize execution.

## Rollback runbook

Rollback always preserves `EXECUTION_MODE=NO_EXECUTION`. It redeploys a previous
known-good image digest as a new single revision; it never retags `latest` and
never rolls the database backward destructively.

At the time of a real incident, use one step at a time and stop after each step
until the sanitized result is understood.

1. Place: Windows PowerShell. Action: set the resource group variable with
   `$resourceGroup = "replace-with-resource-group-name"`. This only stores a
   non-secret name. Expected result: no output. Stop if the prompt changes to
   `>`; press `Ctrl+C` and correct the quote before continuing.
2. Place: Windows PowerShell. Action: run
   `az containerapp revision list --resource-group $resourceGroup --name replace-with-container-app-name --output table`.
   This lists revision identities without secrets. Expected result: one active
   revision and older inactive revisions. Stop if the subscription, resource
   group, or app name is not the intended one.
3. Place: the approved build-evidence store. Action: copy only the previous
   reviewed `repository@sha256:DIGEST` and its matching commit SHA. Expected
   result: both identities match the stored CI artifact. Stop if either is
   missing, tag-only, or mismatched.
4. Place: Windows PowerShell. Action: run an ARM `what-if` for `app.bicep` with
   the known-good digest, matching commit, `enableBrokerSecretReferences=false`
   and `NO_EXECUTION`. Expected result: one Container App revision change and no
   deletion. Stop on any database, Key Vault, identity, replica-count or mode change.
5. Place: Windows PowerShell. Action: execute the same reviewed deployment.
   Expected result: the new revision reaches startup and readiness and the old
   revision is retained inactive. Stop if readiness fails; Container Apps single
   revision mode keeps traffic on the old ready revision.
6. Place: Log Analytics. Action: verify health metadata, structured startup log,
   commit SHA and all broker/network/order counters at zero. Expected result:
   `NO_EXECUTION`, min/max replicas one, readiness pass and zero counters. Stop
   and escalate on any discrepancy.

Forbidden during rollback: deleting or purging Key Vault; deleting PostgreSQL;
displaying secrets; setting Demo or Live mode; enabling broker secret references;
scaling above one; running an order probe; or rewriting migration history.

## Current infrastructure blocker

This workstation has neither Docker nor Azure CLI available, and no Azure
subscription authentication was presented. The definitions and offline tests
can be completed, but the image build/container smoke, Bicep `what-if`, resource
deployment, ACR push, cloud health check and Azure alerts cannot be executed
here. Status therefore remains `CLOUD_ACCESS_REQUIRED`; no secret should be
requested in chat to change that status.
