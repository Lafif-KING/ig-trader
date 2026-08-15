# G2-01 broker-isolated OFFLINE_PAPER E2E

Status: IMPLEMENTED; PAPER ONLY; NO IG DEMO OR LIVE AUTHORITY

## Reproducible command

Run this in Windows PowerShell from the repository root:

```powershell
$env:PYTHONPATH="."
poetry run python -m src.ig_trader.offline_paper --mode OFFLINE_PAPER --input .\fixtures\g2-offline-paper-market.json --state-directory .\.runtime\g2-offline-paper --evidence-json .\.runtime\evidence\g2-offline-paper-e2e.json --evidence-markdown .\.runtime\evidence\g2-offline-paper-e2e.md
```

The command accepts only `OFFLINE_PAPER`. It creates no IG session, REST
client, Lightstreamer client, production `trading.db` connection, or credential
settings object. Output evidence is create-only so an earlier result cannot be
silently overwritten.

## Isolation boundary

The package launcher installs process-level guards before importing pandas,
the Scalper, or orchestration code. The guards:

- reject socket creation, connection, DNS resolution, and child-process launch;
- reject imports of the IG session, HTTP client, streaming client, production
  market-data client, execution adapter, and credential configuration;
- reject unsafe process mode variables and every mode other than
  `OFFLINE_PAPER`;
- expose zero-success counters and separate blocked-attempt counters.

The complete command must report:

```text
network_call_count=0
ig_rest_call_count=0
lightstreamer_connection_count=0
order_endpoint_call_count=0
```

No `.env` file is loaded. The package root was changed to lazy public imports
because its previous import-time behavior resolved settings and created the
production database before an offline mode could install its safety boundary.

## Broker-neutral architecture

The strategy and risk path uses these structural interfaces:

- `MarketDataPort`
- `HistoricalDataPort`
- `ExecutionPort`
- `AccountPort`
- `ReconciliationPort`

Only the local SQLite `PaperBroker` may satisfy `ExecutionPort` in this
composition. A different or wrapped execution object is rejected locally. The
canonical objects are `Quote`, `Candle`, `Signal`, `TradeCandidate`,
`RiskDecision`, `TradeIntent`, `BrokerOrder`, `Fill`, `Position`, `Exit`, and
`AccountSnapshot`. No IG response object enters the Scalper or PortfolioRisk.

## Frozen V1

The offline conductor rejects changes to these values:

- exact ordered universe: EURGBP Mini, EURUSD Mini, GBPUSD Mini;
- existing `ScalperStrategy` only;
- RSI 7, confidence 0.70, ADX 20, warm-up 60 candles;
- stop ATR x 2, reward:risk 1.5, maximum stop 12 pips;
- maximum spread 1.2 pips and spread/target ratio 0.15;
- one total position, one per instrument, and one execution per cycle;
- AI trading authority, optimization, tuning, advanced management and
  autonomous intraday authority disabled.

The position-sizing policy preserves the accepted legacy Scalper allocation:
30% of account balance and 0.5% risk of that strategy allocation. Pip size,
account-currency pip value, minimum size and minimum stop are explicit fixture
fields. Missing values block; none are inferred.

## Persistent lifecycle and recovery

The append-only state transition sequence is:

```text
SIGNAL_DETECTED
-> INTENT_CREATED
-> ORDER_SUBMITTED
-> ORDER_ACCEPTED | ORDER_REJECTED
-> POSITION_OPEN
-> EXIT_REQUESTED
-> POSITION_CLOSED
-> RECONCILED
```

`RISK_REJECTED` records PortfolioRisk vetoes without creating an order.
`FAILED_SAFE` is terminal until state is externally reconciled. A complete
TradeIntent, including exact input hashes, signal inputs, confidence, spread,
risk result, size, stop, target and execution mode, is durable before
`PaperBroker.submit` is allowed.

Paper orders, fills, positions and exits use deterministic references and exact
payload comparison. A restart may safely resume every durable boundary because
PaperBroker submission and close are idempotent. A changed input/configuration,
orphan record, partial record, corrupt JSON, unknown account, ambiguous exit,
stale quote, or mismatched intent blocks all new execution.

## Evidence lineage

The SQLite evidence journal and final JSON retain this chain:

```text
Market/Candle Input
-> Strategy Calculation
-> Signal
-> Candidate
-> Ranking
-> PortfolioRisk
-> Position Sizing
-> TradeIntent
-> Execution
-> Confirmation
-> Position
-> Exit
-> Reconciliation
```

The bundled source is explicitly labelled synthetic deterministic test data.
Expanded candles and the source document receive SHA-256 fingerprints. It is
not represented as downloaded, historical, or broker-supplied market evidence.

## Limits

PaperBroker proves local orchestration, fail-closed state handling,
idempotency, persistence and deterministic fill accounting. It does not prove
IG order semantics, slippage, liquidity, latency, profitability or real
execution quality. G2 grants no authority to place an IG Demo or Live order.

## G1 compatibility repair

The accepted G1 diagnostic at the required base SHA imported
`build_system_ssl_context`, but that symbol and its `truststore` dependency were
absent from the base implementation/lock. G2 carries the minimal independent
repair: one verified system-trust context factory and the dependency
declaration. No G1 allow-list, endpoint, authentication, token, account,
streaming or report logic changed.
