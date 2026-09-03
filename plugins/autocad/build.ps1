param(
    [ValidateSet("2024", "2025")]
    [string]$Version = "2024",
    [string]$Configuration = "Release",
    [string]$DotNetPath = "dotnet",
    [string]$AutoCADManagedPath = ""
)

$ErrorActionPreference = "Stop"
$pluginRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$project = Join-Path $pluginRoot "$Version\TopoSpatial.AutoCAD.$Version.csproj"
if (-not $AutoCADManagedPath) {
    $AutoCADManagedPath = "C:\Program Files\Autodesk\AutoCAD $Version"
}
if (-not (Test-Path -LiteralPath (Join-Path $AutoCADManagedPath "AcMgd.dll"))) {
    throw "AutoCAD $Version managed assemblies were not found at $AutoCADManagedPath"
}

& $DotNetPath build $project -c $Configuration `
    "-p:AutoCADManagedPath=$AutoCADManagedPath" `
    "-p:UseSharedCompilation=false"
if ($LASTEXITCODE -ne 0) {
    throw "AutoCAD plugin build failed with exit code $LASTEXITCODE"
}

$framework = if ($Version -eq "2024") { "net48" } else { "net8.0-windows" }
$source = Join-Path $pluginRoot "$Version\bin\$Configuration\$framework"
$bundle = Join-Path $pluginRoot "dist\TopoSpatial.bundle"
$destination = Join-Path $bundle "Contents\Windows\$Version"
if (Test-Path -LiteralPath $bundle) {
    Remove-Item -LiteralPath $bundle -Recurse -Force
}
New-Item -ItemType Directory -Path $destination -Force | Out-Null

$managedFiles = @(
    "TopoSpatial.AutoCAD.$Version.dll",
    "Newtonsoft.Json.dll",
    "Microsoft.Web.WebView2.Core.dll",
    "Microsoft.Web.WebView2.WinForms.dll"
)
foreach ($file in $managedFiles) {
    Copy-Item -LiteralPath (Join-Path $source $file) -Destination $destination
}
$loaderSource = Join-Path $source "runtimes\win-x64\native\WebView2Loader.dll"
$loaderDestination = Join-Path $destination "runtimes\win-x64\native"
New-Item -ItemType Directory -Path $loaderDestination -Force | Out-Null
Copy-Item -LiteralPath $loaderSource -Destination $loaderDestination

$manifest = Join-Path $pluginRoot "PackageContents.$Version.xml"
if (-not (Test-Path -LiteralPath $manifest)) {
    throw "No bundle manifest exists for AutoCAD $Version"
}
Copy-Item -LiteralPath $manifest -Destination (Join-Path $bundle "PackageContents.xml")

$zipPath = Join-Path $pluginRoot "dist\TopoSpatial-AutoCAD-$Version.zip"
if (Test-Path -LiteralPath $zipPath) {
    Remove-Item -LiteralPath $zipPath -Force
}
Compress-Archive -LiteralPath $bundle -DestinationPath $zipPath -CompressionLevel Optimal

Write-Output "DLL: $(Join-Path $destination "TopoSpatial.AutoCAD.$Version.dll")"
Write-Output "Bundle: $bundle"
Write-Output "ZIP: $zipPath"
