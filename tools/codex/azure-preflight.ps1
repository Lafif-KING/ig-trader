$ErrorActionPreference = 'Stop'
$groupName = 'rg-igtrader-dev-frc-001'
$appName = 'igtrdevfrc-execution-worker'

function Invoke-AzJson([string[]]$Arguments) {
    $raw = & az @Arguments 2>$null
    if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace(($raw -join ''))) { return $null }
    try { return ($raw -join "`n" | ConvertFrom-Json) } catch { return $null }
}

$account = Invoke-AzJson @('account', 'show')
$groupExistsRaw = & az group exists --name $groupName 2>$null
$groupExists = if ($LASTEXITCODE -eq 0) { [bool]::Parse(($groupExistsRaw -join '').Trim()) } else { $null }
$app = if ($groupExists) { Invoke-AzJson @('containerapp', 'show', '--name', $appName, '--resource-group', $groupName) } else { $null }
$postgresList = if ($groupExists) { Invoke-AzJson @('postgres', 'flexible-server', 'list', '--resource-group', $groupName) } else { $null }
$postgres = if ($postgresList) { @($postgresList) | Select-Object -First 1 } else { $null }
$properties = if ($app) { $app.properties } else { $null }
$template = if ($properties) { $properties.template } else { $null }
$scale = if ($template) { $template.scale } else { $null }
$containers = if ($template) { @($template.containers) } else { @() }
$secrets = if ($template) { @($template.secrets) } else { @() }
$envVars = @($containers | ForEach-Object { @($_.env) })
$executionMode = ($envVars | Where-Object { $_.name -eq 'EXECUTION_MODE' } | Select-Object -First 1).value
$readyReplicas = $null
if ($properties) {
    if ($properties.runningStatus -eq 'Running') { $readyReplicas = 1 } else { $readyReplicas = 0 }
}

[ordered]@{
    subscription_name = if ($account) { $account.name } else { $null }
    resource_group_exists = $groupExists
    container_app_state = if ($properties) { $properties.runningStatus } else { $null }
    active_revision = if ($properties) { $properties.latestRevisionName } else { $null }
    ready_replicas = $readyReplicas
    min_replicas = if ($scale) { $scale.minReplicas } else { $null }
    max_replicas = if ($scale) { $scale.maxReplicas } else { $null }
    execution_mode = if ($executionMode) { $executionMode } else { 'UNKNOWN' }
    secret_count = $secrets.Count
    postgres_state = if ($postgres) { $postgres.state } else { $null }
} | ConvertTo-Json -Compress
