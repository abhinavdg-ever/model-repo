# Set up BERT training on a Windows PC. Run from this folder in PowerShell:
#
#   .\setup_pc.ps1                 # auto: CUDA PyTorch when an NVIDIA GPU is found, else CPU
#   .\setup_pc.ps1 -Cuda cu126     # force a CUDA build (cu118, cu121, cu124, cu126, cu128, ...)
#   .\setup_pc.ps1 -Cuda cpu       # force the CPU build
#   .\setup_pc.ps1 -Notebook       # also install Jupyter, to run pc.ipynb
#
# If PowerShell refuses to run it: Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
param(
    [string]$Cuda = "auto",
    [switch]$Notebook
)
$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

Write-Host "== Python venv (.venv, Python 3.12)"
if (-not (Test-Path ".venv\Scripts\python.exe")) {
    py -3.12 -m venv .venv
    if ($LASTEXITCODE -ne 0) { throw "Python 3.12 not found. Install it: winget install Python.Python.3.12" }
}
$py = ".venv\Scripts\python.exe"
& $py -m pip install --upgrade pip | Out-Null

if ($Cuda -eq "auto") {
    $smi = Get-Command nvidia-smi -ErrorAction SilentlyContinue
    if ($smi) {
        # The driver's highest supported CUDA version, e.g. "CUDA Version: 12.6".
        $line = (& nvidia-smi) | Select-String -Pattern "CUDA Version:\s*([0-9]+)\.([0-9]+)" | Select-Object -First 1
        $major = [int]$line.Matches[0].Groups[1].Value
        $minor = [int]$line.Matches[0].Groups[2].Value
        if ($major -gt 12 -or ($major -eq 12 -and $minor -ge 8)) { $Cuda = "cu128" }
        elseif ($major -eq 12 -and $minor -ge 6) { $Cuda = "cu126" }
        elseif ($major -eq 12 -and $minor -ge 4) { $Cuda = "cu124" }
        elseif ($major -eq 12) { $Cuda = "cu121" }
        else { $Cuda = "cu118" }
        Write-Host "== NVIDIA GPU found (driver supports CUDA $major.$minor): PyTorch $Cuda"
    } else {
        $Cuda = "cpu"
        Write-Host "== No NVIDIA GPU found (nvidia-smi missing): PyTorch CPU build"
    }
}

Write-Host "== PyTorch ($Cuda)"
& $py -m pip install torch --index-url "https://download.pytorch.org/whl/$Cuda"
if ($LASTEXITCODE -ne 0) {
    throw "PyTorch install failed for '$Cuda'. Pick a build on https://pytorch.org/get-started/locally/ and rerun with -Cuda <that cuXXX>."
}

Write-Host "== The rest (requirements.txt)"
& $py -m pip install -r requirements.txt
if ($Notebook) { & $py -m pip install notebook ipykernel ipywidgets }

Write-Host "== Check"
& $py -c "import torch, transformers; print('torch', torch.__version__, '| transformers', transformers.__version__); print('GPU:', torch.cuda.get_device_name(0) if torch.cuda.is_available() else 'none - training will use the CPU')"
Write-Host ""
Write-Host "Done. Next:"
Write-Host "  .venv\Scripts\Activate.ps1"
Write-Host "  python train.py --check        # 2 steps + minutes-per-epoch estimate"
Write-Host "  python train.py                # a new run: 4 epochs"
if ($Notebook) { Write-Host "  jupyter notebook pc.ipynb      # or open pc.ipynb in VS Code" }
