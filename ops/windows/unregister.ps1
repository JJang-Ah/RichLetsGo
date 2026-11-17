#requires -Version 7.3
param([Parameter(Mandatory)][string]$Nssm)
$ErrorActionPreference = 'Stop'
$Nssm = (Resolve-Path -LiteralPath $Nssm).Path
foreach ($name in @('RichLetsGo-Dashboard', 'RichLetsGo-Trader', 'RichLetsGo-Watchdog')) {
    $service = Get-Service -Name $name -ErrorAction SilentlyContinue
    if ($service) {
        if ($service.Status -ne 'Stopped') { throw "Stop $name through SCM and verify before removal." }
        & $Nssm remove $name confirm
        if ($LASTEXITCODE -ne 0) { throw 'NSSM removal failed.' }
    }
}
if (Get-ScheduledTask -TaskName 'RichLetsGo-SQLiteBackup' -ErrorAction SilentlyContinue) {
    Unregister-ScheduledTask -TaskName 'RichLetsGo-SQLiteBackup' -Confirm:$false
}
Write-Host 'Service definitions removed. Data, logs, backups and start ledgers retained.'
