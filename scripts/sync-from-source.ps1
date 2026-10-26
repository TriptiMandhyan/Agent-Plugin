<#
Rebuilds the plugin contents from a clone of measureable_content_agents (main), then applies overlay/.
Overlay files are plugin-specific adaptations of source files; a warning is printed when the
source of an overlaid file changed since overlay/baseline.json was recorded.
  .\scripts\sync-from-source.ps1 -SourcePath C:\path\to\measureable_content_agents
  .\scripts\sync-from-source.ps1 -SourcePath ... -UpdateBaseline   # after reviewing the warnings
#>
param(
    [Parameter(Mandatory)][string]$SourcePath,
    [switch]$UpdateBaseline
)
$ErrorActionPreference = 'Stop'
$root = Split-Path $PSScriptRoot -Parent
$gh = Join-Path (Resolve-Path $SourcePath).Path '.github'
$drl = Join-Path $gh 'agents\Drl Pipeline'
$skills = 'cqm-drl', 'hedis-drl', 'mips-drl', 'drl-review-rules'

foreach ($d in 'com.github.copilot', 'skills', 'runtime') {
    $p = Join-Path $root $d
    if (Test-Path $p) { Remove-Item $p -Recurse -Force }
}

$sources = @{}  # plugin-relative path -> source file
function Copy-Tracked($from, $toRel) {
    $to = Join-Path $root $toRel
    New-Item -ItemType Directory -Force (Split-Path $to) | Out-Null
    Copy-Item $from $to
    $sources[$toRel.Replace('\', '/')] = $from
}

foreach ($f in Get-ChildItem (Join-Path $gh 'agents') -Filter 'drl-*.agent.md') {
    Copy-Tracked $f.FullName "com.github.copilot\agents\$($f.Name)"
}
foreach ($s in $skills) {
    $dir = Join-Path $gh "skills\$s"
    foreach ($rel in Get-ChildItem $dir -Recurse -File -Name) {
        Copy-Tracked (Join-Path $dir $rel) "skills\$s\$rel"
    }
}
foreach ($sub in 'config', 'docs', 'prompts', 'templates') {
    $dir = Join-Path $drl $sub
    foreach ($rel in Get-ChildItem $dir -Recurse -File -Name) {
        Copy-Tracked (Join-Path $dir $rel) "runtime\agents\Drl Pipeline\$sub\$rel"
    }
}
foreach ($f in Get-ChildItem (Join-Path $drl 'scripts') -Filter '*.py') {
    Copy-Tracked $f.FullName "runtime\agents\Drl Pipeline\scripts\$($f.Name)"
}
foreach ($n in 'README.md', 'WORKFLOW.md', 'copilot-instructions.md') {
    Copy-Tracked (Join-Path $drl $n) "runtime\agents\Drl Pipeline\$n"
}
foreach ($n in 'config.json', 'statuses.json') {
    Copy-Tracked (Join-Path $gh "orchestration\$n") "runtime\orchestration\$n"
}

$overlay = Join-Path $root 'overlay'
$baselinePath = Join-Path $overlay 'baseline.json'
$baseline = if (Test-Path $baselinePath) { Get-Content $baselinePath -Raw | ConvertFrom-Json } else { $null }
$current = [ordered]@{}
foreach ($name in Get-ChildItem $overlay -Recurse -File -Name | Where-Object { $_ -ne 'baseline.json' }) {
    $f = Get-Item (Join-Path $overlay $name)
    $rel = $name.Replace('\', '/')
    if ($sources.ContainsKey($rel)) {
        $current[$rel] = (Get-FileHash $sources[$rel] -Algorithm SHA256).Hash
        $old = if ($baseline) { $baseline.$rel } else { $null }
        if (-not $UpdateBaseline -and $old -and $old -ne $current[$rel]) {
            Write-Warning "Source changed since overlay was made: $rel - merge the change into overlay/$rel"
        }
    } else {
        Write-Warning "Overlay file has no source counterpart: $rel"
    }
    $dest = Join-Path $root $rel
    New-Item -ItemType Directory -Force (Split-Path $dest) | Out-Null
    Copy-Item $f.FullName $dest -Force
}
if ($UpdateBaseline -or -not $baseline) {
    $current | ConvertTo-Json | Set-Content $baselinePath -Encoding UTF8
    Write-Host "Baseline recorded in overlay/baseline.json"
}
Write-Host "Synced $($sources.Count) source files; applied overlay."
