targetScope = 'resourceGroup'

@description('Short lowercase workload prefix used in globally unique resource names.')
@minLength(3)
@maxLength(12)
param prefix string

@description('Azure region selected for the IG Trader deployment.')
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
  executionAuthority: 'none'
  managedBy: 'bicep'
  workOrder: 'G4A-01'
}

var compactPrefix = toLower(replace(prefix, '-', ''))
var suffix = take(uniqueString(subscription().id, resourceGroup().id), 8)
var acrName = take('${compactPrefix}${suffix}acr', 50)
var vaultName = take('${compactPrefix}-${suffix}-kv', 24)
var postgresName = take('${compactPrefix}-${suffix}-pg', 63)
var environmentName = '${prefix}-aca-env'
var identityName = '${prefix}-execution-identity'
var workspaceName = '${prefix}-logs'
var virtualNetworkName = '${prefix}-vnet'
var containerSubnetName = 'container-apps'
var postgresSubnetName = 'postgresql'
var privateEndpointSubnetName = 'private-endpoints'
var postgresPrivateDnsName = '${compactPrefix}.postgres.database.azure.com'
var keyVaultPrivateDnsName = 'privatelink.vaultcore.azure.net'
var acrPrivateDnsName = 'privatelink.azurecr.io'
var acrPullRoleId = subscriptionResourceId(
  'Microsoft.Authorization/roleDefinitions',
  '7f951dda-4ed3-4680-a7ca-43fe172d538d'
)
var keyVaultSecretsUserRoleId = subscriptionResourceId(
  'Microsoft.Authorization/roleDefinitions',
  '4633458b-17de-408a-b874-0445c86b69e6'
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
    retentionInDays: 90
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
      {
        name: privateEndpointSubnetName
        properties: {
          addressPrefix: '10.42.3.0/27'
          privateEndpointNetworkPolicies: 'Disabled'
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

resource keyVaultPrivateDns 'Microsoft.Network/privateDnsZones@2024-06-01' = {
  name: keyVaultPrivateDnsName
  location: 'global'
  tags: tags
}

resource keyVaultDnsLink 'Microsoft.Network/privateDnsZones/virtualNetworkLinks@2024-06-01' = {
  parent: keyVaultPrivateDns
  name: '${prefix}-vault-link'
  location: 'global'
  properties: {
    registrationEnabled: false
    virtualNetwork: {
      id: virtualNetwork.id
    }
  }
}

resource acrPrivateDns 'Microsoft.Network/privateDnsZones@2024-06-01' = {
  name: acrPrivateDnsName
  location: 'global'
  tags: tags
}

resource acrDnsLink 'Microsoft.Network/privateDnsZones/virtualNetworkLinks@2024-06-01' = {
  parent: acrPrivateDns
  name: '${prefix}-acr-link'
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

resource keyVault 'Microsoft.KeyVault/vaults@2023-07-01' = {
  name: vaultName
  location: location
  tags: tags
  properties: {
    enablePurgeProtection: true
    enableRbacAuthorization: true
    enableSoftDelete: true
    publicNetworkAccess: 'Disabled'
    sku: {
      family: 'A'
      name: 'standard'
    }
    softDeleteRetentionInDays: 90
    tenantId: tenant().tenantId
  }
}

resource keyVaultPrivateEndpoint 'Microsoft.Network/privateEndpoints@2024-05-01' = {
  name: '${prefix}-vault-pe'
  location: location
  tags: tags
  properties: {
    privateLinkServiceConnections: [
      {
        name: 'vault'
        properties: {
          groupIds: [
            'vault'
          ]
          privateLinkServiceId: keyVault.id
        }
      }
    ]
    subnet: {
      id: resourceId(
        'Microsoft.Network/virtualNetworks/subnets',
        virtualNetwork.name,
        privateEndpointSubnetName
      )
    }
  }
}

resource keyVaultDnsZoneGroup 'Microsoft.Network/privateEndpoints/privateDnsZoneGroups@2024-05-01' = {
  parent: keyVaultPrivateEndpoint
  name: 'default'
  properties: {
    privateDnsZoneConfigs: [
      {
        name: 'vault'
        properties: {
          privateDnsZoneId: keyVaultPrivateDns.id
        }
      }
    ]
  }
}

resource containerRegistry 'Microsoft.ContainerRegistry/registries@2023-07-01' = {
  name: acrName
  location: location
  tags: tags
  sku: {
    name: 'Premium'
  }
  properties: {
    adminUserEnabled: false
    dataEndpointEnabled: false
    publicNetworkAccess: 'Disabled'
    zoneRedundancy: 'Enabled'
  }
}

resource acrPrivateEndpoint 'Microsoft.Network/privateEndpoints@2024-05-01' = {
  name: '${prefix}-acr-pe'
  location: location
  tags: tags
  properties: {
    privateLinkServiceConnections: [
      {
        name: 'registry'
        properties: {
          groupIds: [
            'registry'
          ]
          privateLinkServiceId: containerRegistry.id
        }
      }
    ]
    subnet: {
      id: resourceId(
        'Microsoft.Network/virtualNetworks/subnets',
        virtualNetwork.name,
        privateEndpointSubnetName
      )
    }
  }
}

resource acrDnsZoneGroup 'Microsoft.Network/privateEndpoints/privateDnsZoneGroups@2024-05-01' = {
  parent: acrPrivateEndpoint
  name: 'default'
  properties: {
    privateDnsZoneConfigs: [
      {
        name: 'registry'
        properties: {
          privateDnsZoneId: acrPrivateDns.id
        }
      }
    ]
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

resource keyVaultSecretsAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(keyVault.id, executionIdentity.id, keyVaultSecretsUserRoleId)
  scope: keyVault
  properties: {
    principalId: executionIdentity.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: keyVaultSecretsUserRoleId
  }
}

resource postgres 'Microsoft.DBforPostgreSQL/flexibleServers@2024-08-01' = {
  name: postgresName
  location: location
  tags: tags
  sku: {
    name: 'Standard_D2ds_v5'
    tier: 'GeneralPurpose'
  }
  properties: {
    authConfig: {
      activeDirectoryAuth: 'Enabled'
      passwordAuth: 'Disabled'
      tenantId: tenant().tenantId
    }
    backup: {
      backupRetentionDays: 14
      geoRedundantBackup: 'Disabled'
    }
    highAvailability: {
      mode: 'ZoneRedundant'
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
      storageSizeGB: 128
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
    zoneRedundant: true
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
output keyVaultName string = keyVault.name
output logAnalyticsWorkspaceName string = workspace.name
output postgresDatabaseName string = tradingDatabase.name
output postgresHost string = postgres.properties.fullyQualifiedDomainName
output postgresServerName string = postgres.name
