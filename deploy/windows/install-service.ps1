# Registers SignalForge as a scheduled task that behaves like a service:
# starts with Windows (no login needed), restarts if it dies, never times out.
#
#   Run in an ADMIN PowerShell, from the project directory:
#
#       .\deploy\windows\install-service.ps1
#       .\deploy\windows\install-service.ps1 -Status
#       .\deploy\windows\install-service.ps1 -Uninstall
#
# Nothing is registered until `python -m app.main check` passes.

param(
    [switch]$Uninstall,
    [switch]$Status,
    [switch]$SkipCheck
)

$ErrorActionPreference = "Stop"
$taskName = "SignalForge"
$root = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$launcher = Join-Path $root "deploy\windows\start-bot.ps1"

function Assert-Admin {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($identity)
    if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        throw "run this in an Administrator PowerShell"
    }
}

if ($Status) {
    $task = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    if ($null -eq $task) {
        Write-Host "$taskName is not installed."
        return
    }
    $info = Get-ScheduledTaskInfo -TaskName $taskName
    Write-Host "State        : $($task.State)"
    Write-Host "Last run     : $($info.LastRunTime)"
    Write-Host "Last result  : $($info.LastTaskResult)  (0 = ok, 267009 = still running)"
    Write-Host "Next run     : $($info.NextRunTime)"
    $log = Join-Path $root "logs\bot.log"
    if (Test-Path $log) {
        Write-Host ""
        Write-Host "--- last 20 log lines ---"
        Get-Content $log -Tail 20
    }
    return
}

Assert-Admin

if ($Uninstall) {
    Stop-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue
    Write-Host "$taskName removed."
    return
}

if (-not (Test-Path $launcher)) { throw "missing $launcher" }
$python = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) { throw "missing $python - create the virtualenv first" }
if (-not (Test-Path (Join-Path $root ".env"))) { throw "missing .env in $root" }

if (-not $SkipCheck) {
    Write-Host "Running the pre-flight check..." -ForegroundColor Cyan
    Push-Location $root
    $env:PYTHONIOENCODING = "utf-8"
    & $python -m app.main check
    $code = $LASTEXITCODE
    Pop-Location
    if ($code -ne 0) {
        throw "the check failed - fix the FAILED lines above, then run this again (or use -SkipCheck)"
    }
}

$action = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -NonInteractive -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$launcher`"" `
    -WorkingDirectory $root

$trigger = New-ScheduledTaskTrigger -AtStartup

# SYSTEM so the bot runs with nobody logged in.
$principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest

# ExecutionTimeLimit 0 = never kill it (the default would stop the bot after 3 days).
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
    -RestartInterval (New-TimeSpan -Minutes 1) -RestartCount 999 `
    -ExecutionTimeLimit (New-TimeSpan -Seconds 0) `
    -MultipleInstances IgnoreNew

Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger `
    -Principal $principal -Settings $settings `
    -Description "SignalForge halal spot trading bot" -Force | Out-Null

Start-ScheduledTask -TaskName $taskName
Start-Sleep -Seconds 5

$info = Get-ScheduledTaskInfo -TaskName $taskName
Write-Host ""
Write-Host "$taskName installed and started (last result: $($info.LastTaskResult))" -ForegroundColor Green
Write-Host ""
Write-Host "  logs      : $root\logs\bot.log"
Write-Host "  status    : .\deploy\windows\install-service.ps1 -Status"
Write-Host "  stop      : Stop-ScheduledTask -TaskName $taskName"
Write-Host "  start     : Start-ScheduledTask -TaskName $taskName"
Write-Host "  remove    : .\deploy\windows\install-service.ps1 -Uninstall"
Write-Host ""
Write-Host "Restart the task after editing .env." -ForegroundColor Yellow
