targetScope = 'resourceGroup'

@description('Short workload prefix matching the foundation deployment.')
param prefix string

@description('Exact OCI image reference. Production input must use an @sha256 digest.')
param containerImage string

@description('Full 40-character Git commit used to build the image.')
@minLength(40)
@maxLength(40)
param imageCommitSha string

@description('Container Registry name output by foundation.bicep.')
param containerRegistryName string

@description('Container Apps environment name output by foundation.bicep.')
param containerEnvironmentName string

@description('User-assigned managed identity name output by foundation.bicep.')
param executionIdentityName string

@description('Key Vault name output by foundation.bicep.')
param keyVaultName string

@description('Must remain false in G4A. Reserved for a later separately approved execution composition.')
param enableBrokerSecretReferences bool = false

param tags object = {
  application: 'ig-trader'
  executionAuthority: 'none'
  managedBy: 'bicep'
  workOrder: 'G4A-01'
}

var appName = '${prefix}-execution-worker'
var revisionSuffix = take(toLower(imageCommitSha), 12)
var brokerSecrets = enableBrokerSecretReferences ? [
  {
    identity: executionIdentity.id
    keyVaultUrl: '${keyVault.properties.vaultUri}secrets/ig-api-key'
    name: 'ig-api-key'
  }
  {
    identity: executionIdentity.id
    keyVaultUrl: '${keyVault.properties.vaultUri}secrets/ig-identifier'
    name: 'ig-identifier'
  }
  {
    identity: executionIdentity.id
    keyVaultUrl: '${keyVault.properties.vaultUri}secrets/ig-password'
    name: 'ig-password'
  }
] : []
var brokerEnvironment = enableBrokerSecretReferences ? [
  {
    name: 'IG_API_KEY'
    secretRef: 'ig-api-key'
  }
  {
    name: 'IG_IDENTIFIER'
    secretRef: 'ig-identifier'
  }
  {
    name: 'IG_PASSWORD'
    secretRef: 'ig-password'
  }
] : []
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

resource keyVault 'Microsoft.KeyVault/vaults@2023-07-01' existing = {
  name: keyVaultName
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
      secrets: brokerSecrets
    }
    template: {
      revisionSuffix: revisionSuffix
      containers: [
        {
          name: 'execution-worker'
          image: containerImage
          env: concat(safeEnvironment, brokerEnvironment)
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
