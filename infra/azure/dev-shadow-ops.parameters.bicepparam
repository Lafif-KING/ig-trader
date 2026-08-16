using './dev-shadow-ops.bicep'

param location = 'francecentral'
param containerAppName = 'igtrdevfrc-execution-worker'
param logAnalyticsWorkspaceName = 'igtrdevfrc-logs'
param actionGroupName = 'igtrdevfrc-ops-ag'
param actionGroupShortName = 'igtrdevops'
param operatorEmail = readEnvironmentVariable('AZURE_ALERT_EMAIL')
param replicaBelowOneAlertName = 'igtrdevfrc-replicas-below-one'
param replicaAboveOneAlertName = 'igtrdevfrc-replicas-above-one'
param restartAlertName = 'igtrdevfrc-restarts'
param unsafeRuntimeAlertName = 'igtrdevfrc-unsafe-runtime'
param expectedApplicationSourceSha = '903dff5d07af03da593d3afff8b53c427704bd21'
