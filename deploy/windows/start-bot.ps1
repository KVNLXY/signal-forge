# Launcher used by the scheduled task.  Runs the bot and appends everything to
# logs\bot.log, rotating that file at 10 MB.  Run it by hand to watch the bot
# in a console instead.

$ErrorActionPreference = "Stop"

# deploy\windows\start-bot.ps1 -> project root
$root = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
Set-Location $root

# Channel titles and log lines contain emoji; without this Python crashes on a
# console codepage such as cp1251.
$env:PYTHONIOENCODING = "utf-8"
$env:PYTHONUNBUFFERED = "1"

$python = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) {
    throw "virtualenv not found at $python - create it with: python -m venv .venv"
}

$logDir = Join-Path $root "logs"
if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir | Out-Null }
$log = Join-Path $logDir "bot.log"

if ((Test-Path $log) -and ((Get-Item $log).Length -gt 10MB)) {
    Move-Item -Path $log -Destination "$log.1" -Force
}

"===== started $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') =====" |
    Out-File -FilePath $log -Append -Encoding utf8

# The redirection is done by cmd.exe on purpose.  PowerShell 5.1 would write
# the log as UTF-16 and, worse, wrap every stderr line from Python in an
# ErrorRecord - which with ErrorActionPreference=Stop kills the bot on its
# first log line.  cmd appends raw bytes, and Python already emits UTF-8.
$command = '"{0}" -m app.main >> "{1}" 2>&1' -f $python, $log
& cmd.exe /c $command
