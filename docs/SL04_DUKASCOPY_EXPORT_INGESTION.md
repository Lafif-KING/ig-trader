# SL-04 local Dukascopy-go CSV ingestion

SL-04's primary deep-history input is local data exported with
`dukascopy-go 0.2.0`, engine `jetta`, from the Dukascopy public historical
feed. It does not call the developer API during a replay.

The local root contains `m1_90d` and `h1_2y` directories. Every `*.csv` file
is checked against this exact ordered schema:

```text
timestamp,mid_open,mid_high,mid_low,mid_close,spread,volume,bid_open,bid_high,bid_low,bid_close,ask_open,ask_high,ask_low,ask_close
```

Rows require a UTC `Z` timestamp, chronological ordering, unique timestamps,
valid mid/bid/ask OHLC geometry, non-negative provider spread and volume, and
mid values within a deterministic `0.0000005` bid/ask-midpoint tolerance. A
failed file is recorded as rejected; SL-04 does not repair a row, interpolate
a candle, or claim the data is IG candle evidence.

Provenance is recorded as `DUKASCOPY_PUBLIC_FEED`, `LOCAL_CSV_EXPORT`,
`dukascopy-go 0.2.0`, `jetta`, and `EXTERNAL_UNVERIFIED`, alongside its
absolute source path, byte size, raw SHA-256, row count, schema, and UTC
range. Provider spread remains validation evidence only; the backtest cost
model remains the reviewed DQ-03 IG-linked model.

The legacy reviewed local structured-export contract remains a secondary
offline source. Cached Yahoo research history is third priority. Neither path
may download data during SL-04.
