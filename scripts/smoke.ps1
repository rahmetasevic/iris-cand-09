<#
.SYNOPSIS
    Documented smoke path for a clean Docker host (Windows PowerShell 5.1+ / PowerShell 7).
.EXAMPLE
    .\scripts\smoke.ps1
    .\scripts\smoke.ps1 -EnvFile deploy\second-host.env
#>
[CmdletBinding()]
param(
    [string]$EnvFile = ".env",
    [int]$WaitTimeout = 180
)

$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..")

function Write-Step([string]$Message) { Write-Host "[smoke] $Message" }

function Invoke-Compose {
    & docker compose --env-file $EnvFile @args
    if ($LASTEXITCODE -ne 0) { throw "docker compose $($args -join ' ') failed with exit code $LASTEXITCODE" }
}

if (-not (Get-Command docker -ErrorAction SilentlyContinue)) { throw "docker not found on PATH" }
# Windows PowerShell turns redirected native stderr into errors; relax only here.
$ErrorActionPreference = "Continue"
& docker info *> $null
$daemonOk = $LASTEXITCODE -eq 0
$ErrorActionPreference = "Stop"
if (-not $daemonOk) { throw "cannot talk to the Docker daemon (is Docker Desktop running?)" }
$composeVersion = & docker compose version
if ($LASTEXITCODE -ne 0) { throw "the Docker Compose v2 plugin is required ('docker compose')" }
Write-Step "using $composeVersion"

if (-not (Test-Path $EnvFile)) {
    if ($EnvFile -ne ".env") { throw "env file not found: $EnvFile" }
    Copy-Item "deploy\dev.env.example" ".env"
    Write-Step "created .env from deploy\dev.env.example"
}

$secretFile = "./secrets/db_password"
foreach ($line in Get-Content $EnvFile) {
    if ($line -match '^\s*IRIS_DB_SECRET_FILE=(.*)$') { $secretFile = $Matches[1].Trim().Trim('"', "'") }
}
$secretPath = [System.IO.Path]::GetFullPath((Join-Path (Get-Location) $secretFile))
if (-not (Test-Path $secretPath) -or (Get-Item $secretPath).Length -eq 0) {
    New-Item -ItemType Directory -Force (Split-Path $secretPath) | Out-Null
    $bytes = New-Object byte[] 48
    [System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
    $password = ([Convert]::ToBase64String($bytes) -replace '[^A-Za-z0-9]', '').Substring(0, 40)
    # No BOM and no trailing newline.
    [System.IO.File]::WriteAllText($secretPath, $password)
    Write-Step "generated a random database password in $secretFile"
}

Write-Step "validating compose configuration"
Invoke-Compose config --quiet

Write-Step "building images"
Invoke-Compose build

$services = @("db")
$active = & docker compose --env-file $EnvFile config --services
if ($active -contains "source") { $services += "source" }
Write-Step "starting: $($services -join ' ') (waiting for healthy, up to ${WaitTimeout}s)"
Invoke-Compose up --detach --wait --wait-timeout $WaitTimeout @services

Write-Step "applying migrations"
Invoke-Compose run --rm --no-deps migrate

Write-Step "running worker smoke checks"
Invoke-Compose run --rm --no-deps worker smoke

Write-Step "OK - stack is running; stop it with: docker compose --env-file $EnvFile down"
