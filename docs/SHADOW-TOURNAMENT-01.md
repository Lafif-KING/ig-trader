# Shadow Tournament 01

## What it is

Shadow Tournament 01 (SHADOW01-V1) is a prospective observation system. It
records what several fixed market-opinion policies would have said at one
daily observation time, then records what happened after 1, 3, 5, 10, and 20
completed sessions.

It is not a trading system. It has no order, position, deal reference, broker
confirmation, paper-position, Demo-position, or Live-position model. Its
configuration fixes `execution_authority` to `OFF` and its broker adapter
rejects every endpoint outside its small read-only allowlist before transport.

## Safety boundary

- **SHADOW ONLY — ZERO ORDERS.** The Shadow package has no create, close,
  working-order, or execution-authority API.
- It uses `runtime/shadow_tournament.sqlite3`, never `trading.db` or cloud
  execution storage.
- It reads EPICs only from a supplied sanitized DQ-03 handoff. A bare
  schema-shaped registry or self-asserted EPIC is insufficient: Shadow
  requires the Phase-1 registry's Phase-2/3 augmentation provenance, matching
  `discovery_manifest.json` run context, `history_validation.json`, and each
  row's DQ-03 metadata and broker-history fingerprint links. A missing,
  ambiguous, mismatched, unsigned, or unverified fact becomes
  `MARKET_DATA_UNAVAILABLE`; it never substitutes another IG market. The
  current DQ-03 artifact format is not cryptographically signed, so this is a
  strict local consistency gate, not a claim that an arbitrary copied file has
  broker origin.
- It never creates a decision before the immutable tournament epoch.
- Opening the Control Center only reads local files and SQLite in read-only
  mode. It cannot start an observation worker by itself.
- Starting or stopping Shadow never starts the Demo robot. It never changes
  `PAPER_TRADING`, Demo authority, or Live authority.
- The explicit local Demo read-only adapter also requires the nonempty
  exact `IG_EXPECTED_DEMO_ACCOUNT_ID` setting. Before and after every
  allowlisted GET, including account, market, and history reads, it proves that
  the authenticated SessionManager current account ID exactly matches that
  setting. Missing, stale, or mismatched account state blocks the read or
  discards its response. Only a successful HTTP JSON-object response is passed
  to Shadow; non-success, non-JSON, or malformed responses fail closed. The
  internal allowlisted session-authentication POST is included once in the
  read-only request telemetry; execution counters remain zero.

## Frozen V1 scope

The scope is exactly these 20 canonical symbols:

| Asset class | Symbols |
| --- | --- |
| FX | EURUSD, GBPUSD, EURGBP, USDJPY, EURJPY, GBPJPY, AUDUSD, NZDUSD, USDCAD, USDCHF, EURCHF, EURAUD, GBPAUD, AUDJPY, CADJPY, CHFJPY |
| Metals | XAUUSD, XAGUSD |
| Indices | US500, USTECH100 |

The source file is `shadow01_strategy_config.json`. Its canonical JSON SHA-256
fingerprint is saved with every decision. Changing a material rule requires a
new configuration version, a new fingerprint, and a future epoch; it never
rewrites SHADOW01-V1 history.

## Data and decision clock

The planned V1 anchor is **17:10 America/New_York**, after the 17:00 New York
FX day boundary and US cash-index close. `shadow01.clock` validates this in a
DST-aware way.

Before the first epoch, the read-only provider probe must prove that a usable,
completed daily session is available at that time for FX, metals, and indices.
If it cannot, the result is
`SHADOW01_SESSION_CLOCK_HUMAN_GATE_REQUIRED`; no asset-class-specific clock is
invented.

The worker requests approximately 300 completed daily bars where the broker
permits it. It caches validated read-only data locally. A current/incomplete or
future bar is discarded before any feature calculation. Historical data is
only warm-up and calibration data, never a retrospective tournament decision.

The monitor uses the same DST-aware clock for scheduling and validation. A
small scheduler-jitter grace may still use the exact anchor; a later wake emits
`SHADOW01_MONITOR_ANCHOR_MISSED` and performs no retrospective provider read or
decision backfill. On restart, a fully committed same-anchor observation is
recognized from local SQLite evidence and reported as already recorded without
requesting market metadata again. If old or damaged storage contains only part
of a market's immutable evidence bundle, Shadow fails closed with
`SHADOW01_MARKET_OBSERVATION_PARTIAL_EVIDENCE`; it never guesses which rows to
rewrite.

