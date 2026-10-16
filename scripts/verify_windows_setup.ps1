<#
.SYNOPSIS
Silently install, exercise and uninstall a generated Windows Setup package.
.DESCRIPTION
Uses a temporary installation path and synthetic application data under
runtime. Stops before installation if the existing application AppId is registered.
The installed self-test uses only Windows directories in PATH. This checks
packaged dependencies on this host; it does not emulate a clean Windows OS,
authenticate to an AI service, or perform real Whisper inference.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$Installer,
    [Parameter(Mandatory = $true)]
    [string]$Output
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$projectRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$runtimeRoot = [IO.Path]::GetFullPath((Join-Path $projectRoot 'runtime'))
$outputPath = [IO.Path]::GetFullPath($Output)
$registrySuffix = 'Software\Microsoft\Windows\CurrentVersion\Uninstall\{C7ED0871-6753-4AFA-9BBA-440350FABE41}_is1'
$registryKeys = @(
    ('Registry::HKEY_CURRENT_USER\' + $registrySuffix),
    ('Registry::HKEY_LOCAL_MACHINE\' + $registrySuffix),
    ('Registry::HKEY_CURRENT_USER\Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\{C7ED0871-6753-4AFA-9BBA-440350FABE41}_is1'),
    ('Registry::HKEY_LOCAL_MACHINE\Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\{C7ED0871-6753-4AFA-9BBA-440350FABE41}_is1')
)

function Assert-WorkspacePath([string]$Path, [string]$Base) {
    $resolved = [IO.Path]::GetFullPath($Path)
    $baseResolved = [IO.Path]::GetFullPath($Base).TrimEnd([IO.Path]::DirectorySeparatorChar)
    if (-not $resolved.StartsWith($baseResolved + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
        throw 'The verification path must remain inside its intended workspace directory.'
    }
    $current = $resolved
    while ($current.Length -ge $baseResolved.Length) {
        if ((Test-Path -LiteralPath $current) -and ((Get-Item -LiteralPath $current).Attributes -band [IO.FileAttributes]::ReparsePoint)) {
            throw 'Verification targets must not be symlinks or junctions.'
        }
        if ($current.Equals($baseResolved, [StringComparison]::OrdinalIgnoreCase)) { break }
        $current = [IO.Path]::GetDirectoryName($current)
    }
}

function Get-AppRegistration {
    foreach ($key in $registryKeys) {
        if (Test-Path -LiteralPath $key) {
            $properties = Get-ItemProperty -LiteralPath $key
            $locationProperty = $properties.PSObject.Properties['InstallLocation']
            $location = if ($null -ne $locationProperty) { [string]$locationProperty.Value } else { '' }
            [PSCustomObject]@{ Key = $key; Location = $location }
        }
    }
}

function Invoke-VerificationProcess([string]$Executable, [string[]]$Arguments, [int]$TimeoutSeconds) {
    $process = Start-Process -FilePath $Executable -ArgumentList $Arguments -WorkingDirectory $caseRoot -PassThru
    if (-not $process.WaitForExit($TimeoutSeconds * 1000)) {
        try { $process.Kill() } catch { }
        throw 'A verification process exceeded its allowed runtime.'
    }
    if ($process.ExitCode -ne 0) { throw 'A verification process returned a failure exit code.' }
}

function Assert-TestRegistration([string]$InstallPath) {
    $registrations = @(Get-AppRegistration)
    if ($registrations.Count -ne 1 -or -not $registrations[0].Key.StartsWith('Registry::HKEY_CURRENT_USER\', [StringComparison]::OrdinalIgnoreCase)) {
        throw 'The test installation must have exactly one current-user registration.'
    }
    $registeredPath = [IO.Path]::GetFullPath($registrations[0].Location).TrimEnd([IO.Path]::DirectorySeparatorChar)
    if (-not $registeredPath.Equals($InstallPath.TrimEnd([IO.Path]::DirectorySeparatorChar), [StringComparison]::OrdinalIgnoreCase)) {
        throw 'The VASL AI Bridge registration belongs to a different installation.'
    }
}

function Remove-VerificationDirectory {
    # Inno's temporary uninstaller can briefly retain its completion marker
    # after the program and registration have been removed. Retry only I/O
    # failures for a bounded period; unexpected errors still fail verification.
    $cleanupDeadline = [DateTime]::UtcNow.AddSeconds(15)
    while (Test-Path -LiteralPath $caseRoot) {
        Assert-WorkspacePath $caseRoot $runtimeRoot
        $reparseItems = @(Get-ChildItem -LiteralPath $caseRoot -Recurse -Force | Where-Object { $_.Attributes -band [IO.FileAttributes]::ReparsePoint })
        if ($reparseItems.Count -gt 0) { throw 'Verification cleanup refused a symlink or junction.' }
        try {
            Remove-Item -LiteralPath $caseRoot -Recurse -Force
        }
        catch [System.IO.IOException] {
            if ([DateTime]::UtcNow -ge $cleanupDeadline) { throw }
            Start-Sleep -Milliseconds 200
        }
    }
}

if ([Environment]::OSVersion.Platform -ne [PlatformID]::Win32NT) { throw 'Setup verification requires Windows.' }
Assert-WorkspacePath $runtimeRoot $projectRoot
Assert-WorkspacePath $outputPath $projectRoot
$installerPath = (Resolve-Path -LiteralPath $Installer).Path
if ([IO.Path]::GetFileName($installerPath) -notmatch '^VASL-AI-Bridge-Setup-(\d+\.\d+\.\d+)-x64\.exe$') {
    throw 'Select a versioned VASL AI Bridge Windows x64 Setup package.'
}
$appVersion = $Matches[1]
if (@(Get-AppRegistration).Count -ne 0) {
    throw 'VASL AI Bridge is already installed; verification will not replace an existing installation.'
}
$caseName = 'Setup verification ' + [Guid]::NewGuid().ToString('N')
$caseRoot = [IO.Path]::GetFullPath((Join-Path $runtimeRoot $caseName))
$installPath = [IO.Path]::GetFullPath((Join-Path $caseRoot 'Installed VASL AI Bridge'))
$profileRoot = Join-Path $caseRoot 'Synthetic profile'
$localAppData = Join-Path $profileRoot 'Local'
$roamingAppData = Join-Path $profileRoot 'Roaming'
$tempRoot = Join-Path $caseRoot 'Temporary files'
$userDataRoot = Join-Path $localAppData 'ASL_AI'
$sentinelPath = Join-Path $userDataRoot 'retained-user-data.txt'
$selfTestPath = Join-Path $caseRoot 'installed-self-test.json'
$uninstallerPath = Join-Path $installPath 'unins000.exe'
Assert-WorkspacePath $caseRoot $runtimeRoot
Assert-WorkspacePath $installPath $caseRoot
foreach ($directory in @($userDataRoot, $roamingAppData, $tempRoot)) {
    New-Item -ItemType Directory -Path $directory -Force | Out-Null
}
[IO.File]::WriteAllText($sentinelPath, 'VASL AI Bridge synthetic retention sentinel', [Text.Encoding]::UTF8)
$sentinelHash = (Get-FileHash -LiteralPath $sentinelPath -Algorithm SHA256).Hash
$checks = [ordered]@{
    current_user_install = $false
    installed_self_test = $false
    restricted_environment = $false
    uninstall = $false
    user_data_retained = $false
    registration_removed = $false
}
$report = [ordered]@{
    schema = 1
    application = 'VASL AI Bridge'
    version = $appVersion
    platform = 'windows-x64'
    installer_sha256 = (Get-FileHash -LiteralPath $installerPath -Algorithm SHA256).Hash.ToLowerInvariant()
    passed = $false
    checks = $checks
    limitations = @('Restricted PATH is not a clean operating system.', 'No authenticated AI or real Whisper inference was exercised.')
}
$environmentChanges = [ordered]@{
    PATH = (Join-Path $env:SystemRoot 'System32') + ';' + $env:SystemRoot
    LOCALAPPDATA = $localAppData
    APPDATA = $roamingAppData
    USERPROFILE = $profileRoot
    TEMP = $tempRoot
    TMP = $tempRoot
    QT_QPA_PLATFORM = 'windows'
    PYTHONHOME = $null
    PYTHONPATH = $null
    VIRTUAL_ENV = $null
    CONDA_PREFIX = $null
    CONDA_DEFAULT_ENV = $null
    QT_PLUGIN_PATH = $null
    QT_QPA_PLATFORM_PLUGIN_PATH = $null
    QML_IMPORT_PATH = $null
    QML2_IMPORT_PATH = $null
}
$previousEnvironment = @{}
$completed = $false
$installationStarted = $false
$verificationStage = 'PrepareEnvironment'
try {
    foreach ($name in $environmentChanges.Keys) {
        $previousEnvironment[$name] = [Environment]::GetEnvironmentVariable($name, 'Process')
        [Environment]::SetEnvironmentVariable($name, $environmentChanges[$name], 'Process')
    }
    $installationStarted = $true
    $verificationStage = 'Install'
    $installArguments = @('/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART', '/SP-', '/CURRENTUSER', '/NOICONS', ('/DIR="' + $installPath + '"'), ('/GROUP="VASL AI Bridge verification ' + [Guid]::NewGuid().ToString('N') + '"'))
    Invoke-VerificationProcess $installerPath $installArguments 180
    Assert-TestRegistration $installPath
    if (-not (Test-Path -LiteralPath (Join-Path $installPath 'VASL AI Bridge.exe')) -or -not (Test-Path -LiteralPath $uninstallerPath)) {
        throw 'Setup did not install the application and uninstaller.'
    }
    $checks.current_user_install = $true
    $verificationStage = 'InstalledSelfTest'
    $testArguments = @('--self-test', '--self-test-report', ('"' + $selfTestPath + '"'))
    Invoke-VerificationProcess (Join-Path $installPath 'VASL AI Bridge.exe') $testArguments 90
    $selfTest = Get-Content -LiteralPath $selfTestPath -Raw | ConvertFrom-Json
    if ($selfTest.passed -ne $true -or $selfTest.frozen -ne $true -or $selfTest.version -ne $appVersion) {
        throw 'The installed self-test did not confirm this application version.'
    }
    $checks.installed_self_test = $true
    $checks.restricted_environment = $true
    $report['application_self_test'] = $selfTest
    $verificationStage = 'Uninstall'
    Assert-TestRegistration $installPath
    Invoke-VerificationProcess $uninstallerPath @('/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART') 180
    $uninstallDeadline = [DateTime]::UtcNow.AddSeconds(30)
    while ((Test-Path -LiteralPath (Join-Path $installPath 'VASL AI Bridge.exe')) -or @(Get-AppRegistration).Count -ne 0) {
        if ([DateTime]::UtcNow -ge $uninstallDeadline) { throw 'Uninstall did not remove the test program and registration.' }
        Start-Sleep -Milliseconds 200
    }
    $checks.uninstall = $true
    $checks.registration_removed = $true
    $verificationStage = 'UserDataRetention'
    if (-not (Test-Path -LiteralPath $sentinelPath) -or (Get-FileHash -LiteralPath $sentinelPath -Algorithm SHA256).Hash -ne $sentinelHash) {
        throw 'Uninstall removed or modified the synthetic application data.'
    }
    $checks.user_data_retained = $true
    $completed = $true
    $report.passed = $true
}
catch {
    $report['error'] = 'SetupVerificationFailed'
    $report['failed_stage'] = $verificationStage
}
finally {
    # Clean up an interrupted test only when its AppId still points at this
    # exact synthetic installation. Never uninstall an unrelated registration.
    if (-not $completed -and $installationStarted -and (Test-Path -LiteralPath $uninstallerPath)) {
        try {
            Assert-TestRegistration $installPath
            Invoke-VerificationProcess $uninstallerPath @('/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART') 180
        }
        catch { $report['cleanup_error'] = 'TestUninstallFailed' }
    }
    foreach ($name in $previousEnvironment.Keys) {
        [Environment]::SetEnvironmentVariable($name, $previousEnvironment[$name], 'Process')
    }
    $outputParent = [IO.Path]::GetDirectoryName($outputPath)
    New-Item -ItemType Directory -Path $outputParent -Force | Out-Null
    $cleanupFailure = $null
    if ($completed) {
        try { Remove-VerificationDirectory }
        catch {
            $cleanupFailure = $_
            $completed = $false
            $report.passed = $false
            $report['cleanup_error'] = 'StagedDirectoryCleanupFailed'
            $report['failed_stage'] = 'Cleanup'
        }
    }
    $report | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath $outputPath -Encoding UTF8
    if ($null -ne $cleanupFailure) { throw $cleanupFailure }
}
if (-not $completed) { throw 'Setup verification failed; inspect its fixed-field report.' }
Write-Host 'Setup installation, installed self-test, uninstall and user-data retention passed.'
