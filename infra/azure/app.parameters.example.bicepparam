using './app.bicep'

param prefix = 'replacepfx'
param containerImage = 'registry-name.azurecr.io/ig-trader@sha256:0000000000000000000000000000000000000000000000000000000000000000'
param imageCommitSha = '0000000000000000000000000000000000000000'
param containerRegistryName = 'replace-with-foundation-output'
param containerEnvironmentName = 'replace-with-foundation-output'
param executionIdentityName = 'replace-with-foundation-output'
param keyVaultName = 'replace-with-foundation-output'
param enableBrokerSecretReferences = false
