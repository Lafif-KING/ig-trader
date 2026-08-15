# G3A-01 Authoritative Market Data Pipeline

Status: accepted as `PARTIAL_DATA`; retained as the G3A-02 input baseline

Order authority: none

## Scope

`tools.g3a_market_data` acquires the frozen research universe only:

| Instrument | EPIC candidate |
|---|---|
| EUR/GBP Mini | `CS.D.EURGBP.MINI.IP` |
| EUR/USD Mini | `CS.D.EURUSD.CEFM.IP` |
| GBP/USD Mini | `CS.D.GBPUSD.MINI.IP` |

The candidates are not trusted by configuration alone. Every run first requires
an exact instrument-name and EPIC match from both `GET /markets?searchTerm=...`
and `GET /markets/{epic}`. A mismatch stops before any historical-price call.

This three-instrument research scope does not add multi-instrument execution to
the active bot. The module does not import or initialize `main`, the execution
adapter, the database, or a strategy runner. It does not run optimization.

## Read-only broker boundary

The transport allow-list contains only:

- `POST /session`, version 2, for Demo authentication;
- `GET /session`, version 1, for the active-account timezone offset;
- `GET /markets?searchTerm=...`, version 1;
- `GET /markets/{epic}`, version 3;
- `GET /prices/{epic}`, version 3;
- `DELETE /session`, version 1, for cleanup.

Every other path, API version, or parameter set is rejected before the HTTP
client is called. Tests cover all position and working-order create, update, and
delete forms. The evidence field `order_endpoint_call_count` is zero.

