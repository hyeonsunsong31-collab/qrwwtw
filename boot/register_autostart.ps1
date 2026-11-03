<#
  register_autostart.ps1 — make JARVIS start automatically at every logon (requirements 2, 4).

  Creates a Scheduled Task "JARVIS" that runs the supervisor (boot\jarvis_service.py) with
  pythonw.exe (no console window) the moment the user logs in, and restarts it if it fails.
  This is what makes JARVIS "permanently operate once installed".

  Usage:
    powershell -ExecutionPolicy Bypass -File register_autostart.ps1            # register
    powershell -ExecutionPolicy Bypass -File register_autostart.ps1 -Unregister
#>
param([switch]$Unregister)

$ErrorActionPreference = "Stop"
$TaskName = "JARVIS"
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)

if ($Unregister) {
  if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "JARVIS auto-start removed."
  } else {
    Write-Host "JARVIS auto-start was not registered."
  }
  return
}

# Prefer the venv's pythonw (no console); fall back to pythonw on PATH.
$Pythonw = Join-Path $Root ".venv\Scripts\pythonw.exe"
if (-not (Test-Path $Pythonw)) { $Pythonw = "pythonw.exe" }
$Script = Join-Path $Root "boot\jarvis_service.py"

$Action   = New-ScheduledTaskAction -Execute $Pythonw -Argument "`"$Script`"" -WorkingDirectory $Root
$Trigger  = New-ScheduledTaskTrigger -AtLogOn
$Principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited
$Settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
            -StartWhenAvailable -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) `
            -ExecutionTimeLimit (New-TimeSpan -Seconds 0)

Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger $Trigger `
  -Principal $Principal -Settings $Settings -Force `
  -Description "JARVIS local agentic AI — always-on background assistant." | Out-Null

Write-Host "JARVIS will now start automatically at logon."
