# Saves model J's raw outputs for the 36 validation plans (about 2 minutes, trains nothing).
$ErrorActionPreference = "Continue"
Set-Location (Split-Path -Parent $PSScriptRoot)
$env:PYTHONUNBUFFERED = "1"
$py  = ".venv\Scripts\python.exe"
New-Item -ItemType Directory -Force logs | Out-Null
$log = "logs\dump_segments.log"
"=== dump segments $(Get-Date) ===" | Out-File $log -Encoding utf8
& $py scripts\dump_segments.py 2>&1 | Tee-Object -FilePath $log -Append
"=== finished $(Get-Date) ===" | Tee-Object -FilePath $log -Append
Read-Host "Done. Tell Claude it has finished (no files to send). Press Enter to close"
