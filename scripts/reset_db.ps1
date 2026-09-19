# Clear the public schema and re-apply the latest schema\v1.sql + schema\v2.sql.
# Destructive — all chart/OCR/member/DOS rows are wiped.
#
# Usage:
#   .\scripts\reset_db.ps1
#   .\scripts\reset_db.ps1 -Yes
#   $env:DATABASE_URL = "postgresql://…"; .\scripts\reset_db.ps1 -Yes
#
# Applies, in order:
#   schema\clear_schema.sql
#   schema\v1.sql
#   schema\v2.sql
#
# DATABASE_URL is read from the environment, else core-pipeline\.env.
# Accepts postgresql:// or postgresql+psycopg://.

[CmdletBinding()]
param(
    [switch]$Yes
)

$ErrorActionPreference = "Stop"
$Root = Resolve-Path (Join-Path $PSScriptRoot "..")

function Get-DatabaseUrl {
    if ($env:DATABASE_URL -and $env:DATABASE_URL.Trim()) {
        return $env:DATABASE_URL.Trim()
    }
    $envFile = Join-Path $Root "core-pipeline\.env"
    if (-not (Test-Path $envFile)) {
        throw "DATABASE_URL is not set. Export it or put it in core-pipeline\.env"
    }
    $line = Get-Content $envFile | Where-Object { $_ -match '^\s*DATABASE_URL=' } | Select-Object -Last 1
    if (-not $line) {
        throw "DATABASE_URL not found in core-pipeline\.env"
    }
    $value = ($line -split '=', 2)[1].Trim().Trim('"').Trim("'")
    if (-not $value) { throw "DATABASE_URL is empty in core-pipeline\.env" }
    return $value
}

if (-not (Get-Command psql -ErrorAction SilentlyContinue)) {
    throw "psql not found on PATH"
}

$raw = (Get-DatabaseUrl) -replace '^postgresql\+psycopg://', 'postgresql://'
$uri = [Uri]$raw
$dbName = $uri.AbsolutePath.Trim('/').Split('/')[0]
if (-not $dbName) { $dbName = "imaging_outputs" }

$builder = New-Object System.UriBuilder($uri)
$builder.Path = "/$dbName"
$targetUrl = $builder.Uri.AbsoluteUri

Write-Host "Will CLEAR public schema in database: $dbName"
Write-Host "  then apply clear_schema.sql -> v1.sql -> v2.sql"
if (-not $Yes) {
    $confirm = Read-Host "Type the database name to confirm"
    if ($confirm -ne $dbName) {
        Write-Error "Aborted."
        exit 1
    }
}

Write-Host "-> schema/clear_schema.sql"
psql $targetUrl -v ON_ERROR_STOP=1 -f (Join-Path $Root "schema\clear_schema.sql") | Out-Null

Write-Host "-> schema/v1.sql"
psql $targetUrl -v ON_ERROR_STOP=1 -f (Join-Path $Root "schema\v1.sql") | Out-Null

Write-Host "-> schema/v2.sql"
psql $targetUrl -v ON_ERROR_STOP=1 -f (Join-Path $Root "schema\v2.sql") | Out-Null

Write-Host "-> verify pipeline_stage"
psql $targetUrl -v ON_ERROR_STOP=1 -c "SELECT count(*) AS stages FROM pipeline_stage;"

Write-Host "Done. $dbName cleared and reloaded from latest v1 + v2."
