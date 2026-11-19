# Round 3 step 1: align the retraced plans again (edge fallback) and the 30 new Synaps/CubiCasa plans.
# Writes candidates only. Nothing is added to training here.
$ErrorActionPreference = "Continue"
Set-Location (Split-Path -Parent $PSScriptRoot)
$env:PYTHONUNBUFFERED = "1"
$py  = ".venv\Scripts\python.exe"
$log = "logs\round3_prepare.log"
"=== round 3 prepare $(Get-Date) ===" | Out-File $log -Encoding utf8
"[1/2] retraced plans (54 pairs, edge fallback)..." | Tee-Object -FilePath $log -Append
& $py scripts\build_gt_dataset.py manual --matches dataset\gt\matches_2026-11-03.tsv 2>&1 | Tee-Object -FilePath $log -Append
Copy-Item dataset\gt\manual_report.json dataset\gt\manual_report_retrace_2026-11-04.json -Force
Copy-Item dataset\gt\manual_accepted.txt dataset\gt\manual_accepted_retrace_2026-11-04.txt -Force
"[2/2] new Synaps exports (30 pairs: 19 SYN B01, 20 SYN B02, 21 SYN B03)..." | Tee-Object -FilePath $log -Append
& $py scripts\build_gt_dataset.py manual --matches dataset\gt\matches_syn_2026-11-04.tsv 2>&1 | Tee-Object -FilePath $log -Append
Copy-Item dataset\gt\manual_report.json dataset\gt\manual_report_syn_2026-11-04.json -Force
Copy-Item dataset\gt\manual_accepted.txt dataset\gt\manual_accepted_syn_2026-11-04.txt -Force
"=== finished $(Get-Date) ===" | Tee-Object -FilePath $log -Append
Read-Host "Done. Tell Claude 'prepare finished'. Press Enter to close"
