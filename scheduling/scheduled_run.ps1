# Unattended daily pipeline run, launched by Task Scheduler (see register_task.ps1): Stage 1 for
# reservoirs that are due, Stage 2, Stage 3, then publish to gh-pages.
#
# Runs at most once per calendar day. A successful run writes today's date to
# logs/last_success.txt and any later trigger that day exits immediately; a failed run leaves it
# alone, so the next trigger (next logon, or the daily time trigger) tries again.
#
# Written for Windows PowerShell 5.1, the powershell.exe that ships with Windows.

param(
    [string]$Uv = "uv",
    [string]$Project = "reservoir-ca"
)

$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot
$logDir = Join-Path $repo "logs"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null

$today = Get-Date -Format "yyyy-MM-dd"
$marker = Join-Path $logDir "last_success.txt"
if ((Test-Path $marker) -and ((Get-Content $marker -Raw).Trim() -eq $today)) { exit 0 }

$log = Join-Path $logDir "pipeline_$today.log"
function Write-Log([string]$message) {
    Add-Content -Path $log -Value "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') $message"
}

# The network can lag a logon by a minute or two; wait for DNS rather than fail the whole day.
$deadline = (Get-Date).AddMinutes(15)
while ($true) {
    try { [System.Net.Dns]::GetHostAddresses("earthengine.googleapis.com") | Out-Null; break }
    catch {
        if ((Get-Date) -gt $deadline) { Write-Log "No network after 15 minutes; giving up until next trigger."; exit 1 }
        Start-Sleep -Seconds 30
    }
}

# Keep the last 30 days of logs.
Get-ChildItem -Path $logDir -Filter "pipeline_*.log" |
    Sort-Object Name -Descending | Select-Object -Skip 30 | Remove-Item -Force

Write-Log "Starting pipeline run."
Set-Location $repo
# Unbuffered, so a hung Earth Engine call (e.g. quota "restricted mode") still leaves a readable
# log. Redirection goes through cmd: in PowerShell 5.1, redirecting a native command's stderr
# under ErrorActionPreference=Stop turns the first warning line into a terminating error.
$env:PYTHONUNBUFFERED = "1"
cmd /c "`"$Uv`" run python run_pipeline.py --project $Project --publish >> `"$log`" 2>&1"
$exitCode = $LASTEXITCODE

if ($exitCode -eq 0) {
    Set-Content -Path $marker -Value $today
    Write-Log "Pipeline run succeeded."
} else {
    Write-Log "Pipeline run failed with exit code $exitCode."
}
exit $exitCode
