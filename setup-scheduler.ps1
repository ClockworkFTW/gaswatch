<#
.SYNOPSIS
    Register the gaswatch Windows Task Scheduler jobs for the CURRENT USER.

.DESCRIPTION
    Creates scheduled tasks that run as the current user, only while logged on:
      gaswatch-pull-am    07:00 daily    run.cmd          (pull-all + export + alerts + dashboard)
      gaswatch-pull-pm    14:00 daily    run.cmd
      gaswatch-pull-eve   21:00 daily    run.cmd
      gaswatch-browser    23:30 daily    run-browser.cmd  (pull-all --include-browser, incl. Ruby)
      gaswatch-rates      Sun 06:30      run-rates.cmd    (pull-all --include-heavy, tariff rates)
      gaswatch-clean      Sun 06:00      clean-raw        (only with -IncludeCleanRaw)

    No administrator rights are required: the tasks use an Interactive/Limited
    principal ("run only when the user is logged on"). Each task also has
    "start when available" (catch up after a missed start) and "wake to run".
    Re-running this script updates existing tasks in place.

.PARAMETER InstallPath
    Path to the gaswatch repo - the folder containing .venv and the run*.cmd
    files. Defaults to the folder this script lives in, so if the script sits
    in the repo root you can just run it with no arguments.

.PARAMETER IncludeCleanRaw
    Also register the weekly task that prunes archived raw responses > 30 days.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\setup-scheduler.ps1

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\setup-scheduler.ps1 -InstallPath "D:\tools\gaswatch" -IncludeCleanRaw

.NOTES
    To remove everything later:
      Get-ScheduledTask gaswatch-* | Unregister-ScheduledTask -Confirm:$false
#>
[CmdletBinding()]
param(
    [string]$InstallPath,
    [switch]$IncludeCleanRaw
)

$ErrorActionPreference = "Stop"

# --- resolve the install path -----------------------------------------------
# Default to the script's own folder. Fall back to the current directory when
# $PSScriptRoot is unavailable (e.g. the script body was pasted into the shell
# or run with -Command instead of -File).
if ([string]::IsNullOrWhiteSpace($InstallPath)) {
    if (-not [string]::IsNullOrWhiteSpace($PSScriptRoot)) {
        $InstallPath = $PSScriptRoot
    } else {
        $InstallPath = (Get-Location).Path
        Write-Host "No -InstallPath and no script-file context; using current directory: $InstallPath"
    }
}
if ([string]::IsNullOrWhiteSpace($InstallPath)) {
    throw "Could not determine InstallPath. Pass -InstallPath 'C:\path\to\gaswatch' (the folder containing .venv)."
}
if (-not (Test-Path -LiteralPath $InstallPath)) {
    throw "InstallPath '$InstallPath' does not exist."
}
$InstallPath = (Resolve-Path -LiteralPath $InstallPath).Path.TrimEnd('\')

$exe = Join-Path $InstallPath ".venv\Scripts\gaswatch.exe"
if (-not (Test-Path -LiteralPath $exe)) {
    throw "gaswatch.exe not found at '$exe'.`n" +
          "Point -InstallPath at the repo root (the folder with .venv), and make sure you have run:`n" +
          "  python -m venv .venv`n" +
          "  .\.venv\Scripts\python.exe -m pip install -e `".[dev]`""
}

# runner scripts must be present (they ship in the repo; they cd to their own folder)
$requiredCmds = @("run.cmd", "run-browser.cmd", "run-rates.cmd")
foreach ($c in $requiredCmds) {
    $p = Join-Path $InstallPath $c
    if (-not (Test-Path -LiteralPath $p)) {
        throw "Runner script '$p' is missing. Pull the latest repo (it should contain run.cmd, run-browser.cmd, run-rates.cmd)."
    }
}

# make sure data\ exists for the logs the runner scripts append to
$dataDir = Join-Path $InstallPath "data"
if (-not (Test-Path -LiteralPath $dataDir)) {
    New-Item -ItemType Directory -Path $dataDir | Out-Null
    Write-Host "Created $dataDir"
}

# --- shared task configuration -----------------------------------------------
# Interactive + Limited = run only when this user is logged on, no elevation,
# no stored password -> works for a standard (non-admin) account.
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" `
                                        -LogonType Interactive -RunLevel Limited

$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -WakeToRun `
                                         -MultipleInstances IgnoreNew `
                                         -ExecutionTimeLimit (New-TimeSpan -Hours 3)

function Register-Gaswatch {
    param([string]$Name, [scriptblock]$ActionFactory, $Trigger, [string]$Desc)
    $action = & $ActionFactory
    Register-ScheduledTask -TaskName $Name -Action $action -Trigger $Trigger `
        -Principal $principal -Settings $settings -Description $Desc -Force | Out-Null
    Write-Host ("  {0,-20} {1}" -f $Name, $Desc)
}

function CmdAction([string]$cmd) {
    New-ScheduledTaskAction -Execute (Join-Path $InstallPath $cmd) -WorkingDirectory $InstallPath
}

Write-Host "Registering gaswatch tasks for $env:USERDOMAIN\$env:USERNAME (install: $InstallPath)"

Register-Gaswatch "gaswatch-pull-am"  { CmdAction "run.cmd" } `
    (New-ScheduledTaskTrigger -Daily -At "07:00") "Morning pull + export + alerts + dashboard"
Register-Gaswatch "gaswatch-pull-pm"  { CmdAction "run.cmd" } `
    (New-ScheduledTaskTrigger -Daily -At "14:00") "Midday pull + export + alerts + dashboard"
Register-Gaswatch "gaswatch-pull-eve" { CmdAction "run.cmd" } `
    (New-ScheduledTaskTrigger -Daily -At "21:00") "Evening pull + export + alerts + dashboard"
Register-Gaswatch "gaswatch-browser"  { CmdAction "run-browser.cmd" } `
    (New-ScheduledTaskTrigger -Daily -At "23:30") "Nightly pull including Ruby (Playwright)"
Register-Gaswatch "gaswatch-rates"    { CmdAction "run-rates.cmd" } `
    (New-ScheduledTaskTrigger -Weekly -DaysOfWeek Sunday -At "06:30") "Weekly tariff rate-values refresh"

if ($IncludeCleanRaw) {
    Register-Gaswatch "gaswatch-clean" { New-ScheduledTaskAction -Execute $exe -Argument "clean-raw --keep-days 30" -WorkingDirectory $InstallPath } `
        (New-ScheduledTaskTrigger -Weekly -DaysOfWeek Sunday -At "06:00") "Weekly raw-response retention (30 days)"
}

Write-Host "`nDone. Current gaswatch tasks:"
Get-ScheduledTask gaswatch-* | Select-Object TaskName, State | Format-Table -AutoSize

Write-Host @"
Next steps:
  - Test one now:   Start-ScheduledTask -TaskName gaswatch-pull-am
  - Watch the log:  Get-Content "$dataDir\pull.log" -Tail 20 -Wait
  - In Task Scheduler these run only while you are logged on (a locked screen is fine).
    Keep the machine on/awake; without admin, 'run whether logged on or not' is unavailable.
"@
