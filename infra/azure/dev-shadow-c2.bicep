targetScope = 'resourceGroup'

@description('Azure region selected for the isolated G4B-01C2 deployment.')
param location string = resourceGroup().location

@description('Name of the existing Azure Container Registry. C2 reads it but does not manage it.')
param containerRegistryName string

@description('Name of the existing runtime user-assigned managed identity.')
param executionIdentityName string

@description('Name of the existing virtual network.')
param virtualNetworkName string

@description('Name of the existing subnet delegated to Container Apps environments.')
param containerSubnetName string

@description('Name of the existing Log Analytics workspace.')
param logAnalyticsWorkspaceName string

@description('Name of the Container Apps managed environment owned by C2.')
param containerEnvironmentName string

@description('Name of the environment diagnostic setting owned by C2.')
param environmentDiagnosticSettingName string

@description('Name of the Container App owned by C2.')
param containerAppName string

@description('Approved immutable C2 image. Only the reviewed repository@sha256 digest is accepted.')
@allowed([
  'igtrdevfrcbzkxc6c6acr.azurecr.io/ig-trader@sha256:cf90c62dbe81166414a864435bff8de2ab2adfd566dd22793571eb9d8accaf45'
])
param containerImage string

@description('Authoritative commit encoded into the approved C2 image.')
@allowed([
  '903dff5d07af03da593d3afff8b53c427704bd21'
])
param imageCommitSha string

param tags object = {
  application: 'ig-trader'
  environment: 'dev-shadow'
  executionAuthority: 'none'
  managedBy: 'bicep'
  workOrder: 'G4B-01C2'
}

var revisionSuffix = take(toLower(imageCommitSha), 12)
var safeEnvironment = [
  {
    name: 'APP_COMMIT_SHA'
    value: imageCommitSha
  }
  {
    name: 'APP_PORT'
    value: '8080'
  }
  {
    name: 'CONTAINER_APP_REVISION'
    value: '${containerAppName}--${revisionSuffix}'
  }
  {
    name: 'EXECUTION_MODE'
    value: 'NO_EXECUTION'
  }
  {
    name: 'LOG_LEVEL'
    value: 'INFO'
  }
  {
    name: 'SHUTDOWN_GRACE_SECONDS'
    value: '20'
  }
]

resource containerRegistry 'Microsoft.ContainerRegistry/registries@2023-07-01' existing = {
  name: containerRegistryName
}

resource executionIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' existing = {
  name: executionIdentityName
}

resource virtualNetwork 'Microsoft.Network/virtualNetworks@2024-05-01' existing = {
  name: virtualNetworkName
}

resource containerSubnet 'Microsoft.Network/virtualNetworks/subnets@2024-05-01' existing = {
  parent: virtualNetwork
  name: containerSubnetName
}

resource logAnalyticsWorkspace 'Microsoft.OperationalInsights/workspaces@2023-09-01' existing = {
  name: logAnalyticsWorkspaceName
}

resource containerEnvironment 'Microsoft.App/managedEnvironments@2025-01-01' = {
  name: containerEnvironmentName
  location: location
  tags: tags
  properties: {
    appLogsConfiguration: {
      destination: 'azure-monitor'
    }
    vnetConfiguration: {
      infrastructureSubnetId: containerSubnet.id
      internal: true
    }
    zoneRedundant: false
  }
}

resource environmentDiagnostics 'Microsoft.Insights/diagnosticSettings@2021-05-01-preview' = {
  name: environmentDiagnosticSettingName
  scope: containerEnvironment
  properties: {
    logAnalyticsDestinationType: 'Dedicated'
    logs: [
      {
        category: 'ContainerAppConsoleLogs'
        enabled: true
      }
      {
        category: 'ContainerAppSystemLogs'
        enabled: true
      }
    ]
    metrics: [
      {
        category: 'AllMetrics'
        enabled: true
      }
    ]
    workspaceId: logAnalyticsWorkspace.id
  }
}

resource executionWorker 'Microsoft.App/containerApps@2025-01-01' = {
  name: containerAppName
  location: location
  tags: tags
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${executionIdentity.id}': {}
    }
  }
  properties: {
    environmentId: containerEnvironment.id
    configuration: {
      activeRevisionsMode: 'Single'
      ingress: {
        allowInsecure: false
        external: false
        targetPort: 8080
        transport: 'http'
      }
      maxInactiveRevisions: 10
      registries: [
        {
          identity: executionIdentity.id
          server: containerRegistry.properties.loginServer
        }
      ]
      secrets: []
    }
    template: {
      revisionSuffix: revisionSuffix
      containers: [
        {
          name: 'execution-worker'
          image: containerImage
          env: safeEnvironment
          probes: [
            {
              type: 'Startup'
              httpGet: {
                path: '/health/live'
                port: 8080
                scheme: 'HTTP'
              }
              failureThreshold: 12
              initialDelaySeconds: 2
              periodSeconds: 5
              timeoutSeconds: 2
            }
            {
              type: 'Liveness'
              httpGet: {
                path: '/health/live'
                port: 8080
                scheme: 'HTTP'
              }
              failureThreshold: 3
              initialDelaySeconds: 5
              periodSeconds: 10
              timeoutSeconds: 2
            }
            {
              type: 'Readiness'
              httpGet: {
                path: '/health/ready'
                port: 8080
                scheme: 'HTTP'
              }
              failureThreshold: 3
              initialDelaySeconds: 2
              periodSeconds: 5
              successThreshold: 1
              timeoutSeconds: 2
            }
          ]
          resources: {
            cpu: json('0.5')
            memory: '1Gi'
          }
        }
      ]
      scale: {
        minReplicas: 1
        maxReplicas: 1
      }
      terminationGracePeriodSeconds: 30
    }
  }
}

output containerAppName string = executionWorker.name
output containerEnvironmentName string = containerEnvironment.name
output executionMode string = 'NO_EXECUTION'
output imageIdentity string = containerImage
output maxReplicas int = 1
output minReplicas int = 1
output profile string = 'DEV_SHADOW_C2_ISOLATED'