DQ-03 registry evidence is supplied locally as its linked artifact set:
`instrument_registry.json`, `discovery_manifest.json`, and
`history_validation.json` under `artifacts/dq03/`. Shadow never searches or
copies another checkout at runtime. IG credentials are also never copied. If
they are not separately configured for this clean checkout, the only honest
state is `SHADOW01_IG_CREDENTIAL_SETUP_REQUIRED`.

### Gate-01 DQ-03 import record

The Gate-01 engineering remediation imported a reviewed, non-secret snapshot
from the prior DQ-03 evidence bundle at source revision
`b198ed83c37b0beeae9c49e9c261b0b1291c9786`
(`codex/dq03-instrument-resolver-data`). This is a recorded import, not a
runtime path dependency on that worktree. Source and local-copy SHA-256 values
were compared before use:

| Artifact | SHA-256 |
| --- | --- |
| `instrument_registry.json` | `4c5ff69d303503f4561c45c5992c9368d5c64cb1a16e47001b2f4b80d60765ad` |
| `discovery_manifest.json` | `24a8993ff0b2f8445a43de8a57da756cf02ddb73ea35642a3f6ab643ba62f635` |
| `history_validation.json` | `af4cba0bb6d339bdace94bff01b26e337e47a7b12b31ae15c813baea021bb438` |

Shadow01's canonical local registry fingerprint for that imported set is
`7238f930990e7ff10e02df7ab83f7df9ab3c7309e34c45a7891a7a61b6f0490b`.
The loader still requires the cross-linked Phase-1/2/3, DEMO, metadata, and
broker-history evidence for every one of the frozen 20 markets; no EPIC is
substituted when a link fails.

### Gate-02 read-only smoke surfaces

Gate 02 adds engineering interfaces only. They are deliberately non-persisting
and do not constitute a live IG smoke test, an epoch, or prospective evidence.
No Gate-02 validation authenticates to IG or constructs a real broker session.

- `ShadowReadOnlyStreamBridge` is an injected, registry-bound price-stream
  lifecycle boundary. It accepts only DQ-03-verified EURUSD, USDJPY, XAUUSD,
  and US500 EPICs; unknown, unverified, malformed, and non-representative
  EPICs fail locally. Its complete public lifecycle is connect, subscribe,
  receive, unsubscribe, disconnect, and a bounded reconnect/restore. It has
  no SessionManager, Lightstreamer, endpoint, token, settings, order,
  position, or working-order dependency. Known execution operation names and
  execution route strings fail locally before its injected transport is
  reachable.
- `Shadow01LocalDemoReadOnlyStreamFactory` supplies the separately lazy
  local-Demo transport that the bridge needs for a future explicitly approved
  smoke. Its `build()` operation does not load settings, construct a session,
  authenticate, or create a streaming client. Only a later explicit bridge
  `connect()` may do so, through the reviewed exact `POST /session` guard.
  The private transport pins the configured expected Demo account at that
  point, proves that identity before and after every stream lifecycle action,
  and best-effort disconnects and clears all local handles if proof fails. Its
  direct Lightstreamer adapter retains individual subscription handles and a
  bounded in-memory update queue so it can safely implement unsubscribe and
  receive-next-update without exposing credentials or a generic REST surface.
- `verify_shadow_session_clock()` is a no-wait, non-persisting assessment of
  the frozen 17:10 America/New_York clock. For EURUSD, XAUUSD, US500, and
  USTECH100 only, it uses bounded V4 identity/status metadata, completed DAY
  history, and an actual fresh canonical IG PRICE-stream quote for each
  representative. V4 remains the metadata authority and the PRICE stream is
  the canonical quote source. The normal smoke maximum is 27 REST requests
  (1 auth, 1 account, 20 V4 market, 4 history, 1 logout); it makes no V2 or V3
  market calls and zero REST live-price polls. A missing, stale, invalid, or
  disconnected canonical stream quote fails closed as `NO_DECISION`; it is
  never replaced with a later quote or retrospective history backfill.
- Declared broker hours are optional advisory evidence only. The Gate-12 V3
  and Gate-13 V2 live field-type proofs both observed
  `instrument.openingHours` present with a `null` value for the representative
  Demo product. A V4 response with missing or `null` declared hours is
  recorded as `DECLARED_HOURS_NOT_PROVIDED`, and an unusable declared-hours
  shape as `DECLARED_HOURS_ADVISORY_UNUSABLE`; neither changes a valid clock
  result to a failure. No declared-hours value is fabricated, and the frozen
  17:10 America/New_York anchor remains DST-aware.
