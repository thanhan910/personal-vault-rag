$ErrorActionPreference = 'SilentlyContinue'
Stop-ScheduledTask -TaskName 'Personal Vault Connector'
Unregister-ScheduledTask -TaskName 'Personal Vault Connector' -Confirm:$false
$install = Join-Path $env:LOCALAPPDATA 'PersonalVault'
Write-Host "Background task removed. Local configuration remains at $install so uninstall is recoverable."
Write-Host "To remove it too, review that folder and delete it manually. Server indexes are not deleted."
