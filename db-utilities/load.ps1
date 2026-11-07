# Load one CSV / XLSX (or a folder of them) into the database: Postgres AND Cosmos by default.
#
#   .\load.ps1 manifest     C:\data\metadata_R1_B1.csv
#   .\load.ps1 ground-truth C:\data\ground_truth.csv
#   .\load.ps1 manifest     C:\data\file.csv -To postgres          # or -To cosmos
#
# Templates: templates\manifest_template.csv, templates\ground_truth_template.csv.
# Settings: db-utilities\.env. Uses Docker Desktop when it is running, else .venv.
param(
    [Parameter(Mandatory = $true)][ValidateSet("manifest", "ground-truth")][string]$Job,
    [Parameter(Mandatory = $true)][string]$Path,
    [ValidateSet("postgres", "cosmos", "both")][string]$To = "both"
)
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
if (-not (Test-Path $Path)) { throw "not found: $Path" }
if (-not (Test-Path ".env")) { throw "db-utilities\.env is missing: Copy-Item .env.example .env and fill it in" }

$docker = $false
try { docker info *> $null; $docker = ($LASTEXITCODE -eq 0) } catch { $docker = $false }
if ($docker) {
    $name = Split-Path $Path -Leaf
    $target = Join-Path "input" $name
    if (Test-Path $target) { Remove-Item -Recurse -Force $target }
    Copy-Item -Recurse $Path $target
    docker compose run --rm db-utilities $Job --local "/input/$name" --to $To
} else {
    if (-not (Test-Path ".venv\Scripts\python.exe")) {
        py -3.12 -m venv .venv
        .venv\Scripts\pip install -q -r requirements.txt
    }
    .venv\Scripts\python -m dbutil $Job --local $Path --to $To
}
