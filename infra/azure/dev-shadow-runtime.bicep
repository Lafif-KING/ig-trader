targetScope = 'resourceGroup'

@description('Digest-pinned SHADOW_DEMO image; repository@sha256:digest only.')
param containerImage string

@minLength(40)
@maxLength(40)
param imageCommitSha string

param prefix string
param containerEnvironmentName string
param containerRegistryName string
param shadowIdentityName string

@description('Key Vault secret reference identifier only; never a plaintext IG credential.')
@secure()
param igDemoSecretReference string

@description('Managed identity client ID used by PostgreSQL authentication.')
param postgresManagedIdentityClientId string

var appName = '${prefix}-shadow-runtime'
var safeEnvironment = [
  {
    name: 'EXECUTION_MODE'
    value: 'SHADOW_DEMO'
  }
  {
    name: 'APP_COMMIT_SHA'
    value: imageCommitSha
  }
  {
    name: 'SHADOW_REPLICA_COUNT'
    value: '1'
  }
  {
    name: 'POSTGRES_MANAGED_IDENTITY_CLIENT_ID'
    value: postgresManagedIdentityClientId
  }
  {
    name: 'IG_DEMO_SECRET_REFERENCE'
    secretRef: 'ig-demo-reference'
  }
]

resource environment 'Microsoft.App/managedEnvironments@2025-01-01' existing = {
  name: containerEnvironmentName
}

resource registry 'Microsoft.ContainerRegistry/registries@2023-07-01' existing = {
  name: containerRegistryName
}

resource identity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' existing = {
  name: shadowIdentityName
}

resource shadowRuntime 'Microsoft.App/containerApps@2025-01-01' = {
  name: appName
  location: resourceGroup().location
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${identity.id}': {}
    }
  }
  properties: {
    environmentId: environment.id
    configuration: {
      activeRevisionsMode: 'Single'
      ingress: {
        external: false
        targetPort: 8080
      }
      registries: [
        {
          identity: identity.id
          server: registry.properties.loginServer
        }
      ]
      secrets: [
        {
          name: 'ig-demo-reference'
          value: igDemoSecretReference
        }
      ]
    }
    template: {
      containers: [
        {
          name: 'shadow-runtime'
          image: containerImage
          env: safeEnvironment
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
    }
  }
}

output executionMode string = 'SHADOW_DEMO'
output orderAuthority bool = false
output workerName string = shadowRuntime.name
output alertContracts array = [
  'worker_replica_below_one'
  'worker_replica_above_one'
  'restart'
  'unsafe_shadow_startup'
  'broker_order_call_count_nonzero'
  'lease_loss'
  'repeated_stale_fence_rejection'
  'ig_authentication_failure'
  'lightstreamer_disconnected'
  'market_data_stale'
  'failed_safe_cycle'
  'database_unavailable'
]