- Gate-12 has a separate, explicitly authorized V3 schedule-contract probe.
  It uses a one-attempt Demo transport and makes only `POST /session`, one
  EURUSD V3 `GET /markets/{epic}`, and `DELETE /session`; an XAUUSD V3 read is
  allowed only when the caller explicitly requests a comparison after a 200
  EURUSD response whose `openingHours` field is absent. Its output contains
  only HTTP status, the actual HTTP-client-dispatched `VERSION`, allowlisted
  key names plus unknown-key counts, field presence, JSON types, and
  `marketTimes` count. It never prints a body, time, price, account identifier,
  token, or other source value. The transport rejects a second authentication,
  retry of a route, or any dispatch beyond the small envelope before it reaches
  IG. This is the required wire proof; the older parsed
  `schedule_source_version=3` label is not wire proof because the parser
  assigns that source version itself.
- Gate-12 fails closed if the observed V3 shape has no declared-hours contract.
  It does not fabricate hours or perform a V2 live request. A V2 comparison is
  limited to code, tests, and documentation until a human explicitly authorizes
  any separate, bounded V2 live probe. A V3 response that has declared hours
  instead isolates a request-version or parsing defect for a narrowly scoped
  fix.
- The offline V2 comparison records IG Labs' documented `GET /markets/{epic}`
  V2 shape only: `instrument.openingHours` is an object containing a
  `marketTimes` array with string `openTime` and `closeTime` fields. It is not
  evidence that an individual Demo product supplies those values. Shadow01
  retains no V2 request path, so the documented contract can never turn into a
  live V2 request without a separate human gate.
- `BoundedFinalClockSmokeV12` is a separate finalization path. It requires a
  sanitized, explicitly labelled operator-attested prior V11 evidence bundle
  for the already proven 20 V4 identity/status reads, four completed DAY
  histories, 20 valid PRICE quotes, and four reconnect checks; those facts are
  not independently or freshly revalidated in V12. Its fresh envelope is
  exactly at most seven one-attempt REST calls: auth, account, four historical
  V3 schedule-contract reads, and logout. It is retained as historical
  contract evidence and is not part of the normal Gate-14 smoke path.
  It makes zero fresh V4, history, REST live-price, stream, persistence, epoch,
  decision, outcome, execution, Live, or Azure calls. The normal Gate-14
  smoke budget is 27 REST requests and has no schedule-call category.
- `run_shadow_warmup_diagnostic()` is a bounded, read-only historical
  readiness check for EURUSD, XAUUSD, and US500. It requests at most three
  300-point daily histories and reports requested/received bars, completed
  sessions, T1/Q1 readiness (61 sessions), M1 full-calibration readiness (273
  sessions), and a diagnostic quality state. It creates no historical Shadow
  decisions.
- `run_shadow_dry_snapshot()` evaluates T1, M1, X1, F1, Q1, C1, and P0--P3
  from a caller-supplied, completed, fingerprinted information set. It never
  acquires broker data itself and requires immutable before/after broker
  counters to match. Every result and per-policy record carries the single
  supplied diagnostic timestamp and is labelled `DRY_RUN_NON_PROSPECTIVE`.
  It imports no runtime, storage, outcome, or decision-materialization code.

The existing Demo worker's streaming implementation is intentionally not
wired directly into Shadow01 because it lacks reviewed unsubscribe and
receive-next-update lifecycle semantics. The local factory instead reuses the
project's safe SessionManager REST guard and system-TLS approach while keeping
its narrower stream lifecycle inside Shadow01. Gate 02 never invokes the
factory or bridge connection, so it remains free of authentication and IG
calls.

### Gate-14 final clock policy

The fixed anchor remains **17:10 America/New_York**, with DST-aware conversion.
The bounded Gate-12 V3 and Gate-13 V2 field-type probes each observed a
successful response with `instrument.openingHours` present and `null`; that
is recorded as `BROKER_DECLARED_HOURS_UNAVAILABLE` evidence, not a claimed
trading schedule. Consequently, declared hours and a declared schedule-window
are advisory only in the final clock verdict. This does not relax identity,
V4 tradeability status, completed-session, timestamp-parseability,
incomplete-row, DST, deterministic-key, or canonical-quote checks.

