param(
    [Parameter(Mandatory = $true)]
    [string]$ResourceGroup,
    [Parameter(Mandatory = $true)]
    [string]$BootstrapImage,
    [Parameter(Mandatory = $true)]
    [string]$BootstrapIdentityPrincipalId,
    [switch]$ValidateOnly
)

$ErrorActionPreference = 'Stop'

& poetry run python tools/validate_immutable_image.py $BootstrapImage
if ($LASTEXITCODE -ne 0) {
    throw 'Immutable bootstrap image validation failed before deployment.'
}

if ($ValidateOnly) {
    return
}

& az deployment group create `
    --resource-group $ResourceGroup `
    --parameters infra/azure/dev-shadow-db-bootstrap.parameters.bicepparam `
    --parameters bootstrapImage=$BootstrapImage `
    --parameters bootstrapIdentityPrincipalId=$BootstrapIdentityPrincipalId
if ($LASTEXITCODE -ne 0) {
    throw 'Database bootstrap deployment failed.'
}
