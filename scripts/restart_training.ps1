# Rebuild the training set from dataset\gt\matches_corrected.tsv and retrain CubiCasa from the pretrained weights.
# Progress is shown here and written to logs\restart_training.log
$ErrorActionPreference = "Continue"
Set-Location (Split-Path -Parent $PSScriptRoot)
$env:PYTHONUNBUFFERED = "1"
$py  = ".venv\Scripts\python.exe"
$log = "logs\restart_training.log"
"=== restart training $(Get-Date) ===" | Out-File $log -Encoding utf8

"[1/3] registering the 164 confirmed images to their IFC storeys..." | Tee-Object -FilePath $log -Append
& $py scripts\build_gt_dataset.py manual --matches dataset\gt\matches_corrected.tsv 2>&1 | Tee-Object -FilePath $log -Append
if ($LASTEXITCODE -ne 0) { "STOPPED: build failed" | Tee-Object -FilePath $log -Append; Read-Host "Press Enter"; exit 1 }

"[2/3] copying verified samples into dataset\gt\training..." | Tee-Object -FilePath $log -Append
& $py scripts\build_gt_dataset.py accept --list dataset\gt\manual_accepted.txt 2>&1 | Tee-Object -FilePath $log -Append
if ($LASTEXITCODE -ne 0) { "STOPPED: accept failed" | Tee-Object -FilePath $log -Append; Read-Host "Press Enter"; exit 1 }

"[3/3] training (60 epochs, starting from the pretrained CubiCasa weights)..." | Tee-Object -FilePath $log -Append
& $py scripts\finetune_cubicasa.py --data dataset\gt\training --epochs 60 2>&1 | Tee-Object -FilePath $log -Append
"=== finished $(Get-Date), exit $LASTEXITCODE ===" | Tee-Object -FilePath $log -Append
Read-Host "Done. Press Enter to close"
