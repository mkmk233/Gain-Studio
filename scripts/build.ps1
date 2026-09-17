$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
if (-not (Test-Path '.venv\Scripts\python.exe')) {
    python -m venv .venv
}
$python = (Resolve-Path '.venv\Scripts\python.exe').Path
& $python -m pip install -r requirements.txt pyinstaller

# PyInstaller resolves native DLLs using PATH. Keep unrelated SDKs and Codex
# runtimes out of the search path so a third-party ICU cannot shadow Windows ICU.
$originalPath = $env:PATH
$pythonHome = & $python -c "import sys; print(sys.base_prefix)"
try {
    $env:PATH = @(
        (Join-Path $root '.venv\Scripts')
        $pythonHome
        (Join-Path $env:SystemRoot 'System32')
        $env:SystemRoot
    ) -join ';'
    & $python -m PyInstaller --noconfirm --clean avif_gain_studio.spec
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller 构建失败，退出码：$LASTEXITCODE" }
}
finally {
    $env:PATH = $originalPath
}

# Do not publish a package merely because the process stays alive: a Qt import
# error dialog also stays alive. Require the actual visible main window.
& $python scripts\smoke_packaged.py
if ($LASTEXITCODE -ne 0) { throw "打包程序 GUI 冒烟测试失败，退出码：$LASTEXITCODE" }

$zip = Join-Path $root 'dist\Gain-Studio-Windows-x64.zip'
if (Test-Path $zip) { Remove-Item -LiteralPath $zip -Force }
Compress-Archive -Path 'dist\Gain Studio\*' -DestinationPath $zip -CompressionLevel Optimal
Write-Host "Built and smoke-tested: $zip"

