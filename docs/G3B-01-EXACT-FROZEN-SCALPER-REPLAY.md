# G3B-01 Exact Frozen Scalper Replay and Strategy Qualification

Status: `PASS_REPLAY_INTEGRITY`; performance evidence `NO_TRADES`; human review required

Execution mode: offline replay only

Network authority: none

Broker order authority: none

Optimization authority: none

## Reproduction command

Run this in Windows PowerShell from the dedicated G3B worktree:

```powershell
poetry run python -m src.ig_trader.g3b_replay --mode OFFLINE_REPLAY --package-root C:\Users\AfifB\projects\ig-trader-artifacts\g3a-02-20260815 --evidence-json .runtime\evidence\g3b-exact-replay.json --evidence-markdown .runtime\evidence\g3b-exact-replay.md
```

The command verifies the complete external package before importing any candle
into replay. It rejects a changed manifest, missing or additional payload file,
changed payload hash or size, changed normalized hash, wrong final dataset
fingerprint, wrong EPIC/resolution inventory, writable payload, or any minute-gap
inventory other than the accepted EURGBP 1M gap.

Evidence paths are create-only. Use new output paths for an independent repeat;
the replay run fingerprint and value content must match.

## Frozen strategy and point-in-time rule

The engine constructs `FrozenV1Config` and the existing `ScalperStrategy`. It
does not expose a tuning argument. RSI 7, confidence 0.70, ADX 20, warm-up 60,
ATR x2 stop, 1.5 reward:risk, maximum 12-pip stop, 1.2-pip spread, 0.15
spread/target ratio, one total position, one per instrument, and one execution
per cycle remain exact.

Canonical timestamps are inclusive candle starts. A 1M decision happens only
at that candle's close. A candle is visible only when its start plus its exact
duration is less than or equal to the decision time. Every valid decision
requires 60 already-closed authoritative candles for 1M, 5M, 15M, and 1H.

The existing Scalper itself defines a 1M calculation and no multi-timeframe
voting formula. G3B therefore runs the existing Scalper on the last 60 closed
1M candles and treats the other three timeframes as point-in-time readiness
gates. It does not invent a new strategy rule.

## Gap handling

At `2026-08-14T19:03:00Z`, EURGBP 1M has no candle. G3B records
`AUTHORITATIVE_GAP`, forces `NO_TRADE`, invalidates the dependent state, and
allows only candles starting at or after `19:04Z` into rebuilt state. Trading
cannot resume until all four timeframes again contain 60 subsequent closed
authoritative candles. The sample ends before EURGBP can rebuild its 1H state.

The gap directly invalidates one missing decision and keeps 115 later EURGBP
decisions in post-gap warm-up, for 116 prevented decisions. A hypothetical
trade count is not calculated because doing so would require inventing the
missing candle or carrying discontinuous state.

## Exact replay behavior on the accepted sample

| Instrument | Decisions | Valid | Invalid | BUY | SELL | Candidates | Selected rejections | Trades |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| EURGBP | 639 | 464 | 175 | 0 | 0 | 0 | 0 | 0 |
| EURUSD | 639 | 580 | 59 | 0 | 3 | 3 | 3 | 0 |
| GBPUSD | 639 | 580 | 59 | 13 | 4 | 17 | 15 | 0 |
| Overall | 1,917 | 1,624 | 293 | 13 | 7 | 20 | 18 | 0 |

Two GBPUSD candidates occurred in cycles that had a higher-ranked candidate,
so the one-execution-per-cycle rule suppressed them. Of the 18 selected
candidates, 10 failed the exact spread/target ratio and eight reached G2
`PortfolioRisk`, which returned `ACCOUNT_STATE_UNKNOWN`.

The accepted G3A package contains market metadata but no authoritative account
balance, account-currency pip conversion, open-position snapshot, or daily-risk
snapshot. G3B records pip size, value-of-one-pip currency, minimum size, and
minimum stop from broker evidence, but does not convert the pip value or invent
an account. Unknown account and daily-risk state therefore remain fail-closed.

No TradeIntent was accepted and no paper trade was executed. Wins, losses,
P&L, expectancy, profit factor, drawdown, holding duration, and intrabar counts
are consequently zero or not applicable. The honest performance-evidence
classification is `NO_TRADES`, not a profitability conclusion.

## Execution semantics

Focused tests prove long entry at offer and exit at bid, short entry at bid and
exit at offer, exact stop/target calculation, stop and spread gates, portfolio
limits, and deterministic stop-first treatment of `AMBIGUOUS_INTRABAR`. Spread
is embedded through executable sides. Commission, financing, slippage,
liquidity, latency, and other fees are `NOT_MODELLED_NOT_ESTABLISHED`.

## Safety boundary

The launcher installs the accepted irreversible offline isolation before
loading pandas, the strategy, the replay engine, or package data. Socket, child
process, IG REST, Lightstreamer, credential configuration, market-data client,
and order-adapter imports are prohibited. The evidence must show all five
authority counters at zero.

G3B does not change the active single-instrument bot, authorize Demo execution,
enable Live mode, or begin optimization. Final strategy disposition remains
`HUMAN_REVIEW_REQUIRED`.
