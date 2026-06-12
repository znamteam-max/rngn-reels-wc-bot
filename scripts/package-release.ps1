$ErrorActionPreference = 'Stop'

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
Set-Location $repoRoot

$dirty = git status --porcelain
if ($dirty) {
  throw 'Working tree is not clean. Commit the current update before packaging.'
}

$package = Get-Content -Raw -Encoding UTF8 'package.json' | ConvertFrom-Json
$shortSha = (git rev-parse --short=8 HEAD).Trim()
$timestamp = Get-Date -Format 'yyyyMMdd-HHmm'
$releaseDir = Join-Path $repoRoot 'releases'
$fileName = "$($package.name)-v$($package.version)-$timestamp-$shortSha.zip"
$outputPath = Join-Path $releaseDir $fileName

New-Item -ItemType Directory -Path $releaseDir -Force | Out-Null

git archive `
  --format=zip `
  --output=$outputPath `
  --prefix="$($package.name)/" `
  HEAD

if (-not (Test-Path -LiteralPath $outputPath)) {
  throw 'Release archive was not created.'
}

$archive = Get-Item -LiteralPath $outputPath
Write-Output $archive.FullName
