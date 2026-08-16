targetScope = 'resourceGroup'

@description('Azure region of the existing DEV/SHADOW resources.')
param location string = resourceGroup().location

@description('Existing private Container Apps managed environment.')
param containerEnvironmentName string

@description('Existing Azure Container Registry.')
param containerRegistryName string

@description('Existing PostgreSQL Flexible Server.')
param postgresServerName string

@description('Existing permanent runtime user-assigned managed identity.')
param runtimeIdentityName string

@description('Temporary bootstrap-only user-assigned managed identity.')
param bootstrapIdentityName string = 'igtrdevfrc-db-bootstrap-identity'

@description('Object ID read from the bootstrap UAMI after the identity-only phase. Required because ARM administrator child names must be known before deployment starts.')
@minLength(36)
@maxLength(36)
param bootstrapIdentityPrincipalId string

@description('Create only the temporary UAMI so its generated object ID can be read before the full finite-job phase.')
param bootstrapIdentityOnly bool = false

@description('Temporary administrative Container Apps Job.')
param bootstrapJobName string = 'igtrdevfrc-db-bootstrap'

@description('Temporary non-admin runtime database probe job.')
param runtimeProbeJobName string = 'igtrdevfrc-runtime-db-probe'

@description('Reviewed immutable database-job image in repository@sha256 form.')
@minLength(80)
param bootstrapImage string

param tags object = {
  project: 'ig-trader'
  application: 'ig-trader'
  environment: 'dev-shadow'
  purpose: 'db-bootstrap-temporary'
  'execution-authority': 'none'
  lifecycle: 'temporary'
  managedBy: 'bicep'
  workOrder: 'G4B-02B2A'
}

var databaseName = 'ig_trader'
var runtimePrincipalName = 'igtrdevfrc-execution-identity'
var acrPullRoleId = subscriptionResourceId(
  'Microsoft.Authorization/roleDefinitions',
  '7f951dda-4ed3-4680-a7ca-43fe172d538d'
)

resource containerEnvironment 'Microsoft.App/managedEnvironments@2025-01-01' existing = {
  name: containerEnvironmentName
}

resource containerRegistry 'Microsoft.ContainerRegistry/registries@2023-07-01' existing = {
  name: containerRegistryName
}

resource postgres 'Microsoft.DBforPostgreSQL/flexibleServers@2024-08-01' existing = {
  name: postgresServerName
}

resource runtimeIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' existing = {
  name: runtimeIdentityName
}

resource bootstrapIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: bootstrapIdentityName
  location: location
  tags: tags
}

resource bootstrapAcrPull 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (!bootstrapIdentityOnly) {
  name: guid(containerRegistry.id, bootstrapIdentity.id, acrPullRoleId)
  scope: containerRegistry
  properties: {
    principalId: bootstrapIdentity.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: acrPullRoleId
  }
}

resource postgresBootstrapAdministrator 'Microsoft.DBforPostgreSQL/flexibleServers/administrators@2024-08-01' = if (!bootstrapIdentityOnly) {
  parent: postgres
  name: bootstrapIdentityPrincipalId
  properties: {
    principalName: bootstrapIdentity.name
    principalType: 'ServicePrincipal'
    tenantId: tenant().tenantId
  }
}

resource bootstrapJob 'Microsoft.App/jobs@2025-01-01' = if (!bootstrapIdentityOnly) {
  name: bootstrapJobName
  location: location
  tags: tags
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${bootstrapIdentity.id}': {}
    }
  }
  properties: {
    environmentId: containerEnvironment.id
    configuration: {
      manualTriggerConfig: {
        parallelism: 1
        replicaCompletionCount: 1
      }
      registries: [
        {
          identity: bootstrapIdentity.id
          server: containerRegistry.properties.loginServer
        }
      ]
      replicaRetryLimit: 0
      replicaTimeout: 600
      secrets: []
      triggerType: 'Manual'
    }
    template: {
      containers: [
        {
          name: 'db-bootstrap'
          image: bootstrapImage
          args: [
            'schema-inspect'
            '--evidence'
            '/tmp/schema-inspection-evidence.json'
          ]
          env: [
            {
              name: 'AZURE_CLIENT_ID'
              value: bootstrapIdentity.properties.clientId
            }
            {
              name: 'JOB_IDENTITY_NAME'
              value: bootstrapIdentity.name
            }
            {
              name: 'JOB_UAMI_OBJECT_ID'
              value: bootstrapIdentity.properties.principalId
            }
            {
              name: 'POSTGRES_DATABASE'
              value: databaseName
            }
            {
              name: 'POSTGRES_HOST'
              value: postgres.properties.fullyQualifiedDomainName
            }
            {
              name: 'RUNTIME_UAMI_OBJECT_ID'
              value: runtimeIdentity.properties.principalId
            }
          ]
          resources: {
            cpu: json('0.5')
            memory: '1Gi'
          }
        }
      ]
    }
  }
  dependsOn: [
    bootstrapAcrPull
    postgresBootstrapAdministrator
  ]
}

resource runtimeProbeJob 'Microsoft.App/jobs@2025-01-01' = if (!bootstrapIdentityOnly) {
  name: runtimeProbeJobName
  location: location
  tags: union(tags, {
    purpose: 'runtime-db-probe-temporary'
  })
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${runtimeIdentity.id}': {}
    }
  }
  properties: {
    environmentId: containerEnvironment.id
    configuration: {
      manualTriggerConfig: {
        parallelism: 1
        replicaCompletionCount: 1
      }
      registries: [
        {
          identity: runtimeIdentity.id
          server: containerRegistry.properties.loginServer
        }
      ]
      replicaRetryLimit: 0
      replicaTimeout: 300
      secrets: []
      triggerType: 'Manual'
    }
    template: {
      containers: [
        {
          name: 'runtime-db-probe'
          image: bootstrapImage
          args: [
            'runtime-probe'
            '--evidence'
            '/tmp/runtime-probe-evidence.json'
          ]
          env: [
            {
              name: 'AZURE_CLIENT_ID'
              value: runtimeIdentity.properties.clientId
            }
            {
              name: 'JOB_IDENTITY_NAME'
              value: runtimePrincipalName
            }
            {
              name: 'JOB_UAMI_OBJECT_ID'
              value: runtimeIdentity.properties.principalId
            }
            {
              name: 'POSTGRES_DATABASE'
              value: databaseName
            }
            {
              name: 'POSTGRES_HOST'
              value: postgres.properties.fullyQualifiedDomainName
            }
          ]
          resources: {
            cpu: json('0.5')
            memory: '1Gi'
          }
        }
      ]
    }
  }
}

output bootstrapIdentityName string = bootstrapIdentity.name
output bootstrapJobName string = bootstrapJobName
output runtimeProbeJobName string = runtimeProbeJobName
output ownership string[] = [
  'Microsoft.ManagedIdentity/userAssignedIdentities'
  'Microsoft.Authorization/roleAssignments'
  'Microsoft.DBforPostgreSQL/flexibleServers/administrators'
  'Microsoft.App/jobs:bootstrap'
  'Microsoft.App/jobs:runtime-probe'
]
output profile string = 'DEV_SHADOW_DB_BOOTSTRAP_TEMPORARY'
