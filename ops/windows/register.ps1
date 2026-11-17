#requires -Version 7.3
param(
    [Parameter(Mandatory)][string]$Nssm,
    [Parameter(Mandatory)][string]$Python,
    [Parameter(Mandatory)][string]$Config,
    [Parameter(Mandatory)][string]$Account,
    [Parameter(Mandatory)][string]$ServiceUser,
    [Parameter(Mandatory)][string]$LogDirectory,
    [string]$DashboardPath,
    [string]$BackupTime = '03:15'
)
$ErrorActionPreference = 'Stop'
if (-not (Test-Path -LiteralPath $Nssm -PathType Leaf)) {
    Write-Host 'Install NSSM 2.24+ from https://nssm.cc/download and supply -Nssm.'
    exit 1
}
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
if (-not ([Security.Principal.WindowsPrincipal]$identity).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'Run this registration script in an elevated PowerShell.'
}
$Nssm = (Resolve-Path -LiteralPath $Nssm).Path
$Python = (Resolve-Path -LiteralPath $Python).Path
$Config = (Resolve-Path -LiteralPath $Config).Path
$RestartDelaySeconds = & $Python -c 'import sys; from pathlib import Path; from richletsgo_watchdog.config import load; print(load(Path(sys.argv[1])).restart_delay)' $Config
if ($LASTEXITCODE -ne 0) { throw 'Watchdog configuration invalid.' }
$RestartDelaySeconds = [int]$RestartDelaySeconds
$LogDirectory = [IO.Path]::GetFullPath($LogDirectory)
New-Item -ItemType Directory -Path $LogDirectory -Force | Out-Null
function Invoke-Nssm([string[]]$Arguments) {
    & $Nssm @Arguments
    if ($LASTEXITCODE -ne 0) { throw 'NSSM configuration failed.' }
}
# Use Python's Windows quoting, including spaces/backslashes. No secret arguments.
function Arguments-For([string]$Module, [string]$Role) {
    $items = @('-m', $Module)
    if ($Role) { $items += $Role }
    $items += @('--config', $Config, '--account', $Account)
    $encoded = ConvertTo-Json -Compress -InputObject $items
    $value = $encoded | & $Python -c 'import json,sys,subprocess; print(subprocess.list2cmdline(json.load(sys.stdin)))'
    if ($LASTEXITCODE -ne 0) { throw 'Argument construction failed.' }
    return $value
}
$roles = @('Trader', 'Watchdog')
if ($DashboardPath -and (Test-Path -LiteralPath $DashboardPath)) { $roles += 'Dashboard' }
else { Write-Host 'Dashboard path missing; skipping Dashboard service only.' }
foreach ($role in $roles) {
    $name = "RichLetsGo-$role"
    if (Get-Service -Name $name -ErrorAction SilentlyContinue) { throw "Service already exists: $name" }
    Invoke-Nssm @('install', $name, $Python)
    # Disabled until the operator configures the credential-owning user in NSSM GUI.
    Invoke-Nssm @('set', $name, 'Start', 'SERVICE_DISABLED')
    Invoke-Nssm @('set', $name, 'AppParameters', (Arguments-For 'richletsgo_watchdog.service' $role))
    Invoke-Nssm @('set', $name, 'AppDirectory', (Split-Path -Parent $Config))
    # NSSM 2.24 has no AppKillProcessTree. Keep its default: on forced stop it ends only this
    # service's own descendants. The venv python.exe is a launcher with the real interpreter as
    # its child, so killing only the launcher PID could orphan a second Trader.
    Invoke-Nssm @('set', $name, 'AppStopMethodConsole', '30000')
    Invoke-Nssm @('set', $name, 'AppExit', 'Default', 'Restart')
    Invoke-Nssm @('set', $name, 'AppExit', '0', 'Exit')
    Invoke-Nssm @('set', $name, 'AppExit', '78', 'Exit')
    Invoke-Nssm @('set', $name, 'AppRestartDelay', ([string]($RestartDelaySeconds * 1000)))
    Invoke-Nssm @('set', $name, 'AppStdout', (Join-Path $LogDirectory "$role.stdout.log"))
    Invoke-Nssm @('set', $name, 'AppStderr', (Join-Path $LogDirectory "$role.stderr.log"))
    Invoke-Nssm @('set', $name, 'AppRotateFiles', '1')
    Invoke-Nssm @('set', $name, 'AppRotateOnline', '1')
    Invoke-Nssm @('set', $name, 'AppRotateBytes', '10485760')
    Write-Host "In NSSM Log on tab set $ServiceUser, enter password locally, then Save. Do not start yet."
    Invoke-Nssm @('edit', $name)
    $service = Get-CimInstance Win32_Service -Filter "Name='$name'"
    if ($service.StartName -ne $ServiceUser) { throw 'Service user mismatch; service remains disabled.' }
    Invoke-Nssm @('set', $name, 'Start', 'SERVICE_AUTO_START')
}
$backupArgs = Arguments-For 'richletsgo.storage' 'backup'
$action = New-ScheduledTaskAction -Execute $Python -Argument $backupArgs -WorkingDirectory (Split-Path -Parent $Config)
$trigger = New-ScheduledTaskTrigger -Daily -At $BackupTime
# Local SQLite backup needs no network credentials. Run even when the user is logged out.
$principal = New-ScheduledTaskPrincipal -UserId $ServiceUser -LogonType S4U -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName 'RichLetsGo-SQLiteBackup' -Action $action -Trigger $trigger -Principal $principal -Settings $settings | Out-Null
Write-Host 'Registered only; inspect settings, then start services manually. Backup retains 14 days via storage.backup.'