Normal smoke performs no V1, V2, or V3 market call and no new field-type
discovery. Its authorized V4 metadata reads remain bounded to the frozen
universe; no V4 response is used as a price source. Each representative needs
an actual fresh `IG_PRICE_STREAM` quote with a valid canonical bid, ask, and
timestamp. If that quote is absent, stale, invalid, or disconnected, the
result is `NO_DECISION`: Shadow performs no later re-decision, history
backfill, or retrospective observation. The normal REST budget is 27 requests
with no schedule-call category. Execution authority remains `OFF`; this policy
does not create an epoch, decision, outcome, order, or execution capability.

### Final live read-only evidence record

The separately authorized final live read-only smoke passed without creating
prospective data. It proved the expected Demo account, 20/20 verified DQ-03
markets with no substitution, 20/20 fresh valid canonical PRICE-stream quotes,
and bounded reconnect/cleanup for EURUSD, USDJPY, XAUUSD, and US500. Each
representative DAY/300 history had 299 completed sessions, meeting T1, M1, and
Q1 readiness; the DST-aware clock passed for FX, METAL, US500, and USTECH100.

The evidence used the bounded 27-request REST envelope: one authentication,
one account read, 20 V4 metadata reads, four historical reads, zero REST
live-price reads, and one logout. The before/after Shadow database state had
no epoch, decisions, or outcomes. Execution, Demo start, Live, and Azure
counters were zero, and execution authority stayed `OFF`. The deterministic
freeze record is `docs/SHADOW01-V1-FREEZE-MANIFEST.json`.

### Gate-07 canonical live quote source

For Shadow01-V1, REST `GET /markets/{epic}` V4 is metadata-only: it proves the
EPIC, market status, dealing rules, instrument metadata, and whether streaming
prices are available. Its `snapshot.priceLadder` is retained only as V4
metadata evidence and is permanently `UNORDERED_FOR_SHADOW_V1`; the published
REST contract does not prove that any array index is top-of-book. Shadow01
therefore never chooses a REST ladder tier and never falls back to V3 direct
snapshot prices.

The sole canonical live-quote source is the IG Price subscription
`PRICE:{account identifier}:{epic}`. `BIDPRICE1`, `ASKPRICE1`, and the UTC
millisecond `TIMESTAMP` form one `ShadowLiveQuote` only after finite-positive
price validation, deterministic timestamp plausibility/future-skew/freshness
checks, and a verified DQ-03 EPIC match. Invalid or stale updates retain only
a stable reason code, not a partial price or timestamp. The official streaming
reference identifies Tier-1 bid and ask fields as the ladder top tier.

The bridge accepts all 20 verified, stream-capable DQ-03 EPICs at initial
subscription. This stays within IG's documented default allowance of up to 40
concurrent price subscriptions. Bounded reconnect tests use EURUSD, USDJPY,
XAUUSD, and US500; they do not infer any REST ladder ordering. C1 receives the
spread from the same accepted stream Tier-1 quote. REST live-price polling is
zero, and dry snapshots remain in-memory `DRY_RUN_NON_PROSPECTIVE` diagnostics
with execution authority `OFF`.

### Gate-08 stream-session lifecycle and history-completion contract

Gate 08 is engineering-only. It does not authenticate to IG, create an epoch,
write a decision or outcome, alter credentials or frozen configuration, or
enable execution authority.

The authenticated read-only REST adapter is the sole owner of account identity,
endpoint, CST/XST token lifetime, and the final REST logout. After the account
read has been proven, it creates immutable `ShadowStreamSessionMaterial` for
the stream. That material has no serialisation method and suppresses all four
values in its representation. The Lightstreamer transport owns only stream
connect, subscribe, unsubscribe, and disconnect; it cannot authenticate or
log out the parent REST session. The expected normal smoke envelope is exactly
one REST authentication and one REST logout, with no stream REST re-login.

The only subscription shape is `PRICE:{account}:{epic}` in `MERGE` mode using
the `Pricing` data adapter and exactly `BIDPRICE1`, `ASKPRICE1`, and
`TIMESTAMP`. Registration is installed before subscription so a synchronous
initial MERGE image cannot be lost. Per-subscription reports retain only the
symbol, field names, boolean contract checks, registration/active status,
callback count, and valid-quote count. They never include endpoint, account,
token, price, raw timestamp, exception body, or subscription item text.

The initial bounded wait accepts one initial update for all 20 verified
markets. It reports no update separately from an invalid canonical update and
does not busy-poll. Only after that image is complete does the smoke perform
one stream-only representative reconnect for EURUSD, USDJPY, XAUUSD, and
US500. It first removes the 20 initial subscriptions; no all-market reconnect
is permitted.

