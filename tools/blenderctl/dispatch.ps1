# Select the bundled Python for the requested Blender executable.
$arguments = @($args)
$selected = $null
$requested = $null
for ($index = 0; $index -lt $arguments.Count; $index++) {
    $token = [string]$arguments[$index]
    if (!$token.StartsWith('--') -or $token -ceq '--') { break }
    $flag, $inline = $token.Split('=', 2)
    if ($flag -cnotin @('--blender','--blender-version','--jobs-dir','--transactions-dir','--timeout','--edition')) { continue }
    if (!$inline) {
        if ($index + 1 -ge $arguments.Count -or ([string]$arguments[$index + 1]).StartsWith('--')) {
            throw "$flag requires a value."
        }
        $inline = [string]$arguments[++$index]
    }
    if ($flag -ceq '--blender') { $selected = $inline }
    if ($flag -ceq '--blender-version') { $requested = $inline }
}
if ($selected -and $requested) { throw 'Select either --blender-version or --blender.' }
if (!$selected) {
    $requested = if ($requested) { $requested } else { '5.3.0' }
    if ($requested -cnotmatch '^\d+\.\d+\.\d+$') { throw 'Expected a Blender version in major.minor.patch form.' }
    $selected = Join-Path $runtimeRoot "$requested/blender-$requested-windows-x64/blender.exe"
}
$binary = Get-Item -LiteralPath $selected
$info = $binary.VersionInfo
$version = $info.ProductVersionRaw.ToString(3)
if ($info.ProductName.Trim() -ne 'Blender' -or ($requested -and $version -cne $requested)) {
    throw "Selected executable does not match Windows Blender $requested."
}
$python = Join-Path $binary.DirectoryName "$($info.ProductVersionRaw.ToString(2))/python/bin/python.exe"
if (!(Test-Path -LiteralPath $python -PathType Leaf)) { throw "Project Python missing: $python" }
& $python -B (Join-Path $PSScriptRoot 'cli.py') --launcher $binary.FullName $version @arguments
exit $LASTEXITCODE
