$ErrorActionPreference = 'Stop'
$source = Split-Path -Parent $MyInvocation.MyCommand.Path
$install = Join-Path $env:LOCALAPPDATA 'PersonalVault'
New-Item -ItemType Directory -Force -Path $install | Out-Null
Copy-Item (Join-Path $source 'personal-vault-connector.exe') (Join-Path $install 'personal-vault-connector.exe') -Force
& (Join-Path $install 'personal-vault-connector.exe') setup
if ($LASTEXITCODE -ne 0) { throw 'Connector setup failed' }
$action = New-ScheduledTaskAction -Execute (Join-Path $install 'personal-vault-connector.exe') -Argument 'run'
$trigger = New-ScheduledTaskTrigger -AtLogOn
$settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit (New-TimeSpan -Days 3650) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 2) -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName 'Personal Vault Connector' -Action $action -Trigger $trigger -Settings $settings -Description 'Indexes only the folders selected in Personal Vault setup.' -Force | Out-Null
Start-ScheduledTask -TaskName 'Personal Vault Connector'
Write-Host "Installed. Background task: Personal Vault Connector"
Write-Host "Pause:  & '$install\personal-vault-connector.exe' pause"
Write-Host "Resume: & '$install\personal-vault-connector.exe' resume"
Write-Host "Remove:  run uninstall.ps1 from this package"
