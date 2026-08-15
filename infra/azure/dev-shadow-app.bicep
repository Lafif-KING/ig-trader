targetScope = 'resourceGroup'

@description('Short workload prefix matching the low-cost DEV/SHADOW foundation deployment.')
param prefix string

@description('Exact OCI image reference. The input must use repository@sha256:DIGEST form.')
param containerImage string

@description('Full 40-character Git commit used to build the image.')
@minLength(40)
@maxLength(40)
param imageCommitSha string

@description('Container Registry name output by dev-shadow-foundation.bicep.')
param containerRegistryName string

@description('Container Apps environment name output by dev-shadow-foundation.bicep.')
param containerEnvironmentName string

@description('User-assigned managed identity name output by dev-shadow-foundation.bicep.')
param executionIdentityName string

param tags object = {
  application: 'ig-trader'
  environment: 'dev-shadow'
  executionAuthority: 'none'
  managedBy: 'bicep'
  workOrder: 'G4B-00'
}

var appName = '${prefix}-execution-worker'
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
    value: '${appName}--${revisionSuffix}'
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

resource containerEnvironment 'Microsoft.App/managedEnvironments@2025-01-01' existing = {
  name: containerEnvironmentName
}

resource executionIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' existing = {
  name: executionIdentityName
}

resource executionWorker 'Microsoft.App/containerApps@2025-01-01' = {
  name: appName
  location: resourceGroup().location
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

output executionMode string = 'NO_EXECUTION'
output executionWorkerName string = executionWorker.name
output imageIdentity string = containerImage
output maxReplicas int = 1
output minReplicas int = 1
output profile string = 'DEV_SHADOW_LOW_COST'
