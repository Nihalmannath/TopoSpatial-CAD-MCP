# Round 7: model J with room closure, scored on the 36-plan manifest (about 3 minutes, trains nothing).
# Compare with eval 20261110_1203: A rooms F1 0.599, J without closure 0.609.
$ErrorActionPreference = "Continue"
Set-Location (Split-Path -Parent $PSScriptRoot)
$env:PYTHONUNBUFFERED = "1"
$py  = ".venv\Scripts\python.exe"
New-Item -ItemType Directory -Force logs | Out-Null
$log = "logs\round7.log"
$ck  = "models\checkpoints"
"=== round 7 $(Get-Date) ===" | Out-File $log -Encoding utf8
"[1/2] unit tests for the room closure..." | Tee-Object -FilePath $log -Append
& $py -m pytest -q tests\test_room_closure.py tests\test_junction_heatmaps.py tests\test_trace_training.py 2>&1 | Tee-Object -FilePath $log -Append
"[2/2] tracing evaluation: J with room closure..." | Tee-Object -FilePath $log -Append
& $py scripts\eval_trace.py --manifest dataset\gt\eval_manifest_v2.json --no-defaults --close-rooms --stages canonical --model "J+close=$ck\cubicasa_finetuned_J.pth" 2>&1 | Tee-Object -FilePath $log -Append
"=== finished $(Get-Date) ===" | Tee-Object -FilePath $log -Append
Read-Host "Done. Tell Claude round 7 has finished (no files to send). Press Enter to close"