The installed Lightstreamer Python client has an asynchronous WebSocket
dispose callback defect. The adapter therefore forces its documented
`HTTP-STREAMING` transport before connect, avoiding that WebSocket cleanup
path without patching third-party package files.

Historical `GET /prices/{epic}/DAY/300` V2 parsing accepts only
`snapshotTimeUTC` as UTC ISO-8601 (including the documented offset-free UTC
form) and bid/offer-or-ask OHLC blocks. It records a sanitized row-shape
diagnostic only. A 300-row response with the final current-day midnight candle
produces 299 completed sessions, which remains sufficient for T1, M1, and Q1;
the current candle is never used by the clock, warm-up, or dry snapshot.

### Gate-09 live field-type proof and history-row contract

Gate 09 used exactly one bounded IG Demo, read-only contract probe. It made one
authenticated session, one EURUSD `DAY/5` historical read, and one Price
subscription before bounded unsubscribe, stream disconnect, and the single
REST logout. It made no account or market-metadata reads, no order or
execution calls, and did not create a database, epoch, decision, or outcome.

The value-safe evidence established these actual representations without
recording source values: history rows carry `snapshotTime` as the exact IG
slash datetime format `YYYY/MM/DD HH:MM:SS`; OHLC blocks contain float bid/ask
fields with a nullable `lastTraded`; and `lastTradedVolume` is an integer.
The subscribed Price callback resolved its expected item and reported an
initial snapshot with `BIDPRICE1` and `ASKPRICE1` numeric strings plus a
13-digit ASCII millisecond `TIMESTAMP` string. Callback diagnostics retain
only field presence, runtime type, parsing booleans, length/plausibility,
snapshot state, changed field names, and classified rejection counts.

The history parser now accepts only the already-reviewed `snapshotTimeUTC`
ISO-8601 forms and the Gate-09-proven `snapshotTime` slash form, treating that
specified DAY representation as UTC. The stream parser now accepts only an
ASCII digit-string millisecond timestamp in addition to its earlier numeric
object forms. No broader local-time, timestamp, or price fallback was added.
The completed-bar rule remains unchanged: it excludes only a current-day
midnight DAY candle, not older completed sessions.

Gate 09 is engineering complete and ready for the separately authorized
all-20 `SHADOW01` Stream Smoke V9. The stream smoke must still prove its own
all-market stream behavior; Gate 09 neither reruns Gate 02 nor creates a
tournament epoch.

## Engines

All calculations use completed observations only.

The following exact V1 formula text is part of the fingerprinted configuration:

```text
normalized_n = return_n / max(population_realized_volatility_n, epsilon);
direction is LONG only when normalized_20 and normalized_60 are both positive,
SHORT only when both are negative, otherwise FLAT;
strength = min(trend_strength_cap, (abs(normalized_20) + abs(normalized_60)) / 2).

percentile = (count(prior_values < current_value) +
0.5 * count(prior_values == current_value)) / count(prior_values);
prior_values are the latest valid normalized 5-session returns and exclude current_value.
```

- **T1 — multi-speed trend:** computes 1/5/20/60-session returns, ATR20/price,
  20-session realized volatility, trend values, 60-session mean distance, and
  drawdown. Its score is the capped mean of absolute 20- and 60-session
  normalized returns. It is long only when both are positive, short only when
  both are negative, and flat when they disagree.
- **M1 — stretch/reversion:** normalizes the completed 5-session return by
  trailing volatility and ranks it against the *prior* 252 valid values. A
  lower-tail stretch is a long reversion opinion; an upper-tail stretch is a
  short reversion opinion. It is not an order signal.
- **X1 — cross-asset/rates:** records only documented causal context input.
  It can use relevant rates, real yields, VIX, oil, metals, and risk inputs
  supplied by an approved read-only source. Missing context is `UNKNOWN`; it
  never fabricates an economic result.
- **F1 — fundamental context:** records policy state/trend, staleness, data
  quality, and reliable event risk. It never creates a long/short direction.
  Without an available causal provider it is `UNKNOWN`; a missing FRED key is
  recorded as `FRED_CONTEXT_UNAVAILABLE`.
- **Q1 — data quality:** reports `NORMAL`, `WARNING`, `BLOCKED`, or `UNKNOWN`
  from freshness, history, provider, session, and feature facts. `BLOCKED`
  prevents all candidate decisions.
- **C1 — cost context:** records raw spread, stop metadata, product type, and
  funding facts. It reports `COST_UNKNOWN` when an honest cross-asset
  normalization is unavailable, rather than making a global cost claim.

