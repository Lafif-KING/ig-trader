# IG Trader project state

Updated: 2026-08-17
Authoritative branch: `origin/main`
Last verified source baseline: `95fbc14f5c99a2715ceb6085af29c99e62c9793f`
Current source SHA: verify with `git rev-parse origin/main`.

## Current gate

G4 COMPLETE — ready for `SHADOW_DEMO` preparation.

## PostgreSQL

- Migrations 001 and 002 complete.
- Durable owner: `ig_trader_schema_owner`.
- Runtime principal: `igtrdevfrc-execution-identity`.
- Runtime ACL: missing=0, excess=0.
- Managed-identity authentication: PASS.
- Private PostgreSQL/TLS: PASS.
- Real Azure lease: PASS. Fencing: T1=1, T2=2, T2>T1, stale T1 rejected.

## Azure application

- Resource group: `rg-igtrader-dev-frc-001`.
- Container App: `igtrdevfrc-execution-worker`.
- Deployed revision: `igtrdevfrc-execution-worker--693b2a3d083e`.
- Image: `igtrdevfrcbzkxc6c6acr.azurecr.io/ig-trader@sha256:e3481e90b7e77d5b0597bdc958920637161e98cecdf6dd21f75c85442d3b15d6`
  from source `693b2a3d083e011dc1c7072cf1f6bcf090e47740`.
- Mode: `NO_EXECUTION`; steady replicas: 1; min/max: 1/1.
- Internal ingress: yes; secrets: 0; broker configuration: none.

## Monitoring and safety

Four monitoring rules are enabled. IG credentials, IG REST, Lightstreamer,
orders, and positions are all 0.

## Next actions

1. Prepare `SHADOW_DEMO` with S0 only; do not begin it without a separate gate.
2. Engineering Shadow qualification.
3. Build the Strategy Lab foundation, then S1 and S2 challengers.

## Strategy Lab (SL-01)

- A local broker-neutral Strategy Lab has been added as a separate research
  package. It has no broker, Demo, Live, cloud, or database execution path.
- The initial 26-instrument universe is research-only. IG EPICs and broker
  metadata remain UNKNOWN until a separate read-only discovery process verifies
  them.
- Local datasets retain UTC timestamps, source/source-quality, gaps, synthetic
  flags, and source/dataset fingerprints. Missing friction blocks promotion as
  `COST_MODEL_INCOMPLETE`.
- The Control Center can show local generated Strategy Lab artifacts. It has no
  promotion button; Demo and Live authority remain disabled.

## DQ-03 Instrument Resolution and Data

- DQ-03 is a separate, read-only resolver for the existing 26-symbol research
  universe. It uses explicit IG search aliases, contract-type exclusions,
  metadata fingerprints, batched V2 market reads with bounded V4 fallbacks,
  and a centrally counted rolling limit of 25 non-trading REST calls per
  sixty seconds. A 403 is classified and safe-stopped rather than retried.
- Discovery/metadata, bounded history, and streaming smoke are separate,
  resumable phases. Later phases require fresh Phase 1 artifacts for the same
  sanitized Demo account identity and resolver version.
- History validation preserves normalized, sanitized 20-point broker samples
  with timestamp parser evidence, bid/ask-or-offer OHLC, spread, row-quality,
  and fingerprint facts. `snapshotTimeUTC` is preferred; broker-local
  `snapshotTime` is converted only using the authenticated IG session's
  declared UTC offset and otherwise fails closed.
- Later phases augment the Phase 1 registry instead of replacing its resolution
  provenance. History and streaming evidence are separate ignored artifacts;
  streaming uses one bounded Lightstreamer session and subscription, explicit
  broker account identity, system-trust TLS, fresh quote vetoes, and a clean
  disconnect.
- The resulting local artifacts are ignored by Git. They record broker facts
  and bounded validation samples only; they never create a Demo execution
  registration or change the disabled Demo/Live authority gates.
- A small IG history sample validates broker response shape and attribution. It
  is not broad Strategy Lab history. Until a reviewed local or external
  dataset is supplied, research remains `DATA_NOT_AVAILABLE` and exact cost
  models remain `COST_MODEL_INCOMPLETE`.
