# One-shot server setup for SignalForge on Windows Server 2016 / 2019 / 2022.
#
#   1. unzip signalforge-deploy.zip to C:\SignalForge
#   2. copy your .env next to it
#   3. in an ADMINISTRATOR PowerShell:
#        cd C:\SignalForge
#        powershell -NoProfile -ExecutionPolicy Bypass -File .\deploy\windows\setup-server.ps1
#
# Installs Python 3.12 and the Visual C++ runtime when they are missing, builds
# the virtualenv, runs the pre-flight check and registers the scheduled task
# through install-service.ps1.  Safe to run again - every step is skipped when
# it is already done.

param(
    [switch]$NoService     # stop after the pre-flight check, do not register the task
)

$ErrorActionPreference = "Stop"
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

$root = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
Set-Location $root

$PythonUrl   = "https://www.python.org/ftp/python/3.12.10/python-3.12.10-amd64.exe"
$PythonExe   = "C:\Program Files\Python312\python.exe"
$VcRedistUrl = "https://aka.ms/vs/17/release/vc_redist.x64.exe"

function Step([string]$text) { Write-Host ""; Write-Host "== $text" -ForegroundColor Cyan }
function Ok([string]$text)   { Write-Host "   ok  $text" -ForegroundColor Green }
function Warn([string]$text) { Write-Host "   !!  $text" -ForegroundColor Yellow }

# Native programs write progress to stderr; with ErrorActionPreference=Stop
# PowerShell 5.1 can turn that into a terminating error.  Run them relaxed and
# judge by the exit code only.
function Invoke-Native([string]$exe, [string[]]$arguments) {
    $previous = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try { & $exe @arguments | Out-Host } finally { $ErrorActionPreference = $previous }
    return $LASTEXITCODE
}

function Get-PythonVersion([string]$exe) {
    if (-not (Test-Path $exe)) { return "" }
    $previous = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        return (& $exe -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>$null | Out-String).Trim()
    } catch {
        return ""
    } finally {
        $ErrorActionPreference = $previous
    }
}

function Find-Python {
    $candidates = @(
        "C:\Program Files\Python312\python.exe",
        "C:\Program Files\Python313\python.exe",
        "C:\Program Files\Python311\python.exe"
    )
    $onPath = Get-Command python -ErrorAction SilentlyContinue
    if ($onPath) { $candidates += $onPath.Source }
    foreach ($exe in $candidates) {
        if ((Get-PythonVersion $exe) -match '^3\.(11|12|13|14)$') { return $exe }
    }
    return $null
}

# ---------------------------------------------------------------- admin
$identity  = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = New-Object Security.Principal.WindowsPrincipal($identity)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw "run this in an Administrator PowerShell"
}

# ---------------------------------------------------------------- machine
Step "Machine"
$os     = Get-CimInstance Win32_OperatingSystem
$drive  = Get-PSDrive -Name $root.Substring(0, 1)
$freeGb = [math]::Round($drive.Free / 1GB, 1)
$freeMb = [math]::Round($os.FreePhysicalMemory / 1KB)
Write-Host "   $($os.Caption), build $($os.BuildNumber)"
Write-Host "   project: $root"
Write-Host "   free disk on $($drive.Name): $freeGb GB, free RAM: $freeMb MB"
if ($drive.Free -lt 2GB) {
    throw "need at least 2 GB free on $($drive.Name): - Python and the packages take about 1 GB"
}
if ($freeMb -lt 700) {
    Warn "under 700 MB of RAM free - the chart reader (onnxruntime) needs about 300 MB on top of the bot"
}

