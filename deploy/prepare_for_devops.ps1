<#
Copy a freshly downloaded GitHub zip into the Azure DevOps working folder, leaving out what
the VM does not need (training, scripts, tests). Run on the Windows laptop. No git needed.

  powershell -ExecutionPolicy Bypass -File deploy\prepare_for_devops.ps1 `
      -Source "$env:USERPROFILE\Downloads\model-repo-main" `
      -Target "C:\work\advantmed-devops"

Source  the folder extracted from GitHub's "Download ZIP".
Target  the folder you push to Azure DevOps. Its .env files, .git, models and logs stay.
#>
param(
    [Parameter(Mandatory = $true)][string]$Source,
    [Parameter(Mandatory = $true)][string]$Target
)
$ErrorActionPreference = "Stop"

# A zip from GitHub has one top folder (model-repo-main); accept either level.
if (-not (Test-Path (Join-Path $Source "core-pipeline"))) {
    $inner = Get-ChildItem $Source -Directory | Where-Object { Test-Path (Join-Path $_.FullName "core-pipeline") } | Select-Object -First 1
    if (-not $inner) { throw "No core-pipeline folder under $Source" }
    $Source = $inner.FullName
}
New-Item -ItemType Directory -Force -Path $Target | Out-Null

# Not copied, and (robocopy /XD, /XF) never purged from the target: development-only
# folders, tool folders, and per-host data (weights copied by hand, logs, chart output).
$skipDirs = @("training", "scripts", "tests", "deploy", ".venv-test", ".git", ".venv", "node_modules",
              (Join-Path $Source "core-pipeline\models"), (Join-Path $Target "core-pipeline\models"),
              (Join-Path $Source "core-pipeline\logs"),   (Join-Path $Target "core-pipeline\logs"),
              (Join-Path $Source "core-pipeline\outputs"), (Join-Path $Target "core-pipeline\outputs"),
              (Join-Path $Source "review-ui\data"),       (Join-Path $Target "review-ui\data"))
$skipFiles = @(".env", "CLAUDE.md", "PLAN.md", "._*", ".DS_Store")

robocopy $Source $Target /MIR /NFL /NDL /NJH /NP /XD $skipDirs /XF $skipFiles | Out-Null
if ($LASTEXITCODE -ge 8) { throw "robocopy failed ($LASTEXITCODE)" }

# The small model files kept in git (joblib heads, heads.pt, hw pkl, READMEs): copied in,
# never purging the big weights already in the target's models folder.
robocopy (Join-Path $Source "core-pipeline\models") (Join-Path $Target "core-pipeline\models") /E /NFL /NDL /NJH /NP /XF "._*" | Out-Null
if ($LASTEXITCODE -ge 8) { throw "robocopy (models) failed ($LASTEXITCODE)" }
foreach ($keep in @("review-ui\data", "core-pipeline\logs")) {
    $src = Join-Path $Source $keep
    if (Test-Path $src) {
        robocopy $src (Join-Path $Target $keep) /E /NFL /NDL /NJH /NP /XF "._*" | Out-Null
        if ($LASTEXITCODE -ge 8) { throw "robocopy ($keep) failed ($LASTEXITCODE)" }
    }
}

# Folders a previous copy may have left behind.
foreach ($dir in @("training", "scripts", "tests", "deploy")) {
    $path = Join-Path $Target $dir
    if (Test-Path $path) { Remove-Item -Recurse -Force $path }
}

Copy-Item (Join-Path $Source "deploy\devops.gitignore") (Join-Path $Target ".gitignore") -Force

Write-Host "Ready: $Target"
Write-Host "Left out: training, scripts, tests, deploy. Kept in the target: .env files, model weights, logs, chart data."
Write-Host "Next: push $Target to Azure DevOps as usual, then deploy on the VM (deploy\README.md)."
