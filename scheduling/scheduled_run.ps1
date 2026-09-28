# Unattended daily pipeline run, launched by Task Scheduler (see register_task.ps1): Stage 1 for
# reservoirs that are due, Stage 2, Stage 3, publish to gh-pages, then commit the updated data
# files and push master.
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
# Native commands run through cmd for their redirection: in PowerShell 5.1, redirecting a native
# command's stderr under ErrorActionPreference=Stop turns the first warning line (Python
# warnings, git's progress output) into a terminating error.
function Invoke-Logged([string]$commandLine) {
    cmd /c "$commandLine >> `"$log`" 2>&1"
    return $LASTEXITCODE
}

# Unbuffered, so a hung Earth Engine call (e.g. quota "restricted mode") still leaves a readable
# log.
$env:PYTHONUNBUFFERED = "1"
$exitCode = Invoke-Logged "`"$Uv`" run python run_pipeline.py --project $Project --publish"
if ($exitCode -ne 0) {
    Write-Log "Pipeline run failed with exit code $exitCode."
    exit $exitCode
}
Set-Content -Path $marker -Value $today
Write-Log "Pipeline run succeeded."

# Commit only the files the pipeline writes, so anything else being edited by hand in the working
# tree is never swept into an automated commit. A failed commit or push doesn't undo the day's
# run: the data stays in the working tree and the next run's commit/push picks it up.
$dataPaths = "reservoirs/ProcessedImagery.csv reservoirs/timeseries reservoirs/site_assets/representative"
$branch = (git rev-parse --abbrev-ref HEAD).Trim()
if ($branch -ne "master") {
    Write-Log "On branch '$branch', not master; skipping commit and push."
    exit 0
}
Invoke-Logged "git add -- $dataPaths" | Out-Null
git diff --cached --quiet -- $dataPaths.Split(" ")
if ($LASTEXITCODE -eq 0) {
    Write-Log "No data changes to commit."
} elseif ((Invoke-Logged "git commit --quiet -m `"Scheduled pipeline run $today`" -- $dataPaths") -ne 0) {
    Write-Log "Commit failed; data left uncommitted."
    exit 0
} else {
    Write-Log "Committed data updates."
}
if ((Invoke-Logged "git push --quiet origin master") -eq 0) {
    Write-Log "Pushed master."
} else {
    Write-Log "Push failed; commits stay local until the next successful push."
}
exit 0
