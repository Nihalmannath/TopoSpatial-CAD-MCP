# Round 6: model J = the model-A recipe (same data, seed and settings) plus the junction-heatmap loss.
# Then: tracing evaluation J vs A on the 36-plan manifest, and the junction probe for pretrained / A / J.
$ErrorActionPreference = "Continue"
Set-Location (Split-Path -Parent $PSScriptRoot)
$env:PYTHONUNBUFFERED = "1"
$py  = ".venv\Scripts\python.exe"
New-Item -ItemType Directory -Force logs | Out-Null
$log = "logs\round6.log"
$ck  = "models\checkpoints"
$pre = "models\pretrained\model_best_val_loss_var.pkl"
"=== round 6 $(Get-Date) ===" | Out-File $log -Encoding utf8
"[0/3] setup check (2 tiny steps with the junction loss, saves nothing)..." | Tee-Object -FilePath $log -Append
& $py scripts\finetune_cubicasa.py --smoke --heatmap-weight 1 2>&1 | Tee-Object -FilePath $log -Append
if ($LASTEXITCODE -ne 0) {
  "SETUP CHECK FAILED - nothing was trained." | Tee-Object -FilePath $log -Append
  Read-Host "Send Claude the lines above (logs\round6.log). Press Enter to close"
  exit 1
}
"[1/3] model J: 60 epochs with junction heatmaps (about 90 minutes)..." | Tee-Object -FilePath $log -Append
& $py scripts\finetune_cubicasa.py --data dataset\gt\training --epochs 60 --heatmap-weight 1 --out "$ck\cubicasa_finetuned_J.pth" 2>&1 | Tee-Object -FilePath $log -Append
if (-not (Test-Path "$ck\cubicasa_finetuned_J.pth")) {
  "TRAINING FAILED - no model J was saved." | Tee-Object -FilePath $log -Append
  Read-Host "Send Claude the lines above (logs\round6.log). Press Enter to close"
  exit 1
}
"[2/3] tracing evaluation on the 36-plan manifest: J against A..." | Tee-Object -FilePath $log -Append
& $py scripts\eval_trace.py --manifest dataset\gt\eval_manifest_v2.json --model "J=$ck\cubicasa_finetuned_J.pth" --model "A=$ck\cubicasa_finetuned_A.pth" 2>&1 | Tee-Object -FilePath $log -Append
"[3/3] junction probe: pretrained / A / J..." | Tee-Object -FilePath $log -Append
& $py scripts\junction_probe.py --out dataset\gt\junction_probe_J --maps 0 --full 0 --model "pretrained=$pre" --model "A=$ck\cubicasa_finetuned_A.pth" --model "J=$ck\cubicasa_finetuned_J.pth" 2>&1 | Tee-Object -FilePath $log -Append
"=== finished $(Get-Date) ===" | Tee-Object -FilePath $log -Append
Read-Host "Done. Tell Claude round 6 has finished (no files to send). Press Enter to close"
