# IG Trader project state

Last updated: 2026-08-16

## Active execution scope

The active broker-facing bot remains the existing single-instrument EUR/GBP
Mini path. G2 does not activate, qualify or modify that path and grants no Demo
or Live order authority.

## G1

G1 read-only IG Demo authentication remains represented by base commit
`46244bc04b6282d62299dfeee16e20c7abbc701d`. Its 42 offline tests remain the
focused compatibility gate. G2 adds only the system-trust helper/dependency
that the accepted G1 diagnostic already imported but the base commit omitted.

## G2

G2 adds a separate `OFFLINE_PAPER` composition for the frozen
EURGBP/EURUSD/GBPUSD research universe. It uses local synthetic candles, the
exact frozen Scalper, deterministic ranking, PortfolioRisk veto, position
sizing, a persistent TradeIntent state machine, SQLite PaperBroker, stop/target
exit, restart recovery, reconciliation and evidence lineage.

The command and full safety boundary are documented in
`docs/G2-01-OFFLINE-PAPER-E2E.md`. Import and socket guards prevent the offline
composition from resolving credentials or constructing IG REST/Lightstreamer
clients. Unknown or mismatched state fails closed.

## G3A

G3A accepted the immutable IG Demo historical package for the frozen
EURGBP/EURUSD/GBPUSD research universe and classified the EURGBP 1M
`2026-08-14T19:03:00Z` omission as an authoritative gap. G3A grants no order
authority and does not change the active bot's EURGBP-only execution scope.

## G3B

G3B adds a separate broker-isolated exact historical replay composition. It
re-verifies the complete external G3A package, aligns only already-closed 1M,
5M, 15M, and 1H candles, enforces `GAP_AWARE_REPLAY_V1`, calls the existing
frozen Scalper and G2 risk/domain contracts, uses bid/offer execution semantics,
and generates deterministic create-only evidence.

G3B-01 established replay integrity but correctly failed eight selected GBPUSD
candidates closed because the G3A market package has no account state. G3B-02
reuses the accepted deterministic G2 account fixture and exact
`AccountPort`/PaperBroker/PortfolioRisk path. The fixture and qualification
account state are hash-pinned; an unavailable or changed source stops as
`QUALIFICATION_ACCOUNT_STATE_GAP`.

The sample remains 1,917 instrument-decisions, 1,624 valid decisions and 20
candidates. Ten candidates fail the spread/target-ratio limit, two same-cycle
candidates are suppressed, four are vetoed by authoritative open-position
state, and four receive accepted local PaperBroker fills. All four close at
their stop for approximately -16 spread-adjusted pips and -4R. This limited
sample is `NEGATIVE_ON_AVAILABLE_SAMPLE`; strategy disposition remains
`HUMAN_REVIEW_REQUIRED` and no Demo or Live execution is authorized. The exact
account dependency, candidate audit and limitations are documented in
`docs/G3B-02-ACCOUNT-STATE-COMPLETE-FROZEN-REPLAY.md`.

## G4A

G4A adds a separate container/cloud entry point that accepts only
`NO_EXECUTION`. It exposes liveness/readiness/release identity, emits structured
logs, drains on termination, blocks broker imports and outbound connections, and
requires no credential. The legacy broker-capable `main.py` is unchanged.

The production container is a locked two-stage, non-root build. CI verifies the
lock, full suite, Ruff, format, secret scans, image build, container lifecycle,
commit metadata and zero order calls. Azure Bicep defines an internal
single-revision Container App (`minReplicas=1`, `maxReplicas=1`), managed
identity, private ACR
and Key Vault, Log Analytics, and private Entra-only PostgreSQL. SQLite remains
the deterministic offline test backend; the versioned PostgreSQL schema is a
separate future cloud-persistence contract.

The implementation and rollback procedure are documented in
`docs/G4A-01-CLOUD-FOUNDATION.md`. Docker and Azure CLI were unavailable in the
authoring environment, so deployment status is `CLOUD_ACCESS_REQUIRED`; no
cloud resource or broker session was created.

## G4B

G4B-00 authenticated Azure preflight accepted France Central, resource group
`rg-igtrader-dev-frc-001` and prefix `igtrdevfrc`, but rejected the G4A
production-like profile for the first DEV environment on cost grounds. The G4A
templates remain unchanged as the future hardened profile.

The separate `dev-shadow-*` Bicep profile keeps the internal single-revision
Container App (`minReplicas=1`, `maxReplicas=1`), immutable image identity,
managed-identity ACR pull, private Entra-only PostgreSQL, durable schema and
operational logging. It uses Basic ACR, Burstable B1ms PostgreSQL 16 with 32 GB,
no HA, seven-day backups and 30-day logs. Key Vault and ACR/Key Vault private
endpoints are intentionally deferred because `NO_EXECUTION` accepts no broker
credential. The decision and mandatory future security gates are documented in
`docs/G4B-00-LOW-COST-DEV-SHADOW.md`.

G4B-02A2 proved that Container Apps can temporarily overlap ready replicas
during a platform revision restart even when min/max remain one. Replica count
therefore remains a steady-state cost and drift control, not the trading
singleton guarantee. G4B-02B1 reuses the accepted PostgreSQL
`trading.worker_leases` table and adds atomic acquisition, heartbeat, expiry,
monotonic fencing, fenced execution-state writes, explicit replica roles, and
managed-identity token authentication design. The current runtime remains
`NO_EXECUTION`, emits `authorized=false`, does not connect to PostgreSQL, and
has no broker authority. See `docs/G4B-02B1-EXECUTION-LEASE.md`.

G4B-02B2A adds source, a deliberately narrow non-root image, stage-isolated
Bicep, CI gates, and an operator runbook for a future finite database bootstrap
and runtime-identity probe. It hash-pins migrations 001/002, fails closed on
partial or unknown schema state, maps the permanent UAMI by exact Entra object
ID, and verifies least privilege plus real PostgreSQL lease/fencing behavior.
No Azure or database mutation is authorized by this source-only stage. See
`docs/G4B-02B2A-DB-BOOTSTRAP.md`.
