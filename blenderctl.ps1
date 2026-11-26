# SPDX-License-Identifier: GPL-3.0-or-later
$ErrorActionPreference = 'Stop'
$cliPython = $env:BLENDERCTL_PYTHON
if ($cliPython) {
    if (!(Test-Path -LiteralPath $cliPython -PathType Leaf)) { throw 'BLENDERCTL_PYTHON must name an existing executable.' }
} else {
    foreach ($candidate in @('python3', 'python')) {
        $pythonCommand = Get-Command $candidate -CommandType Application -ErrorAction SilentlyContinue
        if ($pythonCommand) { $cliPython = $pythonCommand.Source; break }
    }
}
if (!$cliPython) { throw 'Set BLENDERCTL_PYTHON to Python 3.12+ or the matching Blender Python.' }
& $cliPython -c 'import sys; sys.exit(0 if sys.version_info >= (3,12) else 12)'
if ($LASTEXITCODE -ne 0) { throw 'Python 3.12+ required. Set BLENDERCTL_PYTHON explicitly.' }

$valueFlags = @('--blender', '--blender-version', '--jobs-dir', '--transactions-dir', '--timeout')
$commandIndex = 0
while ($commandIndex -lt $args.Count -and ([string]$args[$commandIndex]).StartsWith('--', [StringComparison]::Ordinal) -and $args[$commandIndex] -cne '--') {
    $token = [string]$args[$commandIndex]
    $parts = $token.Split('=', 2)
    $commandIndex++
    if ($parts.Length -eq 1 -and $parts[0] -cin $valueFlags -and $commandIndex -lt $args.Count) {
        $commandIndex++
    }
}

$entryPoint = 'tools\blenderctl\cli.py'
if ($args.Count -gt $commandIndex) {
    $sub = [string]$args[$commandIndex]
    if ($sub -ceq 'rigging-workflow') {
        $entryPoint = 'tools\rigging_workflow_cli.py'
    } elseif ($sub -ceq 'preview') {
        $entryPoint = 'tools\viewport_preview\cli.py'
    }
}
& $cliPython (Join-Path $PSScriptRoot $entryPoint) @args
exit $LASTEXITCODE
