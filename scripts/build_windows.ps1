<#
.SYNOPSIS
Build the Windows x64 application and per-user installer from pinned tools.
.DESCRIPTION
Use an existing x64 Python 3.12 environment with requirements-build.txt
and an installed Inno Setup 6.7.1 or 6.7.3 compiler. Both paths must be
supplied explicitly. This script verifies the compiler's publisher and
actual version; it never downloads or installs build tools. Local and
GitHub Actions builds use this same definition.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$PythonExe,
    [Parameter(Mandatory = $true)]
    [string]$IsccExe,
    [string]$SourceRevision = $env:GITHUB_SHA
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

if ([Environment]::OSVersion.Platform -ne [PlatformID]::Win32NT) {
    throw 'The Windows installer must be built on Windows.'
}
$projectRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$distRoot = [IO.Path]::GetFullPath((Join-Path $projectRoot 'dist'))
$buildRoot = [IO.Path]::GetFullPath((Join-Path $projectRoot 'build/windows'))
$pyinstallerWorkRoot = [IO.Path]::GetFullPath((Join-Path $buildRoot 'pyinstaller'))
$payloadRoot = [IO.Path]::GetFullPath((Join-Path $distRoot 'VASL AI Bridge'))
if (-not $payloadRoot.StartsWith($distRoot + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
    throw 'The PyInstaller payload must remain inside the project dist directory.'
}
foreach ($directory in @($distRoot, (Join-Path $projectRoot 'build'), $buildRoot, $pyinstallerWorkRoot, $payloadRoot)) {
    if ((Test-Path -LiteralPath $directory) -and ((Get-Item -LiteralPath $directory).Attributes -band [IO.FileAttributes]::ReparsePoint)) {
        throw 'Build and dist targets must not be symlinks or junctions.'
    }
}
$PythonExe = (Resolve-Path -LiteralPath $PythonExe).Path
$IsccExe = (Resolve-Path -LiteralPath $IsccExe).Path
if (-not (Test-Path -LiteralPath $PythonExe -PathType Leaf) -or -not (Test-Path -LiteralPath $IsccExe -PathType Leaf)) {
    throw 'PythonExe and IsccExe must refer to existing executable files.'
}
$compilerSignature = Get-AuthenticodeSignature -LiteralPath $IsccExe
if ($compilerSignature.Status -ne [System.Management.Automation.SignatureStatus]::Valid -or
    $null -eq $compilerSignature.SignerCertificate -or
    $compilerSignature.SignerCertificate.GetNameInfo([Security.Cryptography.X509Certificates.X509NameType]::SimpleName, $false) -ne 'Pyrsys B.V.') {
    throw 'The installed Inno Setup compiler must have a valid Pyrsys B.V. signature.'
}
# Compiler resources can report 0.0.0.0. Read the compiler's own ISPP VER;
# this minimal script has Output=no and cannot produce or run an installer.
$compilerOutput = @(& $IsccExe (Join-Path $projectRoot 'packaging/inno_version.iss') 2>&1)
if ($LASTEXITCODE -ne 0) { throw 'Could not query the installed Inno Setup compiler version.' }
$versionMarkers = @($compilerOutput | ForEach-Object {
    if ([string]$_ -match '^\s*(?:Line \d+:\s*)?VASL_AI_BRIDGE_INNO_VERSION=(\d+\.\d+\.\d+)\s*$') { $Matches[1] }
})
if ($versionMarkers.Count -ne 1 -or $versionMarkers[0] -notin @('6.7.1', '6.7.3')) {
    throw 'Only Inno Setup 6.7.1 and 6.7.3 are supported for this build.'
}
$innoVersion = $versionMarkers[0]
Write-Host ('Verified installed Inno Setup ' + $innoVersion + ' compiler from Pyrsys B.V.')
if ([string]::IsNullOrWhiteSpace($SourceRevision)) {
    $SourceRevision = 'local'
}
New-Item -ItemType Directory -Path $buildRoot -Force | Out-Null

Push-Location $projectRoot
try {
    & $PythonExe -B packaging/prepare_build.py --source-revision $SourceRevision --inno-version $innoVersion
    if ($LASTEXITCODE -ne 0) { throw 'Build metadata/license preparation failed.' }
    $appVersion = (& $PythonExe -B packaging/prepare_build.py --version-only).Trim()
    if ($LASTEXITCODE -ne 0 -or $appVersion -notmatch '^\d+\.\d+\.\d+$') { throw 'Could not read the project version.' }
    & $PythonExe -B -m PyInstaller --noconfirm --distpath $distRoot --workpath $pyinstallerWorkRoot packaging/VASL_AI_Bridge.spec
    if ($LASTEXITCODE -ne 0) { throw 'PyInstaller failed.' }
    & $IsccExe ('/DAppVersion=' + $appVersion) ('/DPayloadDir=' + $payloadRoot) ('/DOutputDir=' + $distRoot) packaging/VASL_AI_Bridge.iss
    if ($LASTEXITCODE -ne 0) { throw 'Inno Setup compilation failed.' }
    $installer = Join-Path $distRoot ('VASL-AI-Bridge-Setup-' + $appVersion + '-x64.exe')
    # The only permitted checksum operation is the final GitHub-built EXE.
    if ($env:GITHUB_ACTIONS -eq 'true') {
        $checksum = (Get-FileHash -LiteralPath $installer -Algorithm SHA256).Hash.ToLowerInvariant()
        ($checksum + '  ' + [IO.Path]::GetFileName($installer)) | Set-Content -LiteralPath (Join-Path $distRoot 'SHA256SUMS.txt') -Encoding ascii
    }
    Write-Host ('Built ' + [IO.Path]::GetFileName($installer))
}
finally {
    Pop-Location
}
