# Run the pipeline API + its in-process nightly job on this PC.
#
# Started by the StockDataPipeline logon Scheduled Task (install-scheduled-task.ps1),
# or by hand:
#   powershell -ExecutionPolicy Bypass -File deploy\windows\run-local.ps1
#
# Serves http://127.0.0.1:<Port> (loopback only -- Trader-Screener's default API
# base) and runs the full nightly sequence (market, political, validate) at
# PIPELINE_HOUR_UTC. Data lives in -DataDir, outside the repo, so switching
# branches never touches it. A PC asleep at the hour runs the night when it wakes
# (misfire grace) or at the next start (catch-up) -- see api.py.
#
# Secrets are read from the USER environment at start and never written to the
# task definition or the log:
#   FRED_API_KEY    -- /api/macro (FRED). Set once:  setx FRED_API_KEY <key>
#   SEC_USER_AGENT  -- SEC fair-access "<name> <email>"; falls back to
#                      EDGAR_USER_AGENT, which Trader-Screener's scrubber uses.
param(
    [string]$DataDir = (Join-Path $env:USERPROFILE '.stock-data-pipeline\data'),
    [int]$Port = 8000
)

$ErrorActionPreference = 'Stop'
$repo = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$logDir = Join-Path (Split-Path $DataDir -Parent) 'logs'
New-Item -ItemType Directory -Force -Path $DataDir, $logDir | Out-Null
$log = Join-Path $logDir 'api.log'

function UserEnv([string]$name) {
    $v = [Environment]::GetEnvironmentVariable($name, 'Process')
    if (-not $v) { $v = [Environment]::GetEnvironmentVariable($name, 'User') }
    return $v
}

$env:DATA_DIR = $DataDir
$env:DB_PATH = Join-Path $DataDir 'stocks.db'
$env:POL_DB_PATH = Join-Path $DataDir 'politicians.db'
$env:RUN_SCHEDULER = '1'
$env:PYTHONIOENCODING = 'utf-8'
$env:FRED_API_KEY = UserEnv 'FRED_API_KEY'
$sec = UserEnv 'SEC_USER_AGENT'
if (-not $sec) { $sec = UserEnv 'EDGAR_USER_AGENT' }
$env:SEC_USER_AGENT = $sec

$python = (Get-Command python -ErrorAction Stop).Source
$stamp = (Get-Date).ToString('s')
Add-Content -Path $log -Value ("`n==== {0} start: port {1}, data {2}, FRED_API_KEY {3}, SEC_USER_AGENT {4}" -f `
    $stamp, $Port, $DataDir, [bool]$env:FRED_API_KEY, [bool]$env:SEC_USER_AGENT)

Set-Location $repo
# Keep the API up. The Scheduled Task's "restart on failure" only covers a failed
# LAUNCH, not the program exiting later: on 2026-10-04 uvicorn ended mid-run with
# no traceback and nothing restarted it until the next logon. So restart it here,
# backing off 30s -> 5min if it keeps dying fast, and log every exit.
$delay = 30
while ($true) {
    $t0 = Get-Date
    # cmd handles the redirect so uvicorn's stderr lands in the log as plain text.
    # One worker only: every worker would start its own nightly job.
    & cmd.exe /c "`"$python`" -m uvicorn api:app --host 127.0.0.1 --port $Port --workers 1 >> `"$log`" 2>&1"
    $code = $LASTEXITCODE
    $ran = ((Get-Date) - $t0).TotalSeconds
    if ($ran -gt 600) { $delay = 30 } else { $delay = [Math]::Min($delay * 2, 300) }
    Add-Content -Path $log -Value ("==== {0} uvicorn exited (code {1}) after {2:N0}s -- restarting in {3}s" -f `
        (Get-Date).ToString('s'), $code, $ran, $delay)
    Start-Sleep -Seconds $delay
}
