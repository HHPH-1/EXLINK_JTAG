param(
    [switch]$NoVenv,
    [switch]$SkipInstall
)

$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoRoot = Resolve-Path (Join-Path $ScriptDir "..\..")
$SpecPath = Join-Path $ScriptDir "ExlinkManager.spec"
$Requirements = Join-Path $ScriptDir "requirements.txt"
$DistPath = Join-Path $RepoRoot "dist"
$BuildPath = Join-Path $RepoRoot "build\exlink_manager_pyinstaller"
$VenvPath = Join-Path $RepoRoot ".venv-exlink-manager"

function Invoke-Checked {
    param(
        [Parameter(Mandatory = $true)]
        [string]$FilePath,
        [Parameter(ValueFromRemainingArguments = $true)]
        [string[]]$Arguments
    )
    & $FilePath @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Command failed with exit code $LASTEXITCODE`: $FilePath $($Arguments -join ' ')"
    }
}

if ($NoVenv) {
    $Python = "python"
} else {
    if (-not (Test-Path $VenvPath)) {
        python -m venv $VenvPath
    }
    $Python = Join-Path $VenvPath "Scripts\python.exe"
}

if (-not $SkipInstall) {
    Invoke-Checked $Python -m pip install --upgrade pip
    Invoke-Checked $Python -m pip install -r $Requirements
}

New-Item -ItemType Directory -Force $DistPath | Out-Null
New-Item -ItemType Directory -Force $BuildPath | Out-Null

Invoke-Checked $Python -m PyInstaller `
    --clean `
    --noconfirm `
    --distpath $DistPath `
    --workpath $BuildPath `
    $SpecPath

$ExePath = Join-Path $DistPath "ExlinkManager.exe"
if (-not (Test-Path $ExePath)) {
    throw "Expected EXE was not produced: $ExePath"
}

Write-Host "Built $ExePath"
