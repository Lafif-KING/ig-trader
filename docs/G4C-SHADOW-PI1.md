# G4C Shadow PI1

## Scope

`SHADOW_DEMO` is a broker-neutral, hypothetical execution path. It has
permanent `authorized=false`, `order_authority=false`, and
`broker_order_call_count=0`. It cannot import an execution adapter or contain
broker order endpoint paths.

## Frozen configuration and boundaries

The runtime consumes the accepted `FrozenV1Config` and `PortfolioRisk` from
the offline-paper conductor. This is the one frozen V1 policy source: RSI 7,
confidence 0.70, ADX 20, 60 candles, ATR x2, 12-pip stop cap, 1.5 reward/risk,
1.2-pip spread cap, 0.15 spread/target cap, and one total/cycle/instrument
position. The allowlist is EUR/GBP, EUR/USD, and GBP/USD Mini only.

The orchestration boundary requires broker-neutral ports for account state,
instrument metadata, current BID/OFFER, and finalized-candle ATR. Missing,
future, stale, crossed, non-finite, or otherwise inconsistent state produces
`FAILED_SAFE` or `NO_TRADE`. Stop price, target price, daily loss, and open
position count are not public cycle inputs.

## Lifecycle, fencing, and restart

The durable lifecycle is `SHADOW_INTENT_CREATED`, `OPEN`, `CLOSED`,
`RECONCILED`, or `FAILED_SAFE`. Existing records are reported using their
actual durable state: `SHADOW_OPEN`, `SHADOW_CLOSED`, `SHADOW_RECONCILED`, or
`FAILED_SAFE`; a closed record is never reported as open. Global cycle IDs
exclude EPIC, so a cycle can claim only one intent across the frozen universe.
Identical retries are idempotent and conflicting retries fail closed.

`PostgresShadowStore` uses the current execution-lease fencing token before
every write and relies on the database-fenced transaction for stale-write
rejection. Active positions include both created intents and open positions.
The disposable PostgreSQL CI proofs cover restart, read-only counting, and
zero-row stale writes.

## Risk and performance

The trusted decision path derives the executable entry from OFFER for BUY and
BID for SELL. It computes `min(ATR x 2, 12 pips)`, then target distance at 1.5
times the stop distance. BID/OFFER spread must be at most 1.2 pips and at most
15% of target distance. Performance evidence contains raw price delta, pips,
R multiple, timestamps, and exit reason only; it makes no cash-P&L claim.

## Limitations and deployment dependency

This branch does not authenticate to IG, connect to Lightstreamer, or deploy to
Azure. The following cloud-preparation branch must provide read-only market
adapters, finalized-candle assembly, health checks, a broker-safe image, and
deployment contracts. GitHub CI, including disposable PostgreSQL tests, is
required before integration.
