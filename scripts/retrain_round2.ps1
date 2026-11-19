# Round 2: add folder 13 (new IFC) + the weak overlays ticked in dataset\gt\weak_review.html, then retrain from pretrained.
$ErrorActionPreference = "Continue"
Set-Location (Split-Path -Parent $PSScriptRoot)
$env:PYTHONUNBUFFERED = "1"
$py  = ".venv\Scripts\python.exe"
$log = "logs\retrain_round2.log"
"=== round 2 $(Get-Date) ===" | Out-File $log -Encoding utf8

$weak = Get-ChildItem "dataset\gt\weak_accepted*.txt", "$env:USERPROFILE\Downloads\weak_accepted*.txt" -ErrorAction SilentlyContinue | Sort-Object LastWriteTime -Descending | Select-Object -First 1
if ($weak) { "weak list: $($weak.FullName) ($((Get-Content $weak.FullName | Where-Object {$_.Trim()}).Count) ticked)" | Tee-Object -FilePath $log -Append }
else { "no weak_accepted.txt found - adding folder 13 only" | Tee-Object -FilePath $log -Append }

"[1/4] registering folder 13 images to the new IFC..." | Tee-Object -FilePath $log -Append
& $py scripts\build_gt_dataset.py manual --matches dataset\gt\matches_folder13.tsv 2>&1 | Tee-Object -FilePath $log -Append

"[2/4] adding folder 13 + ticked weak samples to dataset\gt\training..." | Tee-Object -FilePath $log -Append
& $py scripts\build_gt_dataset.py accept --list dataset\gt\manual_accepted.txt --val-groups 1_FIRST_6 6_SIXTH_15 2>&1 | Tee-Object -FilePath $log -Append
if ($weak) { & $py scripts\build_gt_dataset.py accept --list $weak.FullName --val-groups 1_FIRST_6 6_SIXTH_15 2>&1 | Tee-Object -FilePath $log -Append }

"[3/4] keeping round-1 model as cubicasa_finetuned_round1.pth" | Tee-Object -FilePath $log -Append
if (Test-Path models\checkpoints\cubicasa_finetuned.pth) { Move-Item -Force models\checkpoints\cubicasa_finetuned.pth models\checkpoints\cubicasa_finetuned_round1.pth }

"[4/4] training (60 epochs from the pretrained weights)..." | Tee-Object -FilePath $log -Append
& $py scripts\finetune_cubicasa.py --data dataset\gt\training --epochs 60 2>&1 | Tee-Object -FilePath $log -Append
"[5/5] scoring all models on the held-out plans (scripts\eval_trace.py)..." | Tee-Object -FilePath $log -Append
& $py scripts\eval_trace.py 2>&1 | Tee-Object -FilePath $log -Append
"=== finished $(Get-Date), exit $LASTEXITCODE ===" | Tee-Object -FilePath $log -Append
Read-Host "Done. Press Enter to close"
