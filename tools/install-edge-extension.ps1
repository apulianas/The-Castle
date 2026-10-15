[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'

if (-not $env:LOCALAPPDATA) {
    throw 'LOCALAPPDATA is not set. Run this installer on Windows as the Edge profile user.'
}

$source = Join-Path (Split-Path -Parent $PSScriptRoot) 'edge_extension'
$destination = Join-Path $env:LOCALAPPDATA 'The-Castle\edge-extension'
$files = @('manifest.json', 'worker.js', 'collector.js', 'popup.html', 'popup.js', 'popup.css', 'SETUP.md')

foreach ($name in $files) {
    if (-not (Test-Path -LiteralPath (Join-Path $source $name) -PathType Leaf)) {
        throw "Missing extension source file: $name"
    }
}
$manifest = Get-Content -LiteralPath (Join-Path $source 'manifest.json') -Raw -Encoding UTF8 | ConvertFrom-Json
if ($manifest.manifest_version -ne 3 -or -not $manifest.version) {
    throw 'The source extension manifest is invalid.'
}

New-Item -ItemType Directory -Path $destination -Force | Out-Null
foreach ($name in $files) {
    $sourceFile = Join-Path $source $name
    $installedFile = Join-Path $destination $name
    Copy-Item -LiteralPath $sourceFile -Destination $installedFile -Force
    if ((Get-FileHash -LiteralPath $sourceFile -Algorithm SHA256).Hash -ne
        (Get-FileHash -LiteralPath $installedFile -Algorithm SHA256).Hash) {
        throw "Installed extension file failed verification: $name. Do not reload the extension."
    }
}

Write-Output "Installed Ravens extension v$($manifest.version) to:"
Write-Output $destination
Write-Output 'In the dedicated Edge profile, Load unpacked from this folder once; use Reload for later updates.'
Write-Output 'Moving from a different folder changes the extension ID. Register the new ID with the native host before Start.'
Write-Output 'This installer does not change native-host registration or start collection.'
