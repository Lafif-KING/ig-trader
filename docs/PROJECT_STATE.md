# IG Trader project state

Last updated: 2026-08-15

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

The accepted sample contains 1,917 instrument-decisions, of which 1,624 are
valid. The frozen Scalper produces 20 candidates. Ten selected candidates fail
the spread/target-ratio limit, eight fail closed on unknown account state, and
two same-cycle candidates are suppressed by the one-execution rule. No
TradeIntent or paper trade is accepted, so performance evidence is `NO_TRADES`.
Replay integrity is `PASS_REPLAY_INTEGRITY`, while final strategy disposition
remains `HUMAN_REVIEW_REQUIRED`. The command and limitations are documented in
`docs/G3B-01-EXACT-FROZEN-SCALPER-REPLAY.md`.

## G4A

G4A adds a separate container/cloud entry point that accepts only
`NO_EXECUTION`. It exposes liveness/readiness/release identity, emits structured
logs, drains on termination, blocks broker imports and outbound connections, and
requires no credential. The legacy broker-capable `main.py` is unchanged.

The production container is a locked two-stage, non-root build. CI verifies the
lock, full suite, Ruff, format, secret scans, image build, container lifecycle,
commit metadata and zero order calls. Azure Bicep defines an internal singleton
Container App (`minReplicas=1`, `maxReplicas=1`), managed identity, private ACR
and Key Vault, Log Analytics, and private Entra-only PostgreSQL. SQLite remains
the deterministic offline test backend; the versioned PostgreSQL schema is a
separate future cloud-persistence contract.

The implementation and rollback procedure are documented in
`docs/G4A-01-CLOUD-FOUNDATION.md`. Docker and Azure CLI were unavailable in the
authoring environment, so deployment status is `CLOUD_ACCESS_REQUIRED`; no
cloud resource or broker session was created.
