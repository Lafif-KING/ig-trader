# G3A-02 Targeted Gap Recovery and Merge Hygiene

Status: `PARTIAL_DATA_WITH_VALID_GAP_POLICY`

Order authority: none

## Authoritative recovery result

The fresh IG Demo query for `CS.D.EURGBP.MINI.IP` requested native 1-minute
prices from `2026-08-14T19:02:00Z` through `19:04:00Z`. It returned only the
native `19:02` and `19:04` candles. It did not return `19:03`; no G3A-01 cache
was consulted.

Because the target was absent, the same Demo session queried the official
historical-price endpoint at `SECOND` resolution for `19:02:00Z` through
`19:04:59Z`. IG accepted the resolution with HTTP 200 and returned 28
authoritative second-bars:

- 12 bars in 19:02;
- zero bars in 19:03;
- 16 bars in 19:04.

Aggregation uses only returned broker second-bars. Open is the first returned
second open, high is the maximum returned second high, low is the minimum
returned second low, and close is the last returned second close. No absent
second is synthesized or carried forward. At the broker-reported five-decimal
precision, all eight bid/offer OHLC fields reconstructed for both adjacent
minutes exactly match their native 1-minute candles. The target minute has no
second-bar component, so it cannot be reconstructed.

The `19:03` gap therefore remains authoritative. No interpolation, midpoint,
carry-forward, cross-instrument borrowing, or alternate-provider data is used.

## GAP_AWARE_REPLAY_V1

When a required authoritative interval is absent:

1. Signal evaluation affected by the gap is `NO_TRADE`.
2. Indicator state crossing the discontinuity is invalidated.
3. Required warm-up is rebuilt solely from subsequent authoritative candles.
4. Trading cannot resume until every required timeframe and indicator is valid
   after full warm-up.
5. The gap event and the blocked interval are recorded in replay evidence.

The machine-readable contract is
`schemas/gap-aware-replay-policy-v1.json`. This is a replay qualification
policy only; G3A-02 does not run the Scalper, initialize execution, or authorize
an order.

## External immutable data package

Bulk raw responses, normalized datasets, and superseded audit captures are not
part of the merge candidate. They are preserved at:

```text
C:\Users\AfifB\projects\ig-trader-artifacts\g3a-02-20260815
```

The package contains 125 files and 15,583,812 bytes. Its content fingerprint is
`61442f9cf91260ed32098767a206ef45e9d45ea3dc17c93572cba3111eff3780`.
The manifest SHA-256 is
`c6469e45f743204dafd05361e0522c965398529466015568dad9fabd8a98b6d4`.
Payload files and the manifest are read-only, and G3B must run the verifier
before consuming them:

```powershell
poetry run python -m tools.g3a_artifact_package verify --package-root C:\Users\AfifB\projects\ig-trader-artifacts\g3a-02-20260815
```

Any changed, missing, or additional payload file fails verification. The Git
tree retains only compact final manifests, evidence summaries, hashes, schemas,
source, tests, and documentation. `.runtime/` is ignored and remains available
locally for audit.

## Safety and limits

One session made four market-data GETs: search, market detail, native minute,
and SECOND prices. Login and logout were the only write-method requests. Order,
position, and working-order endpoint counts were zero. The execution adapter
and optimization were not started. Remaining historical allowance was 2,942
points.
