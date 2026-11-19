# Round 5: wall labels = centre band only (Synaps wall thickness ignored). Run AFTER round 4 has finished (same GPU).
$ErrorActionPreference = "Continue"
Set-Location (Split-Path -Parent $PSScriptRoot)
$env:PYTHONUNBUFFERED = "1"
$py  = ".venv\Scripts\python.exe"
$log = "logs\round5.log"
$ck  = "models\checkpoints"
"=== round 5 $(Get-Date) ===" | Out-File $log -Encoding utf8
"[0] checking training_v3 (core-wall labels)..." | Tee-Object -FilePath $log -Append
& $py scripts\core_wall_labels.py 2>&1 | Tee-Object -FilePath $log -Append
"[1/2] model C: core-wall labels, 40 epochs, thin-wall weight 1 (thickness is no longer a target)..." | Tee-Object -FilePath $log -Append
& $py scripts\finetune_cubicasa.py --data dataset\gt\training_v3 --epochs 40 --thin-wall-weight 1 --out "$ck\cubicasa_finetuned_C.pth" 2>&1 | Tee-Object -FilePath $log -Append
"[2/2] tracing evaluation on the 36-plan manifest: C against the other models (ignore the width-error column)..." | Tee-Object -FilePath $log -Append
& $py scripts\eval_trace.py --manifest dataset\gt\eval_manifest_v2.json --model "C=$ck\cubicasa_finetuned_C.pth" --model "A=$ck\cubicasa_finetuned_A.pth" --model "B=$ck\cubicasa_finetuned_B.pth" 2>&1 | Tee-Object -FilePath $log -Append
"=== finished $(Get-Date) ===" | Tee-Object -FilePath $log -Append
Read-Host "Done. Send Claude the training log (logs\round5.log) and the new index.html. Press Enter to close"
