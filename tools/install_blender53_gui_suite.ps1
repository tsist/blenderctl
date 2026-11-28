# SPDX-License-Identifier: GPL-3.0-or-later
<#
.SYNOPSIS
    Package, validate, and install Blender AI 1.0 GUI extensions into local Blender 5.3.
#>
param(
    [string[]]$Plugin = @(),
    [switch]$All,
    [string]$BlenderExe = $env:BLENDER_53_EXE,
    [string]$Repo = 'user_default',
    [switch]$SkipSync
)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$pythonExe = Join-Path (Split-Path -Parent $BlenderExe) '5.3\python\bin\python.exe'
$registryPath = Join-Path $projectRoot 'tools\blenderctl\registry.json'
$distDir = Join-Path $projectRoot 'runtime\gui-extensions'

if (!(Test-Path -LiteralPath $BlenderExe -PathType Leaf)) { throw "Blender not found: $BlenderExe" }
if (!(Test-Path -LiteralPath $pythonExe -PathType Leaf)) { throw "Bundled Python not found: $pythonExe" }

$registry = Get-Content -LiteralPath $registryPath -Raw | ConvertFrom-Json
$allDomains = @($registry.plugins | ForEach-Object { $_.domain })
$targets = if ($All) { $allDomains } elseif ($Plugin.Count -gt 0) { $Plugin } else { @('lighting') }

foreach ($d in $targets) {
    if ($d -notin $allDomains) { throw "Unknown plugin domain: $d" }
}

if (-not $SkipSync) {
    Write-Output "[1/3] Syncing _ui_kit into target plugins: $($targets -join ', ')"
    $syncArgs = @('-X', 'utf8', (Join-Path $projectRoot 'tools\ui_kit\sync_ui_kit.py'))
    foreach ($d in $targets) { $syncArgs += @('--plugin', $d) }
    & $pythonExe @syncArgs
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}

Write-Output "[2/3] Packaging Blender 5.3 extension archives..."
$packArgs = @('-X', 'utf8', (Join-Path $projectRoot 'tools\ui_kit\package_extensions.py'), '--dist', $distDir)
foreach ($d in $targets) { $packArgs += @('--plugin', $d) }
$packedRaw = & $pythonExe @packArgs
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
$packedObj = $packedRaw | ConvertFrom-Json
$packages = @($packedObj.packages)
if ($packages.Count -ne @($targets).Count) { throw 'The package count does not match the requested extensions.' }

Write-Output "[3/3] Validating & installing $($packages.Count) extension(s) into Blender 5.3 repository '$Repo'..."
foreach ($item in $packages) {
    Write-Output "  -> Validating $($item.domain) ($($item.bytes) bytes): $($item.zip)"
    & $BlenderExe --factory-startup --offline-mode --command extension validate $item.zip
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

    Write-Output "  -> Installing & enabling $($item.domain) into '$Repo'"
    $installOutput = @(& $BlenderExe --offline-mode --command extension install-file -r $Repo -e $item.zip 2>&1)
    $installExit = $LASTEXITCODE
    $installOutput | Write-Output
    if ($installExit -ne 0) { exit $installExit }
    $installedStatus = '^STATUS (?:Installed|Reinstalled) "' + [regex]::Escape($item.manifest_id) + '"\s*$'
    if (($installOutput -match '^ERROR\b') -or !($installOutput -match $installedStatus)) {
        throw "Blender did not confirm installation of $($item.manifest_id)."
    }
}

Write-Output "DONE: Installed and enabled $($packages.Count) Blender AI GUI extension(s) into '$Repo'."
exit 0
