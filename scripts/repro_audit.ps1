# Reproduce the training-audit collapse under fixed settings: 3 plans x 2 checkpoints x bias 0 / +2.
# Every run writes masks, stage panels and run.json (checkpoint hash, effective bias, counts per stage)
# to scratch\repro_audit\<plan>_<model>_b<bias>\ ; the table goes to scratch\repro_audit\summary.txt.
#
#   powershell -ExecutionPolicy Bypass -File scripts\repro_audit.ps1
param([string]$Out = "scratch\repro_audit")

$ErrorActionPreference = "Continue"
Set-Location (Split-Path $PSScriptRoot -Parent)
$py = ".venv\Scripts\python.exe"
$env:PYTHONUNBUFFERED = "1"
New-Item -ItemType Directory -Force -Path $Out, "logs" | Out-Null

# image, mm per px (audit used 1 / px_per_mm from meta.json), short name
$plans = @(
    @("dataset\gt\training\6_SIXTH_15__1jcaQSM_Nk8_rdBASTD8Nw3PMdjz7v62__Building_frame_3\image.png", 10.672, "1jca"),
    @("dataset\gt\training\1_FIRST_6__1700J5p19WSHql_63Yd5g_anJ7QpOJ9Ef__Building_frame_0\image.png", 10.373, "1700"),
    @("scratch\ground_floor_plan.jpg", 12.2, "gfp1")
)
$models = @(
    @("models\pretrained\model_best_val_loss_var.pkl", "pre"),
    @("models\checkpoints\cubicasa_finetuned.pth", "ft")
)
$biases = @(0, 2)

foreach ($p in $plans) {
    foreach ($m in $models) {
        foreach ($b in $biases) {
            $dir = Join-Path $Out ("{0}_{1}_b{2}" -f $p[2], $m[1], $b)
            Write-Host ("=== {0}  {1}  bias {2}" -f $p[2], $m[1], $b)
            & $py scripts\stage_trace.py $p[0] --engine neural --mm-per-px $p[1] --no-ocr `
                --weights $m[0] --bias $b --out $dir 2>&1 | Select-Object -First 3
        }
    }
}
& $py scripts\stage_summary.py $Out
Write-Host "`nDone. Tell Claude it finished."
