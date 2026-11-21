# Restore a previously verified M0-A archive into a new directory outside the checkout.
[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$Archive,
    [Parameter(Mandatory)][ValidatePattern('^[0-9a-fA-F]{64}$')][string]$ExpectedSHA256,
    [Parameter(Mandatory)][string]$Destination
)
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.IO.Compression.FileSystem
$zip = (Resolve-Path -LiteralPath $Archive).ProviderPath
if ((Get-FileHash -LiteralPath $zip -Algorithm SHA256).Hash -ne $ExpectedSHA256) {
    throw 'Archive SHA-256 mismatch'
}

$checkout = (Get-Item -LiteralPath (Join-Path $PSScriptRoot '..')).FullName.TrimEnd('\')
$target = [IO.Path]::GetFullPath($ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($Destination)).TrimEnd('\')
if ($target -eq $checkout -or $target.StartsWith($checkout + '\', [StringComparison]::OrdinalIgnoreCase)) {
    throw 'Choose a destination outside the source checkout'
}
if (Test-Path -LiteralPath $target) { throw 'Destination must be new' }
New-Item -Path $target -ItemType Directory -ErrorAction Stop | Out-Null
[IO.Compression.ZipFile]::ExtractToDirectory($zip, $target)

$allow = @(Get-Content -LiteralPath (Join-Path $target 'baseline/files.txt') -Encoding UTF8)
$sumFile = Join-Path $target 'SHA256SUMS'
$actual = @(Get-ChildItem -LiteralPath $target -Recurse -Force -File | ForEach-Object {
    $_.FullName.Substring($target.Length + 1).Replace('\','/')
})
if (Compare-Object ($allow + 'SHA256SUMS') $actual -CaseSensitive) { throw 'Restored file inventory differs' }
$verified = @()
foreach ($line in (Get-Content -LiteralPath $sumFile -Encoding UTF8)) {
    if ($line -cnotmatch '^([0-9a-f]{64})  ([A-Za-z0-9_./-]+)$') { throw 'Invalid checksum entry' }
    $digest = $Matches[1]
    $name = $Matches[2]
    if ($name -cnotin $allow) { throw 'Unexpected checksum entry' }
    if ((Get-FileHash -LiteralPath (Join-Path $target $name) -Algorithm SHA256).Hash -ne $digest) {
        throw "File checksum mismatch: $name"
    }
    $verified += $name
}
if (Compare-Object $allow $verified -CaseSensitive) { throw 'Incomplete or duplicate checksums' }
"PASS restore: $($verified.Count) files matched the allowlist and SHA-256"
