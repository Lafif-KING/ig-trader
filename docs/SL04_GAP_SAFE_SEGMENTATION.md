# SL-04 gap-safe contiguous research segments

## Root cause confirmed

Before this remediation, `CanonicalDataset.has_quality_failure` returned true
when any dataset gap was `MISSING_DATA`, `UNEXPLAINED_MISSING_DATA`, or
`PROVIDER_OUTAGE`.  SL-03 called `_blocked_reason` before simulation, so one
audited unexplained gap produced `DATA_QUALITY_FAIL` for every scheduled
strategy on that instrument/timeframe.

This affected direct H1 data as well as H4 data derived from it.  The local
complete-bucket resampler omitted incomplete M5, M15, and H4 buckets without
creating candles.  Those valid omissions became later target-timeframe gaps;
the audit then counted them independently from the underlying source gap.
Consequently one missing M1 row could block the M1-derived M5/M15 dataset and
produce additional derived audit gaps.  Sparse H1 gaps similarly invalidated a
whole two-year H1/H4 dataset.

## Remediation semantics

`GapSafeResearchSegmenter` preserves every unexplained gap as a hard boundary.
It never interpolates, forward-fills, synthesizes, or suppresses a candle.
Each side becomes an independent canonical segment with fingerprints and
boundary records.  No indicator history, strategy state, or open trade crosses
a boundary.

The fixed eligibility rule is 300 candles per segment.  The frozen simulator
requires 22 warm-up candles.  Three chronological 100-candle portions ensure
that development, validation, and untouched test can each contain at least 60
candles (22 warm-up plus 38 independently evaluated candles).  This is a
deterministic data-quality minimum and is not adjusted for strategy results.
Smaller spans are reported as `SEGMENT_TOO_SHORT`; they do not invalidate
other eligible clean spans.

Eligible observations are placed in chronological 60% development, 20%
validation, and final 20% untouched-test partitions.  Phase slices are never
allowed to borrow across a segment boundary.  A too-short phase slice is
excluded.  The existing conservative `END_OF_DATA` close applies separately to
every independent segment/phase slice, so no position can survive a hard gap.

Walk-forward windows are generated separately inside eligible segments.  The
artifact reports planned/accepted windows plus counts skipped because a hard
gap or short segment prevented a safe window.

## Resampling lineage

M1-to-M5/M15 and H1-to-H4 aggregation remains complete-bucket-only.  The
resampling manifest records each root `SOURCE_GAP` and every
`DERIVED_BUCKET_OMITTED` with links to the originating source-gap identifier.
Derived omissions remain hard research boundaries but are explicitly not
reported as separate provider failures.

## Scope and safety

This changes data-quality semantics only.  S0/S1–S7 definitions, parameter
grids, entry/stop/R:R rules, DQ-03 friction, source-alignment checks,
qualification thresholds, stress, bootstrap, and execution authority remain
unchanged.  SL-04 uses local files only: network acquisition, IG create/close,
Live, and Azure remain zero/off.
