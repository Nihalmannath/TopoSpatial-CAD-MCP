param(
    [string]$BundlePath = "",
    [switch]$NoBackup
)

$ErrorActionPreference = "Stop"
$pluginRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
if (-not $BundlePath) {
    $BundlePath = Join-Path $pluginRoot "dist\TopoSpatial.bundle"
}
$source = (Resolve-Path -LiteralPath $BundlePath).Path
$applicationPlugins = Join-Path $env:APPDATA "Autodesk\ApplicationPlugins"
New-Item -ItemType Directory -Path $applicationPlugins -Force | Out-Null
$applicationPlugins = (Resolve-Path -LiteralPath $applicationPlugins).Path
$target = Join-Path $applicationPlugins "TopoSpatial.bundle"

if (-not $target.StartsWith($applicationPlugins, [StringComparison]::OrdinalIgnoreCase)) {
    throw "Resolved plugin target is outside Autodesk ApplicationPlugins"
}
if (Test-Path -LiteralPath $target) {
    if ($NoBackup) {
        Remove-Item -LiteralPath $target -Recurse -Force
    }
    else {
        $timestamp = Get-Date -Format "yyyyMMdd-HHmmss"
        $backup = Join-Path $applicationPlugins "TopoSpatial.bundle.backup-$timestamp"
        Move-Item -LiteralPath $target -Destination $backup
        Write-Output "Previous bundle backed up to: $backup"
    }
}
Copy-Item -LiteralPath $source -Destination $target -Recurse
Get-ChildItem -LiteralPath $target -Recurse -File | Unblock-File

Write-Output "Installed bundle: $target"
Write-Output "Restart AutoCAD, start the TopoSpatial MCP server, then run TOPOSTUDIO."
