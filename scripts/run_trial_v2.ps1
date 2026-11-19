# Trial v2: relabel walls from the image -> short fine-tune from the ORIGINAL weights -> geometry eval.
# Nothing existing is overwritten: new labels go to dataset\gt\training_v2, the checkpoint gets its own name.
param([string]$Tag = "trial_v2_lr3e5_e10", [int]$Epochs = 10, [string]$Lr = "3e-5", [switch]$Force)
$ErrorActionPreference = "Continue"
Set-Location (Split-Path -Parent $PSScriptRoot)
$env:PYTHONUNBUFFERED = "1"
$py = ".venv\Scripts\python.exe"
New-Item -ItemType Directory -Force logs | Out-Null
$log = "logs\$Tag.log"
"=== $Tag $(Get-Date) ===" | Out-File $log -Encoding utf8

$have = if (Test-Path dataset\gt\training_v2) { (Get-ChildItem dataset\gt\training_v2 -Directory).Count } else { 0 }
$want = (Get-ChildItem dataset\gt\training -Directory).Count
if ($Force -or $have -lt $want) {
    "[1/3] relabelling walls ($want plans)..." | Tee-Object -FilePath $log -Append
    & $py scripts\relabel_walls.py 2>&1 | Tee-Object -FilePath $log -Append
} else { "[1/3] training_v2 already has $have plans - skipping (use -Force to redo)" | Tee-Object -FilePath $log -Append }

$out = "models\checkpoints\$Tag.pth"
if (Test-Path $out) { "ERROR: $out exists; pick a new -Tag" | Tee-Object -FilePath $log -Append; exit 2 }
"[2/3] fine-tuning from the original weights: $Epochs epochs, lr $Lr -> $out" | Tee-Object -FilePath $log -Append
& $py scripts\finetune_cubicasa.py --data dataset\gt\training_v2 --epochs $Epochs --lr $Lr --out $out 2>&1 | Tee-Object -FilePath $log -Append

"[3/3] geometry eval on the frozen val plans (pretrained vs $Tag, wall bias 0 and 2)..." | Tee-Object -FilePath $log -Append
& $py scripts\eval_trace.py --no-defaults --model "pretrained=models\pretrained\model_best_val_loss_var.pkl" --model "$Tag=$out" --wall-bias 0,2 --stages canonical 2>&1 | Tee-Object -FilePath $log -Append
"=== finished $(Get-Date) ===" | Tee-Object -FilePath $log -Append
