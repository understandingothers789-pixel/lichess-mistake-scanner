[CmdletBinding()]
param([switch]$WithMaia)

$ErrorActionPreference = 'Stop'
$root = Split-Path $PSScriptRoot -Parent
Push-Location $root
try {
    if (-not (Test-Path '.venv\Scripts\python.exe')) {
        & py -3.12 -m venv .venv
        if ($LASTEXITCODE -ne 0) { throw 'Python 3.12 x64 is required. Install it from python.org.' }
    }

    $python = Join-Path $root '.venv\Scripts\python.exe'
    & $python -c "import struct,sys; raise SystemExit(0 if sys.version_info[:2] == (3,12) and struct.calcsize('P') == 8 else 1)"
    if ($LASTEXITCODE -ne 0) { throw 'The project virtual environment must use Python 3.12 x64.' }
    & $python -m pip install --require-hashes -r scripts/requirements-windows.lock
    if ($LASTEXITCODE -ne 0) { throw 'The locked Windows dependencies could not be installed.' }
    & $python -m pip install --no-deps -e .
    if ($LASTEXITCODE -ne 0) { throw 'Second Look could not be installed.' }

    if ($WithMaia) {
        & $python -m pip install --index-url https://download.pytorch.org/whl/cpu 'torch==2.8.0+cpu'
        if ($LASTEXITCODE -ne 0) { throw 'PyTorch CPU installation failed.' }
        & $python -m pip install '.[maia]'
        if ($LASTEXITCODE -ne 0) { throw 'The pinned official Maia 3 runtime could not be installed. Check Git for Windows and Internet access.' }
    }

    & $python -m mistake_scanner --help
    if ($LASTEXITCODE -ne 0) { throw 'Second Look failed its startup check.' }
    Write-Host 'Launch with .\.venv\Scripts\python.exe -m mistake_scanner'
}
finally {
    Pop-Location
}
