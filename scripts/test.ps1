$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
.\.venv\Scripts\python.exe -m compileall -q avif_gain_studio main.py
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
