[CmdletBinding()]
param([switch]$SkipTests, [switch]$WithMaia)

$ErrorActionPreference = 'Stop'
$root = Split-Path $PSScriptRoot -Parent
Push-Location $root
try {
    $python = Join-Path $root '.venv\Scripts\python.exe'
    if (-not (Test-Path $python)) { throw 'Run scripts\setup-windows.ps1 first.' }
    if (-not $SkipTests) {
        & $python -m pytest -q
        if ($LASTEXITCODE -ne 0) { throw 'Tests failed.' }
    }

    $version = & $python -c "from mistake_scanner import __version__; print(__version__)"
    if ($LASTEXITCODE -ne 0 -or $version -notmatch '^\d+\.\d+\.\d+$') { throw 'Could not determine the package version.' }
    $options = @('--noconfirm', '--clean', '--windowed', '--onedir', '--name', 'SecondLook', '--paths', 'src', '--collect-data', 'mistake_scanner')
    if ($WithMaia) {
        & $python -c "import maia3,torch"
        if ($LASTEXITCODE -ne 0) { throw 'Run setup-windows.ps1 -WithMaia before building with Maia.' }
        $options += @('--collect-submodules', 'maia3', '--copy-metadata', 'maia3')
    }
    else {
        $options += @('--exclude-module', 'torch', '--exclude-module', 'maia3')
    }
    & $python -m PyInstaller @options src/mistake_scanner/__main__.py
    if ($LASTEXITCODE -ne 0) { throw 'PyInstaller build failed.' }

    Copy-Item README.md, THIRD_PARTY.md -Destination dist/SecondLook
    Copy-Item scripts/requirements-windows.lock -Destination dist/SecondLook
    & $python scripts/bundle-notices.py dist/SecondLook
    if ($LASTEXITCODE -ne 0) { throw 'Dependency notices could not be bundled.' }
    $folder = "dist/SecondLook-v$version"
    if (Test-Path $folder) { Remove-Item $folder -Recurse -Force }
    Rename-Item -LiteralPath dist/SecondLook -NewName "SecondLook-v$version"
    Compress-Archive -Path $folder -DestinationPath dist/SecondLook-windows-x64.zip -Force
    Get-FileHash dist/SecondLook-windows-x64.zip -Algorithm SHA256
}
finally {
    Pop-Location
}
