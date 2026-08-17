# IG Trader project state

Updated: 2026-08-17
Authoritative branch: `origin/main`
Last verified source baseline: `e7f37c143baf0a6ca5819144c2f7780eef72b76d`
Current source SHA: verify with `git rev-parse origin/main`.

## Current gate

G4 final lease-enabled `NO_EXECUTION` application deployment is pending.

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
- Deployed revision: `igtrdevfrc-execution-worker--903dff5d07af`.
- Mode: `NO_EXECUTION`; steady replicas: 1; min/max: 1/1.
- Internal ingress: yes; secrets: 0; broker configuration: none.

## Monitoring and safety

Four monitoring rules are enabled. IG credentials, IG REST, Lightstreamer,
orders, and positions are all 0.

## Next actions

1. Deploy the lease-enabled `NO_EXECUTION` application.
2. Close G4.
3. Start `SHADOW_DEMO` with S0 only.
4. Engineering Shadow qualification.
5. Build the Strategy Lab foundation, then S1 and S2 challengers.
