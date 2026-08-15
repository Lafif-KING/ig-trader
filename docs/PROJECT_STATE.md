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
