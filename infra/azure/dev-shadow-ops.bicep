targetScope = 'resourceGroup'

@description('Azure region of the existing DEV/SHADOW resources and the log-search alert.')
param location string = resourceGroup().location

@description('Name of the existing NO_EXECUTION Container App. This stage reads it but does not manage it.')
param containerAppName string

@description('Name of the existing Log Analytics workspace. This stage reads it but does not configure it.')
param logAnalyticsWorkspaceName string

@description('Name of the DEV operator Action Group owned by this operations stage.')
param actionGroupName string

@description('Action Group short name shown in notifications. Azure permits at most 12 characters.')
@maxLength(12)
param actionGroupShortName string

@description('Project-operator email supplied only at validation/deployment time. Never commit the real address.')
@secure()
param operatorEmail string

@description('Name of the critical alert for fewer than one active replica.')
param replicaBelowOneAlertName string

@description('Name of the critical alert for more than one active replica.')
param replicaAboveOneAlertName string

@description('Name of the warning alert for a replica restart.')
param restartAlertName string

@description('Name of the critical structured-log alert for an unsafe runtime startup state.')
param unsafeRuntimeAlertName string

@description('Application source SHA expected in structured cloud_service_started logs.')
@allowed([
  '903dff5d07af03da593d3afff8b53c427704bd21'
])
param expectedApplicationSourceSha string

param tags object = {
  application: 'ig-trader'
  environment: 'dev-shadow'
  executionAuthority: 'none'
  managedBy: 'bicep'
  workOrder: 'G4B-02A1'
}

var metricEvaluationFrequency = 'PT1M'
var metricWindowSize = 'PT5M'
var unsafeRuntimeQuery = join([
  'ContainerAppConsoleLogs'
  '| where ContainerAppName == \'${containerAppName}\''
  '| extend payload = parse_json(Log)'
  '| where tostring(payload.event) == \'cloud_service_started\''
  '| extend execution_mode = tostring(payload.execution_mode), worker_enabled = tobool(payload.worker_enabled), release_sha = tostring(payload.commit_sha)'
  '| where isempty(execution_mode) or execution_mode != \'NO_EXECUTION\' or isnull(worker_enabled) or worker_enabled != false or isempty(release_sha) or release_sha != \'${expectedApplicationSourceSha}\''
  '| summarize UnsafeRuntimeStates = count()'
  '| where UnsafeRuntimeStates > 0'
], '\n')

resource executionWorker 'Microsoft.App/containerApps@2025-01-01' existing = {
  name: containerAppName
}

resource logAnalyticsWorkspace 'Microsoft.OperationalInsights/workspaces@2023-09-01' existing = {
  name: logAnalyticsWorkspaceName
}

resource operatorActionGroup 'Microsoft.Insights/actionGroups@2023-01-01' = {
  name: actionGroupName
  location: 'global'
  tags: tags
  properties: {
    enabled: true
    groupShortName: actionGroupShortName
    emailReceivers: [
      {
        emailAddress: operatorEmail
        name: 'project-operator'
        useCommonAlertSchema: true
      }
    ]
  }
}

resource replicaBelowOneAlert 'Microsoft.Insights/metricAlerts@2018-03-01' = {
  name: replicaBelowOneAlertName
  location: 'global'
  tags: tags
  properties: {
    actions: [
      {
        actionGroupId: operatorActionGroup.id
      }
    ]
    autoMitigate: true
    criteria: {
      'odata.type': 'Microsoft.Azure.Monitor.SingleResourceMultipleMetricCriteria'
      allOf: [
        {
          criterionType: 'StaticThresholdCriterion'
          metricName: 'Replicas'
          metricNamespace: 'Microsoft.App/containerApps'
          name: 'ReplicaCountBelowOne'
          operator: 'LessThan'
          skipMetricValidation: false
          threshold: 1
          timeAggregation: 'Maximum'
        }
      ]
    }
    description: 'Critical availability: the singleton NO_EXECUTION worker has had zero replicas for the complete five-minute window.'
    enabled: true
    evaluationFrequency: metricEvaluationFrequency
    scopes: [
      executionWorker.id
    ]
    severity: 0
    targetResourceRegion: location
    targetResourceType: 'Microsoft.App/containerApps'
    windowSize: metricWindowSize
  }
}

