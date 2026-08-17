# IG Trader project rules

## Purpose and priorities

This is a single-user automated IG trading robot. Priorities are:

1. Safety
2. Determinism
3. Evidence-driven strategy development
4. Low operating cost
5. Lean implementation

Do not introduce enterprise infrastructure unless a work order explicitly authorizes it.

## Roles

- User: product owner and sole real-money risk approver.
- ChatGPT: architecture, roadmap, QA, and gate authority.
- Codex: implementation engineer.

## Execution authority

Supported modes are `NO_EXECUTION`, `SHADOW_DEMO`, `DEMO_EXECUTION`, and
`LIVE_EXECUTION`. The default is `NO_EXECUTION`. Never increase execution
authority without explicit work-order authorization.

## Broker pipeline

The pipeline is Market Data → Strategy → SignalCandidate → Decision →
PortfolioRisk → Execution Authority → Broker. Strategies never call the broker
directly. PortfolioRisk always has absolute veto.

## Instruments and strategies

The qualified universe is EURGBP, EURUSD, and GBPUSD. USDJPY and XAUUSD are
research expansion only; new instruments begin in research/shadow. S0 Frozen
Scalper is the baseline/champion. S1 Intraday Momentum, S2 Trend/Breakout,
S3 FVG + Liquidity Sweep + CISD, S4 Session Sweep, and S5 Regime Mean
Reversion are future challengers. Do not modify frozen S0 parameters without
explicit approval.

Supported timeframes are 4H, 1H, 15M, 5M, and 1M; the default core is 1H,
15M, and 5M.

## Cloud persistence and singleton safety

Cloud persistence is PostgreSQL. Durable owner: `ig_trader_schema_owner`.
Runtime principal/UAMI: `igtrdevfrc-execution-identity`. Runtime remains
non-admin, non-owner, and least privilege. There is no SQLite fallback in
cloud. SQLite is for deterministic offline tests only.

Azure `maxReplicas=1` is not the trading singleton guarantee. The guarantee is
the PostgreSQL execution lease plus monotonic fencing token; stale tokens must
be rejected.

## Azure and cost

The target architecture is deliberately low-cost. Avoid services unless
required. Planning target is approximately EUR 35–60/month; review recurring
architecture above EUR 75/month.

## Security

Never print or persist Azure/ACR access tokens, IG credentials, PostgreSQL
Entra tokens, passwords, or `.env` contents. Never use token-printing
diagnostics when a safer command exists. Never commit secrets.

## Development and scope

Implement only the current work order. Record unrelated improvements as
backlog; do not implement them. If scope expands materially, stop and report.
Read `docs/PROJECT_STATE.md` before broad discovery; verified Git/Azure reality
overrides stale documentation.

## Testing and evidence

Use targeted tests during implementation and the smallest relevant integration
gate at acceptance. Run the full suite once only when genuinely required; do
not repeatedly rerun unrelated historical gates. Do not rebuild or republish
an unchanged image without proving build-input equivalence first. Keep normal
responses to 15 concise numbered items or fewer and do not dump large logs.

## Credit discipline

Treat compute as scarce: small work targets fewer than 50 credits, medium is
50–150, and large work requires approval. If work materially expands, report
`TASK_SPLIT_RECOMMENDED`.
