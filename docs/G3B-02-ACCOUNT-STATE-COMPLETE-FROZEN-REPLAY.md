# G3B-02 Account-State-Complete Frozen Replay

Status: `PASS_REPLAY_INTEGRITY`; recommendation `PASS_FOR_G3B_MERGE`;
performance evidence `NEGATIVE_ON_AVAILABLE_SAMPLE`; human review required

Execution mode: offline replay through the accepted local G2 PaperBroker only

IG REST, Lightstreamer, credential, and broker-order authority: none

Optimization authority: none

## Dependency analysis

G3B-01 deliberately returned `None` from its replay-only account adapter because
the accepted G3A package contains market data, not an account snapshot. That
caused the existing G2 `PortfolioRisk` to return `ACCOUNT_STATE_UNKNOWN` for
eight selected GBPUSD candidates. The strategy, package, gap policy, and risk
logic were not responsible for the unknown state.

The accepted G2 commit
`4bc3de5b03eedcfcbb71d3f7042047533c4ca75b` already provides the required
qualification architecture:

| Risk prerequisite | Accepted source | G3B-02 treatment |
|---|---|---|
| Balance, starting balance, currency | G2 deterministic fixture | Reused exactly |
| State-known flag and timestamp | `PaperBroker.account_snapshot` / `AccountPort` | Queried at each decision |
| Daily loss | `AccountSnapshot.daily_loss_pct` | Derived by the accepted G2 domain object |
| Open positions | G2 PaperBroker SQLite state | Updated by accepted local fills/exits |
| Absolute portfolio vetoes and sizing | `PortfolioRisk.evaluate` | Called unchanged and authoritatively |
| GBPUSD account-currency pip value | G2 fixture, exact GBPUSD EPIC | Reused exactly |
| Stop and minimum size | Accepted G3A instrument rules | Remain authoritative |

The accepted G2 fixture SHA-256 is
`ee1a15853e77e2a9aece0a88a623ea378bf976d45d00a44610ae4e53a1d6ac2d`.
Its qualification account-state hash is
`3f180e98de682edae72ea2f96d61b349ba8d0309a6ec9a1bd6a426e510863b84`.
Loading any different fixture is classified
`QUALIFICATION_ACCOUNT_STATE_GAP` and stops before PaperBroker creation.

The G2 EURUSD fixture uses `CS.D.EURUSD.MINI.IP`, while accepted G3A uses
`CS.D.EURUSD.CEFM.IP`. G3B-02 does not transfer or infer that pip value. All
three EURUSD candidates fail the unchanged spread/target gate before
`PortfolioRisk`; a future EURUSD candidate reaching sizing would fail closed.
G2 stop distances are never substituted for G3A market rules.

## Reproduction command

Run this in Windows PowerShell from the dedicated G3B-02 worktree:

```powershell
poetry run python -m src.ig_trader.g3b_replay --mode OFFLINE_REPLAY --package-root C:\Users\AfifB\projects\ig-trader-artifacts\g3a-02-20260815 --qualification-fixture .\fixtures\g2-offline-paper-market.json --state-root .\.runtime\state\g3b-account-state-replay --evidence-json .\.runtime\evidence\g3b-account-state-replay.json --evidence-markdown .\.runtime\evidence\g3b-account-state-replay.md
```

Evidence and state paths are create-only. Use new paths for an independent
repeat. The CLI creates separate fresh PaperBroker databases for its two
internal determinism runs and requires canonical byte equivalence.

## Exact candidate dispositions

The accepted G3A data, 1,917 decisions, 1,624 valid decisions, 293 invalid
decisions, one authoritative gap, 292 warm-up invalidations, 13 BUY signals,
seven SELL signals, and 20 candidates are unchanged from G3B-01.

| Disposition | Count |
|---|---:|
| `STRATEGY_NO_SIGNAL` | 1,604 |
| `SPREAD_REJECTION` | 10 |
| `RISK_REJECTION_ACCOUNT_STATE` | 0 |
| `RISK_REJECTION_OTHER` (`TOTAL_POSITION_LIMIT`) | 4 |
| `CYCLE_SUPPRESSED` | 2 |
| `TRADEINTENT_ACCEPTED` | 4 |

The JSON and Markdown evidence contain an audit row for every original
candidate: EPIC, decision and signal timestamps, side, confidence, spread,
stop, target, spread/target ratio, account snapshot result, exact
`PortfolioRisk` result, final disposition, and intent ID where accepted.

The four position-limit vetoes occur while an earlier accepted G2 PaperBroker
position is open. This is evidence that positions are read from authoritative
PaperBroker state rather than bypassed with a static account object.

## Limited performance result

Four GBPUSD TradeIntents receive accepted local PaperBroker fills and close on
the accepted future bid/offer candles. All four reach their stop. The combined
result is approximately `-16.0` spread-adjusted pips, `-4.0R`, and `-59.8 EUR`
on the deterministic paper account. Maximum drawdown is approximately 16 pips,
maximum consecutive losses are four, average holding time is 540 seconds, and
no position remains open at dataset end.

This is `NEGATIVE_ON_AVAILABLE_SAMPLE`. Four trades from a limited historical
sample cannot qualify or reject the strategy for Demo or Live execution by
themselves. The result requires human review and grants no optimization,
parameter-change, Demo-order, or Live-order authority.

## Safety boundary

The existing offline launcher still installs irreversible socket, process,
IG-import, Lightstreamer-import, credential-import, and order-import guards
before replay dependencies load. Local `PaperBroker.submit` is not an IG order
endpoint. Evidence must show network, IG REST, Lightstreamer, order-endpoint,
and credential-resolution counters at zero.

The active broker-facing bot, `PAPER_TRADING`, `.env`, strategy parameters,
accepted package, gap policy, and G2 risk implementation are unchanged. No
merge and no Demo execution are authorized by this work order.