resource replicaAboveOneAlert 'Microsoft.Insights/metricAlerts@2018-03-01' = {
  name: replicaAboveOneAlertName
  location: 'global'
  tags: tags
  properties: {
    actions: [
      {
        actionGroupId: operatorActionGroup.id
      }
    ]
    autoMitigate: true
    criteria: {
      'odata.type': 'Microsoft.Azure.Monitor.SingleResourceMultipleMetricCriteria'
      allOf: [
        {
          criterionType: 'StaticThresholdCriterion'
          metricName: 'Replicas'
          metricNamespace: 'Microsoft.App/containerApps'
          name: 'ReplicaCountAboveOne'
          operator: 'GreaterThan'
          skipMetricValidation: false
          threshold: 1
          timeAggregation: 'Maximum'
        }
      ]
    }
    description: 'Critical singleton-safety drift: more than one execution-worker replica was observed in the five-minute window.'
    enabled: true
    evaluationFrequency: metricEvaluationFrequency
    scopes: [
      executionWorker.id
    ]
    severity: 0
    targetResourceRegion: location
    targetResourceType: 'Microsoft.App/containerApps'
    windowSize: metricWindowSize
  }
}

resource restartAlert 'Microsoft.Insights/metricAlerts@2018-03-01' = {
  name: restartAlertName
  location: 'global'
  tags: tags
  properties: {
    actions: [
      {
        actionGroupId: operatorActionGroup.id
      }
    ]
    autoMitigate: true
    criteria: {
      'odata.type': 'Microsoft.Azure.Monitor.SingleResourceMultipleMetricCriteria'
      allOf: [
        {
          criterionType: 'StaticThresholdCriterion'
          metricName: 'RestartCount'
          metricNamespace: 'Microsoft.App/containerApps'
          name: 'ReplicaRestartCountAboveZero'
          operator: 'GreaterThan'
          skipMetricValidation: false
          threshold: 0
          timeAggregation: 'Maximum'
        }
      ]
    }
    description: 'Warning: at least one replica restart was reported during the five-minute window.'
    enabled: true
    evaluationFrequency: metricEvaluationFrequency
    scopes: [
      executionWorker.id
    ]
    severity: 2
    targetResourceRegion: location
    targetResourceType: 'Microsoft.App/containerApps'
    windowSize: metricWindowSize
  }
}

resource unsafeRuntimeAlert 'Microsoft.Insights/scheduledQueryRules@2023-12-01' = {
  name: unsafeRuntimeAlertName
  location: location
  kind: 'LogAlert'
  tags: tags
  properties: {
    actions: {
      actionGroups: [
        operatorActionGroup.id
      ]
    }
    autoMitigate: true
    checkWorkspaceAlertsStorageConfigured: false
    criteria: {
      allOf: [
        {
          failingPeriods: {
            minFailingPeriodsToAlert: 1
            numberOfEvaluationPeriods: 1
          }
          operator: 'GreaterThan'
          query: unsafeRuntimeQuery
          threshold: 0
          timeAggregation: 'Count'
        }
      ]
    }
    description: 'Critical: a structured startup event reports a non-NO_EXECUTION mode, an enabled worker, or an unexpected/missing release SHA.'
    displayName: 'IG Trader DEV unsafe runtime startup state'
    enabled: true
    evaluationFrequency: 'PT5M'
    scopes: [
      logAnalyticsWorkspace.id
    ]
    severity: 0
    skipQueryValidation: false
    windowSize: 'PT5M'
  }
}

output actionGroupName string = operatorActionGroup.name
output applicationSafetyQuery string = unsafeRuntimeQuery
output metricAlertNames array = [
  replicaBelowOneAlert.name
  replicaAboveOneAlert.name
  restartAlert.name
]
output profile string = 'DEV_SHADOW_OPERATIONS_NO_EXECUTION'
output scheduledQueryAlertName string = unsafeRuntimeAlert.name