# ---------------------------------------------------------------- network
Step "Network"
try {
    $null = Invoke-WebRequest -Uri "https://api.mexc.com/api/v3/ping" -UseBasicParsing -TimeoutSec 15
    Ok "MEXC api.mexc.com"
} catch {
    Warn "MEXC unreachable: $($_.Exception.Message)"
}
if (Test-NetConnection -ComputerName 149.154.167.51 -Port 443 -InformationLevel Quiet -WarningAction SilentlyContinue) {
    Ok "Telegram DC (149.154.167.51:443)"
} else {
    Warn "Telegram data centre unreachable on port 443 - the user session cannot connect"
}
try {
    $mexcTime = (Invoke-RestMethod -Uri "https://api.mexc.com/api/v3/time" -TimeoutSec 15).serverTime
    $driftMs  = [math]::Abs([DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds() - $mexcTime)
    if ($driftMs -gt 2000) {
        Warn "clock is $driftMs ms off MEXC - LIVE orders would be rejected; run: w32tm /resync"
    } else {
        Ok "clock within $driftMs ms of MEXC"
    }
} catch {
    Warn "could not compare the clock with MEXC: $($_.Exception.Message)"
}

# ---------------------------------------------------------------- VC++ runtime
Step "Visual C++ 2015-2022 runtime (onnxruntime needs it)"
$vc = Get-ItemProperty "HKLM:\SOFTWARE\Microsoft\VisualStudio\14.0\VC\Runtimes\x64" -ErrorAction SilentlyContinue
if ($vc -and $vc.Installed -eq 1 -and $vc.Major -eq 14 -and $vc.Minor -ge 20) {
    Ok "installed, $($vc.Version)"
} else {
    $exe = Join-Path $env:TEMP "vc_redist.x64.exe"
    Write-Host "   downloading $VcRedistUrl"
    Invoke-WebRequest -Uri $VcRedistUrl -OutFile $exe -UseBasicParsing
    $p = Start-Process -FilePath $exe -ArgumentList "/install", "/quiet", "/norestart" -Wait -PassThru
    if ($p.ExitCode -notin 0, 1638, 3010) { throw "vc_redist installer failed, exit code $($p.ExitCode)" }
    Ok "installed"
}

# ---------------------------------------------------------------- Python
Step "Python"
$python = Find-Python
if ($python) {
    Ok "$python ($(Get-PythonVersion $python))"
} else {
    $exe = Join-Path $env:TEMP "python-3.12.10-amd64.exe"
    Write-Host "   not found - downloading $PythonUrl"
    Invoke-WebRequest -Uri $PythonUrl -OutFile $exe -UseBasicParsing
    $p = Start-Process -FilePath $exe -Wait -PassThru -ArgumentList `
        "/quiet", "InstallAllUsers=1", "PrependPath=1", "Include_test=0", "Include_launcher=1"
    if ($p.ExitCode -ne 0) { throw "Python installer failed, exit code $($p.ExitCode)" }
    $python = $PythonExe
    if (-not (Test-Path $python)) { throw "Python installed but $python is missing" }
    Ok "installed $python"
}

# ---------------------------------------------------------------- virtualenv
Step "Virtualenv and packages"
$venvPython = Join-Path $root ".venv\Scripts\python.exe"
if (Test-Path $venvPython) {
    Ok ".venv exists"
} else {
    if ((Invoke-Native $python @("-m", "venv", (Join-Path $root ".venv"))) -ne 0) { throw "python -m venv failed" }
    Ok ".venv created"
}
$null = Invoke-Native $venvPython @("-m", "pip", "install", "--quiet", "--upgrade", "pip")
if ((Invoke-Native $venvPython @("-m", "pip", "install", "--quiet", "-r", (Join-Path $root "requirements.txt"))) -ne 0) {
    throw "pip install -r requirements.txt failed"
}
Ok "packages installed"

# ---------------------------------------------------------------- .env
Step ".env"
$envFile = Join-Path $root ".env"
if (-not (Test-Path $envFile)) {
    throw "copy your .env into $root first - it holds the Telegram session and the coin list"
}
$mode = Select-String -Path $envFile -Pattern '^TRADING_MODE=(\w+)'
if ($mode) { Write-Host "   TRADING_MODE=$($mode.Matches[0].Groups[1].Value)" } else { Write-Host "   TRADING_MODE not set (PAPER)" }
Warn "the same Telegram session must not run on two machines at once - stop the bot on your PC"

# ---------------------------------------------------------------- check
Step "Pre-flight check"
$env:PYTHONIOENCODING = "utf-8"
if ((Invoke-Native $venvPython @("-m", "app.main", "check")) -ne 0) {
    throw "the check failed - fix the FAILED lines above, then run this script again"
}

if ($NoService) {
    Write-Host ""
    Write-Host "Done. Start by hand with: .\deploy\windows\start-bot.ps1" -ForegroundColor Green
    return
}

# ---------------------------------------------------------------- service
Step "Scheduled task"
& (Join-Path $PSScriptRoot "install-service.ps1") -SkipCheck
