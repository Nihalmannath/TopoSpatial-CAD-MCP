# Round 3 step 2: fine-tune from the pretrained CubiCasa weights on every accepted plan in dataset\gt\training
# (Synaps-labelled brochure plans + the CC__ CubiCasa5K sample). Validation = brochure folders only.
$ErrorActionPreference = "Continue"
Set-Location (Split-Path -Parent $PSScriptRoot)
$env:PYTHONUNBUFFERED = "1"
$py  = ".venv\Scripts\python.exe"
$log = "logs\round3_train.log"
"=== round 3 train $(Get-Date) ===" | Out-File $log -Encoding utf8
if (Test-Path models\checkpoints\cubicasa_finetuned.pth) {
  Copy-Item models\checkpoints\cubicasa_finetuned.pth models\checkpoints\cubicasa_finetuned_before_round3.pth -Force
  "kept previous model as cubicasa_finetuned_before_round3.pth" | Tee-Object -FilePath $log -Append
}
"[1/2] training (60 epochs from the pretrained weights)..." | Tee-Object -FilePath $log -Append
& $py scripts\finetune_cubicasa.py --data dataset\gt\training --epochs 60 2>&1 | Tee-Object -FilePath $log -Append
"[2/2] scoring all models on the held-out plans..." | Tee-Object -FilePath $log -Append
& $py scripts\eval_trace.py 2>&1 | Tee-Object -FilePath $log -Append
"=== finished $(Get-Date) ===" | Tee-Object -FilePath $log -Append
Read-Host "Done. Press Enter to close"