## Tournament policies

Each policy receives exactly the same timestamp and information fingerprint.
None can trade.

| Policy | Rule |
| --- | --- |
| P0_TECHNICAL_TREND_ONLY | Uses T1 only. |
| P1_TECHNICAL_REVERSION_ONLY | Uses M1 only. |
| P2_TREND_PLUS_CROSS_ASSET | Uses T1 only when X1 is supportive or neutral; it blocks an opposing or unknown X1 result. |
| P3_CONSERVATIVE_CONTEXT | Starts from T1 and blocks for Q1 `BLOCKED`, known C1 `COST_HIGH`, X1 opposition, or a reliable F1 event-risk block. |

Every directional opinion receives deterministic factor tags. For example,
EURUSD short contains `EUR_SHORT` and `USD_LONG`; USDJPY long contains
`USD_LONG` and `JPY_SHORT`. Reports count both decisions and unique factor
bets so correlated USD or JPY expressions are visible.

## Storage and outcomes

The local SQLite tables are append-only:

- `tournament_runs`
- `market_snapshots`
- `engine_insights`
- `shadow_decisions`
- `outcome_labels`
- `provider_health`
- `epoch_readiness`

The epoch can transition from absent to present once, after a separate exact
human authorization. Decisions must be at or after that time. SQLite triggers
reject update/delete attempts for snapshots, insights, decisions, and outcomes.

Each market observation's snapshot, seven engine insights, and four policy
decisions are committed in one SQLite transaction. A stop or crash before the
commit leaves none of that bundle behind, so a later restart does not become
stuck on a unique snapshot row. A committed bundle is append-only and can only
be acknowledged as already recorded; it is never recalculated or overwritten.

Later outcome labels are stored separately from decision features. On later
anchored observation cycles, the observer uses only newly completed daily bars
and the immutable decision snapshot to resolve due 1/3/5/10/20-session labels.
A horizon that is not due yet is left unlabelled; it is never written as a
temporary `BLOCKED` row because the database is append-only. The causal feature
builder has no outcome-storage import. Cost-adjusted outcomes stay empty unless
a separately reviewed cost model exists.

Leaderboard labels are evidence labels only:

- Fewer than 30 resolved observations: `INSUFFICIENT_EVIDENCE`
- 30–99: `EARLY_EVIDENCE`
- 100 or more: `EVALUABLE`

None of these labels qualifies a strategy for Demo or Live trading.

## Control Center and local use

The **SHADOW TOURNAMENT** page explains the state in plain language: the
version, epoch, data health, 20-market matrix, current opinions, decisions,
outcomes, leaderboard, and factor audit. It has no trade-execution action.

In **Windows Command Prompt**, double-click or run
`tools\launch_shadow_tournament.cmd`. It opens the local Control Center only;
it does not start a worker or contact IG. On the Shadow Tournament page, the
separate **START SHADOW MONITOR** button remains disabled until a human has
created an epoch after engineering review. A start click is the only dashboard
path that asks the local CLI to construct its Demo-only, read-only adapter; the
CLI first requires a complete linked DQ-03 registry, Demo endpoint, local
operator setting, expected Demo account ID, and separately configured
credentials. **STOP SHADOW
MONITOR** only asks the Shadow observer to stop; it cannot affect the Demo
robot.

The CLI also has an explicit `probe --use-local-demo-read-only` path for a
bounded pre-epoch provider check. `status` and `stop` do not construct a broker
client or authenticate. There is intentionally no CLI command that creates an
epoch.

Do not run an order, position, working-order, Demo robot, or Azure command as
part of Shadow Tournament setup. Stop and obtain a separate authorization if a
step proposes any of those actions.

## First-run human gate

After Gate-01 engineering validation, the only ready classification is
`SHADOW01_ENGINEERING_READY_FOR_READONLY_SMOKE`. A bounded read-only smoke is
separate and is reported as `NOT_RUN`, `BLOCKED`, or `PASS`; validation alone
does not start it. Gate-01 must not create an epoch. The operator reviews the
configuration, provider evidence, and first read-only snapshot before giving
the separate authorization `START SHADOW01-V1 EPOCH`.

## Future MetaDecision work

SHADOW01-V1 deliberately trains no model. Clean prospective decisions,
context, quality, costs, factor tags, and separate outcome labels may later be
reviewed as a training dataset. Any XGBoost, LightGBM, neural-network, or RL
work requires a new work order and cannot promote execution authority.
