# Junction probe: measures the model's junction outputs on the 36 validation plans. Trains nothing (about 2 minutes on the GPU).
$ErrorActionPreference = "Continue"
Set-Location (Split-Path -Parent $PSScriptRoot)
$env:PYTHONUNBUFFERED = "1"
$py  = ".venv\Scripts\python.exe"
New-Item -ItemType Directory -Force logs | Out-Null
$log = "logs\junction_probe.log"
"=== junction probe $(Get-Date) ===" | Out-File $log -Encoding utf8
& $py scripts\junction_probe.py 2>&1 | Tee-Object -FilePath $log -Append
"=== finished $(Get-Date) ===" | Tee-Object -FilePath $log -Append
Read-Host "Done. Tell Claude it has finished (no files to send). Press Enter to close"
