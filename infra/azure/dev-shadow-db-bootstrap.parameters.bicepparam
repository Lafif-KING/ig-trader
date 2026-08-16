using './dev-shadow-db-bootstrap.bicep'

param location = 'francecentral'
param containerEnvironmentName = 'igtrdevfrc-aca-env'
param containerRegistryName = 'igtrdevfrcbzkxc6c6acr'
param postgresServerName = 'igtrdevfrc-bzkxc6c6-pg'
param runtimeIdentityName = 'igtrdevfrc-execution-identity'
param bootstrapIdentityName = 'igtrdevfrc-db-bootstrap-identity'
// What-If-only placeholder. An approved future deployment must first run the
// identity-only phase and then supply that UAMI's exact generated principalId.
param bootstrapIdentityPrincipalId = '00000000-0000-0000-0000-000000000000'
param bootstrapIdentityOnly = false
param bootstrapJobName = 'igtrdevfrc-db-bootstrap'
param runtimeProbeJobName = 'igtrdevfrc-runtime-db-probe'

// What-If-only placeholder. Future deployment approval must replace this with
// the reviewed ACR image digest produced by the separately approved publish step.
param bootstrapImage = 'igtrdevfrcbzkxc6c6acr.azurecr.io/ig-trader-db-bootstrap@sha256:0000000000000000000000000000000000000000000000000000000000000000'
