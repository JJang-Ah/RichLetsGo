#requires -Version 7.3
param([Parameter(Mandatory)][string]$DiskPath, [string]$TimePeer)
# Inspection only. No service, power, network or clock changes.
Write-Output 'Power / sleep policy:'
powercfg /query SCHEME_CURRENT SUB_SLEEP
Write-Output 'Windows time status / source / last successful sync:'
w32tm /query /status /verbose
w32tm /query /source
Write-Output 'Configured time peers:'
w32tm /query /peers
if ($TimePeer) {
    Write-Output 'Approximate offset (read-only network measurement to supplied peer):'
    w32tm /stripchart "/computer:$TimePeer" /samples:3 /dataonly
} else { Write-Output 'Offset not measured. Supply -TimePeer with an operator-approved NTP peer.' }
Get-NetAdapter | Select-Object Name, InterfaceDescription, Status, MediaType, LinkSpeed
Get-Service -Name 'RichLetsGo-Trader','RichLetsGo-Watchdog','RichLetsGo-Dashboard' -ErrorAction SilentlyContinue
Get-Item -LiteralPath $DiskPath | Select-Object FullName, @{Name='FreeBytes';Expression={$_.PSDrive.Free}}
Get-ScheduledTaskInfo -TaskName 'RichLetsGo-SQLiteBackup' -ErrorAction SilentlyContinue
Write-Output 'Operator note: Free-running System Clock is not synchronized. Demo observed +1.2s/day; local clock_limit_ms=5000. This script changes nothing.'
