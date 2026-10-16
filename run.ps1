param(
    [string]$EnvironmentName = "asl_al",
    [switch]$Windowed
)

$ErrorActionPreference = "Stop"
Push-Location -LiteralPath $PSScriptRoot
try {
    if ($Windowed) {
        $aslPythonw = Join-Path $env:USERPROFILE "miniconda3\envs\$EnvironmentName\pythonw.exe"
        if (-not (Test-Path -LiteralPath $aslPythonw -PathType Leaf)) {
            throw "The Miniconda environment '$EnvironmentName' was not found. Use run.ps1 from a Miniconda terminal to check the environment."
        }
        Start-Process -FilePath $aslPythonw -ArgumentList @("-B", "-m", "vasl_ai_bridge.launcher") -WorkingDirectory $PSScriptRoot | Out-Null
    }
    elseif (Get-Command conda -ErrorAction SilentlyContinue) {
        conda run --no-capture-output -n $EnvironmentName python -B -m vasl_ai_bridge
    }
    else {
        $aslPython = Join-Path $env:USERPROFILE "miniconda3\envs\$EnvironmentName\python.exe"
        if (-not (Test-Path -LiteralPath $aslPython -PathType Leaf)) {
            throw "Conda is unavailable and the Miniconda environment was not found. Open a Miniconda terminal and run python -B -m vasl_ai_bridge from this directory."
        }
        & $aslPython -B -m vasl_ai_bridge
    }
    if (-not $Windowed -and $LASTEXITCODE -ne 0) {
        throw "VASL AI Bridge exited with code $LASTEXITCODE."
    }
}
catch {
    if ($Windowed) {
        Add-Type -AssemblyName System.Windows.Forms
        [System.Windows.Forms.MessageBox]::Show(
            $_.Exception.Message,
            "VASL AI Bridge startup error",
            [System.Windows.Forms.MessageBoxButtons]::OK,
            [System.Windows.Forms.MessageBoxIcon]::Error
        ) | Out-Null
        exit 1
    }
    throw
}
finally {
    Pop-Location
}
