# G3A-02 Gap Recovery Summary

Final classification: **PARTIAL_DATA_WITH_VALID_GAP_POLICY**

- Native 19:03 returned: `False`
- SECOND availability: `AVAILABLE`
- Final 19:03 handling: `AUTHORITATIVE_GAP_RETAINED`
- Exact Scalper replay ready: `False`

## Adjacent-minute validation

### 2026-08-14T19:02:00+00:00

| Field | Reconstructed | Native | Delta | Match |
|---|---:|---:|---:|---|
| bid_open | 0.85457 | 0.85457 | 0.00000 | True |
| bid_high | 0.85457 | 0.85457 | 0.00000 | True |
| bid_low | 0.85456 | 0.85456 | 0.00000 | True |
| bid_close | 0.85456 | 0.85456 | 0.00000 | True |
| offer_open | 0.85466 | 0.85466 | 0.00000 | True |
| offer_high | 0.85466 | 0.85466 | 0.00000 | True |
| offer_low | 0.85465 | 0.85465 | 0.00000 | True |
| offer_close | 0.85465 | 0.85465 | 0.00000 | True |

### 2026-08-14T19:04:00+00:00

| Field | Reconstructed | Native | Delta | Match |
|---|---:|---:|---:|---|
| bid_open | 0.85455 | 0.85455 | 0.00000 | True |
| bid_high | 0.85456 | 0.85456 | 0.00000 | True |
| bid_low | 0.85452 | 0.85452 | 0.00000 | True |
| bid_close | 0.85454 | 0.85454 | 0.00000 | True |
| offer_open | 0.85464 | 0.85464 | 0.00000 | True |
| offer_high | 0.85465 | 0.85465 | 0.00000 | True |
| offer_low | 0.85461 | 0.85461 | 0.00000 | True |
| offer_close | 0.85463 | 0.85463 | 0.00000 | True |

## GAP_AWARE_REPLAY_V1

The affected signal is NO_TRADE. Indicator state crossing the gap is invalid. Warm-up must be rebuilt solely from subsequent authoritative candles, and trading cannot resume until every required timeframe and indicator is valid. The gap event must be recorded in replay evidence.
