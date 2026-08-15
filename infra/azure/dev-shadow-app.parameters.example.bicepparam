using './dev-shadow-app.bicep'

param prefix = 'igtrdevfrc'
param containerImage = 'registry-name.azurecr.io/ig-trader@sha256:0000000000000000000000000000000000000000000000000000000000000000'
param imageCommitSha = '0000000000000000000000000000000000000000'
param containerRegistryName = 'replace-with-dev-shadow-foundation-output'
param containerEnvironmentName = 'replace-with-dev-shadow-foundation-output'
param executionIdentityName = 'replace-with-dev-shadow-foundation-output'
