# Round 4: duplicate twins removed from training (6 moved to dataset\gt\training_excluded_twins_2026-11-04).
# A = same settings as round 3 on clean data.  B = same data, thin-wall weight 6 (round 3 drew walls too thick).
# Both are scored on the old frozen 16-plan manifest and on a new 36-plan manifest (all val plans).
$ErrorActionPreference = "Continue"
Set-Location (Split-Path -Parent $PSScriptRoot)
$env:PYTHONUNBUFFERED = "1"
$py  = ".venv\Scripts\python.exe"
$log = "logs\round4.log"
"=== round 4 $(Get-Date) ===" | Out-File $log -Encoding utf8
$ck = "models\checkpoints"
if (Test-Path "$ck\cubicasa_finetuned.pth") { Copy-Item "$ck\cubicasa_finetuned.pth" "$ck\cubicasa_finetuned_round3.pth" -Force }
if (-not (Test-Path dataset\gt\eval_manifest_v2.json)) {
  "[0] freezing the 36-plan validation manifest (v2)..." | Tee-Object -FilePath $log -Append
  & $py scripts\eval_trace.py --freeze-manifest --manifest dataset\gt\eval_manifest_v2.json 2>&1 | Tee-Object -FilePath $log -Append
}
"[1/4] model A: round-3 settings on clean data..." | Tee-Object -FilePath $log -Append
& $py scripts\finetune_cubicasa.py --data dataset\gt\training --epochs 60 --out "$ck\cubicasa_finetuned_A.pth" 2>&1 | Tee-Object -FilePath $log -Append
"[2/4] model B: thin-wall weight 6..." | Tee-Object -FilePath $log -Append
& $py scripts\finetune_cubicasa.py --data dataset\gt\training --epochs 60 --thin-wall-weight 6 --out "$ck\cubicasa_finetuned_B.pth" 2>&1 | Tee-Object -FilePath $log -Append
"[3/4] scoring on the frozen 16-plan manifest..." | Tee-Object -FilePath $log -Append
& $py scripts\eval_trace.py --model "A=$ck\cubicasa_finetuned_A.pth" --model "B=$ck\cubicasa_finetuned_B.pth" --model "round3=$ck\cubicasa_finetuned_round3.pth" 2>&1 | Tee-Object -FilePath $log -Append
"[4/4] scoring on the 36-plan manifest..." | Tee-Object -FilePath $log -Append
& $py scripts\eval_trace.py --manifest dataset\gt\eval_manifest_v2.json --model "A=$ck\cubicasa_finetuned_A.pth" --model "B=$ck\cubicasa_finetuned_B.pth" --model "round3=$ck\cubicasa_finetuned_round3.pth" 2>&1 | Tee-Object -FilePath $log -Append
"=== finished $(Get-Date) ===" | Tee-Object -FilePath $log -Append
Read-Host "Done. Send Claude the two evaluation reports. Press Enter to close"
