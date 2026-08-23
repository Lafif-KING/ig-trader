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
