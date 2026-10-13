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
& $cliPython (Join-Path $PSScriptRoot 'tools\blenderctl\cli.py') @args
exit $LASTEXITCODE
