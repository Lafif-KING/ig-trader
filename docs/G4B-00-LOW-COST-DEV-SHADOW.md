# G4B-00 low-cost DEV/SHADOW Azure profile

Status: IMPLEMENTED AS CODE; VALIDATION ONLY; NO DEPLOYMENT AUTHORITY

Base: `b03dfc94c5ca88dcf75e7651410748fce80e52e8`

Target subscription is the operator's approved current/default Azure
subscription. The approved region is France Central, the future resource group
is `rg-igtrader-dev-frc-001`, and the approved prefix is `igtrdevfrc`.

This profile is a cost-reduced alternative for the first Azure DEV/SHADOW
environment. It does not replace or weaken the production-like G4A profile in
`foundation.bicep` and `app.bicep`. It creates no Demo or Live authority and
must not be deployed without a separate G4B-01 approval.

## Safety boundary

`dev-shadow-app.bicep` deploys the unchanged G4A cloud runtime with:

- `EXECUTION_MODE=NO_EXECUTION`;
- one process, one active revision and a steady-state target of one replica;
- 0.5 vCPU and 1 GiB memory;
- internal-only ingress;
- no Key Vault reference, broker secret, broker environment variable or secret value;
- a mandatory exact `repository@sha256:DIGEST` input and matching commit identity;
- startup, liveness and readiness probes plus the existing termination grace period.

The runtime itself still rejects supplied broker credentials, blocks broker
imports and outbound sockets, and exposes the zero-call health counters. The
legacy broker-capable application, image construction, PostgreSQL schema and CI
container acceptance architecture are unchanged.

## Foundation inventory

`dev-shadow-foundation.bicep` defines:

1. one 30-day Log Analytics workspace;
2. one VNet with only a delegated Container Apps `/23` subnet and delegated
   PostgreSQL `/28` subnet;
3. one private PostgreSQL DNS zone and VNet link;
4. one user-assigned execution identity;
5. one Basic ACR with the admin account disabled;
6. one `AcrPull` assignment for the execution identity;
7. one private, Entra-only PostgreSQL 16 Flexible Server using Burstable B1ms,
   32 GB storage, autogrow, no HA, no geo-backup and seven-day backup retention;
8. the unchanged `ig_trader` database and human-controlled Entra administrator;
9. one internal non-zone-redundant Container Apps Consumption environment; and
10. console, system and metric diagnostic routing to Log Analytics.

The application stage adds one internal, single-revision Container App sized
for one steady-state replica. No Key Vault,
ACR private endpoint, Key Vault private endpoint, private-endpoint subnet, NAT
Gateway, firewall or load balancer is defined.

## Security and reliability tradeoffs

Basic ACR does not support Private Link. Its public endpoint therefore remains
network reachable, although image pull still requires Azure authorization. The
registry admin account is disabled, the Container App uses its managed identity
with only `AcrPull`, and the app input must be an immutable digest. Before any
deployment, the push identity must be separately reviewed and the produced
digest must match CI evidence. This is a reduced network-isolation posture and
is approved only for the initial `NO_EXECUTION` DEV/SHADOW environment.

PostgreSQL retains private delegated networking and Entra-only authentication,
so database exposure and password shortcuts are not accepted. B1ms and the
absence of HA reduce availability and performance; a zone or host failure can
cause downtime, and seven-day retention narrows recovery history. These are DEV
availability tradeoffs only. They do not change the durable trading-state
schema, unknown-state fail-closed rules, migration discipline or singleton
fencing contract.

Key Vault is deferred because this composition cannot accept or use an IG
credential. No secret may be placed in an app setting, parameter file, registry,
database or source repository as a shortcut. Before a separately approved
`SHADOW_DEMO` composition introduces any broker credential, Key Vault, private
access, managed-identity secret references, secret redaction and an explicit
security review are mandatory. The unchanged G4A hardened profile preserves the
reference architecture for that work.

The Container Apps environment is not zone redundant, and the worker targets
one steady-state replica. This is intentional for DEV cost. Azure platform
operations can briefly overlap replicas, so execution singleton safety must
come from the PostgreSQL lease and fencing token, not replica count.
Operational continuity comes from immutable-image redeployment plus fenced
lease handoff, not concurrent execution authority.

## Logging and evidence

Log Analytics retention is 30 days. Console logs, system logs and metrics remain
enabled so the operator can prove startup, health, readiness, restart/failure,
execution mode and release identity. The existing cloud runtime also reports
the order, IG REST and network counters, which must remain zero. Logging must
not include credentials, authorization headers, tokens, account identifiers,
request bodies or private addresses.

## Cost and budget guardrail

The target run-rate is approximately EUR 40-60 per month at low DEV usage,
subject to current retail rates, log volume, data transfer, taxes, reservation
status and subscription pricing. This is not a guaranteed invoice amount.

Before deployment approval, prepare a EUR 60 monthly budget and require
management review if estimated run-rate exceeds EUR 75. Configure actual-cost
alerts at 50%, 75%, 90% and 100%, plus forecast alerts at 80% and 100%. Budget
alerts notify; they do not automatically stop resources.

## Immutable rollback

Rollback preserves `NO_EXECUTION` and never increases replica count. Retain the
prior reviewed `repository@sha256:DIGEST` and matching 40-character commit SHA.
Run what-if with those identities, then redeploy the prior digest as a new
single revision. The template retains up to ten inactive revisions, but the
registry digest and CI evidence are the durable rollback source. Stop if the
prior digest is absent, tag-only, mismatched, or would alter the database,
identity, replica count or execution mode.

## Deployment blockers

G4B-01 remains prohibited until all of these are separately reviewed:

- required providers are `Registered` and France Central quotas remain adequate;
- the resource group creation is explicitly approved;
- exact global names are rechecked;
- the Entra PostgreSQL administrator object and principal name are verified;
- an authorized, non-admin image push path and immutable digest exist;
- subscription-scope what-if shows only the expected DEV/SHADOW resources;
- the estimated run-rate remains at or below the EUR 75 management threshold;
- no broker credential, secret reference, Demo mode or Live mode is present.
