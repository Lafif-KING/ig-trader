param(
    [Parameter(Mandatory = $true)]
    [ValidateSet('db', 'cloud', 'shadow', 'strategy', 'full')]
    [string]$Area
)

$ErrorActionPreference = 'Stop'
$commands = @{
    db       = @('tests/test_execution_lease.py', 'tests/test_g4b_db_bootstrap.py')
    cloud    = @('tests/test_cloud_runtime.py', 'tests/test_cloud_foundation_contracts.py')
    strategy = @('tests/test_strategies.py', 'tests/test_risk.py')
    full     = @()
}

if ($Area -eq 'shadow') {
    Write-Output 'TEST_AREA_NOT_IMPLEMENTED'
    exit 2
}

if ($Area -eq 'full') {
    Write-Output 'poetry run pytest -q'
    & poetry run pytest -q
    exit $LASTEXITCODE
}

$paths = $commands[$Area]
Write-Output ("Running {0}: {1}" -f $Area, ($paths -join ', '))
& poetry run pytest -q -- $paths
exit $LASTEXITCODE
