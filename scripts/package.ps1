[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"

$pluginRoot = Split-Path $PSScriptRoot -Parent
$manifestPath = Join-Path $pluginRoot "plugin.json"
$manifest = Get-Content $manifestPath -Raw | ConvertFrom-Json

foreach ($property in @("schemaVersion", "id", "name", "version", "entrypoint")) {
    if (-not $manifest.$property) {
        throw "plugin.json is missing required property '$property'."
    }
}

$entrypointPath = Join-Path $pluginRoot $manifest.entrypoint
if (-not (Test-Path $entrypointPath -PathType Leaf)) {
    throw "Plugin entrypoint '$($manifest.entrypoint)' does not exist."
}

$distPath = Join-Path $pluginRoot "dist"
$stagingPath = Join-Path ([System.IO.Path]::GetTempPath()) ("agentplugin-" + [guid]::NewGuid())
$archiveBaseName = "$($manifest.id)-$($manifest.version)"
$zipPath = Join-Path $distPath "$archiveBaseName.zip"
$pluginPath = Join-Path $distPath "$archiveBaseName.agentplugin"

try {
    New-Item -ItemType Directory -Force $distPath | Out-Null
    New-Item -ItemType Directory -Force $stagingPath | Out-Null

    Copy-Item (Join-Path $pluginRoot "plugin.json") $stagingPath
    Copy-Item (Join-Path $pluginRoot "agent.md") $stagingPath
    Copy-Item (Join-Path $pluginRoot "README.md") $stagingPath
    Copy-Item (Join-Path $pluginRoot "integration-contract.md") $stagingPath

    Remove-Item $zipPath, $pluginPath -Force -ErrorAction SilentlyContinue
    Compress-Archive -Path (Join-Path $stagingPath "*") -DestinationPath $zipPath
    Move-Item $zipPath $pluginPath

    $hash = (Get-FileHash $pluginPath -Algorithm SHA256).Hash.ToLowerInvariant()
    "$hash  $archiveBaseName.agentplugin" | Set-Content "$pluginPath.sha256" -Encoding ascii

    Write-Output "Built $pluginPath"
    Write-Output "SHA-256 $hash"
}
finally {
    Remove-Item $stagingPath -Recurse -Force -ErrorAction SilentlyContinue
}