The implementation follows the official [IG REST API reference](https://labs.ig.com/rest-trading-api-reference.html),
[historical-data and REST limits](https://labs.ig.com/faq.html), and
[session timezone-offset contract](https://labs.ig.com/reference/session.html).
The documented weekly historical allowance is 10,000 points. The approved run
requested at most 8,400 intervals, used one session, used no parallel
connections, and paced requests by 2.1 seconds.

## Immutable artifact layout

The default runtime layout is:

```text
.runtime/g3a/data/
  raw/{run-id}/
  normalized/{run-id}/
  manifests/{run-id}/
.runtime/evidence/
  g3a-data-quality.json
  g3a-data-quality.md
```

Successful market-data response bodies are stored byte-for-byte under `raw`.
They are create-only: an existing different file is an immutable-output
conflict. A matching file is a cache hit, allowing a failed run to resume
without redownloading completed requests. Failed HTTP responses are retained in
a separate `raw/{run-id}/errors` path when their bodies pass the same safety
scan.

Session responses are not persisted because they can contain account identity
or authentication material. The required timezone offset is projected into
sanitized evidence only. Raw market-data bodies are rejected rather than
written if a credential, token, account, or deal-identity field is present.

Normalized JSONL and manifests are also create-only. Every raw source file has
a SHA-256 hash; every normalized series has a byte-level SHA-256 hash; every
series manifest and the run manifest have deterministic fingerprints. Missing
candles are never generated, copied from another instrument, interpolated, or
presented as broker evidence.

Qualification can be repeated without contacting IG by combining
`--offline-cache-only`, the immutable raw run ID, and the exact acquisition
evidence file. Offline mode validates that the source evidence recorded Demo,
zero order calls, no execution adapter, a passing secret scan, and three
verified EPICs before reading the cached payloads. Its evidence records both
the original acquisition counters and zero current-run network calls.

## Canonical candle and time rules

Each normalized row contains:

- `epic`, `resolution`, and `timestamp_utc`;
- bid open/high/low/close;
- offer open/high/low/close;
- last-traded open/high/low/close when supplied;
- volume when supplied;
- the original `snapshotTime` and `snapshotTimeUTC` text;
- raw source file, page, and item index.

`snapshotTimeUTC` is the only timestamp authority. A missing or invalid value is
a timezone gap and the candle is rejected. `snapshotTime` is evidence only and
is never used to infer UTC, so repeated or skipped local times at a DST boundary
cannot alter canonical time. Canonical timestamps are serialized as explicit
UTC ISO-8601 instants. The timestamp is the inclusive candle start; the
requested end is exclusive.

The active-account fixed offset is used only to construct IG V3 `from` and `to`
parameters. The broker-provided `snapshotTimeUTC` is still authoritative for
every returned candle.

## Data quality and gap classification

Every EPIC/resolution manifest records requested and actual ranges, candle and
meaningful expected counts, missing intervals, duplicates, non-monotonic input,
invalid OHLC, crossed or zero/negative spread, stale sequences, large gaps,
timezone ambiguity, source metadata, source hashes, normalized hash, and the
qualification result.

Gap classifications are deliberately evidence-bounded:

- `EXPECTED_WEEKEND_CLOSURE`: wholly inside the conservative Friday 22:00 UTC
  through Sunday 21:00 UTC rule;
- `EXPECTED_MARKET_SESSION_GAP`: the same boundary is absent for all three
  instruments at the same resolution;
- `BROKER_MAINTENANCE`: used only with an explicit broker maintenance marker;
- `ACTUAL_MISSING_DATA`: not explained by weekend closure or three-instrument
  session consensus;
- `API_ALLOWANCE_LIMITATION`: only IG's documented historical-allowance error.

Price absence alone is never called broker maintenance. Expected closure and
session gaps remain absent in canonical files; they are not filled.

## Reproduction command

Place: Windows PowerShell, in the isolated G3A worktree.

The dotenv path below is a read-only credential source. It is not copied into
the isolated worktree, evidence, manifests, or Git.

```powershell
poetry run python -m tools.g3a_market_data --environment demo --run-id g3a-01-20260815 --end-utc 2026-08-14T22:00:00Z --intervals-per-series 700 --data-root .runtime/g3a/data --evidence-json .runtime/evidence/g3a-data-quality.json --dotenv C:\Users\AfifB\projects\ig-trader\.env
```

The final boundary-aware evidence was regenerated from that immutable raw run,
with no broker request, using:

```powershell
poetry run python -m tools.g3a_market_data --environment demo --run-id g3a-01-20260815-v4 --raw-cache-run-id g3a-01-20260815 --offline-cache-only --source-acquisition-evidence .runtime/evidence/superseded/g3a-data-quality-pre-boundary-fix.json --end-utc 2026-08-14T22:00:00Z --intervals-per-series 700 --data-root .runtime/g3a/data --evidence-json .runtime/evidence/g3a-data-quality.json --dotenv C:\Users\AfifB\projects\ig-trader\.env
```

Stop if the command does not show `order_endpoint_call_count=0`. Do not change
the environment, enable a live hostname, set `PAPER_TRADING=false`, increase
the bounded interval count, or bypass allowance errors with another session.

## Current run result

The 2026-08-15 acquisition dynamically verified all three EPICs and produced
all 12 required 1H, 15M, 5M, and 1M series. Eleven series qualified. EUR/GBP 1M
has one broker omission between `2026-08-14T19:02:00Z` and
`2026-08-14T19:04:00Z`; EUR/USD and GBP/USD contain the intervening candle.
Nothing was filled. Leading, internal, and trailing requested-range gaps are
all represented in the final manifests, and a continuous gap is split when its
per-interval classification changes between market-session and weekend
closure. The run therefore remains `PARTIAL_DATA` even though 638 common 1M
candle starts are present.

Authoritative details and fingerprints are in:

- `artifacts/g3a/manifests/g3a-01-final/` in the merge-safe Git tree;
- the verified external package identified by
  `artifacts/g3a/external-package.json` for raw, normalized, and full audit data;
- `artifacts/g3a/evidence/g3a-02-summary.json` for the targeted recovery result.

No Scalper optimization, Demo order, active bot, or Live process was run.
