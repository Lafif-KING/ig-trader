# G4B-02A1 operational alerts and restart monitoring

Status: SOURCE / IAC / CI VALIDATION ONLY. No alert, Action Group, restart, or
other Azure mutation is authorized by G4B-02A1.

Base: `76c8fcd6b0c735dd4a1e1d2ffc622c80075ad4c2`

Target resource group: `rg-igtrader-dev-frc-001`. Existing application:
`igtrdevfrc-execution-worker`. Existing workspace: `igtrdevfrc-logs`.

## Ownership boundary

`dev-shadow-ops.bicep` may create exactly one Action Group, three metric alert
rules, and one scheduled log-search alert rule. The Container App and Log
Analytics workspace are `existing` references. The stage does not own or
configure the Container Apps environment, ACR, identities, PostgreSQL,
networking, private DNS, workspace settings, budget, role assignments, or Key
Vault.

The Action Group has one common-schema email receiver named
`project-operator`. The reviewed address is supplied at validation or deployment
time through `AZURE_ALERT_EMAIL`; its real value is not committed. SMS, voice,
webhook, ITSM, function, Logic App, and automation receivers are excluded.

## Alert definitions

Azure severity values are `0` Critical, `1` Error, `2` Warning, `3`
Informational, and `4` Verbose. This stage uses:

| Alert | Signal and threshold | Severity | Evaluation |
| --- | --- | --- | --- |
| Replica below one | `Replicas`, Maximum `< 1` | 0 Critical | Every 1 minute over 5 minutes |
| Replica above one | `Replicas`, Maximum `> 1` | 0 Critical | Every 1 minute over 5 minutes |
| Restart | `RestartCount`, Maximum `> 0` | 2 Warning | Every 1 minute over 5 minutes |
| Unsafe startup | Count of bounded structured-query matches `> 0` | 0 Critical | Every 5 minutes over 5 minutes |

The five-minute metric window filters single-sample telemetry gaps while still
detecting availability loss quickly. Maximum is the service's primary
aggregation for both live metrics. For the singleton guard and restart signal,
Maximum also ensures any unsafe high replica count or restart in the window is
visible. All rules are stateful and automatically resolve when their condition
clears. An alert is evidence only; it never grants execution authority.

## Actual Log Analytics schema and bounded query

Read-only inspection on 2026-08-16 confirmed these current dedicated tables:

- `ContainerAppConsoleLogs`: `TimeGenerated` (datetime), `ContainerName`,
  `ContainerGroupName`, `ContainerImage`, `Stream`, `ContainerGroupId`, `Log`,
  `ContainerAppName`, `ContainerId`, and `RevisionName`;
- `ContainerAppSystemLogs`: includes `TimeGenerated`, `Log`, `RevisionName`,
  `ContainerAppName`, `ReplicaName`, `Reason`, and `EventSource`.

The current `cloud_service_started` console record is valid JSON. It reliably
contains `event`, `execution_mode`, `worker_enabled`, `worker_process_count`,
and `commit_sha`. The observed safe values are `NO_EXECUTION`, `false`, `1`,
and application source SHA
`903dff5d07af03da593d3afff8b53c427704bd21`. The field `authorized` and the
network/broker counters are not present in this startup record, so this alert
does not pretend to validate them from logs. They remain mandatory direct
health/readiness evidence, and adding them to a future structured safety event
is required before `SHADOW_DEMO` observability can claim that coverage.

The alert uses only the proven current schema:

```kusto
ContainerAppConsoleLogs
| where ContainerAppName == 'igtrdevfrc-execution-worker'
| extend payload = parse_json(Log)
| where tostring(payload.event) == 'cloud_service_started'
| extend execution_mode = tostring(payload.execution_mode), worker_enabled = tobool(payload.worker_enabled), release_sha = tostring(payload.commit_sha)
| where isempty(execution_mode) or execution_mode != 'NO_EXECUTION' or isnull(worker_enabled) or worker_enabled != false or isempty(release_sha) or release_sha != '903dff5d07af03da593d3afff8b53c427704bd21'
| summarize UnsafeRuntimeStates = count()
| where UnsafeRuntimeStates > 0
```

The query is intentionally bounded to structured startup safety and release
identity. It does not use guessed `_s` columns, substring matching, broker data,
or a fabricated `authorized` field.

## Cost guardrail

The Azure Retail Prices API (EUR, France Central reference) reports the first
10 monitored metric time series at EUR 0 and additional static metric time
series at EUR 0.0878/month each. This stage adds three static series and uses no
dynamic thresholds. With no existing metric alert rules in this project stage,
their planning increment is EUR 0 within the included tier.

One five-minute system-log alert is EUR 1.3164/month. The one email receiver is
EUR 0 at the current published email-notification tiers. The planning increment
is therefore approximately EUR 1.32/month before taxes or agreement-specific
pricing. Adding that to the accepted complete-environment references yields
approximately EUR 33.37/month normal and EUR 53.96/month conservative. These
are planning estimates, not guaranteed Azure charges, and remain below the EUR
60 budget and EUR 75 management-review threshold.

## G4B-02A2 restart drill — DO NOT EXECUTE DURING G4B-02A1

This procedure requires a separate G4B-02A2 approval. Run it from Windows
PowerShell against the reviewed current/default subscription. Stop immediately
if the resource names, active revision, replica count, health, or execution
safety state differ from the approved baseline.

Before the action, capture and sanitize evidence for:

1. exactly one active, Healthy/Ready revision and exactly one replica;
2. the replica name and current restart metric;
3. HTTP 200 from `/health`, `/health/live`, and `/health/ready` from the existing
   internal validation path;
4. `EXECUTION_MODE=NO_EXECUTION`, `authorized=false`, `worker_enabled=false`,
   `worker_process_count=1`;
5. `network_call_count = 0`, `ig_rest_call_count = 0`,
   `lightstreamer_connection_count = 0`, `order_endpoint_call_count = 0`, and
   `credential_resolution_count = 0`.

Set reviewed PowerShell variables for the fixed resource group, app, and active
revision. Verify each value before proceeding. The only authorized future
mutation is Azure's supported revision restart command, exactly once:

```powershell
az containerapp revision restart --resource-group $drillResourceGroup --name $drillContainerApp --revision $drillActiveRevision
```

Do not retry the restart if the command result is unknown, times out, or returns
an error. Treat the state as unknown, stop, and inspect read-only Azure state.

After the single restart, wait within the separately approved bounded window
and prove:

1. the same revision returns Healthy/Ready with exactly one replica and never
   exposes a second execution replica;
2. all three health endpoints return HTTP 200;
3. `EXECUTION_MODE=NO_EXECUTION`, authorization remains false, the worker stays
   disabled, and all five safety counters remain zero;
4. Log Analytics receives the relevant restart/start lifecycle records and a
   new structured safe `cloud_service_started` event;
5. the restart alert fires and the Action Group notification evidence contains
   no secret or unmasked private data.

Forbidden during the drill: scaling, revision activation changes, new images,
configuration changes, secrets, role changes, database access, IG credentials,
IG/Lightstreamer connections, Demo/Live execution, orders, positions, and
working orders.
