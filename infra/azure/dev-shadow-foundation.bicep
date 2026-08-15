targetScope = 'resourceGroup'

@description('Approved lowercase workload prefix for the low-cost DEV/SHADOW environment.')
@minLength(3)
@maxLength(12)
param prefix string

@description('Azure region selected for the low-cost DEV/SHADOW deployment.')
param location string = resourceGroup().location

@description('Object ID of the human-controlled Microsoft Entra PostgreSQL administrator.')
param postgresEntraAdminObjectId string

@description('Display name of the Microsoft Entra PostgreSQL administrator.')
param postgresEntraAdminPrincipalName string

@allowed([
  'Group'
  'ServicePrincipal'
  'User'
])
param postgresEntraAdminPrincipalType string = 'Group'

param tags object = {
  application: 'ig-trader'
  environment: 'dev-shadow'
  executionAuthority: 'none'
  managedBy: 'bicep'
  workOrder: 'G4B-00'
}

var compactPrefix = toLower(replace(prefix, '-', ''))
var suffix = take(uniqueString(subscription().id, resourceGroup().id), 8)
var acrName = take('${compactPrefix}${suffix}acr', 50)
var postgresName = take('${compactPrefix}-${suffix}-pg', 63)
var environmentName = '${prefix}-aca-env'
var identityName = '${prefix}-execution-identity'
var workspaceName = '${prefix}-logs'
var virtualNetworkName = '${prefix}-vnet'
var containerSubnetName = 'container-apps'
var postgresSubnetName = 'postgresql'
var postgresPrivateDnsName = '${compactPrefix}.postgres.database.azure.com'
var acrPullRoleId = subscriptionResourceId(
  'Microsoft.Authorization/roleDefinitions',
  '7f951dda-4ed3-4680-a7ca-43fe172d538d'
)

resource workspace 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: workspaceName
  location: location
  tags: tags
  properties: {
    features: {
      disableLocalAuth: true
      enableLogAccessUsingOnlyResourcePermissions: true
    }
    publicNetworkAccessForIngestion: 'Enabled'
    publicNetworkAccessForQuery: 'Enabled'
    retentionInDays: 30
    sku: {
      name: 'PerGB2018'
    }
  }
}

resource virtualNetwork 'Microsoft.Network/virtualNetworks@2024-05-01' = {
  name: virtualNetworkName
  location: location
  tags: tags
  properties: {
    addressSpace: {
      addressPrefixes: [
        '10.42.0.0/16'
      ]
    }
    subnets: [
      {
        name: containerSubnetName
        properties: {
          addressPrefix: '10.42.0.0/23'
          delegations: [
            {
              name: 'container-apps-delegation'
              properties: {
                serviceName: 'Microsoft.App/environments'
              }
            }
          ]
        }
      }
      {
        name: postgresSubnetName
        properties: {
          addressPrefix: '10.42.2.0/28'
          delegations: [
            {
              name: 'postgresql-delegation'
              properties: {
                serviceName: 'Microsoft.DBforPostgreSQL/flexibleServers'
              }
            }
          ]
        }
      }
    ]
  }
}

resource postgresPrivateDns 'Microsoft.Network/privateDnsZones@2024-06-01' = {
  name: postgresPrivateDnsName
  location: 'global'
  tags: tags
}

resource postgresDnsLink 'Microsoft.Network/privateDnsZones/virtualNetworkLinks@2024-06-01' = {
  parent: postgresPrivateDns
  name: '${prefix}-postgres-link'
  location: 'global'
  properties: {
    registrationEnabled: false
    virtualNetwork: {
      id: virtualNetwork.id
    }
  }
}

resource executionIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: identityName
  location: location
  tags: tags
}

resource containerRegistry 'Microsoft.ContainerRegistry/registries@2023-07-01' = {
  name: acrName
  location: location
  tags: tags
  sku: {
    name: 'Basic'
  }
  properties: {
    adminUserEnabled: false
    dataEndpointEnabled: false
    publicNetworkAccess: 'Enabled'
  }
}

resource acrPullAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(containerRegistry.id, executionIdentity.id, acrPullRoleId)
  scope: containerRegistry
  properties: {
    principalId: executionIdentity.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: acrPullRoleId
  }
}

resource postgres 'Microsoft.DBforPostgreSQL/flexibleServers@2024-08-01' = {
  name: postgresName
  location: location
  tags: tags
  sku: {
    name: 'Standard_B1ms'
    tier: 'Burstable'
  }
  properties: {
    authConfig: {
      activeDirectoryAuth: 'Enabled'
      passwordAuth: 'Disabled'
      tenantId: tenant().tenantId
    }
    backup: {
      backupRetentionDays: 7
      geoRedundantBackup: 'Disabled'
    }
    highAvailability: {
      mode: 'Disabled'
    }
    network: {
      delegatedSubnetResourceId: resourceId(
        'Microsoft.Network/virtualNetworks/subnets',
        virtualNetwork.name,
        postgresSubnetName
      )
      privateDnsZoneArmResourceId: postgresPrivateDns.id
      publicNetworkAccess: 'Disabled'
    }
    storage: {
      autoGrow: 'Enabled'
      storageSizeGB: 32
    }
    version: '16'
  }
  dependsOn: [
    postgresDnsLink
  ]
}

resource postgresAdministrator 'Microsoft.DBforPostgreSQL/flexibleServers/administrators@2024-08-01' = {
  parent: postgres
  name: postgresEntraAdminObjectId
  properties: {
    principalName: postgresEntraAdminPrincipalName
    principalType: postgresEntraAdminPrincipalType
    tenantId: tenant().tenantId
  }
}

resource tradingDatabase 'Microsoft.DBforPostgreSQL/flexibleServers/databases@2024-08-01' = {
  parent: postgres
  name: 'ig_trader'
  properties: {
    charset: 'UTF8'
    collation: 'en_US.utf8'
  }
}

resource containerEnvironment 'Microsoft.App/managedEnvironments@2025-01-01' = {
  name: environmentName
  location: location
  tags: tags
  properties: {
    appLogsConfiguration: {
      destination: 'azure-monitor'
    }
    vnetConfiguration: {
      infrastructureSubnetId: resourceId(
        'Microsoft.Network/virtualNetworks/subnets',
        virtualNetwork.name,
        containerSubnetName
      )
      internal: true
    }
    zoneRedundant: false
  }
}

resource environmentDiagnostics 'Microsoft.Insights/diagnosticSettings@2021-05-01-preview' = {
  name: '${prefix}-environment-logs'
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
    workspaceId: workspace.id
  }
}

output containerEnvironmentName string = containerEnvironment.name
output containerRegistryName string = containerRegistry.name
output containerRegistryServer string = containerRegistry.properties.loginServer
output executionIdentityClientId string = executionIdentity.properties.clientId
output executionIdentityName string = executionIdentity.name
output logAnalyticsWorkspaceName string = workspace.name
output postgresDatabaseName string = tradingDatabase.name
output postgresHost string = postgres.properties.fullyQualifiedDomainName
output postgresServerName string = postgres.name
output profile string = 'DEV_SHADOW_LOW_COST'
