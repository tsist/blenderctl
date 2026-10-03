# SPDX-License-Identifier: GPL-3.0-or-later
# Explicit Python override, compatible portable runtime, then an installed Python.
$ErrorActionPreference = 'Stop'
$cliPython = $env:BLENDERCTL_PYTHON
if ($cliPython) {
    if (!(Test-Path -LiteralPath $cliPython -PathType Leaf)) {
        throw "BLENDERCTL_PYTHON must name an existing Python executable."
    }
} else {
    $portablePython = Join-Path $PSScriptRoot 'runtime\blender\releases\5.2.1\blender-5.2.1-windows-x64\5.2\python\bin\python.exe'
    if (Test-Path -LiteralPath $portablePython -PathType Leaf) {
        $cliPython = $portablePython
    } else {
        foreach ($candidate in @('python3', 'python')) {
            $pythonCommand = Get-Command $candidate -CommandType Application -ErrorAction SilentlyContinue
            if ($pythonCommand) {
                $cliPython = $pythonCommand.Source
                break
            }
        }
    }
}
if (!$cliPython) { throw 'Python 3.12+ is required. Set BLENDERCTL_PYTHON to its executable path.' }
& $cliPython -c 'import sys; sys.exit(0 if sys.version_info >= (3,12) else 12)'
if ($LASTEXITCODE -ne 0) { throw 'Cannot run Python 3.12+. Set BLENDERCTL_PYTHON explicitly.' }
& $cliPython (Join-Path $PSScriptRoot 'tools\blenderctl\cli.py') @args
exit $LASTEXITCODE
